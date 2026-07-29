"""Estadistica basica en aritmetica pura de Python.

Sin numpy a proposito: la CPU del CT202 (Bobcat, sin SSE4.2) aborta con numpy
2.x, y esta es exactamente la restriccion medida que ya decidio el plan de ML
del proyecto. Todo lo que necesita Wallet Intelligence cabe en 60 lineas.
"""

from __future__ import annotations

import math
from typing import Iterable, Sequence

# z de la cota inferior de Wilson al 95% (una cola), el mismo que usa el
# Capital Allocation Manager. No puede haber dos definiciones de "confianza
# ajustada por muestra" en el mismo sistema.
_Z: float = 1.645


def mean(values: Iterable[float]) -> float:
    vals = list(values)
    return sum(vals) / len(vals) if vals else 0.0


def stdev(values: Sequence[float]) -> float:
    """Desviacion tipica MUESTRAL (n-1). Con n<2 devuelve 0.0: una sola
    observacion no tiene dispersion, y fingir una seria inventar informacion."""
    vals = list(values)
    if len(vals) < 2:
        return 0.0
    mu = mean(vals)
    return math.sqrt(sum((v - mu) ** 2 for v in vals) / (len(vals) - 1))


def wilson_lower(wins: int, n: int, z: float = _Z) -> float:
    """Cota inferior de Wilson de una proporcion.

    Portada tal cual de `medusa/allocation/manager.py`: el intervalo de Wald
    colapsa en proporciones extremas, y esa fue una leccion del estudio de
    calibracion. Aqui es lo que convierte "10 de 10 aciertos" (n ridicula) en un
    numero honesto en vez de en un 100%.
    """
    if n <= 0:
        return 0.0
    phat = wins / n
    denom = 1 + z * z / n
    center = phat + z * z / (2 * n)
    margin = z * math.sqrt((phat * (1 - phat) + z * z / (4 * n)) / n)
    return max(0.0, (center - margin) / denom)


def pearson(xs: Sequence[float], ys: Sequence[float]) -> float:
    """Correlacion de Pearson. 0.0 si no hay varianza en algun lado."""
    if len(xs) != len(ys) or len(xs) < 2:
        return 0.0
    mx, my = mean(xs), mean(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if dx <= 0 or dy <= 0:
        return 0.0
    return max(-1.0, min(1.0, num / (dx * dy)))


def gini(values: Sequence[float]) -> float:
    """Coeficiente de Gini de valores no negativos, en [0, 1].

    0 = todos iguales, 1 = todo concentrado en uno. Aqui mide CONVICCION: una
    wallet que apuesta siempre lo mismo no esta expresando confianza en ninguna
    posicion concreta; una que concentra, si. Es una medida de forma, no de
    calidad: no dice que concentrar sea bueno.
    """
    vals = sorted(float(v) for v in values if v is not None and v >= 0)
    n = len(vals)
    total = sum(vals)
    if n < 2 or total <= 0:
        return 0.0
    cum = sum((i + 1) * v for i, v in enumerate(vals))
    return max(0.0, min(1.0, (2 * cum) / (n * total) - (n + 1) / n))


def max_drawdown(pnl_series: Sequence[float]) -> float:
    """Maxima caida relativa de una curva de PnL ACUMULADO, en [0, 1].

    Se mide contra el pico de capital comprometido (pico + 1 para no dividir
    por ~0 cuando la curva arranca en cero). Devuelve magnitud positiva: 0.15
    es "cayo un 15% desde su mejor momento".
    """
    peak = 0.0
    worst = 0.0
    for value in pnl_series:
        peak = max(peak, value)
        base = abs(peak) if abs(peak) > 1e-9 else 1.0
        worst = max(worst, (peak - value) / base)
    return max(0.0, min(1.0, worst))


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def safe_div(num: float, den: float, default: float = 0.0) -> float:
    return num / den if den else default
