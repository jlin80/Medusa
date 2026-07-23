"""Modelos ORM (SQLAlchemy 2.0) - persistencia de Medusa.

Esquema: markets, opportunities, orders, fills, positions, trades,
equity_snapshots, bot_state, event_logs, strategy_signals. Se crean con
init_db() (create_all + migracion ligera de columnas nuevas).
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class Base(DeclarativeBase):
    pass


class MarketRow(Base):
    __tablename__ = "markets"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)  # condition_id
    question: Mapped[str] = mapped_column(Text, default="")
    slug: Mapped[str] = mapped_column(String(256), default="")
    category: Mapped[str] = mapped_column(String(128), default="")
    yes_token_id: Mapped[str] = mapped_column(String(128), default="")
    no_token_id: Mapped[str] = mapped_column(String(128), default="")
    end_date: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    yes_price: Mapped[float] = mapped_column(Float, default=0.0)
    no_price: Mapped[float] = mapped_column(Float, default=0.0)
    best_bid: Mapped[float] = mapped_column(Float, default=0.0)
    best_ask: Mapped[float] = mapped_column(Float, default=0.0)
    spread: Mapped[float] = mapped_column(Float, default=0.0)
    volume_24h: Mapped[float] = mapped_column(Float, default=0.0)
    liquidity: Mapped[float] = mapped_column(Float, default=0.0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Capa de inteligencia: categoria de la taxonomia de Medusa (no la etiqueta
    # de Gamma, que vive en `category`) y puntaje del pre-scorer (0-100).
    medusa_category: Mapped[str] = mapped_column(String(32), default="", index=True)
    opportunity_score: Mapped[float] = mapped_column(Float, default=0.0)
    first_seen: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    last_seen: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class OpportunityRow(Base):
    __tablename__ = "opportunities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    market_id: Mapped[str] = mapped_column(String(128), index=True)
    question: Mapped[str] = mapped_column(Text, default="")
    outcome: Mapped[str] = mapped_column(String(8), default="YES")   # YES | NO
    market_price: Mapped[float] = mapped_column(Float, default=0.0)
    market_prob: Mapped[float] = mapped_column(Float, default=0.0)
    medusa_prob: Mapped[float] = mapped_column(Float, default=0.0)
    edge: Mapped[float] = mapped_column(Float, default=0.0)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    action: Mapped[str] = mapped_column(String(16), default="skip")
    status: Mapped[str] = mapped_column(String(16), default="detected")
    reason: Mapped[str] = mapped_column(Text, default="")
    # Atribucion multi-estrategia: sin esto no hay rendimiento por estrategia.
    strategy: Mapped[str] = mapped_column(String(64), default="")
    category: Mapped[str] = mapped_column(String(32), default="")
    score: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)


class OrderRow(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    opportunity_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    market_id: Mapped[str] = mapped_column(String(128), index=True)
    mode: Mapped[str] = mapped_column(String(8), default="paper")
    outcome: Mapped[str] = mapped_column(String(8), default="YES")
    side: Mapped[str] = mapped_column(String(8), default="buy")
    price: Mapped[float] = mapped_column(Float, default=0.0)
    size: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(16), default="new")
    external_id: Mapped[str] = mapped_column(String(128), default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)


class FillRow(Base):
    __tablename__ = "fills"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(Integer, index=True)
    market_id: Mapped[str] = mapped_column(String(128))
    price: Mapped[float] = mapped_column(Float, default=0.0)
    size: Mapped[float] = mapped_column(Float, default=0.0)
    fee: Mapped[float] = mapped_column(Float, default=0.0)
    slippage_cost: Mapped[float] = mapped_column(Float, default=0.0)
    spread_cost: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class PositionRow(Base):
    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    market_id: Mapped[str] = mapped_column(String(128), index=True)
    question: Mapped[str] = mapped_column(Text, default="")
    mode: Mapped[str] = mapped_column(String(8), default="paper")
    outcome: Mapped[str] = mapped_column(String(8), default="YES")
    token_id: Mapped[str] = mapped_column(String(128), default="")
    avg_price: Mapped[float] = mapped_column(Float, default=0.0)
    size: Mapped[float] = mapped_column(Float, default=0.0)
    cost_basis: Mapped[float] = mapped_column(Float, default=0.0)
    entry_edge: Mapped[float] = mapped_column(Float, default=0.0)   # edge estimado al entrar
    entry_cost: Mapped[float] = mapped_column(Float, default=0.0)   # fees+slippage+spread de entrada
    # Mid del libro al entrar. Es la referencia para TP/SL: un stop debe medir
    # que el MERCADO se movio en contra, no que acabamos de pagar el spread.
    entry_mid: Mapped[float] = mapped_column(Float, default=0.0)
    # Estrategia que origino la posicion (atribucion de rendimiento).
    strategy: Mapped[str] = mapped_column(String(64), default="")
    realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    unrealized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(16), default="open", index=True)
    opened_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    closed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class TradeRow(Base):
    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    market_id: Mapped[str] = mapped_column(String(128), index=True)
    question: Mapped[str] = mapped_column(Text, default="")
    mode: Mapped[str] = mapped_column(String(8), default="paper", index=True)
    outcome: Mapped[str] = mapped_column(String(8), default="YES")
    entry_price: Mapped[float] = mapped_column(Float, default=0.0)
    exit_price: Mapped[float] = mapped_column(Float, default=0.0)
    size: Mapped[float] = mapped_column(Float, default=0.0)
    cost: Mapped[float] = mapped_column(Float, default=0.0)      # costos totales (fees+spread+slippage)
    pnl: Mapped[float] = mapped_column(Float, default=0.0)
    roi: Mapped[float] = mapped_column(Float, default=0.0)
    edge: Mapped[float] = mapped_column(Float, default=0.0)
    won: Mapped[bool] = mapped_column(Boolean, default=False)
    # Estrategia que origino el trade (atribucion de rendimiento).
    strategy: Mapped[str] = mapped_column(String(64), default="")
    opened_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    closed_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)


class EquityRow(Base):
    __tablename__ = "equity_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)
    mode: Mapped[str] = mapped_column(String(8), default="paper")
    balance: Mapped[float] = mapped_column(Float, default=0.0)
    equity: Mapped[float] = mapped_column(Float, default=0.0)
    exposure: Mapped[float] = mapped_column(Float, default=0.0)
    open_positions: Mapped[int] = mapped_column(Integer, default=0)
    realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    unrealized_pnl: Mapped[float] = mapped_column(Float, default=0.0)


class BotStateRow(Base):
    __tablename__ = "bot_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    mode: Mapped[str] = mapped_column(String(8), default="paper")
    kill_switch: Mapped[bool] = mapped_column(Boolean, default=False)
    # True si el kill-switch lo puso el propio bot por perdida diaria. Solo estos
    # se levantan solos al cambiar el dia: uno puesto a mano por el usuario NO se
    # reanuda nunca por su cuenta.
    kill_switch_auto: Mapped[bool] = mapped_column(Boolean, default=False)
    day_start_equity: Mapped[float] = mapped_column(Float, default=0.0)
    day_start_date: Mapped[str] = mapped_column(String(10), default="")  # YYYY-MM-DD UTC
    live_unlocked: Mapped[bool] = mapped_column(Boolean, default=False)
    paper_start_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    balance: Mapped[float] = mapped_column(Float, default=0.0)
    starting_balance: Mapped[float] = mapped_column(Float, default=0.0)
    # Mercados Up/Down activos (CSV de activos: "bnb,eth"). NULL = nunca se tocó
    # el toggle => se usa el default de la config (UPDOWN_ASSETS). "" = todos
    # apagados a mano. Esta distincion es a proposito: un default 0/"" no debe
    # confundirse con una eleccion explicita del usuario.
    updown_assets: Mapped[str | None] = mapped_column(String(128), nullable=True)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


class StrategySignalRow(Base):
    """Señal emitida por una estrategia, se opere o no (modo shadow).

    Es la memoria con la que Medusa DESCUBRE que estrategias funcionan y en que
    categorias: cuando el mercado resuelve, la señal se marca ganada/perdida
    con el PnL teorico de haberla operado al precio de entrada real (ask). Ese
    historial alimenta el Capital Allocation Manager. NO se poda: es el
    registro de aprendizaje del sistema (y pesa poco: una fila por señal, con
    dedup de señales abiertas repetidas).
    """

    __tablename__ = "strategy_signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)
    strategy: Mapped[str] = mapped_column(String(64), index=True)
    market_id: Mapped[str] = mapped_column(String(128), index=True)
    category: Mapped[str] = mapped_column(String(32), default="", index=True)
    question: Mapped[str] = mapped_column(Text, default="")
    outcome: Mapped[str] = mapped_column(String(8), default="YES")
    token_id: Mapped[str] = mapped_column(String(128), default="")
    # DOBLE COTA DE PnL (portada del bot Python; el gap ES el spread):
    #   entry_price       = ASK real -> crucé el spread     (TAKER, realista)
    #   entry_price_maker = BID real -> me llenaron al bid  (MAKER, techo)
    # Manda la TAKER: pnl_per_share/roi (los que consume el asignador) se
    # calculan con ella. La maker es SOLO diagnostico -- ver cuanto del ROI de
    # una estrategia es techo optimista y cuanto sobrevive a la ejecucion real.
    # Medir solo la maker es como Hermes v1.1 "se veia rentable": su propia doc
    # la etiqueta "optimista (techo)" y avisa de que sin la taker no se puede
    # saber si el edge sobrevive.
    entry_price: Mapped[float] = mapped_column(Float, default=0.0)   # ask real al disparar
    entry_price_maker: Mapped[float | None] = mapped_column(Float, nullable=True)
    # CONTEXTO DE COSTE EN EL MOMENTO DE LA SEÑAL. Sin esto el histórico no
    # puede responder la unica pregunta que importa ("¿hay edge DESPUES de
    # costes?"): el listón medido es ~2.2% de ida y vuelta, y los mercados de
    # precio extremo tienen spread relativo de 2-8% y hasta 27%. Un ROI shadow
    # calculado sin saber el spread mezcla señales operables con señales que el
    # Risk Manager jamas habria aprobado, y el asignador podria dar peso por
    # beneficios que nunca fueron capturables.
    # NULLABLE a proposito: las filas anteriores al 2026-07-16 NO tienen el
    # dato. NULL significa "no se registro"; un 0.0 mentiria diciendo "operar
    # aqui era gratis", que es justo el error que este campo evita.
    spread: Mapped[float | None] = mapped_column(Float, nullable=True)
    liquidity: Mapped[float | None] = mapped_column(Float, nullable=True)
    signal_prob: Mapped[float] = mapped_column(Float, default=0.0)
    market_prob: Mapped[float] = mapped_column(Float, default=0.0)
    edge: Mapped[float] = mapped_column(Float, default=0.0)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    score: Mapped[float] = mapped_column(Float, default=0.0)
    explanation: Mapped[str] = mapped_column(Text, default="")
    valid_conditions: Mapped[str] = mapped_column(Text, default="")
    end_date: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # open -> resolved (mercado resuelto) | void (irresoluble tras 30 dias).
    status: Mapped[str] = mapped_column(String(16), default="open", index=True)
    resolved_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    won: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    settle_price: Mapped[float] = mapped_column(Float, default=0.0)
    # Cota TAKER (realista): la que manda y la que consume el asignador.
    pnl_per_share: Mapped[float] = mapped_column(Float, default=0.0)
    roi: Mapped[float] = mapped_column(Float, default=0.0)           # pnl / entry_price
    # Cota MAKER (techo optimista): SOLO diagnostico. La diferencia contra la
    # taker es el peaje del spread, y es exactamente el numero que separa "esto
    # se veia rentable" de "esto gana dinero".
    pnl_maker: Mapped[float | None] = mapped_column(Float, nullable=True)
    roi_maker: Mapped[float | None] = mapped_column(Float, nullable=True)
    traded: Mapped[bool] = mapped_column(Boolean, default=False)     # ¿se opero de verdad?


class FeatureRow(Base):
    """Feature Store: toda variable cuantitativa que produce el Intelligence Layer.

    APPEND-ONLY y a proposito: es el "histórico propio" del que saldrán los
    modelos. Nunca depender solo del estado actual del mercado significa que
    cada feature se guarda con su ts y jamás se sobreescribe.

    Coste en disco (calculado sobre el ritmo real, no supuesto): microstructure
    corre cada 900 s sobre el top-K (15 mercados) y produce ~5 features por
    mercado => 96 pasadas/día x 15 x 5 = ~7.200 filas/día, ~2,6 M/año. A ~100 B
    por fila son ~250 MB/año, sobre 31 GB libres en el CT202: asumible. Si se
    baja el interval o se suman modulos, rehacer esta cuenta ANTES, no despues.

    Por eso la poda (FEATURE_RETENTION_DAYS) tiene default largo (365) y no los
    30 de los datos de alta frecuencia: podar esto sería tirar el activo que da
    sentido a todo el layer.
    """

    __tablename__ = "features"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    market_id: Mapped[str] = mapped_column(String(128), index=True)
    name: Mapped[str] = mapped_column(String(64), index=True)
    value: Mapped[float] = mapped_column(Float, default=0.0)
    module: Mapped[str] = mapped_column(String(64), default="", index=True)
    # Contexto no numérico de la feature (fuente, explicación, ids externos).
    # Texto JSON: el valor operable SIEMPRE es `value` (float), nunca esto.
    meta: Mapped[str] = mapped_column(Text, default="")
    ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)


class EventLogRow(Base):
    __tablename__ = "event_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)
    level: Mapped[str] = mapped_column(String(16), default="INFO", index=True)  # INFO/WARNING/ERROR/TRADE/RISK/SYSTEM
    source: Mapped[str] = mapped_column(String(64), default="engine")
    message: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[str] = mapped_column(Text, default="")
