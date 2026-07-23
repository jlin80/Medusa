"""Momentum: continuacion del movimiento reciente del precio.

Hipotesis: cuando el precio se ha movido con fuerza en 24h es porque hay
informacion nueva incorporandose, y la incorporacion suele ser gradual (la
gente infra-reacciona), asi que el movimiento tiende a continuar.

Evidencia hasta hoy: research/momentum_study.py (2026-07-16) NO encontro señal
en el horizonte que midio (cambio T-48h->T-24h vs resolucion final; delta
estratificado +0.012, z=0.17). Esta estrategia corre en SHADOW para medir lo
que el estudio no cubrio: horizontes cortos y desglose por categoria. Si el
historial de señales resueltas tampoco muestra edge, el asignador de capital
la mantendra en peso 0 y jamas operara.
"""

from __future__ import annotations

from medusa.core.models import Market, MarketContext
from medusa.strategies.base import Strategy, StrategySignal

# Tope de edge que el momentum puede atribuirse, por fuerte que sea el
# movimiento: proyectar mas alla es extrapolacion sin datos.
_MAX_EDGE = 0.08


class MomentumStrategy(Strategy):
    name = "momentum"

    def evaluate(self, m: Market, ctx: MarketContext) -> StrategySignal | None:
        move = m.price_change_24h
        if abs(move) < self.s.momentum_min_move:
            return None

        direction = 1.0 if move > 0 else -1.0
        projected = direction * min(abs(move) * self.s.momentum_beta, _MAX_EDGE)
        prob_yes = m.yes_price + projected

        strength = min(1.0, abs(move) / 0.15)   # Δ24h de 15pts satura la fuerza
        return self._directional_signal(
            m, ctx,
            prob_yes=prob_yes,
            confidence=self._market_confidence(m) * (0.7 + 0.3 * strength),
            score=strength * 100.0,
            explanation=(
                f"Δ24h {move:+.3f} >= umbral {self.s.momentum_min_move}: se "
                f"proyecta continuacion de {projected:+.3f} "
                f"(beta {self.s.momentum_beta})"
            ),
            valid_conditions=(
                "Mercados liquidos con movimiento 24h fuerte. Hipotesis NO "
                "validada: el estudio de momentum a horizonte-resolucion dio "
                "z=0.17. Shadow hasta que el historial por categoria diga otra "
                "cosa."
            ),
        )
