"""Acceso a Postgres via SQLAlchemy 2.0 async + creacion de esquema."""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from medusa.config import get_settings
from medusa.data.db_models import Base

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = create_async_engine(
            get_settings().database_url,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=5,
            future=True,
        )
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(
            get_engine(), class_=AsyncSession, expire_on_commit=False
        )
    return _sessionmaker


async def check_db() -> bool:
    """Devuelve True si Postgres responde; lanza excepcion si no."""
    async with get_engine().connect() as conn:
        await conn.execute(text("SELECT 1"))
    return True


# Migracion ligera: create_all crea TABLAS nuevas pero no añade COLUMNAS a
# tablas existentes, y la BD del CT202 lleva datos reales que no se pueden
# resetear. ADD COLUMN IF NOT EXISTS es idempotente (Postgres >= 9.6): correr
# esto en cada arranque es barato y no rompe nada. Si algun dia hay un cambio
# de esquema mas serio, ahi si tocara Alembic (ya esta en requirements).
_COLUMN_MIGRATIONS: tuple[str, ...] = (
    "ALTER TABLE markets ADD COLUMN IF NOT EXISTS medusa_category VARCHAR(32) DEFAULT ''",
    "ALTER TABLE markets ADD COLUMN IF NOT EXISTS opportunity_score FLOAT DEFAULT 0.0",
    "ALTER TABLE opportunities ADD COLUMN IF NOT EXISTS strategy VARCHAR(64) DEFAULT ''",
    "ALTER TABLE opportunities ADD COLUMN IF NOT EXISTS category VARCHAR(32) DEFAULT ''",
    "ALTER TABLE opportunities ADD COLUMN IF NOT EXISTS score FLOAT DEFAULT 0.0",
    "ALTER TABLE positions ADD COLUMN IF NOT EXISTS strategy VARCHAR(64) DEFAULT ''",
    "ALTER TABLE trades ADD COLUMN IF NOT EXISTS strategy VARCHAR(64) DEFAULT ''",
    "CREATE INDEX IF NOT EXISTS ix_markets_medusa_category ON markets (medusa_category)",
    # Feature Store: la consulta caliente es "ultimas features de este mercado"
    # (DISTINCT ON (name) ... ORDER BY name, ts DESC). Sin este indice compuesto
    # esa query degenera en un scan por mercado en cada ciclo.
    "CREATE INDEX IF NOT EXISTS ix_features_market_name_ts ON features (market_id, name, ts DESC)",
    # Contexto de coste de cada señal. SIN DEFAULT a proposito: las filas
    # anteriores quedan a NULL = "no se registro". Un DEFAULT 0.0 afirmaria que
    # operar alli era gratis y contaminaria justo el analisis que este campo
    # existe para hacer posible (¿hay edge DESPUES de costes?).
    "ALTER TABLE strategy_signals ADD COLUMN IF NOT EXISTS spread FLOAT",
    "ALTER TABLE strategy_signals ADD COLUMN IF NOT EXISTS liquidity FLOAT",
    # Doble cota de PnL: entrada maker (bid) + su PnL/ROI al resolver. NULL en
    # las filas viejas = "no se midio"; jamas 0.0, que se leeria como "el techo
    # era cero" y es mentira.
    "ALTER TABLE strategy_signals ADD COLUMN IF NOT EXISTS entry_price_maker FLOAT",
    "ALTER TABLE strategy_signals ADD COLUMN IF NOT EXISTS pnl_maker FLOAT",
    "ALTER TABLE strategy_signals ADD COLUMN IF NOT EXISTS roi_maker FLOAT",
    # Mercados Up/Down activos (toggle por mercado desde el dashboard). SIN
    # DEFAULT a proposito: NULL = "nunca se tocó" => se usa el default de la
    # config; un "" seria "todos apagados a mano", que es una eleccion distinta.
    "ALTER TABLE bot_state ADD COLUMN IF NOT EXISTS updown_assets VARCHAR(128)",
)


def _extra_migrations() -> tuple[str, ...]:
    """Migraciones de los paquetes ADITIVOS (Market Intelligence Graph, Wallet
    Intelligence, Information Flow Engine e Hypothesis Engine).

    Se importan aqui dentro, no arriba, y cada uno en su propio try: un fallo de
    importacion de un paquete opcional no puede impedir el arranque del engine
    ni de la API, y tampoco puede arrastrar al otro paquete opcional. Sin el
    import, el resto del esquema se crea igual que antes de que existieran. El
    import ademas registra sus tablas en `Base`, que es lo que hace que
    `create_all` (arriba) las cree.
    """
    out: tuple[str, ...] = ()
    try:
        from medusa.intelligence.mig.migrations import MIG_MIGRATIONS
        out += MIG_MIGRATIONS
    except Exception:  # noqa: BLE001 - un paquete opcional nunca bloquea el arranque
        pass
    try:
        from medusa.intelligence.wallet.migrations import WALLET_MIGRATIONS
        out += WALLET_MIGRATIONS
    except Exception:  # noqa: BLE001
        pass
    try:
        from medusa.intelligence.flow.migrations import FLOW_MIGRATIONS
        out += FLOW_MIGRATIONS
    except Exception:  # noqa: BLE001
        pass
    try:
        from medusa.intelligence.hypothesis.migrations import HYPOTHESIS_MIGRATIONS
        out += HYPOTHESIS_MIGRATIONS
    except Exception:  # noqa: BLE001
        pass
    return out


async def init_db() -> None:
    """Crea las tablas si no existen y aplica la migracion ligera (idempotente)."""
    extra = _extra_migrations()
    async with get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        for stmt in _COLUMN_MIGRATIONS + extra:
            await conn.execute(text(stmt))


async def dispose() -> None:
    global _engine
    if _engine is not None:
        await _engine.dispose()
        _engine = None
