"""Tests de las 19 metricas del ADN. Puros: sin BD, sin red, sin reloj real."""

from __future__ import annotations

import datetime as dt

import pytest

from medusa.intelligence.wallet.wallet_dna import build_dna, build_population, population_stats
from medusa.intelligence.wallet.wallet_dna import metrics as m
from medusa.intelligence.wallet.types import DNA_DEFINITIONS, DNA_FEATURES, WalletPosition

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 7, 28, 12, 0, tzinfo=UTC)


def pos(
    roi=0.1, pnl=None, cost=100.0, days_ago=1, closed=True, category="crypto",
    liquidity=5000.0, spread=0.02, market_id=None, opened_frac=0.0, closed_frac=1.0,
    size=100.0,
):
    """Una posicion sintetica con fechas de mercado coherentes.

    El mercado vive 10 dias; `opened_frac`/`closed_frac` colocan la entrada y la
    salida dentro de esa vida, que es lo que miden las metricas de timing.
    """
    end = NOW - dt.timedelta(days=days_ago)
    start = end - dt.timedelta(days=10)
    span = (end - start).total_seconds()
    return WalletPosition(
        wallet="0xa", market_id=market_id or f"m{days_ago}-{roi}", category=category,
        size=size, entry_price=0.5, exit_price=0.6, cost=cost,
        pnl=pnl if pnl is not None else roi * cost, roi=roi,
        opened_at=start + dt.timedelta(seconds=span * opened_frac),
        closed_at=(start + dt.timedelta(seconds=span * closed_frac)) if closed else None,
        closed=closed, won=(roi > 0) if closed else None,
        market_start=start, market_end=end, liquidity=liquidity, spread=spread,
    )


# ------------------------------------------------------------- rendimiento --
def test_sin_posiciones_todo_a_cero():
    dna = build_dna("0xa", [], now=NOW)
    assert dna.vector() == [0.0] * len(DNA_FEATURES)
    assert dna.n_positions == 0 and dna.n_closed == 0


def test_una_posicion_viva_no_cuenta_como_rendimiento():
    """El error clasico: inflar el track record con PnL no realizado."""
    vivas = [pos(roi=5.0, closed=False)]
    assert m.roi_historical(vivas) == 0.0
    assert m.win_rate(vivas) == 0.0
    assert m.reliability(vivas) == 0.0


def test_roi_historico_es_la_media_por_posicion():
    assert m.roi_historical([pos(roi=0.2), pos(roi=-0.1)]) == pytest.approx(0.05)


def test_roi_reciente_solo_mira_la_ventana():
    ps = [pos(roi=1.0, days_ago=200), pos(roi=0.1, days_ago=2)]
    assert m.roi_recent(ps, NOW, days=30) == pytest.approx(0.1)
    assert m.roi_historical(ps) == pytest.approx(0.55)


def test_roi_reciente_sin_cierres_en_la_ventana_es_cero():
    assert m.roi_recent([pos(roi=1.0, days_ago=200)], NOW, days=30) == 0.0


def test_sharpe_necesita_al_menos_dos_posiciones():
    assert m.sharpe([pos(roi=0.5)]) == 0.0


def test_sharpe_penaliza_la_dispersion():
    estable = [pos(roi=0.10, market_id=f"a{i}") for i in range(6)]
    estable[0] = pos(roi=0.11, market_id="a0")     # algo de varianza, poca
    volatil = [pos(roi=r, market_id=f"b{i}") for i, r in enumerate([1.0, -0.8, 0.9, -0.7, 0.6, -0.5])]
    assert m.sharpe(estable) > m.sharpe(volatil)


def test_win_rate_y_reliability_castigan_la_muestra_corta():
    """3 de 3 no es un 100%: Wilson lo dice."""
    tres = [pos(roi=0.1, market_id=f"w{i}") for i in range(3)]
    assert m.win_rate(tres) == 1.0
    assert m.reliability(tres) < 0.6

    treinta = [pos(roi=0.1, market_id=f"x{i}") for i in range(30)]
    assert m.reliability(treinta) > m.reliability(tres)


def test_consistencia_es_forma_no_calidad():
    """Una wallet consistentemente MALA puntua alto en consistencia."""
    mala_estable = [pos(roi=-0.10, market_id=f"c{i}") for i in range(5)]
    mala_estable[0] = pos(roi=-0.11, market_id="c0")
    assert m.consistency(mala_estable) > 0.8


