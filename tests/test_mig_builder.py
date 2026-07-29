"""Tests del builder: filas de la BD -> nodos y aristas. Puro, sin BD."""

from __future__ import annotations

import datetime as dt

from medusa.intelligence.mig import builder
from medusa.intelligence.mig.builder import build_graph, event_key_from_slug
from medusa.intelligence.mig.types import EdgeType, NodeType

UTC = dt.timezone.utc


def _market(mid, question, slug="", cat="crypto", end=None, score=50.0):
    return {
        "id": mid, "question": question, "slug": slug, "medusa_category": cat,
        "category": cat, "opportunity_score": score, "end_date": end,
        "volume_24h": 1000.0, "liquidity": 2000.0,
    }


def _signal(strategy, mid, cat="crypto", status="open", won=None, roi=0.0,
            edge=0.03, outcome="YES"):
    return {
        "strategy": strategy, "market_id": mid, "category": cat, "status": status,
        "won": won, "roi": roi, "edge": edge, "outcome": outcome,
        "question": f"pregunta {mid}", "entry_price": 0.5,
    }


def _trade(tid, mid, strategy="momentum", won=True, outcome="YES", pnl=1.0):
    return {
        "id": tid, "market_id": mid, "strategy": strategy, "won": won,
        "outcome": outcome, "pnl": pnl, "roi": 0.1, "question": f"pregunta {mid}",
        "mode": "paper",
    }


# ------------------------------------------------------------------ eventos --
def test_serie_deducida_del_slug():
    assert event_key_from_slug("bnb-updown-5m-2026-07-28-1200") == "bnb-updown-5m"
    assert event_key_from_slug("btc-updown-5m-1430") == "btc-updown-5m"


def test_slug_sin_cola_numerica_no_es_serie():
    """Sin cola recortable no hay serie: no se inventa un Event."""
    assert event_key_from_slug("will-the-fed-cut-rates") == ""
    assert event_key_from_slug("") == ""
    assert event_key_from_slug("unico") == ""


def test_event_solo_si_agrupa_dos_mercados_o_mas():
    g = build_graph(markets=[_market("m1", "BNB arriba?", "bnb-updown-5m-1200")])
    assert g.nodes_of(NodeType.EVENT) == []

    g = build_graph(markets=[
        _market("m1", "BNB arriba?", "bnb-updown-5m-1200"),
        _market("m2", "BNB arriba?", "bnb-updown-5m-1205"),
    ])
    assert [n.key for n in g.nodes_of(NodeType.EVENT)] == ["event:bnb-updown-5m"]
    assert g.get_edge("market:m1", "event:bnb-updown-5m", EdgeType.BELONGS_TO)


def test_cadena_temporal_preceded_y_followed():
    t0 = dt.datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
    g = build_graph(markets=[
        _market("m2", "BNB arriba?", "bnb-updown-5m-1205", end=t0 + dt.timedelta(minutes=5)),
        _market("m1", "BNB arriba?", "bnb-updown-5m-1200", end=t0),
    ])
    assert g.get_edge("market:m1", "market:m2", EdgeType.PRECEDED) is not None
    assert g.get_edge("market:m2", "market:m1", EdgeType.FOLLOWED) is not None
    # y no al reves: el sentido es la semantica
    assert g.get_edge("market:m2", "market:m1", EdgeType.PRECEDED) is None


def test_sin_end_date_no_hay_cadena():
    g = build_graph(markets=[
        _market("m1", "BNB arriba?", "bnb-updown-5m-1200"),
        _market("m2", "BNB arriba?", "bnb-updown-5m-1205"),
    ])
    assert g.edges_of(EdgeType.PRECEDED) == []


