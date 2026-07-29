"""Servicio del IFE: ingerir -> detectar -> medir -> persistir.

Unica pieza del paquete que habla con la red, con la BD y con el reloj. Todo lo
que decide algo (que es una cascada, que es un eslabon, cuanto vale una wallet)
vive en modulos puros (`ingest`, `cascades`, `scoring`, `metrics`) y se testea
sin infraestructura.

CONTRATO DE AISLAMIENTO (identico al del MIG y al de Wallet Intelligence, y aqui
tambien es estructural, no una promesa):

  - No importa `medusa.execution`, `medusa.trading`, `medusa.risk` ni
    `medusa.strategies`. No tiene con que operar aunque quisiera.
  - No escribe una sola fila fuera de las tablas `flow_*`.
  - Corre en su propio loop, con `wait_for(timeout)`, y jamas se le espera desde
    el ciclo de trading.
  - Apagado por defecto (`FLOW_ENABLED=false`). Encenderlo no cambia una sola
    decision del bot: añade filas a seis tablas nuevas y una pagina al panel.

Y EL CONTRATO EPISTEMICO, que es el que de verdad define este paquete: aqui se
MIDE PROPAGACION. Ninguna funcion de este fichero devuelve un lado, un tamaño ni
un precio de entrada, y ninguna afirma que una wallet haya provocado la entrada
de otra.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import time

from medusa.config import get_settings
from medusa.intelligence.flow import cascades as casc
from medusa.intelligence.flow import ingest
from medusa.intelligence.flow import repository as flow_repo
from medusa.intelligence.flow import scoring
from medusa.intelligence.flow.feed import FlowFeed
from medusa.intelligence.flow.types import Cascade, FlowTrade
from medusa.logging_setup import err

UTC = dt.timezone.utc


class InformationFlowService:
    def __init__(self, log, publish_log=None) -> None:
        self.log = log
        self.s = get_settings()
        # Publicador de eventos opcional (el mismo del resto del engine). Si no
        # llega, el servicio funciona igual: no puede depender de el.
        self._publish = publish_log
        self._feed: FlowFeed | None = None
        self.last_run: dict | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.s.flow_enabled)

    def info(self) -> dict:
        """Descripcion del servicio para /flow/info (no toca la BD)."""
        return {
            "enabled": self.enabled,
            "interval_seconds": self.s.flow_interval,
            "max_markets": self.s.flow_max_markets,
            "trades_per_market": self.s.flow_trades_per_market,
            "lookback_hours": self.s.flow_lookback_hours,
            "window_seconds": self.s.flow_window_seconds,
            "min_participants": self.s.flow_min_participants,
            "max_hops": self.s.flow_max_hops,
            "consensus_fraction": self.s.flow_consensus_fraction,
            "speed_half_life": self.s.flow_speed_half_life,
            "early_fraction": self.s.flow_early_fraction,
            "late_fraction": self.s.flow_late_fraction,
            "min_price_move": self.s.flow_min_price_move,
            "min_samples": self.s.flow_min_samples,
            "retention_days": self.s.flow_retention_days,
            "trade_retention_days": self.s.flow_trade_retention_days,
            # Los tres NO del paquete, en la respuesta de la API para que no
            # haya que leerse el codigo para saber que es esto.
            "measures_causality": False,
            "can_place_orders": False,
            "emits_signals": False,
            "last_run": self.last_run,
        }

    def _get_feed(self) -> FlowFeed:
        if self._feed is None:
            self._feed = FlowFeed(self.log)
        return self._feed

    async def close(self) -> None:
        if self._feed is not None:
            await self._feed.close()
            self._feed = None

    # ------------------------------------------------------------ ingesta --
    async def ingest_tape(self, market_ids: list[str]) -> list[FlowTrade]:
        """Descarga y normaliza la cinta publica de cada mercado.

        Un mercado que falle se queda FUERA de esta pasada en vez de entrar a
        medias: una cinta incompleta no produce "cascadas degradadas", produce
        cascadas FALSAS -- participantes que faltan cambian el rango de todos
        los demas y con el todas las metricas de liderazgo.
        """
        feed = self._get_feed()
        out: list[FlowTrade] = []
        for market_id in market_ids:
            try:
                rows = await feed.fetch_trades(
                    market_id, limit=self.s.flow_trades_per_market)
                if rows:
                    out.extend(ingest.normalize_trades(market_id, rows))
            except Exception as exc:  # noqa: BLE001 - un mercado malo no tumba la pasada
                self.log.warning("flow.ingest_failed", market=market_id, error=err(exc))
        return out

    async def resolutions_of(self, market_ids: list[str]) -> dict[str, float | None]:
        """Resolucion de cada mercado (precio final del YES) o None si sigue vivo."""
        if not market_ids:
            return {}
        try:
            meta = await self._get_feed().fetch_market_meta(market_ids)
        except Exception as exc:  # noqa: BLE001
            self.log.warning("flow.meta_failed", error=err(exc))
            return {}
        return {cid: ingest.resolution_of(row) for cid, row in meta.items()}

    # ------------------------------------------------------------ analisis --
    def analyze(
        self, trades: list[FlowTrade], resolutions: dict[str, float | None] | None = None,
    ) -> dict:
        """Cinta -> cascadas -> eslabones -> metricas. PURO (sin BD, sin red).

        Se expone aparte de `run()` para poder verificar el pipeline entero con
        datos sinteticos y para que la API pueda previsualizar sin escribir nada.
        """
        found: list[Cascade] = casc.detect_cascades(
            trades,
            window_seconds=self.s.flow_window_seconds,
            min_participants=self.s.flow_min_participants,
            consensus_fraction=self.s.flow_consensus_fraction,
        )
        casc.annotate_resolutions(found, resolutions or {})
        events = casc.all_propagation_events(
            found, max_hops=self.s.flow_max_hops,
            max_lag_seconds=self.s.flow_window_seconds,
        )
        wallets = scoring.wallet_metrics(
            found,
            min_samples=self.s.flow_min_samples,
            speed_half_life=self.s.flow_speed_half_life,
            early_fraction=self.s.flow_early_fraction,
            late_fraction=self.s.flow_late_fraction,
            min_price_move=self.s.flow_min_price_move,
        )
        markets = scoring.market_metrics(
            found, events, min_samples=self.s.flow_min_participants)
        return {
            "cascades": found,
            "events": events,
            "wallets": wallets,
            "markets": markets,
            "pairs": scoring.pair_stats(
                events, min_observations=self.s.flow_min_pair_observations),
            "summary": scoring.flow_summary(found, events),
        }

    # -------------------------------------------------------------- pasada --
    async def run(self, persist: bool = True) -> dict:
        """Pasada completa. Devuelve el resumen.

        No captura excepciones a proposito: quien la llama decide que hacer con
        el fallo (el loop del engine la aisla; la API la traduce a un 5xx). Un
        try/except aqui haria que una pasada a medias pareciera un exito.
        """
        started = time.time()
        markets = await flow_repo.target_markets(limit=self.s.flow_max_markets)
        market_ids = [str(m.get("id") or "") for m in markets]
        market_ids = [m for m in market_ids if m]

        fresh = await self.ingest_tape(market_ids)
        written_trades = await flow_repo.save_trades(fresh) if persist else 0

        # La ventana de ANALISIS sale de la BD, no de la red: la API pagina y
        # una sola peticion no llega a las 72 h que interesan. Si no se persiste
        # (preview), se analiza solo lo que se acaba de descargar.
        since = dt.datetime.now(UTC) - dt.timedelta(hours=self.s.flow_lookback_hours)
        tape = await flow_repo.load_trades(market_ids, since) if persist else fresh

        resolutions = await self.resolutions_of(market_ids)
        result = self.analyze(tape, resolutions)
        elapsed = time.time() - started

        written = {"trades": written_trades, "cascades": 0, "events": 0,
                   "wallets": 0, "markets": 0}
        if persist:
            written["cascades"] = await flow_repo.save_cascades(result["cascades"])
            written["events"] = await flow_repo.save_events(result["events"])
            written["wallets"] = await flow_repo.upsert_wallet_metrics(result["wallets"])
            written["markets"] = await flow_repo.upsert_market_metrics(result["markets"])
            await flow_repo.save_snapshot(
                result["summary"], markets=len(market_ids), trades=len(tape),
                build_seconds=elapsed,
            )

        summary = {
            "ok": True,
            "persisted": persist,
            "build_seconds": round(elapsed, 3),
            "markets_scanned": len(market_ids),
            "trades_fetched": len(fresh),
            "trades_analyzed": len(tape),
            "written": written,
            "stats": result["summary"],
            "top_leaders": [w.to_dict() for w in result["wallets"][:5]],
            "top_pairs": result["pairs"][:5],
        }
        self.last_run = {
            "build_seconds": summary["build_seconds"],
            "markets": len(market_ids),
            "cascades": result["summary"]["cascades"],
            "events": result["summary"]["events"],
            "wallets": result["summary"]["wallets"],
        }
        self.log.info("flow.run_done", **self.last_run)
        return summary

    async def run_guarded(self) -> dict | None:
        """`run()` con timeout y sin propagar errores: la variante del loop del
        engine. Si el motor falla o se pasa de tiempo, se registra y el resto del
        sistema sigue exactamente igual."""
        try:
            return await asyncio.wait_for(self.run(), timeout=self.s.flow_timeout)
        except asyncio.TimeoutError:
            self.log.warning("flow.run_timeout", timeout=self.s.flow_timeout)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - el IFE jamas tumba el engine
            self.log.error("flow.run_failed", error=err(exc), exc_info=True)
        return None

    async def prune(self) -> dict:
        """Poda la cinta cruda (retencion corta) y el conocimiento (larga).
        Las metricas por wallet y por mercado nunca se podan."""
        return await flow_repo.prune(
            self.s.flow_retention_days, self.s.flow_trade_retention_days)
