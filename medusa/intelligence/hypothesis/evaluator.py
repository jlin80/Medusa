"""Evaluacion fuera de muestra y ciclo de vida. Puro: sin BD y sin red.

ESTE FICHERO ES LA VALLA. El generador propone mirando datos; el evaluador decide
mirando SOLO los datos que el generador no pudo ver. La regla, entera, es una
linea:

    cuentan las observaciones cuyo `ts` es POSTERIOR a `created_at`.

Sin esa linea el motor seria una maquina de confirmarse: propone la relacion mas
llamativa de la ventana y despues la "valida" con la misma ventana, que es como
elegir el numero despues de ver la ruleta. Con ella, `sample_count` significa
algo -- "casos nuevos vistos desde que la hipotesis existe" -- y `confidence` puede
empezar en 0.0 y subir solo cuando llega realidad nueva.

EL GRAFO DE ESTADOS TIENE DOS ASIMETRIAS, LAS DOS DELIBERADAS:

  - `rejected` es TERMINAL. Una hipotesis rechazada no vuelve a `proposed` ni
    aunque el generador la redescubra: como el `id` es el hash del enunciado, la
    redeteccion cae en la fila cerrada y no abre una nueva. Sin esto bastaria con
    esperar la pasada en la que el ruido salga a favor, y el motor acabaria
    publicando exactamente las relaciones que mas veces ha fallado.
  - `validated` NO es terminal. Puede caer a `rejected` si mas adelante una
    muestra mayor la contradice con intervalo que excluye el nulo. Lo contrario
    -- fijar la conclusion por haber llegado primero -- convertiria el motor en un
    archivo de aciertos antiguos. Lo que no existe es la vuelta de `validated` a
    `testing`: una validacion que se debilita no se borra, se lee en la
    `confidence`, que baja sola.
"""

from __future__ import annotations

import dataclasses
import datetime as dt

from medusa.intelligence.hypothesis import features as feat
from medusa.intelligence.hypothesis import stats
from medusa.intelligence.hypothesis.types import (
    GROUP_CONTRAST,
    MONOTONE,
    PROPOSED,
    REJECTED,
    TESTING,
    THRESHOLD,
    VALIDATED,
    EffectEstimate,
    Hypothesis,
    Observation,
    can_transition,
)

UTC = dt.timezone.utc

# Efecto que se considera "grande" en cada escala, y que sirve de referencia para
# normalizar la confianza. Son CONVENCIONES declaradas, no verdades: 0.5 de rho
# es una asociacion de rangos fuerte, y 0.8 de d es la convencion clasica de
# "efecto grande". Viven aqui, con nombre, en vez de escondidas en una formula.
REFERENCE_EFFECT: dict[str, float] = {
    MONOTONE: 0.5,
    GROUP_CONTRAST: 0.8,
    THRESHOLD: 0.8,
}


def out_of_sample(
    hypothesis: Hypothesis, observations: list[Observation],
) -> list[Observation]:
    """Observaciones que la hipotesis NO pudo ver. La comparacion es estricta.

    Estricta (`>`) y no `>=`: una observacion cuyo instante coincide exactamente
    con la creacion pudo estar en la ventana de descubrimiento, y en la duda la
    resolucion correcta es descartarla. Cuesta una observacion; la alternativa
    cuesta la validez.
    """
    fence = hypothesis.created_at
    if fence is None:
        return list(observations)
    return [o for o in observations
            if o.source == hypothesis.source and o.ts > fence]


def measure(
    hypothesis: Hypothesis, observations: list[Observation],
) -> EffectEstimate:
    """Reestima el efecto de la hipotesis sobre las observaciones dadas.

    Usa EXACTAMENTE la misma forma y los mismos parametros congelados que se
    fijaron al proponer -- el mismo corte, el mismo nivel, el mismo par de
    variables. Es lo que hace que el numero de aqui sea comparable con el del
    descubrimiento en vez de ser otro contraste distinto con el mismo nombre.
    """
    if not observations:
        return EffectEstimate()
    if hypothesis.form == MONOTONE:
        xs, ys = feat.paired_values(
            observations, hypothesis.predictor, hypothesis.outcome)
        return EffectEstimate(**stats.correlation_estimate(xs, ys))
    if hypothesis.form == GROUP_CONTRAST:
        inside, outside = feat.split_by_level(
            observations, hypothesis.predictor, hypothesis.level,
            hypothesis.outcome)
        return EffectEstimate(**stats.standardized_difference(inside, outside))
    if hypothesis.form == THRESHOLD:
        cut = float(hypothesis.params.get("cut", 0.0))
        above, below = feat.split_by_cut(
            observations, hypothesis.predictor, cut, hypothesis.outcome)
        return EffectEstimate(**stats.standardized_difference(above, below))
    return EffectEstimate()


def confidence_of(hypothesis: Hypothesis, estimate: EffectEstimate,
                  min_samples: int) -> float:
    """Confianza en [0,1] a partir de la evidencia FUERA DE MUESTRA.

    Vale 0.0 en tres casos, todos correctos: sin observaciones nuevas, con
    intervalo que cruza el nulo, y -- este es el importante -- cuando el signo
    observado es el CONTRARIO al que la hipotesis afirma. Una hipotesis con la
    direccion invertida no esta "poco confirmada": esta contradicha, y darle
    confianza por la fuerza del efecto premiaria justo el fallo.
    """
    if estimate.n <= 0 or estimate.direction != hypothesis.direction:
        return 0.0
    reference = REFERENCE_EFFECT.get(hypothesis.form, 0.5)
    return stats.evidence_confidence(
        estimate.magnitude_lower, estimate.n, reference, min_samples)


