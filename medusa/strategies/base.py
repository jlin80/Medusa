"""Contrato base de las estrategias de Medusa.

Cada estrategia es independiente y produce StrategySignal con:

  score            - fuerza de la señal (0..100)
  confidence       - confianza (0..1)
  explanation      - POR QUE se dispara (auditable en el dashboard)
  valid_conditions - bajo que condiciones la señal es valida (y que evidencia
                     tiene o no tiene detras)

Toda señal se REGISTRA siempre (shadow), se opere o no: la resolucion real del
mercado la convierte en un dato de rendimiento por estrategia/categoria. Ese
historial, no la opinion de nadie, es lo que decide que estrategias llegan a
operar y con cuanto capital (medusa.allocation).

Para añadir una estrategia nueva: heredar de Strategy, implementar evaluate()
y añadirla a build_default_strategies() en medusa/strategies/__init__.py. Nada
mas cambia: el manager, el registro de señales, el tracking de rendimiento y
la asignacion de capital la recogen solos.
"""

from __future__ import annotations

import datetime as dt
from abc import ABC, abstractmethod
from dataclasses import dataclass

from medusa.config import get_settings
from medusa.core.models import Market, MarketContext


def clamp_prob(p: float) -> float:
    """Ninguna estrategia puede afirmar certeza absoluta."""
    return min(0.99, max(0.01, p))


@dataclass
class StrategySignal:
    strategy: str
    market_id: str
    question: str
    category: str
    outcome: str                  # YES | NO
    token_id: str
    # DOBLE COTA DE PnL (metodologia portada del bot Python/Hermes, 2026-07-16).
    # Medir con un solo supuesto de ejecucion es como se fabrica la ilusion de
    # rentabilidad: Hermes v1.1 solo trackea la cota maker y su propia doc la
    # etiqueta "optimista (techo)". Aqui se guardan LAS DOS y manda la pesimista.
    #   entry_price       = ASK real  -> crucé el spread (TAKER, realista)
    #   entry_price_maker = BID real  -> posté límite y me llenaron (MAKER, techo)
    # La diferencia entre ambas ES el spread: el peaje que decide si hay negocio.
    entry_price: float            # ask del outcome elegido (cota TAKER)
    signal_prob: float            # prob estimada por la estrategia (outcome elegido)
    market_prob: float            # prob implicita de mercado (outcome elegido)
    edge: float                   # signal_prob - market_prob
    confidence: float             # 0..1
    score: float                  # 0..100 fuerza de la señal
    explanation: str
    valid_conditions: str
    # bid del outcome elegido (cota MAKER). 0.0 = no habia libro para medirlo.
    entry_price_maker: float = 0.0
    spread: float = 0.0
    liquidity: float = 0.0
    end_date: dt.datetime | None = None
    # False = señal solo informativa/alerta (p.ej. stat_arb necesita ejecutar
    # dos patas a la vez, cosa que el camino de ordenes actual no soporta).
    tradeable: bool = True


