"""Repositorio de Wallet Intelligence: persistencia en PostgreSQL.

Convencion del proyecto: cada funcion abre su sesion, hace commit y devuelve
dicts (nunca objetos ORM).

SOLO ESCRIBE en tablas `wi_*`. Las lecturas de tablas ajenas se limitan a
`markets` (para descubrir en que mercados buscar wallets) y van a traves de los
repositorios que ya existen. Hay un test que lo verifica sobre el codigo fuente.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

from sqlalchemy import delete, desc, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from medusa.infra.db import get_sessionmaker
from medusa.intelligence.wallet.models import (
    WalletClusterRow,
    WalletDNAHistoryRow,
    WalletProfileRow,
    WalletRunRow,
    WalletSimilarityRow,
)

_CHUNK = 300


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _json(data: Any) -> str:
    if not data:
        return ""
    try:
        return json.dumps(data, default=str)
    except (TypeError, ValueError):
        return ""


def _loads(raw: str) -> Any:
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}


def _row_to_dict(row: Any) -> dict:
    out: dict = {}
    for col in row.__table__.columns:
        val = getattr(row, col.name)
        if isinstance(val, dt.datetime):
            val = val.isoformat()
        out[col.name] = val
    for field in ("dna", "categories", "centroid", "separating_features",
                  "population", "feature_importance"):
        if field in out and isinstance(out[field], str):
            out[field] = _loads(out[field])
    return out


def _chunks(items: list, size: int = _CHUNK):
    for i in range(0, len(items), size):
        yield items[i:i + size]


# ------------------------------------------------------------- escritura ----
async def upsert_profiles(rows: list[dict]) -> int:
    """Perfiles vigentes. `first_seen` se CONSERVA en el conflicto (no aparece
    en el SET): es el ancla de todo lo longitudinal."""
    if not rows:
        return 0
    now = _utcnow()
    written = 0
    async with get_sessionmaker()() as s:
        async with s.begin():
            for batch in _chunks(rows):
                stmt = pg_insert(WalletProfileRow).values([
                    {
                        "wallet": r["wallet"], "dna": _json(r.get("dna")),
                        "score": float(r.get("score") or 0.0),
                        "reputation": float(r.get("reputation") or 0.0),
                        "cluster": int(r.get("cluster", -1)),
                        "n_positions": int(r.get("n_positions") or 0),
                        "n_closed": int(r.get("n_closed") or 0),
                        "n_markets": int(r.get("n_markets") or 0),
                        "n_categories": int(r.get("n_categories") or 0),
                        "categories": _json(r.get("categories")),
                        "first_trade": r.get("first_trade"),
                        "last_trade": r.get("last_trade"),
                        "first_seen": now, "updated_at": now,
                    }
                    for r in batch
                ])
                await s.execute(stmt.on_conflict_do_update(
                    index_elements=[WalletProfileRow.wallet],
                    set_={
                        "dna": stmt.excluded.dna,
                        "score": stmt.excluded.score,
                        "reputation": stmt.excluded.reputation,
                        "cluster": stmt.excluded.cluster,
                        "n_positions": stmt.excluded.n_positions,
                        "n_closed": stmt.excluded.n_closed,
                        "n_markets": stmt.excluded.n_markets,
                        "n_categories": stmt.excluded.n_categories,
                        "categories": stmt.excluded.categories,
                        "first_trade": stmt.excluded.first_trade,
                        "last_trade": stmt.excluded.last_trade,
                        "updated_at": stmt.excluded.updated_at,
                    },
                ))
                written += len(batch)
    return written


async def save_dna_history(rows: list[dict]) -> int:
    """Foto del ADN de cada wallet (append-only): la serie de evolucion."""
    if not rows:
        return 0
    now = _utcnow()
    async with get_sessionmaker()() as s:
        async with s.begin():
            for r in rows:
                s.add(WalletDNAHistoryRow(
                    ts=now, wallet=r["wallet"], dna=_json(r.get("dna")),
                    score=float(r.get("score") or 0.0),
                    reputation=float(r.get("reputation") or 0.0),
                    cluster=int(r.get("cluster", -1)),
                    n_closed=int(r.get("n_closed") or 0),
                ))
    return len(rows)


async def upsert_similarity(pairs: list[dict]) -> int:
    if not pairs:
        return 0
    now = _utcnow()
    written = 0
    async with get_sessionmaker()() as s:
        async with s.begin():
            for batch in _chunks(pairs):
                stmt = pg_insert(WalletSimilarityRow).values([
                    {"wallet_a": p["wallet_a"], "wallet_b": p["wallet_b"],
                     "similarity": float(p.get("similarity") or 0.0),
                     "updated_at": now}
                    for p in batch
                ])
                await s.execute(stmt.on_conflict_do_update(
                    index_elements=[WalletSimilarityRow.wallet_a,
                                    WalletSimilarityRow.wallet_b],
                    set_={"similarity": stmt.excluded.similarity,
                          "updated_at": stmt.excluded.updated_at},
                ))
                written += len(batch)
    return written


async def save_clusters(clusters: list[dict]) -> int:
    """Centroides de la pasada. Append: la historia de clusters es lo que
    permite ver si la estructura de la poblacion se mueve."""
    if not clusters:
        return 0
    now = _utcnow()
    async with get_sessionmaker()() as s:
        async with s.begin():
            for c in clusters:
                s.add(WalletClusterRow(
                    ts=now, cluster=int(c.get("cluster", -1)),
                    size=int(c.get("size") or 0), share=float(c.get("share") or 0.0),
                    centroid=_json(c.get("centroid")),
                    separating_features=_json(c.get("separating_features")),
                ))
    return len(clusters)


async def save_run(
    *, wallets: int, positions: int, clusters: int, similarity_pairs: int,
    build_seconds: float, population: dict, feature_importance: list,
) -> None:
    async with get_sessionmaker()() as s:
        async with s.begin():
            s.add(WalletRunRow(
                wallets=wallets, positions=positions, clusters=clusters,
                similarity_pairs=similarity_pairs,
                build_seconds=round(float(build_seconds), 3),
                population=_json(population),
                feature_importance=_json(feature_importance),
            ))


# --------------------------------------------------------------- lectura ----
async def list_profiles(
    limit: int = 100, order_by: str = "reputation", cluster: int | None = None,
    search: str | None = None, min_closed: int = 0,
) -> list[dict]:
    """Wallet Explorer: listado ordenable por reputacion, score o muestra."""
    col = {
        "reputation": WalletProfileRow.reputation,
        "score": WalletProfileRow.score,
        "n_closed": WalletProfileRow.n_closed,
        "updated_at": WalletProfileRow.updated_at,
    }.get(order_by, WalletProfileRow.reputation)
    async with get_sessionmaker()() as s:
        q = select(WalletProfileRow)
        if cluster is not None:
            q = q.where(WalletProfileRow.cluster == cluster)
        if search:
            q = q.where(WalletProfileRow.wallet.ilike(f"%{search.lower()}%"))
        if min_closed > 0:
            q = q.where(WalletProfileRow.n_closed >= min_closed)
        res = await s.execute(q.order_by(desc(col)).limit(limit))
        return [_row_to_dict(r) for r in res.scalars().all()]


async def get_profile(wallet: str) -> dict | None:
    async with get_sessionmaker()() as s:
        row = await s.get(WalletProfileRow, wallet.lower())
        return _row_to_dict(row) if row else None


async def wallet_history(wallet: str, limit: int = 200) -> list[dict]:
    """Evolucion de una wallet, de vieja a nueva (lista para dibujar)."""
    async with get_sessionmaker()() as s:
        res = await s.execute(
            select(WalletDNAHistoryRow)
            .where(WalletDNAHistoryRow.wallet == wallet.lower())
            .order_by(desc(WalletDNAHistoryRow.ts)).limit(limit)
        )
        return [_row_to_dict(r) for r in reversed(res.scalars().all())]


async def similar_to(wallet: str, limit: int = 10) -> list[dict]:
    """Vecinos de una wallet. Se busca en los DOS extremos porque el par se
    guarda una sola vez (a < b)."""
    w = wallet.lower()
    async with get_sessionmaker()() as s:
        res = await s.execute(
            select(WalletSimilarityRow)
            .where((WalletSimilarityRow.wallet_a == w) | (WalletSimilarityRow.wallet_b == w))
            .order_by(desc(WalletSimilarityRow.similarity)).limit(limit)
        )
        out = []
        for row in res.scalars().all():
            other = row.wallet_b if row.wallet_a == w else row.wallet_a
            out.append({"wallet": other, "similarity": round(row.similarity, 6)})
        return out


async def latest_clusters() -> list[dict]:
    """Clusters de la ULTIMA pasada."""
    async with get_sessionmaker()() as s:
        last = await s.execute(select(func.max(WalletClusterRow.ts)))
        ts = last.scalar()
        if ts is None:
            return []
        res = await s.execute(
            select(WalletClusterRow).where(WalletClusterRow.ts == ts)
            .order_by(desc(WalletClusterRow.size))
        )
        return [_row_to_dict(r) for r in res.scalars().all()]


async def latest_run() -> dict | None:
    async with get_sessionmaker()() as s:
        res = await s.execute(select(WalletRunRow).order_by(desc(WalletRunRow.ts)).limit(1))
        row = res.scalars().first()
        return _row_to_dict(row) if row else None


async def runs(limit: int = 100) -> list[dict]:
    async with get_sessionmaker()() as s:
        res = await s.execute(select(WalletRunRow).order_by(desc(WalletRunRow.ts)).limit(limit))
        return [_row_to_dict(r) for r in reversed(res.scalars().all())]


async def stats() -> dict:
    """Resumen para la cabecera del dashboard."""
    async with get_sessionmaker()() as s:
        total = await s.execute(select(func.count()).select_from(WalletProfileRow))
        with_sample = await s.execute(
            select(func.count()).select_from(WalletProfileRow)
            .where(WalletProfileRow.n_closed > 0)
        )
        avg_rep = await s.execute(select(func.avg(WalletProfileRow.reputation)))
        pairs = await s.execute(select(func.count()).select_from(WalletSimilarityRow))
        return {
            "wallets": int(total.scalar() or 0),
            "wallets_with_closed_positions": int(with_sample.scalar() or 0),
            "avg_reputation": round(float(avg_rep.scalar() or 0.0), 6),
            "similarity_pairs": int(pairs.scalar() or 0),
            "last_run": await latest_run(),
        }


async def top_reputation(limit: int = 25, min_closed: int = 1) -> list[dict]:
    return await list_profiles(limit=limit, order_by="reputation", min_closed=min_closed)


async def prune(days: int) -> dict:
    """Poda del historico y de las pasadas. Los PERFILES no se podan: son el
    estado actual, y borrarlos perderia el `first_seen`."""
    cutoff = _utcnow() - dt.timedelta(days=days)
    deleted: dict[str, int] = {}
    async with get_sessionmaker()() as s:
        async with s.begin():
            for name, table, col in (
                ("wi_dna_history", WalletDNAHistoryRow, WalletDNAHistoryRow.ts),
                ("wi_clusters", WalletClusterRow, WalletClusterRow.ts),
                ("wi_runs", WalletRunRow, WalletRunRow.ts),
            ):
                res = await s.execute(delete(table).where(col < cutoff))
                deleted[name] = res.rowcount or 0
    return deleted
