"""Ingesta de actividad publica de wallets (Data API + Gamma de Polymarket).

Solo lectura de datos PUBLICOS. Este modulo no conoce ninguna clave privada, no
firma nada y no habla con el CLOB: las tres cosas que harian falta para operar
no estan aqui.

Robustez: cada metodo devuelve [] o {} ante cualquier fallo y registra un
warning. NUNCA lanza. Wallet Intelligence es una capa de features; que Polymarket
no responda tiene que dejar el perfil sin actualizar, jamas tumbar un loop.

Cliente httpx propio, como hace `medusa/updown/feed.py`: asi este subsistema no
modifica `PolymarketClient` ni compite por su pool de conexiones.
"""

from __future__ import annotations

import httpx

from medusa.config import get_settings
from medusa.logging_setup import err


class WalletFeed:
    def __init__(self, log, *, timeout: float | None = None) -> None:
        s = get_settings()
        self.log = log
        self._data = s.data_api_url.rstrip("/")
        self._gamma = s.gamma_api_url.rstrip("/")
        self._client = httpx.AsyncClient(timeout=timeout or s.http_timeout)

    async def close(self) -> None:
        await self._client.aclose()

    async def _get(self, url: str, params: dict, what: str) -> list | dict | None:
        try:
            resp = await self._client.get(url, params=params)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:  # noqa: BLE001 - un feed caido no puede propagar
            self.log.warning("wallet.feed_failed", what=what, error=err(exc))
            return None

    async def fetch_holders(self, condition_id: str, limit: int = 100) -> list[dict]:
        """Wallets con posicion en un mercado. Es la via de DESCUBRIMIENTO:
        Medusa no tiene una lista de wallets, la deduce de los mercados que ya
        esta mirando."""
        data = await self._get(
            f"{self._data}/holders",
            {"market": condition_id, "limit": str(limit)}, "holders",
        )
        if isinstance(data, dict):
            # La Data API devuelve {"holders": [...]} en unos endpoints y la
            # lista pelada en otros. Se aceptan las dos formas.
            data = data.get("holders") or data.get("data") or []
        if not isinstance(data, list):
            return []
        out: list[dict] = []
        for row in data:
            if isinstance(row, dict) and row.get("holders"):
                out.extend(h for h in row["holders"] if isinstance(h, dict))
            elif isinstance(row, dict):
                out.append(row)
        return out

    async def fetch_activity(self, wallet: str, limit: int = 500) -> list[dict]:
        """Actividad de una wallet (compras/ventas con timestamp).

        Es la unica fuente de CUANDO entro y salio, y sin eso las metricas de
        timing no existen.
        """
        data = await self._get(
            f"{self._data}/activity",
            {"user": wallet, "limit": str(limit), "sortBy": "TIMESTAMP",
             "sortDirection": "ASC"}, "activity",
        )
        if isinstance(data, dict):
            data = data.get("data") or data.get("activity") or []
        return [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []

    async def fetch_positions(self, wallet: str, limit: int = 500) -> list[dict]:
        """Posiciones de una wallet: es de donde sale el PnL REALIZADO.

        La actividad sola no basta: emparejar compras y ventas a mano para
        reconstruir el PnL es justo donde se cuelan los errores de contabilidad
        que este proyecto ya ha pagado una vez.
        """
        data = await self._get(
            f"{self._data}/positions",
            {"user": wallet, "limit": str(limit), "sizeThreshold": "0"}, "positions",
        )
        if isinstance(data, dict):
            data = data.get("data") or data.get("positions") or []
        return [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []

    async def fetch_market_meta(self, condition_ids: list[str]) -> dict[str, dict]:
        """Contexto de los mercados: apertura, cierre, liquidez y spread.

        `startDate` NO lo expone la Data API y sin el las metricas de timing no
        se pueden calcular (¿el 20% de que vida?). Por eso se pide a Gamma, que
        si lo trae.
        """
        out: dict[str, dict] = {}
        # Gamma acepta el parametro repetido; se trocea para no construir una
        # URL kilometrica que algunos proxies rechazan.
        for i in range(0, len(condition_ids), 20):
            chunk = [c for c in condition_ids[i:i + 20] if c]
            if not chunk:
                continue
            data = await self._get(
                f"{self._gamma}/markets",
                {"condition_ids": chunk, "limit": str(len(chunk))}, "market_meta",
            )
            if not isinstance(data, list):
                continue
            for row in data:
                if not isinstance(row, dict):
                    continue
                cid = str(row.get("conditionId") or "")
                if cid:
                    out[cid] = row
        return out
