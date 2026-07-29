"""WALLET REPUTATION: el score, castigado por lo que no sabemos.

Un score alto sobre 4 posiciones cerradas hace ocho meses no es reputacion: es
una anecdota vieja. La reputacion multiplica el score por tres factores que
miden CUANTO merece la pena creerselo:

    reputation = score x sample_factor x freshness x stability

  sample_factor  n/(n+min_samples). Con n = min_samples vale 0.5 y crece hacia 1
                 sin llegar. Mismo umbral que el asignador de capital
                 (ALLOC_MIN_SAMPLES): no puede haber dos definiciones de "hay
                 muestra suficiente" en el mismo sistema.
  freshness      ya viene en el ADN: exp(-dias inactiva / semivida).
  stability      combina consistencia y ausencia de degradacion (`decay`). Una
                 wallet cuyo edge se esta cayendo vale menos HOY que ayer,
                 aunque su historia acumulada siga siendo buena.

Los tres son multiplicativos a proposito: cualquiera de ellos cerca de cero
tiene que poder anular la reputacion por su cuenta. Sumarlos permitiria que una
muestra ridicula se compensara con frescura, que es justo el error que este
modulo existe para impedir.

La reputacion NO autoriza nada. No hay ningun camino desde este numero hasta una
orden: es una feature mas, y la consume quien decide, no este paquete.
"""

from __future__ import annotations

from typing import Sequence

from medusa.intelligence.wallet.stats import clamp
from medusa.intelligence.wallet.types import PopulationStats, WalletDNA
from medusa.intelligence.wallet.wallet_scoring import score_wallet

__all__ = ["reputation_of", "reputation_population", "sample_factor"]


def sample_factor(n: int, min_samples: int) -> float:
    """n/(n+min_samples): saturacion suave, sin escalon en el umbral.

    Un corte duro haria que 29 y 31 posiciones dieran veredictos opuestos, y esa
    discontinuidad no describe nada real.
    """
    if n <= 0 or min_samples <= 0:
        return 0.0
    return n / float(n + min_samples)


def reputation_of(
    dna: WalletDNA, pop: PopulationStats, *, min_samples: int = 30,
    weights: dict[str, float] | None = None,
) -> dict:
    """Reputacion de una wallet, en [0,1], con sus factores desglosados."""
    scored = score_wallet(dna, pop, weights)
    sf = sample_factor(dna.n_closed, min_samples)
    fresh = clamp(float(dna.metrics.get("freshness", 0.0)))
    consistency = clamp(float(dna.metrics.get("consistency", 0.0)))
    # `decay` vive en (-1,1); se remapea a (0,1) para poder multiplicar. Un
    # decay de -1 (edge derrumbado) deja el factor en 0 y borra la reputacion:
    # es exactamente lo que debe pasar.
    decay = (float(dna.metrics.get("decay", 0.0)) + 1.0) / 2.0
    stability = clamp(0.5 * consistency + 0.5 * decay)
    reputation = clamp(scored["score"] * sf * fresh * stability)
    return {
        "wallet": dna.wallet,
        "reputation": round(reputation, 6),
        "score": scored["score"],
        "sample_factor": round(sf, 6),
        "freshness": round(fresh, 6),
        "stability": round(stability, 6),
        "n_closed": dna.n_closed,
        "min_samples": min_samples,
        "contributions": scored["contributions"],
    }


def reputation_population(
    dnas: Sequence[WalletDNA], pop: PopulationStats, *, min_samples: int = 30,
    weights: dict[str, float] | None = None,
) -> list[dict]:
    """Reputacion de toda la poblacion, de mayor a menor."""
    rows = [reputation_of(d, pop, min_samples=min_samples, weights=weights) for d in dnas]
    rows.sort(key=lambda r: r["reputation"], reverse=True)
    for rank, row in enumerate(rows, start=1):
        row["rank"] = rank
    return rows
