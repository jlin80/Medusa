"""Cliente de Polymarket: Gamma API (mercados) + CLOB (order book, precios).

Solo lectura de datos publicos (no requiere wallet). Se usa tanto en Paper como
en Live para obtener precios/liquidez reales.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import httpx

from medusa.config import get_settings
from medusa.core.models import Market, OrderBook


def _to_list(value: Any) -> list:
    """Gamma a veces devuelve listas como strings JSON."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except (json.JSONDecodeError, ValueError):
            return []
    return []


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_date(value: Any) -> dt.datetime | None:
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        parsed = dt.datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed
    except (ValueError, TypeError):
        return None


class PolymarketClient:
    def __init__(self) -> None:
        s = get_settings()
        self._gamma = s.gamma_api_url.rstrip("/")
        self._clob = s.clob_api_url.rstrip("/")
        self._data = s.data_api_url.rstrip("/")
        self._client = httpx.AsyncClient(timeout=s.http_timeout)

    async def close(self) -> None:
        await self._client.aclose()

    async def fetch_positions(self, wallet: str, min_shares: float = 1.0) -> list[dict]:
        """Posiciones REALES on-chain de una wallet (Data API, dato publico).

        Es la unica forma de descubrir posiciones que Medusa NO sabe que tiene:
        el CLOB solo responde por tokens que le preguntes, asi que consultando
        token a token nunca encontrarias lo que no esta en la BD.

        Devuelve dicts con: asset (token_id), conditionId, size, avgPrice,
        curPrice, redeemable, title, outcome.
        """
        resp = await self._client.get(
            f"{self._data}/positions",
            params={"user": wallet, "sizeThreshold": str(min_shares), "limit": "500"},
        )
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else []

    async def fetch_markets(self, limit: int = 40, offset: int = 0) -> list[Market]:
        """Obtiene mercados activos ordenados por volumen (Gamma API)."""
        params = {
            "closed": "false",
            "active": "true",
            "limit": str(limit),
            "offset": str(offset),
            "order": "volume24hr",
            "ascending": "false",
        }
        resp = await self._client.get(f"{self._gamma}/markets", params=params)
        resp.raise_for_status()
        data = resp.json()
        rows = data if isinstance(data, list) else data.get("data", [])

        markets: list[Market] = []
        for row in rows:
            try:
                markets.append(self._parse_market(row))
            except Exception:  # noqa: BLE001 - un mercado malformado no debe romper el escaneo
                continue
        return [m for m in markets if m.yes_token_id]

    async def fetch_all_markets(self, max_markets: int = 500) -> list[Market]:
        """Universo global: pagina la Gamma API hasta max_markets.

        Gamma pagina de 100 en 100 y corta la paginacion alrededor del offset
        ~2100 (medido en el estudio de calibracion), asi que max_markets muy por
        encima de eso no devuelve mas. Dedup por condition_id: la paginacion de
        Gamma repite mercados entre paginas (bug conocido, ya mordio una vez).
        """
        page_size = 100
        seen: set[str] = set()
        universe: list[Market] = []
        offset = 0
        while len(universe) < max_markets:
            batch = await self.fetch_markets(limit=page_size, offset=offset)
            if not batch:
                break
            for m in batch:
                if m.id and m.id not in seen:
                    seen.add(m.id)
                    universe.append(m)
            if len(batch) < page_size:
                break
            offset += page_size
        return universe[:max_markets]

    def _parse_market(self, row: dict) -> Market:
        outcomes = _to_list(row.get("outcomes"))
        prices = _to_list(row.get("outcomePrices"))
        token_ids = _to_list(row.get("clobTokenIds"))

        yes_price = _to_float(prices[0]) if len(prices) > 0 else 0.0
        no_price = _to_float(prices[1]) if len(prices) > 1 else (1.0 - yes_price)
        yes_token = str(token_ids[0]) if len(token_ids) > 0 else ""
        no_token = str(token_ids[1]) if len(token_ids) > 1 else ""

        return Market(
            id=str(row.get("conditionId") or row.get("id") or ""),
            question=row.get("question", ""),
            slug=row.get("slug", ""),
            category=row.get("category", "") or "",
            end_date=_parse_date(row.get("endDate")),
            yes_token_id=yes_token,
            no_token_id=no_token,
            yes_price=yes_price,
            no_price=no_price,
            volume_24h=_to_float(row.get("volume24hr") or row.get("volume")),
            liquidity=_to_float(row.get("liquidityNum") or row.get("liquidity")),
            active=bool(row.get("active", True)) and not bool(row.get("closed", False)),
            # Gamma trae la variacion ya calculada y el mejor bid/ask del libro:
            # suficiente para pre-puntuar sin pagar una llamada al CLOB por
            # mercado. El libro completo solo se pide para el top del ranking.
            price_change_24h=_to_float(row.get("oneDayPriceChange")),
            price_change_1h=_to_float(row.get("oneHourPriceChange")),
            best_bid=_to_float(row.get("bestBid")),
            best_ask=_to_float(row.get("bestAsk")),
            spread=_to_float(row.get("spread")),
        )

    async def fetch_market_by_condition(self, condition_id: str) -> Market | None:
        """Recupera un mercado concreto por condition_id (para liquidacion).

        Cuando el mercado se resuelve, Gamma marca closed=true y outcomePrices
        pasa a ["1","0"] o ["0","1"], que es lo que usamos para liquidar.

        OJO (bug corregido 2026-07-18): /markets EXCLUYE los mercados cerrados por
        defecto, que son justo los que hacen falta para liquidar en la resolucion.
        Se consulta primero el default (mercados vivos) y, si no aparece, con
        closed=true (resueltos). Sin este segundo intento, una posicion NUNCA se
        liquidaba: fetch devolvia None y el settler creia que el mercado seguia
        abierto para siempre (PnL/ROI congelados).
        """
        for extra in ({}, {"closed": "true"}):
            resp = await self._client.get(
                f"{self._gamma}/markets",
                params={"condition_ids": condition_id, **extra},
            )
            resp.raise_for_status()
            data = resp.json()
            rows = data if isinstance(data, list) else data.get("data", [])
            if rows:
                try:
                    return self._parse_market(rows[0])
                except Exception:  # noqa: BLE001
                    return None
        return None

    async def fetch_order_book(self, token_id: str) -> OrderBook:
        """Obtiene el order book de un token (outcome) via CLOB."""
        resp = await self._client.get(f"{self._clob}/book", params={"token_id": token_id})
        resp.raise_for_status()
        data = resp.json()

        bids = [
            (_to_float(b.get("price")), _to_float(b.get("size")))
            for b in data.get("bids", [])
        ]
        asks = [
            (_to_float(a.get("price")), _to_float(a.get("size")))
            for a in data.get("asks", [])
        ]
        # Normalizar orden: bids desc por precio, asks asc por precio.
        bids.sort(key=lambda x: x[0], reverse=True)
        asks.sort(key=lambda x: x[0])
        return OrderBook(token_id=token_id, bids=bids, asks=asks)

    async def fetch_price_history(
        self, token_id: str, interval: str = "1w", fidelity: int = 60,
    ) -> list[tuple[float, float]]:
        """Historico de precios de un token via CLOB /prices-history.

        Devuelve [(timestamp_unix, precio)] ascendente. fidelity es la
        resolucion en minutos. Lo usan las estrategias que necesitan la FORMA
        del precio reciente (volatilidad), no solo el ultimo valor.
        """
        resp = await self._client.get(
            f"{self._clob}/prices-history",
            params={"market": token_id, "interval": interval, "fidelity": str(fidelity)},
        )
        resp.raise_for_status()
        data = resp.json()
        points = data.get("history", []) if isinstance(data, dict) else []
        out: list[tuple[float, float]] = []
        for p in points:
            ts, price = _to_float(p.get("t")), _to_float(p.get("p"))
            if ts > 0 and 0.0 < price < 1.0:
                out.append((ts, price))
        out.sort(key=lambda x: x[0])
        return out
