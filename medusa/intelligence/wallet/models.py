"""Modelos ORM de Wallet Intelligence (PostgreSQL). Prefijo `wi_`.

Cuelgan del `Base` compartido: `init_db()` las crea con su `create_all` de
siempre. Ninguna tiene clave foranea contra las tablas de trading -- este
subsistema OBSERVA, no ata: una poda en `markets` jamas puede fallar por su
culpa. Es el mismo criterio que el del MIG.

`wi_dna_history` es APPEND-ONLY y es la que hace posible el panel de Evolucion:
sin una fila por pasada no hay forma de ver si la reputacion de una wallet sube,
baja o se derrumba. Sobreescribir el perfil y ya seria quedarse solo con la foto
de hoy.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import DateTime, Float, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from medusa.data.db_models import Base


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class WalletProfileRow(Base):
    """Perfil VIGENTE de una wallet: ultimo ADN, score, reputacion y cluster."""

    __tablename__ = "wi_wallets"

    wallet: Mapped[str] = mapped_column(String(128), primary_key=True)
    # Vector de ADN serializado (JSON de {metrica: valor}) en el orden canonico
    # de DNA_FEATURES. Se guarda como texto y no en 19 columnas: añadir una
    # metrica no puede exigir una migracion de esquema.
    dna: Mapped[str] = mapped_column(Text, default="")
    score: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    reputation: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    cluster: Mapped[int] = mapped_column(Integer, default=-1, index=True)
    n_positions: Mapped[int] = mapped_column(Integer, default=0)
    n_closed: Mapped[int] = mapped_column(Integer, default=0)
    n_markets: Mapped[int] = mapped_column(Integer, default=0)
    n_categories: Mapped[int] = mapped_column(Integer, default=0)
    categories: Mapped[str] = mapped_column(Text, default="")
    first_trade: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_trade: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # first_seen NO se reescribe nunca: es cuando Medusa vio esta wallet por
    # primera vez, y sin ese ancla no se puede medir nada longitudinal.
    first_seen: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)


class WalletDNAHistoryRow(Base):
    """Foto del ADN de una wallet en un instante (append-only). Panel Evolucion."""

    __tablename__ = "wi_dna_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)
    wallet: Mapped[str] = mapped_column(String(128), index=True)
    dna: Mapped[str] = mapped_column(Text, default="")
    score: Mapped[float] = mapped_column(Float, default=0.0)
    reputation: Mapped[float] = mapped_column(Float, default=0.0)
    cluster: Mapped[int] = mapped_column(Integer, default=-1)
    n_closed: Mapped[int] = mapped_column(Integer, default=0)


class WalletSimilarityRow(Base):
    """Par de wallets parecidas. Unico por (a, b) con a < b: la similitud es
    simetrica y guardar las dos direcciones doblaria cualquier recuento."""

    __tablename__ = "wi_similarity"
    __table_args__ = (
        UniqueConstraint("wallet_a", "wallet_b", name="uq_wi_similarity_pair"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    wallet_a: Mapped[str] = mapped_column(String(128), index=True)
    wallet_b: Mapped[str] = mapped_column(String(128), index=True)
    similarity: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class WalletClusterRow(Base):
    """Centroide de un cluster en una pasada. El cluster es un ENTERO: su
    significado son los 19 numeros del centroide, no una etiqueta."""

    __tablename__ = "wi_clusters"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)
    cluster: Mapped[int] = mapped_column(Integer, index=True)
    size: Mapped[int] = mapped_column(Integer, default=0)
    share: Mapped[float] = mapped_column(Float, default=0.0)
    centroid: Mapped[str] = mapped_column(Text, default="")
    separating_features: Mapped[str] = mapped_column(Text, default="")


class WalletRunRow(Base):
    """Telemetria de cada pasada + estadistica de poblacion + importancia de
    features. Guardar la poblacion es imprescindible: un score de 0.8 solo
    significa algo contra la media y la desviacion con las que se calculo."""

    __tablename__ = "wi_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)
    wallets: Mapped[int] = mapped_column(Integer, default=0)
    positions: Mapped[int] = mapped_column(Integer, default=0)
    clusters: Mapped[int] = mapped_column(Integer, default=0)
    similarity_pairs: Mapped[int] = mapped_column(Integer, default=0)
    build_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    population: Mapped[str] = mapped_column(Text, default="")
    feature_importance: Mapped[str] = mapped_column(Text, default="")
