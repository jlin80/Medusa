"""Estadistica y scoring del Information Flow Engine.

Dos cosas se comprueban aqui, y la segunda importa mas que la primera:

  1. que los numeros salen bien (medianas, Wilson, rangos, latencias),
  2. que la EVIDENCIA viaja con ellos: n, cotas inferiores y el flag de muestra
     suficiente. Un motor de investigacion que publique un 1.00 de n=1 al lado
     de un 0.61 de n=400 sin distinguirlos no esta midiendo, esta decorando.
"""

from __future__ import annotations

import datetime as dt

from medusa.intelligence.flow import cascades as casc
from medusa.intelligence.flow import metrics, scoring
from medusa.intelligence.flow.types import FlowTrade

UTC = dt.timezone.utc
T0 = dt.datetime(2026, 7, 20, 12, 0, tzinfo=UTC)


def _t(wallet, offset, *, market="m1", side="YES", price=0.5, day=0):
    return FlowTrade(
        market_id=market, wallet=wallet, side=side, price=price, size=100.0,
        ts=T0 + dt.timedelta(days=day, seconds=offset),
        uid=f"{market}-{wallet}-{day}-{offset}",
    )


def _cascadas(trades, resolutions=None):
    found = casc.detect_cascades(trades, window_seconds=600, min_participants=3)
    return casc.annotate_resolutions(found, resolutions or {})


# ------------------------------------------------------------ estadistica --
def test_la_mediana_aguanta_una_cola_larga():
    """Por eso todos los tiempos del motor van en mediana y no en media: un solo
    trade a las seis horas dejaria la media inservible."""
    assert metrics.median([60, 60, 60, 21600]) == 60.0
    assert metrics.mean([60, 60, 60, 21600]) > 5000


def test_wilson_castiga_la_muestra_pequena():
    """3 de 3 parece 1.00 y 240 de 400 parece 0.60, pero el orden se invierte
    en cuanto se mira la cota inferior."""
    assert metrics.wilson_lower(3, 3) < metrics.wilson_lower(240, 400)
    assert 0.0 <= metrics.wilson_lower(3, 3) <= 1.0
    assert metrics.wilson_lower(0, 0) == 0.0


def test_una_sola_observacion_no_sostiene_un_intervalo():
    assert metrics.mean_lower([0.9]) == 0.0
    assert metrics.mean_lower([]) == 0.0


def test_el_score_de_velocidad_es_monotono_y_acotado():
    rapido = metrics.decay_score(0, 300)
    medio = metrics.decay_score(300, 300)
    lento = metrics.decay_score(3000, 300)
    assert rapido == 1.0 and medio == 0.5
    assert 0.0 < lento < medio < rapido


def test_el_consenso_es_el_instante_en_que_entra_la_fraccion():
    offsets = [0.0, 10.0, 20.0, 30.0]
    assert metrics.quantile_time(offsets, 0.5) == 10.0
    assert metrics.quantile_time(offsets, 1.0) == 30.0
    assert metrics.quantile_time([], 0.5) == 0.0


def test_una_proporcion_nunca_sale_sin_su_n():
    out = metrics.summarize_proportion(3, 4)
    assert set(out) == {"rate", "n", "lower"}
    assert out["rate"] == 0.75 and out["n"] == 4 and out["lower"] < 0.75


# ------------------------------------------------------ metricas de wallet --
def _poblacion(dias: int = 12) -> list:
    """`lider` siempre abre, `medio` va en medio, `tardia` siempre cierra."""
    trades = []
    for d in range(dias):
        trades += [
            _t("lider", 0, market=f"m{d}", day=d, price=0.40),
            _t("medio", 60, market=f"m{d}", day=d, price=0.45),
            _t("tardia", 120, market=f"m{d}", day=d, price=0.55),
        ]
    return trades


def test_quien_siempre_abre_lidera_y_quien_siempre_cierra_sigue():
    ms = {m.wallet: m for m in scoring.wallet_metrics(_cascadas(_poblacion()))}
    assert ms["lider"].leadership_score == 1.0
    assert ms["lider"].follow_score == 0.0
    assert ms["tardia"].leadership_score == 0.0
    assert ms["tardia"].follow_score == 1.0
    assert ms["medio"].leadership_score == 0.5


