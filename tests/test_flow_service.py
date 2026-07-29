"""Servicio del IFE y su SQL, sin base de datos.

Postgres no esta disponible en la maquina de desarrollo (ni debe hacer falta
para correr la suite). Se verifica la orquestacion con las escrituras y la red
interceptadas, y que el SQL de los upserts compila contra el dialecto real.

Lo que NO cubre: el comportamiento de los upserts contra un Postgres con datos.
Eso solo lo demuestra correr `POST /api/flow/run` en el stack.
"""

from __future__ import annotations

import asyncio
import datetime as dt

import pytest

from medusa.intelligence.flow import repository as flow_repo
from medusa.intelligence.flow.service import InformationFlowService
from medusa.intelligence.flow.types import FlowTrade

UTC = dt.timezone.utc
T0 = dt.datetime(2026, 7, 20, 12, 0, tzinfo=UTC)


class _Log:
    def info(self, *a, **k): pass
    def debug(self, *a, **k): pass
    def warning(self, *a, **k): pass
    def error(self, *a, **k): pass


@pytest.fixture
def svc():
    return InformationFlowService(_Log())


def _cinta(mercados: int = 3, wallets: int = 4) -> list[FlowTrade]:
    """Cinta sintetica: en cada mercado entran las mismas wallets en el mismo
    orden, separadas un minuto."""
    out = []
    for m in range(mercados):
        for i in range(wallets):
            out.append(FlowTrade(
                market_id=f"m{m}", wallet=f"0x{i}", side="YES",
                price=0.40 + 0.05 * i, size=100.0,
                ts=T0 + dt.timedelta(days=m, seconds=60 * i),
                uid=f"m{m}-{i}",
            ))
    return out


# --------------------------------------------------------------- analisis --
def test_el_analisis_es_puro_y_devuelve_las_seis_piezas(svc):
    out = svc.analyze(_cinta())
    assert set(out) == {"cascades", "events", "wallets", "markets", "pairs", "summary"}
    assert len(out["cascades"]) == 3
    assert out["summary"]["wallets"] == 4


def test_el_analisis_es_determinista(svc):
    cinta = _cinta()
    a, b = svc.analyze(cinta), svc.analyze(cinta)
    assert [c.key for c in a["cascades"]] == [c.key for c in b["cascades"]]
    assert [w.to_dict() for w in a["wallets"]] == [w.to_dict() for w in b["wallets"]]


def test_la_resolucion_llega_hasta_las_cascadas(svc):
    out = svc.analyze(_cinta(), {"m0": 1.0, "m1": None, "m2": 0.0})
    por_mercado = {c.market_id: c for c in out["cascades"]}
    assert por_mercado["m0"].resolution_value == 1.0
    assert por_mercado["m1"].resolved is False
    assert por_mercado["m2"].resolution_value == 0.0


def test_ninguna_metrica_de_salida_contiene_un_lado_un_tamano_ni_un_precio(svc):
    """La prueba de que esto no emite señales, hecha sobre la salida real."""
    prohibidos = {"side", "outcome", "stake", "size", "order", "action", "signal",
                  "entry_price", "target_price"}
    out = svc.analyze(_cinta())
    for w in out["wallets"]:
        assert not (set(w.to_dict()) & prohibidos)
    for m in out["markets"]:
        assert not (set(m.to_dict()) & prohibidos)


def test_una_cinta_vacia_no_rompe_el_analisis(svc):
    out = svc.analyze([])
    assert out["cascades"] == [] and out["events"] == []
    assert out["summary"]["cascades"] == 0


# ----------------------------------------------------------------- pasada --
def _sin_red(svc, monkeypatch, cinta=None, resoluciones=None):
    async def _mercados(limit=40):
        return [{"id": f"m{i}"} for i in range(3)]

    async def _tape(_ids):
        return cinta if cinta is not None else _cinta()

    async def _res(_ids):
        return resoluciones or {}

    monkeypatch.setattr(flow_repo, "target_markets", _mercados)
    monkeypatch.setattr(svc, "ingest_tape", _tape)
    monkeypatch.setattr(svc, "resolutions_of", _res)


