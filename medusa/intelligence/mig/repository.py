"""Repositorio del MIG: persistencia del grafo en PostgreSQL.

Sigue la convencion del resto de Medusa (`medusa/data/repositories.py`): cada
funcion abre su propia sesion async, hace commit y devuelve dicts, nunca objetos
ORM.

Este modulo es de SOLO ESCRITURA sobre tablas `mig_*` y de SOLO LECTURA sobre
todo lo demas. No hay una sola sentencia que modifique `markets`, `positions`,
`trades`, `orders`, `strategy_signals`, `bot_state` ni `features`. Hay un test
(`tests/test_mig_isolation.py`) que lo verifica sobre el codigo fuente, para que
deje de ser una promesa y sea una comprobacion.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

from sqlalchemy import delete, desc, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from medusa.data import repositories as repo
from medusa.infra.db import get_sessionmaker
from medusa.intelligence.mig.models import (
    MIGDiscoveryRow,
    MIGEdgeRow,
    MIGNodeRow,
    MIGSnapshotRow,
)
from medusa.intelligence.mig.types import Discovery, GraphEdge, GraphNode

# Tamaño de lote de los upserts. El grafo puede traer miles de nodos; mandarlos
# en una sola sentencia hincha la memoria del proceso y el log de Postgres en
# una maquina de 3 GB.
_CHUNK = 500


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _json(data: Any) -> str:
    if not data:
        return ""
    try:
        return json.dumps(data, default=str)
    except (TypeError, ValueError):
        return ""


def _loads(raw: str) -> dict:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def _row_to_dict(row: Any) -> dict:
    out: dict = {}
    for col in row.__table__.columns:
        val = getattr(row, col.name)
        if isinstance(val, dt.datetime):
            val = val.isoformat()
        out[col.name] = val
    for field in ("meta", "evidence", "node_counts", "edge_counts", "sources"):
        if field in out and isinstance(out[field], str):
            out[field] = _loads(out[field])
    return out


def _chunks(items: list, size: int = _CHUNK):
    for i in range(0, len(items), size):
        yield items[i:i + size]


# ------------------------------------------------------------- escritura ----
async def upsert_nodes(nodes: list[GraphNode]) -> int:
    """Inserta o actualiza nodos.

    `first_seen` se conserva SIEMPRE en el conflicto (no aparece en el SET): es
    la fecha en la que el grafo vio esa entidad por primera vez y es lo unico
    que permite medir crecimiento real. Reescribirla convertiria la curva de
    crecimiento en una linea plana que solo dice "hoy hay N nodos".
    """
    if not nodes:
        return 0
    now = _utcnow()
    written = 0
    async with get_sessionmaker()() as s:
        async with s.begin():
            for batch in _chunks(nodes):
                stmt = pg_insert(MIGNodeRow).values([
                    {
                        "key": n.key, "node_type": n.node_type.value,
                        "label": n.label or "", "weight": float(n.weight),
                        "meta": _json(n.meta), "first_seen": n.ts or now,
                        "last_seen": now,
                    }
                    for n in batch
                ])
                await s.execute(stmt.on_conflict_do_update(
                    index_elements=[MIGNodeRow.key],
                    set_={
                        "label": stmt.excluded.label,
                        "weight": stmt.excluded.weight,
                        "meta": stmt.excluded.meta,
                        "last_seen": stmt.excluded.last_seen,
                    },
                ))
                written += len(batch)
    return written


async def upsert_edges(edges: list[GraphEdge]) -> int:
    """Inserta o actualiza aristas sobre el triple (src, dst, edge_type).

    En conflicto se PISAN peso y count con los del grafo recien construido, no
    se acumulan. Es deliberado: el builder ya recalcula ambos sobre toda la
    ventana de datos, asi que sumar aqui contaria dos veces las mismas
    observaciones y el peso dejaria de ser interpretable.
    """
    if not edges:
        return 0
    now = _utcnow()
    written = 0
    async with get_sessionmaker()() as s:
        async with s.begin():
            for batch in _chunks(edges):
                stmt = pg_insert(MIGEdgeRow).values([
                    {
                        "src": e.src, "dst": e.dst, "edge_type": e.edge_type.value,
                        "weight": float(e.weight), "count": int(e.count),
                        "meta": _json(e.meta), "first_seen": e.ts or now,
                        "last_seen": now,
                    }
                    for e in batch
                ])
                await s.execute(stmt.on_conflict_do_update(
                    index_elements=[MIGEdgeRow.src, MIGEdgeRow.dst, MIGEdgeRow.edge_type],
                    set_={
                        "weight": stmt.excluded.weight,
                        "count": stmt.excluded.count,
                        "meta": stmt.excluded.meta,
                        "last_seen": stmt.excluded.last_seen,
                    },
                ))
                written += len(batch)
    return written


async def save_discoveries(discoveries: list[Discovery]) -> int:
    """Guarda descubrimientos (append-only: cada construccion es una observacion)."""
    if not discoveries:
        return 0
    now = _utcnow()
    async with get_sessionmaker()() as s:
        async with s.begin():
            for d in discoveries:
                s.add(MIGDiscoveryRow(
                    ts=d.ts or now, kind=d.kind, subject=d.subject[:320],
                    statement=d.statement, score=float(d.score), value=float(d.value),
                    evidence=_json(d.evidence), feature_name=d.feature_name[:128],
                ))
    return len(discoveries)


async def save_snapshot(
    stats: dict, discoveries: int = 0, build_seconds: float = 0.0,
    sources: dict | None = None,
) -> None:
    """Foto del grafo tras una construccion: la serie de crecimiento."""
    async with get_sessionmaker()() as s:
        async with s.begin():
            s.add(MIGSnapshotRow(
                nodes=int(stats.get("nodes") or 0),
                edges=int(stats.get("edges") or 0),
                discoveries=int(discoveries),
                node_counts=_json(stats.get("node_counts")),
                edge_counts=_json(stats.get("edge_counts")),
                build_seconds=round(float(build_seconds), 3),
                sources=_json(sources or {}),
            ))


# --------------------------------------------------------------- lectura ----
async def graph_stats() -> dict:
    """Estadisticas del grafo PERSISTIDO (no del que se acaba de construir)."""
    async with get_sessionmaker()() as s:
        nodes = await s.execute(
            select(MIGNodeRow.node_type, func.count().label("n"))
            .group_by(MIGNodeRow.node_type).order_by(desc("n"))
        )
        node_counts = {row.node_type: int(row.n) for row in nodes.all()}
        edges = await s.execute(
            select(MIGEdgeRow.edge_type, func.count().label("n"),
                   func.avg(MIGEdgeRow.weight).label("avg_w"),
                   func.sum(MIGEdgeRow.count).label("obs"))
            .group_by(MIGEdgeRow.edge_type).order_by(desc("n"))
        )
        edge_rows = [
            {"edge_type": r.edge_type, "n": int(r.n),
             "avg_weight": round(float(r.avg_w or 0), 6),
             "observations": int(r.obs or 0)}
            for r in edges.all()
        ]
        last_build = await s.execute(
            select(MIGSnapshotRow).order_by(desc(MIGSnapshotRow.ts)).limit(1)
        )
        snap = last_build.scalars().first()
        newest = await s.execute(select(func.max(MIGNodeRow.last_seen)))
        oldest = await s.execute(select(func.min(MIGNodeRow.first_seen)))
        n_total = sum(node_counts.values())
        e_total = sum(r["n"] for r in edge_rows)
        return {
            "nodes": n_total,
            "edges": e_total,
            "node_counts": node_counts,
            "edge_counts": {r["edge_type"]: r["n"] for r in edge_rows},
            "edge_detail": edge_rows,
            "density": round(e_total / (n_total * (n_total - 1)), 8) if n_total > 1 else 0.0,
            "first_seen": (oldest.scalar() or None) and oldest.scalar().isoformat(),
            "last_seen": (newest.scalar() or None) and newest.scalar().isoformat(),
            "last_build": _row_to_dict(snap) if snap else None,
        }


async def list_nodes(
    limit: int = 100, node_type: str | None = None, search: str | None = None,
) -> list[dict]:
    async with get_sessionmaker()() as s:
        q = select(MIGNodeRow)
        if node_type:
            q = q.where(MIGNodeRow.node_type == node_type)
        if search:
            q = q.where(MIGNodeRow.label.ilike(f"%{search}%"))
        res = await s.execute(q.order_by(desc(MIGNodeRow.last_seen)).limit(limit))
        return [_row_to_dict(r) for r in res.scalars().all()]


async def list_edges(
    limit: int = 100, edge_type: str | None = None, node: str | None = None,
) -> list[dict]:
    async with get_sessionmaker()() as s:
        q = select(MIGEdgeRow)
        if edge_type:
            q = q.where(MIGEdgeRow.edge_type == edge_type)
        if node:
            q = q.where((MIGEdgeRow.src == node) | (MIGEdgeRow.dst == node))
        res = await s.execute(
            q.order_by(desc(MIGEdgeRow.count), desc(MIGEdgeRow.weight)).limit(limit)
        )
        return [_row_to_dict(r) for r in res.scalars().all()]


async def neighbors(node_key: str, limit: int = 50) -> dict:
    """Vecindario de un nodo: sus aristas de salida y de entrada."""
    async with get_sessionmaker()() as s:
        out = await s.execute(
            select(MIGEdgeRow).where(MIGEdgeRow.src == node_key)
            .order_by(desc(MIGEdgeRow.count)).limit(limit)
        )
        inc = await s.execute(
            select(MIGEdgeRow).where(MIGEdgeRow.dst == node_key)
            .order_by(desc(MIGEdgeRow.count)).limit(limit)
        )
        node = await s.get(MIGNodeRow, node_key)
        return {
            "node": _row_to_dict(node) if node else None,
            "outgoing": [_row_to_dict(r) for r in out.scalars().all()],
            "incoming": [_row_to_dict(r) for r in inc.scalars().all()],
        }


async def list_discoveries(
    limit: int = 50, kind: str | None = None, latest_only: bool = True,
) -> list[dict]:
    """Descubrimientos. Por defecto SOLO el mas reciente de cada (kind, subject):
    la tabla es append-only y sin esto el panel repetiria el mismo patron una vez
    por construccion."""
    async with get_sessionmaker()() as s:
        q = select(MIGDiscoveryRow)
        if kind:
            q = q.where(MIGDiscoveryRow.kind == kind)
        if latest_only:
            # DISTINCT ON es de Postgres y es exactamente lo que hace falta: una
            # pasada ordenada en vez de un GROUP BY con subconsulta.
            q = q.distinct(MIGDiscoveryRow.kind, MIGDiscoveryRow.subject).order_by(
                MIGDiscoveryRow.kind, MIGDiscoveryRow.subject, desc(MIGDiscoveryRow.ts)
            )
            res = await s.execute(q)
            rows = [_row_to_dict(r) for r in res.scalars().all()]
            rows.sort(key=lambda r: r.get("score") or 0, reverse=True)
            return rows[:limit]
        res = await s.execute(q.order_by(desc(MIGDiscoveryRow.ts)).limit(limit))
        return [_row_to_dict(r) for r in res.scalars().all()]


async def growth(limit: int = 200) -> list[dict]:
    """Serie de crecimiento del grafo (una fila por construccion), de vieja a
    nueva para que el dashboard la dibuje sin invertirla."""
    async with get_sessionmaker()() as s:
        res = await s.execute(
            select(MIGSnapshotRow).order_by(desc(MIGSnapshotRow.ts)).limit(limit)
        )
        return [_row_to_dict(r) for r in reversed(res.scalars().all())]


async def prune(days: int) -> dict:
    """Poda de lo unico que crece sin techo: snapshots y descubrimientos viejos.

    Nodos y aristas NO se podan aqui: son el grafo. Perderlos rompe el
    `first_seen` y con el toda la historia de crecimiento.
    """
    cutoff = _utcnow() - dt.timedelta(days=days)
    deleted: dict[str, int] = {}
    async with get_sessionmaker()() as s:
        async with s.begin():
            for name, table, col in (
                ("mig_discoveries", MIGDiscoveryRow, MIGDiscoveryRow.ts),
                ("mig_snapshots", MIGSnapshotRow, MIGSnapshotRow.ts),
            ):
                res = await s.execute(delete(table).where(col < cutoff))
                deleted[name] = res.rowcount or 0
    return deleted


# ------------------------------------------------------- fuentes (lectura) ---
# Lecturas de las tablas del sistema de trading. SOLO SELECT: el MIG observa,
# no escribe una sola fila fuera de sus propias tablas.

async def load_sources(
    max_markets: int = 300, max_signals: int = 5000, max_trades: int = 2000,
    max_features: int = 5000,
) -> dict[str, list[dict]]:
    """Fotografia de los datos con los que se construye el grafo.

    Los topes existen para que una BD con meses de historia no se cargue entera
    en 3 GB de RAM. Son configurables (MIG_MAX_*) y su efecto es acotar la
    ventana, no falsear nada: lo que no entra, simplemente no se relaciona
    todavia.
    """
    markets = await repo.list_ranked_markets(limit=max_markets)
    if not markets:
        # `list_ranked_markets` solo mira la ultima hora (es un ranking vivo).
        # Con el engine parado eso da vacio; para el grafo, un mercado de ayer
        # sigue siendo una entidad perfectamente valida.
        markets = await repo.list_markets(limit=max_markets)
    signals = await repo.list_signals(limit=max_signals)
    trades = await repo.list_trades(limit=max_trades)
    features = await repo.list_features(limit=max_features)
    return {
        "markets": markets, "signals": signals, "trades": trades,
        "features": features,
        "wallets": await _load_wallets(),
    }


async def _load_wallets(limit: int = 200) -> list[dict]:
    """Wallets perfiladas por Wallet Intelligence, si ese paquete esta presente.

    Hasta que existio Wallet Intelligence esta fuente llegaba VACIA y los nodos
    `wallet` del grafo no se creaban -- documentado asi en su momento en vez de
    inventar datos. Ahora hay fuente real, pero sigue siendo opcional: si el
    paquete falla o no hay perfiles, el grafo se construye igual que antes.

    Solo se traen las categorias (de donde sale `specialized_in`). NO se
    inventan aristas `participated_in` hacia mercados: el perfil no guarda en
    que mercados concretos opero cada wallet, y fabricarlas seria justo el tipo
    de dato falso que este grafo no puede permitirse.
    """
    try:
        from medusa.intelligence.wallet import repository as wi_repo
    except Exception:  # noqa: BLE001 - paquete opcional
        return []
    try:
        profiles = await wi_repo.list_profiles(limit=limit, order_by="reputation")
    except Exception:  # noqa: BLE001 - tabla inexistente o BD caida
        return []
    out: list[dict] = []
    for p in profiles:
        cats = p.get("categories") or {}
        dna = p.get("dna") or {}
        out.append({
            "address": p.get("wallet"),
            "markets": [],
            "categories": {c: int(v.get("n") or 0) for c, v in cats.items()
                           if isinstance(v, dict)},
            "roi": float(dna.get("roi_historical") or 0.0),
            "win_rate": float(dna.get("win_rate") or 0.0),
        })
    return out
