"""Ingesta y deteccion de cascadas del Information Flow Engine.

Todo lo que se prueba aqui es PURO: entra JSON o una lista de trades escrita a
mano y sale una estructura. Sin BD, sin red y sin reloj -- que es justo lo que
permite fijar los casos raros (una venta, un empate de tiempos, un hueco largo)
en vez de esperar a verlos en produccion.
"""

from __future__ import annotations

import datetime as dt

from medusa.intelligence.flow import cascades as casc
from medusa.intelligence.flow import ingest
from medusa.intelligence.flow.types import FlowTrade

UTC = dt.timezone.utc
T0 = dt.datetime(2026, 7, 20, 12, 0, tzinfo=UTC)


def _t(wallet: str, offset: float, side: str = "YES", price: float = 0.50,
       size: float = 100.0, market: str = "m1") -> FlowTrade:
    return FlowTrade(
        market_id=market, wallet=wallet, side=side, price=price, size=size,
        ts=T0 + dt.timedelta(seconds=offset), uid=f"{wallet}-{offset}-{side}",
    )


# ----------------------------------------------------------------- ingesta --
def test_comprar_si_es_entrar_en_si_y_vender_si_es_entrar_en_no():
    """La normalizacion que hace que la cadena signifique algo."""
    assert ingest.entered_side("Yes", "BUY") == "YES"
    assert ingest.entered_side("Yes", "SELL") == "NO"
    assert ingest.entered_side("No", "BUY") == "NO"
    assert ingest.entered_side("No", "SELL") == "YES"


def test_un_lado_ilegible_se_descarta_en_vez_de_suponerse():
    """Suponer YES colocaria la entrada en la cadena equivocada, y un
    participante mal colocado corrompe el rango de todos los demas."""
    assert ingest.entered_side("Maybe", "BUY") is None
    assert ingest.entered_side("Yes", "TRANSFER") is None


def test_el_precio_se_normaliza_al_lado_entrado():
    rows = [
        {"proxyWallet": "0xA", "outcome": "Yes", "side": "BUY", "price": "0.30",
         "size": "10", "timestamp": 1_780_000_000},
        {"proxyWallet": "0xB", "outcome": "Yes", "side": "SELL", "price": "0.30",
         "size": "10", "timestamp": 1_780_000_060},
    ]
    trades = ingest.normalize_trades("m1", rows)
    assert [t.side for t in trades] == ["YES", "NO"]
    # El vendedor de SI a 0.30 queda en NO con probabilidad implicita 0.70.
    assert trades[0].price == 0.30
    assert abs(trades[1].price - 0.70) < 1e-9


def test_las_filas_incompletas_no_entran_con_valores_por_defecto():
    rows = [
        {"proxyWallet": "0xA", "outcome": "Yes", "side": "BUY", "price": "0.5",
         "size": "10"},                                    # sin timestamp
        {"outcome": "Yes", "side": "BUY", "price": "0.5", "size": "10",
         "timestamp": 1_780_000_000},                      # sin wallet
        {"proxyWallet": "0xB", "outcome": "Yes", "side": "BUY", "price": "1.4",
         "size": "10", "timestamp": 1_780_000_000},        # precio imposible
        {"proxyWallet": "0xC", "outcome": "Yes", "side": "BUY", "price": "0.5",
         "size": "0", "timestamp": 1_780_000_000},         # sin tamaño
    ]
    assert ingest.normalize_trades("m1", rows) == []


def test_el_mismo_trade_dos_veces_solo_cuenta_una():
    row = {"proxyWallet": "0xA", "outcome": "Yes", "side": "BUY", "price": "0.5",
           "size": "10", "timestamp": 1_780_000_000, "transactionHash": "0xdead"}
    assert len(ingest.normalize_trades("m1", [row, dict(row)])) == 1


