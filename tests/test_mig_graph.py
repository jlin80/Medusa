"""Tests de la estructura del grafo en memoria (sin BD, sin red)."""

from __future__ import annotations

from medusa.intelligence.mig.graph import IntelligenceGraph
from medusa.intelligence.mig.types import (
    EdgeType,
    GraphEdge,
    GraphNode,
    NodeType,
    node_key,
)


def _g() -> IntelligenceGraph:
    g = IntelligenceGraph()
    g.node(NodeType.MARKET, "m1", label="Mercado 1")
    g.node(NodeType.MARKET, "m2", label="Mercado 2")
    g.node(NodeType.CATEGORY, "crypto", label="crypto")
    return g


def test_node_key_es_estable_y_tipado():
    assert node_key(NodeType.MARKET, "abc") == "market:abc"
    assert node_key("market", "abc") == "market:abc"


def test_anadir_el_mismo_nodo_dos_veces_no_duplica():
    g = _g()
    antes = len(g.nodes)
    g.node(NodeType.MARKET, "m1", label="Mercado 1 (actualizado)")
    assert len(g.nodes) == antes
    assert g.get_node("market:m1").label == "Mercado 1 (actualizado)"


def test_una_etiqueta_vacia_no_pisa_una_buena():
    g = _g()
    g.add_node(GraphNode(key="market:m1", node_type=NodeType.MARKET, label=""))
    assert g.get_node("market:m1").label == "Mercado 1"


def test_arista_colgante_se_descarta_y_no_inventa_nodos():
    g = _g()
    assert g.edge("market:m1", "market:NO_EXISTE", EdgeType.SIMILAR_TO) is None
    assert not g.has_node("market:NO_EXISTE")
    assert len(g.edges) == 0


def test_bucle_sobre_si_mismo_se_descarta():
    g = _g()
    assert g.edge("market:m1", "market:m1", EdgeType.SIMILAR_TO) is None
    assert len(g.edges) == 0


def test_arista_simetrica_se_normaliza():
    """similar_to(a,b) y similar_to(b,a) son la MISMA relacion."""
    g = _g()
    g.edge("market:m2", "market:m1", EdgeType.SIMILAR_TO, weight=0.5)
    g.edge("market:m1", "market:m2", EdgeType.SIMILAR_TO, weight=0.5)
    assert len(g.edges) == 1
    assert g.get_edge("market:m1", "market:m2", EdgeType.SIMILAR_TO) is not None
    assert g.get_edge("market:m2", "market:m1", EdgeType.SIMILAR_TO) is not None


def test_arista_dirigida_no_se_normaliza():
    """belongs_to(a,b) NO es belongs_to(b,a): el sentido es la semantica."""
    g = _g()
    g.edge("market:m1", "category:crypto", EdgeType.BELONGS_TO)
    assert g.get_edge("category:crypto", "market:m1", EdgeType.BELONGS_TO) is None


def test_dos_tipos_de_arista_entre_el_mismo_par_conviven():
    g = _g()
    g.node(NodeType.STRATEGY, "momentum")
    g.edge("strategy:momentum", "market:m1", EdgeType.PARTICIPATED_IN)
    g.edge("strategy:momentum", "market:m1", EdgeType.PREDICTED, weight=0.04)
    assert len(g.edges) == 2


def test_fusion_de_aristas_promedia_por_observaciones():
    """Peso fusionado = media ponderada por count; count = suma."""
    g = _g()
    g.add_edge(GraphEdge("market:m1", "category:crypto", EdgeType.BELONGS_TO,
                         weight=1.0, count=3))
    g.add_edge(GraphEdge("market:m1", "category:crypto", EdgeType.BELONGS_TO,
                         weight=0.0, count=1))
    e = g.get_edge("market:m1", "category:crypto", EdgeType.BELONGS_TO)
    assert e.count == 4
    assert abs(e.weight - 0.75) < 1e-9      # (1.0*3 + 0.0*1) / 4


def test_estadisticas_y_grado():
    g = _g()
    g.edge("market:m1", "category:crypto", EdgeType.BELONGS_TO)
    g.edge("market:m2", "category:crypto", EdgeType.BELONGS_TO)
    st = g.stats()
    assert st["nodes"] == 3 and st["edges"] == 2
    assert st["node_counts"] == {"category": 1, "market": 2}
    assert st["edge_counts"] == {"belongs_to": 2}
    assert st["isolated_nodes"] == 0
    assert g.degree("category:crypto") == 2
    assert g.top_nodes(limit=1)[0]["key"] == "category:crypto"


def test_estadisticas_de_grafo_vacio_no_dividen_por_cero():
    st = IntelligenceGraph().stats()
    assert st == {
        "nodes": 0, "edges": 0, "node_counts": {}, "edge_counts": {},
        "density": 0.0, "avg_degree": 0.0, "max_degree": 0, "isolated_nodes": 0,
    }


def test_vecinos_en_ambos_sentidos():
    g = _g()
    g.edge("market:m1", "category:crypto", EdgeType.BELONGS_TO)
    salientes = g.neighbors("market:m1")
    entrantes = g.neighbors("category:crypto", incoming=True)
    assert [k for k, _ in salientes] == ["category:crypto"]
    assert [k for k, _ in entrantes] == ["market:m1"]
    assert g.neighbors("market:m1", edge_type=EdgeType.SIMILAR_TO) == []
