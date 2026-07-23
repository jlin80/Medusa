"""Microstructure Intelligence: features de la estructura del mercado.

Produce tres de las variables que la especificacion del Feature Store nombra
explicitamente -- orderbook_score, liquidity_score, volatility_score -- mas un
par de derivadas utiles.

Por que este es el PRIMER modulo del layer:
  - Coste CERO en llamadas nuevas: usa exclusivamente lo que el scanner YA
    trajo en este ciclo (order books y, si algun modulo lo pidio, historico).
    No depende de ninguna fuente externa, ninguna clave y ninguna cuota.
  - No puede fallar por red (needs_network=False).
  - Empieza a poblar el histórico desde el primer ciclo, que es lo que hace
    falta para que algun dia haya algo que entrenar.

Ojo con lo que esto NO es: estas features describen la ESTRUCTURA del mercado
(cuanta liquidez hay, como de sano es el libro, cuanto se mueve), no hacia
donde va el precio. El veredicto de calibracion sigue en pie: el precio ya es
la probabilidad. Estas variables sirven para ponderar y contextualizar otras
señales, y como material para modelos futuros; por si solas no son un edge y no
se deben leer como tal.
"""

from __future__ import annotations

import math

from medusa.core.models import Market, OrderBook
from medusa.intelligence_layer.base import Feature, IntelligenceModule

# Referencias de escala (las mismas del pre-scorer, a proposito: dos escalas
# distintas para el mismo concepto es como se acaba con un dashboard que dice
# una cosa y un modelo que cree otra).
_LIQ_FLOOR, _LIQ_CEIL = 500.0, 100_000.0
_MAX_REL_SPREAD = 0.08


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _log_scale(value: float, floor: float, ceil: float) -> float:
    if value <= floor:
        return 0.0
    return _clamp01(math.log10(value / floor) / math.log10(ceil / floor))


def _book_depth(book: OrderBook, band: float = 0.05) -> tuple[float, float]:
    """(USDC en bids, USDC en asks) dentro de mid +/- band."""
    mid = book.mid
    bid = sum(p * s for p, s in book.bids if p >= mid - band)
    ask = sum(p * s for p, s in book.asks if p <= mid + band)
    return bid, ask


def _mean_abs_move(prices: list[float]) -> float:
    if len(prices) < 2:
        return 0.0
    return sum(abs(prices[i] - prices[i - 1]) for i in range(1, len(prices))) / (len(prices) - 1)


class MicrostructureIntelligence(IntelligenceModule):
    name = "microstructure"
    # 15 min. Entre una foto de liquidez a 5 min y otra a 15 no hay informacion
    # nueva que un modelo pueda aprovechar, y a 5 min el store crece x3
    # (~21.600 filas/dia vs ~7.200). Es el equilibrio entre "histórico util" y
    # la restriccion de consumo minimo del CT202. Ver el calculo en
    # db_models.FeatureRow.
    interval = 900.0
    timeout = 15.0
    needs_network = False
    description = "Salud del libro, liquidez y volatilidad desde datos ya recolectados (sin APIs externas)"

    async def compute(self, markets: list[Market], ctx: dict) -> list[Feature]:
        books = ctx.get("books", {})       # market_id -> OrderBook (YES)
        history = ctx.get("history", {})   # market_id -> [(ts, precio)]
        out: list[Feature] = []

        for m in markets:
            out.append(self._feature(
                m.id, "liquidity_score", _log_scale(m.liquidity, _LIQ_FLOOR, _LIQ_CEIL),
                liquidity_usd=round(m.liquidity, 2),
            ))

            book = books.get(m.id)
            if book is not None and book.bids and book.asks:
                out.extend(self._book_features(m, book))

            prices = [p for _, p in history.get(m.id, [])]
            if len(prices) >= 8:
                out.append(self._volatility_feature(m, prices))

        return out

    def _book_features(self, m: Market, book: OrderBook) -> list[Feature]:
        mid = book.mid
        feats: list[Feature] = []

        # orderbook_score: cuan SANO es el libro para operar (0..1). Combina
        # spread relativo y profundidad total en banda. No dice nada de la
        # direccion; dice si se puede entrar y salir sin regalar el edge.
        rel_spread = ((book.best_ask - book.best_bid) / mid) if mid > 0 else 1.0
        spread_health = 1.0 - _clamp01(rel_spread / _MAX_REL_SPREAD)
        bid_liq, ask_liq = _book_depth(book)
        depth_health = _clamp01((bid_liq + ask_liq) / 2000.0)
        feats.append(self._feature(
            m.id, "orderbook_score", 0.6 * spread_health + 0.4 * depth_health,
            rel_spread=round(rel_spread, 4),
            bid_depth=round(bid_liq, 2), ask_depth=round(ask_liq, 2),
        ))

        # book_imbalance en crudo (-1..1). Se guarda como FEATURE, sin juicio:
        # la estrategia order_book_imbalance decide que hacer con ella; aqui
        # solo se mide y se archiva para el histórico.
        total = bid_liq + ask_liq
        if total > 0:
            feats.append(self._feature(
                m.id, "book_imbalance", (bid_liq - ask_liq) / total,
                depth_usd=round(total, 2),
            ))

        # Coste real de round-trip (spread + slippage + fees), en unidades de
        # probabilidad. Es el listón que cualquier señal debe batir: ~2.2%
        # medido. Guardarlo por mercado y por momento permite que un modelo
        # futuro aprenda DONDE es caro operar, no solo donde hay señal.
        slippage = m.yes_price * (self.s.extra_slippage_bps / 10_000.0) * 2
        fees = (self.s.fee_bps / 10_000.0) * min(m.yes_price, 1 - m.yes_price) * 2
        feats.append(self._feature(
            m.id, "execution_cost", (book.best_ask - book.best_bid) + slippage + fees,
            spread_abs=round(book.best_ask - book.best_bid, 4),
        ))
        return feats

    def _volatility_feature(self, m: Market, prices: list[float]) -> Feature:
        """Volatilidad reciente vs base, normalizada a 0..1.

        Media de |Δprecio| entre puntos (aritmética pura: sin numpy, que en el
        Bobcat del CT202 ni siquiera importa en su version 2.x).
        """
        recent = prices[-6:]
        baseline = prices[:-6] or prices
        vol_recent = _mean_abs_move(recent)
        vol_base = max(_mean_abs_move(baseline), 0.001)
        ratio = vol_recent / vol_base
        return self._feature(
            m.id, "volatility_score", _clamp01(ratio / 4.0),
            vol_recent=round(vol_recent, 5), vol_baseline=round(vol_base, 5),
            ratio=round(ratio, 3),
        )
