"""El contrato de Wallet Intelligence, verificado sobre el CODIGO FUENTE.

"No es copy trading" y "no puede operar" son afirmaciones comprobables, no notas
al pie. Estos tests recorren el AST y el texto del paquete para que romper el
contrato tumbe la suite en vez de descubrirse en produccion.
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest

PKG = pathlib.Path(__file__).resolve().parents[1] / "medusa" / "intelligence" / "wallet"
SOURCES = sorted(PKG.rglob("*.py"))

# Paquetes que Wallet Intelligence NO puede alcanzar. Cada uno es una via a
# mover dinero o a saltarse un candado.
PROHIBIDOS = (
    "medusa.execution",
    "medusa.trading",
    "medusa.risk",
    "medusa.strategies",
    "medusa.allocation",
    "medusa.updown",
    "py_clob_client",
)

# Tablas del sistema de trading: se leen (via los repositorios existentes), no
# se escriben.
TABLAS_AJENAS = (
    "markets", "opportunities", "orders", "fills", "positions", "trades",
    "equity_snapshots", "bot_state", "strategy_signals", "features", "event_logs",
)


def test_hay_codigo_que_revisar():
    assert SOURCES, "no se encontro el paquete de Wallet Intelligence"


def test_existen_los_cinco_subpaquetes_pedidos():
    for nombre in ("wallet_dna", "wallet_scoring", "wallet_reputation",
                   "wallet_clusters", "wallet_similarity"):
        assert (PKG / nombre / "__init__.py").exists(), nombre


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
                f"{path.name} importa {mod}: no puede alcanzar {prohibido}"
            )


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: p.name)
def test_no_escribe_en_tablas_ajenas(path: pathlib.Path):
    texto = path.read_text(encoding="utf-8").lower()
    for tabla in TABLAS_AJENAS:
        for verbo in ("insert into", "update", "delete from", "alter table", "drop table"):
            assert not re.search(rf"{verbo}\s+{tabla}\b", texto), (
                f"{path.name}: '{verbo} {tabla}' — solo se escribe en wi_*"
            )


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: p.name)
def test_ninguna_funcion_publica_suena_a_ejecucion(path: pathlib.Path):
    """Ni un `place_order`, ni un `copy_trade`, ni un `follow_wallet`."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    prohibidos = ("place_order", "submit_order", "copy_trade", "copy_wallet",
                  "follow_wallet", "mirror_", "execute_trade", "open_position",
                  "close_position", "send_order")
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            low = node.name.lower()
            assert not any(p in low for p in prohibidos), f"{path.name}:{node.name}"


def test_las_migraciones_solo_crean_objetos_wi():
    from medusa.intelligence.wallet.migrations import WALLET_MIGRATIONS

    assert WALLET_MIGRATIONS
    for stmt in WALLET_MIGRATIONS:
        low = stmt.lower()
        assert low.startswith("create "), stmt
        assert "if not exists" in low, stmt
        assert " on wi_" in low, stmt
        for verbo in ("drop ", "alter ", "delete ", "update ", "truncate "):
            assert verbo not in low, stmt


def test_los_modelos_solo_declaran_tablas_wi_y_sin_claves_foraneas():
    from medusa.intelligence.wallet import models

    tablas = []
    for obj in vars(models).values():
        table = getattr(obj, "__table__", None)
        if table is None:
            continue
        tablas.append(table.name)
        assert not list(table.foreign_keys), f"{table.name} tiene claves foraneas"
    assert tablas and all(t.startswith("wi_") for t in tablas), tablas


def test_el_router_solo_escribe_en_build():
    from medusa.intelligence.wallet.api import router

    metodos = {r.path: set(getattr(r, "methods", set())) for r in router.routes}
    no_get = {p: m for p, m in metodos.items() if m - {"GET", "HEAD"}}
    assert set(no_get) == {"/wallets/build"}, no_get


def test_el_modulo_de_features_no_emite_el_lado_de_nadie():
    """La linea que separa una feature de una señal de copia: emitir el LADO
    (YES/NO) de las wallets buenas seria copy trading disfrazado de numero."""
    fuente = (PKG / "module.py").read_text(encoding="utf-8")
    tree = ast.parse(fuente)
    nombres_feature: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "_feature":
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    nombres_feature.append(arg.value)
    assert nombres_feature, "el modulo no emite ninguna feature"
    for nombre in nombres_feature:
        low = nombre.lower()
        assert not any(p in low for p in ("side", "outcome", "yes", "no_", "signal",
                                          "action", "buy", "sell")), nombre


def test_el_subsistema_esta_apagado_por_defecto():
    from medusa.config import Settings

    campos = Settings.model_fields
    assert campos["wallet_intel_enabled"].default is False
    # El umbral de muestra no puede ser mas laxo que el del asignador.
    assert campos["wallet_min_samples"].default >= campos["alloc_min_samples"].default


def _docstring_ids(tree: ast.AST) -> set[int]:
    """id() de las constantes que son docstring, para excluirlas del barrido."""
    out: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", [])
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                and isinstance(body[0].value.value, str):
            out.add(id(body[0].value))
    return out


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: p.name)
def test_no_hay_etiquetas_cualitativas_hardcodeadas(path: pathlib.Path):
    """Ni 'smart money', ni 'ballena', ni 'novato': el ADN es solo numeros.

    Se barre el AST -- literales de cadena que NO sean docstring, mas nombres de
    funcion, clase y variable. Comentarios y docstrings quedan fuera a
    proposito: ahi estas palabras aparecen justamente para explicar por que NO
    se usan.
    """
    etiquetas = ("smart money", "smart_money", "whale", "ballena", "novato",
                 "degen", "expert_label", "tier_a", "tier_b", "elite")
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = _docstring_ids(tree)
    sospechosos: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and id(node) not in docstrings:
            sospechosos.append(node.value.lower())
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            sospechosos.append(node.name.lower())
        elif isinstance(node, ast.Name):
            sospechosos.append(node.id.lower())
        elif isinstance(node, ast.Attribute):
            sospechosos.append(node.attr.lower())

    for texto in sospechosos:
        for etiqueta in etiquetas:
            assert etiqueta not in texto, f"{path.name}: etiqueta '{etiqueta}' en '{texto}'"


def test_el_adn_declara_exactamente_19_metricas_numericas():
    from medusa.intelligence.wallet.types import DNA_DEFINITIONS, DNA_FEATURES

    assert len(DNA_FEATURES) == 19
    assert len(set(DNA_FEATURES)) == 19
    assert set(DNA_DEFINITIONS) == set(DNA_FEATURES)
    # Las 19 pedidas, con sus nombres.
    esperadas = {
        "roi_historical", "roi_recent", "sharpe", "win_rate", "consistency",
        "trade_frequency", "entry_timing", "exit_timing", "liquidity_preference",
        "spread_preference", "category_expertise", "conviction", "alpha", "beta",
        "drawdown", "volatility", "reliability", "freshness", "decay",
    }
    assert set(DNA_FEATURES) == esperadas