# ---------------------------------------------------------------- similitud --
def test_similar_to_une_mercados_parecidos_de_la_misma_categoria():
    g = build_graph(markets=[
        _market("m1", "Will Bitcoin close above 100000 dollars in July?"),
        _market("m2", "Will Bitcoin close above 120000 dollars in July?"),
        _market("m3", "Will the Lakers win the championship?", cat="sports"),
    ], min_similarity=0.3)
    assert g.get_edge("market:m1", "market:m2", EdgeType.SIMILAR_TO) is not None
    assert g.get_edge("market:m1", "market:m3", EdgeType.SIMILAR_TO) is None


def test_similitud_no_cruza_categorias():
    g = build_graph(markets=[
        _market("m1", "Will Bitcoin close above 100000 dollars?", cat="crypto"),
        _market("m2", "Will Bitcoin close above 100000 dollars?", cat="macro"),
    ], min_similarity=0.1)
    assert g.edges_of(EdgeType.SIMILAR_TO) == []


def test_max_markets_acota_el_coste_de_la_similitud():
    markets = [_market(f"m{i}", "Will Bitcoin close above 100000 dollars?") for i in range(20)]
    g = build_graph(markets=markets, min_similarity=0.3, max_markets=5)
    involucrados = {e.src for e in g.edges_of(EdgeType.SIMILAR_TO)} | \
                   {e.dst for e in g.edges_of(EdgeType.SIMILAR_TO)}
    assert len(involucrados) <= 5


# ------------------------------------------------------------------ señales --
def test_senal_crea_estrategia_participacion_y_prediccion():
    g = build_graph(markets=[_market("m1", "q")], signals=[_signal("momentum", "m1", edge=0.05)])
    assert g.has_node("strategy:momentum")
    assert g.get_edge("strategy:momentum", "market:m1", EdgeType.PARTICIPATED_IN)
    pred = g.get_edge("strategy:momentum", "market:m1", EdgeType.PREDICTED)
    assert abs(pred.weight - 0.05) < 1e-9


def test_resolucion_del_mercado_no_es_el_lado_de_la_senal():
    """Una señal NO ganadora significa que el mercado resolvio NO."""
    g = build_graph(
        markets=[_market("m1", "q")],
        signals=[_signal("momentum", "m1", status="resolved", won=False, roi=-1.0)],
    )
    assert g.has_node("outcome:m1:NO")
    assert not g.has_node("outcome:m1:YES")
    assert g.get_edge("strategy:momentum", "outcome:m1:NO", EdgeType.LOST) is not None


def test_senal_ganadora_en_lado_no_resuelve_el_mercado_a_no():
    g = build_graph(
        markets=[_market("m1", "q")],
        signals=[_signal("momentum", "m1", status="resolved", won=True, roi=0.8, outcome="NO")],
    )
    assert g.has_node("outcome:m1:NO")
    assert g.get_edge("strategy:momentum", "outcome:m1:NO", EdgeType.WON) is not None


def test_senal_abierta_no_crea_outcome():
    g = build_graph(markets=[_market("m1", "q")], signals=[_signal("momentum", "m1")])
    assert g.nodes_of(NodeType.OUTCOME) == []


def test_specialized_in_usa_el_roi_taker_medio_y_cuenta_muestra():
    señales = [
        _signal("momentum", f"m{i}", status="resolved", won=i % 2 == 0, roi=0.1 if i % 2 == 0 else -0.1)
        for i in range(4)
    ]
    g = build_graph(markets=[_market(f"m{i}", "q") for i in range(4)], signals=señales)
    e = g.get_edge("strategy:momentum", "category:crypto", EdgeType.SPECIALIZED_IN)
    assert e.count == 4
    assert abs(e.weight) < 1e-9            # dos +0.1 y dos -0.1
    assert e.meta["win_rate"] == 0.5


def test_mercado_ausente_de_la_muestra_se_crea_desde_la_senal():
    """Una señal vieja de un mercado que ya no esta en `markets` no puede
    perder sus aristas."""
    g = build_graph(markets=[], signals=[_signal("momentum", "viejo")])
    assert g.has_node("market:viejo")
    assert g.get_edge("strategy:momentum", "market:viejo", EdgeType.PARTICIPATED_IN)


