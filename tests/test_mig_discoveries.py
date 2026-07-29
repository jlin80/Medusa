"""Tests de los descubrimientos (objetos de inteligencia reutilizables)."""

from __future__ import annotations

from medusa.intelligence.mig.builder import build_graph
from medusa.intelligence.mig.discoveries import (
    event_series,
    experiment_verdicts,
    extract_discoveries,
    feature_associations,
    market_clusters,
    strategy_specializations,
)
from medusa.intelligence.mig.graph import IntelligenceGraph
from medusa.intelligence.mig.types import EdgeType, NodeType


def _signal(strategy, mid, cat="crypto", status="resolved", won=True, roi=0.05):
    return {"strategy": strategy, "market_id": mid, "category": cat, "status": status,
            "won": won, "roi": roi, "edge": 0.03, "outcome": "YES",
            "question": f"pregunta {mid}"}


def _market(mid, question, slug="", cat="crypto"):
    return {"id": mid, "question": question, "slug": slug, "medusa_category": cat,
            "opportunity_score": 10.0, "end_date": None}


def test_especializacion_exige_muestra():
    pocas = [_signal("momentum", f"m{i}") for i in range(3)]
    g = build_graph(signals=pocas, min_samples=30)
    assert strategy_specializations(g, min_samples=30) == []


def test_especializacion_con_muestra_reporta_roi_y_muestra():
    señales = [_signal("momentum", f"m{i}", roi=0.05) for i in range(10)]
    g = build_graph(signals=señales, min_samples=5)
    ds = strategy_specializations(g, min_samples=5)
    assert len(ds) == 1
    d = ds[0]
    assert d.kind == "strategy_specialization"
    assert d.evidence["n"] == 10 and abs(d.value - 0.05) < 1e-9
    assert 0.0 <= d.score <= 1.0


def test_se_reporta_tambien_lo_que_pierde():
    """Un sistema honesto no esconde donde una estrategia pierde dinero."""
    señales = [_signal("momentum", f"m{i}", won=False, roi=-0.08) for i in range(10)]
    g = build_graph(signals=señales, min_samples=5)
    d = strategy_specializations(g, min_samples=5)[0]
    assert d.value < 0 and d.score > 0


def test_veredicto_de_experimento():
    señales = [_signal("momentum", f"m{i}", roi=0.05) for i in range(10)]
    g = build_graph(signals=señales, min_samples=5)
    ds = experiment_verdicts(g, min_samples=5)
    assert ds and ds[0].evidence["verdict"] == "apoya"
    assert ds[0].subject == "momentum|crypto"


def test_veredicto_negativo_dice_contradice():
    señales = [_signal("momentum", f"m{i}", won=False, roi=-0.05) for i in range(10)]
    g = build_graph(signals=señales, min_samples=5)
    assert experiment_verdicts(g, min_samples=5)[0].evidence["verdict"] == "contradice"


def test_asociacion_de_feature_exige_observaciones():
    señales = [_signal("s", "m1"), _signal("s", "m2", won=False, roi=-1.0)]
    features = [{"market_id": "m1", "name": "vol", "module": "micro", "value": 5.0, "ts": "t"},
                {"market_id": "m2", "name": "vol", "module": "micro", "value": 1.0, "ts": "t"}]
    g = build_graph(signals=señales, features=features)
    assert feature_associations(g, min_observations=20) == []


def test_asociacion_de_feature_con_muestra_suficiente():
    señales, features = [], []
    for i in range(30):
        won = i % 2 == 0
        señales.append(_signal("s", f"m{i}", won=won, roi=0.1 if won else -1.0))
        features.append({"market_id": f"m{i}", "name": "vol", "module": "micro",
                         "value": 10.0 if won else 1.0, "ts": "t"})
    g = build_graph(signals=señales, features=features)
    ds = feature_associations(g, min_observations=5)
    assert len(ds) == 1
    d = ds[0]
    # Los valores altos acompañan a YES y los bajos a NO: apoyo unanime.
    assert d.value == 1.0 and d.evidence["contradicts"] == 0
    assert d.subject == "micro::vol"


def test_asociacion_no_se_presenta_como_edge():
    """El enunciado debe decir explicitamente que no esta validada."""
    señales, features = [], []
    for i in range(30):
        won = i % 2 == 0
        señales.append(_signal("s", f"m{i}", won=won, roi=0.1 if won else -1.0))
        features.append({"market_id": f"m{i}", "name": "vol", "module": "micro",
                         "value": 10.0 if won else 1.0, "ts": "t"})
    g = build_graph(signals=señales, features=features)
    assert "sin validar" in feature_associations(g, min_observations=5)[0].statement


