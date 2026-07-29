"""Guardas del bug del 2026-07-29: un loop caido tiene que RESUCITAR.

Incidente real, medido en el CT202: a las 09:14:21 UTC un timeout transitorio de
Redis ("Timeout reading from redis:6379") mato el loop de heartbeat. `_guarded`
capturaba la excepcion, la registraba y RETORNABA, asi que el loop quedaba
muerto para el resto de la vida del proceso. El engine siguio escaneando y
gestionando posiciones durante NUEVE HORAS sin latir:

  - el watchdog de la API grito "CAIDA DEL SERVICIO" cada 30 min sobre un
    servicio perfectamente vivo, y
  - una caida REAL habria sido indistinguible de esa falsa alarma.

El heartbeat era el caso benigno. El mismo fallo en `_trading_loop` habria
dejado de vigilar las posiciones abiertas en silencio.

Estos tests fijan las dos correcciones: `_guarded` reinicia con backoff, y el
propio heartbeat absorbe un fallo puntual sin morir.
"""

from __future__ import annotations

import asyncio

import pytest

from medusa.config import get_settings
from medusa.engine import Engine


class _Log:
    def __init__(self) -> None:
        self.errors: list[dict] = []
        self.warnings: list[dict] = []

    def info(self, *a, **k): pass
    def debug(self, *a, **k): pass
    def warning(self, event="", **k): self.warnings.append({"event": event, **k})
    def error(self, event="", **k): self.errors.append({"event": event, **k})


class _Notifier:
    def __init__(self) -> None:
        self.alerts: list[tuple] = []

    async def error(self, title, msg):
        self.alerts.append((title, msg))


@pytest.fixture
def engine(monkeypatch):
    """Engine sin red ni BD: solo se ejercita la maquinaria de loops."""
    monkeypatch.setattr(Engine, "__init__", _fake_init)
    eng = Engine(get_settings(), _Log())
    return eng


def _fake_init(self, settings, log):
    self.settings = settings
    self.log = log
    self._stop = asyncio.Event()
    self.notifier = _Notifier()


async def _publish_noop(*a, **k):
    return None


# ------------------------------------------------------- reinicio de loops --
def test_un_loop_que_revienta_se_reinicia(engine, monkeypatch):
    """El corazon del fix: caerse ya no es morir."""
    monkeypatch.setattr("medusa.engine.events.publish_log", _publish_noop)
    monkeypatch.setattr(engine, "LOOP_RETRY_BACKOFF", 0.01)
    intentos = {"n": 0}

    async def loop_roto():
        intentos["n"] += 1
        if intentos["n"] >= 3:
            engine._stop.set()          # tercera vuelta: se corta el test
            return
        raise RuntimeError("Timeout reading from redis:6379")

    asyncio.run(engine._guarded(loop_roto, "heartbeat"))
    assert intentos["n"] == 3, "el loop tenia que haberse reiniciado dos veces"
    assert len(engine.log.errors) == 2
    assert all(e["loop"] == "heartbeat" for e in engine.log.errors)


def test_el_backoff_crece_y_tiene_techo(engine, monkeypatch):
    monkeypatch.setattr("medusa.engine.events.publish_log", _publish_noop)
    monkeypatch.setattr(engine, "LOOP_RETRY_BACKOFF", 1.0)
    monkeypatch.setattr(engine, "LOOP_RETRY_MAX", 4.0)
    esperas: list[float] = []

    async def _sleep(seconds):
        esperas.append(seconds)

    monkeypatch.setattr(engine, "_sleep", _sleep)
    intentos = {"n": 0}

    async def loop_roto():
        intentos["n"] += 1
        if intentos["n"] > 5:
            engine._stop.set()
            return
        raise RuntimeError("boom")

    asyncio.run(engine._guarded(loop_roto, "trading"))
    assert esperas == [1.0, 2.0, 4.0, 4.0, 4.0], esperas


def test_un_loop_que_aguanto_sano_no_hereda_el_castigo(engine, monkeypatch):
    """Un fallo puntual tras horas sanas vuelve a la espera inicial."""
    monkeypatch.setattr("medusa.engine.events.publish_log", _publish_noop)
    monkeypatch.setattr(engine, "LOOP_RETRY_BACKOFF", 1.0)
    monkeypatch.setattr(engine, "LOOP_HEALTHY_AFTER", 0.0)   # todo cuenta como sano
    esperas: list[float] = []

    async def _sleep(seconds):
        esperas.append(seconds)

    monkeypatch.setattr(engine, "_sleep", _sleep)
    intentos = {"n": 0}

    async def loop_roto():
        intentos["n"] += 1
        if intentos["n"] > 3:
            engine._stop.set()
            return
        raise RuntimeError("boom")

    asyncio.run(engine._guarded(loop_roto, "updown"))
    assert esperas == [1.0, 1.0, 1.0], esperas


