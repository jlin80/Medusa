"""WALLET DNA: de posiciones a un perfil de 19 numeros.

El ADN es la representacion canonica de una wallet en todo el subsistema.
Scoring, reputacion, clusters y similitud NO vuelven a mirar las posiciones:
trabajan sobre este vector. Esa separacion es la que hace que el perfil sea
auditable (19 numeros con definicion escrita) en vez de una caja negra.

Todo lo de aqui es PURO: `build_dna` no abre sesiones, no llama a la Data API y
recibe el "ahora" como argumento.
"""

from __future__ import annotations

import datetime as dt
from typing import Sequence

from medusa.intelligence.wallet.stats import mean, stdev
from medusa.intelligence.wallet.wallet_dna import metrics as m
from medusa.intelligence.wallet.types import (
    DNA_FEATURES,
    PopulationStats,
    WalletDNA,
    WalletPosition,
)

__all__ = [
    "DNA_FEATURES",
    "build_dna",
    "build_population",
    "population_stats",
    "metrics",
]


def build_dna(
    wallet: str,
    positions: Sequence[WalletPosition],
    *,
    now: dt.datetime,
    recent_days: float = 30.0,
    half_life_days: float = 30.0,
    bucket_days: int = 7,
    population_by_bucket: dict[str, float] | None = None,
) -> WalletDNA:
    """Perfil completo de una wallet. Sin posiciones devuelve un ADN a ceros.

    Un ADN a ceros es una afirmacion honesta ("no hay datos"), y aguas abajo la
    reputacion lo castiga via `reliability` y la muestra. Lo que NO se hace es
    saltarse la wallet en silencio: que exista con ceros es informacion.
    """
    pop = population_by_bucket or {}
    values = {
        "roi_historical": m.roi_historical(positions),
        "roi_recent": m.roi_recent(positions, now, recent_days),
        "sharpe": m.sharpe(positions),
        "win_rate": m.win_rate(positions),
        "consistency": m.consistency(positions),
        "trade_frequency": m.trade_frequency(positions),
        "entry_timing": m.entry_timing(positions),
        "exit_timing": m.exit_timing(positions),
        "liquidity_preference": m.liquidity_preference(positions),
        "spread_preference": m.spread_preference(positions),
        "category_expertise": m.category_expertise(positions),
        "conviction": m.conviction(positions),
        "alpha": m.alpha(positions, pop, bucket_days),
        "beta": m.beta(positions, pop, bucket_days),
        "drawdown": m.drawdown(positions),
        "volatility": m.volatility(positions),
        "reliability": m.reliability(positions),
        "freshness": m.freshness(positions, now, half_life_days),
        "decay": m.decay(positions, now, recent_days),
    }
    # Candado de contrato: el vector y su definicion no pueden divergir nunca.
    assert set(values) == set(DNA_FEATURES), "el ADN no cuadra con DNA_FEATURES"

    stamps_open = [p.opened_at for p in positions if p.opened_at is not None]
    stamps_any = [p.closed_at or p.opened_at for p in positions
                  if (p.closed_at or p.opened_at) is not None]
    closed = m.closed_positions(positions)
    return WalletDNA(
        wallet=wallet,
        metrics={k: float(v) for k, v in values.items()},
        n_positions=len(positions),
        n_closed=len(closed),
        n_markets=len({p.market_id for p in positions if p.market_id}),
        n_categories=len({p.category for p in positions if p.category}),
        first_trade=min(stamps_open) if stamps_open else None,
        last_trade=max(stamps_any) if stamps_any else None,
        categories=m.category_breakdown(positions),
        ts=now,
    )


def build_population(
    positions_by_wallet: dict[str, list[WalletPosition]],
    *,
    now: dt.datetime,
    recent_days: float = 30.0,
    half_life_days: float = 30.0,
    bucket_days: int = 7,
) -> list[WalletDNA]:
    """ADN de todas las wallets, con alpha/beta medidos contra la MISMA
    poblacion.

    Se calcula primero el ROI de la poblacion por cubo temporal y despues cada
    perfil. Hacerlo al reves (cada wallet contra su propia referencia) daria
    alphas incomparables entre si, y todo el ranking dejaria de significar nada.
    """
    todas: list[WalletPosition] = []
    for rows in positions_by_wallet.values():
        todas.extend(rows)
    pop = m.population_buckets(todas, bucket_days)
    return [
        build_dna(wallet, rows, now=now, recent_days=recent_days,
                  half_life_days=half_life_days, bucket_days=bucket_days,
                  population_by_bucket=pop)
        for wallet, rows in sorted(positions_by_wallet.items())
    ]


def population_stats(dnas: Sequence[WalletDNA]) -> PopulationStats:
    """Media y desviacion de cada metrica en la poblacion analizada.

    Es la referencia contra la que se estandariza todo lo demas. Con una sola
    wallet las desviaciones son 0 y la estandarizacion devuelve ceros: correcto,
    porque una poblacion de uno no distingue a nadie.
    """
    if not dnas:
        return PopulationStats(n=0)
    mu, sd = {}, {}
    for name in DNA_FEATURES:
        vals = [float(d.metrics.get(name, 0.0)) for d in dnas]
        mu[name] = mean(vals)
        sd[name] = stdev(vals)
    return PopulationStats(mean=mu, stdev=sd, n=len(dnas))
