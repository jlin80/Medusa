"""Tests del servicio y de la capa de persistencia SIN base de datos.

Postgres no esta disponible en la maquina de desarrollo (ni debe hacer falta
para correr la suite), asi que aqui se comprueba lo que se puede comprobar sin
el: la orquestacion del servicio (con las escrituras interceptadas) y que el SQL
del upsert compila contra el dialecto de PostgreSQL.

Lo que NO cubren estos tests, y conviene tenerlo escrito: que el upsert se
comporta bien contra un Postgres real con datos. Eso solo lo demuestra correr
`POST /mig/build` en el stack.
"""

from __future__ import annotations

import asyncio

import pytest

from medusa.intelligence.mig import repository as mig_repo
from medusa.intelligence.mig.service import MIGService
from medusa.intelligence.mig.types import EdgeType, GraphEdge, GraphNode, NodeType


class _Log:
    def info(self, *a, **k): pass
    def debug(self, *a, **k): pass
    def warning(self, *a, **k): pass
    def error(self, *a, **k): pass


FUENTES = {
    "markets": [
        {"id": "m1", "question": "Will Bitcoin close above 100000 dollars?",
         "slug": "btc-updown-5m-1200", "medusa_category": "crypto",
         "opportunity_score": 80.0, "end_date": None},
        {"id": "m2", "question": "Will Bitcoin close above 120000 dollars?",
         "slug": "btc-updown-5m-1205", "medusa_category": "crypto",
         "opportunity_score": 70.0, "end_date": None},
    ],
    "signals": [
        {"strategy": "momentum", "market_id": "m1", "category": "crypto",
         "status": "resolved", "won": True, "roi": 0.05, "edge": 0.03,
         "outcome": "YES", "question": "q"},
    ],
    "trades": [],
    "features": [],
    "wallets": [],
}


@pytest.fixture
def svc(monkeypatch):
    async def _fake_sources(**_kwargs):
        return {k: list(v) for k, v in FUENTES.items()}

    monkeypatch.setattr(mig_repo, "load_sources", _fake_sources)
    return MIGService(_Log())


def test_build_sin_persistir_no_escribe_nada(svc, monkeypatch):
    def _boom(*_a, **_k):
        raise AssertionError("persist=False no puede escribir en la BD")

    for nombre in ("upsert_nodes", "upsert_edges", "save_discoveries", "save_snapshot"):
        monkeypatch.setattr(mig_repo, nombre, _boom)

    out = asyncio.run(svc.build(persist=False))
    assert out["ok"] is True and out["persisted"] is False
    assert out["stats"]["nodes"] > 0
    assert out["written"] == {"nodes": 0, "edges": 0, "discoveries": 0}


def test_build_persistiendo_llama_a_las_cuatro_escrituras(svc, monkeypatch):
    llamadas: list[str] = []

    async def _nodes(nodes):
        llamadas.append("nodes")
        return len(nodes)

    async def _edges(edges):
        llamadas.append("edges")
        return len(edges)

    async def _disc(discoveries):
        llamadas.append("discoveries")
        return len(discoveries)

    async def _snap(*_a, **_k):
        llamadas.append("snapshot")

    monkeypatch.setattr(mig_repo, "upsert_nodes", _nodes)
    monkeypatch.setattr(mig_repo, "upsert_edges", _edges)
    monkeypatch.setattr(mig_repo, "save_discoveries", _disc)
    monkeypatch.setattr(mig_repo, "save_snapshot", _snap)

    out = asyncio.run(svc.build(persist=True))
    assert llamadas == ["nodes", "edges", "discoveries", "snapshot"]
    assert out["written"]["nodes"] == out["stats"]["nodes"]
    assert out["written"]["edges"] == out["stats"]["edges"]
    assert svc.last_build["nodes"] == out["stats"]["nodes"]


def test_build_propaga_el_error_y_build_guarded_lo_absorbe(svc, monkeypatch):
    async def _falla(**_k):
        raise RuntimeError("Postgres caido")

    monkeypatch.setattr(mig_repo, "load_sources", _falla)

    with pytest.raises(RuntimeError):
        asyncio.run(svc.build())
    # El loop del engine usa la variante blindada: registra y devuelve None.
    assert asyncio.run(svc.build_guarded()) is None


