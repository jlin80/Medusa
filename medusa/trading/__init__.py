"""Trading Engine.

Convierte las instrucciones aprobadas por el Risk Manager en ordenes y las envia
al ExecutionAdapter activo (paper o live). Agnostico al modo.
"""

from medusa.trading.engine import TradingEngine

__all__ = ["TradingEngine"]
