"""Tests de la normalizacion del JSON de Polymarket.

Es la parte fragil del subsistema: los nombres de campo de una API ajena que
cambia sin avisar. Todo se prueba con fixtures escritas a mano, sin red.
"""

from __future__ import annotations

import datetime as dt

import pytest

from medusa.intelligence.wallet import ingest

UTC = dt.timezone.utc


def test_timestamps_en_segundos_milisegundos_e_iso():
    assert ingest._ts(1785000000) == dt.datetime.fromtimestamp(1785000000, tz=UTC)
    assert ingest._ts(1785000000000) == dt.datetime.fromtimestamp(1785000000, tz=UTC)
    assert ingest._ts("2026-07-28T12:00:00Z") == dt.datetime(2026, 7, 28, 12, tzinfo=UTC)
    assert ingest._ts("1785000000") == dt.datetime.fromtimestamp(1785000000, tz=UTC)


def test_timestamps_ilegibles_devuelven_none_no_una_fecha_inventada():
    for basura in (None, "", 0, "ayer", {}, "not-a-date"):
        assert ingest._ts(basura) is None


def test_los_alias_de_campo_se_resuelven_en_orden():
    assert ingest._f({"initialValue": "12.5"}, "initialValue", "cost") == 12.5
    assert ingest._f({"cost": 3}, "initialValue", "cost") == 3.0
    assert ingest._f({}, "initialValue", "cost", default=-1.0) == -1.0
    assert ingest._f({"initialValue": "no-numero"}, "initialValue") == 0.0


def test_extraer_wallets_deduplica_normaliza_y_ordena():
    holders = [
        {"proxyWallet": "0xB"}, {"wallet": "0xa"}, {"proxyWallet": "0xB"},
        {"user": ""}, {"address": "0xC"},
    ]
    assert ingest.extract_wallets(holders) == ["0xa", "0xb", "0xc"]


def test_extraer_wallets_filtra_el_polvo():
    holders = [{"proxyWallet": "0xa", "amount": 0.2}, {"proxyWallet": "0xb", "amount": 50}]
    assert ingest.extract_wallets(holders, min_size=1.0) == ["0xb"]


def test_la_actividad_ignora_los_redeem():
    """Cobrar un mercado ya resuelto no es una decision de salida: contarlo
    empujaria el exit_timing de todo el mundo a 1.0."""
    actividad = [
        {"type": "TRADE", "conditionId": "m1", "timestamp": 1785000000},
        {"type": "REDEEM", "conditionId": "m1", "timestamp": 1785999999},
    ]
    ventanas = ingest.activity_windows(actividad)
    assert ventanas["m1"]["n"] == 1
    assert ventanas["m1"]["last"] == ingest._ts(1785000000)


def test_la_actividad_agrega_primer_y_ultimo_movimiento():
    actividad = [
        {"type": "TRADE", "conditionId": "m1", "timestamp": 300},
        {"type": "TRADE", "conditionId": "m1", "timestamp": 100},
        {"type": "TRADE", "conditionId": "m2", "timestamp": 500},
    ]
    ventanas = ingest.activity_windows(actividad)
    assert ventanas["m1"]["first"] == ingest._ts(100)
    assert ventanas["m1"]["last"] == ingest._ts(300)
    assert ventanas["m2"]["n"] == 1


def test_posicion_cerrada_frente_a_viva():
    posiciones = [
        {"conditionId": "m1", "size": 0, "avgPrice": 0.4, "initialValue": 40,
         "realizedPnl": 10, "percentRealizedPnl": 25},
        {"conditionId": "m2", "size": 100, "avgPrice": 0.5, "initialValue": 50,
         "cashPnl": 5},
    ]
    rows = ingest.normalize_positions("0xa", posiciones)
    cerrada = next(r for r in rows if r.market_id == "m1")
    viva = next(r for r in rows if r.market_id == "m2")
    assert cerrada.closed is True and cerrada.won is True
    assert cerrada.roi == pytest.approx(0.25)
    assert viva.closed is False and viva.won is None


def test_una_posicion_viva_no_recibe_fecha_de_cierre():
    """Si no esta cerrada, el ultimo movimiento NO es su cierre."""
    posiciones = [{"conditionId": "m1", "size": 100, "avgPrice": 0.5, "initialValue": 50}]
    actividad = [{"type": "TRADE", "conditionId": "m1", "timestamp": 1785000000}]
    row = ingest.normalize_positions("0xa", posiciones, activity=actividad)[0]
    assert row.opened_at is not None and row.closed_at is None


def test_el_roi_se_reconstruye_desde_pnl_y_coste_si_falta_el_porcentaje():
    posiciones = [{"conditionId": "m1", "size": 0, "avgPrice": 0.5,
                   "initialValue": 200, "realizedPnl": 50}]
    row = ingest.normalize_positions("0xa", posiciones)[0]
    assert row.roi == pytest.approx(0.25)


def test_el_coste_cae_a_totalBought_cuando_no_hay_initialValue():
    posiciones = [{"conditionId": "m1", "size": 0, "avgPrice": 0.4,
                   "totalBought": 100, "realizedPnl": 8}]
    row = ingest.normalize_positions("0xa", posiciones)[0]
    assert row.cost == pytest.approx(40.0)
    assert row.roi == pytest.approx(0.2)


def test_el_contexto_de_mercado_llega_desde_gamma():
    posiciones = [{"conditionId": "m1", "size": 0, "avgPrice": 0.5, "initialValue": 50,
                   "realizedPnl": 5}]
    meta = {"m1": {"conditionId": "m1", "startDate": "2026-07-01T00:00:00Z",
                   "endDate": "2026-07-11T00:00:00Z", "liquidityNum": 12345.0,
                   "spread": 0.03, "category": "crypto"}}
    actividad = [{"type": "TRADE", "conditionId": "m1",
                  "timestamp": "2026-07-02T00:00:00Z"}]
    row = ingest.normalize_positions("0xa", posiciones, activity=actividad,
                                     market_meta=meta)[0]
    assert row.liquidity == 12345.0 and row.spread == 0.03
    assert row.category == "crypto"
    # Entro el dia 2 de una vida de 10 dias => 10% de la vida transcurrida.
    assert row.duration_fraction(row.opened_at) == pytest.approx(0.1, abs=0.01)


def test_sin_contexto_de_mercado_no_se_inventa_nada():
    posiciones = [{"conditionId": "m1", "size": 0, "avgPrice": 0.5, "initialValue": 50}]
    row = ingest.normalize_positions("0xa", posiciones)[0]
    assert row.market_start is None and row.market_end is None
    assert row.duration_fraction(row.opened_at) is None


def test_posiciones_sin_condition_id_se_descartan():
    assert ingest.normalize_positions("0xa", [{"size": 10}, {}]) == []


def test_un_mercado_de_duracion_cero_no_divide_por_cero():
    posiciones = [{"conditionId": "m1", "size": 0, "avgPrice": 0.5, "initialValue": 50}]
    meta = {"m1": {"startDate": "2026-07-01T00:00:00Z", "endDate": "2026-07-01T00:00:00Z"}}
    actividad = [{"type": "TRADE", "conditionId": "m1", "timestamp": "2026-07-01T00:00:00Z"}]
    row = ingest.normalize_positions("0xa", posiciones, activity=actividad,
                                     market_meta=meta)[0]
    assert row.duration_fraction(row.opened_at) is None
