"""UpDownTrader: el loop de ENTRADA del micro-trader 'Up or Down' de 5 min.

Cada tick (cada `updown_tick_interval` s) mira la ventana viva de cada mercado
ACTIVO (los toggles del dashboard mandan) y, si en los ultimos segundos el
movimiento ya hace el resultado casi seguro Y el ask deja EV positivo tras el
peaje, abre la apuesta.

INTEGRACION CON EL PORTFOLIO: la apuesta se escribe en el ledger REAL de paper
(record_entry -> posicion + balance) exactamente como cualquier otra operacion,
asi que aparece en "Operativa · Activas", cuenta en balance/equity/exposicion y,
al resolver, pasa a "Historial" y suma al winrate/PnL/ROI. Tambien se registra
como Oportunidad (para verla en el panel de oportunidades). El fill se SIMULA
contra el libro real (motor Paper, pesimista). SOLO PAPER: nunca hay orden
on-chain.

LA LIQUIDACION NO vive aqui: la hace el Trading Engine (manage_positions) cuando
el mercado resuelve, igual que con cualquier posicion. Ese modulo tiene una
guarda para NO aplicar TP/SL ni cierre-por-proximidad a las posiciones updown
(se mantienen a resolucion) y para liquidar el outcome Up/Down a 1/0.

Aislado por diseño: cualquier fallo (feed caido, Gamma lenta, libro vacio) se
traga con un warning; nunca lanza hacia el loop del engine.
"""

from __future__ import annotations

import datetime as dt
import time

from medusa.config import get_settings
from medusa.core import state
from medusa.core.enums import LogType
from medusa.core.models import OrderRequest, Opportunity
from medusa.data import repositories as repo
from medusa.execution.paper import PaperExecutionEngine
from medusa.updown.feed import PriceFeed
from medusa.updown.market import UpDownMarketFeed, UpDownWindow, slug_for, window_start_for
from medusa.updown.model import EntryPolicy, assess, fair_prob_up, favored_side
from medusa.updown.risk import UpDownRiskManager
from medusa.logging_setup import err

# Par spot de Binance por activo: el proxy de direccion del stream Chainlink con
# el que Polymarket resuelve estas ventanas.
_SYMBOLS: dict[str, str] = {
    "bnb": "BNBUSDT",
    "btc": "BTCUSDT",
    "eth": "ETHUSDT",
    "sol": "SOLUSDT",
    "xrp": "XRPUSDT",
}

_WINDOW_SECS = 300
_ASSETS_TTL = 15.0   # cada cuanto se relee el toggle de mercados activos