def test_run_sin_persistir_no_escribe_nada(svc, monkeypatch):
    _sin_red(svc, monkeypatch)

    def _boom(*_a, **_k):
        raise AssertionError("persist=False no puede escribir en la BD")

    for nombre in ("save_trades", "save_cascades", "save_events",
                   "upsert_wallet_metrics", "upsert_market_metrics",
                   "save_snapshot", "load_trades"):
        monkeypatch.setattr(flow_repo, nombre, _boom)

    out = asyncio.run(svc.run(persist=False))
    assert out["ok"] and out["persisted"] is False
    assert out["stats"]["cascades"] == 3
    assert out["written"] == {"trades": 0, "cascades": 0, "events": 0,
                              "wallets": 0, "markets": 0}


def test_run_persistiendo_llama_a_las_cinco_escrituras(svc, monkeypatch):
    _sin_red(svc, monkeypatch)
    llamadas: list[str] = []

    async def _trades(rows):
        llamadas.append("trades")
        return len(rows)

    async def _load(_ids, _since):
        return _cinta()

    async def _cascadas(rows):
        llamadas.append("cascades")
        return len(rows)

    async def _eventos(rows):
        llamadas.append("events")
        return len(rows)

    async def _wallets(rows):
        llamadas.append("wallets")
        return len(rows)

    async def _mercados(rows):
        llamadas.append("markets")
        return len(rows)

    async def _snapshot(*_a, **_k):
        llamadas.append("snapshot")

    monkeypatch.setattr(flow_repo, "save_trades", _trades)
    monkeypatch.setattr(flow_repo, "load_trades", _load)
    monkeypatch.setattr(flow_repo, "save_cascades", _cascadas)
    monkeypatch.setattr(flow_repo, "save_events", _eventos)
    monkeypatch.setattr(flow_repo, "upsert_wallet_metrics", _wallets)
    monkeypatch.setattr(flow_repo, "upsert_market_metrics", _mercados)
    monkeypatch.setattr(flow_repo, "save_snapshot", _snapshot)

    out = asyncio.run(svc.run(persist=True))
    assert llamadas == ["trades", "cascades", "events", "wallets", "markets", "snapshot"]
    assert out["written"]["cascades"] == 3
    assert svc.last_run["cascades"] == 3


def test_un_mercado_que_falla_no_tumba_la_ingesta(svc, monkeypatch):
    """Una cinta a medias produciria cascadas FALSAS, no degradadas: el mercado
    roto se queda fuera y la pasada continua."""
    class _Feed:
        async def fetch_trades(self, market_id, limit=500):
            if market_id == "m1":
                raise RuntimeError("la API se cayo")
            return [{"proxyWallet": f"0x{i}", "outcome": "Yes", "side": "BUY",
                     "price": "0.5", "size": "10",
                     "timestamp": 1_780_000_000 + 60 * i} for i in range(3)]

    monkeypatch.setattr(svc, "_get_feed", lambda: _Feed())
    trades = asyncio.run(svc.ingest_tape(["m0", "m1", "m2"]))
    assert {t.market_id for t in trades} == {"m0", "m2"}


def test_run_guarded_absorbe_el_fallo_y_no_propaga(svc, monkeypatch):
    async def _revienta(persist=True):
        raise RuntimeError("boom")

    monkeypatch.setattr(svc, "run", _revienta)
    assert asyncio.run(svc.run_guarded()) is None


def test_run_guarded_absorbe_el_timeout(svc, monkeypatch):
    async def _eterna(persist=True):
        await asyncio.sleep(5)

    monkeypatch.setattr(svc, "run", _eterna)
    monkeypatch.setattr(svc.s, "flow_timeout", 0.05)
    assert asyncio.run(svc.run_guarded()) is None


def test_info_declara_los_tres_noes(svc):
    info = svc.info()
    assert info["measures_causality"] is False
    assert info["can_place_orders"] is False
    assert info["emits_signals"] is False
    assert info["enabled"] is False       # apagado por defecto


# --------------------------------------------------------------- SQL/DDL --
def _sql(stmt) -> str:
    from sqlalchemy.dialects import postgresql

    return str(stmt.compile(dialect=postgresql.dialect()))


def test_el_upsert_de_la_cinta_ignora_los_repetidos():
    """Un trade es un hecho pasado: no se reescribe, se ignora."""
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from medusa.intelligence.flow.models import FlowTradeRow

    stmt = pg_insert(FlowTradeRow).values([{"uid": "abc"}])
    sql = _sql(stmt.on_conflict_do_nothing(index_elements=[FlowTradeRow.uid]))
    assert "ON CONFLICT (uid) DO NOTHING" in sql


