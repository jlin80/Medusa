"""Paper Trading Engine (F3).

Simula la ejecucion contra el order book REAL de Polymarket. Deliberadamente NO
usa precios ideales (ni mid, ni last): recorre los niveles del libro pagando el
spread, aplica slippage y fees, y limita el fill a la liquidez realmente
disponible, lo que produce fills parciales cuando el libro es fino.

Sesgo de diseño: ante la duda, pesimista. Un paper trading optimista es peor que
no tener paper trading, porque valida una estrategia que perdera dinero en Live.
"""

from __future__ import annotations

from medusa.config import get_settings
from medusa.core.models import Fill, OrderBook, OrderRequest, OrderResult
from medusa.data import repositories as repo
from medusa.execution.base import ExecutionAdapter

# Rango de precios valido en Polymarket (tick 0.001).
_MIN_PRICE = 0.001
_MAX_PRICE = 0.999


class PaperExecutionEngine(ExecutionAdapter):
    """ExecutionAdapter que simula fills sobre el libro real."""

    def __init__(self, client, log) -> None:
        self.client = client
        self.log = log
        self.s = get_settings()

    @property
    def mode(self) -> str:
        return "paper"

    @property
    def tracks_balance(self) -> bool:
        return True

    # -------------------------------------------------------------- balance --
    async def get_balance(self) -> float:
        state = await repo.get_bot_state()
        if state is None:
            return self.s.paper_starting_balance
        return float(state["balance"])

    # ----------------------------------------------------------------- fees --
    def _fee_per_share(self, price: float) -> float:
        """Forma de la fee de Polymarket: rate * min(p, 1-p) por share."""
        rate = self.s.fee_bps / 10_000.0
        return rate * min(price, 1.0 - price)

    # ------------------------------------------------------------ simulacion --
    def _simulate(self, book: OrderBook, req: OrderRequest) -> OrderResult:
        """Recorre el libro nivel a nivel y construye los fills resultantes."""
        buying = req.side == "buy"
        levels = book.asks if buying else book.bids
        mid = book.mid
        slip = self.s.extra_slippage_bps / 10_000.0
        depth_frac = self.s.book_depth_fraction

        if not levels:
            return OrderResult(0.0, 0.0, [], "rejected", "libro vacio")

        fills: list[Fill] = []
        remaining_usd = req.usd_size
        remaining_shares = req.shares

        for price, size in levels:
            if price <= 0 or size <= 0:
                continue
            available = size * depth_frac
            if available <= 0:
                continue

            # El slippage siempre juega en contra: se compra mas caro y se vende
            # mas barato que el precio mostrado en el libro.
            eff = price * (1 + slip) if buying else price * (1 - slip)
            eff = min(max(eff, _MIN_PRICE), _MAX_PRICE)
            fee_share = self._fee_per_share(eff)

            if buying:
                unit_cost = eff + fee_share       # coste real por share
                if unit_cost <= 0 or remaining_usd <= 0:
                    break
                take = min(available, remaining_usd / unit_cost)
            else:
                if remaining_shares <= 0:
                    break
                take = min(available, remaining_shares)

            if take <= 1e-9:
                break

            fills.append(Fill(
                price=eff,
                size=take,
                fee=fee_share * take,
                slippage_cost=abs(eff - price) * take,
                spread_cost=abs(price - mid) * take,
            ))

            if buying:
                remaining_usd -= take * (eff + fee_share)
            else:
                remaining_shares -= take

        filled = sum(f.size for f in fills)
        if filled <= 0:
            return OrderResult(0.0, 0.0, [], "rejected", "sin liquidez ejecutable")

        avg_price = sum(f.price * f.size for f in fills) / filled

        # Polymarket rechaza ordenes por debajo de orderMinSize (5 shares).
        if filled < self.s.min_order_shares:
            return OrderResult(
                0.0, 0.0, [], "rejected",
                f"fill {filled:.2f} < minimo {self.s.min_order_shares} shares",
            )

        if buying:
            done = (req.usd_size - remaining_usd) / req.usd_size if req.usd_size else 1.0
        else:
            done = filled / req.shares if req.shares else 1.0
        status = "filled" if done >= 0.99 else "partial"

        return OrderResult(filled, avg_price, fills, status, book_mid=mid)

    # ---------------------------------------------------------- place_order --
    async def place_order(self, order: OrderRequest) -> OrderResult:
        """Ejecuta la orden contra un snapshot fresco del libro real."""
        try:
            book = await self.client.fetch_order_book(order.token_id)
        except Exception as exc:  # noqa: BLE001 - un fallo de API no tumba el engine
            self.log.warning("paper.book_fail", token=order.token_id, error=str(exc))
            return OrderResult(0.0, 0.0, [], "rejected", f"order book no disponible: {exc}")

        result = self._simulate(book, order)
        if result.status == "rejected":
            self.log.info("paper.order_rejected", market=order.market_id, reason=result.reason)
            return result

        # El balance NO se mueve aqui. Lo mueve el Trading Engine en la misma
        # transaccion que registra la posicion, para que un reinicio no pueda
        # dejar el dinero cobrado y la posicion sin guardar. Aqui solo se
        # comprueba que hay fondos.
        if order.side == "buy" and result.cash_out > await self.get_balance():
            return OrderResult(0.0, 0.0, [], "rejected", "balance insuficiente")

        self.log.info(
            "paper.order_filled",
            market=order.market_id, side=order.side, outcome=order.outcome,
            shares=round(result.filled_size, 2), avg_price=round(result.avg_price, 4),
            status=result.status, fee=round(result.total_fee, 4),
            slippage=round(result.total_slippage, 4),
        )
        return result

    async def cancel_order(self, order_id: str) -> bool:
        """En paper las ordenes son de mercado e inmediatas: nada que cancelar."""
        return True

    async def get_fills(self, order_id: str) -> list:
        return []
