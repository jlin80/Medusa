"""Descubrimiento de VARIABLES. Puro: sin BD, sin red y sin reloj.

Este modulo responde a una sola pregunta: de todo lo que traen las observaciones
de una fuente, ¿sobre que se puede hablar? La respuesta NO es "sobre lo que
declara el esquema". Una columna puede existir y venir vacia, o constante, o con
un unico nivel poblado, y proponer sobre ella produce efectos indefinidos o
triviales que despues hay que explicar en un panel.

Los cuatro filtros, y lo que cada uno evita:

    cobertura       una columna presente en menos de `min_coverage` de la
                    ventana describe un subconjunto raro, no la ventana. El caso
                    real es `strategy_signals.spread`, que es NULL en todas las
                    filas anteriores al 2026-07-16.
    distintos       una numerica con dos o tres valores distintos no es una
                    variable continua: es una categorica disfrazada, y Spearman
                    sobre ella devuelve un rho que depende casi entero de como
                    caiga el empate.
    varianza        una constante no correlaciona con nada (denominador cero).
    niveles         una categorica con un solo nivel no contrasta con nada, y una
                    con doscientos (un `market_id`) daria doscientos contrastes
                    de los que casi todos son un caso contra el mundo.

DONDE ESTA LA FRONTERA DE "NO HARDCODEAR". Aqui se declara la FORMA de los datos
-- que una columna es una condicion y otra es un resultado --, y eso es lineage,
no una hipotesis: es la diferencia entre decir "el spread es una condicion y el
ROI un resultado" y decir "el spread alto baja el ROI". Lo primero lo sabe quien
escribio la tabla; lo segundo tiene que salir de los datos, y sale en
`generator.py`.
"""

from __future__ import annotations

from medusa.intelligence.hypothesis.stats import variance
from medusa.intelligence.hypothesis.types import (
    CATEGORICAL,
    LABEL,
    NUMERIC,
    OUTCOME,
    PREDICTOR,
    Observation,
    Variable,
)


def _numeric_column(observations: list[Observation], name: str) -> list[float]:
    """Valores presentes de una columna numerica. Los huecos NO se rellenan."""
    out = []
    for obs in observations:
        value = obs.numeric(name)
        if value is not None:
            out.append(value)
    return out


def _column_names(observations: list[Observation], bag: str) -> list[str]:
    """Nombres que APARECEN en los datos, en orden estable.

    Se recorren todas las observaciones y no solo la primera: una fuente puede
    traer filas heterogeneas (una feature derivada que solo existe cuando hay
    con que derivarla) y quedarse con las claves de la fila 0 borraria variables
    reales de forma silenciosa.
    """
    seen: dict[str, None] = {}
    for obs in observations:
        for key in getattr(obs, bag):
            seen.setdefault(key, None)
    return sorted(seen)


def discover_variables(
    observations: list[Observation],
    *,
    min_coverage: float = 0.6,
    min_distinct: int = 8,
    max_levels: int = 12,
    min_level_size: int = 15,
) -> list[Variable]:
    """Variables que la ventana SOSTIENE, con su papel y su cobertura.

    Espera observaciones de UNA fuente (el servicio llama una vez por fuente):
    mezclar fuentes juntaria columnas homonimas que no significan lo mismo -- el
    `spread` de una señal de estrategia es el del instante del disparo, y el de
    un mercado es el actual.
    """
    if not observations:
        return []
    total = len(observations)
    source = observations[0].source
    out: list[Variable] = []

    # --- numericas: condiciones (predictores) y resultados (outcomes) ---
    for bag, role in (("features", PREDICTOR), ("outcomes", OUTCOME)):
        for name in _column_names(observations, bag):
            values = _numeric_column(observations, name)
            n = len(values)
            coverage = n / total
            if coverage < min_coverage:
                continue
            n_distinct = len(set(values))
            # El minimo de valores distintos se exige a los predictores, no a los
            # outcomes: un resultado binario legitimo (`won`, 0/1) es justo lo
            # que se quiere explicar, y Spearman sobre rangos con empates
            # promediados lo trata bien. Como PREDICTOR, en cambio, un binario
            # pertenece a la forma de contraste de grupos, no a la monotona.
            if role == PREDICTOR and n_distinct < min_distinct:
                continue
            if n_distinct < 2 or variance(values) <= 0:
                continue
            out.append(Variable(
                name=name, kind=NUMERIC, role=role, source=source,
                n=n, coverage=coverage, n_distinct=n_distinct,
            ))

    # --- categoricas: etiquetas con niveles que aguantan un contraste ---
    for name in _column_names(observations, "labels"):
        counts: dict[str, int] = {}
        for obs in observations:
            value = obs.label(name)
            if value is not None:
                counts[value] = counts.get(value, 0) + 1
        n = sum(counts.values())
        if n / total < min_coverage:
            continue
        if len(counts) < 2 or len(counts) > max_levels:
            continue
        # Solo los niveles con masa suficiente llegan a ser contrastables. Los
        # demas se quedan fuera de `levels` pero siguen contando como "resto":
        # borrarlos del resto cambiaria la comparacion sin avisar.
        levels = tuple(sorted(k for k, c in counts.items() if c >= min_level_size))
        if not levels:
            continue
        out.append(Variable(
            name=name, kind=CATEGORICAL, role=LABEL, source=source,
            n=n, coverage=n / total, n_distinct=len(counts), levels=levels,
        ))

    return out


def paired_values(
    observations: list[Observation], predictor: str, outcome: str,
) -> tuple[list[float], list[float]]:
    """Pares (predictor, outcome) COMPLETOS, en el orden de las observaciones.

    Descarte por parejas: si a una observacion le falta cualquiera de los dos, se
    cae del par. Es la unica opcion honesta -- imputar la media del predictor
    tira la correlacion hacia cero, e imputar la del outcome la infla -- y por eso
    el `n` que viaja en la estimacion es el de PARES completos y no el de la
    ventana.
    """
    xs: list[float] = []
    ys: list[float] = []
    for obs in observations:
        x = obs.numeric(predictor)
        y = obs.numeric(outcome)
        if x is None or y is None:
            continue
        xs.append(x)
        ys.append(y)
    return xs, ys


def split_by_level(
    observations: list[Observation], label: str, level: str, outcome: str,
) -> tuple[list[float], list[float]]:
    """Outcome partido en (nivel, resto). Solo entran observaciones con etiqueta.

    Las que no tienen la etiqueta se caen de LOS DOS lados. Meterlas en el resto
    convertiria "sports contra el resto de categorias" en "sports contra el resto
    de categorias mas todo lo que no se etiqueto", que es otro contraste.
    """
    inside: list[float] = []
    outside: list[float] = []
    for obs in observations:
        tag = obs.label(label)
        if tag is None:
            continue
        value = obs.numeric(outcome)
        if value is None:
            continue
        (inside if tag == level else outside).append(value)
    return inside, outside


def split_by_cut(
    observations: list[Observation], predictor: str, cut: float, outcome: str,
) -> tuple[list[float], list[float]]:
    """Outcome partido en (predictor > corte, predictor <= corte).

    El corte llega DADO, nunca se calcula aqui. Es lo que permite congelarlo al
    proponer y reutilizar exactamente el mismo al contrastar fuera de muestra:
    recalcularlo sobre los datos de prueba seria elegir el corte que mejor queda
    en el test y llamarlo replicacion.
    """
    above: list[float] = []
    below: list[float] = []
    for obs in observations:
        x = obs.numeric(predictor)
        y = obs.numeric(outcome)
        if x is None or y is None:
            continue
        (above if x > cut else below).append(y)
    return above, below
