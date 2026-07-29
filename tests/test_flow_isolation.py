"""El contrato del Information Flow Engine, verificado sobre el CODIGO FUENTE.

El paquete promete cuatro cosas: que no opera, que no toca el Risk Manager, que
no escribe fuera de sus tablas y que mide propagacion, jamas causalidad. Una
promesa en un docstring no vale nada: estos tests la comprueban con el AST y con
el texto de las sentencias, de modo que romperla tumbe la suite en vez de
descubrirse en produccion.
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest

FLOW_DIR = pathlib.Path(__file__).resolve().parents[1] / "medusa" / "intelligence" / "flow"
SOURCES = sorted(FLOW_DIR.glob("*.py"))

# Paquetes que el IFE NO puede importar desde su propio codigo. Cada uno es una
# via a mover dinero o a saltarse un candado.
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

# Tablas del sistema de trading. El IFE lee algunas (via los repositorios
# existentes) pero no puede escribir ninguna. Ojo con `trades`: la tabla de
# operaciones de Medusa. La cinta publica vive en `flow_trades`, que es otra.
TABLAS_AJENAS = (
    "markets", "opportunities", "orders", "fills", "positions", "trades",
    "equity_snapshots", "bot_state", "strategy_signals", "features", "event_logs",
)


def test_hay_codigo_que_revisar():
    assert SOURCES, "no se encontro el paquete del Information Flow Engine"


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
                f"{path.name} importa {mod}: el IFE no puede alcanzar {prohibido}"
            )


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: p.name)
def test_no_escribe_en_tablas_ajenas(path: pathlib.Path):
    texto = path.read_text(encoding="utf-8").lower()
    for tabla in TABLAS_AJENAS:
        for verbo in ("insert into", "update", "delete from", "alter table", "drop table"):
            # `flow_trades` contiene "trades": se excluye el prefijo propio para
            # no dar un falso positivo sobre las tablas del propio motor.
            patron = rf"{verbo}\s+(?!flow_){tabla}\b"
            assert not re.search(patron, texto), (
                f"{path.name} contiene '{verbo} {tabla}': el IFE solo escribe en flow_*"
            )


def test_las_migraciones_solo_crean_objetos_flow():
    from medusa.intelligence.flow.migrations import FLOW_MIGRATIONS

    assert FLOW_MIGRATIONS
    for stmt in FLOW_MIGRATIONS:
        low = stmt.lower()
        assert low.startswith("create "), f"migracion no es un CREATE: {stmt}"
        assert "if not exists" in low, f"migracion no es idempotente: {stmt}"
        assert " on flow_" in low, f"migracion toca una tabla ajena: {stmt}"
        for verbo in ("drop ", "alter ", "delete ", "update ", "truncate "):
            assert verbo not in low, f"migracion destructiva: {stmt}"


def test_los_modelos_solo_declaran_tablas_flow():
    from medusa.intelligence.flow import models

    tablas = [v.__tablename__ for v in vars(models).values()
              if hasattr(v, "__tablename__")]
    assert tablas
    assert all(t.startswith("flow_") for t in tablas), tablas


def test_los_modelos_no_tienen_claves_foraneas_contra_el_trading():
    """El motor OBSERVA el sistema, no lo ata: una poda en `markets` jamas puede
    fallar por culpa de una fila del IFE."""
    from medusa.intelligence.flow import models

    for obj in vars(models).values():
        table = getattr(obj, "__table__", None)
        if table is None:
            continue
        assert not list(table.foreign_keys), f"{table.name} tiene claves foraneas"


def test_el_paquete_no_expone_nada_que_ejecute():
    """La superficie publica no puede contener verbos de ejecucion."""
    import medusa.intelligence.flow as flow

    prohibidos = ("order", "execute", "buy", "sell", "place", "position", "signal")
    for nombre in flow.__all__:
        low = nombre.lower()
        assert not any(p in low for p in prohibidos), nombre


def test_el_router_solo_escribe_en_run():
    """Unico endpoint no-GET del IFE: la pasada de analisis."""
    from medusa.intelligence.flow.api import router

    metodos = {}
    for route in router.routes:
        metodos[route.path] = set(getattr(route, "methods", set()))
    no_get = {p: m for p, m in metodos.items() if m - {"GET", "HEAD"}}
    assert set(no_get) == {"/flow/run"}, no_get


def test_el_motor_esta_apagado_por_defecto():
    """Toda pieza nueva es opt-in: el runtime debe correr igual sin ella."""
    from medusa.config import Settings

    campos = Settings.model_fields
    assert campos["flow_enabled"].default is False
    # Y una cascada necesita al menos tres participantes: con dos, cualquier par
    # de entradas seguidas seria una "cascada".
    assert campos["flow_min_participants"].default >= 3


def test_ningun_tipo_publico_afirma_causalidad():
    """El contrato epistemico, comprobado sobre los campos reales de la salida.

    Si algun dia aparece un campo llamado `caused_by` o `influence`, el motor
    habra dejado de medir y habra empezado a inventar: este test lo impide.
    """
    from medusa.intelligence.flow.types import (
        Cascade,
        MarketFlowMetrics,
        PropagationEvent,
        WalletFlowMetrics,
    )

    prohibidos = ("caused", "causal", "influence", "signal", "recommend",
                  "should_", "action")
    for tipo in (Cascade, PropagationEvent, WalletFlowMetrics, MarketFlowMetrics):
        campos = getattr(tipo, "__annotations__", {})
        for nombre in campos:
            assert not any(p in nombre.lower() for p in prohibidos), f"{tipo.__name__}.{nombre}"


def test_toda_metrica_de_wallet_viaja_con_su_muestra():
    """Un score sin n no es evidencia. La salida tiene que traer siempre el
    tamaño de muestra y el flag que dice si se puede leer como tal."""
    from medusa.intelligence.flow.types import WalletFlowMetrics

    salida = WalletFlowMetrics(wallet="0xa").to_dict()
    assert {"n_cascades", "n_early", "n_late", "enough_samples",
            "leadership_lower", "early_lower", "late_lower"} <= set(salida)


def test_el_feed_solo_sabe_hacer_peticiones_de_lectura():
    """Las tres cosas que harian falta para operar (una clave, una firma y un
    verbo de escritura) no estan en el feed.

    Se comprueba sobre el AST y no sobre el texto: los docstrings del paquete
    nombran a proposito lo que NO hace, y un `grep` los confundiria con codigo.
    """
    tree = ast.parse((FLOW_DIR / "feed.py").read_text(encoding="utf-8"))
    llamadas = {
        node.func.attr for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert not (llamadas & {"post", "put", "patch", "delete", "sign",
                            "sign_message", "create_order", "post_order"}), llamadas
    nombres = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    nombres |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert not any("private" in n.lower() or "secret" in n.lower() for n in nombres)
