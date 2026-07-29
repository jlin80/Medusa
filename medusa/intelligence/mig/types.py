"""Vocabulario del MARKET INTELLIGENCE GRAPH (MIG).

Aqui viven los tipos: que puede ser un nodo, que puede ser una arista y como se
representan en memoria antes de tocar Postgres. Es el unico sitio donde se
define el vocabulario del grafo; ni el builder ni el repositorio inventan tipos
nuevos por su cuenta.

REGLA DEL PAQUETE (no negociable, igual que en el Intelligence Layer V3):
el MIG describe RELACIONES, jamas decisiones. No opera, no toca posiciones, no
conoce al Risk Manager. Un nodo y una arista no tienen forma de mandar una
orden: lo maximo que produce este paquete es un objeto de conocimiento
(`Discovery`) que ALGUN DIA podra convertirse en una Feature -- y esa conversion
la hara otro modulo, con el contrato de siempre (`Feature.value` float).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from enum import Enum


class NodeType(str, Enum):
    """Entidades que el grafo sabe representar."""

    WALLET = "wallet"
    MARKET = "market"
    CATEGORY = "category"
    EVENT = "event"
    FEATURE = "feature"
    STRATEGY = "strategy"
    TRADE = "trade"
    OUTCOME = "outcome"
    EXPERIMENT = "experiment"


class EdgeType(str, Enum):
    """Relaciones que el grafo sabe representar.

    Cada una tiene una semantica FIJA (ver docs/MARKET_INTELLIGENCE_GRAPH.md).
    Una arista sin semantica clara es ruido: envenena cualquier analisis que
    salga del grafo, exactamente igual que una feature inventada envenena el
    Feature Store.
    """

    PARTICIPATED_IN = "participated_in"
    SPECIALIZED_IN = "specialized_in"
    WON = "won"
    LOST = "lost"
    PREDICTED = "predicted"
    FOLLOWED = "followed"
    PRECEDED = "preceded"
    SIMILAR_TO = "similar_to"
    SUPPORTS = "supports"
    CONTRADICTS = "contradicts"
    BELONGS_TO = "belongs_to"


# Aristas simetricas: A~B es lo mismo que B~A. El grafo las normaliza (ordena
# los extremos) para que "similar_to(a,b)" y "similar_to(b,a)" no cuenten dos
# veces la misma relacion e inflen las estadisticas.
SYMMETRIC_EDGES: frozenset[EdgeType] = frozenset({EdgeType.SIMILAR_TO})

# Pares inversos: si se registra uno, el otro describe la misma realidad leida
# al reves. Se guardan los dos a proposito (una consulta "que vino ANTES de X"
# no deberia tener que invertir la arista a mano en SQL).
INVERSE_EDGES: dict[EdgeType, EdgeType] = {
    EdgeType.PRECEDED: EdgeType.FOLLOWED,
    EdgeType.FOLLOWED: EdgeType.PRECEDED,
}


def node_key(node_type: NodeType | str, external_id: str) -> str:
    """Clave estable de un nodo: "<tipo>:<id externo>".

    Es la PK en Postgres. Estable a proposito: el mismo mercado visto en dos
    construcciones del grafo tiene que ser EL MISMO nodo, o el grafo crece por
    duplicados y las estadisticas de crecimiento mienten.
    """
    t = node_type.value if isinstance(node_type, NodeType) else str(node_type)
    return f"{t}:{external_id}"


@dataclass
class GraphNode:
    """Un nodo en memoria. `weight` es el unico numero operable; el resto es
    contexto para auditar (mismo principio que Feature.value vs Feature.meta)."""

    key: str
    node_type: NodeType
    label: str = ""
    weight: float = 0.0
    meta: dict = field(default_factory=dict)
    ts: dt.datetime | None = None

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "node_type": self.node_type.value,
            "label": self.label,
            "weight": round(float(self.weight), 6),
            "meta": dict(self.meta),
        }


@dataclass
class GraphEdge:
    """Una arista en memoria.

    `count` es el numero de observaciones que la sostienen. Una arista con
    count=1 y otra con count=400 no valen lo mismo, y cualquier consumidor del
    grafo tiene que poder distinguirlas sin ir a buscar los datos crudos.
    """

    src: str
    dst: str
    edge_type: EdgeType
    weight: float = 0.0
    count: int = 1
    meta: dict = field(default_factory=dict)
    ts: dt.datetime | None = None

    def to_dict(self) -> dict:
        return {
            "src": self.src,
            "dst": self.dst,
            "edge_type": self.edge_type.value,
            "weight": round(float(self.weight), 6),
            "count": int(self.count),
            "meta": dict(self.meta),
        }


@dataclass
class Discovery:
    """Objeto de inteligencia REUTILIZABLE derivado del grafo.

    Es el producto final del MIG y el puente hacia el futuro: un Discovery
    describe un patron con su evidencia y propone el NOMBRE de la feature en la
    que podria convertirse (`feature_name`). NO crea la feature, no la escribe
    en el Feature Store y no llega al camino de decision. Convertirlo en Feature
    es una decision explicita de otro modulo, mas adelante, con el contrato de
    siempre.

    `score` en [0, 1]: cuanta confianza merece el patron segun su evidencia
    (tamaño de muestra y magnitud del efecto). Un score alto NO significa
    "operable": significa "el patron esta bien soportado por los datos".
    """

    kind: str
    subject: str
    statement: str
    score: float = 0.0
    value: float = 0.0
    evidence: dict = field(default_factory=dict)
    feature_name: str = ""
    ts: dt.datetime | None = None

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "subject": self.subject,
            "statement": self.statement,
            "score": round(float(self.score), 4),
            "value": round(float(self.value), 6),
            "evidence": dict(self.evidence),
            "feature_name": self.feature_name,
        }

    def to_feature_spec(self) -> dict:
        """Plantilla de la Feature en la que ESTE descubrimiento podria
        convertirse. Devuelve un dict, no una Feature: el MIG no importa el
        Intelligence Layer ni escribe en el Feature Store."""
        return {
            "name": self.feature_name or f"mig_{self.kind}",
            "value": float(self.value),
            "module": "mig",
            "meta": {"kind": self.kind, "subject": self.subject,
                     "score": round(float(self.score), 4), **self.evidence},
        }
