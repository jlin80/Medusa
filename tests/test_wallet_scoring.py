"""Tests de scoring, reputacion e importancia de features."""

from __future__ import annotations

import datetime as dt

import pytest

from medusa.intelligence.wallet.types import DNA_FEATURES, PopulationStats, WalletDNA
from medusa.intelligence.wallet.wallet_reputation import reputation_of, reputation_population, sample_factor
from medusa.intelligence.wallet.wallet_scoring import (
    feature_importance,
    score_population,
    score_wallet,
    scoring_features,
)

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 7, 28, 12, 0, tzinfo=UTC)


def dna(wallet: str, n_closed: int = 50, **metrics) -> WalletDNA:
    base = {name: 0.0 for name in DNA_FEATURES}
    base.update(metrics)
    return WalletDNA(wallet=wallet, metrics=base, n_closed=n_closed,
                     n_positions=n_closed, ts=NOW)


def pop_of(dnas) -> PopulationStats:
    from medusa.intelligence.wallet.wallet_dna import population_stats

    return population_stats(dnas)


# ------------------------------------------------------------------ score --
def test_solo_puntuan_las_metricas_con_direccion_inequivoca():
    """Los timings, las preferencias, la frecuencia, beta y la conviccion son
    ESTILO: meterlos en el score seria inventarse un hallazgo."""
    puntuadas = set(scoring_features())
    assert "entry_timing" not in puntuadas
    assert "exit_timing" not in puntuadas
    assert "liquidity_preference" not in puntuadas
    assert "spread_preference" not in puntuadas
    assert "trade_frequency" not in puntuadas
    assert "conviction" not in puntuadas
    assert "beta" not in puntuadas
    assert {"roi_historical", "sharpe", "alpha", "reliability", "drawdown"} <= puntuadas


def test_poblacion_sin_dispersion_deja_a_todos_en_la_media():
    ds = [dna(f"0x{i}", roi_historical=0.1) for i in range(4)]
    pop = pop_of(ds)
    assert score_wallet(ds[0], pop)["score"] == pytest.approx(0.5)


def test_mejor_roi_puntua_mas_alto():
    ds = [dna("0xa", roi_historical=0.5, sharpe=2.0, alpha=0.4),
          dna("0xb", roi_historical=-0.3, sharpe=-1.0, alpha=-0.2),
          dna("0xc", roi_historical=0.0, sharpe=0.0, alpha=0.0)]
    pop = pop_of(ds)
    scores = {r["wallet"]: r["score"] for r in score_population(ds, pop)}
    assert scores["0xa"] > scores["0xc"] > scores["0xb"]


def test_drawdown_y_volatilidad_restan():
    """Son las dos metricas donde MENOS es mejor."""
    ds = [dna("0xa", roi_historical=0.2, drawdown=0.05, volatility=0.05),
          dna("0xb", roi_historical=0.2, drawdown=0.9, volatility=0.9)]
    pop = pop_of(ds)
    assert score_wallet(ds[0], pop)["score"] > score_wallet(ds[1], pop)["score"]


def test_el_score_desglosa_sus_contribuciones():
    """Un score sin defensa posible no es auditable."""
    ds = [dna("0xa", roi_historical=0.5), dna("0xb", roi_historical=-0.5)]
    out = score_wallet(ds[0], pop_of(ds))
    assert out["contributions"]["roi_historical"] > 0
    assert set(out["contributions"]) <= set(scoring_features())


def test_un_extremo_no_aplasta_a_la_poblacion():
    """El recorte a 3 sigmas evita que una wallet absurda domine la escala."""
    normales = [dna(f"0x{i}", sharpe=float(i) / 10.0) for i in range(10)]
    con_extremo = normales + [dna("0xzz", sharpe=1000.0)]
    pop = pop_of(con_extremo)
    top = score_wallet(con_extremo[-1], pop)
    assert top["score"] <= 1.0
    assert top["z_mean"] <= 3.0


def test_el_ranking_va_de_mayor_a_menor():
    ds = [dna("0xa", roi_historical=0.1), dna("0xb", roi_historical=0.5),
          dna("0xc", roi_historical=-0.2)]
    rows = score_population(ds, pop_of(ds))
    assert [r["rank"] for r in rows] == [1, 2, 3]
    assert rows[0]["wallet"] == "0xb"


# ------------------------------------------------------------- reputacion --
def test_el_factor_de_muestra_es_suave_y_vale_medio_en_el_umbral():
    assert sample_factor(30, 30) == pytest.approx(0.5)
    assert sample_factor(0, 30) == 0.0
    assert sample_factor(300, 30) > 0.9
    # Sin escalon: 29 y 31 no pueden dar veredictos opuestos.
    assert abs(sample_factor(29, 30) - sample_factor(31, 30)) < 0.02