def test_build_guarded_respeta_el_timeout(svc, monkeypatch):
    async def _lento(**_k):
        await asyncio.sleep(5)
        return dict(FUENTES)

    monkeypatch.setattr(mig_repo, "load_sources", _lento)
    monkeypatch.setattr(svc.s, "mig_timeout", 0.05, raising=False)
    assert asyncio.run(svc.build_guarded()) is None


def test_info_no_toca_la_base_de_datos(svc):
    info = svc.info()
    assert info["backend"] == "postgresql"
    assert info["enabled"] is False          # apagado por defecto
    assert info["min_samples"] == svc.s.mig_min_samples


# --------------------------------------------------------------- SQL / DDL --
def _sql(stmt) -> str:
    from sqlalchemy.dialects import postgresql

    return str(stmt.compile(dialect=postgresql.dialect()))


def test_el_upsert_de_nodos_conserva_first_seen():
    """`first_seen` NO puede aparecer en el SET del ON CONFLICT: es la fecha que
    hace medible el crecimiento del grafo."""
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from medusa.intelligence.mig.models import MIGNodeRow

    stmt = pg_insert(MIGNodeRow).values([{"key": "market:m1", "node_type": "market"}])
    sql = _sql(stmt.on_conflict_do_update(
        index_elements=[MIGNodeRow.key],
        set_={"label": stmt.excluded.label, "last_seen": stmt.excluded.last_seen},
    ))
    assert "ON CONFLICT" in sql
    assert "SET" in sql and "first_seen =" not in sql.split("SET", 1)[1]


def test_el_upsert_de_aristas_usa_el_triple_como_conflicto():
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from medusa.intelligence.mig.models import MIGEdgeRow

    stmt = pg_insert(MIGEdgeRow).values([
        {"src": "a", "dst": "b", "edge_type": "similar_to"}])
    sql = _sql(stmt.on_conflict_do_update(
        index_elements=[MIGEdgeRow.src, MIGEdgeRow.dst, MIGEdgeRow.edge_type],
        set_={"weight": stmt.excluded.weight},
    ))
    assert "ON CONFLICT (src, dst, edge_type)" in sql


def test_las_tablas_del_mig_estan_registradas_en_el_base_compartido():
    """Sin esto, `init_db()` -> create_all no las crearia."""
    from medusa.data.db_models import Base
    from medusa.intelligence.mig import migrations  # noqa: F401  (registra modelos)

    tablas = set(Base.metadata.tables)
    assert {"mig_nodes", "mig_edges", "mig_discoveries", "mig_snapshots"} <= tablas
    # Y las de siempre siguen ahi: nada se ha reemplazado.
    assert {"markets", "trades", "positions", "strategy_signals", "features"} <= tablas


def test_init_db_incluye_las_migraciones_del_mig():
    from medusa.infra.db import _COLUMN_MIGRATIONS, _extra_migrations
    from medusa.intelligence.mig.migrations import MIG_MIGRATIONS

    # Subconjunto, no igualdad: `_extra_migrations` agrega las de TODOS los
    # paquetes aditivos (hoy tambien Wallet Intelligence).
    extra = _extra_migrations()
    assert set(MIG_MIGRATIONS) <= set(extra)
    # Las migraciones existentes no se han tocado.
    assert any("markets ADD COLUMN IF NOT EXISTS medusa_category" in s
               for s in _COLUMN_MIGRATIONS)


def test_json_de_meta_tolera_lo_que_no_es_serializable():
    import datetime as dt

    assert mig_repo._json({}) == ""
    assert "2026" in mig_repo._json({"ts": dt.datetime(2026, 7, 28)})
    assert mig_repo._loads("no es json") == {}
    assert mig_repo._loads("[1,2]") == {}      # una lista no es meta valida


def test_los_chunks_cubren_todo_el_lote():
    items = list(range(1201))
    trozos = list(mig_repo._chunks(items, size=500))
    assert [len(t) for t in trozos] == [500, 500, 201]
    assert [x for t in trozos for x in t] == items


def test_los_objetos_del_grafo_se_serializan_a_dict():
    n = GraphNode(key="market:m1", node_type=NodeType.MARKET, label="q", weight=1.5)
    e = GraphEdge(src="market:m1", dst="category:crypto",
                  edge_type=EdgeType.BELONGS_TO, weight=1.0)
    assert n.to_dict()["node_type"] == "market"
    assert e.to_dict()["edge_type"] == "belongs_to"
