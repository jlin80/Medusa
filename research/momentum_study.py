#!/usr/bin/env python
"""Estudio de momentum en Polymarket: ¿el CAMBIO reciente del precio predice el
resultado mas alla del NIVEL del precio?

    docker compose exec engine python research/momentum_study.py [n_mercados]

POR QUE. El estudio de calibracion demostro que el NIVEL de precio ya es la
probabilidad (mercado bien calibrado) -> no hay edge en comprar barato/caro per
se. La siguiente hipotesis razonable es el momentum: si los mercados
SUB-reaccionan a las noticias (documentado en otros mercados de prediccion),
un movimiento reciente deberia continuar, y comprar la direccion del movimiento
tendria edge. Si SOBRE-reaccionan, lo contrario. Si son eficientes, nada.

METODO. Mercados resueltos con historico horario. Por mercado, DOS lecturas:
  p48 = precio a T-48h del final,  p24 = precio a T-24h del final.
  momentum = p24 - p48  ->  UP (>+0.02) / DOWN (<-0.02) / FLAT.
La pregunta: dentro del MISMO nivel de precio p24 (estratificado por cubos,
para no confundir momentum con nivel), ¿los UP resuelven SI mas a menudo de lo
que p24 promete, y los DOWN menos? Eso seria continuacion (sub-reaccion).

ESTADISTICA. Por cubo y grupo: exceso = %real_SI - precio_medio, IC de Wilson.
Resumen estratificado (Mantel-Haenszel-ish): delta_i = exceso_UP - exceso_DOWN
por cubo, combinado con pesos 1/var; z global. Sin significancia global, no hay
señal. Y aunque la haya: el efecto debe superar ~0.02-0.03 de coste round-trip
para ser dinero y no una curiosidad.

Mismo dedup y mismos sesgos declarados que calibration_study.py.
"""

from __future__ import annotations

import asyncio
import math
import sys
from collections import defaultdict

sys.path.insert(0, "/app")

import httpx  # noqa: E402

from research.calibration_study import (  # noqa: E402
    BUCKETS, CONCURRENCY, MIN_BUCKET, bucket_of, fetch_closed_markets,
    fetch_history, wilson_ci,
)

# |p24-p48| minimo para contar como movimiento. Configurable por CLI (argv[2])
# porque el primer pase con 0.02 mando a FLAT a 342 de 563 mercados y dejo los
# estratos sin potencia: con ~0.005 se testea casi el puro signo del movimiento.
MOM_THRESHOLD = 0.02
H24, H48 = 24, 48


def group_of(mom: float) -> str:
    if mom > MOM_THRESHOLD:
        return "UP"
    if mom < -MOM_THRESHOLD:
        return "DOWN"
    return "FLAT"


def cell_stats(rows: list[tuple[float, float]]) -> dict:
    n = len(rows)
    price = sum(p for p, _ in rows) / n
    actual = sum(o for _, o in rows) / n
    lo, hi = wilson_ci(sum(o for _, o in rows), n)
    return {"n": n, "price": price, "actual": actual,
            "excess": actual - price, "lo": lo, "hi": hi,
            "sig": not (lo <= price <= hi)}


