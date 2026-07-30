"""La valla temporal y el ciclo de vida. El test mas importante del paquete.

Si algo de aqui se rompe, el motor deja de medir y empieza a confirmarse: propone
la relacion mas llamativa de una ventana y la "valida" con la misma ventana, que es
elegir el numero despues de ver la ruleta. Lo que se fija:

  - solo cuentan las observaciones POSTERIORES a `created_at`,
  - `rejected` es terminal (candado contra el blanqueo de hipotesis),
  - un giro de signo es un RECHAZO, no una falta de confirmacion,
  - `confidence` vale 0.0 sin muestra, con el intervalo cruzando el nulo y con el
    signo invertido,
  - los parametros congelados (el corte) se reutilizan tal cual.
"""

from __future__ import annotations

import datetime as dt
import random

import pytest

from medusa.intelligence.hypothesis import evaluator
from medusa.intelligence.hypothesis.types import (
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
VALLA = dt.datetime(2026, 3, 1, tzinfo=UTC)
AHORA = dt.datetime(2026, 9, 1, tzinfo=UTC)


def h_monotona(direction=-1, status=PROPOSED, discovery_effect=-0.8):
    return Hypothesis(
        description="A mayor cond, menor res [s]", status=status,
        created_at=VALLA, updated_at=VALLA,
        form=MONOTONE, source="s", predictor="cond", outcome="res",
        direction=direction,
        discovery=EffectEstimate(n=150, effect=discovery_effect,
                                 lower=discovery_effect - .1,
                                 upper=discovery_effect + .1, p_value=1e-9),
        tested_in_pass=40,
    )


def serie(n, start, pendiente=-2.0, ruido=0.0, seed=1, source="s"):
    rng = random.Random(seed)
    return [
        Observation(source=source, entity=f"e{i}",
                    ts=start + dt.timedelta(hours=i),
                    features={"cond": float(i)},
                    outcomes={"res": pendiente * i + rng.gauss(0, ruido)})
        for i in range(n)
    ]


# ------------------------------------------------------------------ la valla --
def test_lo_anterior_a_la_valla_no_cuenta():
    h = h_monotona()
    antiguas = serie(300, VALLA - dt.timedelta(days=60))
    assert evaluator.out_of_sample(h, antiguas) == []
    despues = evaluator.evaluate(h, antiguas, now=AHORA)
    assert despues.sample_count == 0
    assert despues.status == PROPOSED
    assert despues.confidence == 0.0
    assert "sin observaciones posteriores" in despues.status_reason


def test_la_comparacion_es_estricta():
    """Una observacion en el instante exacto de la creacion pudo estar en la
    ventana de descubrimiento: en la duda se descarta."""
    h = h_monotona()
    justo = [Observation(source="s", entity="e", ts=VALLA,
                         features={"cond": 1.0}, outcomes={"res": 1.0})]
    assert evaluator.out_of_sample(h, justo) == []


def test_solo_cuentan_las_observaciones_de_su_fuente():
    """Una hipotesis sobre `signals` no puede validarse con filas de `trades`
    aunque las columnas se llamen igual."""
    h = h_monotona()
    ajenas = serie(300, VALLA + dt.timedelta(days=1), source="otra_fuente")
    assert evaluator.out_of_sample(h, ajenas) == []


def test_se_mezclan_bien_las_de_antes_y_las_de_despues():
    h = h_monotona()
    datos = (serie(200, VALLA - dt.timedelta(days=90))
             + serie(120, VALLA + dt.timedelta(days=1), seed=2))
    fuera = evaluator.out_of_sample(h, datos)
    assert len(fuera) == 120
    assert all(o.ts > VALLA for o in fuera)


# --------------------------------------------------------------- veredictos ---
def test_replica_y_se_valida():
    h = h_monotona()
    nuevas = serie(200, VALLA + dt.timedelta(days=1), pendiente=-2.0, ruido=5.0)
    r = evaluator.evaluate(h, nuevas, min_test_samples=60, now=AHORA)
    assert r.status == VALIDATED
    assert r.sample_count == 200
    assert r.confidence > 0.5
    assert r.decided_at == AHORA
    assert "replica fuera de muestra" in r.status_reason


def test_poca_muestra_deja_la_hipotesis_en_pruebas():
    h = h_monotona()
    nuevas = serie(20, VALLA + dt.timedelta(days=1), ruido=5.0)
    r = evaluator.evaluate(h, nuevas, min_test_samples=60, now=AHORA)
    assert r.status == TESTING
    assert r.sample_count == 20
    assert r.decided_at is None
    assert r.first_tested_at == AHORA
    assert "de las 60" in r.status_reason


def test_un_giro_de_signo_es_un_rechazo_sin_esperar_mas_datos():
    """Una hipotesis que afirma un signo y encuentra el contrario CON
    significancia ya ha fallado: mantenerla «en pruebas» solo alarga la vida de un
    enunciado falso."""
    h = h_monotona(direction=-1)
    invertidas = serie(40, VALLA + dt.timedelta(days=1), pendiente=+2.0, ruido=3.0)
    r = evaluator.evaluate(h, invertidas, min_test_samples=200, now=AHORA)
    assert r.status == REJECTED
    assert r.confidence == 0.0, "una hipotesis contradicha no tiene confianza"
    assert "se invierte" in r.status_reason


def test_no_replicar_con_muestra_de_sobra_es_un_rechazo():
    """Con esta n, si el efecto propuesto existiera, se veria. Eso no es «aun no
    se sabe»: es que no replica."""
    h = h_monotona()
    rng = random.Random(7)
    ruido = [
        Observation(source="s", entity=f"e{i}",
                    ts=VALLA + dt.timedelta(days=1, hours=i),
                    features={"cond": rng.random()}, outcomes={"res": rng.random()})
        for i in range(400)
    ]
    r = evaluator.evaluate(h, ruido, min_test_samples=60, reject_after=200, now=AHORA)
    assert r.status == REJECTED
    assert r.confidence == 0.0
    assert "no replica" in r.status_reason


def test_sin_replicar_pero_sin_muestra_para_descartar_sigue_en_pruebas():
    """La asimetria deliberada: rechazar pronto por falta de datos tiraria
    hipotesis buenas por impaciencia."""
    h = h_monotona()
    rng = random.Random(7)
    ruido = [
        Observation(source="s", entity=f"e{i}",
                    ts=VALLA + dt.timedelta(days=1, hours=i),
                    features={"cond": rng.random()}, outcomes={"res": rng.random()})
        for i in range(90)
    ]
    r = evaluator.evaluate(h, ruido, min_test_samples=60, reject_after=200, now=AHORA)
    assert r.status == TESTING
    assert r.confidence == 0.0, "el intervalo cruza el nulo"
    assert "compatible con el nulo" in r.status_reason


# ------------------------------------------------- el grafo de transiciones ---
def test_rejected_es_terminal():
    """El candado contra el blanqueo: sin esto bastaria con esperar la pasada en la
    que el ruido saliera a favor."""
    h = h_monotona(status=REJECTED)
    perfectas = serie(500, VALLA + dt.timedelta(days=1), pendiente=-2.0)
    r = evaluator.evaluate(h, perfectas, now=AHORA)
    assert r.status == REJECTED
    assert r.sample_count == h.sample_count, "no se vuelve a medir un expediente cerrado"
    assert r.updated_at == h.updated_at


def test_una_validada_puede_caer_a_rechazada():
    """`validated` NO es terminal: fijar la conclusion por haber llegado primero
    convertiria el motor en un archivo de aciertos antiguos."""
    h = h_monotona(status=VALIDATED)
    invertidas = serie(300, VALLA + dt.timedelta(days=1), pendiente=+2.0, ruido=3.0)
    r = evaluator.evaluate(h, invertidas, now=AHORA)
    assert r.status == REJECTED


def test_una_validada_que_se_debilita_no_vuelve_a_pruebas():
    """No existe validated -> testing. Una validacion que se queda floja no se
    borra: se lee en la confianza, que baja sola."""
    h = h_monotona(status=VALIDATED)
    rng = random.Random(3)
    ruido = [
        Observation(source="s", entity=f"e{i}",
                    ts=VALLA + dt.timedelta(days=1, hours=i),
                    features={"cond": rng.random()}, outcomes={"res": rng.random()})
        for i in range(80)
    ]
    r = evaluator.evaluate(h, ruido, min_test_samples=60, reject_after=500, now=AHORA)
    assert r.status == VALIDATED
    assert r.confidence == 0.0, "la confianza cae aunque el estado se conserve"
    assert "no es una transicion legal" in r.status_reason


def test_el_grafo_de_transiciones_es_el_declarado():
    assert can_transition(PROPOSED, TESTING)
    assert can_transition(PROPOSED, VALIDATED)
    assert can_transition(TESTING, VALIDATED)
    assert can_transition(TESTING, REJECTED)
    assert can_transition(VALIDATED, REJECTED)
    # Los tres caminos prohibidos.
    assert not can_transition(REJECTED, PROPOSED)
    assert not can_transition(REJECTED, TESTING)
    assert not can_transition(VALIDATED, TESTING)
    # Quedarse igual siempre es legal.
    for estado in (PROPOSED, TESTING, VALIDATED, REJECTED):
        assert can_transition(estado, estado)


# --------------------------------------------------------------- confianza ----
def test_la_confianza_crece_con_la_muestra_replicando():
    h = h_monotona()
    poca = evaluator.evaluate(
        h, serie(70, VALLA + dt.timedelta(days=1), ruido=8.0),
        min_test_samples=60, now=AHORA)
    mucha = evaluator.evaluate(
        h, serie(600, VALLA + dt.timedelta(days=1), ruido=8.0),
        min_test_samples=60, now=AHORA)
    assert 0.0 < poca.confidence < mucha.confidence <= 1.0


def test_la_confianza_ignora_el_efecto_cuando_el_signo_esta_al_reves():
    """Una hipotesis con la direccion invertida no esta «poco confirmada»: esta
    contradicha, y premiarla por la fuerza del efecto premiaria el fallo."""
    h = h_monotona(direction=+1)
    est = EffectEstimate(n=500, effect=-0.9, lower=-0.95, upper=-0.85)
    assert evaluator.confidence_of(h, est, 60) == 0.0


# ------------------------------------------------- parametros congelados ------
def test_la_forma_umbral_reutiliza_el_corte_congelado():
    """Recalcular el corte sobre los datos de prueba seria elegir el que mejor
    queda en el test y llamarlo replicacion."""
    h = Hypothesis(
        description="Con cond por encima de 100, res es mas alto [s]",
        created_at=VALLA, updated_at=VALLA, form=THRESHOLD, source="s",
        predictor="cond", outcome="res", direction=1, params={"cut": 100.0},
        discovery=EffectEstimate(n=200, effect=2.0, lower=1.5, upper=2.5))
    rng = random.Random(2)
    nuevas = [
        Observation(source="s", entity=f"e{i}",
                    ts=VALLA + dt.timedelta(days=1, hours=i),
                    features={"cond": float(i)},
                    outcomes={"res": (0.0 if i <= 100 else 40.0) + rng.gauss(0, 2)})
        for i in range(300)
    ]
    r = evaluator.evaluate(h, nuevas, min_test_samples=60, now=AHORA)
    assert r.status == VALIDATED
    assert r.test.effect > 0
    # El corte no se toca.
    assert r.params["cut"] == 100.0


def test_medir_sin_observaciones_no_revienta():
    h = h_monotona()
    assert evaluator.measure(h, []).n == 0


def test_evaluar_no_muta_la_hipotesis_recibida():
    """El servicio compara el antes y el despues para anotar la transicion."""
    h = h_monotona()
    antes = (h.status, h.confidence, h.sample_count)
    evaluator.evaluate(h, serie(300, VALLA + dt.timedelta(days=1), ruido=5.0),
                       now=AHORA)
    assert (h.status, h.confidence, h.sample_count) == antes


def test_la_evidencia_de_descubrimiento_sobrevive_a_la_evaluacion():
    """Es el registro de POR QUE se propuso, y lo que se compara con la
    replicacion: pisarlo borraria la unica forma de contrastar las dos."""
    h = h_monotona(discovery_effect=-0.83)
    r = evaluator.evaluate(h, serie(300, VALLA + dt.timedelta(days=1), ruido=5.0),
                           now=AHORA)
    assert r.discovery.effect == pytest.approx(-0.83)
    assert r.discovery.n == 150
    assert r.created_at == VALLA, "la valla no se mueve nunca"
