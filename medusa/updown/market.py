"""Descubrimiento de la ventana 'Up or Down' viva y lectura de su resolucion.

Estas ventanas NO aparecen en el escaneo normal (el scanner filtra por horas a
resolucion y rankea por volumen). Pero su slug es DETERMINISTA:

    {asset}-updown-5m-{epoch_de_inicio}

y el epoch de inicio esta alineado a 300s. Asi que la ventana viva no hay que
buscarla: se calcula  epoch = (now // 300) * 300  y se pide el evento por slug.
Una sola llamada a Gamma, sin paginar ni buscar.

  outcomes      = ["Up", "Down"]          (siempre en ese orden)
  clobTokenIds  = [token_Up, token_Down]
  resuelve "Up" si  precio_final >= precio_inicial  (fuente: stream Chainlink
  BNB/USD; ver descripcion del mercado). Al resolver, outcomePrices pasa a
  ["1","0"] (gano Up) o ["0","1"] (gano Down).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx

from medusa.config import get_settings
from medusa.core.models import OrderBook

_WINDOW_SECS = 300


def _json_list(value: Any) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except (json.JSONDecodeError, ValueError):
            return []
    return []


def window_start_for(now_epoch: int) -> int:
    """Epoch de inicio de la ventana que contiene `now_epoch` (alineado a 300s)."""
    return (now_epoch // _WINDOW_SECS) * _WINDOW_SECS


def slug_for(asset: str, start_epoch: int) -> str:
    return f"{asset}-updown-5m-{start_epoch}"


@dataclass
class UpDownWindow:
    asset: str
    slug: str
    condition_id: str
    start_epoch: int
    end_epoch: int
    up_token: str
    down_token: str
    question: str = ""
    closed: bool = False
    accepting_orders: bool = True
    # settle_up: 1.0 gano Up, 0.0 gano Down, None todavia sin resolver limpio.
    settle_up: float | None = None

    def token_for(self, favored: str) -> str:
        return self.up_token if favored == "Up" else self.down_token

    def secs_left(self, now_epoch: float) -> float:
        return self.end_epoch - now_epoch


class UpDownMarketFeed:
    """Habla con Gamma para localizar la ventana y leer su resolucion, y con el
    CLOB (via el PolymarketClient compartido) para el libro real."""

    def __init__(self, client, log, *, timeout: float | None = None) -> None:
        s = get_settings()
        self.client = client   # PolymarketClient compartido (para fetch_order_book)
        self.log = log
        self._gamma = s.gamma_api_url.rstrip("/")
        self._http = httpx.AsyncClient(timeout=timeout or s.http_timeout)

    async def close(self) -> None:
        await self._http.aclose()

    async def _event_by_slug(self, slug: str) -> dict | None:
        try:
            resp = await self._http.get(f"{self._gamma}/events", params={"slug": slug})
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:  # noqa: BLE001 - una ventana ilocalizable no rompe nada
            self.log.warning("updown.event_fail", slug=slug, error=str(exc))
            return None
        rows = data if isinstance(data, list) else data.get("data", [])
        return rows[0] if rows else None

    def _parse(self, asset: str, start_epoch: int, ev: dict) -> UpDownWindow | None:
        markets = ev.get("markets") or []
        if not markets:
            return None
        m = markets[0]
        tokens = _json_list(m.get("clobTokenIds"))
        if len(tokens) < 2:
            return None
        prices = _json_list(m.get("outcomePrices"))
        settle_up: float | None = None
        closed = bool(m.get("closed"))
        if closed and len(prices) >= 1:
            try:
                up = float(prices[0])
            except (TypeError, ValueError):
                up = -1.0
            if up >= 0.99:
                settle_up = 1.0
            elif up <= 0.01:
                settle_up = 0.0
        return UpDownWindow(
            asset=asset,
            slug=slug_for(asset, start_epoch),
            condition_id=str(m.get("conditionId") or m.get("id") or ""),
            start_epoch=start_epoch,
            end_epoch=start_epoch + _WINDOW_SECS,
            up_token=str(tokens[0]),
            down_token=str(tokens[1]),
            question=m.get("question", "") or ev.get("title", ""),
            closed=closed,
            accepting_orders=bool(m.get("acceptingOrders", True)),
            settle_up=settle_up,
        )

    async def current_window(self, asset: str, now_epoch: int) -> UpDownWindow | None:
        """La ventana viva ahora mismo para `asset` (o None si no existe/no se
        pudo leer)."""
        start = window_start_for(now_epoch)
        slug = slug_for(asset, start)
        ev = await self._event_by_slug(slug)
        if ev is None:
            return None
        return self._parse(asset, start, ev)

    async def resolution(self, window: UpDownWindow) -> float | None:
        """Relee el evento y devuelve settle_up (1.0/0.0) si ya resolvio limpio,
        None si sigue pendiente."""
        ev = await self._event_by_slug(window.slug)
        if ev is None:
            return None
        parsed = self._parse(window.asset, window.start_epoch, ev)
        return parsed.settle_up if parsed else None

    async def order_book(self, token_id: str) -> OrderBook | None:
        try:
            return await self.client.fetch_order_book(token_id)
        except Exception as exc:  # noqa: BLE001
            self.log.warning("updown.book_fail", token=token_id, error=str(exc))
            return None
