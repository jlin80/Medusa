"""El contrato del Hypothesis Engine, verificado sobre el CODIGO FUENTE.

El paquete promete seis cosas: que no opera, que no toca el Risk Manager, que no
escribe fuera de sus tablas, que no acepta hipotesis escritas a mano, que ninguna
hipotesis esta hardcodeada y que no afirma causalidad. Una promesa en un docstring
no vale nada: estos tests la comprueban con el AST, con el texto de las sentencias
y -- las dos ultimas -- con el COMPORTAMIENTO, de modo que romperla tumbe la suite
en vez de descubrirse en produccion.
"""

from __future__ import annotations

import ast
import datetime as dt
import pathlib
import re

import pytest

from medusa.intelligence.hypothesis import features as feat
from medusa.intelligence.hypothesis import generator
from medusa.intelligence.hypothesis.types import Observation

HYP_DIR = (pathlib.Path(__file__).resolve().parents[1]
           / "medusa" / "intelligence" / "hypothesis")
SOURCES = sorted(HYP_DIR.glob("*.py"))
UTC = dt.timezone.utc

# Paquetes que el HE NO puede importar desde su propio codigo. Cada uno es una via
# a mover dinero o a saltarse un candado.
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

# Tablas del sistema de trading. El HE lee varias (es su materia prima) pero no
# puede escribir ninguna.
TABLAS_AJENAS = (
    "markets", "opportunities", "orders", "fills", "positions", "trades",
    "equity_snapshots", "bot_state", "strategy_signals", "features", "event_logs",
)

# Verbos que afirman causalidad. Ninguno puede aparecer en las plantillas del
# generador: el motor observa asociacion y no tiene con que sostener mas.
VERBOS_CAUSALES = (
    "reduce", "aumenta", "mejora", "empeora", "provoca", "causa", "hace que",
    "produce", "genera", "impulsa", "baja el", "sube el", "explica",
)


def test_hay_codigo_que_revisar():
    assert SOURCES, "no se encontro el paquete del Hypothesis Engine"


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
                f"{path.name} importa {mod}: el HE no puede alcanzar {prohibido}"
            )


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: p.name)
def test_no_escribe_en_tablas_ajenas(path: pathlib.Path):
    texto = path.read_text(encoding="utf-8").lower()
    for tabla in TABLAS_AJENAS:
        for verbo in ("insert into", "update", "delete from", "alter table", "drop table"):
            # Se excluye el prefijo propio para no dar un falso positivo sobre las
            # tablas del propio motor (`hyp_observations` contiene "observations").
            patron = rf"{verbo}\s+(?!hyp_){tabla}\b"
            assert not re.search(patron, texto), (
                f"{path.name} contiene '{verbo} {tabla}': el HE solo escribe en hyp_*"
            )


def test_las_migraciones_solo_crean_objetos_hyp():
    from medusa.intelligence.hypothesis.migrations import HYPOTHESIS_MIGRATIONS

    assert HYPOTHESIS_MIGRATIONS
    for stmt in HYPOTHESIS_MIGRATIONS:
        low = stmt.lower()
        assert low.startswith("create "), f"migracion no es un CREATE: {stmt}"
        assert "if not exists" in low, f"migracion no es idempotente: {stmt}"
        assert " on hyp_" in low, f"migracion toca una tabla ajena: {stmt}"
        for verbo in ("drop ", "alter ", "delete ", "update ", "truncate "):
            assert verbo not in low, f"migracion destructiva: {stmt}"


def test_los_modelos_solo_declaran_tablas_hyp():
    from medusa.intelligence.hypothesis import models

    tablas = [v.__tablename__ for v in vars(models).values()
              if hasattr(v, "__tablename__")]
    assert tablas
    assert all(t.startswith("hyp_") for t in tablas), tablas