class UpDownTrader:
    def __init__(self, client, notifier, log, publish_log) -> None:
        self.log = log
        self.notifier = notifier
        self.publish_log = publish_log
        self.s = get_settings()

        self.market = UpDownMarketFeed(client, log)
        self._feeds: dict[str, PriceFeed] = {}   # perezosos: solo el activo usado
        # Reusa el motor Paper SOLO para simular el fill contra el libro real.
        self._paper = PaperExecutionEngine(client, log)

        self.policy = EntryPolicy(
            entry_window_secs=self.s.updown_entry_window_secs,
            min_secs_left=self.s.updown_min_secs_left,
            min_prob=self.s.updown_min_prob,
            min_ev=self.s.updown_min_ev,
            max_ask=self.s.updown_max_ask,
        )
        # Cuanto arriesgar y cuando parar (sizing, exposicion, racha, stop diario).
        self.risk = UpDownRiskManager(log)

        self._s0: dict[str, float] = {}
        self._win_cache: dict[str, UpDownWindow] = {}
        self._acted: set[str] = set()
        self._assets_cache: list[str] = []
        self._assets_at: float = 0.0

    @property
    def enabled(self) -> bool:
        return self.s.updown_enabled

    @property
    def known_assets(self) -> list[str]:
        return list(_SYMBOLS)

    def _feed(self, asset: str) -> PriceFeed:
        feed = self._feeds.get(asset)
        if feed is None:
            feed = PriceFeed(_SYMBOLS[asset], self.log)
            self._feeds[asset] = feed
        return feed

    async def active_assets(self) -> list[str]:
        """Mercados activos (toggle del dashboard), cacheado unos segundos."""
        now = time.time()
        if now - self._assets_at < _ASSETS_TTL and self._assets_cache:
            return self._assets_cache
        try:
            assets = await repo.get_updown_assets(self.s.updown_assets)
        except Exception as exc:  # noqa: BLE001 - si la BD falla, usa el default
            self.log.warning("updown.assets_read_fail", error=err(exc))
            assets = [a.strip().lower() for a in self.s.updown_assets.split(",") if a.strip()]
        assets = [a for a in assets if a in _SYMBOLS]
        self._assets_cache, self._assets_at = assets, now
        return assets

    async def close(self) -> None:
        await self.market.close()
        for feed in self._feeds.values():
            await feed.close()

    # =====================================================================
    #  tick
    # =====================================================================
    async def tick(self) -> None:
        """Un paso del loop. Nunca lanza: aisla el fallo de cada rama."""
        self._prune_caches()

        # 1) LIQUIDAR lo resuelto, SIEMPRE (cerrar riesgo se permite incluso con
        #    el kill-switch puesto). Es el settler de estas apuestas: corre cada
        #    ~5s, no cada 60s del ciclo principal, para que un trade resuelto pase
        #    a Historial en cuanto Polymarket publique la resolucion.
        try:
            await self._settle_open()
        except Exception as exc:  # noqa: BLE001
            self.log.warning("updown.settle_loop_fail", error=err(exc))

        # 2) ENTRADAS nuevas: solo si el kill-switch no esta puesto.
        try:
            if await state.get_kill_switch():
                return
        except Exception:  # noqa: BLE001 - si Redis falla, mejor no abrir nada
            return
        for asset in await self.active_assets():
            try:
                await self._maybe_enter(asset)
            except Exception as exc:  # noqa: BLE001 - un activo no bloquea a los demas
                self.log.warning("updown.enter_fail", asset=asset, error=err(exc))

    # =====================================================================
    #  liquidacion (settler unico de las posiciones updown)
    # =====================================================================
    async def _settle_open(self) -> None:
        """Liquida las posiciones updown abiertas cuyo mercado ya resolvio.

        Es el UNICO settler de estas posiciones (manage_positions las ignora), asi
        que no hay doble liquidacion. Liquida a 1/0 contra la resolucion REAL del
        mercado, igual que la rama de resolucion del Trading Engine.
        """
        positions = await repo.get_open_positions("paper")
        for pos in positions:
            if not str(pos.get("strategy") or "").startswith("updown"):
                continue
            try:
                await self._settle_one(pos)
            except Exception as exc:  # noqa: BLE001 - una posicion no bloquea a las demas
                self.log.warning("updown.settle_one_fail", position=pos.get("id"), error=err(exc))

    def _age_secs(self, pos: dict) -> float:
        """Segundos desde que se abrio la posicion (0 si la fecha no es usable)."""
        opened = pos.get("opened_at")
        if not isinstance(opened, dt.datetime):
            return 0.0
        if opened.tzinfo is None:
            opened = opened.replace(tzinfo=dt.timezone.utc)
        return (dt.datetime.now(dt.timezone.utc) - opened).total_seconds()

    async def _settle_one(self, pos: dict) -> None:
        # FUGA DE POSICIONES ATASCADAS (2026-07-28): antes, si Gamma no devolvia
        # el mercado, seguia marcado active, o el precio se quedaba en un valor
        # intermedio, esta funcion hacia `return` y lo reintentaba... para
        # siempre. La posicion quedaba ABIERTA de forma permanente, comiendose un
        # hueco de updown_max_open (3) y su parte del presupuesto de exposicion,
        # e inflando today_bets. Con dos o tres atascadas el subsistema deja de
        # apostar del todo sin un solo error en los logs. La config ya preveia
        # esto (UPDOWN_VOID_AFTER) pero NO estaba cableada a ningun sitio: era
        # config muerta. Ahora, pasado ese plazo, la posicion se marca a mercado
        # y se cierra en vez de quedarse colgada.
        age = self._age_secs(pos)
        expired = age > self.s.updown_void_after

        market = await self.market.client.fetch_market_by_condition(pos["market_id"])
        if market is None:
            if expired:
                await self._void(pos, 0.0, "anulada: mercado ilocalizable")
            return
        up = pos["outcome"] in ("YES", "Up")
        settle = market.yes_price if up else market.no_price
        if market.active:
            if expired:
                # La ventana dura 300s: si sigue "activa" una hora despues, el
                # dato de Gamma esta roto, no es que el mercado siga vivo.
                await self._void(pos, settle, "anulada: ventana sin cerrar")
            return
        if settle >= 0.99:
            settle = 1.0
        elif settle <= 0.01:
            settle = 0.0
        elif expired:
            await self._void(pos, settle, "anulada: resolución ambigua")
            return
        else:
            return  # resolucion ambigua (precio intermedio): esperar
        proceeds = settle * pos["size"]
        closed = await repo.record_exit(
            position_id=pos["id"], mode="paper", exit_price=settle,
            proceeds=proceeds, cost=0.0, reason="resolución Up/Down",
            balance_delta=proceeds,
        )
        if not closed:
            return  # ya lo cerro otro (idempotente): no se notifica dos veces
        pnl, roi = closed["pnl"], closed["roi"]
        msg = (
            f"SALIDA {closed['outcome']} @ {settle:.3f} · PnL ${pnl:+.2f} "
            f"({roi:+.1%}) · resolución · {closed['question'][:70]}"
        )
        self.log.info("updown.settled", position=pos["id"], won=closed["won"], pnl=round(pnl, 2))
        await self.publish_log(LogType.TRADE, msg, {
            "market_id": closed["market_id"], "pnl": round(pnl, 2),
            "roi": round(roi, 4), "won": closed["won"],
        })
        await self.notifier.trade_exit(
            question=closed["question"], outcome=closed["outcome"], exit_price=settle,
            pnl=pnl, roi=roi, reason="resolución Up/Down", mode="paper",
        )

    async def _void(self, pos: dict, mark: float, reason: str) -> None:
        """Cierra una posicion atascada marcandola a `mark` (0..1).

        No inventa un ganador: liquida al precio que el mercado marque en ese
        momento (0 si no hay dato). Lo importante es liberar el hueco y la
        exposicion; el PnL resultante es honesto porque sale del mismo flujo de
        caja que cualquier otra salida.
        """
        mark = min(max(mark, 0.0), 1.0)
        proceeds = mark * pos["size"]
        closed = await repo.record_exit(
            position_id=pos["id"], mode="paper", exit_price=mark,
            proceeds=proceeds, cost=0.0, reason=reason, balance_delta=proceeds,
        )
        if not closed:
            return
        self.log.warning(
            "updown.voided", position=pos["id"], market=pos.get("market_id"),
            mark=round(mark, 3), reason=reason, age_secs=round(self._age_secs(pos)),
        )
        await self.publish_log(
            LogType.RISK,
            f"Up/Down: posición #{pos['id']} cerrada por timeout @ {mark:.3f} — {reason}",
            {"market_id": pos.get("market_id"), "pnl": round(closed["pnl"], 2)},
        )

    def _prune_caches(self) -> None:
        cutoff = int(time.time()) - 2 * _WINDOW_SECS

        def stale(slug: str) -> bool:
            try:
                return int(slug.rsplit("-", 1)[-1]) < cutoff
            except (ValueError, IndexError):
                return True

        for slug in [k for k in self._win_cache if stale(k)]:
            self._win_cache.pop(slug, None)
        for slug in [k for k in self._s0 if stale(k)]:
            self._s0.pop(slug, None)
        for slug in [k for k in self._acted if stale(k)]:
            self._acted.discard(slug)

    # =====================================================================
    #  entrada
    # =====================================================================
    async def _maybe_enter(self, asset: str) -> None:
        now = time.time()
        start = window_start_for(int(now))
        slug = slug_for(asset, start)

        if slug in self._acted:
            return
        secs_left = (start + _WINDOW_SECS) - now
        if secs_left > self.policy.entry_window_secs or secs_left < self.policy.min_secs_left:
            return

        window = self._win_cache.get(slug)
        if window is None:
            window = await self.market.current_window(asset, int(now))
            if window is None or window.slug != slug:
                return
            self._win_cache[slug] = window
        if window.closed or not window.accepting_orders:
            return
        if await repo.has_open_updown_bet(window.condition_id):
            self._acted.add(slug)
            return

        s0 = self._s0.get(slug)
        if s0 is None:
            s0 = await self._feed(asset).window_open(window.start_epoch)
            if s0 is None or s0 <= 0:
                return
            self._s0[slug] = s0

        feed = self._feed(asset)
        spot = await feed.spot()
        sigma = await feed.sigma_sec()
        if not spot or not sigma:
            return

        # Filtro de certeza ANTES de pedir el libro: si no está casi seguro, no
        # hay nada que comprar y se ahorra la llamada al CLOB.
        p_up = fair_prob_up(s0, spot, secs_left, sigma)
        favored = favored_side(p_up)
        p_fav = p_up if favored == "Up" else 1.0 - p_up
        if p_fav < self.policy.min_prob:
            return

        token = window.token_for(favored)
        book = await self.market.order_book(token)
        if book is None or book.best_ask <= 0:
            return

        decision = assess(
            s0=s0, st=spot, secs_left=secs_left, sigma_sec=sigma,
            favored_ask=book.best_ask, policy=self.policy,
        )
        if not decision.act:
            self.log.debug("updown.skip", asset=asset, slug=slug,
                           secs_left=round(secs_left), reason=decision.reason)
            return

        # Risk manager: decide CUANTO arriesgar y si se permite abrir ahora
        # (exposicion, nº de abiertas, racha, stop diario, suelo de balance...).
        # Se consulta SOLO cuando el modelo ya quiere apostar: no castiga la BD en
        # cada tick, solo cuando de verdad hay una decision.
        try:
            snap = await repo.updown_risk_data()
        except Exception as exc:  # noqa: BLE001 - sin la foto no se arriesga
            self.log.warning("updown.risk_data_fail", error=err(exc))
            return
        verdict = self.risk.assess(snap, time.time())
        if not verdict.allow:
            self.log.info("updown.risk_block", asset=asset, reason=verdict.reason)
            await self.publish_log(
                LogType.RISK,
                f"Up/Down {asset.upper()}: apuesta NO abierta por riesgo — {verdict.reason}",
                {"slug": slug, "reason": verdict.reason},
            )
            self._acted.add(slug)   # una decision por ventana; no reintentar
            return

        await self._open(asset, window, book, token, decision, verdict.stake,
                         s0, spot, sigma, secs_left)

    async def _open(self, asset, window, book, token, decision, stake,
                    s0, spot, sigma, secs_left) -> None:
        # Simular el fill contra el libro real (motor Paper, pesimista). El
        # importe lo fija el risk manager (bankroll/exposicion), no un fijo.
        req = OrderRequest(
            token_id=token, side="buy", usd_size=stake,
            market_id=window.condition_id, outcome=decision.favored, ref_price=book.best_ask,
        )
        result = self._paper._simulate(book, req)
        if result.status == "rejected":
            self.log.info("updown.fill_rejected", slug=window.slug, reason=result.reason)
            self._acted.add(window.slug)
            return

        # GUARDA ANTI-HERMES REAL (bug encontrado en produccion, 2026-07-20): el
        # candado max_ask en assess() se evalua contra book.best_ask (el mejor
        # precio del libro EN ESE INSTANTE), pero el FILL real puede caminar
        # niveles mas caros si el libro esta fino cerca de la certeza -- que es
        # justo donde mas importa el techo -- y ademas suma el slippage
        # (extra_slippage_bps) de _simulate(). Medido: 7 de 26 apuestas reales
        # (27%) se llenaron por encima del UPDOWN_MAX_ASK configurado (0.95),
        # hasta 0.974, porque el gate solo miro el mejor nivel. Eso deja pasar
        # exactamente lo que la politica dice evitar: pagar donde solo queda un
        # 1-2% de techo, con una perdida del 100% del stake si falla. Se
        # comprueba aqui el precio MEDIO real del fill, no el nominal de la
        # decision.
        #
        # TERCERA VUELTA (2026-07-28): las dos guardas de abajo miraban
        # `result.avg_price`, que es la media de `Fill.price` -- el precio SIN
        # fee. Pero lo que sale de la caja y lo que se apunta como cost_basis es
        # `result.cash_out` = nocional + fees. Es decir: el PnL SI cobra el
        # peaje, pero los candados que deben evitarlo NO lo veian. El coste real
        # por share es cash_out/filled_size, y es contra ese numero contra el que
        # hay que medir tanto el techo como el EV. Con fee_bps sobre min(p,1-p) y
        # asks cerca de 0.95, la fee anade ~0.001-0.003 por share: justo del
        # orden del min_ev configurado (0.02), o sea que la omision se comia
        # hasta un 15% del margen exigido sin que ningun candado saltara.
        unit_cost = result.cash_out / result.filled_size if result.filled_size else 0.0

        if unit_cost > self.s.updown_max_ask:
            self.log.info(
                "updown.fill_above_ceiling", slug=window.slug,
                unit_cost=round(unit_cost, 4), avg_price=round(result.avg_price, 4),
                ceiling=self.s.updown_max_ask, decision_ask=decision.ask,
            )
            self._acted.add(window.slug)
            return

        # GUARDA DE EV REAL (misma familia que el techo de arriba, 2026-07-23): el
        # candado min_ev de assess() mide EV = fair_prob - book.best_ask, el mejor
        # nivel NOMINAL. Pero el fill real camina el libro (+slippage) y paga un
        # avg_price MAYOR, sobre todo cerca de la certeza donde el libro es fino.
        # Con min_ev de solo 0.02, ese sobrecoste se come el edge entero: medido,
        # una apuesta que el gate ve a +0.027 se llena a EV real +0.016 (42% del
        # borde evaporado) SIN disparar el techo de max_ask. Ese goteo, repetido,
        # es exactamente por que el subsistema no pasa a positivo. Aqui se
        # revalida el EV contra el precio MEDIO REAL del fill: si tras el peaje ya
        # no queda el margen minimo, no se abre.
        real_ev = decision.fair_prob - unit_cost
        if real_ev < self.s.updown_min_ev:
            self.log.info(
                "updown.ev_eaten_by_fill", slug=window.slug,
                unit_cost=round(unit_cost, 4), avg_price=round(result.avg_price, 4),
                nominal_ev=decision.ev,
                real_ev=round(real_ev, 4), min_ev=self.s.updown_min_ev,
            )
            self._acted.add(window.slug)
            return

        # No abrir si no hay caja (tras un wipe a balance pequeño, esto importa).
        balance = await self._paper.get_balance()
        if result.cash_out > balance:
            self.log.info("updown.no_balance", slug=window.slug,
                          need=round(result.cash_out, 2), have=round(balance, 2))
            self._acted.add(window.slug)
            return

        strat = f"updown_{asset}"
        # Oportunidad: para que la decisión se vea en el panel de oportunidades,
        # con la misma forma que el resto (P mercado vs P Medusa, edge).
        opp = Opportunity(
            market_id=window.condition_id, question=window.question,
            outcome=decision.favored, market_price=decision.ask, market_prob=decision.ask,
            medusa_prob=decision.fair_prob, edge=decision.ev, confidence=decision.fair_prob,
            action="buy_up" if decision.favored == "Up" else "buy_down",
            reason=decision.reason, token_id=token, spread=book.spread,
            liquidity=book.ask_liquidity, strategy=strat, category="crypto",
            score=round(decision.fair_prob * 100.0, 1),
        )
        try:
            opp_id = await repo.save_opportunity(opp, status="executed")
        except Exception as exc:  # noqa: BLE001 - telemetria, no bloquea la entrada
            self.log.warning("updown.opp_save_fail", error=err(exc))
            opp_id = None

        # Entrada en el ledger REAL de paper: posicion + balance, atomico.
        await repo.record_entry(
            market_id=window.condition_id, question=window.question, mode="paper",
            outcome=decision.favored, token_id=token, price=result.avg_price,
            size=result.filled_size, status=result.status, fills=result.fills,
            cost_basis=result.cash_out, entry_edge=decision.ev,
            entry_cost=result.total_cost, balance_delta=-result.cash_out,
            entry_mid=(result.book_mid or book.mid), opportunity_id=opp_id, strategy=strat,
        )
        self._acted.add(window.slug)

        msg = (
            f"ENTRADA {asset.upper()} {decision.favored} @ {result.avg_price:.3f} · "
            f"{result.filled_size:.1f} sh · ${result.cash_out:.2f} · "
            f"P={decision.fair_prob:.3f} ({decision.z_sigmas:+.1f}σ) · "
            f"EV {decision.ev:+.3f} · {secs_left:.0f}s al cierre · [{strat}]"
        )
        self.log.info("updown.enter", asset=asset, favored=decision.favored, slug=window.slug)
        await self.publish_log(LogType.TRADE, msg, {
            "market_id": window.condition_id, "outcome": decision.favored,
            "fair_prob": decision.fair_prob, "ev": decision.ev,
        })
        await self.notifier.updown_entry(
            asset=asset, favored=decision.favored, price=result.avg_price,
            shares=result.filled_size, usd=result.cash_out,
            prob=decision.fair_prob, ev=decision.ev, secs_left=secs_left,
        )


def build_updown_trader(client, notifier, log, publish_log) -> UpDownTrader:
    return UpDownTrader(client, notifier, log, publish_log)
