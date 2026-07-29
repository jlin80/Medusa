"""Descubrimientos: del grafo a OBJETOS DE INTELIGENCIA REUTILIZABLES.

Un `Discovery` es un patron leido del grafo, con su evidencia, su tamaño de
muestra y el nombre de la feature en la que PODRIA convertirse mas adelante.

Lo que un Discovery NO es, y conviene tenerlo escrito:

  - NO es una señal. No tiene lado, ni precio, ni tamaño.
  - NO es una feature. No se escribe en el Feature Store ni la ve una estrategia.
  - NO es un edge demostrado. Nada de esto ha pasado un contraste out-of-sample,
    y el proyecto ya midio lo que cuesta esa confusion (~2.2% de peaje por ida y
    vuelta, y cuatro vias descartadas por los datos).

`score` en [0,1] combina DOS cosas y ninguna es rentabilidad: cuanta muestra
sostiene el patron y cuanta magnitud tiene el efecto. Un score de 0.9 dice "el
patron esta bien soportado", jamas "esto gana dinero".
"""

from __future__ import annotations

from collections import defaultdict

from medusa.intelligence.mig.graph import IntelligenceGraph
from medusa.intelligence.mig.types import Discovery, EdgeType, NodeType


def _sample_score(n: int, min_samples: int) -> float:
    """Saturacion suave por muestra: n/(n+min_samples).

    Con n = min_samples da 0.5, y crece hacia 1 sin llegar nunca. Un escalon
    duro en el umbral haria que 29 y 31 muestras dieran veredictos opuestos.
    """
    if n <= 0 or min_samples <= 0:
        return 0.0
    return n / float(n + min_samples)


def _magnitude_score(value: float, scale: float) -> float:
    if scale <= 0:
        return 0.0
    return min(1.0, abs(value) / scale)


def strategy_specializations(
    g: IntelligenceGraph, min_samples: int = 30,
) -> list[Discovery]:
    """"Esta estrategia se comporta distinto en esta categoria".

    Sale de `specialized_in`, cuyo peso es el ROI TAKER medio (la cota
    pesimista). Se emite tambien cuando el ROI es negativo: saber donde una
    estrategia pierde es exactamente igual de util que saber donde gana, y es
    justo lo que un sistema honesto no puede permitirse esconder.
    """
    out: list[Discovery] = []
    for e in g.edges_of(EdgeType.SPECIALIZED_IN):
        src = g.get_node(e.src)
        if src is None or src.node_type != NodeType.STRATEGY:
            continue   # `specialized_in` tambien lo emiten las wallets
        n = int(e.count)
        if n < min_samples:
            continue
        strat = e.src.split(":", 1)[1]
        cat = e.dst.split(":", 1)[1]
        roi = float(e.weight)
        score = _sample_score(n, min_samples) * _magnitude_score(roi, 0.10)
        out.append(Discovery(
            kind="strategy_specialization",
            subject=f"{strat}@{cat}",
            statement=(
                f"'{strat}' acumula {n} señales resueltas en '{cat}' con ROI taker "
                f"medio {roi:+.2%} (win rate {e.meta.get('win_rate', 0):.0%})."
            ),
            score=round(score, 4), value=roi,
            evidence={"strategy": strat, "category": cat, "n": n,
                      "avg_roi": round(roi, 6),
                      "win_rate": e.meta.get("win_rate", 0.0)},
            feature_name=f"mig_spec_{strat}_{cat}",
        ))
    return sorted(out, key=lambda d: d.score, reverse=True)