def test_clusters_de_mercados():
    markets = [_market(f"m{i}", "Will Bitcoin close above 100000 dollars in July?")
               for i in range(4)]
    g = build_graph(markets=markets, min_similarity=0.3)
    ds = market_clusters(g, min_size=3)
    assert len(ds) == 1 and ds[0].evidence["size"] == 4


def test_cluster_pequeno_no_se_reporta():
    markets = [_market("m1", "Will Bitcoin close above 100000 dollars?"),
               _market("m2", "Will Bitcoin close above 120000 dollars?")]
    g = build_graph(markets=markets, min_similarity=0.3)
    assert market_clusters(g, min_size=3) == []


def test_serie_de_eventos():
    markets = [_market(f"m{i}", "BNB arriba?", slug=f"bnb-updown-5m-12{i:02d}")
               for i in range(4)]
    g = build_graph(markets=markets)
    ds = event_series(g, min_markets=3)
    assert len(ds) == 1 and ds[0].evidence["markets"] == 4


def test_extraccion_completa_ordena_por_score():
    señales = [_signal("momentum", f"m{i}", roi=0.05) for i in range(10)]
    ds = extract_discoveries(build_graph(signals=señales, min_samples=5), min_samples=5)
    assert ds
    assert all(a.score >= b.score for a, b in zip(ds, ds[1:]))


def test_grafo_vacio_no_descubre_nada():
    assert extract_discoveries(IntelligenceGraph()) == []


def test_specialized_in_de_una_wallet_no_es_especializacion_de_estrategia():
    """`specialized_in` lo emiten wallets y estrategias: no se pueden mezclar."""
    g = build_graph(
        markets=[_market("m1", "q")],
        wallets=[{"address": "0xabc", "markets": ["m1"], "categories": {"crypto": 99}}],
    )
    assert g.get_edge("wallet:0xabc", "category:crypto", EdgeType.SPECIALIZED_IN)
    assert strategy_specializations(g, min_samples=5) == []


def test_discovery_propone_feature_pero_no_la_crea():
    """El puente al Feature Store es una PLANTILLA, no una escritura."""
    señales = [_signal("momentum", f"m{i}", roi=0.05) for i in range(10)]
    g = build_graph(signals=señales, min_samples=5)
    spec = strategy_specializations(g, min_samples=5)[0].to_feature_spec()
    assert set(spec) == {"name", "value", "module", "meta"}
    assert spec["module"] == "mig"
    assert isinstance(spec["value"], float)


def test_todos_los_tipos_del_vocabulario_son_construibles():
    """Los 9 tipos de nodo y las 11 relaciones tienen que poder existir; si no,
    el vocabulario declara mas de lo que el grafo sabe hacer."""
    markets = [
        _market("m1", "Will Bitcoin close above 100000 dollars in July?",
                slug="btc-updown-5m-1200"),
        _market("m2", "Will Bitcoin close above 120000 dollars in July?",
                slug="btc-updown-5m-1205"),
        _market("m3", "Will Bitcoin close above 130000 dollars in July?",
                slug="btc-updown-5m-1210"),
    ]
    for m, end in zip(markets, ("2026-07-28T12:00:00+00:00", "2026-07-28T12:05:00+00:00",
                                "2026-07-28T12:10:00+00:00")):
        m["end_date"] = end
    señales = [_signal("momentum", f"m{(i % 3) + 1}", won=i % 2 == 0,
                       roi=0.05 if i % 2 == 0 else -0.05) for i in range(12)]
    features = [{"market_id": f"m{i}", "name": "vol", "module": "micro",
                 "value": float(i * 5), "ts": "t"} for i in (1, 2, 3)]
    trades = [{"id": 1, "market_id": "m1", "strategy": "momentum", "won": True,
               "outcome": "YES", "pnl": 2.0, "roi": 0.1, "question": "q", "mode": "paper"}]
    wallets = [{"address": "0xabc", "markets": ["m1"], "categories": {"crypto": 3}}]

    g = build_graph(markets=markets, signals=señales, trades=trades, features=features,
                    wallets=wallets, min_samples=5, min_similarity=0.3)

    tipos_nodo = set(g.node_counts())
    assert tipos_nodo == {t.value for t in NodeType}, sorted({t.value for t in NodeType} - tipos_nodo)
    tipos_arista = set(g.edge_counts())
    assert tipos_arista == {t.value for t in EdgeType}, sorted({t.value for t in EdgeType} - tipos_arista)