def test_muestra_ridicula_hunde_la_reputacion_aunque_el_score_sea_alto():
    # 0xa domina a la poblacion en TODAS las metricas puntuables, pero solo
    # tiene 3 posiciones cerradas.
    mejor = dna("0xa", n_closed=3, roi_historical=0.9, roi_recent=0.9, sharpe=3.0,
                win_rate=0.9, consistency=1.0, alpha=0.8, reliability=0.9,
                freshness=1.0, decay=0.5, drawdown=0.0, volatility=0.0)
    resto = [dna(f"0x{i}", n_closed=3, roi_historical=-0.1 * i, sharpe=-0.5 * i,
                 alpha=-0.1 * i, drawdown=0.5, volatility=0.5) for i in range(1, 9)]
    pop = pop_of([mejor] + resto)
    rep = reputation_of(mejor, pop, min_samples=30)
    assert rep["score"] > 0.8            # el score si es alto
    assert rep["reputation"] < 0.15      # la reputacion no se lo cree
    assert rep["sample_factor"] < 0.1    # y el motivo es la muestra


def test_una_wallet_inactiva_pierde_la_reputacion():
    ds = [dna("0xa", n_closed=100, roi_historical=0.5, freshness=0.01, consistency=1.0),
          dna("0xb", n_closed=100, roi_historical=-0.5, freshness=1.0)]
    assert reputation_of(ds[0], pop_of(ds), min_samples=30)["reputation"] < 0.05


def test_un_edge_derrumbandose_borra_la_reputacion():
    """decay = -1 deja el factor de estabilidad en 0 y con el la reputacion."""
    ds = [dna("0xa", n_closed=100, roi_historical=0.5, freshness=1.0,
              consistency=0.0, decay=-1.0),
          dna("0xb", n_closed=100, roi_historical=-0.5)]
    rep = reputation_of(ds[0], pop_of(ds), min_samples=30)
    assert rep["stability"] == 0.0 and rep["reputation"] == 0.0


def test_la_reputacion_desglosa_sus_tres_factores():
    ds = [dna("0xa", n_closed=60, roi_historical=0.3, freshness=0.8, consistency=0.7),
          dna("0xb", n_closed=60, roi_historical=-0.3)]
    rep = reputation_of(ds[0], pop_of(ds), min_samples=30)
    assert set(("sample_factor", "freshness", "stability")) <= set(rep)
    esperado = rep["score"] * rep["sample_factor"] * rep["freshness"] * rep["stability"]
    assert rep["reputation"] == pytest.approx(esperado, abs=1e-6)


def test_reputacion_siempre_en_cero_uno():
    ds = [dna(f"0x{i}", n_closed=i * 20, roi_historical=(i - 2) / 2.0,
              freshness=i / 4.0, consistency=i / 4.0, decay=(i - 2) / 2.0)
          for i in range(5)]
    for row in reputation_population(ds, pop_of(ds)):
        assert 0.0 <= row["reputation"] <= 1.0


# ----------------------------------------------- importancia de features --
def test_importancia_necesita_poblacion():
    assert feature_importance([dna("0xa")], pop_of([dna("0xa")])) == []


def test_una_metrica_constante_no_explica_nada():
    ds = [dna(f"0x{i}", roi_recent=float(i) / 10.0, win_rate=0.5) for i in range(6)]
    filas = {r["feature"]: r for r in feature_importance(ds, pop_of(ds))}
    assert filas["win_rate"]["importance"] == 0.0
    assert filas["win_rate"]["dispersion"] == 0.0


def test_una_metrica_que_acompana_al_objetivo_puntua_alto():
    ds = [dna(f"0x{i}", roi_recent=float(i) / 10.0, sharpe=float(i),
              conviction=0.5) for i in range(8)]
    filas = {r["feature"]: r for r in feature_importance(ds, pop_of(ds), target="roi_recent")}
    assert filas["sharpe"]["association"] > 0.95
    assert filas["sharpe"]["importance"] > filas["conviction"]["importance"]


def test_el_objetivo_no_se_explica_a_si_mismo():
    ds = [dna(f"0x{i}", roi_recent=float(i)) for i in range(6)]
    filas = {r["feature"]: r for r in feature_importance(ds, pop_of(ds), target="roi_recent")}
    assert filas["roi_recent"]["association"] == 0.0


def test_la_importancia_cubre_las_19_metricas_y_marca_cuales_puntuan():
    ds = [dna(f"0x{i}", roi_recent=float(i), sharpe=float(i)) for i in range(6)]
    filas = feature_importance(ds, pop_of(ds))
    assert {r["feature"] for r in filas} == set(DNA_FEATURES)
    assert {r["feature"] for r in filas if r["in_score"]} == set(scoring_features())
