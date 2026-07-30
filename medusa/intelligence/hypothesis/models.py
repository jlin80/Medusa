"""Modelos ORM del Hypothesis Engine. PostgreSQL, prefijo `hyp_`.

Las tablas cuelgan del mismo `Base` que el resto de Medusa: `init_db()` las crea
con su `create_all` de siempre, sin tocar nada del esquema existente. Ninguna
tabla tiene clave foranea contra las tablas de trading, igual que en el MIG y en
el IFE: el motor OBSERVA el sistema, no lo ata, y una poda en `markets` jamas
puede fallar por culpa de una fila de aqui.

QUE SE GUARDA Y POR QUE:

    hyp_observations  las observaciones cosechadas, con su instante. Es la tabla
                      IRREPETIBLE del motor y la razon de que exista: la valla
                      temporal solo se puede AUDITAR si queda registrado que una
                      observacion entro en tal momento. Sin esta tabla, "esta
                      hipotesis se valido con datos posteriores a su creacion"
                      seria una afirmacion que nadie podria comprobar.
    hyp_hypotheses    el estado actual de cada hipotesis (upsert por `id`, que es
                      el hash del enunciado). Cinco columnas de aqui son el
                      contrato publico: id, description, status, confidence,
                      sample_count, created_at y updated_at.
    hyp_evidence      APPEND-ONLY: una fila por hipotesis y por pasada con su
                      estimacion fuera de muestra. Es la curva de como la
                      evidencia fue creciendo (o no), y es lo que permite ver que
                      una hipotesis validada llevaba tres pasadas debilitandose.
    hyp_transitions   APPEND-ONLY: cada cambio de estado, con su motivo. El
                      expediente. Sin el, un `rejected` es un veredicto sin
                      instruccion.
    hyp_snapshots     una fila por pasada: la serie temporal del motor, con
                      cuantos contrastes se probaron (el denominador de la
                      correccion por multiplicidad).
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
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from medusa.data.db_models import Base


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class HypObservationRow(Base):
    """Una observacion cosechada. PK = huella estable (`uid`).

    `ts` es el instante en el que la observacion QUEDO COMPLETA (para una señal
    de estrategia, su `resolved_at`), no el instante en el que empezo. Es la
    columna contra la que se compara `created_at` de cada hipotesis, asi que
    confundirla con la fecha de inicio rompe la valla temporal en silencio.

    `ingested_at` es otra cosa y tambien hace falta: es cuando el motor la vio.
    La diferencia entre las dos delata un retraso de cosecha, que es el unico
    modo de que una observacion "vieja" aparezca despues de una hipotesis nueva.
    """

    __tablename__ = "hyp_observations"

    uid: Mapped[str] = mapped_column(String(40), primary_key=True)
    source: Mapped[str] = mapped_column(String(64), index=True)
    entity: Mapped[str] = mapped_column(String(128), index=True)
    ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), index=True)
    # Los tres sacos, en JSON. El motor descubre las columnas de los datos: una
    # tabla con columnas fijas obligaria a una migracion cada vez que una fuente
    # empezara a traer una feature nueva, y con ella se perderia la unica gracia
    # del motor -- poder hablar de una variable que nadie previo.
    features: Mapped[str] = mapped_column(Text, default="")
    labels: Mapped[str] = mapped_column(Text, default="")
    outcomes: Mapped[str] = mapped_column(Text, default="")
    ingested_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True)


class HypothesisRow(Base):
    """Estado actual de una hipotesis. `id` = hash del enunciado, no aleatorio."""

    __tablename__ = "hyp_hypotheses"

    # --- contrato publico ---
    id: Mapped[str] = mapped_column(String(24), primary_key=True)
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="proposed", index=True)
    # NO es P(hipotesis cierta): es la fuerza de la evidencia fuera de muestra,
    # acotada en [0,1]. Vale 0.0 mientras el estado sea `proposed`.
    confidence: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    # Observaciones POSTERIORES a `created_at`. No es el total de la fuente.
    sample_count: Mapped[int] = mapped_column(Integer, default=0, index=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True)

    # --- de donde salio ---
    form: Mapped[str] = mapped_column(String(24), default="monotone", index=True)
    source: Mapped[str] = mapped_column(String(64), default="", index=True)
    predictor: Mapped[str] = mapped_column(String(64), default="", index=True)
    outcome: Mapped[str] = mapped_column(String(64), default="", index=True)
    level: Mapped[str] = mapped_column(String(64), default="")
    direction: Mapped[int] = mapped_column(Integer, default=0)
    # Parametros congelados al proponer (el corte de la forma umbral). Congelados
    # y no recalculados: reajustar el corte sobre los datos de prueba es elegir
    # el que mejor queda en el test y llamarlo replicacion.
    params: Mapped[str] = mapped_column(Text, default="")

    # --- evidencia de descubrimiento (dentro de muestra: NO es evidencia) ---
    discovery_n: Mapped[int] = mapped_column(Integer, default=0)
    discovery_effect: Mapped[float] = mapped_column(Float, default=0.0)
    discovery_lower: Mapped[float] = mapped_column(Float, default=0.0)
    discovery_upper: Mapped[float] = mapped_column(Float, default=0.0)
    discovery_p: Mapped[float] = mapped_column(Float, default=1.0)
    # Cuantas relaciones se contrastaron en la pasada que la propuso. Sin este
    # numero la significancia no se puede juzgar: un p<0.05 entre 400 contrastes
    # es lo que hace el azar veinte veces.
    tested_in_pass: Mapped[int] = mapped_column(Integer, default=0)

    # --- evidencia fuera de muestra (la unica que cuenta) ---
    test_n: Mapped[int] = mapped_column(Integer, default=0)
    test_effect: Mapped[float] = mapped_column(Float, default=0.0)
    test_lower: Mapped[float] = mapped_column(Float, default=0.0)
    test_upper: Mapped[float] = mapped_column(Float, default=0.0)
    test_p: Mapped[float] = mapped_column(Float, default=1.0)
    test_excludes_null: Mapped[bool] = mapped_column(Boolean, default=False)

    # --- expediente ---
    status_reason: Mapped[str] = mapped_column(Text, default="")
    first_tested_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    decided_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)


class HypEvidenceRow(Base):
    """Una medicion fuera de muestra, por hipotesis y por pasada. APPEND-ONLY.

    Unica por (hipotesis, instante) para que reejecutar una pasada no duplique la
    curva. Es la tabla que permite ver la HISTORIA de la confianza y no solo su
    ultimo valor: una hipotesis validada que lleva cuatro pasadas bajando dice
    algo que su fila actual no puede decir.
    """

    __tablename__ = "hyp_evidence"
    __table_args__ = (
        UniqueConstraint("hypothesis_id", "ts", name="uq_hyp_evidence_point"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    hypothesis_id: Mapped[str] = mapped_column(String(24), index=True)
    ts: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True)
    status: Mapped[str] = mapped_column(String(16), default="proposed")
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    sample_count: Mapped[int] = mapped_column(Integer, default=0)
    effect: Mapped[float] = mapped_column(Float, default=0.0)
    lower: Mapped[float] = mapped_column(Float, default=0.0)
    upper: Mapped[float] = mapped_column(Float, default=0.0)
    p_value: Mapped[float] = mapped_column(Float, default=1.0)


class HypTransitionRow(Base):
    """Un cambio de estado, con su motivo. APPEND-ONLY: es el expediente."""

    __tablename__ = "hyp_transitions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    hypothesis_id: Mapped[str] = mapped_column(String(24), index=True)
    ts: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True)
    from_status: Mapped[str] = mapped_column(String(16), default="")
    to_status: Mapped[str] = mapped_column(String(16), default="", index=True)
    sample_count: Mapped[int] = mapped_column(Integer, default=0)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    reason: Mapped[str] = mapped_column(Text, default="")


class HypSnapshotRow(Base):
    """Foto tras cada pasada: la serie temporal del motor.

    `tested` es el numero de contrastes de la pasada y se guarda a proposito: es
    el denominador de la correccion por multiplicidad, y sin el la serie de
    "descubrimientos" no se puede interpretar.
    """

    __tablename__ = "hyp_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True)
    observations: Mapped[int] = mapped_column(Integer, default=0)
    new_observations: Mapped[int] = mapped_column(Integer, default=0)
    variables: Mapped[int] = mapped_column(Integer, default=0)
    tested: Mapped[int] = mapped_column(Integer, default=0)
    proposed: Mapped[int] = mapped_column(Integer, default=0)
    testing: Mapped[int] = mapped_column(Integer, default=0)
    validated: Mapped[int] = mapped_column(Integer, default=0)
    rejected: Mapped[int] = mapped_column(Integer, default=0)
    new_proposals: Mapped[int] = mapped_column(Integer, default=0)
    transitions: Mapped[int] = mapped_column(Integer, default=0)
    avg_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    build_seconds: Mapped[float] = mapped_column(Float, default=0.0)
