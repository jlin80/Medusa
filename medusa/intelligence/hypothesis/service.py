"""Servicio del HE: cosechar -> descubrir variables -> proponer -> evaluar -> persistir.

Unica pieza del paquete que habla con la BD y con el reloj. Todo lo que decide
algo (que variable es utilizable, que relacion merece proponerse, si una hipotesis
replica) vive en modulos puros (`features`, `generator`, `evaluator`, `stats`) y se
testea sin infraestructura.

CONTRATO DE AISLAMIENTO (identico al del MIG, al de Wallet Intelligence y al del
IFE, y aqui tambien es estructural, no una promesa):

  - No importa `medusa.execution`, `medusa.trading`, `medusa.risk` ni
    `medusa.strategies`. No tiene con que operar aunque quisiera.
  - No escribe una sola fila fuera de las tablas `hyp_*`.
  - Corre en su propio loop, con `wait_for(timeout)`, y jamas se le espera desde
    el ciclo de trading.
  - Apagado por defecto (`HYPOTHESIS_ENABLED=false`). Encenderlo no cambia una
    sola decision del bot: añade filas a cinco tablas nuevas y dos paginas al
    panel.

Y EL CONTRATO EPISTEMICO, que es el que de verdad define este paquete:

  1. NINGUNA HIPOTESIS ESTA ESCRITA EN EL CODIGO. Lo escrito es la gramatica y el
     lineage de las fuentes; el enunciado sale de los datos.
  2. UNA HIPOTESIS SE VALIDA CON DATOS QUE NO PUDO VER. El orden de esta funcion
     no es casual: primero se PROPONE con la ventana de descubrimiento y despues
     se EVALUA lo ya existente contra la ventana completa, porque las hipotesis
     que nacen en esta pasada tienen `created_at` = ahora y por tanto cero
     observaciones fuera de muestra. Nacen con `confidence` 0.0 y asi se guardan.
  3. Nada de lo que sale de aqui tiene lado, tamaño ni precio de entrada. Una
     hipotesis validada es una frase con un intervalo de confianza, no una orden.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import time

from medusa.config import get_settings
from medusa.intelligence.hypothesis import evaluator
from medusa.intelligence.hypothesis import features as feat
from medusa.intelligence.hypothesis import generator
from medusa.intelligence.hypothesis import repository as hyp_repo
from medusa.intelligence.hypothesis import sources
from medusa.intelligence.hypothesis.types import (
    FORMS,
    PROPOSED,
    REJECTED,
    TESTING,
    VALIDATED,
    Hypothesis,
    Observation,
)
from medusa.logging_setup import err

UTC = dt.timezone.utc


class HypothesisService:
    def __init__(self, log, publish_log=None) -> None:
        self.log = log
        self.s = get_settings()
        # Publicador de eventos opcional (el mismo del resto del engine). Si no
        # llega, el servicio funciona igual: no puede depender de el.
        self._publish = publish_log
        self.last_run: dict | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.s.hypothesis_enabled)

    def info(self) -> dict:
        """Descripcion del servicio para /hypotheses/info (no toca la BD)."""
        return {
            "enabled": self.enabled,
            "interval_seconds": self.s.hypothesis_interval,
            "discovery_days": self.s.hypothesis_discovery_days,
            "lookback_days": self.s.hypothesis_lookback_days,
            "max_rows_per_source": self.s.hypothesis_max_rows_per_source,
            "min_coverage": self.s.hypothesis_min_coverage,
            "min_distinct": self.s.hypothesis_min_distinct,
            "max_levels": self.s.hypothesis_max_levels,
            "min_level_size": self.s.hypothesis_min_level_size,
            "min_discovery_samples": self.s.hypothesis_min_discovery_samples,
            "min_effect_rho": self.s.hypothesis_min_effect_rho,
            "min_effect_d": self.s.hypothesis_min_effect_d,
            "alpha": self.s.hypothesis_alpha,
            "min_test_samples": self.s.hypothesis_min_test_samples,
            "reject_after": self.s.hypothesis_reject_after,
            "max_open_per_source": self.s.hypothesis_max_open_per_source,
            "retention_days": self.s.hypothesis_retention_days,
            "observation_retention_days": self.s.hypothesis_observation_retention_days,
            "sources": [spec.to_dict() for spec in sources.SPECS.values()],
            "forms": list(FORMS),
            # Los NO del paquete, en la respuesta de la API para que no haya que
            # leerse el codigo para saber que es esto.
            "hypotheses_hardcoded": False,
            "validates_in_sample": False,
            "measures_causality": False,
            "can_place_orders": False,
            "emits_signals": False,
            "last_run": self.last_run,
        }

    # ------------------------------------------------------------- cosecha --
    async def harvest(self) -> dict[str, list[Observation]]:
        """Lee las fuentes y las normaliza a observaciones."""
        return await sources.harvest(
            lookback_days=self.s.hypothesis_lookback_days,
            max_rows_per_source=self.s.hypothesis_max_rows_per_source,
        )

    # ------------------------------------------------------------ analisis --
    def discover(
        self, observations: list[Observation], source: str,
        *, now: dt.datetime, existing_ids: set[str], open_count: int = 0,
    ) -> generator.Proposal:
        """Variables -> gramatica -> candidatas NUEVAS de una fuente. PURO.

        Se descartan las candidatas cuyo `id` ya existe, incluidas las RECHAZADAS.
        Ese descarte es el candado contra el blanqueo de hipotesis: el generador
        redescubre lo mismo en cada pasada, y sin el una hipotesis rechazada
        volveria a entrar como `proposed` con la evidencia de hoy, hasta que
        alguna vez el azar la validase.
        """
        cutoff = now - dt.timedelta(days=self.s.hypothesis_discovery_days)
        window = [o for o in observations if o.ts >= cutoff]
        if len(window) < self.s.hypothesis_min_discovery_samples:
            return generator.Proposal(alpha=self.s.hypothesis_alpha)

        variables = feat.discover_variables(
            window,
            min_coverage=self.s.hypothesis_min_coverage,
            min_distinct=self.s.hypothesis_min_distinct,
            max_levels=self.s.hypothesis_max_levels,
            min_level_size=self.s.hypothesis_min_level_size,
        )
        spec = sources.SPECS.get(source)
        proposal = generator.propose(
            window, variables, source=source,
            blocked_pairs=spec.blocked_pairs if spec else (),
            min_samples=self.s.hypothesis_min_discovery_samples,
            min_effect_rho=self.s.hypothesis_min_effect_rho,
            min_effect_d=self.s.hypothesis_min_effect_d,
            alpha=self.s.hypothesis_alpha,
            cut_quantile=self.s.hypothesis_cut_quantile,
            max_proposals=self.s.hypothesis_max_proposals_per_source,
            now=now,
        )
        proposal.hypotheses = [
            h for h in proposal.hypotheses if h.id not in existing_ids]
        # Tope de hipotesis abiertas por fuente. Sin el, el motor propondria en
        # cada pasada y acabaria con miles de enunciados en `proposed` que nadie
        # va a leer y que solo diluyen el tablero.
        room = max(0, self.s.hypothesis_max_open_per_source - open_count)
        proposal.hypotheses = proposal.hypotheses[:room]
        proposal.variables = len(variables)
        return proposal

    def evaluate_all(
        self, hypotheses: list[Hypothesis],
        by_source: dict[str, list[Observation]], *, now: dt.datetime,
    ) -> tuple[list[Hypothesis], list[dict]]:
        """Reevalua las hipotesis vivas y devuelve (actualizadas, transiciones)."""
        updated: list[Hypothesis] = []
        changes: list[dict] = []
        for h in hypotheses:
            after = evaluator.evaluate(
                h, by_source.get(h.source, []),
                min_test_samples=self.s.hypothesis_min_test_samples,
                min_effect_rho=self.s.hypothesis_min_effect_rho,
                min_effect_d=self.s.hypothesis_min_effect_d,
                reject_after=self.s.hypothesis_reject_after,
                confidence_min_samples=self.s.hypothesis_min_test_samples,
                now=now,
            )
            updated.append(after)
            if after.status != h.status:
                changes.append({
                    "id": after.id, "from": h.status, "to": after.status,
                    "sample_count": after.sample_count,
                    "confidence": after.confidence,
                    "reason": after.status_reason,
                })
        return updated, changes

    # -------------------------------------------------------------- pasada --
    async def run(self, persist: bool = True) -> dict:
        """Pasada completa. Devuelve el resumen.

        No captura excepciones a proposito: quien la llama decide que hacer con el
        fallo (el loop del engine la aisla; la API la traduce a un 5xx). Un
        try/except aqui haria que una pasada a medias pareciera un exito.

        Con `persist=False` no escribe NADA y analiza solo lo que acaba de
        cosechar. Es una vista previa util, pero hay que leerla sabiendo que sus
        hipotesis salen con `created_at` = ahora y por tanto con cero evidencia:
        ninguna vista previa puede validar nada, por construccion.
        """
        started = time.time()
        now = dt.datetime.now(UTC)

        fresh = await self.harvest()
        new_observations = 0
        if persist:
            flat = [o for batch in fresh.values() for o in batch]
            new_observations = await hyp_repo.save_observations(flat)

        # La ventana de ANALISIS sale de la BD, no de la cosecha: la cosecha trae
        # lo que las fuentes puedan dar hoy, y la valla temporal necesita el
        # historico completo para poder distinguir "antes" de "despues".
        since = now - dt.timedelta(days=self.s.hypothesis_lookback_days)
        by_source: dict[str, list[Observation]] = {}
        for name in sources.SPECS:
            by_source[name] = (
                await hyp_repo.load_observations(
                    name, since=since, limit=self.s.hypothesis_max_observations)
                if persist else fresh.get(name, [])
            )

        existing_ids = await hyp_repo.known_ids() if persist else set()
        live = await hyp_repo.load_hypotheses() if persist else []
        open_per_source: dict[str, int] = {}
        for h in live:
            if h.status != REJECTED:
                open_per_source[h.source] = open_per_source.get(h.source, 0) + 1

        # --- 1. proponer (dentro de muestra: NO es evidencia) ---
        proposals: dict[str, generator.Proposal] = {}
        born: list[Hypothesis] = []
        for name, observations in by_source.items():
            proposal = self.discover(
                observations, name, now=now, existing_ids=existing_ids,
                open_count=open_per_source.get(name, 0))
            proposals[name] = proposal
            born.extend(proposal.hypotheses)

        # --- 2. evaluar lo que ya existia (fuera de muestra: la unica evidencia) ---
        updated, changes = self.evaluate_all(live, by_source, now=now)

        tested = sum(p.tested for p in proposals.values())
        variables = sum(getattr(p, "variables", 0) for p in proposals.values())
        everything = updated + born
        confidences = [h.confidence for h in everything
                       if h.status in (TESTING, VALIDATED)]
        board = {
            "proposed": sum(1 for h in everything if h.status == PROPOSED),
            "testing": sum(1 for h in everything if h.status == TESTING),
            "validated": sum(1 for h in everything if h.status == VALIDATED),
            "rejected": sum(1 for h in everything if h.status == REJECTED),
        }

        summary = {
            "ok": True,
            "persisted": persist,
            "build_seconds": round(time.time() - started, 3),
            "observations": sum(len(v) for v in by_source.values()),
            "new_observations": new_observations,
            "variables": variables,
            "tested": tested,
            "new_proposals": len(born),
            "transitions": len(changes),
            "avg_confidence": (
                round(sum(confidences) / len(confidences), 4) if confidences else 0.0),
            "board": board,
            "by_source": {n: p.to_dict() for n, p in proposals.items()},
            "changes": changes,
            "born": [h.to_dict() for h in born[:10]],
        }

        if persist:
            written = 0
            if everything:
                written = await hyp_repo.upsert_hypotheses(everything)
            await hyp_repo.save_evidence(updated, ts=now)
            await hyp_repo.save_transitions(changes, ts=now)
            await hyp_repo.save_snapshot(summary, build_seconds=summary["build_seconds"])
            summary["written"] = written
            # El tablero real sale de la BD, no de la suma en memoria: si otra
            # pasada (la de la API, p. ej.) escribio entretanto, el numero de la
            # BD es el correcto y el de memoria seria una foto vieja.
            summary["board"] = await hyp_repo.board()

        self.last_run = {
            "build_seconds": summary["build_seconds"],
            "observations": summary["observations"],
            "tested": tested,
            "new_proposals": len(born),
            "transitions": len(changes),
            "validated": summary["board"].get("validated", 0),
        }
        self.log.info("hypothesis.run_done", **self.last_run)
        return summary

    async def run_guarded(self) -> dict | None:
        """`run()` con timeout y sin propagar errores: la variante del loop del
        engine. Si el motor falla o se pasa de tiempo, se registra y el resto del
        sistema sigue exactamente igual."""
        try:
            return await asyncio.wait_for(
                self.run(), timeout=self.s.hypothesis_timeout)
        except asyncio.TimeoutError:
            self.log.warning("hypothesis.run_timeout",
                             timeout=self.s.hypothesis_timeout)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - el HE jamas tumba el engine
            self.log.error("hypothesis.run_failed", error=err(exc), exc_info=True)
        return None

    async def prune(self) -> dict:
        """Poda snapshots y observaciones viejas. Las hipotesis NUNCA se podan:
        una rechazada de hace un año es justo lo que evita volver a proponerla."""
        return await hyp_repo.prune(
            self.s.hypothesis_retention_days,
            self.s.hypothesis_observation_retention_days)

    async def close(self) -> None:
        """Simetria con los otros servicios del engine. El HE no abre clientes de
        red -- lee la BD y nada mas -- asi que no hay nada que cerrar."""
        return None
