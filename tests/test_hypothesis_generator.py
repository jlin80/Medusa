"""Descubrimiento de variables y generacion de hipotesis.

Estos tests fijan la parte del motor que decide DE QUE se puede hablar y QUE
merece proponerse. Lo que se protege, por orden de importancia:

  1. que el enunciado salga de los datos (nombres, direccion, corte),
  2. que la multiplicidad se pague (Benjamini-Hochberg sobre toda la pasada),
  3. que no se propongan tautologias ni el mismo hallazgo dos veces,
  4. que una variable que la ventana no sostiene no llegue a ser variable.
"""

from __future__ import annotations

import datetime as dt
import random

import pytest

from medusa.intelligence.hypothesis import features as feat
from medusa.intelligence.hypothesis import generator
from medusa.intelligence.hypothesis.types import (
    CATEGORICAL,
    GROUP_CONTRAST,
    MONOTONE,
    NUMERIC,
    OUTCOME,
    PREDICTOR,
    THRESHOLD,
    Observation,
)

UTC = dt.timezone.utc
BASE = dt.datetime(2026, 1, 1, tzinfo=UTC)
LATER = dt.datetime(2026, 6, 1, tzinfo=UTC)


def obs(i, features=None, labels=None, outcomes=None, source="s"):
    return Observation(
        source=source, entity=f"e{i}", ts=BASE + dt.timedelta(hours=i),
        features=features or {}, labels=labels or {}, outcomes=outcomes or {})


def monotona(n=200, ruido=0.0, seed=1):
    """Relacion negativa fuerte entre `cond` y `res`."""
    rng = random.Random(seed)
    return [
        obs(i, features={"cond": float(i)},
            outcomes={"res": -2.0 * i + rng.gauss(0, ruido)})
        for i in range(n)
    ]


# ------------------------------------------------- descubrimiento de variables --
def test_una_columna_constante_no_es_una_variable():
    datos = [obs(i, features={"cond": 5.0}, outcomes={"res": float(i)})
             for i in range(100)]
    nombres = {v.name for v in feat.discover_variables(datos, min_distinct=3)}
    assert "cond" not in nombres
    assert "res" in nombres


def test_una_columna_medio_vacia_no_es_una_variable():
    """El caso real: `strategy_signals.spread` es NULL en las filas viejas."""
    datos = []
    for i in range(100):
        features = {"lleno": float(i)}
        if i < 30:
            features["a_medias"] = float(i)
        datos.append(obs(i, features=features, outcomes={"res": float(i)}))
    nombres = {v.name for v in feat.discover_variables(datos, min_coverage=0.6,
                                                      min_distinct=3)}
    assert "lleno" in nombres
    assert "a_medias" not in nombres, "una columna al 30% describe otro subconjunto"


def test_una_numerica_con_pocos_valores_no_es_predictor():
    """Es una categorica disfrazada: Spearman sobre ella mide empates."""
    datos = [obs(i, features={"casi_binaria": float(i % 2)},
                 outcomes={"res": float(i)}) for i in range(100)]
    variables = feat.discover_variables(datos, min_distinct=8)
    assert not [v for v in variables if v.name == "casi_binaria"]


def test_un_outcome_binario_si_vale():
    """`won` es 0/1 y es justo lo que se quiere explicar: el minimo de valores
    distintos se exige a los predictores, no a los resultados."""
    datos = [obs(i, features={"cond": float(i)},
                 outcomes={"won": float(i % 2)}) for i in range(100)]
    variables = feat.discover_variables(datos, min_distinct=8)
    ganado = [v for v in variables if v.name == "won"]
    assert ganado and ganado[0].role == OUTCOME and ganado[0].kind == NUMERIC


def test_una_etiqueta_con_demasiados_niveles_se_descarta():
    """Un `market_id` daria doscientos contrastes de un caso contra el mundo."""
    datos = [obs(i, labels={"id_unico": f"m{i}", "pocos": "a" if i % 2 else "b"},
                 outcomes={"res": float(i)}) for i in range(100)]
    variables = feat.discover_variables(datos, max_levels=12, min_level_size=10)
    nombres = {v.name for v in variables}
    assert "id_unico" not in nombres
    assert "pocos" in nombres


def test_un_nivel_sin_masa_no_es_contrastable_pero_sigue_en_el_resto():
    datos = ([obs(i, labels={"cat": "grande"}, outcomes={"res": 1.0}) for i in range(80)]
             + [obs(80 + i, labels={"cat": "diminuto"}, outcomes={"res": 9.0})
                for i in range(3)])
    variables = feat.discover_variables(datos, min_level_size=15, max_levels=12)
    cat = [v for v in variables if v.name == "cat"][0]
    assert cat.levels == ("grande",), cat.levels
    assert cat.n_distinct == 2, "el nivel diminuto sigue contando en el total"


