"""Migraciones de Wallet Intelligence (PostgreSQL). Idempotentes y aditivas.

Mismo criterio que `medusa/infra/db.py` y que el MIG: todo con
`IF NOT EXISTS`, y SOLO sobre objetos con prefijo `wi_`. Ni un ALTER, ni un
DROP, ni un UPDATE sobre una tabla del sistema de trading.
"""

from __future__ import annotations

# El import registra las tablas en el `Base` compartido; sin el, `create_all`
# no las crearia aunque estas sentencias se ejecutasen.
from medusa.intelligence.wallet import models as _models  # noqa: F401

WALLET_MIGRATIONS: tuple[str, ...] = (
    # Rankings del dashboard: "top por reputacion" y "top por score".
    "CREATE INDEX IF NOT EXISTS ix_wi_wallets_reputation ON wi_wallets (reputation DESC)",
    "CREATE INDEX IF NOT EXISTS ix_wi_wallets_score ON wi_wallets (score DESC)",
    "CREATE INDEX IF NOT EXISTS ix_wi_wallets_cluster ON wi_wallets (cluster, reputation DESC)",
    # Panel de evolucion: la consulta es siempre "historia de ESTA wallet".
    "CREATE INDEX IF NOT EXISTS ix_wi_dna_history_wallet_ts ON wi_dna_history (wallet, ts DESC)",
    # Similitud: unicidad del par (destino del ON CONFLICT del upsert) y
    # busqueda por cualquiera de los dos extremos.
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_wi_similarity_pair ON wi_similarity (wallet_a, wallet_b)",
    "CREATE INDEX IF NOT EXISTS ix_wi_similarity_a ON wi_similarity (wallet_a, similarity DESC)",
    "CREATE INDEX IF NOT EXISTS ix_wi_similarity_b ON wi_similarity (wallet_b, similarity DESC)",
    # Clusters y pasadas: siempre se lee la ultima.
    "CREATE INDEX IF NOT EXISTS ix_wi_clusters_ts ON wi_clusters (ts DESC, cluster)",
    "CREATE INDEX IF NOT EXISTS ix_wi_runs_ts ON wi_runs (ts DESC)",
)


def statements() -> tuple[str, ...]:
    return WALLET_MIGRATIONS
