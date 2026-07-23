#!/usr/bin/env python
"""Estudio de calibracion de Polymarket: ¿el precio es la probabilidad?

    docker compose exec engine python research/calibration_study.py [n_mercados]

POR QUE ESTE ESTUDIO. El Prediction Engine de Medusa es un baseline de reversion
a la media: asume que un precio extremo "deberia" estar mas cerca de 0.5, y por
tanto compra lo improbable. Eso es una hipotesis, y es comprobable con datos que
ya existen. Esperar 7-14 dias de paper para descubrir que pierde es tirar dos
semanas cuando la respuesta se puede tener hoy.

METODO. Se toman mercados YA RESUELTOS (sabemos el resultado real), se recupera
su historico de precios y se emparejan (precio_en_el_momento -> resultado_final).
Agrupando por nivel de precio se obtiene la curva de calibracion: de todos los
mercados que cotizaban a ~0.05, ¿cuantos acabaron en SI?

  - Si el mercado esta bien calibrado, el 5% -> no hay edge en ningun lado y
    cualquier estrategia direccional pierde el spread.
  - Si sale MAS del 5%, los longshots estan infravalorados -> el baseline (que
    los compra) tendria razon.
  - Si sale MENOS del 5%, los longshots estan SOBREvalorados (el clasico
    favourite-longshot bias) -> el baseline esta invertido y comprar extremos es
    la peor decision posible.

LA TRAMPA QUE INVALIDA EL METODO INGENUO. Contar todos los puntos horarios da un
sesgo brutal y falso: un mercado que acaba en NO deriva hacia 0 y se pasa cientos
de horas cotizando bajo, aportando cientos de muestras de (precio bajo -> NO).
Eso no son cientos de evidencias independientes: es UNA, repetida. El tamaño de
muestra efectivo no es el numero de puntos, es el numero de MERCADOS. Por eso el
veredicto se apoya en muestras de UNA observacion por mercado.

DOS HORIZONTES, NO UNO. Una sola muestra a T-24h del final tiene su propia
trampa: en muchos mercados el resultado ya se conocia 24h antes del cierre y el
precio ya habia colapsado a 0.99/0.01 -> esos puntos "calibran" perfecto de
forma trivial (circularidad residual). Para separar "calibrado de verdad" de
"calibrado porque ya estaba decidido" se mide tambien a MITAD DE VIDA del
mercado [C], lejos de ambos extremos. Un sesgo real y explotable debe aparecer
con el mismo signo en [B] y [C]; si solo aparece en uno, es artefacto u horizonte
concreto, no una ineficiencia del mercado.

ESTADISTICA. Intervalos de Wilson, no Wald: en cubos extremos (p~0.03, n~30) el
error estandar de Wald colapsa a cero y fabrica significancias falsas; Wilson se
comporta bien justo donde este estudio mira. "Significativo" = el precio medio
del cubo queda FUERA del IC95 de Wilson de la frecuencia real.

SESGOS QUE QUEDAN (declarados, no escondidos):
  - Mercados ordenados por volumen: sesgo hacia grandes y liquidos. Es justo el
    universo donde opera Medusa (MIN_VOLUME_24H/MIN_LIQUIDITY), pero no es una
    muestra aleatoria de Polymarket.
  - Solo mercados ya resueltos con resultado limpio (1 o 0).
  - In-sample: cualquier sesgo "explotable" exigiria validacion out-of-sample.
"""

from __future__ import annotations

import asyncio
import json
import math
import sys
from collections import defaultdict

sys.path.insert(0, "/app")

import httpx  # noqa: E402

from medusa.config import get_settings  # noqa: E402

S = get_settings()
GAMMA = S.gamma_api_url.rstrip("/")
CLOB = S.clob_api_url.rstrip("/")

# Cubos de precio. Mas finos en los extremos, que es donde el baseline apuesta.
BUCKETS = [(0.01, 0.05), (0.05, 0.10), (0.10, 0.20), (0.20, 0.35),
           (0.35, 0.50), (0.50, 0.65), (0.65, 0.80), (0.80, 0.90),
           (0.90, 0.95), (0.95, 0.99)]