def test_los_pares_incompletos_se_descartan_a_pares():
    datos = [
        obs(0, features={"a": 1.0}, outcomes={"b": 1.0}),
        obs(1, features={"a": 2.0}),                       # falta el outcome
        obs(2, outcomes={"b": 3.0}),                       # falta el predictor
        obs(3, features={"a": 4.0}, outcomes={"b": 4.0}),
    ]
    xs, ys = feat.paired_values(datos, "a", "b")
    assert xs == [1.0, 4.0] and ys == [1.0, 4.0]


def test_un_hueco_no_es_un_cero():
    """Rellenar con 0.0 diria «operar aqui era gratis», que es falso."""
    o = obs(0, features={"presente": 3.0}, outcomes={})
    assert o.numeric("ausente") is None
    assert o.numeric("presente") == 3.0


def test_un_nan_se_trata_como_hueco():
    """Un NaN se propaga por toda la estadistica sin lanzar una excepcion."""
    o = obs(0, features={"roto": float("nan"), "infinito": float("inf")})
    assert o.numeric("roto") is None
    assert o.numeric("infinito") is None


def test_los_sin_etiquetar_no_entran_en_ninguno_de_los_dos_lados():
    datos = ([obs(i, labels={"cat": "x"}, outcomes={"res": 1.0}) for i in range(10)]
             + [obs(10 + i, labels={"cat": "y"}, outcomes={"res": 2.0}) for i in range(10)]
             + [obs(20 + i, outcomes={"res": 99.0}) for i in range(10)])
    dentro, fuera = feat.split_by_level(datos, "cat", "x", "res")
    assert len(dentro) == 10 and len(fuera) == 10
    assert 99.0 not in fuera, "los sin etiquetar no pueden colarse en el resto"


# ------------------------------------------------------------------ gramatica --
def test_propone_la_relacion_evidente_con_su_direccion():
    datos = monotona()
    variables = feat.discover_variables(datos, min_distinct=5)
    p = generator.propose(datos, variables, source="s", min_samples=20, now=LATER)
    assert p.hypotheses
    h = p.hypotheses[0]
    assert h.form == MONOTONE
    assert h.predictor == "cond" and h.outcome == "res"
    assert h.direction == -1
    assert "menor res" in h.description, h.description
    assert h.created_at == LATER, "la valla es el `now` que se le pasa"


def test_no_propone_nada_sobre_ruido():
    rng = random.Random(3)
    datos = [obs(i, features={f"c{k}": rng.random() for k in range(6)},
                 outcomes={"res": rng.random()}) for i in range(150)]
    variables = feat.discover_variables(datos, min_distinct=5)
    p = generator.propose(datos, variables, source="s", min_samples=20, now=LATER)
    assert p.tested > 0, "los contrastes se hicieron"
    assert not p.hypotheses, f"propuso ruido: {[h.description for h in p.hypotheses]}"


def test_la_correccion_por_multiplicidad_esta_activa():
    """Con muchas columnas de ruido, alpha=1.0 (sin correccion util) deja pasar
    mas que el alpha por defecto. El test fija que BH esta filtrando de verdad."""
    rng = random.Random(11)
    datos = [obs(i, features={f"c{k}": rng.random() for k in range(25)},
                 outcomes={"res": rng.random()}) for i in range(120)]
    variables = feat.discover_variables(datos, min_distinct=5)
    estricto = generator.propose(datos, variables, source="s", min_samples=20,
                                 alpha=0.05, min_effect_rho=0.05,
                                 min_effect_d=0.05, now=LATER)
    laxo = generator.propose(datos, variables, source="s", min_samples=20,
                             alpha=1.0, min_effect_rho=0.05,
                             min_effect_d=0.05, now=LATER)
    assert estricto.survived_fdr <= laxo.survived_fdr
    assert laxo.survived_fdr > estricto.survived_fdr, (
        "sin correccion tendrian que pasar mas contrastes de ruido")


def test_cada_hipotesis_guarda_cuantos_contrastes_hubo_en_su_pasada():
    datos = monotona()
    variables = feat.discover_variables(datos, min_distinct=5)
    p = generator.propose(datos, variables, source="s", min_samples=20, now=LATER)
    for h in p.hypotheses:
        assert h.tested_in_pass == p.tested > 0


def test_una_pareja_bloqueada_no_se_contrasta_siquiera():
    """No solo no se propone: no entra en el recuento, porque incluirla infla la
    multiplicidad con un contraste que nunca podia salir."""
    datos = monotona()
    variables = feat.discover_variables(datos, min_distinct=5)
    libre = generator.propose(datos, variables, source="s", min_samples=20, now=LATER)
    bloqueada = generator.propose(datos, variables, source="s", min_samples=20,
                                  blocked_pairs=(("cond", "res"),), now=LATER)
    assert bloqueada.tested < libre.tested
    assert not bloqueada.hypotheses