# ------------------------------------------------------------------- trades --
def test_trade_crea_nodo_y_cadena_completa():
    g = build_graph(markets=[_market("m1", "q")],
                    trades=[_trade(7, "m1", won=True, outcome="YES")])
    assert g.has_node("trade:7")
    assert g.get_edge("trade:7", "market:m1", EdgeType.BELONGS_TO)
    assert g.get_edge("strategy:momentum", "trade:7", EdgeType.PARTICIPATED_IN)
    assert g.get_edge("trade:7", "outcome:m1:YES", EdgeType.WON)


def test_trade_perdedor_apunta_a_la_resolucion_contraria():
    g = build_graph(markets=[_market("m1", "q")],
                    trades=[_trade(8, "m1", won=False, outcome="YES")])
    assert g.get_edge("trade:8", "outcome:m1:NO", EdgeType.LOST) is not None


# ------------------------------------------------------------ experimentos --
def test_experimento_sin_muestra_existe_pero_no_dictamina():
    g = build_graph(
        markets=[_market("m1", "q")],
        signals=[_signal("momentum", "m1", status="resolved", won=True, roi=0.5)],
        min_samples=30,
    )
    assert g.has_node("experiment:momentum|crypto")
    assert g.edges_of(EdgeType.SUPPORTS) == []
    assert g.edges_of(EdgeType.CONTRADICTS) == []


def test_experimento_con_muestra_y_roi_positivo_apoya_la_estrategia():
    señales = [_signal("momentum", f"m{i}", status="resolved", won=True, roi=0.05)
               for i in range(5)]
    g = build_graph(signals=señales, min_samples=5)
    e = g.get_edge("experiment:momentum|crypto", "strategy:momentum", EdgeType.SUPPORTS)
    assert e is not None and e.meta["n"] == 5


def test_experimento_con_muestra_y_roi_negativo_contradice():
    señales = [_signal("momentum", f"m{i}", status="resolved", won=False, roi=-0.05)
               for i in range(5)]
    g = build_graph(signals=señales, min_samples=5)
    assert g.get_edge("experiment:momentum|crypto", "strategy:momentum",
                      EdgeType.CONTRADICTS) is not None


# ----------------------------------------------------------------- features --
def test_feature_constante_no_afirma_nada():
    features = [{"market_id": f"m{i}", "name": "spread", "module": "micro", "value": 1.0,
                 "ts": "2026-07-28T00:00:00"} for i in range(4)]
    señales = [_signal("s", f"m{i}", status="resolved", won=True, roi=0.1) for i in range(4)]
    g = build_graph(signals=señales, features=features)
    assert g.edges_of(EdgeType.SUPPORTS) == []
    assert g.edges_of(EdgeType.CONTRADICTS) == []


def test_feature_por_encima_de_la_media_apoya_un_yes():
    señales = [
        _signal("s", "m1", status="resolved", won=True, roi=0.1),    # mercado -> YES
        _signal("s", "m2", status="resolved", won=False, roi=-1.0),  # mercado -> NO
        _signal("s", "m3", status="resolved", won=True, roi=0.1),
    ]
    features = [
        {"market_id": "m1", "name": "vol", "module": "micro", "value": 10.0, "ts": "t1"},
        {"market_id": "m2", "name": "vol", "module": "micro", "value": 1.0, "ts": "t1"},
        {"market_id": "m3", "name": "vol", "module": "micro", "value": 9.0, "ts": "t1"},
    ]
    g = build_graph(signals=señales, features=features)
    # m1 esta por encima de la media y resolvio YES -> apoyo
    assert g.get_edge("feature:micro::vol", "outcome:m1:YES", EdgeType.SUPPORTS) is not None
    # m2 esta por debajo y resolvio NO -> tambien apoyo (la desviacion concuerda)
    assert g.get_edge("feature:micro::vol", "outcome:m2:NO", EdgeType.SUPPORTS) is not None


