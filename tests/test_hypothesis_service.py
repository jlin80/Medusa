"""Orquestacion del HE, sin BD y sin red.

Se ejercitan los dos metodos que deciden algo (`discover` y `evaluate_all`) y el
contrato de las fuentes. Lo que se protege aqui es la coordinacion, que es donde
viven los fallos que ni el generador ni el evaluador pueden ver solos:

  - que una hipotesis ya conocida (incluida una RECHAZADA) no vuelva a nacer,
  - que el tope de hipotesis abiertas por fuente se respete,
  - que el ciclo completo propone-espera-valida funcione de punta a punta,
  - que las fuentes declaren lineage coherente (unidad, parejas bloqueadas).
"""

from __future__ import annotations

import datetime as dt
import random

import pytest

from medusa.intelligence.hypothesis import sources
from medusa.intelligence.hypothesis.service import HypothesisService
from medusa.intelligence.hypothesis.types import (
    PROPOSED,
    REJECTED,
    VALIDATED,
    Observation,
)

UTC = dt.timezone.utc


class _Log:
    def __init__(self) -> None:
        self.events: list[tuple] = []

    def info(self, event="", **k): self.events.append((event, k))
    def debug(self, *a, **k): pass
    def warning(self, *a, **k): pass
    def error(self, *a, **k): pass


@pytest.fixture
def svc():
    return HypothesisService(_Log())


def serie(n, start, *, pendiente=-2.0, ruido=3.0, seed=1, source="signals"):
    rng = random.Random(seed)
    return [
        Observation(source=source, entity=f"e{i}",
                    ts=start + dt.timedelta(hours=i),
                    features={"cond": float(i), "ruidosa": rng.random()},
                    outcomes={"res": pendiente * i + rng.gauss(0, ruido)})
        for i in range(n)
    ]


# ------------------------------------------------------------------ arranque --
def test_el_servicio_arranca_apagado(svc):
    assert svc.enabled is False
    info = svc.info()
    assert info["enabled"] is False
    assert info["hypotheses_hardcoded"] is False
    assert info["validates_in_sample"] is False
    assert info["measures_causality"] is False
    assert info["can_place_orders"] is False
    assert info["emits_signals"] is False
    assert info["last_run"] is None


def test_info_no_toca_la_bd(svc):
    """Se llama desde /hypotheses/info y desde el panel: tiene que ser barato."""
    assert set(svc.info()) >= {"interval_seconds", "sources", "forms", "alpha"}


# ------------------------------------------------------------ descubrimiento --
def test_descubre_sobre_la_ventana_reciente(svc):
    ahora = dt.datetime(2026, 6, 1, tzinfo=UTC)
    datos = serie(400, ahora - dt.timedelta(days=15))
    p = svc.discover(datos, "signals", now=ahora, existing_ids=set())
    assert p.hypotheses
    assert p.variables > 0
    assert all(h.status == PROPOSED and h.confidence == 0.0 for h in p.hypotheses)
    assert all(h.created_at == ahora for h in p.hypotheses)


def test_lo_anterior_a_la_ventana_de_descubrimiento_no_propone(svc):
    """La ventana es corta a proposito: un año propondria relaciones de un
    regimen de mercado que ya no existe."""
    ahora = dt.datetime(2026, 6, 1, tzinfo=UTC)
    viejos = serie(400, ahora - dt.timedelta(days=400))
    p = svc.discover(viejos, "signals", now=ahora, existing_ids=set())
    assert p.hypotheses == []


def test_sin_muestra_suficiente_no_propone_nada(svc):
    ahora = dt.datetime(2026, 6, 1, tzinfo=UTC)
    p = svc.discover(serie(5, ahora - dt.timedelta(days=1)), "signals",
                     now=ahora, existing_ids=set())
    assert p.hypotheses == []
    assert p.tested == 0


def test_una_hipotesis_ya_conocida_no_vuelve_a_nacer(svc):
    ahora = dt.datetime(2026, 6, 1, tzinfo=UTC)
    datos = serie(400, ahora - dt.timedelta(days=15))
    primera = svc.discover(datos, "signals", now=ahora, existing_ids=set())
    assert primera.hypotheses
    ids = {h.id for h in primera.hypotheses}
    segunda = svc.discover(datos, "signals", now=ahora, existing_ids=ids)
    assert segunda.hypotheses == []
    # Y los contrastes se hicieron igual: el descarte es por identidad, no por
    # dejar de mirar.
    assert segunda.tested == primera.tested > 0


