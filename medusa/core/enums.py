"""Enumeraciones de dominio compartidas."""

from enum import Enum


class Mode(str, Enum):
    """Modo global de operacion."""

    PAPER = "paper"
    LIVE = "live"


class LogType(str, Enum):
    """Tipos de log para la terminal del dashboard."""

    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    TRADE = "TRADE"
    RISK = "RISK"
    SYSTEM = "SYSTEM"


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class Outcome(str, Enum):
    """Lado del mercado binario de Polymarket."""

    YES = "YES"
    NO = "NO"


class OrderStatus(str, Enum):
    NEW = "new"
    PARTIAL = "partial"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


class Action(str, Enum):
    """Accion recomendada por una estrategia sobre una oportunidad."""

    BUY_YES = "buy_yes"
    BUY_NO = "buy_no"
    HOLD = "hold"
    SKIP = "skip"