def test_un_mercado_vivo_no_tiene_resolucion():
    assert ingest.resolution_of({"closed": False, "outcomePrices": '["1","0"]'}) is None
    # Cerrado pero sin resultado legible tampoco resuelve: "cerrado" != "resuelto".
    assert ingest.resolution_of({"closed": True, "outcomePrices": "[]"}) is None
    assert ingest.resolution_of({"closed": True, "outcomePrices": '["1","0"]'}) == 1.0
    assert ingest.resolution_of({"closed": True, "outcomePrices": '["0","1"]'}) == 0.0


# ------------------------------------------------------ primeras entradas --
def test_solo_cuenta_la_primera_entrada_de_cada_wallet():
    trades = [_t("a", 0), _t("a", 30), _t("a", 90), _t("b", 60)]
    entries = casc.first_entries(trades)[("m1", "YES")]
    assert [e.wallet for e in entries] == ["a", "b"]
    assert entries[0].ts == T0     # la mas antigua, no la ultima


def test_los_dos_lados_son_cadenas_distintas():
    """Un comprador de NO no es seguidor de un comprador de SI."""
    trades = [_t("a", 0, "YES"), _t("b", 10, "NO"), _t("c", 20, "YES")]
    entries = casc.first_entries(trades)
    assert [e.wallet for e in entries[("m1", "YES")]] == ["a", "c"]
    assert [e.wallet for e in entries[("m1", "NO")]] == ["b"]


# ---------------------------------------------------------------- cascadas --
def test_una_racha_seguida_es_una_cascada():
    trades = [_t("a", 0), _t("b", 60), _t("c", 120)]
    found = casc.detect_cascades(trades, window_seconds=600, min_participants=3)
    assert len(found) == 1
    assert [e.wallet for e in found[0].entries] == ["a", "b", "c"]
    assert found[0].span_seconds == 120


def test_un_hueco_mayor_que_la_ventana_parte_la_cascada():
    trades = [_t("a", 0), _t("b", 60), _t("c", 120),
              _t("d", 5000), _t("e", 5060), _t("f", 5120)]
    found = casc.detect_cascades(trades, window_seconds=600, min_participants=3)
    assert len(found) == 2
    assert [e.wallet for e in found[1].entries] == ["d", "e", "f"]


def test_las_rachas_cortas_no_son_cascadas():
    trades = [_t("a", 0), _t("b", 60)]
    assert casc.detect_cascades(trades, window_seconds=600, min_participants=3) == []


def test_la_cascada_calcula_su_tiempo_de_propagacion_y_su_consenso():
    # Entradas a 0, 60, 120 y 600 s: saltos de 60, 60 y 480.
    trades = [_t("a", 0), _t("b", 60), _t("c", 120), _t("d", 600)]
    c = casc.detect_cascades(trades, window_seconds=600, min_participants=3)[0]
    assert c.propagation_time == 60.0            # mediana de [60, 60, 480]
    # Consenso al 50% de 4 participantes = el segundo en entrar, a los 60 s.
    assert c.consensus_delay == 60.0


def test_el_rango_normalizado_va_de_cero_a_uno():
    trades = [_t("a", 0), _t("b", 60), _t("c", 120), _t("d", 180), _t("e", 240)]
    c = casc.detect_cascades(trades, window_seconds=600, min_participants=3)[0]
    assert c.rank(0) == 0.0 and c.rank(4) == 1.0
    assert c.rank(2) == 0.5


def test_el_movimiento_del_precio_es_el_del_lado():
    trades = [_t("a", 0, price=0.40), _t("b", 60, price=0.45), _t("c", 120, price=0.55)]
    c = casc.detect_cascades(trades, window_seconds=600, min_participants=3)[0]
    assert abs(c.price_move - 0.15) < 1e-9


# ------------------------------------------------- eventos de propagacion --
def test_cada_par_a_distancia_permitida_produce_un_eslabon():
    trades = [_t("a", 0), _t("b", 60), _t("c", 120), _t("d", 180)]
    c = casc.detect_cascades(trades, window_seconds=600, min_participants=3)[0]
    events = casc.propagation_events(c, max_hops=1)
    assert [(e.leader, e.follower, e.hop) for e in events] == [
        ("a", "b", 1), ("b", "c", 1), ("c", "d", 1)]
    assert [e.lag_seconds for e in events] == [60.0, 60.0, 60.0]