def test_un_retorno_limpio_no_se_reintenta(engine):
    """Un subsistema apagado por config termina su loop: eso no es una caida."""
    vueltas = {"n": 0}

    async def loop_apagado():
        vueltas["n"] += 1
        return

    asyncio.run(engine._guarded(loop_apagado, "mig"))
    assert vueltas["n"] == 1
    assert engine.log.errors == []


def test_la_cancelacion_se_propaga(engine):
    """Ctrl-C / shutdown tienen que poder parar un loop de verdad."""
    async def loop_cancelado():
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(engine._guarded(loop_cancelado, "trading"))


def test_con_el_stop_puesto_no_se_reintenta(engine, monkeypatch):
    monkeypatch.setattr("medusa.engine.events.publish_log", _publish_noop)
    intentos = {"n": 0}

    async def loop_roto():
        intentos["n"] += 1
        engine._stop.set()              # apagado ordenado durante el fallo
        raise RuntimeError("boom")

    asyncio.run(engine._guarded(loop_roto, "wallet"))
    assert intentos["n"] == 1


def test_un_fallo_al_avisar_no_impide_reintentar(engine, monkeypatch):
    """Si Discord o el log de eventos fallan, el loop se reintenta igual: avisar
    no puede ser mas importante que seguir vivo."""
    async def _publish_roto(*a, **k):
        raise RuntimeError("Postgres caido")

    monkeypatch.setattr("medusa.engine.events.publish_log", _publish_roto)
    monkeypatch.setattr(engine, "LOOP_RETRY_BACKOFF", 0.01)
    intentos = {"n": 0}

    async def loop_roto():
        intentos["n"] += 1
        if intentos["n"] >= 2:
            engine._stop.set()
            return
        raise RuntimeError("boom")

    asyncio.run(engine._guarded(loop_roto, "heartbeat"))
    assert intentos["n"] == 2


def test_la_alerta_de_discord_no_hace_spam(engine, monkeypatch):
    """El log estructurado sale en cada reintento; la alerta, no."""
    monkeypatch.setattr("medusa.engine.events.publish_log", _publish_noop)
    monkeypatch.setattr(engine, "LOOP_RETRY_BACKOFF", 0.01)
    intentos = {"n": 0}

    async def loop_roto():
        intentos["n"] += 1
        if intentos["n"] >= 4:
            engine._stop.set()
            return
        raise RuntimeError("boom")

    asyncio.run(engine._guarded(loop_roto, "trading"))
    assert len(engine.log.errors) == 3          # un log por caida
    assert len(engine.notifier.alerts) == 1     # una sola alerta


# ------------------------------------------------------------- heartbeat --
def test_el_heartbeat_absorbe_un_fallo_puntual_de_redis(engine, monkeypatch):
    """El fallo EXACTO del incidente: un timeout de Redis no puede saltarse el
    latido siguiente."""
    latidos = {"n": 0}

    async def _set_heartbeat(_ts):
        latidos["n"] += 1
        if latidos["n"] == 1:
            raise RuntimeError("Timeout reading from redis:6379")

    async def _publish(*a, **k):
        return None

    async def _sleep(_s):
        if latidos["n"] >= 3:
            engine._stop.set()

    monkeypatch.setattr("medusa.engine.state.set_heartbeat", _set_heartbeat)
    monkeypatch.setattr("medusa.engine.events.publish", _publish)
    monkeypatch.setattr(engine, "_sleep", _sleep)

    asyncio.run(engine._heartbeat_loop())
    assert latidos["n"] >= 3, "el loop tenia que seguir latiendo tras el timeout"
    assert any(w["event"] == "engine.heartbeat_failed" for w in engine.log.warnings)


def test_todos_los_loops_del_engine_pasan_por_la_guarda():
    """Ningun loop puede colarse en `run()` sin la proteccion de `_guarded`."""
    import inspect

    fuente = inspect.getsource(Engine.run)
    loops = [n for n in dir(Engine) if n.endswith("_loop")]
    # El numero es un canario: si aparece un loop nuevo, hay que decidir a
    # conciencia que tambien va guardado (y actualizar esta cuenta).
    assert len(loops) == 8, loops
    for nombre in loops:
        assert f"self.{nombre}" in fuente, f"{nombre} no esta en run()"
        assert f"_guarded(self.{nombre}" in fuente, f"{nombre} no pasa por _guarded"
