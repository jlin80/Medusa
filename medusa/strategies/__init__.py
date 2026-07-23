"""Estrategias de Medusa.

Cada estrategia es un modulo independiente que hereda de Strategy y produce
StrategySignal (score, confianza, explicacion, condiciones de validez). El
StrategyManager las orquesta; el registro de señales en shadow y su resolucion
contra el resultado real del mercado (medusa.data.repositories +
medusa.engine) generan el historial de rendimiento por estrategia/categoria
que alimenta la asignacion de capital (medusa.allocation).

Para AÑADIR una estrategia: crear el modulo, heredar de Strategy y sumarla a
build_default_strategies(). Nada mas cambia.

Estrategias implementadas:
  mean_reversion        - baseline original (neutralizado por calibracion, k=0)
  momentum              - continuacion del movimiento 24h (shadow)
  order_book_imbalance  - presion de profundidad en el libro (shadow)
  liquidity_breakout    - movimiento 1h con lado del libro fino (shadow)
  stat_arb              - YES+NO fuera de $1 (alerta; ganancia sin riesgo)
  volatility_expansion  - ruptura de regimen de volatilidad (shadow)

Pendientes de fuente de datos EXTERNA (donde el veredicto de calibracion dice
que esta el edge real, si existe):
  wallet_consensus      - requiere seguir wallets ganadoras (Data API /holders
                          + historial por wallet; caro en llamadas, hay que
                          diseñar el muestreo antes de implementarlo)
  cross_market_correlation - requiere un mapeo de mercados relacionados
                          (mismo evento en distintas preguntas) que hoy no existe
  event_driven          - requiere feed de noticias/datos deportivos-electorales
                          en tiempo real; es la via señalada por los 3 estudios
"""

from medusa.strategies.base import Strategy, StrategySignal
from medusa.strategies.liquidity_breakout import LiquidityBreakoutStrategy
from medusa.strategies.manager import StrategyManager
from medusa.strategies.mean_reversion import MeanReversionStrategy
from medusa.strategies.momentum import MomentumStrategy
from medusa.strategies.order_book_imbalance import OrderBookImbalanceStrategy
from medusa.strategies.stat_arb import StatArbStrategy
from medusa.strategies.volatility_expansion import VolatilityExpansionStrategy

__all__ = [
    "Strategy",
    "StrategySignal",
    "StrategyManager",
    "build_default_strategies",
]


def build_default_strategies(log) -> list[Strategy]:
    """Instancia el set de estrategias incorporadas."""
    return [
        MeanReversionStrategy(log),
        MomentumStrategy(log),
        OrderBookImbalanceStrategy(log),
        LiquidityBreakoutStrategy(log),
        StatArbStrategy(log),
        VolatilityExpansionStrategy(log),
    ]
