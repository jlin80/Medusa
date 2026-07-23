"""Interfaz comun de ejecucion (ExecutionAdapter).

Contrato que cumplen tanto el motor de Paper como el de Live. El resto del
sistema (Trading Engine) solo habla con esta interfaz, de modo que la logica
validada en Paper es exactamente la que corre en Live.
"""

from abc import ABC, abstractmethod

from medusa.core.models import OrderRequest, OrderResult


class ExecutionAdapter(ABC):
    """Contrato de ejecucion, independiente del modo (paper/live)."""

    @property
    @abstractmethod
    def mode(self) -> str:
        """'paper' o 'live'."""

    @property
    def tracks_balance(self) -> bool:
        """True si el balance lo lleva Medusa (paper), False si vive fuera (live).

        En paper el balance es una fila de Postgres que hay que mover con cada
        operacion. En live el balance es USDC on-chain: no se toca, se consulta.
        """
        return False

    @abstractmethod
    async def get_balance(self) -> float:
        """Balance disponible (USDC) en la cuenta simulada o real."""

    @abstractmethod
    async def place_order(self, order: OrderRequest) -> OrderResult:
        """Coloca una orden y devuelve el resultado (fills, estado)."""

    @abstractmethod
    async def cancel_order(self, order_id: str) -> bool:
        """Cancela una orden abierta."""

    @abstractmethod
    async def get_fills(self, order_id: str) -> list:
        """Devuelve los fills (parciales/completos) de una orden."""
