"""Liquidity Breakout: movimiento reciente + lado del libro sin defensa.

Hipotesis: si el precio se esta moviendo (Δ1h) y el lado del libro que deberia
frenar ese movimiento esta fino (poca profundidad de asks en una subida, pocos
bids en una bajada), el movimiento tiene via libre para continuar: no hay
liquidez que lo absorba.

Combina dos datos que ya tenemos gratis: la variacion 1h de Gamma y el libro
que el analisis profundo ya pidio. Sin evidencia historica: nace en SHADOW.
"""

from __future__ import annotations

from medusa.core.models import Market, MarketContext
from medusa.strategies.base import Strategy, StrategySignal
from medusa.strategies.order_book_imbalance import _band_liquidity

# Tope de edge atribuible al breakout.
_MAX_EDGE = 0.06


class LiquidityBreakoutStrategy(Strategy):
    name = "liquidity_breakout"

    def evaluate(self, m: Market, ctx: MarketContext) -> StrategySignal | None:
        book = ctx.book_yes
        move = m.price_change_1h
        if book is None or not book.bids or not book.asks:
            return None
        if abs(move) < self.s.breakout_min_move_1h:
            return None

        bid_liq, ask_liq = _band_liquidity(book, self.s.imbalance_band)
        if bid_liq + ask_liq < 200.0:
            return None

        rising = move > 0
        resistance = ask_liq if rising else bid_liq     # lo que frena el movimiento
        support = bid_liq if rising else ask_liq        # lo que lo empuja
        if support <= 0 or resistance > support * self.s.breakout_thin_ratio:
            return None   # el lado que frena tiene liquidez de sobra: no hay breakout

        thinness = 1.0 - (resistance / max(support, 1e-9))   # 0..1, mas fino = mas señal
        direction = 1.0 if rising else -1.0
        edge_mag = min(_MAX_EDGE, abs(move)) * thinness
        prob_yes = m.yes_price + direction * edge_mag

        return self._directional_signal(
            m, ctx,
            prob_yes=prob_yes,
            confidence=self._market_confidence(m) * (0.6 + 0.4 * thinness),
            score=min(100.0, thinness * abs(move) / self.s.breakout_min_move_1h * 50.0),
            explanation=(
                f"Δ1h {move:+.3f} con lado {'ask' if rising else 'bid'} fino: "
                f"resistencia ${resistance:.0f} vs empuje ${support:.0f} "
                f"(ratio {resistance / max(support, 1e-9):.2f})"
            ),
            valid_conditions=(
                "Movimiento 1h >= umbral con el lado de resistencia por debajo "
                f"del {self.s.breakout_thin_ratio:.0%} del lado de empuje. Sin "
                "validacion historica: shadow hasta demostrar edge."
            ),
        )