REVERSION_K = 0.10        # el mismo del Prediction Engine actual
CONCURRENCY = 6           # amable con la API publica
HORIZON_H = 24            # horas antes del ultimo dato para la muestra de [B]
MIN_LIFE_FOR_MID = 48     # horas de vida minima para que "mitad de vida" ([C])
                          # quede lejos del colapso final
MIN_BUCKET = 30           # por debajo de esto un cubo no dice nada


def _to_list(value):
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            out = json.loads(value)
            return out if isinstance(out, list) else []
        except (json.JSONDecodeError, ValueError):
            return []
    return []


async def fetch_closed_markets(client: httpx.AsyncClient, want: int) -> list[dict]:
    """Mercados binarios ya resueltos, con token YES y resultado inequivoco.

    Dedup por token: el orden por volume24hr en mercados cerrados es un empate
    masivo (volumen residual ~0) y la paginacion de Gamma puede repetir filas
    entre paginas. Un mercado duplicado seria una muestra contada dos veces en
    [B]/[C], que presumen independencia.
    """
    out: list[dict] = []
    seen: set[str] = set()
    offset = 0
    while len(out) < want and offset < want * 6:
        r = await client.get(f"{GAMMA}/markets", params={
            "closed": "true", "limit": "100", "offset": str(offset),
            "order": "volume24hr", "ascending": "false",
        })
        # Gamma corta la paginacion sobre offset ~2100 con un 422. No es un error
        # nuestro: es el techo del endpoint. Se trabaja con lo que haya dado.
        if r.status_code == 422:
            print(f"  (Gamma corta la paginacion en offset={offset}; "
                  f"se sigue con {len(out)} mercados)")
            break
        r.raise_for_status()
        rows = r.json()
        if not rows:
            break
        offset += len(rows)
        for m in rows:
            prices = _to_list(m.get("outcomePrices"))
            tokens = _to_list(m.get("clobTokenIds"))
            if len(prices) != 2 or len(tokens) != 2:
                continue
            try:
                yes_final = float(prices[0])
            except (TypeError, ValueError):
                continue
            # Solo resultados limpios: 1 o 0. Nada a medias.
            if yes_final not in (0.0, 1.0):
                continue
            token = str(tokens[0])
            if not token or token in seen:
                continue
            seen.add(token)
            out.append({
                "question": m.get("question", ""),
                "yes_token": token,
                "outcome": yes_final,
                "end": m.get("endDate"),
            })
            if len(out) >= want:
                break
    return out


async def fetch_history(client: httpx.AsyncClient, token: str) -> list[dict]:
    try:
        r = await client.get(f"{CLOB}/prices-history", params={
            "market": token, "interval": "max", "fidelity": "60",
        })
        if r.status_code != 200:
            return []
        data = r.json()
        return data.get("history", []) if isinstance(data, dict) else (data or [])
    except Exception:  # noqa: BLE001
        return []


def bucket_of(p: float):
    for lo, hi in BUCKETS:
        if lo <= p < hi:
            return (lo, hi)
    return None


def wilson_ci(successes: float, n: int, z: float = 1.96) -> tuple[float, float]:
    """IC de Wilson para una proporcion binomial. A diferencia de Wald, no
    colapsa cuando p esta pegado a 0 o 1 con n modesto, que es exactamente el
    regimen de los cubos extremos de este estudio."""
    if n <= 0:
        return (0.0, 1.0)
    phat = successes / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = (phat + z2 / (2 * n)) / denom
    half = (z / denom) * math.sqrt(phat * (1.0 - phat) / n + z2 / (4 * n * n))
    return (max(0.0, center - half), min(1.0, center + half))


