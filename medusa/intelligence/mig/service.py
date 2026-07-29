"""Servicio del MIG: orquesta leer -> construir -> persistir -> descubrir.

Es la unica pieza del paquete que habla con la base de datos y con el reloj.
Todo lo que decide algo (que nodo, que arista, que patron) vive en `builder.py`
y `discoveries.py`, que son puros y se testean sin infraestructura.

CONTRATO DE AISLAMIENTO (lo mismo que exige el Intelligence Layer V3, y aqui es
estructural, no una promesa):

  - No importa `medusa.execution`, `medusa.trading`, `medusa.risk` ni
    `medusa.strategies`. No tiene con que operar aunque quisiera.
  - No escribe una sola fila fuera de las tablas `mig_*`.
  - Corre en su propio loop, con `wait_for(timeout)`, y jamas se le espera desde
    el ciclo de trading.
  - Apagado por defecto (`MIG_ENABLED=false`). Encenderlo no cambia una sola
    decision del bot: añade filas a cuatro tablas nuevas y una pagina al
    dashboard.
"""

from __future__ import annotations

import asyncio
import time

from medusa.config import get_settings
from medusa.intelligence.mig import repository as mig_repo
from medusa.intelligence.mig.builder import build_from_sources
from medusa.intelligence.mig.discoveries import extract_discoveries
from medusa.intelligence.mig.graph import IntelligenceGraph
from medusa.intelligence.mig.types import Discovery


class MIGService:
    """Constructor del grafo. Una instancia por proceso; sin estado compartido
    mas alla de la telemetria de la ultima construccion."""

    def __init__(self, log, publish_log=None) -> None:
        self.log = log
        self.s = get_settings()
        # Publicador de eventos opcional (el mismo que usa el resto del engine).
        # Si no llega, el servicio funciona igual: no puede depender de el.
        self._publish = publish_log
        self.last_build: dict | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.s.mig_enabled)

    def info(self) -> dict:
        """Descripcion del servicio para /mig/info (no toca la BD)."""
        return {
            "enabled": self.enabled,
            "interval_seconds": self.s.mig_interval,
            "min_samples": self.s.mig_min_samples,
            "min_similarity": self.s.mig_min_similarity,
            "max_markets": self.s.mig_max_markets,
            "retention_days": self.s.mig_retention_days,
            "backend": "postgresql",
            "last_build": self.last_build,
        }

    # ---------------------------------------------------------- construccion --
    def build_in_memory(self, sources: dict) -> IntelligenceGraph:
        """Construye el grafo sin tocar la BD. Util para tests y para /mig/preview."""
        return build_from_sources(
            sources,
            min_samples=self.s.mig_min_samples,
            min_similarity=self.s.mig_min_similarity,
            max_markets=self.s.mig_max_markets,
        )

    def discoveries_of(self, graph: IntelligenceGraph) -> list[Discovery]:
        return extract_discoveries(
            graph, min_samples=self.s.mig_min_samples,
            min_observations=self.s.mig_min_observations,
        )

    async def build(self, persist: bool = True) -> dict:
        """Ciclo completo. Devuelve el resumen de la construccion.

        No captura excepciones a proposito: quien la llama decide que hacer con
        el fallo (el loop del engine la aisla; la API la traduce a un 5xx). Un
        try/except aqui haria que un grafo a medias pareciera un exito.
        """
        started = time.time()
        sources = await mig_repo.load_sources(
            max_markets=self.s.mig_max_markets,
            max_signals=self.s.mig_max_signals,
            max_trades=self.s.mig_max_trades,
            max_features=self.s.mig_max_features,
        )
        graph = self.build_in_memory(sources)
        discoveries = self.discoveries_of(graph)
        stats = graph.stats()
        elapsed = time.time() - started

        written = {"nodes": 0, "edges": 0, "discoveries": 0}
        if persist:
            written["nodes"] = await mig_repo.upsert_nodes(graph.nodes)
            written["edges"] = await mig_repo.upsert_edges(graph.edges)
            written["discoveries"] = await mig_repo.save_discoveries(discoveries)
            await mig_repo.save_snapshot(
                stats, discoveries=len(discoveries), build_seconds=elapsed,
                sources={k: len(v) for k, v in sources.items()},
            )

        summary = {
            "ok": True,
            "persisted": persist,
            "build_seconds": round(elapsed, 3),
            "sources": {k: len(v) for k, v in sources.items()},
            "stats": stats,
            "written": written,
            "discoveries": len(discoveries),
            "top_discoveries": [d.to_dict() for d in discoveries[:5]],
        }
        self.last_build = {
            "build_seconds": summary["build_seconds"],
            "nodes": stats["nodes"], "edges": stats["edges"],
            "discoveries": len(discoveries),
        }
        self.log.info(
            "mig.built", nodes=stats["nodes"], edges=stats["edges"],
            discoveries=len(discoveries), seconds=summary["build_seconds"],
        )
        return summary

    async def build_guarded(self) -> dict | None:
        """`build()` con timeout y sin propagar errores: la variante que usa el
        loop del engine. Si el MIG falla o se pasa de tiempo, se registra y el
        resto del sistema sigue exactamente igual."""
        from medusa.logging_setup import err

        try:
            return await asyncio.wait_for(self.build(), timeout=self.s.mig_timeout)
        except asyncio.TimeoutError:
            self.log.warning("mig.build_timeout", timeout=self.s.mig_timeout)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - el MIG jamas tumba el engine
            self.log.error("mig.build_failed", error=err(exc), exc_info=True)
        return None

    async def prune(self) -> dict:
        """Poda de descubrimientos y snapshots viejos (nodos y aristas nunca)."""
        return await mig_repo.prune(self.s.mig_retention_days)