def test_el_nulo_del_liderazgo_es_el_azar_y_se_publica():
    """0.5 es el rango normalizado esperado bajo intercambiabilidad: sin ese
    contraste, un leadership de 0.5 pareceria informacion."""
    ms = {m.wallet: m for m in scoring.wallet_metrics(_cascadas(_poblacion()))}
    assert ms["medio"].edge_vs_chance == 0.0
    assert ms["lider"].edge_vs_chance == 0.5
    assert ms["tardia"].edge_vs_chance == -0.5


def test_la_velocidad_mide_desde_el_inicio_de_la_cascada():
    ms = {m.wallet: m for m in scoring.wallet_metrics(_cascadas(_poblacion()))}
    assert ms["lider"].information_speed == 0.0
    assert ms["medio"].information_speed == 60.0
    assert ms["tardia"].information_speed == 120.0
    # Y el score acotado ordena al reves (mas rapido, mas alto).
    assert ms["lider"].speed_score > ms["medio"].speed_score > ms["tardia"].speed_score


def test_el_tiempo_de_propagacion_de_una_wallet_es_el_salto_al_siguiente():
    ms = {m.wallet: m for m in scoring.wallet_metrics(_cascadas(_poblacion()))}
    assert ms["lider"].propagation_time == 60.0
    # La ultima no tiene siguiente: sin observaciones, 0.0 y n que lo respalda.
    assert ms["tardia"].propagation_time == 0.0


def test_la_muestra_insuficiente_queda_marcada():
    pocas = scoring.wallet_metrics(_cascadas(_poblacion(dias=2)), min_samples=10)
    assert all(not m.enough_samples for m in pocas)
    muchas = scoring.wallet_metrics(_cascadas(_poblacion(dias=12)), min_samples=10)
    assert all(m.enough_samples for m in muchas)


def test_el_ranking_ordena_por_la_cota_y_no_por_la_media():
    """Una wallet perfecta con una sola cascada no puede encabezar el ranking."""
    trades = _poblacion(dias=12)
    trades += [_t("novata", 0, market="mX", day=99, price=0.4),
               _t("relleno1", 60, market="mX", day=99),
               _t("relleno2", 120, market="mX", day=99)]
    ranking = scoring.wallet_metrics(_cascadas(trades))
    ms = {m.wallet: m for m in ranking}
    assert ms["novata"].leadership_score == 1.0     # abre su unica cascada
    assert ms["novata"].leadership_lower == 0.0     # pero no sostiene nada
    assert ranking[0].wallet == "lider"


# ---------------------------------------- informacion temprana vs tardia --
def _con_resultado(dias: int, gana: bool) -> list:
    return _poblacion(dias), {f"m{d}": (1.0 if gana else 0.0) for d in range(dias)}


def test_las_entradas_tempranas_puntuan_con_la_resolucion():
    trades, res = _con_resultado(10, gana=True)
    ms = {m.wallet: m for m in scoring.wallet_metrics(_cascadas(trades, res))}
    assert ms["lider"].n_early == 10 and ms["lider"].early_information_score == 1.0
    assert ms["lider"].n_late == 0
    assert ms["tardia"].n_late == 10 and ms["tardia"].late_information_score == 1.0


def test_un_lado_perdedor_puntua_cero_no_se_ignora():
    trades, res = _con_resultado(10, gana=False)
    ms = {m.wallet: m for m in scoring.wallet_metrics(_cascadas(trades, res))}
    assert ms["lider"].early_information_score == 0.0
    assert ms["lider"].n_early == 10          # la observacion cuenta, y falla


def test_sin_resolucion_puntua_el_movimiento_posterior_del_precio():
    trades = [_t("a", 0, price=0.40), _t("b", 60, price=0.45), _t("c", 120, price=0.60)]
    ms = {m.wallet: m for m in scoring.wallet_metrics(_cascadas(trades),
                                                      min_price_move=0.02)}
    # `a` entro a 0.40 y la cascada acabo en 0.60: movimiento a su favor.
    assert ms["a"].early_information_score == 1.0 and ms["a"].n_early == 1
    # `c` entro justo al final: no hay movimiento posterior que medir.
    assert ms["c"].n_late == 0