def test_max_hops_acota_la_explosion_de_pares():
    """Con 4 entradas hay 6 pares posibles; con max_hops=2 solo 5 describen
    propagacion y el resto es coincidencia de mercado."""
    trades = [_t("a", 0), _t("b", 60), _t("c", 120), _t("d", 180)]
    c = casc.detect_cascades(trades, window_seconds=600, min_participants=3)[0]
    assert len(casc.propagation_events(c, max_hops=3)) == 6
    assert len(casc.propagation_events(c, max_hops=2)) == 5
    assert len(casc.propagation_events(c, max_hops=1)) == 3


def test_el_eslabon_lleva_los_precios_de_las_dos_puntas():
    trades = [_t("a", 0, price=0.40), _t("b", 60, price=0.50), _t("c", 120)]
    c = casc.detect_cascades(trades, window_seconds=600, min_participants=3)[0]
    ev = casc.propagation_events(c, max_hops=1)[0]
    assert ev.price_leader == 0.40 and ev.price_follower == 0.50
    assert abs(ev.price_move - 0.10) < 1e-9


def test_ningun_eslabon_afirma_causalidad():
    """El contrato del motor, comprobado sobre la salida real: los campos
    describen orden y distancia, no influencia."""
    trades = [_t("a", 0), _t("b", 60), _t("c", 120)]
    c = casc.detect_cascades(trades, window_seconds=600, min_participants=3)[0]
    salida = casc.propagation_events(c, max_hops=2)[0].to_dict()
    prohibidos = {"caused_by", "influence", "influenced", "signal", "action",
                  "side_to_take", "stake", "order"}
    assert not (set(salida) & prohibidos)
    assert {"lag_seconds", "hop", "leader", "follower"} <= set(salida)


def test_el_lag_maximo_corta_los_pares_lejanos():
    trades = [_t("a", 0), _t("b", 60), _t("c", 500)]
    c = casc.detect_cascades(trades, window_seconds=600, min_participants=3)[0]
    # a->b van 60 s y entra; a->c (500 s) y b->c (440 s) se pasan del tope.
    events = casc.propagation_events(c, max_hops=3, max_lag_seconds=120)
    assert [(e.leader, e.follower) for e in events] == [("a", "b")]


# ------------------------------------------------------------ resoluciones --
def test_la_resolucion_se_invierte_para_el_lado_no():
    trades = [_t("a", 0, "NO"), _t("b", 60, "NO"), _t("c", 120, "NO")]
    found = casc.detect_cascades(trades, window_seconds=600, min_participants=3)
    casc.annotate_resolutions(found, {"m1": 1.0})     # gano el SI
    assert found[0].resolved is True
    assert found[0].resolution_value == 0.0           # el lado NO perdio


def test_un_mercado_sin_resolver_no_recibe_un_resultado_inventado():
    trades = [_t("a", 0), _t("b", 60), _t("c", 120)]
    found = casc.detect_cascades(trades, window_seconds=600, min_participants=3)
    casc.annotate_resolutions(found, {"m1": None})
    assert found[0].resolved is False
    assert found[0].resolution_value is None


def test_la_clave_de_una_cascada_es_estable_entre_pasadas():
    """Dos analisis de la misma ventana tienen que producir la MISMA cascada, o
    la tabla se llenaria de duplicados y la muestra mediria repeticion."""
    trades = [_t("a", 0), _t("b", 60), _t("c", 120)]
    kw = {"window_seconds": 600, "min_participants": 3}
    primera = casc.detect_cascades(trades, **kw)[0]
    # Segunda pasada: la misma ventana mas una entrada nueva al final.
    segunda = casc.detect_cascades(trades + [_t("d", 180)], **kw)[0]
    assert primera.key == segunda.key
    assert segunda.n == 4
