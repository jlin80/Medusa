"""WALLET SCORING: del ADN a un score comparable, y a la importancia de cada
metrica.

Dos ideas gobiernan este modulo:

1. **El score es RELATIVO a la poblacion analizada, no a constantes.** Se
   estandariza cada metrica contra la media y desviacion de la propia poblacion
   y se combina en un z ponderado que pasa por una logistica a [0,1]. Fijar
   umbrales absolutos ("ROI > 0.15 = buena") seria meter una opinion a mano y
   que el sistema la heredase para siempre; el proyecto ya tiene medido lo que
   cuesta confundir una opinion con un dato.

2. **Solo puntuan las metricas con direccion inequivoca.** `roi`, `sharpe`,
   `alpha`, `reliability`... suben; `drawdown` y `volatility` restan. Las demas
   (timings, preferencias, frecuencia, beta, conviction) describen ESTILO, no
   calidad: decidir que entrar tarde es mejor que entrar pronto seria inventarse
   un hallazgo que nadie ha medido. Se quedan fuera del score y siguen enteras
   en el ADN, en los clusters y en la similitud.

El score NO es una recomendacion de copiar a nadie. Es una coordenada.
"""

from __future__ import annotations

import math
from typing import Sequence

from medusa.intelligence.wallet.stats import clamp, mean, pearson, stdev
from medusa.intelligence.wallet.types import (
    DNA_FEATURES,
    DNA_HIGHER_IS_BETTER,
    DNA_LOWER_IS_BETTER,
    PopulationStats,
    WalletDNA,
)

__all__ = ["score_wallet", "score_population", "feature_importance", "scoring_features"]

# Cuanto se recorta cada z antes de agregar. Sin esto, una sola wallet con un
# Sharpe absurdo (n=2) domina la escala de toda la poblacion y aplana al resto
# contra 0.5. Recortar a 3 sigmas conserva el orden y mata el apalancamiento del
# caso extremo.
_Z_CLIP: float = 3.0


def scoring_features() -> tuple[str, ...]:
    """Metricas que entran en el score, en orden canonico."""
    return tuple(f for f in DNA_FEATURES
                 if f in DNA_HIGHER_IS_BETTER or f in DNA_LOWER_IS_BETTER)


def _direction(name: str) -> float:
    return 1.0 if name in DNA_HIGHER_IS_BETTER else -1.0


def score_wallet(
    dna: WalletDNA, pop: PopulationStats, weights: dict[str, float] | None = None,
) -> dict:
    """Score compuesto de una wallet, en [0,1].

    Devuelve tambien la CONTRIBUCION de cada metrica: sin eso el score seria un
    numero sin defensa posible, y la primera pregunta razonable ("¿por que esta
    wallet puntua 0.8?") no tendria respuesta.

    Con una poblacion degenerada (desviaciones 0) todos los z son 0 y el score
    es exactamente 0.5: "no se puede distinguir a nadie". Es la respuesta
    correcta, no un fallo.
    """
    w = weights or {}
    zs = dict(zip(DNA_FEATURES, pop.standardize(dna)))
    contributions: dict[str, float] = {}
    total_w = 0.0
    acc = 0.0
    for name in scoring_features():
        weight = float(w.get(name, 1.0))
        if weight <= 0:
            continue
        z = max(-_Z_CLIP, min(_Z_CLIP, zs.get(name, 0.0))) * _direction(name)
        contributions[name] = round(z * weight, 6)
        acc += z * weight
        total_w += weight
    z_mean = acc / total_w if total_w else 0.0
    return {
        "wallet": dna.wallet,
        # Logistica sobre el z medio: comprime a [0,1] sin cortar y mantiene el
        # orden. 0.5 = exactamente la media de la poblacion.
        "score": round(1.0 / (1.0 + math.exp(-z_mean)), 6),
        "z_mean": round(z_mean, 6),
        "contributions": contributions,
        "n_closed": dna.n_closed,
    }


def score_population(
    dnas: Sequence[WalletDNA], pop: PopulationStats,
    weights: dict[str, float] | None = None,
) -> list[dict]:
    """Scores de toda la poblacion, de mayor a menor."""
    scored = [score_wallet(d, pop, weights) for d in dnas]
    scored.sort(key=lambda s: s["score"], reverse=True)
    for rank, row in enumerate(scored, start=1):
        row["rank"] = rank
    return scored


def feature_importance(
    dnas: Sequence[WalletDNA], pop: PopulationStats, target: str = "roi_recent",
) -> list[dict]:
    """Importancia DESCRIPTIVA de cada metrica del ADN.

    Se combinan dos cosas que responden a preguntas distintas:

      dispersion  -> ¿esta metrica separa a unas wallets de otras, o es la misma
                     para todas? (desviacion normalizada por |media|)
      association -> ¿se mueve junto con el objetivo? (|Pearson| contra `target`,
                     por defecto el ROI reciente)

      importance = dispersion x association

    Que quede claro que es y que no es: **es asociacion en la muestra, no
    causalidad ni poder predictivo**. No hay validacion fuera de muestra, no hay
    control por categoria ni por precio, y el objetivo (`roi_recent`) es parte
    del propio ADN. Sirve para decidir QUE mirar, jamas para justificar una
    operacion. Es el mismo listón que se aplico a los descubrimientos del MIG.

    Una metrica sin dispersion en la poblacion sale con importancia 0: no
    distingue a nadie, asi que no puede estar explicando nada.
    """
    if len(dnas) < 3:
        return []
    targets = [float(d.metrics.get(target, 0.0)) for d in dnas]
    out: list[dict] = []
    for name in DNA_FEATURES:
        vals = [float(d.metrics.get(name, 0.0)) for d in dnas]
        sd = stdev(vals)
        mu = mean(vals)
        dispersion = clamp(sd / (abs(mu) + 1e-9)) if sd > 0 else 0.0
        assoc = 0.0 if name == target else abs(pearson(vals, targets))
        out.append({
            "feature": name,
            "importance": round(dispersion * assoc, 6),
            "dispersion": round(dispersion, 6),
            "association": round(assoc, 6),
            "mean": round(mu, 6),
            "stdev": round(sd, 6),
            "in_score": name in DNA_HIGHER_IS_BETTER or name in DNA_LOWER_IS_BETTER,
        })
    out.sort(key=lambda r: r["importance"], reverse=True)
    return out
