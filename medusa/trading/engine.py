"""Trading Engine: convierte decisiones en ordenes y gestiona posiciones.

Es AGNOSTICO al modo: recibe un ExecutionAdapter (paper o live) y opera igual en
ambos. Ese es el punto de todo el diseño -- lo que se valida en Paper es
literalmente el mismo codigo que corre en Live; lo unico que cambia es el
adaptador que ejecuta la orden.

Responsabilidades:
  - Abrir posiciones a partir de las instrucciones aprobadas por el Risk Manager.
  - Vigilar las posiciones abiertas: take-profit, stop-loss, cierre por
    proximidad a la resolucion y liquidacion cuando el mercado ya resolvio.
"""

from __future__ import annotations

from medusa.config import get_settings
from medusa.core.enums import LogType
from medusa.core.models import OrderRequest
from medusa.data import repositories as repo
from medusa.risk.manager import TradeInstruction


class TradingEngine:
    def __init__(self, client, notifier, log, publish_log) -> None:
        self.client = client
        self.notifier = notifier
        self.log = log
        self.publish_log = publish_log   # callable(level, message, payload)
        self.s = get_settings()

    # ------------------------------------------------------------- entradas --
    async def open_positions(self, instructions: list[TradeInstruction], adapter) -> int:
        """Ejecuta las instrucciones aprobadas. Devuelve nº de posiciones abiertas."""
        opened = 0
        for ins in instructions:
            req = OrderRequest(
                token_id=ins.token_id,
                side="buy",
                usd_size=ins.usd_size,
                market_id=ins.market_id,
                outcome=ins.outcome,
                ref_price=ins.ref_price,
            )
            result = await adapter.place_order(req)

            if result.status == "rejected":
                await repo.save_order(
                    market_id=ins.market_id, mode=adapter.mode, outcome=ins.outcome,
                    side="buy", price=0.0, size=0.0, status="rejected",
                    opportunity_id=ins.opportunity_id,
                )
                await self.publish_log(
                    LogType.WARNING,
                    f"Orden rechazada en {ins.question[:60]}: {result.reason}",
                    {"market_id": ins.market_id},
                )
                continue

            # Orden + fills + posicion + balance en UNA transaccion: si el proceso
            # muere aqui, no puede quedar el dinero gastado y la posicion perdida.
            await repo.record_entry(
                market_id=ins.market_id, question=ins.question, mode=adapter.mode,
                outcome=ins.outcome, token_id=ins.token_id, price=result.avg_price,
                size=result.filled_size, status=result.status, fills=result.fills,
                cost_basis=result.cash_out, entry_edge=ins.edge,
                entry_cost=result.total_cost, opportunity_id=ins.opportunity_id,
                entry_mid=(result.book_mid or ins.ref_price),
                balance_delta=(-result.cash_out if adapter.tracks_balance else 0.0),
                strategy=ins.strategy,
            )
            opened += 1

            # La señal shadow correspondiente queda marcada como operada: asi
            # el analisis puede separar "lo que Medusa penso" de "lo que hizo".
            if ins.strategy:
                try:
                    await repo.mark_signal_traded(ins.strategy, ins.market_id, ins.outcome)
                except Exception as exc:  # noqa: BLE001 - telemetria, nunca bloquea
                    self.log.warning("trading.mark_signal_fail", error=str(exc))

            tag = f" [{ins.strategy}]" if ins.strategy else ""
            msg = (
                f"ENTRADA {ins.outcome} @ {result.avg_price:.3f} · "
                f"{result.filled_size:.1f} shares · ${result.cash_out:.2f} · "
                f"edge {ins.edge:+.3f}{tag} · {ins.question[:70]}"
            )
            await self.publish_log(LogType.TRADE, msg, {
                "market_id": ins.market_id, "outcome": ins.outcome,
                "price": round(result.avg_price, 4), "size": round(result.filled_size, 2),
                "status": result.status,
            })
            await self.notifier.trade_entry(
                question=ins.question, outcome=ins.outcome, price=result.avg_price,
                size=result.filled_size, usd=result.cash_out, edge=ins.edge,
                mode=adapter.mode, partial=(result.status == "partial"),
            )
        return opened

    # -------------------------------------------------------------- salidas --
    async def manage_positions(self, adapter) -> None:
        """Revisa cada posicion abierta: liquidacion, TP, SL o cierre por tiempo."""
        positions = await repo.get_open_positions(adapter.mode)
        for pos in positions:
            try:
                await self._manage_one(pos, adapter)
            except Exception as exc:  # noqa: BLE001 - una posicion no rompe el ciclo
                self.log.warning("trading.manage_fail", position=pos["id"], error=str(exc))

    async def _manage_one(self, pos: dict, adapter) -> None:
        # Las apuestas del micro-trader Up/Down (medusa.updown) las gestiona SU
        # PROPIO loop (rapido, 5s): se liquidan en la resolucion y no llevan TP/SL
        # ni cierre por proximidad. Aqui se IGNORAN por completo, para no
        # aplicarles salidas anticipadas ni competir con su settler (una doble
        # liquidacion concurrente duplicaria el trade y el abono de balance).
        if str(pos.get("strategy") or "").startswith("updown"):
            return

        market = await self.client.fetch_market_by_condition(pos["market_id"])

        # 1) Mercado ya resuelto -> liquidar a 1 o 0, no hay libro que vender.
        if market is not None and not market.active:
            settle = market.yes_price if pos["outcome"] == "YES" else market.no_price
            settle = 1.0 if settle >= 0.99 else (0.0 if settle <= 0.01 else settle)
            proceeds = settle * pos["size"]
            closed = await repo.record_exit(
                position_id=pos["id"], mode=adapter.mode, exit_price=settle,
                proceeds=proceeds, cost=0.0, reason="resolucion", fills=None,
                balance_delta=(proceeds if adapter.tracks_balance else 0.0),
            )
            if closed:
                await self._report_close(closed, adapter, "resolucion del mercado")
            return

        # 2) Mercado vivo. Dos valoraciones distintas, a proposito:
        #
        #    - Para EQUITY se marca contra el bid: es lo que cobrariamos hoy si
        #      liquidaramos. Conservador y realista.
        #    - Para TP/SL se compara el mid actual contra el mid de entrada: un
        #      stop debe dispararse porque el MERCADO se movio en contra, no
        #      porque acabamos de pagar el spread. Si se midiera contra el bid,
        #      toda posicion nace perdiendo el spread entero y en mercados con
        #      spread relativo > stop_loss_pct saltaria el stop en el ciclo
        #      siguiente a entrar: comprar y vender de inmediato, pagando el
        #      spread dos veces sin que el mercado se haya movido.
        book = await self.client.fetch_order_book(pos["token_id"])
        bid = book.best_bid
        mid = book.mid
        if bid <= 0 or mid <= 0:
            return

        unrealized = bid * pos["size"] - pos["cost_basis"]
        await repo.mark_position(pos["id"], round(unrealized, 4))

        # Dust: por debajo del orderMinSize de Polymarket la venta es imposible
        # (el adapter la rechazaria cada ciclo, spameando warnings). Se deja la
        # posicion marcada a mercado y la liquidara la rama de resolucion.
        if pos["size"] < self.s.min_order_shares:
            return

        entry_mid = pos.get("entry_mid") or 0.0
        move = (mid - entry_mid) / entry_mid if entry_mid > 0 else 0.0
        liquidation_roi = unrealized / pos["cost_basis"] if pos["cost_basis"] else 0.0

        hours = market.hours_to_resolution() if market else None
        reason = None
        if entry_mid > 0 and move >= self.s.take_profit_pct:
            reason = f"take-profit (mercado {move:+.1%})"
        elif entry_mid > 0 and move <= -self.s.stop_loss_pct:
            reason = f"stop-loss (mercado {move:+.1%})"
        elif hours is not None and hours <= self.s.exit_min_hours_to_resolution:
            reason = f"cierre por proximidad a resolucion ({hours:.1f}h)"
        if reason is None:
            return

        self.log.info(
            "trading.exit_triggered", market=pos["market_id"], reason=reason,
            market_move=round(move, 4), liquidation_roi=round(liquidation_roi, 4),
        )

        # 3) Cerrar vendiendo contra el libro real (mismo camino en paper y live).
        req = OrderRequest(
            token_id=pos["token_id"], side="sell", shares=pos["size"],
            market_id=pos["market_id"], outcome=pos["outcome"],
        )
        result = await adapter.place_order(req)
        if result.status == "rejected":
            await self.publish_log(
                LogType.WARNING,
                f"No se pudo cerrar {pos['question'][:60]}: {result.reason}",
                {"market_id": pos["market_id"]},
            )
            return

        closed = await repo.record_exit(
            position_id=pos["id"], mode=adapter.mode, exit_price=result.avg_price,
            proceeds=result.cash_in, cost=result.total_cost, reason=reason,
            fills=result.fills, order_status=result.status,
            filled_size=result.filled_size,
            balance_delta=(result.cash_in if adapter.tracks_balance else 0.0),
        )
        if closed:
            await self._report_close(closed, adapter, reason)

    async def _report_close(self, closed: dict, adapter, reason: str) -> None:
        pnl = closed["pnl"]
        partial = closed.get("partial")
        label = "SALIDA PARCIAL" if partial else "SALIDA"
        rest = f" · quedan {closed['remaining']:.1f} shares" if partial else ""
        msg = (
            f"{label} {closed['outcome']} @ {closed['exit_price']:.3f} · "
            f"PnL ${pnl:+.2f} ({closed['roi']:+.1%}) · {reason}{rest} · "
            f"{closed['question'][:70]}"
        )
        await self.publish_log(LogType.TRADE, msg, {
            "market_id": closed["market_id"], "pnl": round(pnl, 4),
            "roi": round(closed["roi"], 4), "won": closed["won"],
        })
        await self.notifier.trade_exit(
            question=closed["question"], outcome=closed["outcome"],
            exit_price=closed["exit_price"], pnl=pnl, roi=closed["roi"],
            reason=reason, mode=adapter.mode,
        )