def test_una_rechazada_no_se_puede_blanquear(svc):
    """El caso que importa. `existing_ids` incluye las rechazadas, asi que el
    redescubrimiento cae en la fila cerrada y no abre una nueva."""
    ahora = dt.datetime(2026, 6, 1, tzinfo=UTC)
    datos = serie(400, ahora - dt.timedelta(days=15))
    original = svc.discover(datos, "signals", now=ahora, existing_ids=set())
    rechazada = original.hypotheses[0]
    rechazada.status = REJECTED

    # Pasadas posteriores con datos aun mas favorables: sigue sin renacer.
    mas_datos = serie(800, ahora - dt.timedelta(days=15), ruido=0.5, seed=99)
    for dia in range(1, 6):
        futuro = ahora + dt.timedelta(days=dia)
        p = svc.discover(mas_datos, "signals", now=futuro,
                         existing_ids={rechazada.id})
        assert rechazada.id not in {h.id for h in p.hypotheses}


def test_el_tope_de_abiertas_por_fuente_se_respeta(svc):
    ahora = dt.datetime(2026, 6, 1, tzinfo=UTC)
    datos = serie(400, ahora - dt.timedelta(days=15))
    tope = svc.s.hypothesis_max_open_per_source
    p = svc.discover(datos, "signals", now=ahora, existing_ids=set(),
                     open_count=tope)
    assert p.hypotheses == [], "sin sitio no se propone nada"
    p2 = svc.discover(datos, "signals", now=ahora, existing_ids=set(),
                      open_count=tope - 1)
    assert len(p2.hypotheses) <= 1


def test_las_parejas_bloqueadas_de_la_fuente_se_aplican(svc):
    """`discover` tiene que leer el lineage de `SPECS`, no ignorarlo."""
    ahora = dt.datetime(2026, 6, 1, tzinfo=UTC)
    rng = random.Random(5)
    datos = [
        Observation(source="signals", entity=f"e{i}",
                    ts=ahora - dt.timedelta(days=10, hours=i),
                    features={"entry_price": 0.1 + i * 0.002},
                    outcomes={"roi": -(0.1 + i * 0.002) * 3 + rng.gauss(0, .01)})
        for i in range(300)
    ]
    p = svc.discover(datos, "signals", now=ahora, existing_ids=set())
    assert not [h for h in p.hypotheses
                if (h.predictor, h.outcome) == ("entry_price", "roi")], (
        "entry_price -> roi es una division, no un hallazgo")


# ------------------------------------------------------------- evaluacion ----
def test_evalua_y_anota_la_transicion(svc):
    ahora = dt.datetime(2026, 6, 1, tzinfo=UTC)
    nacimiento = ahora - dt.timedelta(days=60)
    p = svc.discover(serie(400, nacimiento - dt.timedelta(days=20)),
                     "signals", now=nacimiento, existing_ids=set())
    vivas = p.hypotheses
    assert vivas

    # Nada nuevo: siguen en `proposed` y no hay transicion que anotar.
    quietas, sin_cambios = svc.evaluate_all(vivas, {"signals": []}, now=ahora)
    assert sin_cambios == []
    assert all(h.status == PROPOSED and h.confidence == 0.0 for h in quietas)

    # Llegan observaciones posteriores a la valla: ahora si.
    nuevas = serie(400, nacimiento + dt.timedelta(days=1), seed=7)
    movidas, cambios = svc.evaluate_all(vivas, {"signals": nuevas}, now=ahora)
    assert cambios, "tenia que haber al menos una transicion"
    for c in cambios:
        assert c["from"] == PROPOSED
        assert c["sample_count"] > 0
        assert c["reason"]
    monotona = [h for h in movidas if h.predictor == "cond"][0]
    assert monotona.status == VALIDATED
    assert monotona.confidence > 0.0


