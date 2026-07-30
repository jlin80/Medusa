"""Estadistica del HE. Funciones PURAS, sin BD, sin red y sin reloj.

Todo lo que convierte observaciones en evidencia esta aqui y solo aqui, para que
se pueda testear con numeros escritos a mano. Cuatro decisiones gobiernan el
fichero:

  1. **Rangos, no valores.** La asociacion monotona se mide con Spearman y no con
     Pearson. Casi nada de lo que produce Medusa es normal ni lineal: los ROI
     tienen colas gordisimas y las latencias son log-normales con cola de horas.
     Pearson sobre eso mide sobre todo donde cayo el valor extremo.

  2. **Ningun efecto se publica desnudo.** Todo sale como `EffectEstimate`, con
     intervalo. Un efecto de 0.31 con intervalo [-0.10, 0.62] y otro de 0.19 con
     [0.11, 0.27] se ordenan al reves de como los lee el ojo.

  3. **Escala comun.** Las diferencias de medias se devuelven ESTANDARIZADAS
     (divididas por la desviacion combinada). Sin eso, un contraste sobre `roi`
     (unidades de 0.01) y otro sobre `consensus_delay` (unidades de 1000 s) no se
     pueden comparar, ordenar ni pintar en la misma tabla.

  4. **La multiplicidad se paga.** El motor prueba cientos de relaciones por
     pasada; al 5% eso da decenas de "hallazgos" solo por azar. Se aplica
     Benjamini-Hochberg sobre TODOS los contrastes de la pasada y se guarda
     cuantos fueron, para que la correccion sea auditable y no un parrafo.

Nota sobre los valores p: se usan aproximaciones normales (Fisher para rho,
Welch con z para las medias) en vez de las distribuciones t exactas. Con los
tamaños minimos que exige el motor (decenas de observaciones por grupo) la
diferencia esta muy por debajo de la incertidumbre real de estos datos, y no
merece arrastrar una implementacion de la t incompleta. Donde SI importa el rigor
es en la valla temporal y en la correccion por multiplicidad, que son las dos
cosas que de verdad separan un hallazgo de un artefacto.
"""

from __future__ import annotations

import math

# z de la normal para el 95% bilateral.
Z95 = 1.959963985

# Muestras minimas por debajo de las cuales ni se intenta estimar. Con n<=3 la
# correlacion de rangos no tiene error estandar definido (Fisher pide n-3) y
# devolver "algo" seria inventarse la precision.
MIN_N_CORR = 5
MIN_N_GROUP = 3


def mean(values: list[float]) -> float:
    return (sum(values) / len(values)) if values else 0.0


def variance(values: list[float]) -> float:
    """Varianza muestral (n-1). Con n<2 no hay dispersion que estimar: 0.0."""
    n = len(values)
    if n < 2:
        return 0.0
    mu = mean(values)
    return sum((v - mu) ** 2 for v in values) / (n - 1)


def stdev(values: list[float]) -> float:
    return math.sqrt(variance(values))


def median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(float(v) for v in values)
    n = len(ordered)
    mid = n // 2
    if n % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def quantile(values: list[float], q: float) -> float:
    """Cuantil por interpolacion lineal. `q` se recorta a [0,1]."""
    if not values:
        return 0.0
    ordered = sorted(float(v) for v in values)
    if len(ordered) == 1:
        return ordered[0]
    pos = min(1.0, max(0.0, float(q))) * (len(ordered) - 1)
    lo = int(math.floor(pos))
    hi = min(len(ordered) - 1, lo + 1)
    frac = pos - lo
    return ordered[lo] * (1 - frac) + ordered[hi] * frac


def normal_sf(z: float) -> float:
    """Cola superior de la normal estandar."""
    return 0.5 * math.erfc(float(z) / math.sqrt(2.0))


def two_sided_p(z: float) -> float:
    """Valor p bilateral de un estadistico z."""
    return min(1.0, 2.0 * normal_sf(abs(float(z))))


# ------------------------------------------------------------------ rangos ----
def ranks(values: list[float]) -> list[float]:
    """Rangos con EMPATES PROMEDIADOS (el metodo "midrank").

    Los empates no son un detalle en estos datos: `won` es 0/1, el spread viene
    redondeado a tres decimales y muchas features salen de contadores enteros.
    Asignar rangos por orden de llegada en un empate mete una correlacion falsa
    que depende del orden en que Postgres devolvio las filas.
    """
    n = len(values)
    order = sorted(range(n), key=lambda i: values[i])
    out = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and values[order[j + 1]] == values[order[i]]:
            j += 1
        # Rango medio del bloque de empatados (1-indexado).
        shared = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            out[order[k]] = shared
        i = j + 1
    return out


