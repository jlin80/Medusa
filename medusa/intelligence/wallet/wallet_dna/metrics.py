"""Las 19 metricas del ADN, una funcion pura por metrica.

Cada funcion recibe posiciones ya normalizadas (`WalletPosition`) y devuelve UN
float. Sin I/O, sin reloj propio (el "ahora" se pasa como argumento para que los
tests sean deterministas) y sin estado. Asi cada metrica se puede verificar
aislada con numeros escritos a mano, que es la unica forma de que un perfil de
19 dimensiones sea auditable.

Convenios que se respetan en todas:

  - Muestra insuficiente => 0.0, nunca un valor inventado. Un ADN a ceros dice
    "no se sabe"; un ADN con numeros fabricados MIENTE, y contamina el
    clustering, la similitud y la importancia de features de toda la poblacion.
  - Nada se recorta salvo donde la metrica esta definida en [0,1] por
    construccion. Un Sharpe de 4 se reporta como 4: recortarlo escondería
    justamente el caso raro que interesa mirar.
"""

from __future__ import annotations

import datetime as dt
import math
from collections import defaultdict
from typing import Sequence

from medusa.intelligence.wallet.stats import (
    clamp,
    gini,
    max_drawdown,
    mean,
    safe_div,
    stdev,
    wilson_lower,
)
from medusa.intelligence.wallet.types import WalletPosition


def closed_positions(positions: Sequence[WalletPosition]) -> list[WalletPosition]:
    """Solo lo CERRADO entra en rendimiento.

    Una posicion viva tiene un PnL que todavia puede cambiar de signo; contarla
    es el error clasico de inflar el track record con ganancias no realizadas.
    """
    return [p for p in positions if p.closed]


# ------------------------------------------------------------- rendimiento --
def roi_historical(positions: Sequence[WalletPosition]) -> float:
    closed = closed_positions(positions)
    return mean(p.roi for p in closed) if closed else 0.0


def roi_recent(
    positions: Sequence[WalletPosition], now: dt.datetime, days: float = 30.0,
) -> float:
    """ROI medio de la ventana reciente. Sin cierres en la ventana => 0.0."""
    cutoff = now - dt.timedelta(days=days)
    recent = [p for p in closed_positions(positions)
              if p.closed_at is not None and p.closed_at >= cutoff]
    return mean(p.roi for p in recent) if recent else 0.0


def sharpe(positions: Sequence[WalletPosition]) -> float:
    """media(ROI)/desviacion(ROI) por posicion.

    Sin tasa libre de riesgo y sin anualizar a proposito: la unidad aqui es "una
    posicion", no "un año". Anualizar exigiria asumir una frecuencia estable que
    estas wallets no tienen, y el numero resultante pareceria mas riguroso de lo
    que es.
    """
    closed = closed_positions(positions)
    if len(closed) < 2:
        return 0.0
    rois = [p.roi for p in closed]
    sd = stdev(rois)
    return safe_div(mean(rois), sd) if sd > 0 else 0.0


def win_rate(positions: Sequence[WalletPosition]) -> float:
    closed = closed_positions(positions)
    if not closed:
        return 0.0
    return sum(1 for p in closed if p.pnl > 0) / len(closed)


def consistency(positions: Sequence[WalletPosition]) -> float:
    """1/(1+CV), con CV = desviacion/|media| del ROI. En (0, 1].

    Mide si el resultado se repite o es un puñado de aciertos entre mucho ruido.
    Es deliberadamente independiente del signo: una wallet consistentemente mala
    puntua alto aqui, y eso es correcto -- la consistencia es una forma, no una
    virtud. Quien juzga si el resultado es bueno es el score, no esta metrica.
    """
    closed = closed_positions(positions)
    if len(closed) < 2:
        return 0.0
    rois = [p.roi for p in closed]
    mu, sd = mean(rois), stdev(rois)
    if abs(mu) < 1e-9:
        return 0.0
    return clamp(1.0 / (1.0 + safe_div(sd, abs(mu), default=0.0)))


def volatility(positions: Sequence[WalletPosition]) -> float:
    return stdev([p.roi for p in closed_positions(positions)])


def drawdown(positions: Sequence[WalletPosition]) -> float:
    """Maxima caida de la curva de PnL acumulado, ordenada por cierre."""
    closed = [p for p in closed_positions(positions) if p.closed_at is not None]
    if not closed:
        return 0.0
    closed.sort(key=lambda p: p.closed_at)
    curve, running = [], 0.0
    for p in closed:
        running += p.pnl
        curve.append(running)
    return max_drawdown(curve)


