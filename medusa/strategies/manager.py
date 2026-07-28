"""Strategy Manager: orquesta las estrategias sobre cada mercado.

Decide que estrategias evaluar segun el tipo de mercado (categoria via
applies_to) y el contexto disponible, y agrega sus señales.

CICLO DE VIDA (V3, 2026-07-16): cada estrategia vive en una fase
(medusa/strategies/phases.py):

  research       -> ni se evalua en el runtime (analisis historico)
  shadow         -> se evalua y se registra la señal, pero NUNCA opera (DEFAULT)
  paper          -> opera SOLO contra el motor Paper, y solo con peso > 0
  live_candidate -> unica que puede tocar dinero real (con todos los gates)

Ademas STRATEGIES_DISABLED apaga una estrategia del todo.

El default es todo en shadow: el sistema completo puede correr semanas
acumulando historial de señales resueltas sin arriesgar un dolar, que es
exactamente lo que el veredicto de calibracion exige antes de operar nada.

Compatibilidad V2: STRATEGIES_LIVE_ENABLED sigue funcionando y mapea a
live_candidate; is_live() se conserva como alias. El .env desplegado no
necesita cambios.
"""

from __future__ import annotations

from medusa.config import get_settings
from medusa.core.models import Market, MarketContext
from medusa.strategies.base import Strategy, StrategySignal
from medusa.logging_setup import err
from medusa.strategies.phases import (
    DEFAULT_PHASE,
    Phase,
    is_tradeable,
    parse_phases,
    runs_in_runtime,
)


def _csv(value: str) -> set[str]:
    return {item.strip() for item in value.split(",") if item.strip()}


class StrategyManager:
    def __init__(self, log, strategies: list[Strategy]) -> None:
        self.log = log
        self.s = get_settings()
        disabled = _csv(self.s.strategies_disabled)
        self.strategies: list[Strategy] = [
            st for st in strategies if st.name not in disabled
        ]
        self._live = _csv(self.s.strategies_live_enabled)
        self._phases = self._resolve_phases()
        if disabled:
            self.log.info("strategies.disabled", names=sorted(disabled))
        self.log.info(
            "strategies.loaded",
            phases={st.name: self.phase(st.name).value for st in self.strategies},
            tradeable_in_paper=[
                st.name for st in self.strategies if self.can_trade_in(st.name, "paper")
            ],
        )

    def _resolve_phases(self) -> dict[str, Phase]:
        """Fase efectiva por estrategia: STRATEGY_PHASES manda; si no,
        STRATEGIES_LIVE_ENABLED (V2) asciende a live_candidate; si no, shadow."""
        phases = parse_phases(self.s.strategy_phases)
        resolved: dict[str, Phase] = {}
        for st in self.strategies:
            if st.name in phases:
                resolved[st.name] = phases[st.name]
            elif st.name in self._live:
                resolved[st.name] = Phase.LIVE_CANDIDATE
            else:
                resolved[st.name] = DEFAULT_PHASE
        return resolved

    # --------------------------------------------------------------- fases --
    def phase(self, name: str) -> Phase:
        return self._phases.get(name, DEFAULT_PHASE)

    def in_runtime(self, name: str) -> bool:
        """¿Se evalua en el ciclo? Todo menos research."""
        return runs_in_runtime(self.phase(name))

    def can_trade_in(self, name: str, mode: str) -> bool:
        """¿Puede esta estrategia abrir posiciones en este modo?

        Combina la fase con la capacidad tecnica de la estrategia: stat_arb
        tiene can_trade=False (necesita dos patas simultaneas) y por tanto no
        opera jamas, este en la fase que este.
        """
        st = self.get(name)
        if st is None or not st.can_trade:
            return False
        return is_tradeable(self.phase(name), mode)

    # -------------------------------------------------------------- estados --
    def is_live(self, name: str) -> bool:
        """ALIAS V2: ¿es candidata a live? (el peso del asignador sigue aparte)."""
        return self.can_trade_in(name, "live")

    def get(self, name: str) -> Strategy | None:
        for st in self.strategies:
            if st.name == name:
                return st
        return None

    # ---------------------------------------------- necesidades de contexto --
    @property
    def needs_book_no(self) -> bool:
        return any(st.needs_book_no for st in self.strategies)

    @property
    def needs_history(self) -> bool:
        return any(st.needs_history for st in self.strategies)

    # ------------------------------------------------------------ evaluacion --
    def evaluate(self, m: Market, ctx: MarketContext) -> list[StrategySignal]:
        """Evalua todas las estrategias aplicables al mercado. Una estrategia
        rota no puede tumbar el ciclo ni tapar a las demas."""
        signals: list[StrategySignal] = []
        for st in self.strategies:
            if not self.in_runtime(st.name):
                continue   # research: solo analisis historico, fuera del ciclo
            if not st.applies_to(m):
                continue
            try:
                sig = st.evaluate(m, ctx)
            except Exception as exc:  # noqa: BLE001
                self.log.warning("strategy.failed", strategy=st.name,
                                 market=m.id, error=err(exc))
                continue
            if sig is not None:
                signals.append(sig)
        return signals

    # -------------------------------------------------------------- registro --
    def registry_info(self) -> list[dict]:
        """Descripcion del registro para la API/dashboard."""
        return [
            {
                "name": st.name,
                "categories": list(st.categories) if st.categories else "all",
                "can_trade": st.can_trade,
                "phase": self.phase(st.name).value,
                "trades_in_paper": self.can_trade_in(st.name, "paper"),
                "trades_in_live": self.can_trade_in(st.name, "live"),
                "live_enabled": self.is_live(st.name),   # alias V2 (compat)
                "needs_book_no": st.needs_book_no,
                "needs_history": st.needs_history,
                "doc": (st.__doc__ or "").strip().split("\n")[0],
            }
            for st in self.strategies
        ]
