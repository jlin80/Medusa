"""Order Book Imbalance: presion compradora/vendedora en el libro.

Hipotesis: un desequilibrio fuerte de profundidad cerca del mid (muchos mas
USDC en bids que en asks, o al reves) refleja presion direccional que el precio
aun no ha incorporado del todo.

Solo cuenta la profundidad DENTRO de una banda alrededor del mid: los niveles
lejanos suelen ser ordenes stale o de market makers que nunca se cruzaran, y
contarlos fabrica desequilibrios falsos.

Sin evidencia historica en Polymarket: nace en SHADOW y solo el historial de
señales resueltas puede ganarle peso de capital.
"""

from __future__ import annotations

from medusa.core.models import Market, MarketContext, OrderBook
from medusa.strategies.base import Strategy, StrategySignal


def _band_liquidity(book: OrderBook, band: float) -> tuple[float, float]:
    """(USDC en bids, USDC en asks) dentro de mid +/- band."""
    mid = book.mid
    bid_liq = sum(p * s for p, s in book.bids if p >= mid - band)
    ask_liq = sum(p * s for p, s in book.asks if p <= mid + band)
    return bid_liq, ask_liq


class OrderBookImbalanceStrategy(Strategy):
    name = "order_book_imbalance"

    def evaluate(self, m: Market, ctx: MarketContext) -> StrategySignal | None:
        book = ctx.book_yes
        if book is None or not book.bids or not book.asks:
            return None

        bid_liq, ask_liq = _band_liquidity(book, self.s.imbalance_band)
        total = bid_liq + ask_liq
        if total < 200.0:   # sin profundidad real no hay señal que leer
            return None

        imbalance = (bid_liq - ask_liq) / total   # -1..1, positivo = presion compradora
        if abs(imbalance) < self.s.imbalance_min_ratio:
            return None

        prob_yes = m.yes_price + imbalance * self.s.imbalance_beta
        depth_conf = min(1.0, total / 2000.0)
        return self._directional_signal(
            m, ctx,
            prob_yes=prob_yes,
            confidence=self._market_confidence(m) * (0.6 + 0.4 * depth_conf),
            score=abs(imbalance) * 100.0,
            explanation=(
                f"Desequilibrio {imbalance:+.2f} en banda ±{self.s.imbalance_band} "
                f"del mid (bids ${bid_liq:.0f} vs asks ${ask_liq:.0f})"
            ),
            valid_conditions=(
                "Libros con >= $200 de profundidad en banda. Señal "
                "microestructural de corto plazo, sin validacion historica: "
                "shadow hasta que las señales resueltas demuestren edge."
            ),
        )
