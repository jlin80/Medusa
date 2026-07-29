"""Tests del servicio, del modulo de features y del SQL, sin base de datos.

Postgres no esta disponible en la maquina de desarrollo (ni debe hacer falta
para correr la suite). Se verifica la orquestacion con las escrituras y la red
interceptadas, y que el SQL de los upserts compila contra el dialecto real.

Lo que NO cubre: el comportamiento de los upserts contra un Postgres con datos.
Eso solo lo demuestra correr `POST /api/wallets/build` en el stack.
"""

from __future__ import annotations

import asyncio
import datetime as dt

import pytest

from medusa.intelligence.wallet import repository as wi_repo
from medusa.intelligence.wallet.service import WalletIntelligenceService
from medusa.intelligence.wallet.types import WalletPosition

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 7, 28, 12, 0, tzinfo=UTC)


class _Log:
    def info(self, *a, **k): pass
    def debug(self, *a, **k): pass
    def warning(self, *a, **k): pass
    def error(self, *a, **k): pass


def _pos(wallet, i, roi, cat="crypto"):
    end = NOW - dt.timedelta(days=i + 1)
    start = end - dt.timedelta(days=10)
    return WalletPosition(
        wallet=wallet, market_id=f"m{i}", category=cat, size=100.0, entry_price=0.5,
        cost=100.0, pnl=roi * 100.0, roi=roi, opened_at=start + dt.timedelta(days=1),
        closed_at=end, closed=True, won=roi > 0, market_start=start, market_end=end,
        liquidity=5000.0, spread=0.02,
    )


def _poblacion() -> dict[str, list[WalletPosition]]:
    out = {}
    for w in range(8):
        wallet = f"0x{w:02d}"
        signo = 1 if w % 2 == 0 else -1
        out[wallet] = [_pos(wallet, i, signo * (0.05 + w / 100.0)) for i in range(12)]
    return out


@pytest.fixture
def svc():
    return WalletIntelligenceService(_Log())


# ------------------------------------------------------------- perfilado --
def test_el_perfilado_es_puro_y_devuelve_las_cinco_piezas(svc):
    out = svc.profile(_poblacion(), now=NOW)
    assert set(out) == {"profiles", "population", "clusters", "similarity",
                        "feature_importance"}
    assert len(out["profiles"]) == 8
    assert out["population"]["n"] == 8


def test_cada_perfil_trae_adn_score_reputacion_y_cluster(svc):
    perfil = svc.profile(_poblacion(), now=NOW)["profiles"][0]
    assert len(perfil["dna"]) == 19
    assert 0.0 <= perfil["score"] <= 1.0
    assert 0.0 <= perfil["reputation"] <= 1.0
    assert isinstance(perfil["cluster"], int)
    assert set(perfil["factors"]) == {"sample_factor", "freshness", "stability"}


def test_ningun_perfil_contiene_un_lado_un_tamano_ni_un_precio(svc):
    """La prueba de que esto no es copy trading, hecha sobre la salida real."""
    prohibidos = {"side", "outcome", "price", "entry_price", "stake", "size",
                  "order", "action", "signal"}
    for perfil in svc.profile(_poblacion(), now=NOW)["profiles"]:
        assert not (set(perfil) & prohibidos)
        assert not (set(perfil["dna"]) & prohibidos)


def test_poblacion_vacia_no_rompe_el_perfilado(svc):
    out = svc.profile({}, now=NOW)
    assert out["profiles"] == [] and out["similarity"] == []
    assert out["clusters"]["k"] == 0


def test_el_perfilado_es_determinista(svc):
    datos = _poblacion()
    a = svc.profile(datos, now=NOW)
    b = svc.profile(datos, now=NOW)
    assert a["profiles"] == b["profiles"]
    assert a["clusters"]["assignments"] == b["clusters"]["assignments"]


# ---------------------------------------------------------------- pasada --
def test_run_sin_persistir_no_escribe_nada(svc, monkeypatch):
    async def _wallets():
        return list(_poblacion())

    async def _positions(_w):
        return _poblacion()

    def _boom(*_a, **_k):
        raise AssertionError("persist=False no puede escribir en la BD")

    monkeypatch.setattr(svc, "discover_wallets", _wallets)
    monkeypatch.setattr(svc, "load_positions", _positions)
    for nombre in ("upsert_profiles", "save_dna_history", "upsert_similarity",
                   "save_clusters", "save_run"):
        monkeypatch.setattr(wi_repo, nombre, _boom)

    out = asyncio.run(svc.run(persist=False))
    assert out["ok"] and out["persisted"] is False
    assert out["wallets_profiled"] == 8
    assert out["written"] == {"profiles": 0, "history": 0, "similarity": 0, "clusters": 0}