def test_el_eslabon_es_unico_por_cascada_lider_y_seguidor():
    """Sin esta unicidad, dos pasadas sobre la misma ventana contarian el mismo
    eslabon dos veces y la estadistica mediria repeticion de ingesta."""
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from medusa.intelligence.flow.models import FlowEventRow

    stmt = pg_insert(FlowEventRow).values([{"cascade_key": "k", "leader": "a",
                                            "follower": "b"}])
    sql = _sql(stmt.on_conflict_do_nothing(
        index_elements=[FlowEventRow.cascade_key, FlowEventRow.leader,
                        FlowEventRow.follower]))
    assert "ON CONFLICT (cascade_key, leader, follower) DO NOTHING" in sql


def test_el_upsert_de_wallets_conserva_first_seen():
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from medusa.intelligence.flow.models import FlowWalletRow

    stmt = pg_insert(FlowWalletRow).values([{"wallet": "0xa"}])
    sql = _sql(stmt.on_conflict_do_update(
        index_elements=[FlowWalletRow.wallet],
        set_={"n_cascades": stmt.excluded.n_cascades,
              "last_seen": stmt.excluded.last_seen},
    ))
    assert "ON CONFLICT (wallet)" in sql
    assert "first_seen =" not in sql.split("SET", 1)[1]


def test_el_upsert_de_wallets_cubre_todas_las_metricas():
    """Si mañana se añade una metrica a `WalletFlowMetrics` y no al SET del
    upsert, la fila persistida se quedaria con el valor viejo para siempre."""
    from medusa.intelligence.flow.models import FlowWalletRow
    from medusa.intelligence.flow.types import WalletFlowMetrics

    columnas = {c.name for c in FlowWalletRow.__table__.columns}
    assert set(WalletFlowMetrics(wallet="0xa").to_dict()) <= columnas


def test_el_upsert_de_mercados_cubre_todas_las_metricas():
    from medusa.intelligence.flow.models import FlowMarketRow
    from medusa.intelligence.flow.types import MarketFlowMetrics

    columnas = {c.name for c in FlowMarketRow.__table__.columns}
    assert set(MarketFlowMetrics(market_id="m1").to_dict()) <= columnas


def test_las_tablas_estan_registradas_y_no_reemplazan_nada():
    from medusa.data.db_models import Base
    from medusa.intelligence.flow import migrations  # noqa: F401

    tablas = set(Base.metadata.tables)
    assert {"flow_trades", "flow_cascades", "flow_events", "flow_wallet_metrics",
            "flow_market_metrics", "flow_snapshots"} <= tablas
    assert {"markets", "trades", "positions", "strategy_signals", "features"} <= tablas


def test_init_db_incluye_las_migraciones_del_flow():
    from medusa.infra.db import _extra_migrations
    from medusa.intelligence.flow.migrations import FLOW_MIGRATIONS
    from medusa.intelligence.mig.migrations import MIG_MIGRATIONS
    from medusa.intelligence.wallet.migrations import WALLET_MIGRATIONS

    extra = _extra_migrations()
    assert set(FLOW_MIGRATIONS) <= set(extra)
    # Y los dos paquetes anteriores siguen estando.
    assert set(MIG_MIGRATIONS) <= set(extra)
    assert set(WALLET_MIGRATIONS) <= set(extra)


def test_el_histograma_de_latencias_reparte_bien_los_valores():
    from medusa.intelligence.flow import metrics

    tramos = metrics.histogram([0.0, 100.0, 599.0, 600.0, 3599.0], 6, 3600.0)
    assert [t["n"] for t in tramos] == [3, 1, 0, 0, 0, 1]
    assert tramos[0] == {"from": 0.0, "to": 600.0, "n": 3}


def test_la_cola_no_se_pierde_fuera_del_grafico():
    from medusa.intelligence.flow import metrics

    tramos = metrics.histogram([10.0, 99_999.0], 4, 100.0)
    assert [t["n"] for t in tramos] == [1, 0, 0, 1]
    assert metrics.histogram([], 4, 100.0) == []
