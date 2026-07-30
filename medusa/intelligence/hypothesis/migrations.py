"""Migraciones del HE (PostgreSQL). Idempotentes y aditivas.

Mismo criterio que `medusa/infra/db.py`, que el MIG y que el IFE: `create_all`
crea las tablas nuevas, pero no añade indices a tablas que ya existen. Todo lo de
aqui usa `IF NOT EXISTS`, asi que correrlo en cada arranque es barato y no rompe
nada aunque la BD ya lleve datos reales.

REGLA: este fichero SOLO puede crear objetos del motor (prefijo `hyp_`). Ni un
ALTER, ni un DROP, ni un UPDATE sobre una tabla del sistema de trading. Hay un
test que lo comprueba sobre estas mismas cadenas.
"""

from __future__ import annotations

# Importar los modelos registra las tablas en el `Base` compartido, que es lo que
# hace que `init_db()` -> `create_all` las cree. Sin este import las tablas no
# existirian aunque las sentencias de abajo se ejecutasen.
from medusa.intelligence.hypothesis import models as _models  # noqa: F401

HYPOTHESIS_MIGRATIONS: tuple[str, ...] = (
    # --- observaciones ---
    # La consulta caliente del motor es "observaciones de esta fuente posteriores
    # a tal instante": es literalmente la valla temporal, y se ejecuta una vez por
    # fuente y por pasada. El indice compuesto la cubre entera.
    "CREATE INDEX IF NOT EXISTS ix_hyp_obs_source_ts ON hyp_observations (source, ts)",
    "CREATE INDEX IF NOT EXISTS ix_hyp_obs_entity ON hyp_observations (entity, ts DESC)",
    "CREATE INDEX IF NOT EXISTS ix_hyp_obs_ingested ON hyp_observations (ingested_at DESC)",

    # --- hipotesis ---
    # El tablero agrupa por estado y ordena por confianza: el indice compuesto
    # sirve las cuatro columnas del panel sin ordenar la tabla entera.
    "CREATE INDEX IF NOT EXISTS ix_hyp_status_conf ON hyp_hypotheses (status, confidence DESC)",
    "CREATE INDEX IF NOT EXISTS ix_hyp_updated ON hyp_hypotheses (updated_at DESC)",
    "CREATE INDEX IF NOT EXISTS ix_hyp_source_form ON hyp_hypotheses (source, form)",
    "CREATE INDEX IF NOT EXISTS ix_hyp_samples ON hyp_hypotheses (sample_count DESC)",

    # --- evidencia ---
    # La unicidad de (hipotesis, ts) es el destino del ON CONFLICT del upsert:
    # sin este indice, reejecutar una pasada duplicaria puntos de la curva de
    # confianza y la haria ilegible.
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_hyp_evidence_point ON hyp_evidence (hypothesis_id, ts)",
    "CREATE INDEX IF NOT EXISTS ix_hyp_evidence_hyp ON hyp_evidence (hypothesis_id, ts DESC)",

    # --- expediente ---
    "CREATE INDEX IF NOT EXISTS ix_hyp_trans_hyp ON hyp_transitions (hypothesis_id, ts DESC)",
    "CREATE INDEX IF NOT EXISTS ix_hyp_trans_ts ON hyp_transitions (ts DESC)",

    # --- serie temporal ---
    "CREATE INDEX IF NOT EXISTS ix_hyp_snapshots_ts ON hyp_snapshots (ts DESC)",
)


def statements() -> tuple[str, ...]:
    """Sentencias a aplicar. Existe como funcion para dejar un punto unico donde
    añadir logica condicional el dia que haga falta."""
    return HYPOTHESIS_MIGRATIONS