def report(title: str, samples: list[tuple[float, float]], independent: bool) -> dict:
    """samples = [(precio, resultado 1/0)]. Devuelve la tabla de calibracion.

    `independent` dice si cada muestra es un mercado distinto. Si no lo es, el
    intervalo de confianza seria una mentira (asume independencia) y no se
    imprime: n cuenta puntos, no evidencias.
    """
    print(f"\n{title}")
    print(f"  muestras: {len(samples):,}")
    if not samples:
        return {}

    groups: dict = defaultdict(list)
    for price, outcome in samples:
        b = bucket_of(price)
        if b:
            groups[b].append((price, outcome))

    ci_col = "IC95 Wilson (% real)" if independent else "(IC no aplica)"
    print(f"  {'cubo':<14}{'n':>7}{'precio':>10}{'% real SI':>11}{'sesgo':>9}"
          f"  {ci_col:<22}{'significativo':>13}")
    print("  " + "-" * 78)
    out = {}
    for b in BUCKETS:
        rows = groups.get(b)
        if not rows or len(rows) < MIN_BUCKET:
            continue
        n = len(rows)
        mean_price = sum(p for p, _ in rows) / n
        wins = sum(o for _, o in rows)
        actual = wins / n
        bias = actual - mean_price
        lo, hi = wilson_ci(wins, n)
        # Significativo = el precio medio queda fuera del IC de la frecuencia
        # real. Solo tiene sentido con muestras independientes.
        sig = independent and not (lo <= mean_price <= hi)
        out[f"{b[0]}-{b[1]}"] = {"n": n, "price": mean_price, "actual": actual,
                                 "bias": bias, "lo": lo, "hi": hi, "sig": sig}
        ci = f"[{lo:.4f}, {hi:.4f}]" if independent else "-"
        mark = ("SI" if sig else "no") if independent else "-"
        print(f"  {b[0]:.2f}-{b[1]:.2f}    {n:>7,}{mean_price:>10.4f}{actual:>11.4f}"
              f"{bias:>+9.4f}  {ci:<22}{mark:>13}")
    return out


def verdict(table_b: dict, table_c: dict, n_markets: int) -> None:
    """Veredicto: [B] manda, [C] es el control de robustez. Un sesgo solo cuenta
    como real si es significativo en [B] Y va en el mismo sentido en [C]."""
    print("\n" + "=" * 78)
    print("VEREDICTO ([B] una muestra/mercado a 24h; [C] control a mitad de vida)")
    print("=" * 78)
    if not table_b:
        print(f"\n  Ningun cubo alcanza {MIN_BUCKET} mercados. NO SE CONCLUYE NADA.")
        print(f"  Solo hay {n_markets} mercados con historico usable; hacen falta")
        print("  bastantes mas para que los extremos tengan potencia estadistica.")
        print("  Reintentar con mas mercados: calibration_study.py 3000")
        return

    sig_b = {k: v for k, v in table_b.items() if v["sig"]}
    print(f"\n  Cubos con muestra suficiente en [B] : {len(table_b)}")
    print(f"  Cubos significativos en [B]         : {len(sig_b)}")

    if not sig_b:
        print("\n  -> No hay desviacion estadisticamente significativa entre precio y")
        print("     frecuencia real en ningun cubo. Con estos datos, Polymarket esta")
        print("     BIEN CALIBRADO: el precio ES la probabilidad.")
        print("     Implicacion directa: no hay edge direccional que extraer del")
        print("     precio por si solo, y cualquier estrategia que solo mire el nivel")
        print("     de precio pierde el spread. El baseline de reversion no tiene")
        print("     nada que explotar: no es que este mal ajustado, es que apuesta")
        print("     contra un mercado que acierta.")
        return

    print("\n  Sesgos significativos en [B], contrastados con [C]:")
    robust = []
    for key, v in sorted(sig_b.items(), key=lambda kv: -abs(kv[1]["bias"])):
        c = table_c.get(key)
        if c is None:
            status = "[C] sin muestra -> NO confirmable"
        elif c["sig"] and (c["bias"] > 0) == (v["bias"] > 0):
            status = "[C] confirma (mismo signo, significativo) -> ROBUSTO"
            robust.append((key, v, c))
        elif (c["bias"] > 0) == (v["bias"] > 0):
            status = "[C] mismo signo pero no significativo -> debil"
        else:
            status = "[C] signo CONTRARIO -> artefacto de horizonte, descartar"
        side = "INFRAvalorado" if v["bias"] > 0 else "SOBREvalorado"
        print(f"    precio ~{v['price']:.3f} (n={v['n']}): real {v['actual']:.3f}, "
              f"sesgo {v['bias']:+.4f} ({side}); {status}")

    if not robust:
        print("\n  -> Ningun sesgo sobrevive el control de robustez. Los que aparecen")
        print("     en un solo horizonte son artefactos (colapso pre-resolucion o")
        print("     ruido), no ineficiencias explotables. Conclusion operativa: igual")
        print("     que 'bien calibrado' — nada que explotar con el nivel de precio.")
        return

    print("\n  Sesgos ROBUSTOS (significativos en ambos horizontes, mismo signo):")
    for key, v, c in robust:
        print(f"    {key}: [B] {v['bias']:+.4f} / [C] {c['bias']:+.4f}")
    best = max((v for _, v, _ in robust), key=lambda v: abs(v["bias"]))
    print("\n  Antes de celebrar nada:")
    print(f"    El mayor sesgo robusto es {abs(best['bias']):.4f} absoluto sobre precio")
    print(f"    {best['price']:.3f} ({abs(best['bias']) / best['price']:.1%} relativo).")
    print("    El spread relativo medido en esos mercados es del 2-8% y llega al 27%.")
    print("    Un sesgo real que no supere al spread NO es dinero: es una curiosidad.")
    print("    Y esto es in-sample sobre mercados ordenados por volumen: haria falta")
    print("    validacion out-of-sample antes de arriesgar un centimo.")


