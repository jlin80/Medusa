"""Market Opportunity Pre-Scorer: puntaje preliminar 0-100 por mercado.

Su unico trabajo es decidir QUE mercados merecen analisis profundo. Con cientos
de mercados activos no se puede pedir el order book de todos en cada ciclo
(coste de red y de API); este modulo puntua usando SOLO los datos que ya vienen
de la Gamma API y el ranking decide quien pasa al analisis completo (order
books + estrategias).

El puntaje NO es una prediccion: mide cuanto vale la pena MIRAR un mercado
(operable, liquido, barato de ejecutar, con actividad e informacion fluyendo),
no hacia donde va el precio. Esa es la parte de las estrategias.

Componentes (cada uno normalizado a 0..1) y por que:
  liquidity  - sin liquidez no hay fill; escala log porque la diferencia entre
               $500 y $5k importa mucho mas que entre $50k y $60k.
  volume     - volumen 24h, misma escala log: mercados donde de verdad se opera.
  spread     - spread relativo del libro (Gamma trae bestBid/bestAsk): el
               spread es el primer coste que cualquier edge debe batir.
  exec_cost  - coste round-trip estimado (spread + slippage + fees) contra un
               edge de referencia: mercados caros de operar puntuan bajo aunque
               sean liquidos (medido: spreads relativos de hasta 27%).
  time       - tiempo a resolucion: ni a punto de resolver (sin recorrido, y
               el colapso final ya esta puesto en precio) ni a meses vista
               (capital muerto).
  activity   - rotacion (volumen/liquidez): libros que se mueven, señal de
               flujo real y no solo de un market maker aparcado.
  move       - variacion reciente del precio: donde hay informacion nueva
               incorporandose hay mas probabilidad de dislocacion explotable.

Los pesos son fijos y suman 1. Deliberadamente NO son configurables por .env:
siete knobs mas serian siete formas de sobreajustar a mano; si algun dia hay
datos de que otro peso funciona mejor, se cambia aqui con su justificacion.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from medusa.config import get_settings
from medusa.core.models import Market

# (peso, nombre) de cada componente; deben sumar 1.0.
_WEIGHTS: dict[str, float] = {
    "liquidity": 0.16,
    "volume": 0.16,
    "spread": 0.14,
    "exec_cost": 0.14,
    "time": 0.12,
    "activity": 0.14,
    "move": 0.14,
}

# Referencias de escala (log) para liquidez/volumen: por debajo del minimo
# puntua 0, por encima del techo puntua 1.
_LIQ_FLOOR, _LIQ_CEIL = 500.0, 100_000.0
_VOL_FLOOR, _VOL_CEIL = 1_000.0, 500_000.0

# Spread relativo que consideramos "inoperable" (score 0). 8% es generoso:
# lo medido en mercados de precio extremo llega al 27%.
_MAX_REL_SPREAD = 0.08

# Edge de referencia contra el que se compara el coste de ejecucion. Un coste
# round-trip igual o mayor que esto deja el mercado sin margen operable.
_REF_EDGE = 0.05

# Variacion 24h que satura el componente de movimiento.
_MAX_MOVE = 0.15

# Rotacion (volumen 24h / liquidez) que satura el componente de actividad.
_MAX_TURNOVER = 2.0


@dataclass
class ScoredMarket:
    """Mercado + puntaje + desglose, para ranking y explicabilidad."""

    market: Market
    score: float                       # 0..100
    components: dict[str, float] = field(default_factory=dict)


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _log_scale(value: float, floor: float, ceil: float) -> float:
    if value <= floor:
        return 0.0
    return _clamp01(math.log10(value / floor) / math.log10(ceil / floor))


class OpportunityPreScorer:
    def __init__(self, log=None) -> None:
        self.log = log
        self.s = get_settings()

    # ------------------------------------------------------------- filtros ---
    def is_scannable(self, m: Market) -> bool:
        """Filtro duro de cordura: lo que no pasa esto no se puntua siquiera.

        Son las mismas condiciones minimas del scanner original: mercado vivo,
        precio no degenerado y ventana de resolucion operable.
        """
        if not m.active or not m.yes_token_id:
            return False
        if not (0.02 < m.yes_price < 0.98):
            return False
        if m.liquidity < self.s.min_liquidity:
            return False
        if m.volume_24h < self.s.min_volume_24h:
            return False
        hours = m.hours_to_resolution()
        if hours is not None and not (
            self.s.min_hours_to_resolution <= hours <= self.s.max_hours_to_resolution
        ):
            return False
        return True

    # -------------------------------------------------------------- puntaje --
    def score(self, m: Market) -> ScoredMarket:
        components = {
            "liquidity": _log_scale(m.liquidity, _LIQ_FLOOR, _LIQ_CEIL),
            "volume": _log_scale(m.volume_24h, _VOL_FLOOR, _VOL_CEIL),
            "spread": self._spread_score(m),
            "exec_cost": self._exec_cost_score(m),
            "time": self._time_score(m),
            "activity": self._activity_score(m),
            "move": _clamp01(abs(m.price_change_24h) / _MAX_MOVE),
        }
        total = 100.0 * sum(_WEIGHTS[k] * v for k, v in components.items())
        return ScoredMarket(
            market=m,
            score=round(total, 2),
            components={k: round(v, 3) for k, v in components.items()},
        )

    def rank(self, markets: list[Market]) -> list[ScoredMarket]:
        """Puntua lo escaneable y devuelve el ranking descendente."""
        scored = [self.score(m) for m in markets if self.is_scannable(m)]
        scored.sort(key=lambda sm: sm.score, reverse=True)
        return scored

    # ---------------------------------------------------------- componentes --
    def _rel_spread(self, m: Market) -> float | None:
        """Spread relativo al mid. None si Gamma no trajo el libro."""
        if m.best_bid <= 0 or m.best_ask <= 0 or m.best_ask <= m.best_bid:
            return None
        mid = (m.best_bid + m.best_ask) / 2
        return (m.best_ask - m.best_bid) / mid if mid > 0 else None

    def _spread_score(self, m: Market) -> float:
        rel = self._rel_spread(m)
        if rel is None:
            return 0.5   # sin dato: neutral, que decidan los demas componentes
        return 1.0 - _clamp01(rel / _MAX_REL_SPREAD)

    def _exec_cost_score(self, m: Market) -> float:
        """Coste round-trip estimado vs el edge de referencia.

        Misma forma que el filtro _edge_beats_cost del Risk Manager: spread
        entero + slippage ida y vuelta + fees. Aqui es una estimacion barata
        (sin libro completo) para rankear; el filtro exacto sigue en riesgo.
        """
        rel = self._rel_spread(m)
        spread = (m.best_ask - m.best_bid) if rel is not None else 0.02  # estimado
        slippage = m.yes_price * (self.s.extra_slippage_bps / 10_000.0) * 2
        fees = (self.s.fee_bps / 10_000.0) * min(m.yes_price, 1 - m.yes_price) * 2
        cost = spread + slippage + fees
        return 1.0 - _clamp01(cost / _REF_EDGE)

    def _time_score(self, m: Market) -> float:
        """Plateau entre 12h y 14 dias; decae fuera.

        Muy cerca de resolver no queda recorrido (y la salida forzada por
        exit_min_hours esta al lado); a meses vista el capital queda muerto y
        la informacion aun no fluye.
        """
        hours = m.hours_to_resolution()
        if hours is None:
            return 0.3   # sin fecha: raro, penalizar sin excluir
        if hours < self.s.min_hours_to_resolution:
            return 0.0
        if hours < 12.0:
            return _clamp01((hours - self.s.min_hours_to_resolution)
                            / max(12.0 - self.s.min_hours_to_resolution, 0.1))
        if hours <= 14 * 24.0:
            return 1.0
        # Decae linealmente hasta el maximo configurado.
        max_h = self.s.max_hours_to_resolution
        if hours >= max_h:
            return 0.0
        return _clamp01((max_h - hours) / (max_h - 14 * 24.0))

    def _activity_score(self, m: Market) -> float:
        if m.liquidity <= 0:
            return 0.0
        return _clamp01((m.volume_24h / m.liquidity) / _MAX_TURNOVER)
