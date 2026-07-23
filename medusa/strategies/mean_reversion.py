"""Mean Reversion: el baseline original de Medusa, hoy neutralizado.

Hipotesis: el mercado sobre-reacciona y los precios extremos revierten hacia
0.5. CONTRASTADA Y RECHAZADA con datos reales (research/calibration_study.py,
2026-07-16, n=1856 mercados resueltos, IC de Wilson, doble horizonte):
Polymarket esta bien calibrado; apostar reversion es apostar contra un mercado
que acierta, y solo paga el spread (el paper lo confirmo: ROI -7.9% en ~3h).

Se conserva como estrategia por dos razones: (1) es el enchufe de experimentos
via REVERSION_K en .env, y (2) mantiene la compatibilidad con el antiguo
Prediction Engine, cuya logica era exactamente esta. Con k=0 (default) no emite
ninguna señal.
"""

from __future__ import annotations

from medusa.core.models import Market, MarketContext
from medusa.strategies.base import Strategy, StrategySignal


class MeanReversionStrategy(Strategy):
    name = "mean_reversion"

    def evaluate(self, m: Market, ctx: MarketContext) -> StrategySignal | None:
        k = self.s.reversion_k
        if k == 0.0:
            return None   # sin señal, a proposito: el estudio dice que pierde

        prob_yes = m.yes_price + k * (0.5 - m.yes_price)
        pull = abs(prob_yes - m.yes_price)
        return self._directional_signal(
            m, ctx,
            prob_yes=prob_yes,
            confidence=self._market_confidence(m),
            score=min(100.0, pull * 1000.0),
            explanation=(
                f"Reversion k={k}: precio {m.yes_price:.3f} -> estimado "
                f"{prob_yes:.3f} (tira {pull:+.3f} hacia 0.5)"
            ),
            valid_conditions=(
                "SOLO EXPERIMENTOS (REVERSION_K>0). El estudio de calibracion "
                "(n=1856) demostro que esta hipotesis NO tiene edge: Polymarket "
                "esta bien calibrado y la reversion solo paga spread."
            ),
        )
