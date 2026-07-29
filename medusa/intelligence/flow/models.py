"""Modelos ORM del Information Flow Engine. PostgreSQL, prefijo `flow_`.

Las tablas cuelgan del mismo `Base` que el resto de Medusa: `init_db()` las crea
con su `create_all` de siempre, sin tocar nada del esquema existente. Ninguna
tabla tiene clave foranea contra las tablas de trading, igual que en el MIG: el
motor OBSERVA el sistema, no lo ata, y una poda en `markets` jamas puede fallar
por culpa de una fila de aqui.

QUE SE GUARDA Y POR QUE (el criterio es "poder recalcular sin volver a la red"):

    flow_trades      la cinta cruda ya normalizada. Es lo unico irrepetible: la
                     Data API pagina y envejece, y si mañana cambia la definicion
                     de cascada hay que poder recalcularlo TODO sin haber perdido
                     los datos. Deduplicada por `uid`.
    flow_cascades    cada racha detectada, con sus tiempos y su resolucion.
    flow_events      CADA eslabon de propagacion. El enunciado del motor es
                     "guarda todos los eventos de propagacion", y eso es esta
                     tabla: append-only, sin agregar, sin filtrar por score.
    flow_wallet_metrics / flow_market_metrics
                     el estado actual por wallet y por mercado (upsert). Son
                     derivadas: se pueden reconstruir enteras desde las tres de
                     arriba.
    flow_snapshots   una fila por pasada: la serie temporal del motor.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from medusa.data.db_models import Base


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class FlowTradeRow(Base):
    """Un trade publico normalizado. PK = huella estable (`uid`)."""

    __tablename__ = "flow_trades"

    uid: Mapped[str] = mapped_column(String(40), primary_key=True)
    market_id: Mapped[str] = mapped_column(String(128), index=True)
    wallet: Mapped[str] = mapped_column(String(64), index=True)
    # Lado ENTRADO (ya normalizado: vender SI es entrar en NO).
    side: Mapped[str] = mapped_column(String(4), index=True)
    # Probabilidad implicita DEL LADO ENTRADO, no la del token YES.
    price: Mapped[float] = mapped_column(Float, default=0.0)
    size: Mapped[float] = mapped_column(Float, default=0.0)
    ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), index=True)
    ingested_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow)


class FlowCascadeRow(Base):
    """Una racha de entradas encadenadas. Unica por (mercado, lado, inicio)."""

    __tablename__ = "flow_cascades"
    __table_args__ = (
        UniqueConstraint("cascade_key", name="uq_flow_cascades_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    cascade_key: Mapped[str] = mapped_column(String(200), index=True)
    market_id: Mapped[str] = mapped_column(String(128), index=True)
    side: Mapped[str] = mapped_column(String(4), index=True)
    started_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), index=True)
    ended_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    n_participants: Mapped[int] = mapped_column(Integer, default=0, index=True)
    span_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    propagation_time: Mapped[float] = mapped_column(Float, default=0.0)
    consensus_delay: Mapped[float] = mapped_column(Float, default=0.0)
    price_start: Mapped[float] = mapped_column(Float, default=0.0)
    price_end: Mapped[float] = mapped_column(Float, default=0.0)
    price_move: Mapped[float] = mapped_column(Float, default=0.0)
    notional: Mapped[float] = mapped_column(Float, default=0.0)
    # `resolution_value` es NULL mientras el mercado siga vivo. Nunca 0.5, nunca
    # el ultimo precio: un mercado sin resultado no tiene resultado.
    resolved: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    resolution_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    participants: Mapped[str] = mapped_column(Text, default="")
    ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class FlowEventRow(Base):
    """Un eslabon observado: `leader` entro y despues `follower`.

    ORDEN TEMPORAL, NO CAUSALIDAD. Los nombres de las columnas son deliberados:
    `lag_seconds` y `hop` describen distancia, no influencia. No hay ninguna
    columna que afirme que una wallet provoco la entrada de la otra, porque el
    motor no tiene con que sostener esa afirmacion.
    """

    __tablename__ = "flow_events"
    __table_args__ = (
        UniqueConstraint("cascade_key", "leader", "follower",
                         name="uq_flow_events_link"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    cascade_key: Mapped[str] = mapped_column(String(200), index=True)
    market_id: Mapped[str] = mapped_column(String(128), index=True)
    side: Mapped[str] = mapped_column(String(4))
    leader: Mapped[str] = mapped_column(String(64), index=True)
    follower: Mapped[str] = mapped_column(String(64), index=True)
    hop: Mapped[int] = mapped_column(Integer, default=1, index=True)
    lag_seconds: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    price_leader: Mapped[float] = mapped_column(Float, default=0.0)
    price_follower: Mapped[float] = mapped_column(Float, default=0.0)
    price_move: Mapped[float] = mapped_column(Float, default=0.0)
    ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), index=True)


class FlowWalletRow(Base):
    """Estado actual de una wallet en el flujo. Derivada y reconstruible."""

    __tablename__ = "flow_wallet_metrics"

    wallet: Mapped[str] = mapped_column(String(64), primary_key=True)
    n_cascades: Mapped[int] = mapped_column(Integer, default=0, index=True)
    n_markets: Mapped[int] = mapped_column(Integer, default=0)
    leadership_score: Mapped[float] = mapped_column(Float, default=0.5, index=True)
    follow_score: Mapped[float] = mapped_column(Float, default=0.5)
    leadership_lower: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    edge_vs_chance: Mapped[float] = mapped_column(Float, default=0.0)
    information_speed: Mapped[float] = mapped_column(Float, default=0.0)
    speed_score: Mapped[float] = mapped_column(Float, default=0.0)
    propagation_time: Mapped[float] = mapped_column(Float, default=0.0)
    early_information_score: Mapped[float] = mapped_column(Float, default=0.0)
    late_information_score: Mapped[float] = mapped_column(Float, default=0.0)
    n_early: Mapped[int] = mapped_column(Integer, default=0)
    n_late: Mapped[int] = mapped_column(Integer, default=0)
    early_lower: Mapped[float] = mapped_column(Float, default=0.0)
    late_lower: Mapped[float] = mapped_column(Float, default=0.0)
    information_edge: Mapped[float] = mapped_column(Float, default=0.0)
    # Sin esto, un panel mostraria un 1.00 de n=1 al lado de un 0.61 de n=400.
    enough_samples: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    first_seen: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True)
    last_seen: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True)


class FlowMarketRow(Base):
    """Estado actual de un mercado en el flujo. Derivada y reconstruible."""

    __tablename__ = "flow_market_metrics"

    market_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    n_cascades: Mapped[int] = mapped_column(Integer, default=0, index=True)
    n_wallets: Mapped[int] = mapped_column(Integer, default=0)
    n_events: Mapped[int] = mapped_column(Integer, default=0)
    consensus_delay: Mapped[float] = mapped_column(Float, default=0.0)
    propagation_time: Mapped[float] = mapped_column(Float, default=0.0)
    information_speed: Mapped[float] = mapped_column(Float, default=0.0)
    avg_cascade_size: Mapped[float] = mapped_column(Float, default=0.0)
    price_move: Mapped[float] = mapped_column(Float, default=0.0)
    enough_samples: Mapped[bool] = mapped_column(Boolean, default=False)
    first_seen: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow)
    last_seen: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True)


class FlowSnapshotRow(Base):
    """Foto tras cada pasada: la serie temporal del motor.

    Coste: una fila por pasada. Con el intervalo por defecto (30 min) son
    ~17.500 filas al año, unos pocos MB.
    """

    __tablename__ = "flow_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True)
    markets: Mapped[int] = mapped_column(Integer, default=0)
    trades: Mapped[int] = mapped_column(Integer, default=0)
    cascades: Mapped[int] = mapped_column(Integer, default=0)
    events: Mapped[int] = mapped_column(Integer, default=0)
    wallets: Mapped[int] = mapped_column(Integer, default=0)
    resolved_cascades: Mapped[int] = mapped_column(Integer, default=0)
    median_propagation_time: Mapped[float] = mapped_column(Float, default=0.0)
    median_consensus_delay: Mapped[float] = mapped_column(Float, default=0.0)
    median_lag: Mapped[float] = mapped_column(Float, default=0.0)
    median_cascade_size: Mapped[float] = mapped_column(Float, default=0.0)
    build_seconds: Mapped[float] = mapped_column(Float, default=0.0)
