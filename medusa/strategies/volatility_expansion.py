"""Volatility Expansion: la volatilidad reciente rompe su regimen previo.

Hipotesis: cuando la volatilidad de las ultimas horas es un multiplo de la
volatilidad base de los dias anteriores, algo esta pasando (informacion nueva
entrando). Si ademas hay deriva direccional clara, la dislocacion tiende a
continuar en esa direccion mientras el mercado digiere la noticia.

Necesita el historico de precios (CLOB /prices-history), que el scanner cachea
15 min: la forma de la curva no cambia entre ciclos de 60s.

La volatilidad se mide como media de |Δprecio| entre puntos consecutivos
(aritmetica pura de Python: sin numpy en la CPU del CT202). Sin evidencia
historica en Polymarket: nace en SHADOW.
"""

from __future__ import annotations

from medusa.core.models import Market, MarketContext
from medusa.strategies.base import Strategy, StrategySignal

# Ventanas sobre el historico con fidelity=60min: 6 puntos ~ 6h recientes,
# 48 puntos previos ~ 2 dias de linea base.
_RECENT_POINTS = 6
_BASELINE_POINTS = 48

# Tope de edge atribuible a la deriva.
_MAX_EDGE = 0.06
_DRIFT_BETA = 0.5


def _mean_abs_move(prices: list[float]) -> float:
    if len(prices) < 2:
        return 0.0
    total = sum(abs(prices[i] - prices[i - 1]) for i in range(1, len(prices)))
    return total / (len(prices) - 1)


class VolatilityExpansionStrategy(Strategy):
    name = "volatility_expansion"
    needs_history = True

    def evaluate(self, m: Market, ctx: MarketContext) -> StrategySignal | None:
        prices = [p for _, p in ctx.history]
        if len(prices) < _RECENT_POINTS + 12:   # sin linea base suficiente no hay regimen
            return None

        recent = prices[-_RECENT_POINTS:]
        baseline = prices[-(_RECENT_POINTS + _BASELINE_POINTS):-_RECENT_POINTS]

        vol_recent = _mean_abs_move(recent)
        vol_base = max(_mean_abs_move(baseline), self.s.vol_min_baseline)
        ratio = vol_recent / vol_base
        if ratio < self.s.vol_expansion_ratio:
            return None

        drift = recent[-1] - recent[0]
        if abs(drift) < self.s.vol_min_drift:
            return None   # expansion sin direccion: no hay lado que tomar

        direction = 1.0 if drift > 0 else -1.0
        edge_mag = min(_MAX_EDGE, abs(drift) * _DRIFT_BETA)
        prob_yes = m.yes_price + direction * edge_mag

        strength = min(1.0, (ratio - self.s.vol_expansion_ratio)
                       / (2 * self.s.vol_expansion_ratio))
        return self._directional_signal(
            m, ctx,
            prob_yes=prob_yes,
            confidence=self._market_confidence(m) * (0.6 + 0.4 * strength),
            score=min(100.0, ratio / self.s.vol_expansion_ratio * 50.0),
            explanation=(
                f"Vol 6h {vol_recent:.4f} = {ratio:.1f}x la base 48h "
                f"{vol_base:.4f}, con deriva {drift:+.3f}"
            ),
            valid_conditions=(
                f"Historico >= {_RECENT_POINTS + 12} puntos, expansion >= "
                f"{self.s.vol_expansion_ratio}x y deriva >= "
                f"{self.s.vol_min_drift}. Sin validacion historica: shadow "
                "hasta demostrar edge."
            ),
        )
