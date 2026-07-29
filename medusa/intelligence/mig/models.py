"""Modelos ORM del MIG. PostgreSQL, y SOLO PostgreSQL (sin Neo4j).

Por que Postgres basta para V1: el grafo se consulta casi siempre a un salto
("aristas de este nodo", "cuantos nodos de este tipo", "top por grado"), y eso
en SQL con dos indices es directo y barato. Un motor de grafos añadiria un
servicio mas al stack -- en una maquina de 2 vCPU y 3 GB donde Postgres, Redis,
engine, API y nginx ya conviven -- a cambio de recorridos profundos que hoy
nadie hace. Si algun dia hacen falta caminos de longitud arbitraria, ESA sera la
conversacion; no antes.

Las tablas cuelgan del mismo `Base` que el resto de Medusa: `init_db()` las crea
con su `create_all` de siempre, sin tocar nada del esquema existente. Ninguna
tabla del MIG tiene clave foranea contra las tablas de trading a proposito: el
grafo OBSERVA el sistema, no lo ata. Un borrado o una poda en `markets` jamas
puede fallar por culpa del grafo.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import DateTime, Float, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from medusa.data.db_models import Base


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class MIGNodeRow(Base):
    """Un nodo del grafo. PK = "<tipo>:<id externo>" (clave estable)."""

    __tablename__ = "mig_nodes"

    key: Mapped[str] = mapped_column(String(320), primary_key=True)
    node_type: Mapped[str] = mapped_column(String(32), index=True)
    label: Mapped[str] = mapped_column(Text, default="")
    # Unico numero operable del nodo; todo lo cualitativo va en `meta` (JSON de
    # texto). Misma separacion que Feature.value / Feature.meta.
    weight: Mapped[float] = mapped_column(Float, default=0.0)
    meta: Mapped[str] = mapped_column(Text, default="")
    # first_seen NUNCA se reescribe: es lo que hace posible medir el crecimiento
    # del grafo. Si se pisara en cada construccion, todos los nodos parecerian
    # nuevos y la serie temporal seria plana y falsa.
    first_seen: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)
    last_seen: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)


class MIGEdgeRow(Base):
    """Una arista. Unica por (src, dst, edge_type): el mismo par de nodos puede
    estar unido por varias relaciones distintas, pero no dos veces por la misma."""

    __tablename__ = "mig_edges"
    __table_args__ = (
        UniqueConstraint("src", "dst", "edge_type", name="uq_mig_edges_triple"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    src: Mapped[str] = mapped_column(String(320), index=True)
    dst: Mapped[str] = mapped_column(String(320), index=True)
    edge_type: Mapped[str] = mapped_column(String(32), index=True)
    weight: Mapped[float] = mapped_column(Float, default=0.0)
    # Observaciones que sostienen la arista. Una relacion vista una vez y otra
    # vista 400 no valen lo mismo, y quien lea el grafo debe poder distinguirlo.
    count: Mapped[int] = mapped_column(Integer, default=1)
    meta: Mapped[str] = mapped_column(Text, default="")
    first_seen: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)
    last_seen: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)


class MIGDiscoveryRow(Base):
    """Un objeto de inteligencia derivado del grafo (append-only).

    Append-only y con `ts`: dos construcciones distintas del mismo patron son
    dos observaciones, no una correccion. Asi se puede mirar despues si un
    "descubrimiento" aguanto o se deshizo -- que es justo la pregunta que separa
    un patron real de un artefacto de la muestra.
    """

    __tablename__ = "mig_discoveries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)
    kind: Mapped[str] = mapped_column(String(64), index=True)
    subject: Mapped[str] = mapped_column(String(320), default="", index=True)
    statement: Mapped[str] = mapped_column(Text, default="")
    score: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    value: Mapped[float] = mapped_column(Float, default=0.0)
    evidence: Mapped[str] = mapped_column(Text, default="")
    # Nombre de la feature en la que ESTE descubrimiento podria convertirse. Es
    # una propuesta, no una feature: el MIG no escribe en el Feature Store.
    feature_name: Mapped[str] = mapped_column(String(128), default="")


class MIGSnapshotRow(Base):
    """Foto del grafo tras cada construccion: la serie de crecimiento.

    Coste: una fila por construccion. Con el intervalo por defecto (1 h) son
    ~8.800 filas al año, unos pocos MB. No necesita poda agresiva.
    """

    __tablename__ = "mig_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)
    nodes: Mapped[int] = mapped_column(Integer, default=0)
    edges: Mapped[int] = mapped_column(Integer, default=0)
    discoveries: Mapped[int] = mapped_column(Integer, default=0)
    node_counts: Mapped[str] = mapped_column(Text, default="")
    edge_counts: Mapped[str] = mapped_column(Text, default="")
    build_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    sources: Mapped[str] = mapped_column(Text, default="")