def test_feature_usa_la_ultima_lectura_por_mercado():
    señales = [_signal("s", "m1", status="resolved", won=True, roi=0.1),
               _signal("s", "m2", status="resolved", won=False, roi=-1.0)]
    features = [
        {"market_id": "m1", "name": "vol", "module": "micro", "value": 0.0, "ts": "2026-01-01"},
        {"market_id": "m1", "name": "vol", "module": "micro", "value": 10.0, "ts": "2026-07-01"},
        {"market_id": "m2", "name": "vol", "module": "micro", "value": 1.0, "ts": "2026-07-01"},
    ]
    g = build_graph(signals=señales, features=features)
    e = g.get_edge("feature:micro::vol", "outcome:m1:YES", EdgeType.SUPPORTS)
    assert e is not None and e.meta["value"] == 10.0


# ----------------------------------------------------------------- wallets --
def test_wallets_sin_fuente_no_crean_nodos():
    """En V1 no hay ingesta de wallets: la fuente llega vacia y NO se inventa nada."""
    g = build_graph(markets=[_market("m1", "q")])
    assert g.nodes_of(NodeType.WALLET) == []


def test_wallet_con_fuente_externa_se_conecta():
    g = build_graph(
        markets=[_market("m1", "q")],
        wallets=[{"address": "0xabc", "markets": ["m1"], "categories": {"crypto": 4},
                  "roi": 0.2, "win_rate": 0.6}],
    )
    assert g.has_node("wallet:0xabc")
    assert g.get_edge("wallet:0xabc", "market:m1", EdgeType.PARTICIPATED_IN)
    e = g.get_edge("wallet:0xabc", "category:crypto", EdgeType.SPECIALIZED_IN)
    assert e.weight == 4.0


# ------------------------------------------------------------- robustez ----
def test_grafo_vacio_es_valido():
    g = build_graph()
    assert g.stats()["nodes"] == 0 and g.stats()["edges"] == 0


def test_filas_basura_no_rompen_la_construccion():
    g = build_graph(
        markets=[{"id": "", "question": None}, {"question": "sin id"}],
        signals=[{"strategy": "", "market_id": "m1"}, {}],
        trades=[{"market_id": "m1"}, {"id": 1}],
        features=[{"name": "", "market_id": ""}],
    )
    assert g.stats()["nodes"] == 0


def test_valores_none_en_columnas_nullable_no_explotan():
    """`spread`/`liquidity`/`roi` son nullable en la BD real."""
    g = build_graph(
        markets=[_market("m1", "q", score=None)],
        signals=[{**_signal("s", "m1", status="resolved", won=True), "roi": None,
                  "edge": None}],
    )
    e = g.get_edge("strategy:s", "market:m1", EdgeType.PREDICTED)
    assert e.weight == 0.0


def test_reconstruir_dos_veces_da_el_mismo_grafo():
    """Idempotencia: el grafo describe relaciones, no cuenta ejecuciones."""
    fuentes = dict(
        markets=[_market("m1", "Will Bitcoin close above 100000?"),
                 _market("m2", "Will Bitcoin close above 120000?")],
        signals=[_signal("momentum", "m1", status="resolved", won=True, roi=0.1)],
        trades=[_trade(1, "m1")],
    )
    a, b = build_graph(**fuentes), build_graph(**fuentes)
    assert a.stats() == b.stats()


def test_construccion_no_muta_las_filas_de_entrada():
    markets = [_market("m1", "q")]
    copia = [dict(m) for m in markets]
    build_graph(markets=markets)
    assert markets == copia


def test_tokens_ignoran_palabras_vacias():
    assert "will" not in builder._tokens("Will the Fed cut rates")
    assert "fed" in builder._tokens("Will the Fed cut rates")