# ---------------------------------------------------------------- actividad --
def trade_frequency(positions: Sequence[WalletPosition]) -> float:
    """Posiciones por dia sobre el periodo ACTIVO (primera a ultima operacion).

    No sobre "los ultimos 30 dias": una wallet que operó 40 veces en su primera
    semana y desapareció no es una wallet de 1.3 posiciones/dia. Que lleve
    inactiva es informacion distinta y la lleva `freshness`.
    """
    stamps = [p.opened_at for p in positions if p.opened_at is not None]
    if len(stamps) < 2:
        return 0.0
    span_days = (max(stamps) - min(stamps)).total_seconds() / 86400.0
    if span_days < 1.0:
        span_days = 1.0     # todo en un dia => la frecuencia es el recuento
    return len(positions) / span_days


def freshness(
    positions: Sequence[WalletPosition], now: dt.datetime, half_life_days: float = 30.0,
) -> float:
    """exp(-dias_inactiva / semivida), en (0, 1].

    Un track record excelente de hace ocho meses describe a alguien que ya no
    esta operando. Sin este factor, el ranking de reputacion se llenaria de
    fantasmas.
    """
    stamps = [p.closed_at or p.opened_at for p in positions
              if (p.closed_at or p.opened_at) is not None]
    if not stamps or half_life_days <= 0:
        return 0.0
    days = max(0.0, (now - max(stamps)).total_seconds() / 86400.0)
    return clamp(math.exp(-days / half_life_days))


def decay(
    positions: Sequence[WalletPosition], now: dt.datetime, days: float = 30.0,
) -> float:
    """tanh(ROI reciente - ROI historico), en (-1, 1).

    Positivo = esta mejorando; negativo = su edge se esta degradando. `tanh`
    acota sin recortar bruscamente: una diferencia de +0.05 y otra de +5.0 no
    pueden pesar igual, pero tampoco puede una sola wallet extrema dominar la
    estandarizacion de toda la poblacion.
    """
    closed = closed_positions(positions)
    if len(closed) < 2:
        return 0.0
    return math.tanh(roi_recent(positions, now, days) - roi_historical(positions))


def reliability(positions: Sequence[WalletPosition]) -> float:
    """Cota inferior de Wilson del win rate: la confianza AJUSTADA POR MUESTRA.

    Es la metrica que impide que "3 de 3" se lea como una wallet perfecta. Misma
    funcion que usa el asignador de capital para decidir si una estrategia opera.
    """
    closed = closed_positions(positions)
    if not closed:
        return 0.0
    wins = sum(1 for p in closed if p.pnl > 0)
    return wilson_lower(wins, len(closed))


# ------------------------------------------------------------------ timing --
def entry_timing(positions: Sequence[WalletPosition]) -> float:
    """Fraccion media de la vida del mercado transcurrida al ENTRAR.

    0 = entra nada mas abrir (apuesta a la tesis), 1 = entra al final (apuesta a
    lo que ya es casi seguro). Las posiciones sin fechas de mercado se EXCLUYEN
    del promedio en vez de contarse como 0: un dato ausente no es un timing
    temprano.
    """
    fracs = [f for f in (p.duration_fraction(p.opened_at) for p in positions)
             if f is not None]
    return mean(fracs) if fracs else 0.0


def exit_timing(positions: Sequence[WalletPosition]) -> float:
    """Igual al entrar, pero al salir. Una posicion mantenida hasta la
    resolucion cuenta como 1.0 (salio con el mercado, no antes)."""
    fracs: list[float] = []
    for p in positions:
        if not p.closed:
            continue
        frac = p.duration_fraction(p.closed_at)
        if frac is not None:
            fracs.append(frac)
    return mean(fracs) if fracs else 0.0


# ------------------------------------------------------------ preferencias --
def liquidity_preference(positions: Sequence[WalletPosition]) -> float:
    """log10(1 + liquidez media), normalizado por 6 decadas (~1 M USDC).

    En logaritmo porque la liquidez de Polymarket abarca varios ordenes de
    magnitud: en lineal, tres mercados enormes aplastarian el resto del perfil.
    """
    vals = [p.liquidity for p in positions if p.liquidity > 0]
    if not vals:
        return 0.0
    return clamp(math.log10(1.0 + mean(vals)) / 6.0)


def spread_preference(positions: Sequence[WalletPosition]) -> float:
    """Spread medio de los mercados que opera.

    Alto significa que acepta pagar el peaje. El proyecto ya midio que la ida y
    vuelta cuesta ~2.2% y que los mercados de precio extremo llegan al 27%: esta
    metrica dice si esa wallet vive o no donde el coste se come el edge.
    """
    vals = [p.spread for p in positions if p.spread > 0]
    return clamp(mean(vals)) if vals else 0.0


