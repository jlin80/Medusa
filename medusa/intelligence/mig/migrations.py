"""Migraciones del MIG (PostgreSQL). Idempotentes y aditivas.

Mismo criterio que `medusa/infra/db.py`: `create_all` crea las tablas nuevas,
pero no añade columnas ni indices a tablas que ya existen. Todo lo de aqui usa
`IF NOT EXISTS`, asi que correrlo en cada arranque es barato y no rompe nada
aunque la BD ya lleve datos reales.

REGLA: este fichero SOLO puede crear objetos del MIG (prefijo `mig_`). Ni un
ALTER, ni un DROP, ni un UPDATE sobre una tabla del sistema de trading. Si algun
dia una migracion de aqui necesita tocar `markets`, `positions`, `trades` o
`strategy_signals`, la respuesta correcta es que NO va aqui.
"""

from __future__ import annotations

# Importar los modelos registra las tablas del MIG en el `Base` compartido, que
# es lo que hace que `init_db()` -> `create_all` las cree. Sin este import las
# tablas no existirian aunque las sentencias de abajo se ejecutasen.
from medusa.intelligence.mig import models as _models  # noqa: F401

MIG_MIGRATIONS: tuple[str, ...] = (
    # --- nodos ---
    # La consulta caliente del Research Lab es "cuantos nodos de cada tipo" y
    # "ultimos nodos de este tipo": el indice compuesto cubre las dos.
    "CREATE INDEX IF NOT EXISTS ix_mig_nodes_type_last ON mig_nodes (node_type, last_seen DESC)",
    "CREATE INDEX IF NOT EXISTS ix_mig_nodes_first_seen ON mig_nodes (first_seen)",

    # --- aristas ---
    # Unicidad del triple (src, dst, tipo): es lo que impide que una relacion se
    # duplique al reconstruir el grafo y que las estadisticas de crecimiento
    # midan repeticion en vez de conocimiento nuevo. Ademas es el destino del
    # ON CONFLICT del upsert: sin este indice, el upsert no tiene donde apoyarse.
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_mig_edges_triple ON mig_edges (src, dst, edge_type)",
    "CREATE INDEX IF NOT EXISTS ix_mig_edges_src_type ON mig_edges (src, edge_type)",
    "CREATE INDEX IF NOT EXISTS ix_mig_edges_dst_type ON mig_edges (dst, edge_type)",
    "CREATE INDEX IF NOT EXISTS ix_mig_edges_type_weight ON mig_edges (edge_type, weight DESC)",

    # --- descubrimientos ---
    "CREATE INDEX IF NOT EXISTS ix_mig_discoveries_ts_score ON mig_discoveries (ts DESC, score DESC)",
    "CREATE INDEX IF NOT EXISTS ix_mig_discoveries_kind_ts ON mig_discoveries (kind, ts DESC)",

    # --- snapshots (curva de crecimiento) ---
    "CREATE INDEX IF NOT EXISTS ix_mig_snapshots_ts ON mig_snapshots (ts DESC)",
)


def statements() -> tuple[str, ...]:
    """Sentencias a aplicar. Existe como funcion para que el llamante no tenga
    que importar el modulo por su constante y para dejar un punto unico donde
    añadir logica condicional el dia que haga falta."""
    return MIG_MIGRATIONS