def test_volatilidad_y_drawdown():
    ps = [pos(roi=0.5, pnl=50.0, days_ago=10, market_id="d1"),
          pos(roi=-0.5, pnl=-40.0, days_ago=5, market_id="d2"),
          pos(roi=0.1, pnl=5.0, days_ago=1, market_id="d3")]
    assert m.volatility(ps) > 0
    # Curva acumulada: 50 -> 10 -> 15. Pico 50, minimo posterior 10 => 80%.
    assert m.drawdown(ps) == pytest.approx(0.8)


def test_drawdown_de_curva_siempre_creciente_es_cero():
    ps = [pos(roi=0.1, pnl=10.0, days_ago=d, market_id=f"e{d}") for d in (5, 4, 3)]
    assert m.drawdown(ps) == 0.0


# ---------------------------------------------------------------- actividad --
def test_frecuencia_usa_el_periodo_activo_no_el_calendario():
    """40 operaciones en una semana y ocho meses de silencio no son 0.16/dia."""
    ps = [pos(days_ago=200 - i, market_id=f"f{i}") for i in range(8)]
    freq = m.trade_frequency(ps)
    assert freq == pytest.approx(8 / 7.0, rel=0.01)


def test_frescura_decae_con_la_inactividad():
    reciente = [pos(days_ago=0, market_id="g1")]
    vieja = [pos(days_ago=180, market_id="g2")]
    assert m.freshness(reciente, NOW, 30) > 0.95
    assert m.freshness(vieja, NOW, 30) < 0.01


def test_decay_positivo_si_mejora_y_negativo_si_se_degrada():
    mejorando = [pos(roi=-0.2, days_ago=100, market_id="h1"),
                 pos(roi=0.3, days_ago=2, market_id="h2")]
    degradando = [pos(roi=0.3, days_ago=100, market_id="h3"),
                  pos(roi=-0.2, days_ago=2, market_id="h4")]
    assert m.decay(mejorando, NOW, 30) > 0
    assert m.decay(degradando, NOW, 30) < 0


# ------------------------------------------------------------------ timing --
def test_timings_miden_fraccion_de_vida_del_mercado():
    temprana = [pos(opened_frac=0.05, closed_frac=1.0, market_id="i1")]
    tardia = [pos(opened_frac=0.9, closed_frac=1.0, market_id="i2")]
    assert m.entry_timing(temprana) == pytest.approx(0.05, abs=0.01)
    assert m.entry_timing(tardia) == pytest.approx(0.9, abs=0.01)
    assert m.exit_timing(temprana) == pytest.approx(1.0, abs=0.01)


def test_sin_fechas_de_mercado_el_timing_se_EXCLUYE_no_se_pone_a_cero():
    """Un dato ausente no es un timing temprano."""
    sin_fechas = pos(market_id="i3")
    sin_fechas.market_start = None
    sin_fechas.market_end = None
    con_fechas = pos(opened_frac=0.8, market_id="i4")
    assert m.entry_timing([sin_fechas, con_fechas]) == pytest.approx(0.8, abs=0.01)
    assert m.entry_timing([sin_fechas]) == 0.0


# ------------------------------------------------------------ preferencias --
def test_preferencia_de_liquidez_es_logaritmica_y_monotona():
    baja = [pos(liquidity=1_000.0, market_id="j1")]
    alta = [pos(liquidity=500_000.0, market_id="j2")]
    assert 0.0 < m.liquidity_preference(baja) < m.liquidity_preference(alta) <= 1.0


def test_preferencia_de_spread():
    assert m.spread_preference([pos(spread=0.05), pos(spread=0.15)]) == pytest.approx(0.10)


def test_expertise_pondera_por_peso_de_la_categoria():
    """4 de 4 en una categoria marginal no convierte a nadie en experto."""
    muchas = [pos(roi=0.1, category="sports", market_id=f"k{i}") for i in range(40)]
    pocas = [pos(roi=0.1, category="crypto", market_id=f"l{i}") for i in range(2)]
    detalle = m.category_breakdown(muchas + pocas)
    assert detalle["crypto"]["share"] < 0.06
    # El desglose publica los numeros redondeados a 4 decimales; la metrica usa
    # la precision completa. Por eso la tolerancia es 1e-4 y no 1e-6.
    assert m.category_expertise(muchas + pocas) == pytest.approx(
        detalle["sports"]["share"] * detalle["sports"]["wilson"], abs=1e-4)