def category_expertise(positions: Sequence[WalletPosition]) -> float:
    """max sobre categorias de (peso de la categoria x Wilson del win rate).

    Un unico escalar en [0,1] para el vector; el detalle por categoria vive
    aparte (`category_breakdown`), tambien en numeros. Multiplicar por el peso
    es lo que evita coronar como "experta" a quien acerto 4 de 4 en una
    categoria que representa el 2% de su actividad.
    """
    closed = closed_positions(positions)
    if not closed:
        return 0.0
    per_cat: dict[str, list[WalletPosition]] = defaultdict(list)
    for p in closed:
        per_cat[p.category or "other"].append(p)
    total = len(closed)
    best = 0.0
    for rows in per_cat.values():
        wins = sum(1 for p in rows if p.pnl > 0)
        share = len(rows) / total
        best = max(best, share * wilson_lower(wins, len(rows)))
    return clamp(best)


def category_breakdown(positions: Sequence[WalletPosition]) -> dict[str, dict]:
    """Detalle numerico por categoria. Sin etiquetas: la categoria es la clave,
    y todo lo demas son numeros."""
    closed = closed_positions(positions)
    if not closed:
        return {}
    per_cat: dict[str, list[WalletPosition]] = defaultdict(list)
    for p in closed:
        per_cat[p.category or "other"].append(p)
    total = len(closed)
    out: dict[str, dict] = {}
    for cat, rows in per_cat.items():
        wins = sum(1 for p in rows if p.pnl > 0)
        out[cat] = {
            "n": len(rows),
            "wins": wins,
            "share": round(len(rows) / total, 4),
            "win_rate": round(wins / len(rows), 4),
            "wilson": round(wilson_lower(wins, len(rows)), 4),
            "avg_roi": round(mean(p.roi for p in rows), 6),
        }
    return dict(sorted(out.items(), key=lambda kv: -kv[1]["n"]))


def conviction(positions: Sequence[WalletPosition]) -> float:
    """Gini de los importes comprometidos, en [0,1].

    Mide si la wallet SIZE-A cuando cree, o si reparte igual siempre. Es forma,
    no calidad: por eso no entra en el score compuesto (no hay una direccion
    buena universal).
    """
    return gini([p.cost for p in positions if p.cost > 0])


# ---------------------------------------------- relacion con la poblacion --
def beta(
    positions: Sequence[WalletPosition], population_by_bucket: dict[str, float],
    bucket_days: int = 7,
) -> float:
    """cov(ROI wallet, ROI poblacion) / var(ROI poblacion) por cubos temporales.

    Mide cuanto del resultado de la wallet es simplemente "iba con la marea".
    Sin al menos dos cubos comunes con la poblacion devuelve 0.0: un beta
    estimado sobre un punto no es un beta.
    """
    own = _bucket_rois(positions, bucket_days)
    common = sorted(set(own) & set(population_by_bucket))
    if len(common) < 2:
        return 0.0
    xs = [population_by_bucket[k] for k in common]
    ys = [own[k] for k in common]
    var_x = stdev(xs) ** 2
    if var_x <= 0:
        return 0.0
    mx, my = mean(xs), mean(ys)
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (len(xs) - 1)
    return cov / var_x


def alpha(
    positions: Sequence[WalletPosition], population_by_bucket: dict[str, float],
    bucket_days: int = 7,
) -> float:
    """ROI medio de la wallet - beta x ROI medio de la poblacion.

    El exceso que NO se explica por moverse con el resto. Con una poblacion
    vacia degenera al ROI historico, que es lo correcto: sin nadie contra quien
    compararse, todo el resultado es "suyo" por definicion.
    """
    own_roi = roi_historical(positions)
    if not population_by_bucket:
        return own_roi
    b = beta(positions, population_by_bucket, bucket_days)
    return own_roi - b * mean(population_by_bucket.values())


def _bucket_rois(positions: Sequence[WalletPosition], bucket_days: int) -> dict[str, float]:
    """ROI medio por cubo temporal, indexado por el ordinal del cubo."""
    buckets: dict[str, list[float]] = defaultdict(list)
    for p in closed_positions(positions):
        if p.closed_at is None:
            continue
        key = str(int(p.closed_at.timestamp() // (bucket_days * 86400)))
        buckets[key].append(p.roi)
    return {k: mean(v) for k, v in buckets.items()}


def population_buckets(
    all_positions: Sequence[WalletPosition], bucket_days: int = 7,
) -> dict[str, float]:
    """ROI medio de TODA la poblacion por cubo temporal: el "mercado" contra el
    que se miden alpha y beta."""
    return _bucket_rois(all_positions, bucket_days)