def test_los_modelos_no_tienen_claves_foraneas_contra_el_trading():
    """El motor OBSERVA el sistema, no lo ata: una poda en cualquier tabla del
    trading jamas puede fallar por culpa de una fila del HE."""
    from medusa.intelligence.hypothesis import models

    for obj in vars(models).values():
        table = getattr(obj, "__table__", None)
        if table is None:
            continue
        assert not list(table.foreign_keys), f"{table.name} tiene claves foraneas"


def test_el_paquete_no_expone_nada_que_ejecute():
    """La superficie publica no puede contener verbos de ejecucion."""
    import medusa.intelligence.hypothesis as hyp

    prohibidos = ("order", "execute", "buy", "sell", "place", "position", "signal")
    for nombre in hyp.__all__:
        low = nombre.lower()
        assert not any(p in low for p in prohibidos), nombre


def test_el_router_solo_escribe_en_run():
    """Unico endpoint no-GET del HE: la pasada de analisis.

    En particular, NO hay un POST para crear una hipotesis: seria la puerta por la
    que entraria la primera hipotesis escrita por una persona, y con ella se
    perderia la unica garantia que hace interesante a lo guardado.
    """
    from medusa.intelligence.hypothesis.api import router

    metodos = {}
    for route in router.routes:
        metodos[route.path] = set(getattr(route, "methods", set()))
    no_get = {p: m for p, m in metodos.items() if m - {"GET", "HEAD"}}
    assert set(no_get) == {"/hypotheses/run"}, no_get


def test_el_motor_esta_apagado_por_defecto():
    """Toda pieza nueva es opt-in: el runtime debe correr igual sin ella."""
    from medusa.config import Settings

    campos = Settings.model_fields
    assert campos["hypothesis_enabled"].default is False
    # Y una hipotesis necesita muestra fuera de muestra para tener veredicto: con
    # un umbral bajo, el motor validaria con cuatro observaciones nuevas.
    assert campos["hypothesis_min_test_samples"].default >= 30
    # El FDR tiene que estar activo: alpha=1.0 aceptaria todos los contrastes.
    assert 0.0 < campos["hypothesis_alpha"].default <= 0.1


# ------------------------------------------------------------------------------
# LAS DOS REGLAS QUE DEFINEN EL MOTOR
# ------------------------------------------------------------------------------
def test_las_plantillas_no_nombran_ninguna_variable_del_dominio():
    """La gramatica tiene que ser CIEGA al dominio.

    Si una plantilla nombrase `spread`, `roi` o `sports`, esa hipotesis estaria
    escrita en el codigo aunque estuviera partida en trozos. Las plantillas solo
    pueden tener huecos.
    """
    dominio = ("spread", "roi", "edge", "wallet", "sport", "consenso", "consensus",
               "liquidez", "liquidity", "categoria", "calibrat", "mercado",
               "market", "estrategia", "strategy", "precio", "price")
    for forma, variantes in generator._TEMPLATES.items():
        for plantilla in variantes:
            low = plantilla.lower()
            for palabra in dominio:
                assert palabra not in low, (
                    f"la plantilla de {forma} nombra «{palabra}»: eso es una "
                    f"hipotesis escrita a mano -> {plantilla}"
                )
            assert "{predictor}" in plantilla and "{outcome}" in plantilla, plantilla


def test_las_plantillas_no_afirman_causalidad():
    """El limite epistemico, comprobado sobre la prosa que el motor genera."""
    for forma, variantes in generator._TEMPLATES.items():
        for plantilla in variantes:
            low = plantilla.lower()
            for verbo in VERBOS_CAUSALES:
                assert verbo not in low, (
                    f"la plantilla de {forma} usa el verbo causal «{verbo}»: el "
                    f"motor observa asociacion y no puede sostener mas -> {plantilla}"
                )


def _observaciones(nombre_predictor: str, nombre_outcome: str, n: int = 200):
    """Datos con una relacion monotona fuerte y nombres de columna arbitrarios."""
    base = dt.datetime(2026, 1, 1, tzinfo=UTC)
    return [
        Observation(
            source="s", entity=f"e{i}", ts=base + dt.timedelta(hours=i),
            features={nombre_predictor: float(i)},
            outcomes={nombre_outcome: float(-i) + (i % 3)},
        )
        for i in range(n)
    ]


