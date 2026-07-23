"""Runner del Intelligence Layer: orquesta los modulos FUERA del ciclo de trading.

Tres garantias, que son el motivo de que este modulo exista:

  1. NO BLOQUEA. Corre en su propio task de asyncio. El ciclo de trading no lo
     espera nunca, ni siquiera un segundo. Si el layer entero se cuelga, Medusa
     sigue escaneando, valorando y gestionando salidas como si no existiera.
  2. AISLA FALLOS. Cada modulo va en su propio try/except + wait_for(timeout).
     Un modulo que revienta o que se queda colgado contra una API externa no
     puede tumbar a los demas ni al engine: se registra el error y se sigue.
  3. ES OPCIONAL. Con INTELLIGENCE_ENABLED=false (default) el layer ni arranca.
     Cada modulo se habilita ademas por nombre en INTELLIGENCE_MODULES.

El resultado va SIEMPRE al Feature Store. El runner no devuelve nada al camino
de decision: quien quiera las features las lee del store.
"""

from __future__ import annotations

import asyncio
import time

from medusa.config import get_settings
from medusa.core.enums import LogType
from medusa.data import repositories as repo
from medusa.intelligence_layer.base import IntelligenceModule


def _csv(value: str) -> set[str]:
    return {item.strip() for item in value.split(",") if item.strip()}


class IntelligenceRunner:
    def __init__(self, log, modules: list[IntelligenceModule], publish_log=None) -> None:
        self.log = log
        self.s = get_settings()
        self.publish_log = publish_log
        enabled = _csv(self.s.intelligence_modules)
        # Lista blanca explicita: un modulo solo corre si se le nombra. Nada de
        # "activo por defecto" en una capa que habla con el exterior.
        self.modules = [m for m in modules if m.name in enabled]
        self.all_modules = modules
        self._last_run: dict[str, float] = {}
        self._stats: dict[str, dict] = {
            m.name: {"runs": 0, "features": 0, "errors": 0, "last_error": "",
                     "last_run": None, "last_ms": 0.0}
            for m in modules
        }
        if self.s.intelligence_enabled:
            self.log.info(
                "intel.loaded",
                enabled=[m.name for m in self.modules],
                available=[m.name for m in modules],
            )

    @property
    def enabled(self) -> bool:
        return bool(self.s.intelligence_enabled and self.modules)

    def due(self, module: IntelligenceModule, now: float) -> bool:
        last = self._last_run.get(module.name, 0.0)
        return now - last >= module.interval

    async def run_once(self, markets: list, ctx: dict) -> int:
        """Ejecuta los modulos que toquen. Devuelve nº de features escritas.

        Nunca lanza: cualquier fallo queda dentro. Es el contrato del layer.
        """
        if not self.enabled or not markets:
            return 0

        now = time.time()
        written = 0
        for module in self.modules:
            if not self.due(module, now):
                continue
            self._last_run[module.name] = now
            stats = self._stats[module.name]
            started = time.perf_counter()
            try:
                features = await asyncio.wait_for(
                    module.compute(markets, ctx), timeout=module.timeout
                )
            except asyncio.TimeoutError:
                stats["errors"] += 1
                stats["last_error"] = f"timeout tras {module.timeout}s"
                self.log.warning("intel.module_timeout", module=module.name,
                                 timeout=module.timeout)
                continue
            except asyncio.CancelledError:
                raise   # apagado ordenado: esto SI debe propagarse
            except Exception as exc:  # noqa: BLE001 - un modulo roto no rompe nada mas
                stats["errors"] += 1
                stats["last_error"] = str(exc)[:200]
                self.log.warning("intel.module_failed", module=module.name,
                                 error=str(exc), exc_info=True)
                continue

            stats["last_ms"] = round((time.perf_counter() - started) * 1000, 1)
            stats["runs"] += 1
            stats["last_run"] = now

            if not features:
                continue
            try:
                written += await repo.save_features(features)
                stats["features"] += len(features)
            except Exception as exc:  # noqa: BLE001
                stats["errors"] += 1
                stats["last_error"] = f"no se pudo guardar: {str(exc)[:150]}"
                self.log.warning("intel.save_failed", module=module.name, error=str(exc))

        if written and self.publish_log is not None:
            try:
                await self.publish_log(
                    LogType.INFO, f"Intelligence Layer: {written} features calculadas",
                    {"features": written},
                )
            except Exception:  # noqa: BLE001 - loguear jamas rompe al que loguea
                pass
        return written

    def info(self) -> list[dict]:
        """Estado de cada modulo para /intelligence/modules."""
        enabled_names = {m.name for m in self.modules}
        return [
            {
                "name": m.name,
                "enabled": m.name in enabled_names,
                "interval_seconds": m.interval,
                "timeout_seconds": m.timeout,
                "needs_network": m.needs_network,
                "description": m.description or (m.__doc__ or "").strip().split("\n")[0],
                **self._stats.get(m.name, {}),
            }
            for m in self.all_modules
        ]