def test_el_ciclo_completo_propone_espera_y_valida(svc):
    """De punta a punta, con el reloj avanzando: es la unica forma de comprobar
    que la valla no se salta en la coordinacion."""
    t0 = dt.datetime(2026, 1, 1, tzinfo=UTC)
    historia = serie(300, t0)

    # Pasada 1: propone con lo que hay. Cero evidencia por construccion.
    t1 = t0 + dt.timedelta(days=20)
    p = svc.discover(historia, "signals", now=t1, existing_ids=set())
    vivas = p.hypotheses
    assert vivas and all(h.sample_count == 0 for h in vivas)

    # Pasada 2: pasa el tiempo pero solo con datos VIEJOS. Sigue sin evidencia.
    t2 = t1 + dt.timedelta(days=10)
    vivas, cambios = svc.evaluate_all(vivas, {"signals": historia}, now=t2)
    assert cambios == []
    assert all(h.sample_count == 0 for h in vivas)

    # Pasada 3: llega realidad nueva y el motor puede pronunciarse.
    t3 = t2 + dt.timedelta(days=30)
    futuro = historia + serie(300, t1 + dt.timedelta(hours=1), seed=42)
    vivas, cambios = svc.evaluate_all(vivas, {"signals": futuro}, now=t3)
    assert cambios
    validadas = [h for h in vivas if h.status == VALIDATED]
    assert validadas
    assert all(h.created_at == t1 for h in vivas), "la valla no se movio"


def test_una_hipotesis_sin_datos_de_su_fuente_no_avanza(svc):
    """Si el IFE esta apagado, sus fuentes vienen vacias y sus hipotesis se
    quedan quietas en vez de decidirse con nada."""
    ahora = dt.datetime(2026, 6, 1, tzinfo=UTC)
    nacimiento = ahora - dt.timedelta(days=60)
    p = svc.discover(serie(400, nacimiento - dt.timedelta(days=20),
                           source="flow_wallets"),
                     "flow_wallets", now=nacimiento, existing_ids=set())
    vivas, cambios = svc.evaluate_all(p.hypotheses, {}, now=ahora)
    assert cambios == []
    assert all(h.sample_count == 0 for h in vivas)


# ---------------------------------------------------------------- fuentes ----
def test_todas_las_fuentes_declaran_una_unidad_valida():
    assert sources.SPECS
    for name, spec in sources.SPECS.items():
        assert spec.name == name
        assert spec.unit in (sources.EVENT, sources.ENTITY), spec.unit
        assert spec.title


def test_una_fuente_de_estado_produce_una_observacion_por_entidad():
    """La trampa de las tablas de estado: cosecharlas en cada pasada metería la
    misma wallet cuarenta veces al dia y estrecharia los intervalos hasta hacer
    significativo casi todo."""
    a = Observation(source="flow_wallets", entity="0xabc",
                    ts=dt.datetime(2026, 1, 1, tzinfo=UTC), features={"x": 1.0})
    b = Observation(source="flow_wallets", entity="0xabc",
                    ts=dt.datetime(2026, 5, 9, tzinfo=UTC), features={"x": 9.0})
    assert sources.observation_uid(a) == sources.observation_uid(b), (
        "la segunda cosecha de la misma wallet tiene que chocar con la primera")


def test_una_fuente_de_eventos_produce_una_observacion_por_hecho():
    a = Observation(source="signals", entity="m1",
                    ts=dt.datetime(2026, 1, 1, tzinfo=UTC))
    b = Observation(source="signals", entity="m1",
                    ts=dt.datetime(2026, 1, 2, tzinfo=UTC))
    assert sources.observation_uid(a) != sources.observation_uid(b)


def test_el_uid_es_estable_entre_cosechas():
    o = Observation(source="signals", entity="m1",
                    ts=dt.datetime(2026, 1, 1, tzinfo=UTC))
    assert sources.observation_uid(o) == sources.observation_uid(o)


def test_las_parejas_bloqueadas_llevan_columnas_reales():
    """Un typo en una pareja bloqueada la desactiva en silencio y deja pasar la
    tautologia que iba a impedir."""
    for spec in sources.SPECS.values():
        for pred, out in spec.blocked_pairs:
            assert pred and out and pred != out, (spec.name, pred, out)


def test_un_hueco_de_la_fuente_no_se_convierte_en_cero():
    """`strategy_signals.spread` es NULL en las filas viejas: un 0.0 diria
    «operar aqui era gratis»."""
    assert sources._f(None) is None
    assert sources._f(float("nan")) is None
    assert sources._f("no es un numero") is None
    assert sources._f(0.04) == 0.04
    assert "vacia" not in sources._clean({"llena": 1.0, "vacia": None})
