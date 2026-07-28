"""Notificaciones por Discord webhook.

UNICO canal de notificaciones permitido (sin Telegram / Email / Slack). La URL
se lee de DISCORD_WEBHOOK_URL (entorno), nunca del codigo.

Robustez: si el webhook falla, se registra y se sigue. Una notificacion caida
jamas debe tumbar el engine ni bloquear el bucle de trading.
"""

from __future__ import annotations

import asyncio
import time

import httpx

from medusa.config import get_settings
from medusa.logging_setup import err

# Colores de los embeds.
_GREEN = 0x2ECC71
_RED = 0xE74C3C
_BLUE = 0x3498DB
_ORANGE = 0xE67E22
_GREY = 0x95A5A6

# Ventana de anti-spam para mensajes repetidos (errores / fallos de API).
_THROTTLE_SECONDS = 300


class DiscordNotifier:
    def __init__(self, webhook_url: str | None = None, log=None) -> None:
        settings = get_settings()
        self.webhook_url = webhook_url if webhook_url is not None else settings.discord_webhook_url
        self.log = log
        self._client: httpx.AsyncClient | None = None
        self._last_sent: dict[str, float] = {}
        self._lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.webhook_url)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=10.0)
        return self._client

    # ------------------------------------------------------------ transporte --
    async def _post(self, embed: dict, throttle_key: str | None = None) -> None:
        if not self.enabled:
            return
        if throttle_key is not None:
            now = time.time()
            if now - self._last_sent.get(throttle_key, 0.0) < _THROTTLE_SECONDS:
                return
            self._last_sent[throttle_key] = now

        payload = {"username": "Medusa", "embeds": [embed]}
        try:
            async with self._lock:  # el webhook tiene rate limit: serializar envios
                resp = await self._get_client().post(self.webhook_url, json=payload)
                if resp.status_code == 429:
                    retry = float(resp.headers.get("Retry-After", "1"))
                    await asyncio.sleep(min(retry, 5.0))
                    resp = await self._get_client().post(self.webhook_url, json=payload)
            if resp.status_code >= 400:
                self._warn("discord.rejected", status=resp.status_code, body=resp.text[:200])
        except Exception as exc:  # noqa: BLE001 - notificar nunca puede romper el engine
            self._warn("discord.send_failed", error=err(exc))

    def _warn(self, event: str, **kw) -> None:
        if self.log is not None:
            self.log.warning(event, **kw)

    @staticmethod
    def _embed(title: str, description: str, color: int, fields: list | None = None) -> dict:
        embed: dict = {"title": title, "description": description, "color": color}
        if fields:
            embed["fields"] = fields
        return embed

    # ---------------------------------------------------------------- inicio --
    async def startup(self, mode: str) -> None:
        await self._post(self._embed(
            "🟢 Medusa iniciado",
            f"Bot operativo en modo **{mode.upper()}**.",
            _GREEN if mode == "paper" else _ORANGE,
        ))

    async def shutdown(self) -> None:
        await self._post(self._embed(
            "🔴 Medusa detenido", "El engine se ha apagado.", _GREY,
        ))

    # ------------------------------------------------------------------ modo --
    async def mode_change(self, old: str, new: str) -> None:
        await self._post(self._embed(
            "🔀 Cambio de modo",
            f"**{old.upper()} → {new.upper()}**"
            + ("\n\n⚠️ **DINERO REAL ACTIVO**" if new == "live" else ""),
            _ORANGE if new == "live" else _BLUE,
        ))

    # --------------------------------------------------------------- trading --
    async def opportunity(self, question: str, outcome: str, edge: float,
                          confidence: float, price: float) -> None:
        await self._post(self._embed(
            "🎯 Nueva oportunidad",
            question[:200],
            _BLUE,
            [
                {"name": "Outcome", "value": outcome, "inline": True},
                {"name": "Precio", "value": f"{price:.3f}", "inline": True},
                {"name": "Edge", "value": f"{edge:+.3f}", "inline": True},
                {"name": "Confianza", "value": f"{confidence:.0%}", "inline": True},
            ],
        ))

    async def arb_opportunity(self, question: str, profit: float, detail: str) -> None:
        """Arbitraje intra-mercado detectado (ganancia sin riesgo si se ejecuta
        a mano y rapido). Medido 0 veces en 110 mercados: si suena, importa."""
        await self._post(
            self._embed(
                "💎 Arbitraje intra-mercado",
                f"{question[:200]}\n\n{detail}\n\n"
                "⚠️ Requiere ejecutar DOS patas a la vez: Medusa no lo opera "
                "solo, es una alerta para revision manual inmediata.",
                _GREEN,
                [{"name": "Ganancia por par", "value": f"${profit:.3f}", "inline": True}],
            ),
            throttle_key=f"arb:{question[:80]}",
        )

    async def trade_entry(self, question: str, outcome: str, price: float, size: float,
                          usd: float, edge: float, mode: str, partial: bool = False) -> None:
        await self._post(self._embed(
            f"📈 Entrada{' (fill parcial)' if partial else ''} · {mode.upper()}",
            question[:200],
            _BLUE,
            [
                {"name": "Outcome", "value": outcome, "inline": True},
                {"name": "Precio medio", "value": f"{price:.3f}", "inline": True},
                {"name": "Shares", "value": f"{size:.1f}", "inline": True},
                {"name": "Coste", "value": f"${usd:.2f}", "inline": True},
                {"name": "Edge", "value": f"{edge:+.3f}", "inline": True},
            ],
        ))

    async def trade_exit(self, question: str, outcome: str, exit_price: float,
                         pnl: float, roi: float, reason: str, mode: str) -> None:
        won = pnl > 0
        await self._post(self._embed(
            f"{'✅ Ganancia' if won else '❌ Perdida'} · {mode.upper()}",
            question[:200],
            _GREEN if won else _RED,
            [
                {"name": "Outcome", "value": outcome, "inline": True},
                {"name": "Salida", "value": f"{exit_price:.3f}", "inline": True},
                {"name": "PnL", "value": f"${pnl:+.2f}", "inline": True},
                {"name": "ROI", "value": f"{roi:+.1%}", "inline": True},
                {"name": "Motivo", "value": reason, "inline": False},
            ],
        ))

    # --------------------------------------------------- up/down 5 min (paper) --
    async def updown_entry(self, asset: str, favored: str, price: float, shares: float,
                           usd: float, prob: float, ev: float, secs_left: float) -> None:
        await self._post(self._embed(
            f"⏱️ Up/Down {asset.upper()} · PAPER",
            f"Apuesta **{favored}** en la ventana de 5 min ({secs_left:.0f}s al cierre).",
            _BLUE,
            [
                {"name": "Lado", "value": favored, "inline": True},
                {"name": "Precio", "value": f"{price:.3f}", "inline": True},
                {"name": "Shares", "value": f"{shares:.1f}", "inline": True},
                {"name": "Coste", "value": f"${usd:.2f}", "inline": True},
                {"name": "P(justa)", "value": f"{prob:.1%}", "inline": True},
                {"name": "EV/share", "value": f"{ev:+.3f}", "inline": True},
            ],
        ))

    # --------------------------------------------------------------- sistema --
    async def error(self, message: str, detail: str = "") -> None:
        await self._post(
            self._embed("⚠️ Error", f"{message}\n```{detail[:400]}```", _RED),
            throttle_key=f"error:{message}",
        )

    async def api_failure(self, service: str, detail: str = "") -> None:
        await self._post(
            self._embed("🌐 Fallo de API", f"**{service}**\n```{detail[:400]}```", _ORANGE),
            throttle_key=f"api:{service}",
        )

    async def risk_alert(self, message: str) -> None:
        await self._post(
            self._embed("🛑 Alerta de riesgo", message, _RED),
            throttle_key=f"risk:{message}",
        )