def experiment_verdicts(g: IntelligenceGraph, min_samples: int = 30) -> list[Discovery]:
    """Veredicto de una celda de hipotesis (estrategia x categoria).

    Solo aparecen las celdas que YA tienen muestra: las aristas
    `supports`/`contradicts` de un Experiment solo se crean por encima del
    umbral (ver builder.add_experiments).
    """
    out: list[Discovery] = []
    for edge_type, verdict in ((EdgeType.SUPPORTS, "apoya"), (EdgeType.CONTRADICTS, "contradice")):
        for e in g.edges_of(edge_type):
            src = g.get_node(e.src)
            if src is None or src.node_type != NodeType.EXPERIMENT:
                continue
            n = int(e.meta.get("n") or e.count)
            avg_roi = float(e.meta.get("avg_roi") or 0.0)
            score = _sample_score(n, min_samples) * _magnitude_score(avg_roi, 0.10)
            out.append(Discovery(
                kind="experiment_verdict",
                subject=src.key.split(":", 1)[1],
                statement=(
                    f"La celda '{src.label}' {verdict} a la estrategia: n={n}, "
                    f"ROI taker medio {avg_roi:+.2%}."
                ),
                score=round(score, 4), value=avg_roi,
                evidence={"n": n, "avg_roi": round(avg_roi, 6), "verdict": verdict,
                          **{k: v for k, v in src.meta.items() if k in ("strategy", "category", "wins")}},
                feature_name=f"mig_experiment_{src.key.split(':', 1)[1].replace('|', '_')}",
            ))
    return sorted(out, key=lambda d: d.score, reverse=True)


def feature_associations(
    g: IntelligenceGraph, min_observations: int = 20,
) -> list[Discovery]:
    """Cuanto se inclina una feature hacia un lado en los mercados resueltos.

    value = (apoyos - contradicciones) / total, en [-1, 1]. Es una asociacion
    cruda sobre la muestra que hay en el grafo: NO es predictividad, no esta
    validada fuera de muestra y no controla por categoria ni por precio. Sirve
    para decidir QUE merece un estudio, no para operar.
    """
    tally: dict[str, dict[str, float]] = defaultdict(lambda: {"sup": 0, "con": 0, "z": 0.0})
    for edge_type, bucket in ((EdgeType.SUPPORTS, "sup"), (EdgeType.CONTRADICTS, "con")):
        for e in g.edges_of(edge_type):
            src = g.get_node(e.src)
            if src is None or src.node_type != NodeType.FEATURE:
                continue
            tally[e.src][bucket] += 1
            tally[e.src]["z"] += abs(float(e.weight))

    out: list[Discovery] = []
    for fkey, t in tally.items():
        total = int(t["sup"] + t["con"])
        if total < min_observations:
            continue
        lean = (t["sup"] - t["con"]) / float(total)
        node = g.get_node(fkey)
        name = fkey.split(":", 1)[1]
        score = _sample_score(total, min_observations) * _magnitude_score(lean, 0.5)
        out.append(Discovery(
            kind="feature_association",
            subject=name,
            statement=(
                f"La feature '{name}' se inclina {lean:+.0%} hacia "
                f"{'YES' if lean >= 0 else 'NO'} sobre {total} resoluciones "
                f"observadas (asociacion cruda, sin validar fuera de muestra)."
            ),
            score=round(score, 4), value=round(lean, 6),
            evidence={"feature": name, "supports": int(t["sup"]),
                      "contradicts": int(t["con"]), "observations": total,
                      "avg_abs_z": round(t["z"] / total, 4),
                      "module": (node.meta.get("module") if node else "")},
            feature_name=f"mig_assoc_{name}",
        ))
    return sorted(out, key=lambda d: d.score, reverse=True)