async def main() -> int:
    global MOM_THRESHOLD
    want = int(sys.argv[1]) if len(sys.argv) > 1 else 3000
    if len(sys.argv) > 2:
        MOM_THRESHOLD = float(sys.argv[2])
    print("=" * 74)
    print("MEDUSA - ESTUDIO DE MOMENTUM (continuacion vs reversion vs nada)")
    print("=" * 74)
    print(f"  umbral de momentum: ±{MOM_THRESHOLD}")

    async with httpx.AsyncClient(timeout=30.0) as client:
        markets = await fetch_closed_markets(client, want)
        print(f"\n  mercados resueltos (dedup): {len(markets)}")

        sem = asyncio.Semaphore(CONCURRENCY)
        samples: list[tuple[float, float, float]] = []  # (p24, momentum, outcome)

        async def one(m: dict) -> None:
            async with sem:
                hist = await fetch_history(client, m["yes_token"])
            if len(hist) <= H48:
                return
            try:
                p24 = float(hist[-H24]["p"])
                p48 = float(hist[-H48]["p"])
            except (KeyError, TypeError, ValueError):
                return
            if 0.01 <= p24 <= 0.99 and 0.01 <= p48 <= 0.99:
                samples.append((p24, p24 - p48, m["outcome"]))

        batch = 40
        for i in range(0, len(markets), batch):
            await asyncio.gather(*(one(m) for m in markets[i:i + batch]))
            print(f"  histórico: {min(i + batch, len(markets))}/{len(markets)}...", end="\r")

    print(f"\n  mercados con p24 y p48 usables: {len(samples)}")
    n_up = sum(1 for _, mm, _ in samples if group_of(mm) == "UP")
    n_dn = sum(1 for _, mm, _ in samples if group_of(mm) == "DOWN")
    n_fl = len(samples) - n_up - n_dn
    print(f"  UP: {n_up} | DOWN: {n_dn} | FLAT: {n_fl} (umbral ±{MOM_THRESHOLD})")

    # ---- tabla por cubo de p24 x grupo de momentum --------------------------
    cells: dict = defaultdict(list)
    for p24, mom, out in samples:
        b = bucket_of(p24)
        if b:
            cells[(b, group_of(mom))].append((p24, out))

    print(f"\n  {'cubo p24':<12}{'grupo':<7}{'n':>6}{'precio':>9}{'% real':>9}"
          f"{'exceso':>9}  {'IC95 Wilson':<20}{'sig':>4}")
    print("  " + "-" * 76)
    per_bucket: dict = {}
    for b in BUCKETS:
        for grp in ("UP", "DOWN", "FLAT"):
            rows = cells.get((b, grp))
            if not rows or len(rows) < MIN_BUCKET:
                continue
            st = cell_stats(rows)
            per_bucket.setdefault(b, {})[grp] = st
            print(f"  {b[0]:.2f}-{b[1]:.2f}  {grp:<7}{st['n']:>6}{st['price']:>9.3f}"
                  f"{st['actual']:>9.3f}{st['excess']:>+9.4f}  "
                  f"[{st['lo']:.3f},{st['hi']:.3f}]    {'SI' if st['sig'] else 'no'}")

    # ---- resumen estratificado: exceso(UP) - exceso(DOWN) por cubo ----------
    num = den = 0.0
    strata = 0
    for b, groups in per_bucket.items():
        if "UP" not in groups or "DOWN" not in groups:
            continue
        u, d = groups["UP"], groups["DOWN"]
        delta = u["excess"] - d["excess"]
        var = (max(u["actual"] * (1 - u["actual"]), 1e-9) / u["n"]
               + max(d["actual"] * (1 - d["actual"]), 1e-9) / d["n"])
        if var <= 0:
            continue
        w = 1.0 / var
        num += w * delta
        den += w
        strata += 1

    print("\n" + "=" * 78)
    print("VEREDICTO")
    print("=" * 78)
    if den <= 0 or strata == 0:
        print("\n  Sin cubos con UP y DOWN suficientes a la vez. NO SE CONCLUYE NADA.")
        print("  Reintentar con mas mercados.")
        return 0

    delta = num / den
    se = math.sqrt(1.0 / den)
    z = delta / se if se > 0 else 0.0
    print(f"\n  Estratos usados                 : {strata}")
    print(f"  Delta continuacion (UP - DOWN)  : {delta:+.4f}  (SE {se:.4f}, z = {z:+.2f})")
    print(f"  IC95                            : [{delta - 1.96*se:+.4f}, {delta + 1.96*se:+.4f}]")

    if abs(z) < 1.96:
        print("\n  -> SIN señal significativa. El movimiento reciente del precio NO")
        print("     añade informacion sobre el resultado mas alla del propio nivel de")
        print("     precio. Ni continuacion ni reversion explotables a este horizonte.")
    elif z > 0:
        print("\n  -> CONTINUACION significativa (sub-reaccion): los que subieron")
        print("     resuelven SI mas de lo que su precio promete.")
        print(f"     PERO: el efecto es {delta:+.4f} y el coste round-trip medido es")
        print("     ~0.02-0.03. Si el efecto no lo supera con margen, no es dinero.")
        print("     Y esto es in-sample: exigiria validacion out-of-sample + paper.")
    else:
        print("\n  -> REVERSION significativa (sobre-reaccion): los que subieron")
        print("     resuelven SI menos de lo que su precio promete. Mismas cautelas")
        print("     de coste y validacion que arriba.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