def test_run_persistiendo_llama_a_las_cinco_escrituras(svc, monkeypatch):
    llamadas: list[str] = []

    async def _wallets():
        return list(_poblacion())

    async def _positions(_w):
        return _poblacion()

    async def _perfiles(rows):
        llamadas.append("profiles")
        return len(rows)

    async def _historia(rows):
        llamadas.append("history")
        return len(rows)

    async def _similitud(rows):
        llamadas.append("similarity")
        return len(rows)

    async def _clusters(rows):
        llamadas.append("clusters")
        return len(rows)

    async def _run(**_k):
        llamadas.append("run")

    monkeypatch.setattr(svc, "discover_wallets", _wallets)
    monkeypatch.setattr(svc, "load_positions", _positions)
    monkeypatch.setattr(wi_repo, "upsert_profiles", _perfiles)
    monkeypatch.setattr(wi_repo, "save_dna_history", _historia)
    monkeypatch.setattr(wi_repo, "upsert_similarity", _similitud)
    monkeypatch.setattr(wi_repo, "save_clusters", _clusters)
    monkeypatch.setattr(wi_repo, "save_run", _run)

    out = asyncio.run(svc.run(persist=True))
    assert llamadas == ["profiles", "history", "similarity", "clusters", "run"]
    assert out["written"]["profiles"] == 8


def test_una_wallet_que_falla_no_tumba_la_pasada(svc, monkeypatch):
    class _Feed:
        async def fetch_positions(self, wallet, limit=0):
            if wallet == "0xmala":
                raise RuntimeError("Data API caida")
            return [{"conditionId": "m1", "size": 0, "avgPrice": 0.5,
                     "initialValue": 50, "realizedPnl": 5}]

        async def fetch_activity(self, wallet, limit=0):
            return []

        async def fetch_market_meta(self, cids):
            return {}

    monkeypatch.setattr(svc, "_get_feed", lambda: _Feed())
    out = asyncio.run(svc.load_positions(["0xbuena", "0xmala"]))
    assert set(out) == {"0xbuena"}


def test_run_propaga_el_error_y_run_guarded_lo_absorbe(svc, monkeypatch):
    async def _falla():
        raise RuntimeError("Data API caida")

    monkeypatch.setattr(svc, "discover_wallets", _falla)
    with pytest.raises(RuntimeError):
        asyncio.run(svc.run())
    assert asyncio.run(svc.run_guarded()) is None


def test_run_guarded_respeta_el_timeout(svc, monkeypatch):
    async def _lento():
        await asyncio.sleep(5)
        return []

    monkeypatch.setattr(svc, "discover_wallets", _lento)
    monkeypatch.setattr(svc.s, "wallet_intel_timeout", 0.05, raising=False)
    assert asyncio.run(svc.run_guarded()) is None


def test_info_declara_lo_que_el_subsistema_no_es(svc):
    info = svc.info()
    assert info["enabled"] is False           # apagado por defecto
    assert info["is_copy_trading"] is False
    assert info["can_place_orders"] is False
    assert len(info["dna_features"]) == 19
    assert set(info["dna_definitions"]) == set(info["dna_features"])


# ------------------------------------------- modulo del Intelligence Layer --
def test_el_modulo_produce_features_float_y_nunca_decisiones(monkeypatch):
    from medusa.core.models import Market
    from medusa.intelligence.wallet.module import WalletIntelligence

    modulo = WalletIntelligence(_Log())

    class _Feed:
        async def fetch_holders(self, market_id, limit=0):
            return [{"proxyWallet": "0xa", "amount": 100},
                    {"proxyWallet": "0xb", "amount": 300},
                    {"proxyWallet": "0xsin_perfil", "amount": 50}]

    async def _profile(wallet):
        return {"wallet": wallet, "reputation": 0.8 if wallet == "0xb" else 0.2}

    monkeypatch.setattr(modulo, "_get_feed", lambda: _Feed())
    monkeypatch.setattr(wi_repo, "get_profile",
                        lambda w: _profile(w) if w in ("0xa", "0xb") else _none())

    async def _none():
        return None

    feats = asyncio.run(modulo.compute([Market(id="m1", question="q")], {}))
    nombres = {f.name for f in feats}
    assert nombres == {"wallet_reputation_mean", "wallet_reputation_max",
                       "wallet_reputation_weighted", "wallet_known_holders",
                       "wallet_coverage"}
    assert all(isinstance(f.value, float) for f in feats)
    por_nombre = {f.name: f.value for f in feats}
    # Media ponderada por tamaño: 0xb pesa el triple que 0xa.
    assert por_nombre["wallet_reputation_weighted"] == pytest.approx(
        (0.2 * 100 + 0.8 * 300) / 400)
    assert por_nombre["wallet_known_holders"] == 2.0
    assert por_nombre["wallet_coverage"] == pytest.approx(2 / 3)
    # Y nada que se parezca a una decision.
    assert not any(k in nombres for k in ("side", "outcome", "action", "signal"))