def _verdict(
    hypothesis: Hypothesis, est: EffectEstimate, *,
    min_test_samples: int, min_effect_rho: float, min_effect_d: float,
    reject_after: int,
) -> tuple[str, str]:
    """Estado propuesto y su motivo, en prosa. No aplica nada: solo decide."""
    min_effect = min_effect_rho if hypothesis.form == MONOTONE else min_effect_d
    replicates = est.direction == hypothesis.direction and est.excludes_null

    if est.n <= 0:
        return PROPOSED, ("sin observaciones posteriores a su creacion: no hay "
                          "nada que la confirme ni que la desmienta todavia")

    # Contradiccion con intervalo que excluye el nulo. Se rechaza SIN esperar a
    # `min_test_samples`: una hipotesis que afirma un signo y encuentra el
    # contrario con significancia ya ha fallado, y mantenerla en pruebas
    # "hasta tener mas datos" solo alarga la vida de un enunciado falso.
    if est.excludes_null and est.direction == -hypothesis.direction:
        return REJECTED, (
            f"la direccion se invierte fuera de muestra ({est.effect:+.3f} "
            f"con n={est.n}, intervalo [{est.lower:+.3f}, {est.upper:+.3f}] "
            f"al margen del nulo): contradicha, no solo sin confirmar")

    if est.n < min_test_samples:
        return TESTING, (
            f"{est.n} de las {min_test_samples} observaciones nuevas que hacen "
            f"falta para emitir un veredicto")

    if replicates and abs(est.effect) >= min_effect:
        return VALIDATED, (
            f"replica fuera de muestra: efecto {est.effect:+.3f} con n={est.n}, "
            f"intervalo [{est.lower:+.3f}, {est.upper:+.3f}] al margen del nulo "
            f"y en la direccion propuesta")

    # Muestra de sobra y sigue sin verse nada: no replica. Esto NO es "aun no se
    # sabe" -- con esta n, si el efecto propuesto existiera, se veria.
    if est.n >= reject_after and not est.excludes_null:
        return REJECTED, (
            f"no replica: con n={est.n} el intervalo sigue cruzando el nulo "
            f"([{est.lower:+.3f}, {est.upper:+.3f}]), muestra suficiente para "
            f"haber visto el efecto propuesto ({hypothesis.discovery.effect:+.3f})")

    return TESTING, (
        f"en pruebas con n={est.n}: efecto {est.effect:+.3f}, intervalo "
        f"[{est.lower:+.3f}, {est.upper:+.3f}] — todavia compatible con el nulo")


def evaluate(
    hypothesis: Hypothesis,
    observations: list[Observation],
    *,
    min_test_samples: int = 60,
    min_effect_rho: float = 0.15,
    min_effect_d: float = 0.25,
    reject_after: int = 200,
    confidence_min_samples: int = 60,
    now: dt.datetime | None = None,
) -> Hypothesis:
    """Devuelve la hipotesis actualizada con su evidencia fuera de muestra.

    No muta la que recibe: devuelve una copia. Asi el servicio puede comparar el
    antes y el despues para saber si hubo transicion y anotarla en el histórico.

    Una hipotesis ya rechazada se devuelve INTACTA. Es terminal, y volver a
    medirla cada hora solo gastaria CPU en un expediente cerrado.
    """
    if hypothesis.status == REJECTED:
        return dataclasses.replace(hypothesis)

    moment = now or dt.datetime.now(UTC)
    fresh = out_of_sample(hypothesis, observations)
    est = measure(hypothesis, fresh)
    target, reason = _verdict(
        hypothesis, est, min_test_samples=min_test_samples,
        min_effect_rho=min_effect_rho, min_effect_d=min_effect_d,
        reject_after=reject_after)

    # La guarda del grafo de estados. Si la transicion no es legal (el caso real
    # es `validated` -> `testing` cuando la evidencia se debilita), se conserva el
    # estado y se actualiza la evidencia: la confianza bajara sola, que es la
    # forma honesta de contar que una validacion se ha quedado floja.
    status = target if can_transition(hypothesis.status, target) else hypothesis.status
    if status != target:
        reason = (f"se mantiene en «{status}» (la evidencia actual sugeriria "
                  f"«{target}», que no es una transicion legal desde ahi): "
                  + reason)

    first_tested = hypothesis.first_tested_at
    if first_tested is None and est.n > 0:
        first_tested = moment
    decided = hypothesis.decided_at
    if status in (VALIDATED, REJECTED) and hypothesis.status != status:
        decided = moment

    return dataclasses.replace(
        hypothesis,
        status=status,
        confidence=confidence_of(hypothesis, est, confidence_min_samples),
        sample_count=est.n,
        updated_at=moment,
        test=est,
        status_reason=reason,
        first_tested_at=first_tested,
        decided_at=decided,
    )
