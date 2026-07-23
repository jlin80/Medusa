"""Feed de precio externo para el micro-trading de 'Up or Down'.

El edge de todo el subsistema es tener delante un feed MAS RAPIDO y granular que
el libro de Polymarket: el spot de BNB (Binance), que es un proxy casi perfecto
de la direccion del stream Chainlink BNB/USD con el que Polymarket resuelve estas
ventanas. Este modulo aisla ese feed:

  spot()            -> ultimo precio spot (para el movimiento en vivo).
  window_open(ts)   -> precio EXACTO en el borde de la ventana (open del minuto
                       en klines): es el S0 contra el que resuelve el mercado.
  sigma_sec()       -> volatilidad realizada por segundo (para el modelo).

Robustez: si Binance no responde, cada metodo devuelve None / cae al ultimo
valor conocido y registra un warning. NUNCA lanza: un feed caido deja al trader
sin apostar, jamas tumba el engine. Hay dos hosts (api.binance.com y el mirror
data-api.binance.vision) por si el VPS tiene uno bloqueado geograficamente.
"""

from __future__ import annotations

import math
import statistics
import time

import httpx

# Mirror publico de datos de Binance: misma API, distinto host. Si el principal
# esta bloqueado desde el VPS, el mirror suele seguir alcanzable.
_BASES = ("https://api.binance.com", "https://data-api.binance.vision")


class PriceFeed:
    def __init__(self, symbol: str, log, *, timeout: float = 8.0) -> None:
        self.symbol = symbol
        self.log = log
        self._client = httpx.AsyncClient(timeout=timeout)
        self._last_spot: float | None = None
        # Volatilidad por segundo, cacheada: no cambia de forma en 5 min y pedir
        # 60 klines cada tick seria puro desperdicio.
        self._sigma_sec: float | None = None
        self._sigma_at: float = 0.0
        self._sigma_ttl: float = 120.0

    async def close(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, params: dict) -> object | None:
        """GET contra el primer host que responda. None si todos fallan."""
        last_err: Exception | None = None
        for base in _BASES:
            try:
                resp = await self._client.get(base + path, params=params)
                resp.raise_for_status()
                return resp.json()
            except Exception as exc:  # noqa: BLE001 - se prueba el siguiente host
                last_err = exc
                continue
        if self.log is not None:
            self.log.warning("updown.feed_fail", path=path, error=str(last_err))
        return None

    # --------------------------------------------------------------- spot ----
    async def spot(self) -> float | None:
        """Ultimo precio spot. Cae al ultimo conocido si el feed falla."""
        data = await self._get("/api/v3/ticker/price", {"symbol": self.symbol})
        if isinstance(data, dict) and "price" in data:
            try:
                self._last_spot = float(data["price"])
                return self._last_spot
            except (TypeError, ValueError):
                pass
        return self._last_spot

    # ---------------------------------------------------------- open S0 ------
    async def window_open(self, window_start_epoch: int) -> float | None:
        """Precio de apertura EXACTO de la ventana: open del kline de 1m cuyo
        openTime coincide con el borde de la ventana.

        Las ventanas estan alineadas a 300s y los klines a 60s, asi que el borde
        de la ventana es siempre un borde de minuto: el `open` de ese kline es el
        precio en el instante de apertura, que es justo la referencia S0.
        """
        start_ms = window_start_epoch * 1000
        data = await self._get(
            "/api/v3/klines",
            {"symbol": self.symbol, "interval": "1m", "startTime": str(start_ms), "limit": "1"},
        )
        if isinstance(data, list) and data:
            k = data[0]
            # k = [openTime, open, high, low, close, volume, ...]
            if int(k[0]) == start_ms:
                try:
                    return float(k[1])
                except (TypeError, ValueError):
                    return None
        return None

    # -------------------------------------------------------- volatilidad ----
    async def sigma_sec(self) -> float | None:
        """Volatilidad realizada por segundo del log-retorno (cacheada)."""
        now = time.time()
        if self._sigma_sec is not None and now - self._sigma_at < self._sigma_ttl:
            return self._sigma_sec
        data = await self._get(
            "/api/v3/klines", {"symbol": self.symbol, "interval": "1m", "limit": "60"}
        )
        if not (isinstance(data, list) and len(data) >= 3):
            return self._sigma_sec  # se conserva el ultimo valido si lo hay
        try:
            closes = [float(k[4]) for k in data]
        except (TypeError, ValueError):
            return self._sigma_sec
        rets = [
            math.log(closes[i] / closes[i - 1])
            for i in range(1, len(closes))
            if closes[i] > 0 and closes[i - 1] > 0
        ]
        if len(rets) < 2:
            return self._sigma_sec
        sd_per_min = statistics.pstdev(rets)
        self._sigma_sec = sd_per_min / math.sqrt(60.0)
        self._sigma_at = now
        return self._sigma_sec
