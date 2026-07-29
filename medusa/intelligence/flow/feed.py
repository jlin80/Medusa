"""Ingesta de la cinta publica de trades (Data API + Gamma de Polymarket).

Solo lectura de datos PUBLICOS. Este modulo no conoce ninguna clave privada, no
firma nada y no habla con el CLOB: las tres cosas que harian falta para operar
no estan aqui.

Robustez: cada metodo devuelve [] o {} ante cualquier fallo y registra un
warning. NUNCA lanza. Que Polymarket no responda tiene que dejar la pasada sin
datos nuevos, jamas tumbar un loop.

Cliente httpx propio (como `medusa/intelligence/wallet/feed.py` y
`medusa/updown/feed.py`): asi este subsistema no modifica el cliente de trading
ni compite por su pool de conexiones.
"""

from __future__ import annotations

import httpx

from medusa.config import get_settings
from medusa.logging_setup import err


class FlowFeed:
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
            self.log.warning("flow.feed_failed", what=what, error=err(exc))
            return None

    async def fetch_trades(self, condition_id: str, limit: int = 500) -> list[dict]:
        """Cinta de trades publicos de un mercado, del mas reciente al mas viejo.

        Es LA fuente del motor: sin quien entro y cuando, no hay propagacion que
        medir. Se pide ordenada por timestamp descendente porque asi la pagina
        que la API devuelve por defecto es la ventana reciente, que es la que
        interesa; `ingest.normalize_trades` la reordena ascendente.
        """
        data = await self._get(
            f"{self._data}/trades",
            {"market": condition_id, "limit": str(limit),
             "takerOnly": "false", "sortBy": "TIMESTAMP", "sortDirection": "DESC"},
            "trades",
        )
        if isinstance(data, dict):
            data = data.get("data") or data.get("trades") or []
        return [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []

    async def fetch_market_meta(self, condition_ids: list[str]) -> dict[str, dict]:
        """Contexto de los mercados. De aqui sale la RESOLUCION, que es la unica
        verdad contra la que se puede puntuar una cascada."""
        out: dict[str, dict] = {}
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
