"""Estructura del grafo EN MEMORIA (aritmetica pura de Python, sin I/O).

Deliberadamente sin dependencias: ni networkx ni numpy (la CPU del CT202 no
admite numpy 2.x, y para lo que hace falta aqui una libreria de grafos es peso
muerto). Es tambien lo que hace este modulo testeable al 100% sin Postgres, sin
Redis y sin red.

El grafo es acumulativo y idempotente: añadir dos veces el mismo nodo o la misma
arista NO crea duplicados, fusiona (suma `count`, promedia `weight` ponderado
por observaciones). Esa idempotencia es un requisito, no un detalle: el builder
corre en bucle sobre datos que se solapan y, sin fusion, el grafo crecería por
repeticion y las estadisticas de crecimiento serian mentira.
"""

from __future__ import annotations

from collections import defaultdict

from medusa.intelligence.mig.types import (
    SYMMETRIC_EDGES,
    EdgeType,
    GraphEdge,
    GraphNode,
    NodeType,
)


class IntelligenceGraph:
    """Grafo dirigido con multiples tipos de arista entre el mismo par de nodos.

    Clave de arista: (src, dst, tipo). El mismo par puede estar unido por
    `participated_in` y por `won` a la vez: son hechos distintos.
    """

    def __init__(self) -> None:
        self._nodes: dict[str, GraphNode] = {}
        self._edges: dict[tuple[str, str, str], GraphEdge] = {}

    # ------------------------------------------------------------- escritura --
    def add_node(self, node: GraphNode) -> GraphNode:
        """Añade o fusiona un nodo. Devuelve el nodo VIVO del grafo."""
        existing = self._nodes.get(node.key)
        if existing is None:
            self._nodes[node.key] = node
            return node
        # Fusion conservadora: lo nuevo completa, nunca borra. Una etiqueta
        # vacia no debe pisar una etiqueta buena de una pasada anterior.
        if node.label:
            existing.label = node.label
        if node.weight:
            existing.weight = node.weight
        existing.meta.update(node.meta)
        if node.ts:
            existing.ts = node.ts
        return existing

    def node(
        self, node_type: NodeType, external_id: str, label: str = "",
        weight: float = 0.0, **meta,
    ) -> GraphNode:
        """Atajo: construye la clave y añade el nodo."""
        from medusa.intelligence.mig.types import node_key

        return self.add_node(GraphNode(
            key=node_key(node_type, external_id), node_type=node_type,
            label=label, weight=weight, meta=meta or {},
        ))

    def add_edge(self, edge: GraphEdge) -> GraphEdge | None:
        """Añade o fusiona una arista.

        Devuelve None si alguno de los extremos no existe: una arista colgante
        es un error de construccion, no un dato. Se ignora en silencio a
        proposito (el builder no puede tumbar el proceso por un dato parcial),
        pero jamas se inventa el nodo que falta.
        """
        src, dst = edge.src, edge.dst
        if src == dst:
            return None                       # un bucle no aporta relacion
        if src not in self._nodes or dst not in self._nodes:
            return None
        if edge.edge_type in SYMMETRIC_EDGES and src > dst:
            src, dst = dst, src               # orden canonico: A~B == B~A

        key = (src, dst, edge.edge_type.value)
        existing = self._edges.get(key)
        if existing is None:
            merged = GraphEdge(
                src=src, dst=dst, edge_type=edge.edge_type,
                weight=float(edge.weight), count=int(edge.count),
                meta=dict(edge.meta), ts=edge.ts,
            )
            self._edges[key] = merged
            return merged

        # Media ponderada por observaciones: es la unica fusion que mantiene el
        # peso interpretable cuando la misma relacion se observa muchas veces.
        total = existing.count + edge.count
        if total > 0:
            existing.weight = (
                existing.weight * existing.count + edge.weight * edge.count
            ) / total
        existing.count = total
        existing.meta.update(edge.meta)
        if edge.ts:
            existing.ts = edge.ts
        return existing

    def edge(
        self, src: str, dst: str, edge_type: EdgeType, weight: float = 0.0,
        count: int = 1, **meta,
    ) -> GraphEdge | None:
        return self.add_edge(GraphEdge(
            src=src, dst=dst, edge_type=edge_type, weight=weight,
            count=count, meta=meta or {},
        ))

    # -------------------------------------------------------------- lectura --
    @property
    def nodes(self) -> list[GraphNode]:
        return list(self._nodes.values())

    @property
    def edges(self) -> list[GraphEdge]:
        return list(self._edges.values())

    def has_node(self, key: str) -> bool:
        return key in self._nodes

    def get_node(self, key: str) -> GraphNode | None:
        return self._nodes.get(key)

    def get_edge(self, src: str, dst: str, edge_type: EdgeType) -> GraphEdge | None:
        if edge_type in SYMMETRIC_EDGES and src > dst:
            src, dst = dst, src
        return self._edges.get((src, dst, edge_type.value))

    def nodes_of(self, node_type: NodeType) -> list[GraphNode]:
        return [n for n in self._nodes.values() if n.node_type == node_type]

    def edges_of(self, edge_type: EdgeType) -> list[GraphEdge]:
        return [e for e in self._edges.values() if e.edge_type == edge_type]

    def neighbors(
        self, key: str, edge_type: EdgeType | None = None, incoming: bool = False,
    ) -> list[tuple[str, GraphEdge]]:
        """Vecinos de un nodo. Con `incoming` se recorre el grafo al reves."""
        out: list[tuple[str, GraphEdge]] = []
        for e in self._edges.values():
            if edge_type is not None and e.edge_type != edge_type:
                continue
            if not incoming and e.src == key:
                out.append((e.dst, e))
            elif incoming and e.dst == key:
                out.append((e.src, e))
        return out

    def degree(self, key: str) -> int:
        return sum(1 for e in self._edges.values() if e.src == key or e.dst == key)

    # --------------------------------------------------------- estadisticas --
    def node_counts(self) -> dict[str, int]:
        counts: dict[str, int] = defaultdict(int)
        for n in self._nodes.values():
            counts[n.node_type.value] += 1
        return dict(sorted(counts.items()))

    def edge_counts(self) -> dict[str, int]:
        counts: dict[str, int] = defaultdict(int)
        for e in self._edges.values():
            counts[e.edge_type.value] += 1
        return dict(sorted(counts.items()))

    def stats(self) -> dict:
        """Foto del grafo. Es lo que consume la pagina Research Lab."""
        n, e = len(self._nodes), len(self._edges)
        # Densidad de un dirigido simple: aristas / (n*(n-1)). Con n<2 no esta
        # definida y se reporta 0.0 en vez de dividir por cero.
        density = (e / (n * (n - 1))) if n > 1 else 0.0
        degrees = [self.degree(k) for k in self._nodes]
        return {
            "nodes": n,
            "edges": e,
            "node_counts": self.node_counts(),
            "edge_counts": self.edge_counts(),
            "density": round(density, 6),
            "avg_degree": round(sum(degrees) / n, 3) if n else 0.0,
            "max_degree": max(degrees) if degrees else 0,
            "isolated_nodes": sum(1 for d in degrees if d == 0),
        }

    def top_nodes(self, limit: int = 10, node_type: NodeType | None = None) -> list[dict]:
        """Nodos mas conectados: el "quien manda" del grafo, por grado."""
        pool = self.nodes_of(node_type) if node_type else self.nodes
        ranked = sorted(pool, key=lambda n: (self.degree(n.key), n.key), reverse=True)
        return [
            {**n.to_dict(), "degree": self.degree(n.key)} for n in ranked[:limit]
        ]
