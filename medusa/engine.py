"""Engine principal de Medusa (orquestador asincrono).

Cablea el pipeline de inteligencia multi-estrategia:

    escaneo global (universo completo, throttled)
        -> clasificacion por categoria + pre-scoring (ranking dinamico)
        -> analisis profundo del top-K (order books, historico)
        -> estrategias (StrategyManager; TODAS las señales se registran)
        -> asignacion de capital (peso por estrategia/categoria, por datos)
        -> risk -> execution(paper|live)

y ademas vigila las posiciones abiertas (take-profit, stop-loss, liquidacion por
resolucion) ANTES de buscar entradas nuevas: primero se protege lo que ya esta en
riesgo, despues se arriesga mas.

Las señales en shadow se resuelven periodicamente contra el resultado real del
mercado: ese historial es el unico juez de que estrategias llegan a operar.

Diseñado para 24/7: bucles independientes (heartbeat, trading, mantenimiento) y
apagado ordenado ante SIGTERM/SIGINT (docker stop).
"""

import asyncio
import datetime as dt
import signal
import time

from medusa.allocation import AllocationManager
from medusa.config import Settings, get_settings
from medusa.core import events, state
from medusa.core.enums import LogType, Mode
from medusa.core.models import Opportunity
from medusa.data import repositories as repo
from medusa.data.polymarket.client import PolymarketClient
from medusa.execution.live import LiveExecutionEngine, LiveTradingBlocked
from medusa.execution.paper import PaperExecutionEngine
from medusa.execution.reconcile import Reconciler
from medusa.infra.db import check_db, dispose, init_db
from medusa.infra.redis_bus import check_redis, close
from medusa.intelligence.flow import InformationFlowService
from medusa.intelligence.mig import MIGService
from medusa.intelligence.wallet import WalletIntelligenceService
from medusa.intelligence_layer import IntelligenceRunner, build_default_modules
from medusa.logging_setup import configure_logging
from medusa.notifications.discord import DiscordNotifier
from medusa.portfolio.metrics import compute_metrics
from medusa.risk.manager import RiskManager
from medusa.scanner.scanner import MarketScanner
from medusa.strategies import StrategyManager, StrategySignal, build_default_strategies
from medusa.trading import TradingEngine
from medusa.updown import build_updown_trader
from medusa.logging_setup import err


