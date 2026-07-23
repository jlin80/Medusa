"""LEGACY: shim de compatibilidad del antiguo Prediction Engine.

Su logica (baseline de reversion a la media, neutralizado con k=0 tras el
estudio de calibracion) vive ahora en
medusa/strategies/mean_reversion.py, junto al resto de estrategias del sistema
multi-estrategia. El engine ya NO importa este modulo; se conserva solo por si
algun script externo lo referencia.

evaluate() delega en MeanReversionStrategy y devuelve la misma Opportunity de
siempre (accion "skip" cuando no hay señal, exactamente como antes con k=0).
"""

from __future__ import annotations

from medusa.config import get_settings
from medusa.core.models import Market, MarketContext, Opportunity


class PredictionEngine:
    def __init__(self, log) -> None:
        self.log = log
        self.s = get_settings()
        from medusa.strategies.mean_reversion import MeanReversionStrategy
        self._strategy = MeanReversionStrategy(log)

    def evaluate(self, m: Market) -> Opportunity | None:
        if not (0.0 < m.yes_price < 1.0):
            return None
        sig = self._strategy.evaluate(m, MarketContext())
        if sig is None:
            # k=0: sin señal. Mismo contrato que antes: skip explicito.
            return Opportunity(
                market_id=m.id, question=m.question, outcome="YES",
                market_price=m.yes_price, market_prob=m.yes_price,
                medusa_prob=m.yes_price, edge=0.0,
                confidence=self._strategy._market_confidence(m),
                action="skip", reason="sin señal (REVERSION_K=0)",
                token_id=m.yes_token_id, spread=m.spread, liquidity=m.liquidity,
                strategy="mean_reversion", category=m.medusa_category,
            )
        passes = sig.edge >= self.s.min_edge and sig.confidence >= self.s.min_confidence
        return Opportunity(
            market_id=sig.market_id, question=sig.question, outcome=sig.outcome,
            market_price=sig.entry_price, market_prob=sig.market_prob,
            medusa_prob=sig.signal_prob, edge=sig.edge, confidence=sig.confidence,
            action=("buy_yes" if sig.outcome == "YES" else "buy_no") if passes else "skip",
            reason=sig.explanation, token_id=sig.token_id, spread=sig.spread,
            liquidity=sig.liquidity, strategy=sig.strategy, category=sig.category,
            score=sig.score,
        )