def test_el_enunciado_sale_DE_LOS_DATOS_y_no_del_codigo():
    """La prueba de comportamiento de «ninguna hipotesis esta hardcodeada».

    Se corre el motor sobre dos conjuntos identicos salvo en el NOMBRE de las
    columnas. Si las hipotesis estuvieran escritas en el codigo, la descripcion
    seria la misma en los dos casos. Al salir de los datos, cambia con ellos.
    """
    frases = []
    for pred, out in (("alpha_metric", "beta_result"),
                      ("zzz_condicion", "www_resultado")):
        obs = _observaciones(pred, out)
        variables = feat.discover_variables(obs, min_coverage=0.5, min_distinct=5)
        propuesta = generator.propose(
            obs, variables, source="s", min_samples=20,
            now=dt.datetime(2026, 6, 1, tzinfo=UTC))
        assert propuesta.hypotheses, "no se propuso nada sobre una relacion evidente"
        descripcion = propuesta.hypotheses[0].description
        # Los nombres reales de las columnas aparecen en la frase generada.
        assert pred.replace("_", " ") in descripcion, descripcion
        assert out.replace("_", " ") in descripcion, descripcion
        frases.append(descripcion)

    assert frases[0] != frases[1], (
        "la descripcion no cambio al renombrar las columnas: eso significaria que "
        "el enunciado viene del codigo y no de los datos"
    )


def test_ninguna_hipotesis_nace_con_evidencia():
    """La valla, comprobada en el momento del nacimiento.

    Una hipotesis recien propuesta tiene `confidence` 0.0 y `sample_count` 0 SIN
    EXCEPCION, por brutal que sea el efecto que la genero: la evidencia que la
    propuso es la misma que la eligio entre cientos de candidatas.
    """
    obs = _observaciones("cond", "res")
    variables = feat.discover_variables(obs, min_coverage=0.5, min_distinct=5)
    propuesta = generator.propose(
        obs, variables, source="s", min_samples=20,
        now=dt.datetime(2026, 6, 1, tzinfo=UTC))
    assert propuesta.hypotheses
    for h in propuesta.hypotheses:
        assert h.status == "proposed", h.status
        assert h.confidence == 0.0, h.confidence
        assert h.sample_count == 0, h.sample_count
        # Y el efecto del descubrimiento SI se guarda: es el registro de por que
        # se propuso, y lo que despues se comparara con la replicacion.
        assert h.discovery.n > 0
        assert h.tested_in_pass > 0, "sin el denominador no se puede juzgar nada"


def test_el_id_no_depende_de_la_direccion():
    """El candado contra el blanqueo de hipotesis.

    "A mayor X, mayor Y" y "a mayor X, menor Y" son la MISMA hipotesis sobre la
    misma relacion. Si la direccion entrase en el hash, un cambio de signo crearia
    una hipotesis virgen y el motor podria tirar la moneda hasta que saliera cara.
    """
    from medusa.intelligence.hypothesis.types import Hypothesis

    sube = Hypothesis(form="monotone", source="s", predictor="x",
                      outcome="y", direction=1)
    baja = Hypothesis(form="monotone", source="s", predictor="x",
                      outcome="y", direction=-1)
    assert sube.id == baja.id, "un cambio de signo no puede crear una hipotesis nueva"


def test_los_estados_son_exactamente_los_cuatro_del_enunciado():
    from medusa.intelligence.hypothesis.types import STATUSES

    assert STATUSES == ("proposed", "testing", "validated", "rejected")


def test_la_hipotesis_publica_los_siete_campos_del_contrato():
    from medusa.intelligence.hypothesis.types import Hypothesis

    salida = Hypothesis(form="monotone", source="s", predictor="x",
                        outcome="y", direction=1).to_dict()
    assert {"id", "description", "status", "confidence", "sample_count",
            "created_at", "updated_at"} <= set(salida)