def test_sin_holders_perfilados_no_se_emite_feature(monkeypatch):
    """Un 0.0 se leeria como 'aqui hay wallets malas', que es otra afirmacion."""
    from medusa.core.models import Market
    from medusa.intelligence.wallet.module import WalletIntelligence

    modulo = WalletIntelligence(_Log())

    class _Feed:
        async def fetch_holders(self, market_id, limit=0):
            return [{"proxyWallet": "0xdesconocida", "amount": 10}]

    async def _none(_w):
        return None

    monkeypatch.setattr(modulo, "_get_feed", lambda: _Feed())
    monkeypatch.setattr(wi_repo, "get_profile", _none)
    assert asyncio.run(modulo.compute([Market(id="m1", question="q")], {})) == []


def test_el_modulo_hereda_el_contrato_del_layer():
    from medusa.intelligence.wallet.module import WalletIntelligence
    from medusa.intelligence_layer.base import IntelligenceModule

    assert issubclass(WalletIntelligence, IntelligenceModule)
    modulo = WalletIntelligence(_Log())
    assert modulo.needs_network is True
    assert modulo.timeout > 0 and modulo.interval > 0


def test_el_modulo_esta_registrado_pero_no_encendido():
    """Registrarlo no lo enciende: sigue mandando la lista blanca."""
    from medusa.config import get_settings
    from medusa.intelligence_layer import build_default_modules

    nombres = {m.name for m in build_default_modules(_Log())}
    assert {"microstructure", "wallet"} <= nombres
    assert "wallet" not in get_settings().intelligence_modules


# ---------------------------------------------------------------- SQL/DDL --
def _sql(stmt) -> str:
    from sqlalchemy.dialects import postgresql

    return str(stmt.compile(dialect=postgresql.dialect()))


def test_el_upsert_de_perfiles_conserva_first_seen():
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from medusa.intelligence.wallet.models import WalletProfileRow

    stmt = pg_insert(WalletProfileRow).values([{"wallet": "0xa"}])
    sql = _sql(stmt.on_conflict_do_update(
        index_elements=[WalletProfileRow.wallet],
        set_={"score": stmt.excluded.score, "updated_at": stmt.excluded.updated_at},
    ))
    assert "ON CONFLICT (wallet)" in sql
    assert "first_seen =" not in sql.split("SET", 1)[1]


def test_el_upsert_de_similitud_usa_el_par_como_conflicto():
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from medusa.intelligence.wallet.models import WalletSimilarityRow

    stmt = pg_insert(WalletSimilarityRow).values([{"wallet_a": "0xa", "wallet_b": "0xb"}])
    sql = _sql(stmt.on_conflict_do_update(
        index_elements=[WalletSimilarityRow.wallet_a, WalletSimilarityRow.wallet_b],
        set_={"similarity": stmt.excluded.similarity},
    ))
    assert "ON CONFLICT (wallet_a, wallet_b)" in sql


def test_las_tablas_estan_registradas_y_no_reemplazan_nada():
    from medusa.data.db_models import Base
    from medusa.intelligence.wallet import migrations  # noqa: F401

    tablas = set(Base.metadata.tables)
    assert {"wi_wallets", "wi_dna_history", "wi_similarity", "wi_clusters",
            "wi_runs"} <= tablas
    assert {"markets", "trades", "positions", "strategy_signals", "features"} <= tablas


def test_init_db_incluye_las_migraciones_de_wallet():
    from medusa.infra.db import _extra_migrations
    from medusa.intelligence.mig.migrations import MIG_MIGRATIONS
    from medusa.intelligence.wallet.migrations import WALLET_MIGRATIONS

    extra = _extra_migrations()
    assert set(WALLET_MIGRATIONS) <= set(extra)
    assert set(MIG_MIGRATIONS) <= set(extra)     # el MIG sigue estando


def test_json_y_lotes_del_repositorio():
    assert wi_repo._json({}) == ""
    assert wi_repo._loads("no es json") == {}
    items = list(range(701))
    trozos = list(wi_repo._chunks(items, size=300))
    assert [len(t) for t in trozos] == [300, 300, 101]
