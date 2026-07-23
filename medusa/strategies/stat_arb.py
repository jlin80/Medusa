"""Statistical Arbitrage intra-mercado: YES + NO por menos de $1.

Un par YES+NO siempre liquida exactamente en $1. Si ask_YES + ask_NO < $1 se
puede comprar el par y embolsar la diferencia SIN riesgo direccional. Y al
reves: si bid_YES + bid_NO > $1 se puede mintear el par por $1 y venderlo mas
caro.

Realidad medida (research/arb_scan.py, 110 mercados con ambos libros): CERO
oportunidades; el minimo observado fue $1.001. Los bots dedicados lo aspiran en
segundos. Se mantiene porque es la unica señal de esta lista con ganancia
GARANTIZADA cuando aparece, y escanear el top del ranking cada ciclo es gratis
(el libro NO ya esta pedido).

can_trade=False: ejecutar esto exige comprar DOS patas a la vez y el camino de
ordenes actual (una orden, un token) no lo soporta; una sola pata seria una
posicion direccional, exactamente lo contrario de un arbitraje. Cuando
aparece, se registra y se avisa por Discord.
"""

from __future__ import annotations

from medusa.core.models import Market, MarketContext
from medusa.strategies.base import Strategy, StrategySignal


class StatArbStrategy(Strategy):
    name = "stat_arb"
    needs_book_no = True
    can_trade = False   # dos patas simultaneas: fuera del camino de ordenes actual

    def evaluate(self, m: Market, ctx: MarketContext) -> StrategySignal | None:
        yes, no = ctx.book_yes, ctx.book_no
        if yes is None or no is None:
            return None

        signal = None
        # Pata compradora: comprar YES+NO por debajo de $1.
        if yes.best_ask > 0 and no.best_ask > 0:
            pair_cost = yes.best_ask + no.best_ask
            profit = 1.0 - pair_cost
            if profit >= self.s.stat_arb_min_profit:
                signal = self._pair_signal(
                    m, profit, pair_cost,
                    f"COMPRA del par: ask_YES {yes.best_ask:.3f} + ask_NO "
                    f"{no.best_ask:.3f} = {pair_cost:.3f} < $1 "
                    f"(ganancia garantizada {profit:.3f}/par)",
                )
        # Pata vendedora: mintear el par por $1 y venderlo por encima.
        if signal is None and yes.best_bid > 0 and no.best_bid > 0:
            pair_value = yes.best_bid + no.best_bid
            profit = pair_value - 1.0
            if profit >= self.s.stat_arb_min_profit:
                signal = self._pair_signal(
                    m, profit, pair_value,
                    f"VENTA del par: bid_YES {yes.best_bid:.3f} + bid_NO "
                    f"{no.best_bid:.3f} = {pair_value:.3f} > $1 "
                    f"(ganancia garantizada {profit:.3f}/par)",
                )
        return signal

    def _pair_signal(self, m: Market, profit: float, pair_price: float,
                     explanation: str) -> StrategySignal:
        # entry_price = precio del PAR: el resolver especial-casea stat_arb y
        # calcula el ROI como profit/pair_price, independiente del resultado
        # del mercado (la ganancia esta bloqueada al entrar).
        return StrategySignal(
            strategy=self.name,
            market_id=m.id,
            question=m.question,
            category=m.medusa_category,
            outcome="YES",              # representativo; el arb no tiene lado
            token_id=m.yes_token_id,
            entry_price=round(pair_price, 4),
            signal_prob=1.0,            # la ganancia no depende del resultado
            market_prob=round(m.yes_price, 4),
            edge=round(profit, 4),
            confidence=0.99,
            score=min(100.0, profit * 2000.0),
            explanation=explanation,
            valid_conditions=(
                "Ambos libros con precios ejecutables y suma fuera de $1 por "
                f">= {self.s.stat_arb_min_profit}. Ganancia sin riesgo "
                "direccional, pero requiere ejecutar dos patas a la vez: "
                "solo alerta, no opera."
            ),
            spread=m.spread,
            liquidity=m.liquidity,
            end_date=m.end_date,
            tradeable=False,
        )
