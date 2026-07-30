"""Generacion de hipotesis a partir de datos observados. Puro y sin reloj salvo
la fecha de creacion, que entra como parametro.

AQUI ESTA EL NUCLEO DEL MOTOR Y AQUI ESTA LA REGLA QUE LO DEFINE: no hay ni una
hipotesis escrita en el codigo. Lo que hay es una GRAMATICA de tres formas y un
procedimiento que la instancia con las variables que los datos sostengan.

    MONOTONE        predictor numerico  x  outcome numerico
    GROUP_CONTRAST  nivel de etiqueta   x  outcome numerico
    THRESHOLD       predictor > corte   x  outcome numerico

Sobre esas tres formas, cruzando todas las variables descubiertas, salen cientos
de contrastes por pasada. Los que sobreviven a tres filtros se PROPONEN:

    1. muestra    n >= min_samples pares completos,
    2. tamaño     el efecto supera el minimo de su escala (rho o d),
    3. selección  el intervalo excluye el nulo Y el contraste sobrevive a
                  Benjamini-Hochberg aplicado a TODA la pasada.

EL TERCER FILTRO ES EL QUE HACE QUE ESTO NO SEA UNA MAQUINA DE RUIDO. Un motor
que prueba 400 relaciones y publica las que dan p<0.05 publica ~20 hallazgos
falsos por pasada, cada hora, para siempre. Se guarda `tested_in_pass` en cada
hipotesis para que la correccion sea auditable a posteriori y no un parrafo de
documentacion.

Y AUN ASI, LO QUE SALE DE AQUI NO ES EVIDENCIA. Todo lo que se mide en este
fichero es DENTRO de muestra: son los mismos datos que eligieron el enunciado.
Por eso el generador deja siempre `status=proposed`, `confidence=0.0` y
`sample_count=0`. La evidencia la aporta `evaluator.py`, y solo con datos
posteriores a `created_at`.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from medusa.intelligence.hypothesis import features as feat
from medusa.intelligence.hypothesis import stats
from medusa.intelligence.hypothesis.types import (
    CATEGORICAL,
    GROUP_CONTRAST,
    MONOTONE,
    NUMERIC,
    OUTCOME,
    PREDICTOR,
    PROPOSED,
    THRESHOLD,
    EffectEstimate,
    Hypothesis,
    Observation,
    Variable,
    humanize,
)

UTC = dt.timezone.utc


# --------------------------------------------------------------- gramatica ----
# Las plantillas. Son la UNICA prosa del motor, y hablan de asociacion en los dos
# sentidos posibles sin nombrar ninguna variable: el contenido lo pone el dato.
#
# Ni una plantilla usa un verbo causal ("reduce", "mejora", "provoca"). No es
# estilo: el motor no tiene contrafactual, ni asignacion aleatoria, ni control de
# confusores, asi que "el spread alto REDUCE el edge" es una afirmacion que no
# puede sostener, mientras que "a mayor spread, menor edge" es exactamente lo que
# ha medido. Hay un test que recorre estas plantillas buscando verbos causales.
_TEMPLATES: dict[str, tuple[str, str]] = {
    # forma: (direccion +1, direccion -1)
    MONOTONE: (
        "A mayor {predictor}, mayor {outcome}",
        "A mayor {predictor}, menor {outcome}",
    ),
    GROUP_CONTRAST: (
        "Con {predictor} = {level}, {outcome} es mas alto que en el resto",
        "Con {predictor} = {level}, {outcome} es mas bajo que en el resto",
    ),
    THRESHOLD: (
        "Con {predictor} por encima de {cut}, {outcome} es mas alto",
        "Con {predictor} por encima de {cut}, {outcome} es mas bajo",
    ),
}


def describe(
    form: str, predictor: str, outcome: str, direction: int,
    level: str = "", params: dict | None = None, source: str = "",
) -> str:
    """Enunciado en prosa, generado a partir de la forma y de los nombres reales.

    El sufijo con la fuente no es adorno: la misma pareja de nombres significa
    cosas distintas segun de donde salga (el `spread` de una señal es el del
    instante del disparo; el de un mercado es el de ahora), y sin la fuente dos
    hipotesis legitimamente distintas se leerian como la misma frase.
    """
    plus, minus = _TEMPLATES[form]
    template = plus if int(direction) >= 0 else minus
    cut = (params or {}).get("cut")
    text = template.format(
        predictor=humanize(predictor), outcome=humanize(outcome),
        level=level, cut=("—" if cut is None else f"{float(cut):.4g}"),
    )
    return f"{text} [{source}]" if source else text


# ------------------------------------------------------------- candidatos -----
@dataclass
class _Candidate:
    form: str
    predictor: str
    outcome: str
    level: str = ""
    params: dict = field(default_factory=dict)
    estimate: EffectEstimate = EffectEstimate()
    min_effect: float = 0.0


@dataclass
class Proposal:
    """Resultado de una pasada de generacion.

    `tested` es el numero de contrastes realizados, y viaja fuera del motor a
    proposito: es el denominador sin el que ninguna de las hipotesis propuestas
    se puede juzgar.
    """

    hypotheses: list[Hypothesis] = field(default_factory=list)
    tested: int = 0
    survived_effect: int = 0
    survived_fdr: int = 0
    alpha: float = 0.05
    # Variables que la ventana sostuvo. Lo rellena el servicio; vive aqui para
    # que el resumen de una fuente sea un solo objeto.
    variables: int = 0

    def to_dict(self) -> dict:
        return {
            "proposed": len(self.hypotheses), "tested": self.tested,
            "survived_effect": self.survived_effect,
            "survived_fdr": self.survived_fdr, "alpha": self.alpha,
            "variables": self.variables,
        }


def _enumerate(
    observations: list[Observation], variables: list[Variable],
    *, blocked_pairs: set[tuple[str, str]], min_samples: int,
    min_effect_rho: float, min_effect_d: float, cut_quantile: float,
) -> list[_Candidate]:
    """Todos los contrastes de la pasada, ya estimados y sin filtrar aun.

    Se estiman TODOS antes de decidir nada porque Benjamini-Hochberg necesita el
    conjunto completo de valores p: corregir sobre los que ya han pasado un
    filtro de tamaño de efecto seria corregir sobre una muestra elegida y
    subestimar la multiplicidad real.
    """
    predictors = [v for v in variables if v.role == PREDICTOR and v.kind == NUMERIC]
    outcomes = [v for v in variables if v.role == OUTCOME and v.kind == NUMERIC]
    labels = [v for v in variables if v.kind == CATEGORICAL]
    out: list[_Candidate] = []

    for out_var in outcomes:
        # --- forma monotona y forma umbral sobre cada predictor numerico ---
        for pred in predictors:
            if pred.name == out_var.name:
                continue          # una variable contra si misma es rho=1 trivial
            if (pred.name, out_var.name) in blocked_pairs:
                continue          # pareja definicional; ver `sources.py`
            xs, ys = feat.paired_values(observations, pred.name, out_var.name)
            if len(xs) < min_samples:
                continue
            out.append(_Candidate(
                form=MONOTONE, predictor=pred.name, outcome=out_var.name,
                estimate=EffectEstimate(**stats.correlation_estimate(xs, ys)),
                min_effect=min_effect_rho,
            ))
            # El corte es un cuantil de la ventana de descubrimiento y se
            # CONGELA en `params`: es el mismo numero que usara el evaluador.
            cut = stats.quantile(xs, cut_quantile)
            above, below = feat.split_by_cut(observations, pred.name, cut, out_var.name)
            out.append(_Candidate(
                form=THRESHOLD, predictor=pred.name, outcome=out_var.name,
                params={"cut": round(float(cut), 6), "quantile": cut_quantile},
                estimate=EffectEstimate(
                    **stats.standardized_difference(above, below)),
                min_effect=min_effect_d,
            ))

        # --- forma de contraste de grupos sobre cada nivel de cada etiqueta ---
        for lab in labels:
            for level in lab.levels:
                inside, outside = feat.split_by_level(
                    observations, lab.name, level, out_var.name)
                if len(inside) + len(outside) < min_samples:
                    continue
                out.append(_Candidate(
                    form=GROUP_CONTRAST, predictor=lab.name,
                    outcome=out_var.name, level=level,
                    estimate=EffectEstimate(
                        **stats.standardized_difference(inside, outside)),
                    min_effect=min_effect_d,
                ))
    return out


def propose(
    observations: list[Observation],
    variables: list[Variable],
    *,
    source: str = "",
    blocked_pairs: tuple[tuple[str, str], ...] = (),
    min_samples: int = 40,
    min_effect_rho: float = 0.15,
    min_effect_d: float = 0.25,
    alpha: float = 0.05,
    cut_quantile: float = 0.5,
    max_proposals: int = 25,
    now: dt.datetime | None = None,
) -> Proposal:
    """Cruza la gramatica con las variables descubiertas y devuelve candidatas.

    `now` es la fecha de creacion, o sea LA VALLA: a partir de este instante, y
    solo a partir de el, cuentan las observaciones que validaran o rechazaran lo
    que salga de aqui. Entra como parametro para que el modulo siga siendo puro y
    para que un test pueda situarse en el tiempo.
    """
    if not observations or not variables:
        return Proposal(alpha=alpha)
    src = source or observations[0].source
    created = now or dt.datetime.now(UTC)

    candidates = _enumerate(
        observations, variables,
        blocked_pairs=set(blocked_pairs), min_samples=min_samples,
        min_effect_rho=min_effect_rho, min_effect_d=min_effect_d,
        cut_quantile=cut_quantile,
    )
    if not candidates:
        return Proposal(tested=0, alpha=alpha)

    accepted = stats.benjamini_hochberg(
        [c.estimate.p_value for c in candidates], alpha)

    # Los dos filtros se cuentan por separado para poder ver, desde el panel, si
    # una pasada sin propuestas se quedo sin efectos o se quedo sin significancia
    # tras la correccion. Son dos diagnosticos distintos.
    by_effect = [
        c for c in candidates
        if abs(c.estimate.effect) >= c.min_effect and c.estimate.excludes_null
    ]
    survivors = [
        c for i, c in enumerate(candidates)
        if accepted[i] and abs(c.estimate.effect) >= c.min_effect
        and c.estimate.excludes_null
    ]

    # La forma umbral solo se propone si la MONOTONA de la misma pareja no ha
    # sobrevivido. Cuando una relacion es monotona, su version en escalon
    # tambien sale significativa: son la misma señal contada dos veces, y
    # publicar las dos duplicaria el recuento de "descubrimientos" sin añadir
    # una sola observacion. El umbral existe para las relaciones EN ESCALON, que
    # es justo donde la monotona no llega.
    monotone_pairs = {
        (c.predictor, c.outcome) for c in survivors if c.form == MONOTONE
    }
    survivors = [
        c for c in survivors
        if not (c.form == THRESHOLD and (c.predictor, c.outcome) in monotone_pairs)
    ]

    # Por efecto sostenido, no por valor p: entre dos supervivientes, el que mas
    # dice es el que mas efecto aguanta en el peor caso de su intervalo.
    survivors.sort(key=lambda c: c.estimate.magnitude_lower, reverse=True)

    # De cada (etiqueta, outcome) se queda UN nivel: el de efecto mas sostenido.
    # Con k niveles salen k contrastes que no son independientes -- si "sports"
    # esta por encima del resto, las otras tres categorias quedan por debajo por
    # aritmetica, y el tablero se llenaria de cuatro imagenes del mismo hallazgo.
    # Es el mismo criterio que arriba con la forma umbral: una señal, una
    # hipotesis. El nivel elegido es el que mas separa, que es el que de verdad
    # define la particion.
    seen_group: set[tuple[str, str]] = set()
    deduped: list[_Candidate] = []
    for c in survivors:
        if c.form == GROUP_CONTRAST:
            key = (c.predictor, c.outcome)
            if key in seen_group:
                continue
            seen_group.add(key)
        deduped.append(c)
    survivors = deduped

    hypotheses = [
        Hypothesis(
            description=describe(
                c.form, c.predictor, c.outcome, c.estimate.direction,
                level=c.level, params=c.params, source=src),
            status=PROPOSED,
            confidence=0.0,      # dentro de muestra no hay confianza que dar
            sample_count=0,
            created_at=created, updated_at=created,
            form=c.form, source=src, predictor=c.predictor,
            outcome=c.outcome, level=c.level,
            direction=c.estimate.direction, params=dict(c.params),
            discovery=c.estimate, tested_in_pass=len(candidates),
            status_reason=(
                f"propuesta a partir de {c.estimate.n} observaciones de la "
                f"ventana de descubrimiento; {len(candidates)} relaciones "
                f"contrastadas en la pasada (FDR de Benjamini-Hochberg "
                f"al {alpha:.0%})"
            ),
        )
        for c in survivors[:max_proposals]
    ]
    return Proposal(
        hypotheses=hypotheses, tested=len(candidates),
        survived_effect=len(by_effect), survived_fdr=len(survivors), alpha=alpha,
    )
