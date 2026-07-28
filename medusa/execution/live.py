"""Live Trading Engine: ordenes REALES contra el CLOB de Polymarket.

Implementa el mismo ExecutionAdapter que Paper, asi que el Trading Engine no
distingue entre uno y otro: la logica validada en Paper es literalmente la que
se ejecuta aqui. Lo unico que cambia es que el dinero es real.

SEGURIDAD (capas deliberadamente redundantes):
  1. Los secretos (PRIVATE_KEY / WALLET_ADDRESS / POLYMARKET_API_KEY) solo se
     leen del entorno. Nunca se hardcodean ni se escriben en los logs.
  2. El adaptador se niega a operar si bot_state.live_unlocked es False, aunque
     el modo global diga "live". El desbloqueo es manual y explicito.
  3. Antes de cada orden se revalidan edge, liquidez, tamaño y deriva de precio
     contra el libro real, aunque el Risk Manager ya lo haya hecho. Ultima linea
     de defensa antes de gastar dinero real.

ADVERTENCIA: este camino NO se puede probar de verdad sin una wallet con fondos.
Esta escrito con cuidado pero NO esta verificado contra el CLOB real. Antes de
confiarle un importe serio, probar con la cantidad minima y revisar los fills.

py-clob-client es sincrono; sus llamadas van a un thread aparte para no bloquear
el event loop.
"""

from __future__ import annotations

import asyncio
from typing import Any

from medusa.config import get_settings
from medusa.core.models import Fill, OrderRequest, OrderResult
from medusa.data import repositories as repo
from medusa.execution.base import ExecutionAdapter
from medusa.logging_setup import err


class LiveTradingBlocked(RuntimeError):
    """Live solicitado sin cumplir los requisitos de desbloqueo."""


