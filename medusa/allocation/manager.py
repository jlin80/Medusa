"""Capital Allocation Manager: cuanto capital merece cada (estrategia, categoria).

La asignacion NO es fija: sale del rendimiento historico medido por Medusa
(señales resueltas contra el resultado real del mercado). El peso es un
multiplicador del sizing base del Risk Manager, acotado a
[0, ALLOC_MAX_WEIGHT]; los limites duros de riesgo (exposicion maxima, tamaño
maximo, nº de posiciones, kill-switch) NUNCA se relajan desde aqui: este modulo
solo reparte el capital que el Risk Manager ya permitia arriesgar.

Reglas, deliberadamente conservadoras:

  1. Sin muestra suficiente (n < ALLOC_MIN_SAMPLES) el peso es 0: una
     estrategia sin historial NO opera aunque este en STRATEGIES_LIVE_ENABLED.
     Primero se gana el derecho en shadow.
  2. Se usa el limite INFERIOR del ROI medio (media - z*std/sqrt(n), z=1.64,
     90% una cola), no la media: con pocas muestras la media es optimista por
     construccion (las estrategias que sobreviven a la seleccion son las que
     tuvieron suerte). Si el limite inferior no supera 0, peso 0.
  3. La estadistica por (estrategia, categoria) manda; si esa celda no tiene
     muestra, se cae al total de la estrategia. Igual que hace un analista: la
     evidencia especifica pesa mas que la general, pero la general es mejor
     que nada.

Mismo patron estadistico que el estudio de calibracion: intervalos
conservadores y desconfianza del punto estimado. Aritmetica pura de Python (sin
numpy: CPU del CT202).
"""

from __future__ import annotations

import math
import time

from medusa.config import get_settings
from medusa.data import repositories as repo

# 90% una cola. Constante y no setting: mover la z para "ver mas señal" es la
# forma mas rapida de auto-engañarse.
_Z = 1.64

# Cuanto ROI teorico por señal convierte peso: lb_roi de +5% -> peso 1.5 (con
# el techo por defecto). Lineal y sin misterio.
_ROI_TO_WEIGHT = 10.0


def wilson_lower(wins: int, n: int, z: float = _Z) -> float:
    """Limite inferior de Wilson para una proporcion (el de Wald colapsa en
    proporciones extremas; leccion aprendida del estudio de calibracion)."""
    if n <= 0:
        return 0.0
    phat = wins / n
    denom = 1 + z * z / n
    center = phat + z * z / (2 * n)
    margin = z * math.sqrt((phat * (1 - phat) + z * z / (4 * n)) / n)
    return max(0.0, (center - margin) / denom)


class AllocationManager:
    def __init__(self, log) -> None:
        self.log = log
        self.s = get_settings()
        self._cells: dict[tuple[str, str], dict] = {}   # (estrategia, categoria)
        self._totals: dict[str, dict] = {}              # estrategia (todas las cats)
        self._refreshed_at = 0.0

    # ------------------------------------------------------------- refresco --
    async def refresh(self, force: bool = False) -> None:
        """Recarga la estadistica desde la BD (throttled por config)."""
        now = time.time()
        if not force and now - self._refreshed_at < self.s.alloc_refresh_interval:
            return
        rows = await repo.strategy_performance()
        cells: dict[tuple[str, str], dict] = {}
        totals: dict[str, dict] = {}
        for r in rows:
            cells[(r["strategy"], r["category"])] = r
            t = totals.setdefault(r["strategy"], {
                "n": 0, "wins": 0, "_roi_sum": 0.0, "_var_sum": 0.0,
            })
            t["n"] += r["n"]
            t["wins"] += r["wins"]
            t["_roi_sum"] += r["avg_roi"] * r["n"]
            # Aproximacion de la varianza combinada (ignora el termino entre
            # grupos: conservador no es, pero el gate principal es la celda).
            t["_var_sum"] += (r["std_roi"] ** 2) * max(r["n"] - 1, 0)
        for name, t in totals.items():
            n = t["n"]
            t["avg_roi"] = t["_roi_sum"] / n if n else 0.0
            t["std_roi"] = math.sqrt(t["_var_sum"] / max(n - 1, 1)) if n > 1 else 0.0
        self._cells, self._totals = cells, totals
        self._refreshed_at = now
        self.log.info("alloc.refreshed", cells=len(cells), strategies=len(totals))

    # ---------------------------------------------------------------- pesos --
    def _lb_roi(self, stats: dict) -> float:
        n = stats["n"]
        if n <= 1:
            return -1.0
        return stats["avg_roi"] - _Z * (stats["std_roi"] / math.sqrt(n))

    def weight(self, strategy: str, category: str) -> float:
        """Multiplicador de sizing en [0, alloc_max_weight]. 0 = no operar."""
        stats = self._cells.get((strategy, category))
        if stats is None or stats["n"] < self.s.alloc_min_samples:
            stats = self._totals.get(strategy)
        if stats is None or stats["n"] < self.s.alloc_min_samples:
            return 0.0
        lb = self._lb_roi(stats)
        if lb <= 0.0:
            return 0.0
        return round(min(self.s.alloc_max_weight, 1.0 + lb * _ROI_TO_WEIGHT), 3)

    # ------------------------------------------------------------------ API --
    def to_dict(self) -> list[dict]:
        """Estado completo de la asignacion para la API/dashboard."""
        out = []
        for (strategy, category), stats in sorted(self._cells.items()):
            n, wins = stats["n"], stats["wins"]
            out.append({
                "strategy": strategy,
                "category": category,
                "n": n,
                "wins": wins,
                "win_rate": round(wins / n, 4) if n else 0.0,
                "wilson_lb_win_rate": round(wilson_lower(wins, n), 4),
                # avg_roi ES la cota TAKER (realista). El peso sale SIEMPRE de
                # ella: asignar capital por la cota maker seria financiar un
                # techo optimista que nadie puede capturar.
                "avg_roi": round(stats["avg_roi"], 4),
                "roi_lower_bound": round(self._lb_roi(stats), 4),
                # Contraste maker/taker: cuanto del ROI es techo y cuanto
                # sobrevive al spread. Solo informativo, jamas entra en el peso.
                "avg_roi_maker": stats.get("avg_roi_maker"),
                "spread_toll": stats.get("spread_toll"),
                "weight": self.weight(strategy, category),
                "min_samples": self.s.alloc_min_samples,
            })
        return out