class Strategy(ABC):
    """Base de todas las estrategias."""

    name: str = "base"
    # None = aplica a todas las categorias. El mecanismo de restriccion existe
    # (arquitectura), pero por defecto NINGUNA estrategia se restringe a mano:
    # el sistema no debe asumir que tipo de mercado le va mejor a cada una, lo
    # descubre el historial de señales resueltas por (estrategia, categoria) y
    # lo aplica el Capital Allocation Manager.
    categories: tuple[str, ...] | None = None
    needs_book_no: bool = False     # ¿necesita el libro del token NO?
    needs_history: bool = False     # ¿necesita historico de precios?
    can_trade: bool = True          # False = solo registro/alerta, nunca opera

    def __init__(self, log) -> None:
        self.log = log
        self.s = get_settings()

    def applies_to(self, m: Market) -> bool:
        return self.categories is None or m.medusa_category in self.categories

    @abstractmethod
    def evaluate(self, m: Market, ctx: MarketContext) -> StrategySignal | None:
        """Devuelve una señal o None si no hay nada que decir en este mercado."""

    # ------------------------------------------------------------- helpers ---
    @staticmethod
    def _entry_bounds(book, fallback: float) -> tuple[float, float]:
        """(precio TAKER = ask, precio MAKER = bid) de un libro.

        Sin libro no hay cotas que medir: se devuelve el mismo precio en ambas,
        lo que hace el gap 0 y deja la comparacion maker/taker sin efecto. Es
        preferible a inventarse un spread.
        """
        if book is not None and book.best_ask > 0 and book.best_bid > 0:
            return book.best_ask, book.best_bid
        return fallback, fallback

    @staticmethod
    def _no_side_bounds(ctx: MarketContext, m: Market) -> tuple[float, float]:
        """Cotas del lado NO.

        Si hay libro NO, se usa directo. Si no lo hay (solo stat_arb lo pide),
        se DERIVA del libro YES por la identidad del mercado binario:
            NO_ask = 1 - YES_bid   y   NO_bid = 1 - YES_ask
        Eso preserva el spread real. La alternativa -- caer al mid m.no_price --
        haria maker == taker y borraria justo el peaje que queremos medir,
        fabricando la misma ilusion optimista que este trabajo existe para
        destapar.
        """
        if ctx.book_no is not None and ctx.book_no.best_ask > 0 and ctx.book_no.best_bid > 0:
            return ctx.book_no.best_ask, ctx.book_no.best_bid
        yes = ctx.book_yes
        if yes is not None and yes.best_ask > 0 and yes.best_bid > 0:
            return 1.0 - yes.best_bid, 1.0 - yes.best_ask
        return m.no_price, m.no_price

    def _market_confidence(self, m: Market) -> float:
        """Confianza estructural del mercado (0.5..1): liquidez y volumen.

        Es un multiplicador de calidad de datos, no una opinion direccional:
        el mismo desequilibrio de libro es mas creible en un mercado con
        $50k de liquidez que en uno de $600.
        """
        liq = min(1.0, m.liquidity / 5000.0)
        vol = min(1.0, m.volume_24h / 20000.0)
        return round(0.5 + 0.25 * liq + 0.25 * vol, 3)

    def _directional_signal(
        self,
        m: Market,
        ctx: MarketContext,
        prob_yes: float,
        confidence: float,
        score: float,
        explanation: str,
        valid_conditions: str,
    ) -> StrategySignal:
        """Construye la señal eligiendo el lado (YES/NO) con edge positivo.

        El precio de entrada usa el ask REAL del libro cuando esta disponible:
        una señal en shadow debe medirse contra lo que de verdad habria costado
        entrar, no contra el mid, o el rendimiento medido sale inflado (el
        mismo sesgo optimista que el Paper Engine evita a proposito).
        """
        prob_yes = clamp_prob(prob_yes)
        market_prob_yes = m.yes_price
        edge_yes = prob_yes - market_prob_yes

        if edge_yes >= 0:
            outcome, token = "YES", m.yes_token_id
            signal_prob, market_prob, edge = prob_yes, market_prob_yes, edge_yes
            entry, entry_maker = self._entry_bounds(ctx.book_yes, m.yes_price)
        else:
            outcome, token = "NO", m.no_token_id
            signal_prob, market_prob = 1 - prob_yes, 1 - market_prob_yes
            edge = -edge_yes
            entry, entry_maker = self._no_side_bounds(ctx, m)

        return StrategySignal(
            strategy=self.name,
            market_id=m.id,
            question=m.question,
            category=m.medusa_category,
            outcome=outcome,
            token_id=token,
            entry_price=round(entry, 4),
            entry_price_maker=round(entry_maker, 4),
            signal_prob=round(signal_prob, 4),
            market_prob=round(market_prob, 4),
            edge=round(edge, 4),
            confidence=round(confidence, 3),
            score=round(score, 1),
            explanation=explanation,
            valid_conditions=valid_conditions,
            spread=m.spread,
            liquidity=m.liquidity,
            end_date=m.end_date,
            tradeable=self.can_trade,
        )