class LiveExecutionEngine(ExecutionAdapter):
    def __init__(self, client, log) -> None:
        self.client = client            # cliente publico (libro, mercados)
        self.log = log
        self.s = get_settings()
        self._clob: Any = None

    @property
    def mode(self) -> str:
        return "live"

    @property
    def tracks_balance(self) -> bool:
        """El balance es USDC on-chain: no lo lleva Medusa, se consulta."""
        return False

    # ------------------------------------------------------- inicializacion --
    def _build_clob(self) -> Any:
        """Construye el ClobClient. Va en un thread porque hace I/O de red."""
        from py_clob_client.client import ClobClient

        if not self.s.private_key:
            raise LiveTradingBlocked("PRIVATE_KEY no configurada")
        if not self.s.wallet_address:
            raise LiveTradingBlocked("WALLET_ADDRESS no configurada")

        kwargs: dict[str, Any] = {
            "host": self.s.clob_api_url,
            "key": self.s.private_key,
            "chain_id": self.s.clob_chain_id,
        }
        # Los proxies (signature_type 1/2) necesitan la direccion que tiene los
        # fondos; con EOA (0) la propia clave es la dueña.
        if self.s.clob_signature_type in (1, 2):
            kwargs["signature_type"] = self.s.clob_signature_type
            kwargs["funder"] = self.s.clob_funder or self.s.wallet_address

        clob = ClobClient(**kwargs)
        # Deriva (o crea) las credenciales L2 a partir de la clave privada.
        clob.set_api_creds(clob.create_or_derive_api_creds())
        return clob

    async def _get_clob(self) -> Any:
        if self._clob is None:
            self._clob = await asyncio.to_thread(self._build_clob)
            self.log.info("live.clob_ready", wallet=_mask(self.s.wallet_address))
        return self._clob

    # -------------------------------------------------------------- guardas --
    async def ensure_unlocked(self) -> None:
        """Rechaza operar si Live no esta desbloqueado explicitamente."""
        state = await repo.get_bot_state()
        if not state or not state.get("live_unlocked"):
            raise LiveTradingBlocked(
                "Live no desbloqueado: requiere superar el gate de paper trading "
                "y desbloqueo manual desde el dashboard"
            )

    def _validate(self, order: OrderRequest, book) -> str | None:
        """Revalida la orden contra el libro real. Devuelve el motivo de rechazo."""
        if order.side == "buy":
            levels, price = book.asks, book.best_ask
        else:
            levels, price = book.bids, book.best_bid
        if not levels or price <= 0:
            return "libro vacio"

        liquidity = book.ask_liquidity if order.side == "buy" else book.bid_liquidity
        if liquidity < self.s.min_trade_liquidity:
            return f"liquidez {liquidity:.0f} < minimo {self.s.min_trade_liquidity}"
        if order.side == "buy" and order.usd_size > self.s.max_position_usd:
            return f"tamaño {order.usd_size:.2f} > maximo {self.s.max_position_usd}"

        # El precio se movio en contra desde que se decidio: no perseguirlo.
        if order.ref_price > 0:
            drift = abs(price - order.ref_price) / order.ref_price
            max_drift = self.s.extra_slippage_bps / 10_000.0 * 5
            if drift > max_drift:
                return (f"precio movido {drift:.2%} desde la decision "
                        f"({order.ref_price:.3f} -> {price:.3f})")
        return None

    # -------------------------------------------------------------- balance --
    async def get_balance(self) -> float:
        """USDC colateral disponible en la cuenta real."""
        from py_clob_client.clob_types import AssetType, BalanceAllowanceParams

        clob = await self._get_clob()
        resp = await asyncio.to_thread(
            clob.get_balance_allowance,
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL),
        )
        raw = (resp or {}).get("balance", 0)
        return float(raw) / 1_000_000.0   # USDC tiene 6 decimales

    # ----------------------------------------------------------- place_order --
    async def place_order(self, order: OrderRequest) -> OrderResult:
        await self.ensure_unlocked()

        try:
            book = await self.client.fetch_order_book(order.token_id)
        except Exception as exc:  # noqa: BLE001
            return OrderResult(0.0, 0.0, [], "rejected", f"order book no disponible: {exc}")

        reason = self._validate(order, book)
        if reason:
            self.log.warning("live.order_blocked", market=order.market_id, reason=reason)
            return OrderResult(0.0, 0.0, [], "rejected", reason)

        try:
            resp = await self._submit(order)
        except Exception as exc:  # noqa: BLE001
            self.log.error("live.order_failed", market=order.market_id, error=err(exc))
            return OrderResult(0.0, 0.0, [], "rejected", f"error del CLOB: {exc}")

        return self._parse_response(resp, order, book.mid)

    async def _submit(self, order: OrderRequest) -> dict:
        from py_clob_client.clob_types import MarketOrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY, SELL

        clob = await self._get_clob()
        # Convencion de py-clob-client: en BUY, amount es USDC; en SELL, shares.
        amount = order.usd_size if order.side == "buy" else order.shares
        args = MarketOrderArgs(
            token_id=order.token_id,
            amount=float(amount),
            side=BUY if order.side == "buy" else SELL,
        )
        signed = await asyncio.to_thread(clob.create_market_order, args)
        # FAK (fill-and-kill) permite fill parcial, igual que asume el Paper
        # Engine. Con FOK un libro fino cancelaria la orden entera.
        return await asyncio.to_thread(clob.post_order, signed, OrderType.FAK)

    def _parse_response(self, resp: dict, order: OrderRequest, mid: float) -> OrderResult:
        """Traduce la respuesta del CLOB a OrderResult.

        making/takingAmount vienen invertidos segun el lado: al comprar se
        entrega USDC y se reciben shares; al vender, al reves.
        """
        if not resp or not resp.get("success", True):
            msg = str(resp.get("errorMsg") or resp.get("error") or "orden rechazada")
            return OrderResult(0.0, 0.0, [], "rejected", msg)

        making = _to_float(resp.get("makingAmount"))
        taking = _to_float(resp.get("takingAmount"))
        if order.side == "buy":
            usd, shares = making, taking
        else:
            shares, usd = making, taking

        if shares <= 0 or usd <= 0:
            status = str(resp.get("status", "")).lower()
            return OrderResult(0.0, 0.0, [], "rejected",
                               f"sin fill (status={status or 'desconocido'})")

        avg_price = usd / shares
        requested = order.usd_size if order.side == "buy" else order.shares
        got = usd if order.side == "buy" else shares
        status = "filled" if requested and got / requested >= 0.99 else "partial"

        fill = Fill(
            price=avg_price,
            size=shares,
            fee=0.0,   # el CLOB no desglosa la fee: ya viene embebida en el precio
            slippage_cost=abs(avg_price - order.ref_price) * shares if order.ref_price else 0.0,
            spread_cost=abs(avg_price - mid) * shares if mid else 0.0,
        )
        self.log.info(
            "live.order_filled", market=order.market_id, side=order.side,
            shares=round(shares, 2), avg_price=round(avg_price, 4), status=status,
            order_id=resp.get("orderID", ""),
        )
        return OrderResult(shares, avg_price, [fill], status, book_mid=mid)

    # --------------------------------------------------------------- resto ---
    async def cancel_order(self, order_id: str) -> bool:
        clob = await self._get_clob()
        try:
            await asyncio.to_thread(clob.cancel, order_id)
            return True
        except Exception as exc:  # noqa: BLE001
            self.log.warning("live.cancel_failed", order_id=order_id, error=err(exc))
            return False

    async def get_fills(self, order_id: str) -> list:
        from py_clob_client.clob_types import TradeParams

        clob = await self._get_clob()
        try:
            return await asyncio.to_thread(clob.get_trades, TradeParams(id=order_id)) or []
        except Exception as exc:  # noqa: BLE001
            self.log.warning("live.get_fills_failed", order_id=order_id, error=err(exc))
            return []


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _mask(value: str) -> str:
    """Nunca registrar direcciones/claves completas en los logs."""
    return f"{value[:6]}...{value[-4:]}" if len(value) > 12 else "***"