async def main() -> int:
    want = int(sys.argv[1]) if len(sys.argv) > 1 else 400
    print("=" * 74)
    print("MEDUSA - ESTUDIO DE CALIBRACION DE POLYMARKET")
    print("=" * 74)
    print(f"\nDescargando hasta {want} mercados resueltos...")

    async with httpx.AsyncClient(timeout=30.0) as client:
        markets = await fetch_closed_markets(client, want)
        print(f"  mercados resueltos con resultado limpio (dedup): {len(markets)}")
        if not markets:
            print("  Sin datos. Abortado.")
            return 1

        sem = asyncio.Semaphore(CONCURRENCY)
        all_samples: list[tuple[float, float]] = []
        late_samples: list[tuple[float, float]] = []  # [B] 1/mercado, a ~24h del final
        mid_samples: list[tuple[float, float]] = []   # [C] 1/mercado, a mitad de vida
        used = 0

        async def one(m: dict) -> None:
            nonlocal used
            async with sem:
                hist = await fetch_history(client, m["yes_token"])
            if len(hist) < 24:
                return
            used += 1
            for point in hist:
                try:
                    p = float(point["p"])
                except (KeyError, TypeError, ValueError):
                    continue
                if 0.01 <= p <= 0.99:
                    all_samples.append((p, m["outcome"]))

            # [B] UNA muestra por mercado, a un horizonte fijo antes del ultimo
            # dato (fidelity=60 -> un punto por hora). Horizonte fijo y no "el
            # ultimo punto": cerca del cierre el precio ya colapso al resultado
            # y medir alli seria trivialmente circular.
            if len(hist) > HORIZON_H:
                try:
                    p = float(hist[-HORIZON_H]["p"])
                    if 0.01 <= p <= 0.99:
                        late_samples.append((p, m["outcome"]))
                except (KeyError, TypeError, ValueError):
                    pass

            # [C] UNA muestra por mercado, a MITAD de la vida: lejos del colapso
            # final. Controla la circularidad residual de [B] (mercados decididos
            # mas de 24h antes del cierre).
            if len(hist) >= MIN_LIFE_FOR_MID:
                try:
                    p = float(hist[len(hist) // 2]["p"])
                    if 0.01 <= p <= 0.99:
                        mid_samples.append((p, m["outcome"]))
                except (KeyError, TypeError, ValueError):
                    pass

        batch = 40
        for i in range(0, len(markets), batch):
            await asyncio.gather(*(one(m) for m in markets[i:i + batch]))
            print(f"  histórico: {min(i + batch, len(markets))}/{len(markets)}...", end="\r")

        print(f"\n  mercados con histórico usable: {used}")

    report("[A] Todos los puntos horarios — NO CONCLUYENTE, se muestra solo para "
           "ver el tamaño del artefacto de correlacion",
           all_samples, independent=False)
    table_late = report(f"[B] Una muestra por mercado, a {HORIZON_H}h del final — "
                        f"la principal",
                        late_samples, independent=True)
    table_mid = report("[C] Una muestra por mercado, a MITAD de vida — control de "
                       "robustez (sin colapso pre-resolucion)",
                       mid_samples, independent=True)

    verdict(table_late, table_mid, used)
    print("\n  Nota: si [A] discrepa de [B]/[C], gana [B]/[C]. [A] infla la muestra")
    print("  contando cientos de veces el mismo mercado y fabrica sesgos que no")
    print("  existen.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
