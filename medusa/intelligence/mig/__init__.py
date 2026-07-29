"""MARKET INTELLIGENCE GRAPH (MIG) — V1, PostgreSQL.

Que es: un grafo de RELACIONES entre las entidades que ya existen dentro de
Medusa (mercados, categorias, series de eventos, estrategias, trades,
resoluciones, features, experimentos y — cuando haya fuente — wallets).

Que NO es, y es la parte importante:

  - NO es un motor de trading. No manda ordenes.
  - NO es una estrategia. No emite señales.
  - NO es un modulo de ejecucion. No conoce el adapter de ejecucion.
  - NO toca el Risk Manager. Ni lo importa.

Su unica responsabilidad es construir el grafo y derivar de el objetos de
inteligencia reutilizables (`Discovery`) que MAS ADELANTE podran convertirse en
Features, por decision explicita de otro modulo y con el contrato de siempre
(`Feature.value` float, lo textual en `meta`).

Aditivo por construccion: cuatro tablas nuevas con prefijo `mig_`, un router
HTTP nuevo, una pagina nueva en el dashboard y un loop opcional en el engine.
Apagado por defecto (`MIG_ENABLED=false`). Nada del sistema existente cambia de
comportamiento tanto si esta encendido como si no.

Por que Postgres y no Neo4j: ver la cabecera de `models.py`.

Mapa del paquete:

    types.py        vocabulario (NodeType, EdgeType, GraphNode, GraphEdge, Discovery)
    graph.py        grafo en memoria, puro, sin I/O
    builder.py      filas de la BD -> nodos y aristas (funciones puras)
    discoveries.py  grafo -> objetos de inteligencia reutilizables (puro)
    models.py       tablas ORM (mig_nodes, mig_edges, mig_discoveries, mig_snapshots)
    migrations.py   DDL idempotente (indices y unicidad)
    repository.py   persistencia y consultas
    service.py      orquestacion (leer -> construir -> persistir)
    api.py          router HTTP /mig/*
"""

from medusa.intelligence.mig.builder import build_graph, build_from_sources
from medusa.intelligence.mig.discoveries import extract_discoveries
from medusa.intelligence.mig.graph import IntelligenceGraph
from medusa.intelligence.mig.migrations import MIG_MIGRATIONS
from medusa.intelligence.mig.service import MIGService
from medusa.intelligence.mig.types import (
    Discovery,
    EdgeType,
    GraphEdge,
    GraphNode,
    NodeType,
    node_key,
)

__all__ = [
    "Discovery",
    "EdgeType",
    "GraphEdge",
    "GraphNode",
    "IntelligenceGraph",
    "MIGService",
    "MIG_MIGRATIONS",
    "NodeType",
    "build_from_sources",
    "build_graph",
    "extract_discoveries",
    "node_key",
]
