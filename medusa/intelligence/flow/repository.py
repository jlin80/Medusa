"""Repositorio del IFE: persistencia en PostgreSQL.

Sigue la convencion del resto de Medusa (`medusa/data/repositories.py`): cada
funcion abre su propia sesion async, hace commit y devuelve dicts, nunca objetos
ORM.

Este modulo es de SOLO ESCRITURA sobre tablas `flow_*` y de SOLO LECTURA sobre
todo lo demas. No hay una sola sentencia que modifique `markets`, `positions`,
`trades`, `orders`, `strategy_signals`, `bot_state` ni `features`. Hay un test
(`tests/test_flow_isolation.py`) que lo verifica sobre el codigo fuente, para
que deje de ser una promesa y sea una comprobacion.

Ojo con el nombre: la tabla `trades` es la de operaciones de MEDUSA y aqui no se
toca. La cinta publica de Polymarket vive en `flow_trades`, que es otra cosa.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

from sqlalchemy import delete, desc, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from medusa.data import repositories as repo
from medusa.infra.db import get_sessionmaker
from medusa.intelligence.flow import metrics
from medusa.intelligence.flow.models import (
    FlowCascadeRow,
    FlowEventRow,
    FlowMarketRow,
    FlowSnapshotRow,
    FlowTradeRow,
    FlowWalletRow,
)
from medusa.intelligence.flow.types import (
    Cascade,
    Entry,
    FlowTrade,
    MarketFlowMetrics,
    PropagationEvent,
    WalletFlowMetrics,
)

# Tamaño de lote de los upserts. Una pasada puede traer decenas de miles de
# eslabones; mandarlos en una sola sentencia hincha la memoria del proceso y el
# log de Postgres en una maquina de 3 GB.
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


def _loads(raw: str) -> Any:
    if not raw:
        return []
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []


def _row_to_dict(row: Any) -> dict:
    out: dict = {}
    for col in row.__table__.columns:
        val = getattr(row, col.name)
        if isinstance(val, dt.datetime):
            val = val.isoformat()
        out[col.name] = val
    if isinstance(out.get("participants"), str):
        out["participants"] = _loads(out["participants"])
    return out


def _chunks(items: list, size: int = _CHUNK):
    for i in range(0, len(items), size):
        yield items[i:i + size]


# ------------------------------------------------------------- escritura ----
async def save_trades(trades: list[FlowTrade]) -> int:
    """Guarda la cinta cruda. Los repetidos se IGNORAN (no se actualizan).

    `on conflict do nothing` y no `do update`: un trade es un hecho pasado y no
    cambia. Reescribirlo solo podria empeorarlo si una lectura posterior de la
    API viniera con menos campos. El contador devuelto son las filas NUEVAS, que
    es la unica cifra que dice si la ingesta esta aportando algo.
    """
    if not trades:
        return 0
    now = _utcnow()
    written = 0
    async with get_sessionmaker()() as s:
        async with s.begin():
            for batch in _chunks(trades):
                stmt = pg_insert(FlowTradeRow).values([
                    {"uid": t.uid, "market_id": t.market_id, "wallet": t.wallet,
                     "side": t.side, "price": float(t.price), "size": float(t.size),
                     "ts": t.ts, "ingested_at": now}
                    for t in batch
                ])
                res = await s.execute(
                    stmt.on_conflict_do_nothing(index_elements=[FlowTradeRow.uid])
                )
                written += res.rowcount or 0
    return written


async def save_cascades(cascades: list[Cascade]) -> int:
    """Guarda cascadas. En conflicto se ACTUALIZAN.

    Al contrario que un trade, una cascada si cambia: una racha detectada hace
    una hora puede tener participantes nuevos en la pasada siguiente, y su
    mercado puede haber resuelto. Pisar la fila con el analisis mas reciente es
    justo lo correcto; `cascade_key` (mercado + lado + instante de inicio) hace
    que siga siendo la misma cascada y no una nueva.
    """
    if not cascades:
        return 0
    written = 0
    async with get_sessionmaker()() as s:
        async with s.begin():
            for batch in _chunks(cascades):
                stmt = pg_insert(FlowCascadeRow).values([
                    {"cascade_key": c.key, "market_id": c.market_id, "side": c.side,
                     "started_at": c.started_at, "ended_at": c.ended_at,
                     "n_participants": c.n, "span_seconds": c.span_seconds,
                     "propagation_time": c.propagation_time,
                     "consensus_delay": c.consensus_delay,
                     "price_start": c.price_start, "price_end": c.price_end,
                     "price_move": c.price_move, "notional": c.notional,
                     "resolved": bool(c.resolved),
                     "resolution_value": c.resolution_value,
                     "participants": _json([e.wallet for e in c.entries])}
                    for c in batch
                ])
                await s.execute(stmt.on_conflict_do_update(
                    index_elements=[FlowCascadeRow.cascade_key],
                    set_={
                        "ended_at": stmt.excluded.ended_at,
                        "n_participants": stmt.excluded.n_participants,
                        "span_seconds": stmt.excluded.span_seconds,
                        "propagation_time": stmt.excluded.propagation_time,
                        "consensus_delay": stmt.excluded.consensus_delay,
                        "price_end": stmt.excluded.price_end,
                        "price_move": stmt.excluded.price_move,
                        "notional": stmt.excluded.notional,
                        "resolved": stmt.excluded.resolved,
                        "resolution_value": stmt.excluded.resolution_value,
                        "participants": stmt.excluded.participants,
                    },
                ))
                written += len(batch)
    return written


async def save_events(events: list[PropagationEvent]) -> int:
    """Guarda TODOS los eslabones de propagacion.

    Sin filtrar por score, sin agregar y sin quedarse con los mejores: el motor
    promete guardar cada evento de propagacion, y agregarlos aqui haria imposible
    volver a analizarlos con otra definicion mañana. Los repetidos se ignoran
    (el triple cascada+lider+seguidor ya identifica el eslabon), asi que dos
    pasadas sobre la misma ventana no inflan la muestra.
    """
    if not events:
        return 0
    written = 0
    async with get_sessionmaker()() as s:
        async with s.begin():
            for batch in _chunks(events):
                stmt = pg_insert(FlowEventRow).values([
                    {"cascade_key": e.cascade_key, "market_id": e.market_id,
                     "side": e.side, "leader": e.leader, "follower": e.follower,
                     "hop": int(e.hop), "lag_seconds": float(e.lag_seconds),
                     "price_leader": float(e.price_leader),
                     "price_follower": float(e.price_follower),
                     "price_move": float(e.price_move), "ts": e.ts}
                    for e in batch
                ])
                res = await s.execute(stmt.on_conflict_do_nothing(
                    index_elements=[FlowEventRow.cascade_key, FlowEventRow.leader,
                                    FlowEventRow.follower]
                ))
                written += res.rowcount or 0
    return written


async def upsert_wallet_metrics(rows: list[WalletFlowMetrics]) -> int:
    """Estado actual por wallet. `first_seen` NUNCA se reescribe: es lo unico
    que permite saber desde cuando se observa a esa wallet."""
    if not rows:
        return 0
    now = _utcnow()
    written = 0
    async with get_sessionmaker()() as s:
        async with s.begin():
            for batch in _chunks(rows):
                stmt = pg_insert(FlowWalletRow).values([
                    {**m.to_dict(), "first_seen": now, "last_seen": now}
                    for m in batch
                ])
                await s.execute(stmt.on_conflict_do_update(
                    index_elements=[FlowWalletRow.wallet],
                    set_={c: getattr(stmt.excluded, c) for c in (
                        "n_cascades", "n_markets", "leadership_score", "follow_score",
                        "leadership_lower", "edge_vs_chance", "information_speed",
                        "speed_score", "propagation_time", "early_information_score",
                        "late_information_score", "n_early", "n_late", "early_lower",
                        "late_lower", "information_edge", "enough_samples", "last_seen",
                    )},
                ))
                written += len(batch)
    return written


async def upsert_market_metrics(rows: list[MarketFlowMetrics]) -> int:
    if not rows:
        return 0
    now = _utcnow()
    written = 0
    async with get_sessionmaker()() as s:
        async with s.begin():
            for batch in _chunks(rows):
                stmt = pg_insert(FlowMarketRow).values([
                    {**m.to_dict(), "first_seen": now, "last_seen": now}
                    for m in batch
                ])
                await s.execute(stmt.on_conflict_do_update(
                    index_elements=[FlowMarketRow.market_id],
                    set_={c: getattr(stmt.excluded, c) for c in (
                        "n_cascades", "n_wallets", "n_events", "consensus_delay",
                        "propagation_time", "information_speed", "avg_cascade_size",
                        "price_move", "enough_samples", "last_seen",
                    )},
                ))
                written += len(batch)
    return written


async def save_snapshot(
    summary: dict, *, markets: int = 0, trades: int = 0, build_seconds: float = 0.0,
) -> None:
    """Foto tras una pasada: la serie temporal del motor."""
    async with get_sessionmaker()() as s:
        async with s.begin():
            s.add(FlowSnapshotRow(
                markets=int(markets), trades=int(trades),
                cascades=int(summary.get("cascades") or 0),
                events=int(summary.get("events") or 0),
                wallets=int(summary.get("wallets") or 0),
                resolved_cascades=int(summary.get("resolved_cascades") or 0),
                median_propagation_time=float(summary.get("median_propagation_time") or 0.0),
                median_consensus_delay=float(summary.get("median_consensus_delay") or 0.0),
                median_lag=float(summary.get("median_lag") or 0.0),
                median_cascade_size=float(summary.get("median_cascade_size") or 0.0),
                build_seconds=round(float(build_seconds), 3),
            ))


# --------------------------------------------------------------- lectura ----
async def load_trades(market_ids: list[str], since: dt.datetime) -> list[FlowTrade]:
    """Cinta guardada de unos mercados desde un instante.

    Es lo que permite que una pasada analice mas historia de la que la API
    devuelve en una pagina: la ventana de analisis sale de la BD, no de la red.
    """
    if not market_ids:
        return []
    async with get_sessionmaker()() as s:
        res = await s.execute(
            select(FlowTradeRow)
            .where(FlowTradeRow.market_id.in_(market_ids), FlowTradeRow.ts >= since)
            .order_by(FlowTradeRow.ts)
        )
        return [
            FlowTrade(market_id=r.market_id, wallet=r.wallet, side=r.side,
                      price=float(r.price), size=float(r.size), ts=r.ts, uid=r.uid)
            for r in res.scalars().all()
        ]


async def flow_stats() -> dict:
    """Estadisticas de lo PERSISTIDO (no de la pasada que se acaba de hacer)."""
    async with get_sessionmaker()() as s:
        totals = {}
        for name, table in (("trades", FlowTradeRow), ("cascades", FlowCascadeRow),
                            ("events", FlowEventRow), ("wallets", FlowWalletRow),
                            ("markets", FlowMarketRow)):
            res = await s.execute(select(func.count()).select_from(table))
            totals[name] = int(res.scalar() or 0)
        agg = await s.execute(select(
            func.avg(FlowCascadeRow.n_participants),
            func.avg(FlowCascadeRow.propagation_time),
            func.avg(FlowCascadeRow.consensus_delay),
            func.min(FlowCascadeRow.started_at),
            func.max(FlowCascadeRow.ended_at),
        ))
        avg_size, avg_prop, avg_cons, first, last = agg.one()
        lag = await s.execute(select(func.avg(FlowEventRow.lag_seconds)))
        resolved = await s.execute(
            select(func.count()).select_from(FlowCascadeRow)
            .where(FlowCascadeRow.resolved.is_(True))
        )
        # Wallets con muestra suficiente: la unica cifra de "cuanto sabemos de
        # verdad". El total de wallets incluye a las de una sola cascada.
        with_evidence = await s.execute(
            select(func.count()).select_from(FlowWalletRow)
            .where(FlowWalletRow.enough_samples.is_(True))
        )
        last_snap = await s.execute(
            select(FlowSnapshotRow).order_by(desc(FlowSnapshotRow.ts)).limit(1))
        snap = last_snap.scalars().first()
        return {
            **totals,
            "resolved_cascades": int(resolved.scalar() or 0),
            "wallets_with_evidence": int(with_evidence.scalar() or 0),
            "avg_cascade_size": round(float(avg_size or 0), 3),
            "avg_propagation_time": round(float(avg_prop or 0), 3),
            "avg_consensus_delay": round(float(avg_cons or 0), 3),
            "avg_lag": round(float(lag.scalar() or 0), 3),
            "first_cascade": first.isoformat() if first else None,
            "last_cascade": last.isoformat() if last else None,
            "last_run": _row_to_dict(snap) if snap else None,
        }


async def list_cascades(
    limit: int = 50, market_id: str | None = None, min_participants: int = 0,
) -> list[dict]:
    async with get_sessionmaker()() as s:
        q = select(FlowCascadeRow)
        if market_id:
            q = q.where(FlowCascadeRow.market_id == market_id)
        if min_participants:
            q = q.where(FlowCascadeRow.n_participants >= min_participants)
        res = await s.execute(q.order_by(desc(FlowCascadeRow.started_at)).limit(limit))
        return [_row_to_dict(r) for r in res.scalars().all()]


async def list_events(
    limit: int = 100, wallet: str | None = None, market_id: str | None = None,
    cascade_key: str | None = None, max_hop: int | None = None,
) -> list[dict]:
    """Eslabones de propagacion. Orden temporal, jamas causalidad."""
    async with get_sessionmaker()() as s:
        q = select(FlowEventRow)
        if wallet:
            low = wallet.lower()
            q = q.where((FlowEventRow.leader == low) | (FlowEventRow.follower == low))
        if market_id:
            q = q.where(FlowEventRow.market_id == market_id)
        if cascade_key:
            q = q.where(FlowEventRow.cascade_key == cascade_key)
        if max_hop:
            q = q.where(FlowEventRow.hop <= max_hop)
        res = await s.execute(q.order_by(desc(FlowEventRow.ts)).limit(limit))
        return [_row_to_dict(r) for r in res.scalars().all()]


async def list_wallets(
    limit: int = 50, order_by: str = "leadership", only_with_evidence: bool = True,
) -> list[dict]:
    """Ranking de wallets.

    Por defecto SOLO las que tienen muestra suficiente. Un ranking encabezado
    por wallets de una sola cascada con leadership 1.0 no es un ranking: es una
    lista de casualidades ordenada por casualidad.
    """
    columns = {
        "leadership": desc(FlowWalletRow.leadership_lower),
        "speed": desc(FlowWalletRow.speed_score),
        "follow": desc(FlowWalletRow.follow_score),
        "early": desc(FlowWalletRow.early_lower),
        "edge": desc(FlowWalletRow.information_edge),
        "cascades": desc(FlowWalletRow.n_cascades),
    }
    async with get_sessionmaker()() as s:
        q = select(FlowWalletRow)
        if only_with_evidence:
            q = q.where(FlowWalletRow.enough_samples.is_(True))
        q = q.order_by(columns.get(order_by, columns["leadership"]),
                       desc(FlowWalletRow.n_cascades))
        res = await s.execute(q.limit(limit))
        return [_row_to_dict(r) for r in res.scalars().all()]


async def get_wallet(wallet: str) -> dict | None:
    async with get_sessionmaker()() as s:
        row = await s.get(FlowWalletRow, wallet.lower())
        return _row_to_dict(row) if row else None


async def list_markets(limit: int = 50) -> list[dict]:
    async with get_sessionmaker()() as s:
        res = await s.execute(
            select(FlowMarketRow).order_by(desc(FlowMarketRow.n_cascades)).limit(limit))
        return [_row_to_dict(r) for r in res.scalars().all()]


async def top_pairs(limit: int = 50, min_observations: int = 5) -> list[dict]:
    """Pares (lider, seguidor) que mas veces han coincidido en ese orden.

    NO ES UNA RELACION DE INFLUENCIA (ver `scoring.pair_stats`). `n` va primero
    en la salida a proposito: en un mercado con doscientas wallets activas hay
    pares que coinciden varias veces sin que medie absolutamente nada.
    """
    async with get_sessionmaker()() as s:
        res = await s.execute(
            select(
                FlowEventRow.leader, FlowEventRow.follower,
                func.count().label("n"),
                func.avg(FlowEventRow.lag_seconds).label("avg_lag"),
                func.avg(FlowEventRow.hop).label("avg_hop"),
                func.count(func.distinct(FlowEventRow.market_id)).label("markets"),
                func.avg(FlowEventRow.price_move).label("avg_move"),
            )
            .group_by(FlowEventRow.leader, FlowEventRow.follower)
            .having(func.count() >= min_observations)
            .order_by(desc("n")).limit(limit)
        )
        return [
            {"leader": r.leader, "follower": r.follower, "n": int(r.n),
             "avg_lag": round(float(r.avg_lag or 0), 3),
             "avg_hop": round(float(r.avg_hop or 0), 2),
             "markets": int(r.markets or 0),
             "avg_price_move": round(float(r.avg_move or 0), 6)}
            for r in res.all()
        ]


async def lag_histogram(buckets: int = 12, max_seconds: float = 3600.0) -> list[dict]:
    """Distribucion de las latencias lider->seguidor (para el Research Lab).

    El reparto en tramos lo hace `metrics.histogram`, que es puro y se testea
    sin BD; aqui solo queda la consulta. Se agrupa en Python y no en SQL con
    `width_bucket` a proposito: son como mucho unos miles de filas.
    """
    async with get_sessionmaker()() as s:
        res = await s.execute(
            select(FlowEventRow.lag_seconds)
            .where(FlowEventRow.lag_seconds <= max_seconds)
            .order_by(desc(FlowEventRow.ts)).limit(20000)
        )
        lags = [float(v) for (v,) in res.all()]
    return metrics.histogram(lags, buckets, max_seconds)


async def timeline(limit: int = 200) -> list[dict]:
    """Serie de pasadas, de vieja a nueva para que el panel la dibuje sin
    invertirla."""
    async with get_sessionmaker()() as s:
        res = await s.execute(
            select(FlowSnapshotRow).order_by(desc(FlowSnapshotRow.ts)).limit(limit))
        return [_row_to_dict(r) for r in reversed(res.scalars().all())]


async def prune(days: int, trade_days: int) -> dict:
    """Poda. Dos retenciones distintas y muy a proposito:

      - `trade_days` para la cinta cruda (`flow_trades`), que es lo unico que
        crece rapido: miles de filas por mercado activo.
      - `days`, mucho mas largo, para cascadas, eslabones y snapshots: son el
        conocimiento del motor. Podarlos al ritmo de la cinta destruiria la
        unica serie larga de propagacion que existe.

    Las metricas por wallet y por mercado NO se podan: son el estado actual y
    perderlas borraria su `first_seen`.
    """
    now = _utcnow()
    cutoff = now - dt.timedelta(days=days)
    trade_cutoff = now - dt.timedelta(days=trade_days)
    deleted: dict[str, int] = {}
    async with get_sessionmaker()() as s:
        async with s.begin():
            for name, table, col, limit_ts in (
                ("flow_trades", FlowTradeRow, FlowTradeRow.ts, trade_cutoff),
                ("flow_events", FlowEventRow, FlowEventRow.ts, cutoff),
                ("flow_cascades", FlowCascadeRow, FlowCascadeRow.started_at, cutoff),
                ("flow_snapshots", FlowSnapshotRow, FlowSnapshotRow.ts, cutoff),
            ):
                res = await s.execute(delete(table).where(col < limit_ts))
                deleted[name] = res.rowcount or 0
    return deleted


# ------------------------------------------------------- fuentes (lectura) ---
# Lectura de las tablas del sistema de trading. SOLO SELECT: el motor observa,
# no escribe una sola fila fuera de sus propias tablas.

async def target_markets(limit: int = 40) -> list[dict]:
    """Mercados sobre los que se mide la propagacion.

    Se usan los que Medusa ya vigila, no un universo arbitrario: eso ancla la
    investigacion a lo que el sistema observa de verdad y mantiene acotado el
    numero de peticiones a la API publica.
    """
    markets = await repo.list_ranked_markets(limit=limit)
    if not markets:
        # `list_ranked_markets` solo mira la ultima hora (es un ranking vivo).
        # Con el engine parado eso da vacio; para medir propagacion, un mercado
        # de ayer sigue siendo perfectamente valido.
        markets = await repo.list_markets(limit=limit)
    return markets


def entries_from_participants(participants: list[str]) -> list[Entry]:
    """Ayuda para reconstruir cascadas leidas de la BD en tests y utilidades.

    Los tiempos y precios reales viven en `flow_trades`; esta funcion solo
    devuelve el esqueleto de participantes, y por eso NO se usa para calcular
    ninguna metrica.
    """
    now = _utcnow()
    return [Entry(wallet=w, ts=now, price=0.0, size=0.0) for w in participants]