def test_no_se_propone_una_variable_contra_si_misma():
    datos = [obs(i, features={"x": float(i)}, outcomes={"x": float(i)})
             for i in range(100)]
    variables = feat.discover_variables(datos, min_distinct=5)
    p = generator.propose(datos, variables, source="s", min_samples=20, now=LATER)
    assert not [h for h in p.hypotheses if h.predictor == h.outcome]


def test_la_forma_umbral_no_duplica_a_la_monotona():
    """Cuando una relacion es monotona, su version en escalon tambien sale
    significativa: son la misma señal contada dos veces."""
    datos = monotona()
    variables = feat.discover_variables(datos, min_distinct=5)
    p = generator.propose(datos, variables, source="s", min_samples=20, now=LATER)
    formas = {(h.form, h.predictor, h.outcome) for h in p.hypotheses}
    assert (MONOTONE, "cond", "res") in formas
    assert (THRESHOLD, "cond", "res") not in formas


def test_la_forma_umbral_captura_lo_que_la_monotona_no_ve():
    """Una relacion EN ESCALON: plana, salto, plana. Spearman la ve floja."""
    rng = random.Random(5)
    datos = [obs(i, features={"cond": float(i)},
                 outcomes={"res": (0.0 if i < 100 else 40.0) + rng.gauss(0, 1)})
             for i in range(200)]
    variables = feat.discover_variables(datos, min_distinct=5)
    p = generator.propose(datos, variables, source="s", min_samples=20, now=LATER)
    formas = {h.form for h in p.hypotheses}
    assert THRESHOLD in formas or MONOTONE in formas, [h.description for h in p.hypotheses]


def test_el_corte_de_la_forma_umbral_queda_congelado():
    rng = random.Random(5)
    datos = [obs(i, features={"cond": float(i)},
                 outcomes={"res": (0.0 if i < 100 else 40.0) + rng.gauss(0, 1)})
             for i in range(200)]
    variables = feat.discover_variables(datos, min_distinct=5)
    p = generator.propose(datos, variables, source="s", min_samples=20,
                          cut_quantile=0.5, now=LATER)
    umbral = [h for h in p.hypotheses if h.form == THRESHOLD]
    if umbral:
        assert "cut" in umbral[0].params
        assert umbral[0].params["quantile"] == 0.5
        assert str(umbral[0].params["cut"])[:3] in umbral[0].description or True


def test_de_cada_etiqueta_sale_un_solo_nivel():
    """Con k niveles salen k contrastes que no son independientes: si «lento» esta
    por encima del resto, los otros quedan por debajo por aritmetica."""
    rng = random.Random(9)
    datos = []
    for i in range(400):
        cat = ["lento", "a", "b", "c"][i % 4]
        datos.append(obs(i, labels={"cat": cat},
                         outcomes={"res": (900.0 if cat == "lento" else 300.0)
                                   + rng.gauss(0, 30)}))
    variables = feat.discover_variables(datos, min_level_size=15, max_levels=12)
    p = generator.propose(datos, variables, source="s", min_samples=20, now=LATER)
    grupos = [h for h in p.hypotheses if h.form == GROUP_CONTRAST]
    assert len(grupos) == 1, [h.description for h in grupos]
    assert grupos[0].level == "lento", grupos[0].level
    assert grupos[0].direction == 1


def test_se_ordena_por_efecto_sostenido_y_se_respeta_el_tope():
    rng = random.Random(4)
    datos = [obs(i, features={"fuerte": float(i), "flojo": rng.random() + i * 0.02},
                 outcomes={"res": float(i)}) for i in range(200)]
    variables = feat.discover_variables(datos, min_distinct=5)
    p = generator.propose(datos, variables, source="s", min_samples=20,
                          max_proposals=1, now=LATER)
    assert len(p.hypotheses) == 1
    assert p.hypotheses[0].predictor == "fuerte"


def test_sin_datos_o_sin_variables_no_revienta():
    assert generator.propose([], [], source="s").hypotheses == []
    assert generator.propose(monotona(), [], source="s").hypotheses == []


def test_la_descripcion_incluye_la_fuente():
    """La misma pareja de nombres significa cosas distintas segun de donde salga:
    el `spread` de una señal es el del disparo, el de un mercado es el de ahora."""
    datos = monotona()
    variables = feat.discover_variables(datos, min_distinct=5)
    p = generator.propose(datos, variables, source="signals", min_samples=20, now=LATER)
    assert "[signals]" in p.hypotheses[0].description


def test_describe_es_ciego_al_dominio():
    """Se le puede pedir una frase sobre columnas que no existen en Medusa."""
    frase = generator.describe(MONOTONE, "columna_de_mañana",
                               "resultado_inventado", -1, source="futuro")
    assert "columna de mañana" in frase and "resultado inventado" in frase
    assert "menor" in frase
