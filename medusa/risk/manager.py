"""Risk Manager: valida oportunidades y dimensiona posiciones.

Controla: edge minimo, confianza, liquidez, exposicion total, numero maximo de
posiciones, un mercado a la vez, y kill-switch por drawdown.
"""

from __future__ import annotations

from dataclasses import dataclass

from medusa.config import get_settings
from medusa.core.models import Opportunity


@dataclass
class TradeInstruction:
    market_id: str
    question: str
    outcome: str          # YES | NO
    token_id: str
    usd_size: float       # tamaño objetivo en USDC
    ref_price: float
    edge: float
    confidence: float
    opportunity_id: int | None = None
    strategy: str = ""    # estrategia que origino la operacion (atribucion)


class RiskManager:
    def __init__(self, log) -> None:
        self.log = log
        self.s = get_settings()

    def approve(
        self,
        opportunities: list[Opportunity],
        balance: float,
        open_positions: list[dict],
        allocator=None,
    ) -> list[TradeInstruction]:
        """Valida y dimensiona. `allocator` (AllocationManager) escala el
        sizing por el rendimiento historico de (estrategia, categoria); un
        peso 0 significa "sin historial que lo justifique" y descarta la
        operacion. Los limites duros de este modulo NUNCA se relajan por
        peso alto: el asignador reparte, el riesgo manda."""
        s = self.s
        instructions: list[TradeInstruction] = []
        held = {p["market_id"] for p in open_positions}
        exposure = sum(p.get("cost_basis", 0.0) for p in open_positions)
        n_open = len(open_positions)

        candidates = [o for o in opportunities if o.action in ("buy_yes", "buy_no")]
        candidates.sort(key=lambda o: o.edge * o.confidence, reverse=True)

        for o in candidates:
            if n_open >= s.max_open_positions:
                self.log.debug("risk.max_positions", n_open=n_open)
                break
            if o.market_id in held:
                continue
            if o.edge < s.min_edge or o.confidence < s.min_confidence:
                continue
            if o.liquidity < s.min_trade_liquidity:
                continue
            if not self._edge_beats_cost(o):
                continue

            weight = 1.0
            if allocator is not None and o.strategy:
                weight = allocator.weight(o.strategy, o.category)
                if weight <= 0.0:
                    self.log.debug("risk.no_allocation", strategy=o.strategy,
                                   category=o.category, market=o.market_id)
                    continue

            size = min(s.max_position_usd, balance * s.position_fraction * weight)
            if size < 1.0:
                continue
            if size > balance:
                continue
            if exposure + size > s.max_exposure_pct * balance:
                self.log.debug("risk.exposure_cap", exposure=exposure, size=size)
                continue

            instructions.append(TradeInstruction(
                market_id=o.market_id, question=o.question, outcome=o.outcome,
                token_id=o.token_id, usd_size=round(size, 2), ref_price=o.market_price,
                edge=o.edge, confidence=o.confidence, strategy=o.strategy,
            ))
            held.add(o.market_id)
            exposure += size
            n_open += 1

        return instructions

    def _edge_beats_cost(self, o: Opportunity) -> bool:
        """Rechaza la operacion si operarla cuesta mas que el edge que promete.

        Comprar significa pagar el ask y valorar/vender contra el bid: el spread
        se paga entero. Precios y probabilidades estan en la misma unidad (0..1),
        asi que el edge se puede comparar directamente contra el coste por share.

        Sin este filtro el bot entra en mercados de precio extremo cuyo spread
        relativo es enorme (medido: hasta 27%), donde el spread se traga
        cualquier edge plausible antes de que el mercado se mueva.
        """
        s = self.s
        slippage = o.market_price * (s.extra_slippage_bps / 10_000.0) * 2  # ida y vuelta
        fees = (s.fee_bps / 10_000.0) * min(o.market_price, 1 - o.market_price) * 2
        cost = o.spread + slippage + fees

        if o.edge <= cost * s.edge_cost_ratio:
            self.log.debug(
                "risk.edge_below_cost", market=o.market_id,
                edge=round(o.edge, 4), cost=round(cost, 4),
            )
            return False
        return True

    def daily_loss_exceeded(self, equity: float, day_start_equity: float) -> bool:
        """True si la perdida DEL DIA supera el limite (corta la operativa de hoy).

        Se mide contra la equity con la que empezo el dia, no contra el balance
        inicial de todo el run: es un cortafuegos para un mal dia, no un final de
        partida. Medido contra el inicial, un drawdown del 10% detendria el bot
        para siempre y una validacion desatendida de 7-14 dias moriria a mitad.
        """
        if day_start_equity <= 0:
            return False
        loss = (day_start_equity - equity) / day_start_equity
        return loss >= self.s.max_daily_loss_pct