def test_un_empate_no_cuenta_como_acierto_ni_como_fallo():
    """La forma mas silenciosa de fabricar significancia es contar las
    observaciones vacias. Aqui simplemente no entran en la muestra."""
    trades = [_t("a", 0, price=0.500), _t("b", 60, price=0.501),
              _t("c", 120, price=0.502)]
    ms = {m.wallet: m for m in scoring.wallet_metrics(_cascadas(trades),
                                                      min_price_move=0.02)}
    assert ms["a"].n_early == 0 and ms["a"].n_late == 0


def test_el_information_edge_es_la_diferencia_entre_temprano_y_tardio():
    trades, res = _con_resultado(10, gana=True)
    ms = {m.wallet: m for m in scoring.wallet_metrics(_cascadas(trades, res))}
    m = ms["lider"]
    assert abs(m.information_edge -
               (m.early_information_score - m.late_information_score)) < 1e-9


# ----------------------------------------------------- metricas de mercado --
def test_el_mercado_agrega_consenso_propagacion_y_participantes():
    # Wallets distintas cada dia: una wallet que ya entro en este mercado no
    # vuelve a "entrar" (solo cuenta su primera entrada), asi que repetir las
    # mismas tres daria una sola cascada.
    trades = []
    for d in range(4):
        trades += [_t(f"a{d}", 0, market="mm", day=d), _t(f"b{d}", 60, market="mm", day=d),
                   _t(f"c{d}", 120, market="mm", day=d)]
    found = _cascadas(trades)
    events = casc.all_propagation_events(found, max_hops=2)
    m = scoring.market_metrics(found, events, min_samples=3)[0]
    assert m.market_id == "mm"
    assert m.n_cascades == 4 and m.n_wallets == 12
    assert m.n_events == len(events)
    assert m.consensus_delay == 60.0 and m.propagation_time == 60.0
    assert m.avg_cascade_size == 3.0
    assert m.enough_samples is True


def test_una_cascada_simultanea_no_inventa_un_ritmo():
    """Span 0: todo el mundo dentro del mismo segundo. No aporta ritmo, y
    dividir por cero (o inventarse un span minimo) seria peor."""
    trades = [_t("a", 0), _t("b", 0), _t("c", 0)]
    found = _cascadas(trades)
    m = scoring.market_metrics(found, [], min_samples=1)[0]
    assert m.information_speed == 0.0


# ------------------------------------------------------------------ pares --
def test_un_par_solo_se_publica_si_se_repite():
    trades = _poblacion(dias=8)
    events = casc.all_propagation_events(_cascadas(trades), max_hops=1)
    pares = scoring.pair_stats(events, min_observations=5)
    claves = {(p["leader"], p["follower"]) for p in pares}
    assert ("lider", "medio") in claves
    assert all(p["n"] >= 5 for p in pares)
    # Con el umbral por encima de las coincidencias, no se publica nada.
    assert scoring.pair_stats(events, min_observations=99) == []


def test_el_par_lleva_su_n_por_delante():
    events = casc.all_propagation_events(_cascadas(_poblacion(dias=8)), max_hops=1)
    par = scoring.pair_stats(events, min_observations=5)[0]
    assert par["n"] == 8 and par["markets"] == 8
    assert par["median_lag"] == 60.0


# --------------------------------------------------------------- resumen --
def test_el_resumen_de_una_pasada_cuenta_todo_lo_que_se_persiste():
    found = _cascadas(_poblacion(dias=5), {f"m{d}": 1.0 for d in range(5)})
    events = casc.all_propagation_events(found, max_hops=2)
    out = scoring.flow_summary(found, events)
    assert out["cascades"] == 5 and out["markets"] == 5
    assert out["wallets"] == 3 and out["resolved_cascades"] == 5
    assert out["events"] == len(events)
    assert out["median_cascade_size"] == 3.0


def test_sin_datos_no_hay_metricas_pero_tampoco_excepcion():
    assert scoring.wallet_metrics([]) == []
    assert scoring.market_metrics([], []) == []
    assert scoring.flow_summary([], [])["cascades"] == 0
