"""Migraciones del IFE (PostgreSQL). Idempotentes y aditivas.

Mismo criterio que `medusa/infra/db.py` y que el MIG: `create_all` crea las
tablas nuevas, pero no añade indices a tablas que ya existen. Todo lo de aqui
usa `IF NOT EXISTS`, asi que correrlo en cada arranque es barato y no rompe nada
aunque la BD ya lleve datos reales.

REGLA: este fichero SOLO puede crear objetos del motor (prefijo `flow_`). Ni un
ALTER, ni un DROP, ni un UPDATE sobre una tabla del sistema de trading. Hay un
test que lo comprueba sobre estas mismas cadenas.
"""

from __future__ import annotations

# Importar los modelos registra las tablas en el `Base` compartido, que es lo
# que hace que `init_db()` -> `create_all` las cree. Sin este import las tablas
# no existirian aunque las sentencias de abajo se ejecutasen.
from medusa.intelligence.flow import models as _models  # noqa: F401

FLOW_MIGRATIONS: tuple[str, ...] = (
    # --- cinta cruda ---
    # La consulta caliente es "trades de este mercado en orden": el indice
    # compuesto la cubre entera y evita ordenar la tabla en cada recalculo.
    "CREATE INDEX IF NOT EXISTS ix_flow_trades_market_ts ON flow_trades (market_id, ts)",
    "CREATE INDEX IF NOT EXISTS ix_flow_trades_wallet_ts ON flow_trades (wallet, ts DESC)",

    # --- cascadas ---
    # La unicidad de `cascade_key` es lo que hace que reanalizar la misma
    # ventana no duplique cascadas, y es el destino del ON CONFLICT del upsert:
    # sin este indice, el upsert no tiene donde apoyarse.
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_flow_cascades_key ON flow_cascades (cascade_key)",
    "CREATE INDEX IF NOT EXISTS ix_flow_cascades_market_start ON flow_cascades (market_id, started_at DESC)",
    "CREATE INDEX IF NOT EXISTS ix_flow_cascades_size ON flow_cascades (n_participants DESC)",

    # --- eventos de propagacion ---
    # Un eslabon es unico por (cascada, lider, seguidor). Sin esta unicidad, dos
    # pasadas sobre la misma ventana contarian el mismo eslabon dos veces y toda
    # la estadistica de latencias mediria repeticion de ingesta.
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_flow_events_link ON flow_events (cascade_key, leader, follower)",
    "CREATE INDEX IF NOT EXISTS ix_flow_events_leader_ts ON flow_events (leader, ts DESC)",
    "CREATE INDEX IF NOT EXISTS ix_flow_events_follower_ts ON flow_events (follower, ts DESC)",
    "CREATE INDEX IF NOT EXISTS ix_flow_events_pair ON flow_events (leader, follower)",

    # --- metricas ---
    "CREATE INDEX IF NOT EXISTS ix_flow_wallet_lead ON flow_wallet_metrics (leadership_lower DESC, n_cascades DESC)",
    "CREATE INDEX IF NOT EXISTS ix_flow_wallet_speed ON flow_wallet_metrics (speed_score DESC)",
    "CREATE INDEX IF NOT EXISTS ix_flow_market_cascades ON flow_market_metrics (n_cascades DESC)",

    # --- serie temporal ---
    "CREATE INDEX IF NOT EXISTS ix_flow_snapshots_ts ON flow_snapshots (ts DESC)",
)


def statements() -> tuple[str, ...]:
    """Sentencias a aplicar. Existe como funcion para dejar un punto unico donde
    añadir logica condicional el dia que haga falta."""
    return FLOW_MIGRATIONS
