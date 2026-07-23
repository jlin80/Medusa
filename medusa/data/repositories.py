"""Repositorios de persistencia (CRUD sobre los modelos ORM).

Cada funcion abre su propia sesion async y hace commit. Las lecturas devuelven
dicts (no objetos ORM) para no arrastrar el ciclo de vida de la sesion.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

from sqlalchemy import case, delete, desc, func, select

from medusa.config import get_settings
from medusa.core.models import Market, Opportunity, OrderResult
from medusa.data.db_models import (
    BotStateRow,
    EquityRow,
    EventLogRow,
    FeatureRow,
    FillRow,
    MarketRow,
    OpportunityRow,
    OrderRow,
    PositionRow,
    StrategySignalRow,
    TradeRow,
)
from medusa.infra.db import get_sessionmaker


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _row_to_dict(row: Any) -> dict:
    out: dict = {}
    for col in row.__table__.columns:
        val = getattr(row, col.name)
        if isinstance(val, dt.datetime):
            val = val.isoformat()
        out[col.name] = val
    return out


# --------------------------------------------------------------- markets ----
def _apply_market(row: MarketRow, m: Market) -> None:
    row.question = m.question
    row.slug = m.slug
    row.category = m.category
    row.yes_token_id = m.yes_token_id
    row.no_token_id = m.no_token_id
    row.end_date = m.end_date
    row.yes_price = m.yes_price
    row.no_price = m.no_price
    row.best_bid = m.best_bid
    row.best_ask = m.best_ask
    row.spread = m.spread
    row.volume_24h = m.volume_24h
    row.liquidity = m.liquidity
    row.active = m.active
    row.medusa_category = m.medusa_category
    row.opportunity_score = m.opportunity_score
    row.last_seen = _utcnow()


async def upsert_market(m: Market) -> None:
    async with get_sessionmaker()() as s:
        row = await s.get(MarketRow, m.id)
        if row is None:
            row = MarketRow(id=m.id, first_seen=_utcnow())
            s.add(row)
        _apply_market(row, m)
        await s.commit()


async def upsert_markets(markets: list[Market]) -> None:
    """Upsert masivo en UNA transaccion.

    El escaneo global persiste cientos de mercados de golpe; hacerlo con un
    commit por mercado castiga sin necesidad al Postgres del CT202.
    """
    if not markets:
        return
    async with get_sessionmaker()() as s:
        async with s.begin():
            ids = [m.id for m in markets]
            res = await s.execute(select(MarketRow).where(MarketRow.id.in_(ids)))
            existing = {row.id: row for row in res.scalars().all()}
            for m in markets:
                row = existing.get(m.id)
                if row is None:
                    row = MarketRow(id=m.id, first_seen=_utcnow())
                    s.add(row)
                _apply_market(row, m)


async def list_ranked_markets(limit: int = 50) -> list[dict]:
    """Ranking dinamico de oportunidades: mercados vivos por puntaje del
    pre-scorer. Solo cuenta lo visto en la ultima hora: un mercado que dejo de
    aparecer en el escaneo no es una oportunidad, es una fila vieja."""
    cutoff = _utcnow() - dt.timedelta(hours=1)
    async with get_sessionmaker()() as s:
        res = await s.execute(
            select(MarketRow)
            .where(MarketRow.active.is_(True), MarketRow.last_seen >= cutoff)
            .order_by(desc(MarketRow.opportunity_score))
            .limit(limit)
        )
        return [_row_to_dict(r) for r in res.scalars().all()]


async def category_stats() -> list[dict]:
    """Distribucion del universo escaneable por categoria (ultima hora)."""
    cutoff = _utcnow() - dt.timedelta(hours=1)
    async with get_sessionmaker()() as s:
        res = await s.execute(
            select(
                MarketRow.medusa_category,
                func.count().label("markets"),
                func.avg(MarketRow.opportunity_score).label("avg_score"),
                func.max(MarketRow.opportunity_score).label("max_score"),
                func.sum(MarketRow.volume_24h).label("volume_24h"),
                func.sum(MarketRow.liquidity).label("liquidity"),
            )
            .where(MarketRow.active.is_(True), MarketRow.last_seen >= cutoff)
            .group_by(MarketRow.medusa_category)
            .order_by(desc("markets"))
        )
        return [
            {
                "category": row.medusa_category or "other",
                "markets": row.markets,
                "avg_score": round(float(row.avg_score or 0), 2),
                "max_score": round(float(row.max_score or 0), 2),
                "volume_24h": round(float(row.volume_24h or 0), 2),
                "liquidity": round(float(row.liquidity or 0), 2),
            }
            for row in res.all()
        ]


async def list_markets(limit: int = 50) -> list[dict]:
    async with get_sessionmaker()() as s:
        res = await s.execute(select(MarketRow).order_by(desc(MarketRow.volume_24h)).limit(limit))
        return [_row_to_dict(r) for r in res.scalars().all()]


# ---------------------------------------------------------- opportunities ---
async def save_opportunity(opp: Opportunity, status: str = "detected") -> int:
    async with get_sessionmaker()() as s:
        row = OpportunityRow(
            market_id=opp.market_id,
            question=opp.question,
            outcome=opp.outcome,
            market_price=opp.market_price,
            market_prob=opp.market_prob,
            medusa_prob=opp.medusa_prob,
            edge=opp.edge,
            confidence=opp.confidence,
            action=opp.action,
            status=status,
            reason=opp.reason,
            strategy=opp.strategy,
            category=opp.category,
            score=opp.score,
        )
        s.add(row)
        await s.commit()
        return row.id


async def list_opportunities(limit: int = 50) -> list[dict]:
    async with get_sessionmaker()() as s:
        res = await s.execute(select(OpportunityRow).order_by(desc(OpportunityRow.created_at)).limit(limit))
        return [_row_to_dict(r) for r in res.scalars().all()]


# ----------------------------------------------------------- orders/fills ---
async def save_order(
    market_id: str, mode: str, outcome: str, side: str, price: float, size: float,
    status: str, opportunity_id: int | None = None, external_id: str = "",
) -> int:
    async with get_sessionmaker()() as s:
        row = OrderRow(
            market_id=market_id, mode=mode, outcome=outcome, side=side, price=price,
            size=size, status=status, opportunity_id=opportunity_id, external_id=external_id,
        )
        s.add(row)
        await s.commit()
        return row.id


# ------------------------------------------------- escrituras atomicas ------
# Una operacion toca varias tablas a la vez (orden + fills + posicion + balance).
# Si eso se hace en transacciones sueltas, un reinicio en medio deja el balance
# cobrado y la posicion sin registrar: dinero perdido en paper y, peor, una
# posicion REAL sin seguimiento en live. Por eso cada operacion se escribe en una
# unica transaccion: o entra todo, o no entra nada.

async def record_entry(
    *, market_id: str, question: str, mode: str, outcome: str, token_id: str,
    price: float, size: float, status: str, fills: list, cost_basis: float,
    entry_edge: float, entry_cost: float, balance_delta: float, entry_mid: float = 0.0,
    opportunity_id: int | None = None, external_id: str = "", strategy: str = "",
) -> int:
    """Registra orden + fills + posicion y ajusta el balance atomicamente."""
    async with get_sessionmaker()() as s:
        async with s.begin():
            order = OrderRow(
                market_id=market_id, mode=mode, outcome=outcome, side="buy",
                price=price, size=size, status=status,
                opportunity_id=opportunity_id, external_id=external_id,
            )
            s.add(order)
            await s.flush()
            for f in fills:
                s.add(FillRow(
                    order_id=order.id, market_id=market_id, price=f.price, size=f.size,
                    fee=f.fee, slippage_cost=f.slippage_cost, spread_cost=f.spread_cost,
                ))
            pos = PositionRow(
                market_id=market_id, question=question, mode=mode, outcome=outcome,
                avg_price=price, size=size, cost_basis=cost_basis, status="open",
                token_id=token_id, entry_edge=entry_edge, entry_cost=entry_cost,
                entry_mid=entry_mid, strategy=strategy,
            )
            s.add(pos)
            if balance_delta:
                st = await s.get(BotStateRow, 1)
                if st is not None:
                    st.balance = round(st.balance + balance_delta, 6)
            await s.flush()
            return pos.id


async def record_exit(
    *, position_id: int, mode: str, exit_price: float, proceeds: float, cost: float,
    reason: str, balance_delta: float, fills: list | None = None,
    order_status: str = "filled", filled_size: float = 0.0,
) -> dict | None:
    """Registra una salida (total o PARCIAL) y ajusta el balance, todo en una
    sola transaccion.

    `fills=None` significa liquidacion por resolucion: no hay orden de venta, el
    mercado paga 1 o 0 por share, y el cierre es siempre total.

    Si hubo venta y el fill fue parcial (filled_size < size de la posicion), la
    posicion NO se cierra: se reduce proporcionalmente (size, cost_basis,
    entry_cost) y el trade registrado corresponde solo a la parte vendida.
    Cerrarla entera con proceeds parciales -- el bug que esto arregla -- hacia
    desaparecer las shares no vendidas de la contabilidad: en paper ensucia el
    PnL y en live dejaria una posicion REAL sin seguimiento.
    """
    async with get_sessionmaker()() as s:
        async with s.begin():
            pos = await s.get(PositionRow, position_id)
            if pos is None or pos.status != "open":
                return None

            if fills:
                order = OrderRow(
                    market_id=pos.market_id, mode=mode, outcome=pos.outcome, side="sell",
                    price=exit_price, size=filled_size, status=order_status,
                )
                s.add(order)
                await s.flush()
                for f in fills:
                    s.add(FillRow(
                        order_id=order.id, market_id=pos.market_id, price=f.price,
                        size=f.size, fee=f.fee, slippage_cost=f.slippage_cost,
                        spread_cost=f.spread_cost,
                    ))

            # Cierre total salvo venta con fill parcial. El 0.999 absorbe el
            # ruido de coma flotante de restar fills nivel a nivel.
            partial = (
                fills is not None
                and 0.0 < filled_size < pos.size * 0.999
            )

            if partial:
                fraction = filled_size / pos.size
                sold_basis = pos.cost_basis * fraction
                sold_entry_cost = pos.entry_cost * fraction
                pnl = proceeds - sold_basis
                roi = (pnl / sold_basis) if sold_basis else 0.0
                trade_size = filled_size

                pos.size = round(pos.size - filled_size, 6)
                pos.cost_basis = round(pos.cost_basis - sold_basis, 6)
                pos.entry_cost = round(pos.entry_cost - sold_entry_cost, 6)
                pos.realized_pnl = round(pos.realized_pnl + pnl, 6)
                trade_cost = sold_entry_cost + cost
            else:
                pnl = proceeds - pos.cost_basis
                roi = (pnl / pos.cost_basis) if pos.cost_basis else 0.0
                trade_size = pos.size
                trade_cost = pos.entry_cost + cost
                pos.status = "closed"
                pos.closed_at = _utcnow()
                pos.realized_pnl = pnl
                pos.unrealized_pnl = 0.0

            s.add(TradeRow(
                market_id=pos.market_id, question=pos.question, mode=pos.mode,
                outcome=pos.outcome, entry_price=pos.avg_price, exit_price=exit_price,
                size=trade_size, cost=trade_cost, pnl=pnl, roi=roi,
                edge=pos.entry_edge, won=(pnl > 0), strategy=pos.strategy,
                opened_at=pos.opened_at, closed_at=_utcnow(),
            ))
            if balance_delta:
                st = await s.get(BotStateRow, 1)
                if st is not None:
                    st.balance = round(st.balance + balance_delta, 6)

            return {"pnl": pnl, "roi": roi, "won": pnl > 0, "question": pos.question,
                    "outcome": pos.outcome, "market_id": pos.market_id,
                    "entry_price": pos.avg_price, "exit_price": exit_price,
                    "size": trade_size, "reason": reason,
                    "partial": partial, "remaining": (pos.size if partial else 0.0)}


# --------------------------------------------------------------- positions --
async def get_open_positions(mode: str) -> list[dict]:
    async with get_sessionmaker()() as s:
        res = await s.execute(
            select(PositionRow).where(PositionRow.status == "open", PositionRow.mode == mode)
        )
        return [_row_to_dict(r) for r in res.scalars().all()]


async def mark_position(position_id: int, unrealized_pnl: float) -> None:
    async with get_sessionmaker()() as s:
        row = await s.get(PositionRow, position_id)
        if row:
            row.unrealized_pnl = unrealized_pnl
            await s.commit()


async def list_trades(limit: int = 100, mode: str | None = None) -> list[dict]:
    async with get_sessionmaker()() as s:
        q = select(TradeRow).order_by(desc(TradeRow.closed_at)).limit(limit)
        if mode:
            q = select(TradeRow).where(TradeRow.mode == mode).order_by(desc(TradeRow.closed_at)).limit(limit)
        res = await s.execute(q)
        return [_row_to_dict(r) for r in res.scalars().all()]


# ----------------------------------------------------------------- equity ---
async def save_equity(
    mode: str, balance: float, equity: float, exposure: float,
    open_positions: int, realized_pnl: float, unrealized_pnl: float,
) -> None:
    async with get_sessionmaker()() as s:
        s.add(EquityRow(
            mode=mode, balance=balance, equity=equity, exposure=exposure,
            open_positions=open_positions, realized_pnl=realized_pnl, unrealized_pnl=unrealized_pnl,
        ))
        await s.commit()


async def equity_curve(limit: int = 500, mode: str | None = None) -> list[dict]:
    async with get_sessionmaker()() as s:
        q = select(EquityRow).order_by(desc(EquityRow.ts)).limit(limit)
        if mode:
            q = select(EquityRow).where(EquityRow.mode == mode).order_by(desc(EquityRow.ts)).limit(limit)
        res = await s.execute(q)
        rows = [_row_to_dict(r) for r in res.scalars().all()]
        return list(reversed(rows))


# --------------------------------------------------------------- bot_state --
async def get_bot_state() -> dict | None:
    async with get_sessionmaker()() as s:
        row = await s.get(BotStateRow, 1)
        return _row_to_dict(row) if row else None


async def ensure_bot_state(mode: str, balance: float) -> dict:
    """Crea la fila unica de estado la primera vez. Idempotente."""
    async with get_sessionmaker()() as s:
        row = await s.get(BotStateRow, 1)
        if row is None:
            row = BotStateRow(
                id=1, mode=mode, balance=balance, starting_balance=balance,
                paper_start_at=_utcnow(),
            )
            s.add(row)
            await s.commit()
        return _row_to_dict(row)


async def update_bot_state(**kwargs: Any) -> None:
    async with get_sessionmaker()() as s:
        row = await s.get(BotStateRow, 1)
        if row is None:
            row = BotStateRow(id=1)
            s.add(row)
        for key, val in kwargs.items():
            if hasattr(row, key):
                setattr(row, key, val)
        await s.commit()


# ------------------------------------------------------- strategy signals ---
# Las señales en shadow son el mecanismo con el que Medusa DESCUBRE donde hay
# edge: cada señal se registra al dispararse y se marca ganada/perdida cuando
# el mercado resuelve. El agregado por (estrategia, categoria) alimenta el
# Capital Allocation Manager.

async def open_signal_keys() -> set[tuple[str, str, str]]:
    """Claves (strategy, market_id, outcome) de señales abiertas, para dedup.

    Mientras las condiciones de una señal persistan, cada ciclo la re-emitiria;
    sin dedup la tabla contaria el mismo juicio cientos de veces y el win rate
    resultante seria basura estadistica.
    """
    async with get_sessionmaker()() as s:
        res = await s.execute(
            select(
                StrategySignalRow.strategy,
                StrategySignalRow.market_id,
                StrategySignalRow.outcome,
            ).where(StrategySignalRow.status == "open")
        )
        return {(r.strategy, r.market_id, r.outcome) for r in res.all()}


async def save_signals(signals: list) -> int:
    """Inserta señales nuevas (StrategySignal) en una transaccion."""
    if not signals:
        return 0
    async with get_sessionmaker()() as s:
        async with s.begin():
            for sig in signals:
                s.add(StrategySignalRow(
                    strategy=sig.strategy, market_id=sig.market_id,
                    category=sig.category, question=sig.question,
                    outcome=sig.outcome, token_id=sig.token_id,
                    entry_price=sig.entry_price,
                    # Doble cota: el ask (taker) y el bid (maker). Guardar solo
                    # una es como se fabrica una rentabilidad que no existe.
                    entry_price_maker=(sig.entry_price_maker or None),
                    # El contexto de coste NO es opcional: sin el, el histórico
                    # no puede decir si la señal era operable. Ver la nota en
                    # db_models.StrategySignalRow.
                    spread=sig.spread, liquidity=sig.liquidity,
                    signal_prob=sig.signal_prob,
                    market_prob=sig.market_prob, edge=sig.edge,
                    confidence=sig.confidence, score=sig.score,
                    explanation=sig.explanation,
                    valid_conditions=sig.valid_conditions,
                    end_date=sig.end_date,
                ))
    return len(signals)


async def mark_signal_traded(strategy: str, market_id: str, outcome: str) -> None:
    async with get_sessionmaker()() as s:
        res = await s.execute(
            select(StrategySignalRow).where(
                StrategySignalRow.strategy == strategy,
                StrategySignalRow.market_id == market_id,
                StrategySignalRow.outcome == outcome,
                StrategySignalRow.status == "open",
            )
        )
        for row in res.scalars().all():
            row.traded = True
        await s.commit()


async def markets_with_pending_signals(batch: int = 40) -> list[str]:
    """market_ids con señales abiertas cuyo end_date ya paso (+1h de margen
    para que Gamma marque la resolucion). Limitado para no castigar la API."""
    cutoff = _utcnow() - dt.timedelta(hours=1)
    async with get_sessionmaker()() as s:
        res = await s.execute(
            select(StrategySignalRow.market_id)
            .where(
                StrategySignalRow.status == "open",
                StrategySignalRow.end_date.is_not(None),
                StrategySignalRow.end_date < cutoff,
            )
            .distinct()
            .limit(batch)
        )
        return [r[0] for r in res.all()]


async def resolve_signals_for_market(market_id: str, settle_yes: float) -> int:
    """Resuelve todas las señales abiertas de un mercado ya resuelto.

    settle_yes es el precio final del YES (1.0 o 0.0). El PnL teorico se mide
    contra el precio de entrada REAL registrado (ask al disparar): es lo que
    habria pasado de verdad operando la señal, no una fantasia a mid.

    stat_arb se especial-casea: su ganancia quedo bloqueada al entrar
    (edge = ganancia del par) y no depende del resultado del mercado.
    """
    resolved = 0
    async with get_sessionmaker()() as s:
        async with s.begin():
            res = await s.execute(
                select(StrategySignalRow).where(
                    StrategySignalRow.market_id == market_id,
                    StrategySignalRow.status == "open",
                )
            )
            for row in res.scalars().all():
                if row.strategy == "stat_arb":
                    pnl = row.edge
                    won = True
                    settle = row.entry_price + row.edge
                else:
                    settle = settle_yes if row.outcome == "YES" else 1.0 - settle_yes
                    pnl = settle - row.entry_price
                    won = pnl > 0
                row.status = "resolved"
                row.resolved_at = _utcnow()
                row.won = won
                row.settle_price = round(settle, 4)
                # --- cota TAKER (realista): crucé el spread, pagué el ask ---
                row.pnl_per_share = round(pnl, 4)
                row.roi = round(pnl / row.entry_price, 4) if row.entry_price > 0 else 0.0
                # --- cota MAKER (techo): me llenaron al bid, sin cruzar ---
                # Mantener a resolucion solo paga el spread UNA vez (a la
                # entrada): la resolucion liquida a 1/0 sin libro ni coste. Por
                # eso el gap maker-taker de una señal es exactamente
                # (ask - bid), el peaje de entrar.
                # stat_arb se excluye: su "entrada" es el precio del PAR, no un
                # lado con bid/ask, y su ganancia queda bloqueada al entrar.
                if row.strategy != "stat_arb" and row.entry_price_maker:
                    pnl_mk = settle - row.entry_price_maker
                    row.pnl_maker = round(pnl_mk, 4)
                    row.roi_maker = round(pnl_mk / row.entry_price_maker, 4)
                resolved += 1
    return resolved


async def void_stale_signals(days: int = 30) -> int:
    """Señales abiertas con end_date pasado hace `days` y sin resolucion
    localizable: se marcan void para que dejen de consultar la API y no
    contaminen las estadisticas (ni como ganadas ni como perdidas)."""
    cutoff = _utcnow() - dt.timedelta(days=days)
    async with get_sessionmaker()() as s:
        res = await s.execute(
            select(StrategySignalRow).where(
                StrategySignalRow.status == "open",
                StrategySignalRow.end_date.is_not(None),
                StrategySignalRow.end_date < cutoff,
            )
        )
        rows = res.scalars().all()
        for row in rows:
            row.status = "void"
            row.resolved_at = _utcnow()
        await s.commit()
        return len(rows)


async def list_signals(
    limit: int = 100, strategy: str | None = None, status: str | None = None,
) -> list[dict]:
    async with get_sessionmaker()() as s:
        q = select(StrategySignalRow)
        if strategy:
            q = q.where(StrategySignalRow.strategy == strategy)
        if status:
            q = q.where(StrategySignalRow.status == status)
        q = q.order_by(desc(StrategySignalRow.ts)).limit(limit)
        res = await s.execute(q)
        return [_row_to_dict(r) for r in res.scalars().all()]


async def strategy_performance(tradeable_only: bool = True) -> list[dict]:
    """Agregado de señales RESUELTAS por (estrategia, categoria).

    Es la materia prima del Capital Allocation Manager: n, aciertos, ROI medio
    y PnL teorico acumulado por share. La significancia (Wilson) se calcula en
    la capa de asignacion, no aqui.

    tradeable_only=True (default): SOLO cuenta señales que el Risk Manager
    habria aprobado, es decir con `edge > spread * EDGE_COST_RATIO`. Sin este
    filtro el asignador podria dar peso -- y ascender una estrategia a paper --
    por un ROI generado en mercados de spread imposible (medido: hasta 27% en
    precios extremos) que nunca se habrian operado. El objetivo es edge DESPUES
    de costes; contar señales incobrables es engañarse con un numero bonito.

    Las señales sin `spread` registrado (anteriores al 2026-07-16) quedan FUERA:
    no se puede afirmar que fueran operables, y ante la duda no cuentan.
    """
    s_cfg = get_settings()
    async with get_sessionmaker()() as s:
        q = (
            select(
                StrategySignalRow.strategy,
                StrategySignalRow.category,
                func.count().label("n"),
                func.sum(case((StrategySignalRow.won.is_(True), 1), else_=0)).label("wins"),
                func.avg(StrategySignalRow.roi).label("avg_roi"),
                # La dispersion del ROI es lo que permite castigar por
                # incertidumbre (lb = media - z*std/sqrt(n)) en la asignacion.
                func.stddev_samp(StrategySignalRow.roi).label("std_roi"),
                func.sum(StrategySignalRow.pnl_per_share).label("pnl_total"),
                func.avg(StrategySignalRow.edge).label("avg_edge"),
                # Cota MAKER, solo para el contraste. La distancia contra
                # avg_roi ES el peaje del spread: es el numero que separa "se
                # veia rentable" de "gana dinero".
                func.avg(StrategySignalRow.roi_maker).label("avg_roi_maker"),
                func.max(StrategySignalRow.resolved_at).label("last_resolved"),
            )
            .where(StrategySignalRow.status == "resolved")
        )
        if tradeable_only:
            # Mismo criterio que RiskManager._edge_beats_cost: el edge debe
            # superar el coste de operar por EDGE_COST_RATIO. Aqui solo se
            # dispone del spread (el slippage/fees son de segundo orden frente
            # a spreads del 2-27%), asi que es una cota INFERIOR del coste: si
            # una señal no pasa ni contra el spread solo, jamas habria pasado
            # el filtro completo.
            q = q.where(
                StrategySignalRow.spread.is_not(None),
                StrategySignalRow.edge > StrategySignalRow.spread * s_cfg.edge_cost_ratio,
            )
        q = q.group_by(StrategySignalRow.strategy, StrategySignalRow.category)
        res = await s.execute(q)
        out = []
        for row in res.all():
            out.append({
                "strategy": row.strategy,
                "category": row.category or "other",
                "n": int(row.n or 0),
                "wins": int(row.wins or 0),
                # avg_roi es la cota TAKER: la realista, la que decide.
                "avg_roi": round(float(row.avg_roi or 0), 4),
                "std_roi": round(float(row.std_roi or 0), 4),
                "pnl_total": round(float(row.pnl_total or 0), 4),
                "avg_edge": round(float(row.avg_edge or 0), 4),
                # Techo optimista y peaje: si avg_roi_maker es bonito y avg_roi
                # es negativo, la estrategia NO tiene negocio -- el spread se lo
                # come. Ese contraste es justo lo que Hermes no puede enseñar.
                "avg_roi_maker": round(float(row.avg_roi_maker), 4)
                                 if row.avg_roi_maker is not None else None,
                "spread_toll": round(float(row.avg_roi_maker) - float(row.avg_roi or 0), 4)
                               if row.avg_roi_maker is not None else None,
                "last_resolved": row.last_resolved.isoformat() if row.last_resolved else None,
            })
        return out


async def signal_counts() -> dict:
    """Conteo de señales por estado (telemetria barata para /strategies)."""
    async with get_sessionmaker()() as s:
        res = await s.execute(
            select(
                StrategySignalRow.strategy,
                StrategySignalRow.status,
                func.count().label("n"),
            ).group_by(StrategySignalRow.strategy, StrategySignalRow.status)
        )
        out: dict[str, dict[str, int]] = {}
        for row in res.all():
            out.setdefault(row.strategy, {})[row.status] = int(row.n)
        return out


# ---------------------------------------------------------- feature store ---
# El Feature Store es el "histórico propio" del sistema: append-only, nunca
# sobreescribe, y es de donde saldrán los modelos el día que haya datos. Las
# estrategias lo consultan ANTES que a ninguna API externa.

async def save_features(features: list) -> int:
    """Inserta features (objetos Feature del Intelligence Layer) en UNA transaccion."""
    if not features:
        return 0
    async with get_sessionmaker()() as s:
        async with s.begin():
            for f in features:
                s.add(FeatureRow(
                    market_id=f.market_id, name=f.name, value=float(f.value),
                    module=f.module,
                    meta=json.dumps(f.meta, default=str) if f.meta else "",
                    ts=f.ts or _utcnow(),
                ))
    return len(features)


async def latest_features(market_ids: list[str]) -> dict[str, dict[str, float]]:
    """{market_id: {name: value}} con la ULTIMA lectura de cada feature.

    DISTINCT ON es específico de Postgres y es justo lo que hace falta: una
    pasada por el índice (market_id, name, ts DESC) en vez de un GROUP BY con
    subconsulta. En la CPU del CT202 esa diferencia importa.
    """
    if not market_ids:
        return {}
    async with get_sessionmaker()() as s:
        res = await s.execute(
            select(FeatureRow.market_id, FeatureRow.name, FeatureRow.value)
            .where(FeatureRow.market_id.in_(market_ids))
            .distinct(FeatureRow.market_id, FeatureRow.name)
            .order_by(FeatureRow.market_id, FeatureRow.name, desc(FeatureRow.ts))
        )
        out: dict[str, dict[str, float]] = {}
        for row in res.all():
            out.setdefault(row.market_id, {})[row.name] = row.value
        return out


async def list_features(
    limit: int = 100, market_id: str | None = None, name: str | None = None,
    module: str | None = None,
) -> list[dict]:
    async with get_sessionmaker()() as s:
        q = select(FeatureRow)
        if market_id:
            q = q.where(FeatureRow.market_id == market_id)
        if name:
            q = q.where(FeatureRow.name == name)
        if module:
            q = q.where(FeatureRow.module == module)
        res = await s.execute(q.order_by(desc(FeatureRow.ts)).limit(limit))
        return [_row_to_dict(r) for r in res.scalars().all()]


async def feature_stats() -> list[dict]:
    """Telemetría del store: qué produce cada módulo y cuán fresco está."""
    async with get_sessionmaker()() as s:
        res = await s.execute(
            select(
                FeatureRow.module,
                FeatureRow.name,
                func.count().label("n"),
                func.count(func.distinct(FeatureRow.market_id)).label("markets"),
                func.avg(FeatureRow.value).label("avg_value"),
                func.max(FeatureRow.ts).label("last_ts"),
            ).group_by(FeatureRow.module, FeatureRow.name).order_by(FeatureRow.module, FeatureRow.name)
        )
        return [
            {
                "module": row.module or "?",
                "name": row.name,
                "n": int(row.n or 0),
                "markets": int(row.markets or 0),
                "avg_value": round(float(row.avg_value or 0), 4),
                "last_ts": row.last_ts.isoformat() if row.last_ts else None,
            }
            for row in res.all()
        ]


async def prune_features(days: int) -> int:
    """Poda del Feature Store. Separada de prune_old_data y con default MUY
    largo: este histórico es el activo del Intelligence Layer, no ruido."""
    cutoff = _utcnow() - dt.timedelta(days=days)
    async with get_sessionmaker()() as s:
        async with s.begin():
            res = await s.execute(delete(FeatureRow).where(FeatureRow.ts < cutoff))
            return res.rowcount or 0


# ----------------------------------------------------------------- events ---
async def log_event(level: str, source: str, message: str, payload: dict | None = None) -> None:
    async with get_sessionmaker()() as s:
        s.add(EventLogRow(
            level=level, source=source, message=message,
            payload=json.dumps(payload, default=str) if payload else "",
        ))
        await s.commit()


async def prune_old_data(days: int = 30) -> dict:
    """Borra datos de alta frecuencia mas viejos que `days`.

    Un run desatendido escribe ~9k oportunidades y ~1.4k snapshots de equity al
    dia. No es un problema de espacio a corto plazo, pero sin poda las consultas
    del dashboard se degradan sin techo. Trades, posiciones y mercados NO se
    tocan: son el registro del que sale el reporte de validacion.
    """
    cutoff = _utcnow() - dt.timedelta(days=days)
    deleted: dict[str, int] = {}
    async with get_sessionmaker()() as s:
        async with s.begin():
            for name, table, col in (
                ("opportunities", OpportunityRow, OpportunityRow.created_at),
                ("event_logs", EventLogRow, EventLogRow.ts),
                ("equity_snapshots", EquityRow, EquityRow.ts),
            ):
                res = await s.execute(delete(table).where(col < cutoff))
                deleted[name] = res.rowcount or 0
    return deleted


async def list_events(limit: int = 200, level: str | None = None) -> list[dict]:
    async with get_sessionmaker()() as s:
        q = select(EventLogRow).order_by(desc(EventLogRow.ts)).limit(limit)
        if level:
            q = select(EventLogRow).where(EventLogRow.level == level).order_by(desc(EventLogRow.ts)).limit(limit)
        res = await s.execute(q)
        return [_row_to_dict(r) for r in res.scalars().all()]


# ------------------------------------------------------ up/down (5 min) ------
# El micro-trader de 'Up or Down' (medusa.updown) escribe sus apuestas en el
# ledger REAL de paper (positions/trades) via record_entry/record_exit, para que
# cuenten en el balance, el equity, el winrate y el resto de metricas igual que
# cualquier otra operacion. Aqui solo viven el dedup y el toggle por mercado.

async def has_open_updown_bet(market_id: str) -> bool:
    """¿Hay ya una posicion ABIERTA para esta ventana? Dedup: se apuesta como
    mucho una vez por ventana (el condition_id identifica la ventana)."""
    async with get_sessionmaker()() as s:
        res = await s.execute(
            select(func.count()).select_from(PositionRow).where(
                PositionRow.market_id == market_id,
                PositionRow.status == "open",
            )
        )
        return (res.scalar() or 0) > 0


async def updown_risk_data() -> dict:
    """Foto del estado para el risk manager del micro-trader Up/Down.

    Todo lo que necesita assess() en una sola pasada: balance y balance de
    apertura del dia (de bot_state), apuestas updown abiertas (nº + exposicion),
    trades updown cerrados HOY (nº + PnL) y la racha de perdidas mas reciente.
    """
    now = _utcnow()
    start_of_day = dt.datetime(now.year, now.month, now.day, tzinfo=dt.timezone.utc)
    async with get_sessionmaker()() as s:
        st = await s.get(BotStateRow, 1)
        balance = float(st.balance) if st else 0.0
        day_start = float(st.day_start_equity) if st and st.day_start_equity else balance

        res = await s.execute(
            select(PositionRow).where(
                PositionRow.status == "open",
                PositionRow.strategy.like("updown%"),
            )
        )
        opens = res.scalars().all()

        res_today = await s.execute(
            select(TradeRow).where(
                TradeRow.strategy.like("updown%"),
                TradeRow.closed_at >= start_of_day,
            )
        )
        todays = res_today.scalars().all()

        # Para la racha: los ultimos trades updown, mas reciente primero.
        res_recent = await s.execute(
            select(TradeRow)
            .where(TradeRow.strategy.like("updown%"))
            .order_by(desc(TradeRow.closed_at))
            .limit(20)
        )
        recent = res_recent.scalars().all()

    open_count = len(opens)
    open_exposure = sum(p.cost_basis for p in opens)
    today_pnl = sum(t.pnl for t in todays)
    consecutive = 0
    last_loss_ts = None
    for t in recent:
        if t.won:
            break
        consecutive += 1
        if last_loss_ts is None:
            last_loss_ts = t.closed_at

    return {
        "balance": round(balance, 2),
        "day_start_equity": round(day_start, 2),
        "open_count": open_count,
        "open_exposure": round(open_exposure, 2),
        # Apuestas colocadas hoy (~ cerradas hoy + abiertas ahora; las updown
        # resuelven en 5 min, asi que una abierta es de hoy).
        "today_bets": len(todays) + open_count,
        "today_pnl": round(today_pnl, 2),
        "consecutive_losses": consecutive,
        "last_loss_ts": last_loss_ts.isoformat() if last_loss_ts else None,
        "last_loss_epoch": last_loss_ts.timestamp() if last_loss_ts else 0.0,
    }


async def get_updown_assets(default_csv: str) -> list[str]:
    """Mercados Up/Down activos. Si el usuario nunca tocó el toggle (columna
    NULL), se usa el default de la config; si lo tocó (incluido dejarlo vacio),
    manda su eleccion."""
    async with get_sessionmaker()() as s:
        row = await s.get(BotStateRow, 1)
        raw = row.updown_assets if row is not None else None
    if raw is None:
        raw = default_csv
    return [a.strip().lower() for a in raw.split(",") if a.strip()]


async def set_updown_assets(csv: str) -> None:
    async with get_sessionmaker()() as s:
        row = await s.get(BotStateRow, 1)
        if row is not None:
            row.updown_assets = csv
            await s.commit()


# ----------------------------------------------- control de cuenta (paper) ---
async def set_paper_balance(balance: float) -> None:
    """Fija el balance y el balance inicial (la referencia del ROI) al mismo
    valor. Solo tiene sentido estando plano; se documenta en la API."""
    async with get_sessionmaker()() as s:
        row = await s.get(BotStateRow, 1)
        if row is None:
            return
        row.balance = round(balance, 6)
        row.starting_balance = round(balance, 6)
        row.day_start_equity = round(balance, 6)
        row.day_start_date = _utcnow().date().isoformat()
        await s.commit()


async def wipe_paper(starting_balance: float) -> dict:
    """Borra TODO el historial de trading de paper y reinicia la cuenta a cero.

    Se van: posiciones, trades, oportunidades, ordenes, fills y snapshots de
    equity del modo paper. El balance vuelve a `starting_balance`, el reloj de
    validacion (paper_start_at) se reinicia a ahora y se limpia el kill-switch.
    NO se toca: live_unlocked ni las reglas del gate (14 dias / 30 ops / ROI>0),
    ni el historial de señales shadow (el aprendizaje del sistema).
    """
    deleted: dict[str, int] = {}
    async with get_sessionmaker()() as s:
        async with s.begin():
            for name, table, cond in (
                ("positions", PositionRow, PositionRow.mode == "paper"),
                ("trades", TradeRow, TradeRow.mode == "paper"),
                ("orders", OrderRow, OrderRow.mode == "paper"),
                ("equity_snapshots", EquityRow, EquityRow.mode == "paper"),
                ("opportunities", OpportunityRow, None),
                ("fills", FillRow, None),
            ):
                stmt = delete(table) if cond is None else delete(table).where(cond)
                res = await s.execute(stmt)
                deleted[name] = res.rowcount or 0
            st = await s.get(BotStateRow, 1)
            if st is not None:
                st.balance = round(starting_balance, 6)
                st.starting_balance = round(starting_balance, 6)
                st.paper_start_at = _utcnow()
                st.day_start_equity = round(starting_balance, 6)
                st.day_start_date = _utcnow().date().isoformat()
                st.kill_switch = False
                st.kill_switch_auto = False
    return deleted