def pearson(xs: list[float], ys: list[float]) -> float:
    """Correlacion de Pearson. Aqui solo se usa SOBRE RANGOS (= Spearman)."""
    n = len(xs)
    if n < 2 or n != len(ys):
        return 0.0
    mx, my = mean(xs), mean(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if dx <= 0 or dy <= 0:
        # Una de las dos series es constante: no hay asociacion que medir. Cero
        # y no una excepcion, porque una feature constante en una ventana es un
        # caso normal, no un error.
        return 0.0
    return num / (dx * dy)


def spearman(xs: list[float], ys: list[float]) -> float:
    return pearson(ranks(xs), ranks(ys))


def correlation_estimate(xs: list[float], ys: list[float], z: float = Z95) -> dict:
    """Spearman + intervalo de Fisher. Devuelve el dict de un `EffectEstimate`.

    La transformacion de Fisher (atanh) se usa porque rho vive en [-1,1] y su
    distribucion se aprieta contra los bordes: un intervalo simetrico calculado
    directamente sobre rho se sale del rango y promete cotas imposibles. En la
    escala z el intervalo es simetrico, y al volver con tanh queda dentro de
    [-1,1] por construccion.
    """
    n = len(xs)
    if n < MIN_N_CORR or n != len(ys):
        return {"n": n, "effect": 0.0, "lower": 0.0, "upper": 0.0,
                "p_value": 1.0, "null": 0.0}
    rho = spearman(xs, ys)
    # atanh(±1) es infinito: se recorta justo por dentro para no reventar en el
    # caso (real) de una relacion perfectamente monotona sobre pocos puntos.
    clipped = max(-0.999999, min(0.999999, rho))
    zr = math.atanh(clipped)
    se = 1.0 / math.sqrt(max(1.0, n - 3.0))
    return {
        "n": n, "effect": rho,
        "lower": math.tanh(zr - z * se), "upper": math.tanh(zr + z * se),
        "p_value": two_sided_p(zr / se), "null": 0.0,
    }


# --------------------------------------------------- contraste de dos grupos --
def standardized_difference(
    group: list[float], rest: list[float], z: float = Z95,
) -> dict:
    """Diferencia de medias ESTANDARIZADA (grupo - resto), con intervalo.

    El efecto es `(media_grupo - media_resto) / sd_combinada`, o sea el mismo
    numero que se conoce como d: "cuantas desviaciones tipicas separan a los dos
    grupos". Esa normalizacion es la que permite meter en una misma tabla un
    contraste sobre ROI y otro sobre segundos de retardo.

    El error estandar es el de Welch (no asume varianzas iguales, que aqui nunca
    lo son: un grupo minoritario de 20 casos y el resto con 4.000). La sd
    combinada se trata como conocida al pasar el intervalo a la escala
    estandarizada; con los minimos por grupo que exige el motor, la
    incertidumbre que eso ignora es de segundo orden frente a la del propio
    efecto.
    """
    na, nb = len(group), len(rest)
    if na < MIN_N_GROUP or nb < MIN_N_GROUP:
        return {"n": na + nb, "effect": 0.0, "lower": 0.0, "upper": 0.0,
                "p_value": 1.0, "null": 0.0}
    va, vb = variance(group), variance(rest)
    diff = mean(group) - mean(rest)
    se = math.sqrt(va / na + vb / nb)
    if se <= 0:
        # Las dos series son constantes. Si ademas difieren, la diferencia es
        # real pero no tiene escala en la que expresarse: no se propone.
        return {"n": na + nb, "effect": 0.0, "lower": 0.0, "upper": 0.0,
                "p_value": 1.0, "null": 0.0}
    p = two_sided_p(diff / se)
    # sd combinada ponderada por grados de libertad.
    pooled = math.sqrt(max(1e-12, ((na - 1) * va + (nb - 1) * vb) / (na + nb - 2)))
    scale = 1.0 / pooled
    return {
        "n": na + nb, "effect": diff * scale,
        "lower": (diff - z * se) * scale, "upper": (diff + z * se) * scale,
        "p_value": p, "null": 0.0,
    }


# ------------------------------------------------------------ multiplicidad ---
def benjamini_hochberg(p_values: list[float], alpha: float = 0.05) -> list[bool]:
    """Control de la tasa de falsos descubrimientos (FDR) de Benjamini-Hochberg.

    Por que BH y no Bonferroni: el motor prueba cientos de relaciones y
    Bonferroni (alpha/m) las mataria casi todas, incluidas las reales -- con 400
    contrastes exigiria p < 0.000125. BH controla la PROPORCION esperada de
    falsos entre los aceptados, que es justo la pregunta de un motor que propone
    en lote: "de las veinte que he propuesto, ¿cuantas espero que sean ruido?".

    Devuelve una lista de aceptados en el orden de entrada. Con la lista vacia
    devuelve la lista vacia; ningun contraste no es un contraste aceptado.
    """
    m = len(p_values)
    if m == 0:
        return []
    a = max(0.0, min(1.0, float(alpha)))
    order = sorted(range(m), key=lambda i: p_values[i])
    # Mayor k tal que p_(k) <= k/m * alpha; se aceptan los k primeros ordenados.
    cutoff = 0
    for rank, idx in enumerate(order, start=1):
        if p_values[idx] <= (rank / m) * a:
            cutoff = rank
    accepted = [False] * m
    for rank, idx in enumerate(order, start=1):
        if rank <= cutoff:
            accepted[idx] = True
    return accepted


# --------------------------------------------------------------- confianza ----
def evidence_confidence(
    magnitude_lower: float, n: int, reference: float, min_samples: int,
) -> float:
    """Fuerza de la evidencia en [0,1]. NO es P(hipotesis cierta).

    Dos factores, ambos monotonos:

        fuerza  = min(1, |efecto que sostiene la cota| / referencia)
        peso    = n / (n + min_samples)

    y la confianza es su producto. Las propiedades que se buscan:

      - vale 0.0 si el intervalo cruza el nulo (`magnitude_lower` es 0 ahi): sin
        signo determinado no hay confianza que reportar, por grande que sea la
        muestra;
      - vale 0.0 con n=0, que es el caso de toda hipotesis en `proposed`;
      - un efecto enorme con muestra ridicula NO llega arriba, porque el peso lo
        frena: con n = min_samples el techo es 0.5.

    `reference` es el efecto que se considera "grande" en esa escala (rho o d).
    Es una convencion declarada, y por eso viaja como parametro en vez de estar
    escondida en una constante.
    """
    if n <= 0 or reference <= 0:
        return 0.0
    strength = min(1.0, max(0.0, float(magnitude_lower)) / float(reference))
    weight = n / (n + max(1, int(min_samples)))
    return round(strength * weight, 6)