def test_conviccion_distingue_apuestas_planas_de_concentradas():
    plana = [pos(cost=100.0, market_id=f"n{i}") for i in range(5)]
    concentrada = [pos(cost=c, market_id=f"o{i}") for i, c in enumerate([1, 1, 1, 1, 1000])]
    assert m.conviction(plana) == 0.0
    assert m.conviction(concentrada) > 0.7


# ------------------------------------------------------------- alpha / beta --
def test_beta_sin_poblacion_es_cero_y_alpha_degenera_al_roi():
    ps = [pos(roi=0.2, days_ago=d, market_id=f"p{d}") for d in (30, 20, 10)]
    assert m.beta(ps, {}) == 0.0
    assert m.alpha(ps, {}) == pytest.approx(m.roi_historical(ps))


def test_beta_alto_cuando_la_wallet_sigue_a_la_poblacion():
    ps = [pos(roi=r, days_ago=d, market_id=f"q{d}")
          for r, d in [(0.4, 40), (-0.2, 25), (0.6, 10)]]
    pop = m.population_buckets(ps, 7)
    # Contra si misma, beta ~ 1 y alpha ~ 0: todo su resultado es "la marea".
    assert m.beta(ps, pop, 7) == pytest.approx(1.0, abs=0.05)
    assert abs(m.alpha(ps, pop, 7)) < 0.05


# ------------------------------------------------------------------ perfil --
def test_el_adn_cubre_exactamente_las_19_metricas_declaradas():
    dna = build_dna("0xa", [pos(), pos(roi=-0.2, market_id="z")], now=NOW)
    assert set(dna.metrics) == set(DNA_FEATURES)
    assert len(DNA_FEATURES) == 19
    assert len(dna.vector()) == 19
    # Y todas tienen definicion publicada: un numero sin definicion no es
    # auditable.
    assert set(DNA_DEFINITIONS) == set(DNA_FEATURES)


def test_todo_el_adn_es_numerico():
    """No hay una sola etiqueta cualitativa en el perfil."""
    dna = build_dna("0xa", [pos(), pos(roi=0.3, market_id="zz")], now=NOW)
    assert all(isinstance(v, float) for v in dna.metrics.values())
    assert all(isinstance(v, float) for v in dna.vector())


def test_el_perfil_registra_su_propia_muestra():
    ps = [pos(category="crypto", market_id="r1"), pos(category="sports", market_id="r2"),
          pos(closed=False, category="sports", market_id="r3")]
    dna = build_dna("0xa", ps, now=NOW)
    assert dna.n_positions == 3 and dna.n_closed == 2
    assert dna.n_markets == 3 and dna.n_categories == 2


def test_build_population_usa_la_misma_referencia_para_todos():
    """Alpha/beta tienen que ser comparables entre wallets."""
    por_wallet = {
        "0xa": [pos(roi=0.2, days_ago=d, market_id=f"s{d}") for d in (30, 20, 10)],
        "0xb": [pos(roi=-0.1, days_ago=d, market_id=f"t{d}") for d in (30, 20, 10)],
    }
    dnas = build_population(por_wallet, now=NOW)
    assert [d.wallet for d in dnas] == ["0xa", "0xb"]
    assert dnas[0].metrics["alpha"] != dnas[1].metrics["alpha"]


def test_estadistica_de_poblacion_y_estandarizacion():
    por_wallet = {
        f"0x{i}": [pos(roi=i / 10.0, market_id=f"u{i}-{j}") for j in range(3)]
        for i in range(5)
    }
    dnas = build_population(por_wallet, now=NOW)
    pop = population_stats(dnas)
    assert pop.n == 5 and pop.stdev["roi_historical"] > 0
    z = pop.standardize(dnas[0])
    assert len(z) == 19
    # La wallet con el peor ROI queda por debajo de la media.
    assert z[DNA_FEATURES.index("roi_historical")] < 0


def test_poblacion_de_una_sola_wallet_no_distingue_a_nadie():
    dnas = build_population({"0xa": [pos(), pos(roi=0.3, market_id="v")]}, now=NOW)
    pop = population_stats(dnas)
    assert pop.standardize(dnas[0]) == [0.0] * 19


def test_el_perfil_es_determinista():
    ps = [pos(roi=0.2, market_id="w1"), pos(roi=-0.1, market_id="w2")]
    assert build_dna("0xa", ps, now=NOW).vector() == build_dna("0xa", ps, now=NOW).vector()
