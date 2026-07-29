"""El contrato del MIG, verificado sobre el CODIGO FUENTE.

El paquete promete que no opera, que no toca el Risk Manager y que no escribe
fuera de sus tablas. Una promesa en un docstring no vale nada: estos tests la
comprueban con el AST y con el texto de las sentencias, de modo que romperla
tumbe la suite en vez de descubrirse en produccion.
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest

MIG_DIR = pathlib.Path(__file__).resolve().parents[1] / "medusa" / "intelligence" / "mig"
SOURCES = sorted(MIG_DIR.glob("*.py"))

# Paquetes que el MIG NO puede importar ni directa ni indirectamente desde su
# propio codigo. Cada uno es una via a mover dinero o a saltarse un candado.
PROHIBIDOS = (
    "medusa.execution",
    "medusa.trading",
    "medusa.risk",
    "medusa.strategies",
    "medusa.allocation",
    "medusa.updown",
    "medusa.data.polymarket",
    "py_clob_client",
)

# Tablas del sistema de trading. El MIG las LEE (via los repositorios
# existentes) pero no puede escribirlas.
TABLAS_AJENAS = (
    "markets", "opportunities", "orders", "fills", "positions", "trades",
    "equity_snapshots", "bot_state", "strategy_signals", "features", "event_logs",
)


def test_hay_codigo_que_revisar():
    assert SOURCES, "no se encontro el paquete MIG"


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: p.name)
def test_no_importa_ejecucion_ni_riesgo(path: pathlib.Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    importados: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            importados += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            importados.append(node.module)
    for mod in importados:
        for prohibido in PROHIBIDOS:
            assert not (mod == prohibido or mod.startswith(prohibido + ".")), (
                f"{path.name} importa {mod}: el MIG no puede alcanzar {prohibido}"
            )


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: p.name)
def test_no_escribe_en_tablas_ajenas(path: pathlib.Path):
    texto = path.read_text(encoding="utf-8").lower()
    for tabla in TABLAS_AJENAS:
        for verbo in ("insert into", "update", "delete from", "alter table", "drop table"):
            patron = rf"{verbo}\s+{tabla}\b"
            assert not re.search(patron, texto), (
                f"{path.name} contiene '{verbo} {tabla}': el MIG solo escribe en mig_*"
            )


def test_las_migraciones_solo_crean_objetos_mig():
    from medusa.intelligence.mig.migrations import MIG_MIGRATIONS

    assert MIG_MIGRATIONS
    for stmt in MIG_MIGRATIONS:
        low = stmt.lower()
        assert low.startswith("create "), f"migracion no es un CREATE: {stmt}"
        assert "if not exists" in low, f"migracion no es idempotente: {stmt}"
        assert " on mig_" in low, f"migracion toca una tabla ajena: {stmt}"
        for verbo in ("drop ", "alter ", "delete ", "update ", "truncate "):
            assert verbo not in low, f"migracion destructiva: {stmt}"


def test_los_modelos_solo_declaran_tablas_mig():
    from medusa.intelligence.mig import models

    tablas = [v.__tablename__ for v in vars(models).values()
              if hasattr(v, "__tablename__")]
    assert tablas
    assert all(t.startswith("mig_") for t in tablas), tablas


def test_los_modelos_no_tienen_claves_foraneas_contra_el_trading():
    """El grafo OBSERVA el sistema, no lo ata: una poda en `markets` jamas puede
    fallar por culpa de una FK del MIG."""
    from medusa.intelligence.mig import models

    for obj in vars(models).values():
        table = getattr(obj, "__table__", None)
        if table is None:
            continue
        assert not list(table.foreign_keys), f"{table.name} tiene claves foraneas"


def test_el_paquete_no_expone_nada_que_ejecute():
    """La superficie publica no puede contener verbos de ejecucion."""
    import medusa.intelligence.mig as mig

    prohibidos = ("order", "trade_", "execute", "buy", "sell", "place", "position")
    for nombre in mig.__all__:
        low = nombre.lower()
        assert not any(p in low for p in prohibidos), nombre


def test_el_router_solo_escribe_en_build():
    """Unico endpoint no-GET del MIG: la reconstruccion del grafo."""
    from medusa.intelligence.mig.api import router

    metodos = {}
    for route in router.routes:
        metodos[route.path] = set(getattr(route, "methods", set()))
    no_get = {p: m for p, m in metodos.items() if m - {"GET", "HEAD"}}
    assert set(no_get) == {"/mig/build"}, no_get


def test_el_mig_esta_apagado_por_defecto():
    """Toda pieza nueva es opt-in: el runtime debe correr igual sin ella."""
    from medusa.config import Settings

    campos = Settings.model_fields
    assert campos["mig_enabled"].default is False
    # Y el umbral de muestra no puede ser mas laxo que el del asignador: no
    # puede haber dos definiciones de "hay muestra suficiente".
    assert campos["mig_min_samples"].default >= campos["alloc_min_samples"].default