def market_clusters(g: IntelligenceGraph, min_size: int = 3) -> list[Discovery]:
    """Grupos de mercados parecidos (componentes conexas de `similar_to`).

    Un cluster grande es una familia de mercados que se mueven por la misma
    informacion: el sitio natural donde buscar inconsistencias entre precios
    relacionados. El MIG solo lo NOMBRA; nadie opera nada aqui.
    """
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    sims = g.edges_of(EdgeType.SIMILAR_TO)
    for e in sims:
        union(e.src, e.dst)

    groups: dict[str, list[str]] = defaultdict(list)
    for key in list(parent):
        groups[find(key)].append(key)

    strength: dict[str, list[float]] = defaultdict(list)
    for e in sims:
        strength[find(e.src)].append(float(e.weight))

    out: list[Discovery] = []
    for root, members in groups.items():
        if len(members) < min_size:
            continue
        sims_in = strength.get(root, [])
        avg_sim = sum(sims_in) / len(sims_in) if sims_in else 0.0
        labels = [(g.get_node(m).label if g.get_node(m) else m) for m in members[:5]]
        out.append(Discovery(
            kind="market_cluster",
            subject=root,
            statement=(
                f"Cluster de {len(members)} mercados relacionados "
                f"(similitud media {avg_sim:.2f}). Ejemplos: "
                + "; ".join(x[:60] for x in labels if x)
            ),
            score=round(min(1.0, len(members) / 10.0) * min(1.0, avg_sim / 0.6), 4),
            value=float(len(members)),
            evidence={"size": len(members), "avg_similarity": round(avg_sim, 4),
                      "members": members[:25]},
            feature_name="mig_cluster_size",
        ))
    return sorted(out, key=lambda d: d.score, reverse=True)


def event_series(g: IntelligenceGraph, min_markets: int = 3) -> list[Discovery]:
    """Series temporales de mercados (Event con cadena `preceded`).

    Una serie con historia es el unico sitio del sistema donde "lo que paso
    antes" es comparable con "lo que viene": es material de estudio, no de
    ejecucion.
    """
    out: list[Discovery] = []
    for node in g.nodes_of(NodeType.EVENT):
        members = g.neighbors(node.key, EdgeType.BELONGS_TO, incoming=True)
        if len(members) < min_markets:
            continue
        chain = sum(1 for e in g.edges_of(EdgeType.PRECEDED)
                    if e.meta.get("event") == node.key.split(":", 1)[1])
        out.append(Discovery(
            kind="event_series",
            subject=node.key.split(":", 1)[1],
            statement=(
                f"Serie '{node.label}' con {len(members)} mercados y {chain} "
                f"transiciones ordenadas en el tiempo."
            ),
            score=round(min(1.0, len(members) / 20.0), 4),
            value=float(len(members)),
            evidence={"markets": len(members), "transitions": chain},
            feature_name=f"mig_series_{node.key.split(':', 1)[1]}",
        ))
    return sorted(out, key=lambda d: d.score, reverse=True)


def hub_markets(g: IntelligenceGraph, limit: int = 5, min_degree: int = 4) -> list[Discovery]:
    """Mercados mas conectados del grafo (grado). Son los que concentran
    relaciones: donde una observacion nueva toca mas cosas a la vez."""
    out: list[Discovery] = []
    for item in g.top_nodes(limit=limit, node_type=NodeType.MARKET):
        if item["degree"] < min_degree:
            continue
        out.append(Discovery(
            kind="hub_market",
            subject=item["key"].split(":", 1)[1],
            statement=(
                f"Mercado central del grafo con grado {item['degree']}: "
                f"{(item['label'] or item['key'])[:120]}"
            ),
            score=round(min(1.0, item["degree"] / 20.0), 4),
            value=float(item["degree"]),
            evidence={"degree": item["degree"], "category": item["meta"].get("category", "")},
            feature_name="mig_market_degree",
        ))
    return out


def extract_discoveries(
    g: IntelligenceGraph, *, min_samples: int = 30, min_observations: int = 20,
) -> list[Discovery]:
    """Todos los descubrimientos del grafo, ordenados por score descendente."""
    found: list[Discovery] = []
    found += strategy_specializations(g, min_samples=min_samples)
    found += experiment_verdicts(g, min_samples=min_samples)
    found += feature_associations(g, min_observations=min_observations)
    found += market_clusters(g)
    found += event_series(g)
    found += hub_markets(g)
    return sorted(found, key=lambda d: (d.score, d.kind), reverse=True)
