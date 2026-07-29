"""Estadistica del IFE. Funciones PURAS, sin BD, sin red y sin reloj.

Todo lo que convierte observaciones en evidencia esta aqui y solo aqui, para
que se pueda testear con numeros escritos a mano. Dos ideas gobiernan el
fichero:

  1. **Ninguna proporcion se publica desnuda.** Toda proporcion sale con su
     cota inferior de Wilson: 3/3 y 240/400 valen 1.00 y 0.60 a ojo, pero sus
     cotas son 0.44 y 0.55 -- el orden se INVIERTE. Publicar solo la media es
     como publicar un backtest sin el numero de operaciones.
  2. **Todo score tiene su nulo explicito.** El nulo de la posicion en una
     cascada es 0.5 (bajo intercambiabilidad, el rango normalizado esperado de
     cualquier participante es 0.5). Un `leadership_score` solo significa algo
     comparado con ese 0.5, y por eso se publica tambien `edge_vs_chance`.
"""

from __future__ import annotations

import math

# z de la normal para el 95% bilateral. Wilson con este z da la cota que se
# publica en todas las proporciones del paquete.
Z95 = 1.959963985


def median(values: list[float]) -> float:
    """Mediana. Se usa mediana y no media en todos los tiempos (lags, delays)
    a proposito: las distribuciones de latencia tienen colas larguisimas y un
    solo trade a las seis horas desplazaria la media hasta dejarla inservible."""
    if not values:
        return 0.0
    ordered = sorted(float(v) for v in values)
    n = len(ordered)
    mid = n // 2
    if n % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def mean(values: list[float]) -> float:
    return (sum(float(v) for v in values) / len(values)) if values else 0.0


def wilson_lower(successes: float, n: int, z: float = Z95) -> float:
    """Cota inferior del intervalo de Wilson para una proporcion.

    Wilson y no el intervalo normal: con n pequeño o proporciones pegadas a 0 o
    a 1, el normal produce limites fuera de [0,1] y una confianza que no existe.
    Wilson se comporta en esos extremos, que es justo donde vive casi todo lo
    que mide este motor (pocas cascadas por wallet al principio).
    """
    if n <= 0:
        return 0.0
    p = max(0.0, min(1.0, float(successes) / n))
    z2 = z * z
    denom = 1.0 + z2 / n
    center = p + z2 / (2 * n)
    margin = z * math.sqrt((p * (1 - p) + z2 / (4 * n)) / n)
    return max(0.0, (center - margin) / denom)


def mean_lower(values: list[float], z: float = Z95, lo: float = 0.0) -> float:
    """Cota inferior de la media de valores acotados en [0,1] (rangos, scores).

    Para los rangos normalizados no hay exitos que contar, asi que se usa el
    error estandar de la media. Con n=1 el error estandar no existe y se
    devuelve `lo`: una sola observacion no sostiene ninguna afirmacion, y
    fingir un intervalo de anchura cero seria peor que no dar ninguno.
    """
    n = len(values)
    if n <= 1:
        return lo
    mu = mean(values)
    var = sum((float(v) - mu) ** 2 for v in values) / (n - 1)
    sem = math.sqrt(var / n)
    return max(lo, mu - z * sem)


def decay_score(seconds: float, half_life: float) -> float:
    """Convierte una latencia en un score acotado (0, 1] con vida media dada.

    Por que hace falta: `information_speed` en segundos no es comparable entre
    mercados (una carrera electoral y una ventana de 5 min viven en escalas
    distintas) y no se puede promediar sin que el mercado mas lento domine.
    El decaimiento exponencial da un numero comparable y monotono: mas rapido,
    mas alto, siempre entre 0 y 1.
    """
    if half_life <= 0:
        return 0.0
    return float(2.0 ** (-max(0.0, float(seconds)) / half_life))


def quantile_time(offsets: list[float], fraction: float) -> float:
    """Segundos hasta que ha entrado `fraction` de los participantes.

    `offsets` son los segundos de cada entrada desde el inicio de la cascada,
    en orden. Es la definicion de CONSENSUS DELAY del motor: cuanto tarda media
    cascada (por defecto) en estar dentro. Con fraccion 1.0 devuelve el span
    completo.
    """
    if not offsets:
        return 0.0
    f = min(1.0, max(0.0, float(fraction)))
    idx = max(0, math.ceil(f * len(offsets)) - 1)
    return float(sorted(offsets)[idx])


def gaps(times: list[float]) -> list[float]:
    """Huecos entre elementos consecutivos de una serie ya ordenada."""
    return [times[i] - times[i - 1] for i in range(1, len(times))]


def histogram(values: list[float], buckets: int, max_value: float) -> list[dict]:
    """Reparte valores en tramos de igual anchura (para el Research Lab).

    Lo que cae por encima de `max_value` se acumula en el ultimo tramo en vez de
    desaparecer: una latencia enorme es informacion sobre la cola, y borrarla
    haria parecer que la distribucion se acaba justo donde termina el grafico.
    """
    if not values or buckets < 1 or max_value <= 0:
        return []
    width = float(max_value) / buckets
    counts = [0] * buckets
    for value in values:
        idx = min(buckets - 1, max(0, int(float(value) / width)))
        counts[idx] += 1
    return [
        {"from": round(i * width, 2), "to": round((i + 1) * width, 2), "n": n}
        for i, n in enumerate(counts)
    ]


def summarize_proportion(successes: float, n: int) -> dict:
    """Proporcion + n + cota inferior, que es la unica forma en la que este
    paquete deja salir una proporcion hacia la BD o hacia la API."""
    rate = (float(successes) / n) if n else 0.0
    return {"rate": round(rate, 6), "n": int(n),
            "lower": round(wilson_lower(successes, n), 6)}
