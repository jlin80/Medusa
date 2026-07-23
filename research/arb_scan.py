#!/usr/bin/env python
"""Escaneo de arbitraje intra-mercado en Polymarket, con libros REALES de ahora.

    docker compose exec engine python research/arb_scan.py [n_mercados]

LA IDEA. En un mercado binario, comprar 1 share de YES y 1 de NO garantiza
cobrar exactamente $1 en la resolucion, gane quien gane. Si se pueden comprar
ambos por menos de $1 (ask_YES + ask_NO < 1), la diferencia es ganancia SIN
riesgo direccional. Es el unico "edge" que no necesita predecir nada.

Tambien se mide el lado contrario (bid_YES + bid_NO > 1: vender ambos), que
requiere tener las shares o mintearlas ($1 -> 1 YES + 1 NO en Polymarket).

QUE ESPERAR. Este arbitraje existe pero es competitivo: bots dedicados lo
aspiran en segundos. El proposito del escaneo es medir con datos si a NUESTRO
alcance (scan cada 60s, sin colocation) queda algo, y de que tamaño, antes de
decidir si vale la pena implementarlo en el bot.

Salida: distribucion de (ask_YES + ask_NO) - 1, oportunidades bajo distintos
umbrales, y la profundidad ejecutable (cuanto dinero cabe) de cada una.
"""

from __future__ import annotations

import asyncio
import sys

sys.path.insert(0, "/app")

import httpx  # noqa: E402

from medusa.config import get_settings  # noqa: E402
from medusa.data.polymarket.client import PolymarketClient, _to_list  # noqa: E402

CONCURRENCY = 6


async def fetch_active(client: httpx.AsyncClient, want: int) -> list[dict]:
    s = get_settings()
    out, offset = [], 0
    while len(out) < want:
        r = await client.get(f"{s.gamma_api_url.rstrip('/')}/markets", params={
            "closed": "false", "active": "true", "limit": "100",
            "offset": str(offset), "order": "volume24hr", "ascending": "false",
        })
        if r.status_code != 200:
            break
        rows = r.json()
        if not rows:
            break
        offset += len(rows)
        for m in rows:
            tokens = _to_list(m.get("clobTokenIds"))
            if len(tokens) == 2:
                out.append({
                    "q": m.get("question", ""),
                    "yes": str(tokens[0]),
                    "no": str(tokens[1]),
                    "vol": float(m.get("volume24hr") or 0),
                })
            if len(out) >= want:
                break
    return out


async def main() -> int:
    want = int(sys.argv[1]) if len(sys.argv) > 1 else 120
    print("=" * 74)
    print("MEDUSA - ESCANEO DE ARBITRAJE INTRA-MERCADO (libros reales, ahora)")
    print("=" * 74)

    pm = PolymarketClient()
    results = []
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            markets = await fetch_active(client, want)
            print(f"\n  mercados activos escaneados: {len(markets)}")

            sem = asyncio.Semaphore(CONCURRENCY)

            async def one(m: dict) -> None:
                async with sem:
                    try:
                        by = await pm.fetch_order_book(m["yes"])
                        bn = await pm.fetch_order_book(m["no"])
                    except Exception:  # noqa: BLE001
                        return
                if not (by.asks and bn.asks and by.bids and bn.bids):
                    return
                ask_sum = by.best_ask + bn.best_ask
                bid_sum = by.best_bid + bn.best_bid
                # Profundidad ejecutable al mejor nivel (USD del lado mas fino).
                depth_buy = min(by.asks[0][0] * by.asks[0][1],
                                bn.asks[0][0] * bn.asks[0][1])
                results.append({
                    "q": m["q"], "vol": m["vol"], "ask_sum": ask_sum,
                    "bid_sum": bid_sum, "depth_buy": depth_buy,
                })

            batch = 30
            for i in range(0, len(markets), batch):
                await asyncio.gather(*(one(m) for m in markets[i:i + batch]))
                print(f"  libros: {min(i + batch, len(markets))}/{len(markets)}...", end="\r")
    finally:
        await pm.close()

    print(f"\n  mercados con ambos libros: {len(results)}")
    if not results:
        print("  Sin datos. Abortado.")
        return 1

    edges = sorted(r["ask_sum"] - 1.0 for r in results)
    n = len(edges)
    print("\n  Distribucion de (ask_YES + ask_NO) - 1  [negativo = arb de compra]:")
    for pct_label, idx in (("min", 0), ("p5", n // 20), ("p25", n // 4),
                           ("mediana", n // 2), ("p75", 3 * n // 4), ("max", n - 1)):
        print(f"    {pct_label:<8}: {edges[idx]:+.4f}")

    for thr in (0.0, -0.002, -0.005, -0.01):
        hits = [r for r in results if r["ask_sum"] - 1.0 < thr]
        print(f"\n  Oportunidades con edge < {thr:+.3f}: {len(hits)}")
        for r in sorted(hits, key=lambda x: x["ask_sum"])[:8]:
            print(f"    {r['ask_sum']-1.0:+.4f} | profundidad ~${r['depth_buy']:.0f} | "
                  f"vol24h ${r['vol']/1000:.0f}k | {r['q'][:58]}")

    sell_hits = [r for r in results if r["bid_sum"] - 1.0 > 0.005]
    print(f"\n  Lado venta (bid_YES + bid_NO > 1.005, requiere mint): {len(sell_hits)}")
    for r in sorted(sell_hits, key=lambda x: -x["bid_sum"])[:8]:
        print(f"    {r['bid_sum']-1.0:+.4f} | vol24h ${r['vol']/1000:.0f}k | {r['q'][:58]}")

    print("\n" + "=" * 78)
    print("LECTURA")
    print("=" * 78)
    buy_arbs = [e for e in edges if e < -0.002]
    if not buy_arbs:
        print("\n  Ahora mismo NO hay arbitraje de compra ejecutable (>0.2% tras tick).")
        print("  Lo esperable con bots dedicados compitiendo. Correr varias veces a")
        print("  distintas horas antes de concluir; una sola foto no es un veredicto.")
    else:
        print(f"\n  HAY {len(buy_arbs)} candidatos. Revisar profundidad: un arb de 1% con")
        print("  $20 de profundidad son $0.20 de ganancia; no paga ni el esfuerzo.")
        print("  Si esto se repite en varios escaneos con profundidad decente, valdria")
        print("  implementar el detector en el scanner del bot.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