class Engine:
    def __init__(self, settings: Settings, log) -> None:
        self.settings = settings
        self.log = log
        self._stop = asyncio.Event()
        self._reconcile_report = None
        self._reconcile_at = 0.0
        # Universo puntuado (ScoredMarket) del ultimo escaneo global.
        self._universe = []
        self._universe_at = 0.0
        self._signals_resolved_at = 0.0

        # Contexto compartido con el Intelligence Layer: lo que el analisis
        # profundo ya trajo (libros, historico). El layer lo reutiliza en vez de
        # volver a pedirlo, que es lo que lo hace gratis en llamadas API.
        self._deep_ctx: dict = {"books": {}, "history": {}}
        self._deep_markets: list = []

        self.client = PolymarketClient()
        self.notifier = DiscordNotifier(log=log)
        self.scanner = MarketScanner(self.client, log)
        self.strategies = StrategyManager(log, build_default_strategies(log))
        self.allocator = AllocationManager(log)
        self.intel = IntelligenceRunner(
            log, build_default_modules(log), events.publish_log
        )
        self.risk = RiskManager(log)
        self.trader = TradingEngine(self.client, self.notifier, log, events.publish_log)
        self.reconciler = Reconciler(self.client, log)
        self._paper = PaperExecutionEngine(self.client, log)
        self._live = LiveExecutionEngine(self.client, log)
        # Micro-trader "Up or Down" 5 min: subsistema desacoplado, en su propio
        # loop y su propia tabla. Apagado por defecto; si el flag esta off, su
        # loop no hace absolutamente nada. SOLO PAPER (sin camino a Live).
        self.updown = build_updown_trader(
            self.client, self.notifier, log, events.publish_log
        )
        # Market Intelligence Graph: observa el sistema y construye el grafo de
        # relaciones. Solo lee tablas y escribe en las suyas (`mig_*`); no tiene
        # adapter de ejecucion ni conoce al Risk Manager. Apagado por defecto.
        self.mig = MIGService(log, events.publish_log)
        # Wallet Intelligence: perfila wallets publicas y produce features.
        # NO es copy trading y no tiene forma de operar (ver el paquete). Su
        # loop es independiente y esta apagado por defecto.
        self.wallets = WalletIntelligenceService(log, events.publish_log)
        # Information Flow Engine: mide como se propaga la informacion (quien
        # entra antes, cuanto tarda el siguiente, cuanto tarda el consenso).
        # Mide PROPAGACION, jamas causalidad. No opera, no emite señales y su
        # loop es independiente. Apagado por defecto.
        self.flow = InformationFlowService(log, events.publish_log)

    def adapter_for(self, mode: Mode):
        return self._live if mode == Mode.LIVE else self._paper

    # ------------------------------------------------------------- arranque --
    async def _wait_for(self, check, name: str, retries: int = 30, delay: float = 2.0) -> None:
        for attempt in range(1, retries + 1):
            try:
                await check()
                self.log.info("infra.connected", service=name)
                return
            except Exception as exc:  # noqa: BLE001 - reintentar ante cualquier fallo
                self.log.warning("infra.waiting", service=name, attempt=attempt, error=err(exc))
                await asyncio.sleep(delay)
        raise RuntimeError(f"No se pudo conectar a {name} tras {retries} intentos")

    async def startup(self) -> None:
        self.log.info("engine.starting", configured_mode=self.settings.mode)
        await self._wait_for(check_db, "postgres")
        await self._wait_for(check_redis, "redis")
        await init_db()

        # Postgres es la fuente de verdad del modo; Redis solo lo cachea.
        st = await repo.ensure_bot_state(
            self.settings.mode, self.settings.paper_starting_balance
        )
        mode = Mode(st["mode"])
        await state.set_mode(mode)
        await state.set_kill_switch(bool(st.get("kill_switch")))

        # Los pesos de asignacion deben existir desde el primer ciclo: sin
        # refresh inicial, una estrategia live-enabled con historial valido
        # quedaria en peso 0 hasta el primer refresco periodico.
        await self.allocator.refresh(force=True)

        self.log.info("engine.started", mode=mode.value, balance=st["balance"])
        await events.publish_log(
            LogType.SYSTEM, f"Medusa iniciado en modo {mode.value.upper()}",
            {"balance": st["balance"]},
        )
        await self.notifier.startup(mode.value)

    # ---------------------------------------------------------------- ciclo --
    async def _demote_to_paper(self, reason: str) -> Mode:
        self.log.error("engine.live_blocked", error=reason)
        await state.set_mode(Mode.PAPER)
        await repo.update_bot_state(mode=Mode.PAPER.value)
        await events.publish_log(LogType.RISK, f"Live bloqueado, se vuelve a PAPER: {reason}")
        await self.notifier.risk_alert(f"Live bloqueado, se vuelve a PAPER: {reason}")
        return Mode.PAPER

    async def _resolve_mode(self) -> Mode:
        """Modo efectivo. Si Live esta pedido pero no es seguro, degrada a Paper."""
        mode = await state.get_mode() or Mode.PAPER
        if mode != Mode.LIVE:
            return mode
        try:
            await self._live.ensure_unlocked()
        except LiveTradingBlocked as exc:
            return await self._demote_to_paper(str(exc))

        # La cadena manda. Operar con dinero real sobre una foto de la cartera que
        # no cuadra es peor que no operar: el Risk Manager dimensionaria contra
        # una exposicion falsa.
        try:
            report = await self._reconcile_throttled()
        except Exception as exc:  # noqa: BLE001
            return await self._demote_to_paper(f"no se pudo reconciliar contra la cadena: {exc}")
        if not report.clean:
            return await self._demote_to_paper(report.summary())
        return Mode.LIVE

    async def _reconcile_throttled(self):
        """Reconcilia como mucho cada `reconcile_interval`.

        La primera pasada de cada arranque es obligatoria (es justo despues de un
        crash cuando puede haber quedado una posicion sin registrar); a partir de
        ahi se cachea para no castigar la Data API en cada ciclo.
        """
        now = time.time()
        if (self._reconcile_report is not None
                and now - self._reconcile_at < self.settings.reconcile_interval):
            return self._reconcile_report
        report = await self.reconciler.run(await repo.get_open_positions("live"))
        self._reconcile_report, self._reconcile_at = report, now
        return report

    async def _refresh_universe(self):
        """Escaneo global throttled. Si falla, se sigue con el universo viejo:
        un ranking de hace 5 minutos es mejor que un ciclo ciego."""
        now = time.time()
        if self._universe and now - self._universe_at < self.settings.global_scan_interval:
            return self._universe
        try:
            self._universe = await self.scanner.scan_universe()
            self._universe_at = now
        except Exception as exc:  # noqa: BLE001
            self.log.error("engine.universe_scan_failed", error=err(exc))
            await events.publish_log(LogType.ERROR, f"Fallo el escaneo global: {exc}")
            await self.notifier.api_failure("Polymarket Gamma API", str(exc))
        return self._universe

    @staticmethod
    def _to_opportunity(sig: StrategySignal) -> Opportunity:
        """Adapta una señal operable al contrato del Risk Manager."""
        return Opportunity(
            market_id=sig.market_id,
            question=sig.question,
            outcome=sig.outcome,
            market_price=sig.entry_price,
            market_prob=sig.market_prob,
            medusa_prob=sig.signal_prob,
            edge=sig.edge,
            confidence=sig.confidence,
            action="buy_yes" if sig.outcome == "YES" else "buy_no",
            reason=sig.explanation,
            token_id=sig.token_id,
            spread=sig.spread,
            liquidity=sig.liquidity,
            strategy=sig.strategy,
            category=sig.category,
            score=sig.score,
        )

    async def _attach_features(self, enriched: list) -> None:
        """Pone las features del Feature Store en cada MarketContext.

        Tolerante a fallos por diseño: si el store no responde, las estrategias
        reciben {} y operan como antes de que el layer existiera. El
        Intelligence Layer AÑADE informacion; su ausencia nunca puede degradar
        lo que ya funcionaba.
        """
        if not self.settings.intelligence_enabled or not enriched:
            return
        try:
            features = await repo.latest_features([m.id for m, _ in enriched])
        except Exception as exc:  # noqa: BLE001
            self.log.warning("intel.features_read_failed", error=err(exc))
            return
        for market, ctx in enriched:
            ctx.features = features.get(market.id, {})

    async def _cycle(self) -> None:
        # El kill-switch corta el RIESGO NUEVO, no abandona el riesgo ya
        # abierto ni ciega al sistema.
        #
        # Antes salia aqui con un `return` y eso invertia el principio central
        # del engine ("primero se protege lo que ya esta en riesgo"): saltaba
        # porque se estaba perdiendo dinero y su reaccion era dejar de vigilar
        # las posiciones abiertas. En live, una posicion podia atravesar su
        # stop-loss sin que nadie la cerrase hasta el cambio de dia UTC. De
        # paso tampoco tomaba equity (hueco en la curva) ni registraba señales
        # ni features, que son operaciones de riesgo CERO.
        #
        # Ahora: con kill-switch se gestiona lo abierto, se mide, se escanea y
        # se aprende; lo unico que NO se hace es abrir posiciones nuevas.
        killed = await state.get_kill_switch()
        if killed:
            self.log.warning("engine.kill_switch_active")

        mode = await self._resolve_mode()
        adapter = self.adapter_for(mode)

        # 1) Proteger lo abierto antes de abrir nada nuevo. SIEMPRE, incluso
        #    con el kill-switch puesto: es justo cuando mas falta hace.
        await self.trader.manage_positions(adapter)

        # 2) Universo global (throttled) ya clasificado y rankeado.
        universe = await self._refresh_universe()
        if not universe:
            return

        # 3) Analisis profundo SOLO del top del ranking.
        top = universe[: self.settings.prescore_top_k]
        enriched = await self.scanner.deep_enrich(
            top,
            need_book_no=self.strategies.needs_book_no,
            need_history=self.strategies.needs_history,
        )

        # 4) Features del Intelligence Layer (si esta activo). Se LEEN del
        #    Feature Store, no se calculan aqui: el layer va por su cuenta y el
        #    ciclo de trading jamas lo espera. Si el store falla o esta vacio,
        #    las estrategias reciben {} y siguen funcionando igual.
        await self._attach_features(enriched)

        # Guardar el contexto caro para que el layer lo reutilice sin pedir
        # nada a la API.
        self._deep_markets = [m for m, _ in enriched]
        self._deep_ctx = {
            "books": {m.id: c.book_yes for m, c in enriched if c.book_yes is not None},
            "history": {m.id: c.history for m, c in enriched if c.history},
        }

        # 5) Estrategias: toda señal se registra (shadow); el dedup evita
        #    contar el mismo juicio en cada ciclo mientras persista.
        signals: list[StrategySignal] = []
        for market, ctx in enriched:
            signals.extend(self.strategies.evaluate(market, ctx))

        new_signals = []
        if signals:
            existing = await repo.open_signal_keys()
            new_signals = [
                s for s in signals
                if (s.strategy, s.market_id, s.outcome) not in existing
            ]
            await repo.save_signals(new_signals)

        # Arbitraje: ganancia sin riesgo si se ejecuta a mano y rapido. Es la
        # unica señal que merece alerta inmediata aunque el bot no la opere.
        for sig in new_signals:
            if sig.strategy == "stat_arb":
                await self.notifier.arb_opportunity(sig.question, sig.edge, sig.explanation)

        # 6) Candidatos a operar. Triple candado, y cada uno tapa un agujero
        #    distinto:
        #      (a) la FASE debe permitir operar EN ESTE MODO (una estrategia en
        #          fase paper no toca live ni con el bot en live);
        #      (b) la señal debe ser ejecutable (stat_arb nunca lo es);
        #      (c) el asignador debe darle peso > 0, que exige >=30 señales
        #          resueltas con ROI de limite inferior > 0 (lo aplica el Risk
        #          Manager mas abajo).
        #    Con la config por defecto (todo en shadow) no pasa ninguna: el bot
        #    escanea, aprende y sigue plano.
        await self.allocator.refresh()
        tradeable = [
            self._to_opportunity(sig) for sig in signals
            if sig.tradeable and self.strategies.can_trade_in(sig.strategy, adapter.mode)
        ]
        for opp in tradeable:
            await repo.save_opportunity(opp)
            if self.settings.notify_opportunities:
                await self.notifier.opportunity(
                    opp.question, opp.outcome, opp.edge, opp.confidence, opp.market_price,
                )

        await events.publish_log(
            LogType.INFO,
            f"Escaneo: {len(universe)} mercados en ranking, {len(enriched)} en "
            f"analisis profundo, {len(signals)} señales ({len(new_signals)} "
            f"nuevas), {len(tradeable)} operables",
            {
                "universe": len(universe), "deep": len(enriched),
                "signals": len(signals), "new_signals": len(new_signals),
                "tradeable": len(tradeable),
            },
        )

        # 7) Riesgo: dimensionar y aprobar (el asignador escala, el riesgo manda).
        #    AQUI muerde el kill-switch: se ha escaneado, valorado y aprendido,
        #    pero no se abre NADA. Es el unico efecto que debe tener.
        if killed:
            await self._snapshot_and_guard(adapter)
            return

        balance = await adapter.get_balance()
        positions = await repo.get_open_positions(adapter.mode)
        instructions = self.risk.approve(tradeable, balance, positions,
                                         allocator=self.allocator)

        # 8) Ejecutar.
        if instructions:
            opened = await self.trader.open_positions(instructions, adapter)
            self.log.info("engine.executed", approved=len(instructions), opened=opened)

        # 9) Resolver señales pendientes (throttled) y refrescar pesos.
        await self._resolve_signals_throttled()

        # 10) Foto de equity + kill-switch por drawdown.
        await self._snapshot_and_guard(adapter)

    async def _resolve_signals_throttled(self) -> None:
        """Marca ganada/perdida cada señal cuyo mercado ya resolvio.

        Es el paso que convierte señales en CONOCIMIENTO: sin resolver, el
        historial por estrategia/categoria no existe y la asignacion de capital
        no tiene de donde aprender.
        """
        now = time.time()
        if now - self._signals_resolved_at < self.settings.signal_resolve_interval:
            return
        self._signals_resolved_at = now

        try:
            market_ids = await repo.markets_with_pending_signals(
                self.settings.signal_resolve_batch
            )
            resolved = 0
            for market_id in market_ids:
                market = await self.client.fetch_market_by_condition(market_id)
                if market is None or market.active:
                    continue
                # Igual que la liquidacion de posiciones: solo resoluciones
                # limpias (1/0). Un closed con precio intermedio es ambiguo;
                # se deja abierto y, si nunca aclara, lo recoge el void.
                if market.yes_price >= 0.99:
                    settle_yes = 1.0
                elif market.yes_price <= 0.01:
                    settle_yes = 0.0
                else:
                    continue
                resolved += await repo.resolve_signals_for_market(market_id, settle_yes)

            voided = await repo.void_stale_signals(days=30)
            if resolved or voided:
                self.log.info("signals.resolved", resolved=resolved, voided=voided)
                await events.publish_log(
                    LogType.INFO,
                    f"Señales resueltas: {resolved} (void: {voided})",
                    {"resolved": resolved, "voided": voided},
                )
            if resolved:
                # Hay datos nuevos: los pesos deben verlos ya, no en una hora.
                await self.allocator.refresh(force=True)
        except Exception as exc:  # noqa: BLE001 - la resolucion no tumba el ciclo
            self.log.warning("signals.resolve_failed", error=err(exc))

    async def _roll_day(self, equity: float) -> float:
        """Abre el dia UTC si toca y devuelve la equity de referencia del dia.

        Al cambiar de dia se levanta el kill-switch SI lo puso el bot por perdida
        diaria. Un kill-switch manual no se toca: si el usuario paro el bot a
        mano, no puede reanudarse solo a medianoche.
        """
        st = await repo.get_bot_state() or {}
        today = dt.datetime.now(dt.timezone.utc).date().isoformat()

        if st.get("day_start_date") == today:
            return float(st.get("day_start_equity") or equity)

        updates = {"day_start_date": today, "day_start_equity": equity}
        if st.get("kill_switch") and st.get("kill_switch_auto"):
            updates["kill_switch"] = False
            updates["kill_switch_auto"] = False
            await state.set_kill_switch(False)
            msg = "Nuevo dia UTC: se levanta el kill-switch por perdida diaria y se reanuda la operativa"
            self.log.info("engine.daily_reset", equity=equity)
            await events.publish_log(LogType.RISK, msg, {"equity": equity})
            await self.notifier.risk_alert(msg)
        await repo.update_bot_state(**updates)
        return equity

    async def _snapshot_and_guard(self, adapter) -> None:
        metrics = await compute_metrics(adapter.mode)
        await repo.save_equity(
            mode=adapter.mode, balance=metrics["balance"], equity=metrics["equity"],
            exposure=metrics["exposure"], open_positions=metrics["open_positions"],
            realized_pnl=metrics["pnl_realized"], unrealized_pnl=metrics["pnl_unrealized"],
        )

        # _roll_day es el UNICO sitio que levanta el kill-switch automatico al
        # cambiar el dia UTC. Tiene que correr en CADA ciclo, tambien (y sobre
        # todo) con el kill-switch puesto: cuando el `_cycle` salia antes de
        # llegar aqui, este codigo era inalcanzable y el kill-switch quedaba
        # permanente -- el bug #3 otra vez, por otro camino.
        day_start_equity = await self._roll_day(metrics["equity"])

        if not self.risk.daily_loss_exceeded(metrics["equity"], day_start_equity):
            return
        # Solo se alerta en la TRANSICION vivo->cortado. Sin esto, ahora que el
        # guardia corre en cada ciclo, se escribiria una fila RISK por minuto y
        # Discord repetiria la misma alerta indefinidamente.
        if await state.get_kill_switch():
            return

        await state.set_kill_switch(True)
        await repo.update_bot_state(kill_switch=True, kill_switch_auto=True)
        msg = (
            f"KILL-SWITCH: la perdida de hoy supera "
            f"{self.settings.max_daily_loss_pct:.0%} (equity ${metrics['equity']:.2f} "
            f"vs ${day_start_equity:.2f} al abrir el dia). No se abren posiciones "
            f"nuevas; lo abierto se sigue gestionando. Se reanuda solo al cambiar "
            f"el dia UTC."
        )
        self.log.error("engine.kill_switch_triggered", equity=metrics["equity"],
                       day_start_equity=day_start_equity)
        await events.publish_log(LogType.RISK, msg, metrics)
        await self.notifier.risk_alert(msg)

    # --------------------------------------------------------------- bucles --
    async def _heartbeat_loop(self) -> None:
        """Late cada `heartbeat_interval`. Es lo unico que distingue, desde
        fuera, un engine vivo de uno muerto.

        Un fallo puntual de Redis NO puede saltarse el latido siguiente: se
        registra y se sigue latiendo. `_guarded` reiniciaria el loop de todos
        modos, pero absorber el parpadeo aqui evita perder el latido y evita
        que un blip de la red genere una alerta de "loop muerto" que no
        describe nada real. (Bug del 2026-07-29: un unico "Timeout reading from
        redis:6379" dejo al engine nueve horas sin latir.)
        """
        while not self._stop.is_set():
            try:
                await state.set_heartbeat(time.time())
                await events.publish(events.CH_HEARTBEAT, {"ts": time.time()})
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - el latido no puede pararse por un blip
                self.log.warning("engine.heartbeat_failed", error=err(exc))
            await self._sleep(self.settings.heartbeat_interval)

    async def _trading_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self._cycle()
            except Exception as exc:  # noqa: BLE001 - un ciclo malo no mata el bot
                self.log.error("engine.cycle_failed", error=err(exc), exc_info=True)
                await events.publish_log(LogType.ERROR, f"Fallo en el ciclo: {exc}")
                await self.notifier.error("Fallo en el ciclo de trading", str(exc))
            await self._sleep(self.settings.scan_interval)

    async def _intelligence_loop(self) -> None:
        """Loop del Intelligence Layer, INDEPENDIENTE del ciclo de trading.

        Vive aparte a proposito: el layer habla con fuentes externas y no puede
        tener ninguna via para retrasar una salida de posicion. Trabaja sobre el
        contexto que el ciclo de trading ya recolecto (libros, historico), asi
        que su coste marginal en llamadas API es cero para el modulo de
        microestructura.

        Si el layer esta apagado (default) este loop no hace absolutamente nada.
        """
        if not self.settings.intelligence_enabled:
            self.log.info("intel.disabled")
            return
        while not self._stop.is_set():
            await self._sleep(self.settings.intelligence_interval)
            if self._stop.is_set():
                break
            if not self._deep_markets:
                continue   # aun no hay un ciclo de trading completo del que leer
            try:
                # run_once ya aisla el fallo de cada modulo; este try es la red
                # de seguridad del loop entero.
                await self.intel.run_once(self._deep_markets, self._deep_ctx)
            except Exception as exc:  # noqa: BLE001 - el layer jamas tumba el engine
                self.log.error("intel.loop_failed", error=err(exc), exc_info=True)

    async def _mig_loop(self) -> None:
        """Loop del Market Intelligence Graph, INDEPENDIENTE del de trading.

        El MIG solo lee tablas y escribe en las suyas (`mig_*`): no manda
        ordenes, no toca posiciones y no conoce al Risk Manager. Aun asi vive en
        su propio loop y con timeout, por la misma razon que el Intelligence
        Layer: nada que no sea proteger o abrir posiciones puede meterse en el
        camino del ciclo de trading.

        Si el flag esta apagado (default) este loop no hace absolutamente nada.
        """
        if not self.mig.enabled:
            self.log.info("mig.disabled")
            return
        self.log.info("mig.enabled", interval=self.settings.mig_interval)
        while not self._stop.is_set():
            # Primero se espera: al arrancar, las tablas aun no tienen nada que
            # el ciclo de trading no haya escrito ya en pasadas anteriores.
            await self._sleep(self.settings.mig_interval)
            if self._stop.is_set():
                break
            # build_guarded ya absorbe timeout y errores; este loop no puede
            # tener otra forma de morir.
            summary = await self.mig.build_guarded()
            if summary:
                await events.publish_log(
                    LogType.INFO,
                    f"MIG reconstruido: {summary['stats']['nodes']} nodos, "
                    f"{summary['stats']['edges']} aristas, "
                    f"{summary['discoveries']} descubrimientos",
                    {"nodes": summary["stats"]["nodes"],
                     "edges": summary["stats"]["edges"],
                     "discoveries": summary["discoveries"]},
                )

    async def _wallet_loop(self) -> None:
        """Loop de Wallet Intelligence, INDEPENDIENTE del ciclo de trading.

        Habla con la Data API publica de Polymarket, asi que por principio va en
        su propio task y con timeout: nada que no sea proteger o abrir
        posiciones puede meterse en el camino del ciclo de trading.

        El subsistema produce FEATURES, nunca ordenes. Si el flag esta apagado
        (default) este loop no hace absolutamente nada.
        """
        if not self.wallets.enabled:
            self.log.info("wallet.disabled")
            return
        self.log.info("wallet.enabled", interval=self.settings.wallet_intel_interval)
        while not self._stop.is_set():
            await self._sleep(self.settings.wallet_intel_interval)
            if self._stop.is_set():
                break
            summary = await self.wallets.run_guarded()
            if summary:
                await events.publish_log(
                    LogType.INFO,
                    f"Wallet Intelligence: {summary['wallets_profiled']} wallets "
                    f"perfiladas ({summary['positions']} posiciones, "
                    f"{summary['clusters']} clusters)",
                    {"wallets": summary["wallets_profiled"],
                     "positions": summary["positions"],
                     "clusters": summary["clusters"]},
                )

    async def _flow_loop(self) -> None:
        """Loop del Information Flow Engine, INDEPENDIENTE del de trading.

        Habla con la Data API publica de Polymarket para leer la cinta de
        trades, asi que por principio va en su propio task y con timeout: nada
        que no sea proteger o abrir posiciones puede meterse en el camino del
        ciclo de trading.

        El motor produce EVENTOS DE PROPAGACION y METRICAS, nunca ordenes ni
        señales, y mide orden temporal, jamas causalidad. Si el flag esta
        apagado (default) este loop no hace absolutamente nada.
        """
        if not self.flow.enabled:
            self.log.info("flow.disabled")
            return
        self.log.info("flow.enabled", interval=self.settings.flow_interval)
        while not self._stop.is_set():
            await self._sleep(self.settings.flow_interval)
            if self._stop.is_set():
                break
            # run_guarded ya absorbe timeout y errores; este loop no puede tener
            # otra forma de morir.
            summary = await self.flow.run_guarded()
            if summary:
                stats = summary["stats"]
                await events.publish_log(
                    LogType.INFO,
                    f"Information Flow: {stats['cascades']} cascadas, "
                    f"{stats['events']} eventos de propagacion, "
                    f"{stats['wallets']} wallets",
                    {"cascades": stats["cascades"], "events": stats["events"],
                     "wallets": stats["wallets"],
                     "markets": summary["markets_scanned"]},
                )

    async def _updown_loop(self) -> None:
        """Loop del micro-trader 'Up or Down', INDEPENDIENTE del ciclo de trading.

        Ritmo propio (updown_tick_interval, ~5s) porque estas ventanas viven 300s
        y la decision se toma en los ultimos segundos: el ciclo de 60s del engine
        es demasiado lento. Aislado por completo: su tabla, su feed, sus alertas.
        Si el flag esta apagado (default) este loop no hace nada.
        """
        if not self.updown.enabled:
            self.log.info("updown.disabled")
            return
        # El log de arranque NUNCA debe poder tumbar el loop: los mercados
        # activos se leen de la BD (toggle) y esa lectura podria fallar.
        try:
            active = await self.updown.active_assets()
            self.log.info("updown.enabled", assets=active)
            await events.publish_log(
                LogType.SYSTEM,
                f"Micro-trader Up/Down activo (paper): "
                f"{', '.join(active) or 'sin mercados activos (toggles apagados)'}",
            )
        except Exception as exc:  # noqa: BLE001 - un fallo aqui no impide operar
            self.log.warning("updown.startup_log_failed", error=err(exc))
        while not self._stop.is_set():
            try:
                await self.updown.tick()
            except Exception as exc:  # noqa: BLE001 - el micro-trader jamas tumba el engine
                self.log.error("updown.loop_failed", error=err(exc), exc_info=True)
            await self._sleep(self.settings.updown_tick_interval)

    async def _maintenance_loop(self) -> None:
        """Poda diaria de datos de alta frecuencia. Pensado para correr semanas
        sin que nadie mire: sin esto las tablas crecen sin techo."""
        while not self._stop.is_set():
            await self._sleep(24 * 3600)
            if self._stop.is_set():
                break
            try:
                deleted = await repo.prune_old_data(self.settings.data_retention_days)
                if any(deleted.values()):
                    self.log.info("engine.pruned", **deleted)
                # El Feature Store se poda aparte y con retencion MUY larga: es
                # el histórico del que saldran los modelos, no ruido de alta
                # frecuencia.
                pruned = await repo.prune_features(self.settings.feature_retention_days)
                if pruned:
                    self.log.info("engine.pruned_features", features=pruned)
                # MIG: solo se podan descubrimientos y snapshots. Nodos y
                # aristas NO: son el grafo, y borrarlos falsearia el
                # `first_seen` del que sale la curva de crecimiento.
                if self.mig.enabled:
                    mig_pruned = await self.mig.prune()
                    if any(mig_pruned.values()):
                        self.log.info("engine.pruned_mig", **mig_pruned)
                # Wallet Intelligence: se podan historico y pasadas, nunca los
                # perfiles (perderlos borraria su `first_seen`).
                if self.wallets.enabled:
                    wi_pruned = await self.wallets.prune()
                    if any(wi_pruned.values()):
                        self.log.info("engine.pruned_wallets", **wi_pruned)
                # Information Flow: la cinta cruda se poda pronto (crece rapido);
                # cascadas, eslabones y snapshots aguantan mucho mas, porque son
                # la unica serie larga de propagacion que existe. Las metricas
                # por wallet y por mercado no se podan nunca.
                if self.flow.enabled:
                    flow_pruned = await self.flow.prune()
                    if any(flow_pruned.values()):
                        self.log.info("engine.pruned_flow", **flow_pruned)
            except Exception as exc:  # noqa: BLE001
                self.log.warning("engine.prune_failed", error=err(exc))

    async def _sleep(self, seconds: float) -> None:
        """Espera interrumpible: reacciona al stop sin agotar el intervalo."""
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    # Reintento de un loop caido: espera inicial, techo, y cuanto tiene que
    # haber aguantado para considerar que el fallo fue puntual y no un bucle de
    # arranque roto.
    LOOP_RETRY_BACKOFF = 5.0
    LOOP_RETRY_MAX = 300.0
    LOOP_HEALTHY_AFTER = 300.0
    # Anti-ruido: no se repite la alerta de Discord de un mismo loop mas a
    # menudo que esto (el log estructurado si sale en cada reintento).
    LOOP_ALERT_EVERY = 1800.0

    async def _guarded(self, loop_fn, name: str) -> None:
        """Corre un loop aislado y lo REINICIA si se cae.

        asyncio.gather propaga la primera excepcion y cancela el resto: sin este
        envoltorio, un fallo no capturado en CUALQUIER loop mata el engine y, con
        restart:unless-stopped, lo mete en un crash-loop de arranque.

        POR QUE ADEMAS REINICIA (bug real, medido en produccion el 2026-07-29):
        antes esto capturaba la excepcion, la registraba... y RETORNABA. El loop
        quedaba muerto para el resto de la vida del proceso. A las 09:14 UTC un
        timeout transitorio de Redis ("Timeout reading from redis:6379") mato el
        loop de heartbeat; el engine siguio escaneando y operando con
        normalidad durante NUEVE HORAS, pero sin latir. Consecuencias:

          - el watchdog de la API grito "CAIDA DEL SERVICIO" cada 30 min sobre un
            servicio que estaba perfectamente vivo, y
          - lo mas grave: una caida REAL habria sido indistinguible de esa falsa
            alarma. Una alarma que lleva nueve horas mintiendo ya no es alarma.

        Y el heartbeat era el caso benigno: el mismo fallo en `_trading_loop`
        habria dejado de vigilar las posiciones abiertas EN SILENCIO, que es
        justo el escenario que el diseño del engine existe para evitar.

        El backoff crece hasta el techo para no castigar a un servicio que esta
        caido de verdad, y se reinicia a la espera inicial cuando el loop ha
        aguantado lo suficiente: un fallo puntual tras horas sanas no puede
        heredar el castigo de una racha vieja.
        """
        backoff = self.LOOP_RETRY_BACKOFF
        last_alert = 0.0
        attempt = 0

        while not self._stop.is_set():
            started = time.time()
            try:
                await loop_fn()
                # Retorno limpio = el loop decidio terminar (p.ej. un subsistema
                # apagado por config). Eso NO es una caida y no se reintenta.
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - un loop no puede tumbar el engine
                attempt += 1
                alive_for = time.time() - started
                if alive_for >= self.LOOP_HEALTHY_AFTER:
                    backoff = self.LOOP_RETRY_BACKOFF   # venia sano: fallo puntual
                self.log.error(
                    "engine.loop_crashed", loop=name, error=err(exc), attempt=attempt,
                    alive_seconds=round(alive_for, 1), retry_in=backoff, exc_info=True,
                )
                if self._stop.is_set():
                    return
                try:
                    await events.publish_log(
                        LogType.ERROR,
                        f"Loop '{name}' murio ({exc}). Reintento #{attempt} en "
                        f"{backoff:.0f}s.",
                    )
                    if time.time() - last_alert >= self.LOOP_ALERT_EVERY:
                        await self.notifier.error(
                            f"Loop '{name}' murio",
                            f"{exc}. Se reintenta en {backoff:.0f}s (intento #{attempt}).",
                        )
                        last_alert = time.time()
                except Exception:  # noqa: BLE001 - avisar jamas puede impedir reintentar
                    pass
                await self._sleep(backoff)
                backoff = min(backoff * 2, self.LOOP_RETRY_MAX)

    async def run(self) -> None:
        await self.startup()
        try:
            await asyncio.gather(
                self._guarded(self._heartbeat_loop, "heartbeat"),
                self._guarded(self._trading_loop, "trading"),
                self._guarded(self._maintenance_loop, "maintenance"),
                self._guarded(self._intelligence_loop, "intelligence"),
                self._guarded(self._updown_loop, "updown"),
                self._guarded(self._mig_loop, "mig"),
                self._guarded(self._wallet_loop, "wallet"),
                self._guarded(self._flow_loop, "flow"),
            )
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        self.log.info("engine.stopping")
        try:
            await events.publish_log(LogType.SYSTEM, "Medusa detenido")
            await self.notifier.shutdown()
        except Exception as exc:  # noqa: BLE001
            self.log.warning("engine.shutdown_publish_failed", error=err(exc))
        await self.notifier.close()
        try:
            await self.updown.close()
        except Exception as exc:  # noqa: BLE001
            self.log.warning("updown.close_failed", error=err(exc))
        try:
            await self.wallets.close()
        except Exception as exc:  # noqa: BLE001
            self.log.warning("wallet.close_failed", error=err(exc))
        try:
            await self.flow.close()
        except Exception as exc:  # noqa: BLE001
            self.log.warning("flow.close_failed", error=err(exc))
        await self.client.close()
        await close()
        await dispose()
        self.log.info("engine.stopped")

    def request_stop(self, *_: object) -> None:
        self.log.info("engine.stop_signal_received")
        self._stop.set()


async def main() -> None:
    settings = get_settings()
    log = configure_logging(
        settings.log_level, settings.log_dir, settings.log_json, service="engine"
    )
    engine = Engine(settings, log)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, engine.request_stop)
        except NotImplementedError:  # p.ej. en Windows
            pass

    try:
        await engine.run()
    except Exception as exc:  # noqa: BLE001
        log.error("engine.fatal", error=err(exc), exc_info=True)
        raise


if __name__ == "__main__":
    asyncio.run(main())
