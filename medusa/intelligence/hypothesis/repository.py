"""Repositorio del HE: persistencia en PostgreSQL.

Sigue la convencion del resto de Medusa (`medusa/data/repositories.py`): cada
funcion abre su propia sesion async, hace commit y devuelve dicts o dataclasses,
nunca objetos ORM.

Este modulo es de SOLO ESCRITURA sobre tablas `hyp_*` y de SOLO LECTURA sobre
todo lo demas. Hay un test (`tests/test_hypothesis_isolation.py`) que lo verifica
sobre el codigo fuente, para que deje de ser una promesa y sea una comprobacion.

UNA COSA IMPORTANTE SOBRE LAS ESCRITURAS DE `hyp_observations`: son
`on conflict do nothing`, jamas `do update`. Una observacion es un hecho pasado
con una fecha, y esa fecha es la que la valla temporal compara contra
`created_at`. Si una segunda cosecha pudiera reescribir el `ts` de una
observacion, una hipotesis podria "adelantarse" a datos que ya conocia sin que
nada quedase registrado. La inmutabilidad de esta tabla ES la integridad de la
valla.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

from sqlalchemy import delete, desc, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from medusa.infra.db import get_sessionmaker
from medusa.intelligence.hypothesis import sources
from medusa.intelligence.hypothesis.models import (
    HypEvidenceRow,
    HypObservationRow,
    HypothesisRow,
    HypSnapshotRow,
    HypTransitionRow,
)
from medusa.intelligence.hypothesis.types import (
    PROPOSED,
    REJECTED,
    STATUSES,
    TESTING,
    VALIDATED,
    EffectEstimate,
    Hypothesis,
    Observation,
)

UTC = dt.timezone.utc

# Tamaño de lote de los upserts. Una cosecha puede traer decenas de miles de
# observaciones; mandarlas en una sola sentencia hincha la memoria del proceso y
# el log de Postgres en una maquina de 3 GB.
_CHUNK = 500


def _utcnow() -> dt.datetime:
    return dt.datetime.now(UTC)


def _json(data: Any) -> str:
    if not data:
        return ""
    try:
        return json.dumps(data, default=str)
    except (TypeError, ValueError):
        return ""


def _loads(raw: str) -> dict:
    if not raw:
        return {}
    try:
        out = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return out if isinstance(out, dict) else {}


def _chunks(items: list, size: int = _CHUNK):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _aware(value: dt.datetime | None) -> dt.datetime | None:
    """Fuerza tz-aware en UTC.

    Postgres devuelve `timestamptz` con zona, pero una BD creada antes de que la
    columna llevara `timezone=True` puede devolver naive, y comparar naive con
    aware lanza TypeError justo en la comparacion de la valla temporal -- el peor
    sitio posible para un fallo silencioso.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


# ------------------------------------------------------------ conversiones ----
def _row_to_observation(row: HypObservationRow) -> Observation:
    return Observation(
        source=row.source, entity=row.entity, ts=_aware(row.ts),
        features={k: float(v) for k, v in _loads(row.features).items()
                  if isinstance(v, (int, float))},
        labels={k: str(v) for k, v in _loads(row.labels).items()},
        outcomes={k: float(v) for k, v in _loads(row.outcomes).items()
                  if isinstance(v, (int, float))},
    )


def _row_to_hypothesis(row: HypothesisRow) -> Hypothesis:
    return Hypothesis(
        id=row.id, description=row.description, status=row.status,
        confidence=float(row.confidence), sample_count=int(row.sample_count),
        created_at=_aware(row.created_at), updated_at=_aware(row.updated_at),
        form=row.form, source=row.source, predictor=row.predictor,
        outcome=row.outcome, level=row.level or "", direction=int(row.direction),
        params=_loads(row.params),
        discovery=EffectEstimate(
            n=int(row.discovery_n), effect=float(row.discovery_effect),
            lower=float(row.discovery_lower), upper=float(row.discovery_upper),
            p_value=float(row.discovery_p)),
        tested_in_pass=int(row.tested_in_pass),
        test=EffectEstimate(
            n=int(row.test_n), effect=float(row.test_effect),
            lower=float(row.test_lower), upper=float(row.test_upper),
            p_value=float(row.test_p)),
        status_reason=row.status_reason or "",
        first_tested_at=_aware(row.first_tested_at),
        decided_at=_aware(row.decided_at),
    )


def _hypothesis_values(h: Hypothesis) -> dict:
    return {
        "id": h.id, "description": h.description, "status": h.status,
        "confidence": float(h.confidence), "sample_count": int(h.sample_count),
        "created_at": h.created_at, "updated_at": h.updated_at,
        "form": h.form, "source": h.source, "predictor": h.predictor,
        "outcome": h.outcome, "level": h.level, "direction": int(h.direction),
        "params": _json(h.params),
        "discovery_n": h.discovery.n, "discovery_effect": h.discovery.effect,
        "discovery_lower": h.discovery.lower, "discovery_upper": h.discovery.upper,
        "discovery_p": h.discovery.p_value,
        "tested_in_pass": int(h.tested_in_pass),
        "test_n": h.test.n, "test_effect": h.test.effect,
        "test_lower": h.test.lower, "test_upper": h.test.upper,
        "test_p": h.test.p_value,
        "test_excludes_null": bool(h.test.excludes_null),
        "status_reason": h.status_reason,
        "first_tested_at": h.first_tested_at, "decided_at": h.decided_at,
    }


# -------------------------------------------------------------- escritura ----
async def save_observations(observations: list[Observation]) -> int:
    """Guarda observaciones. Las repetidas se IGNORAN, jamas se reescriben.

    Devuelve las filas NUEVAS, que es la unica cifra que dice si la cosecha esta
    aportando algo: en una fuente de estado (`flow_wallets`) casi todas las
    pasadas devolveran 0 en cuanto se hayan visto todas las wallets, y eso es el
    comportamiento correcto, no un fallo.
    """
    if not observations:
        return 0
    now = _utcnow()
    written = 0
    async with get_sessionmaker()() as s:
        async with s.begin():
            for batch in _chunks(observations):
                stmt = pg_insert(HypObservationRow).values([
                    {"uid": sources.observation_uid(o), "source": o.source,
                     "entity": o.entity, "ts": o.ts,
                     "features": _json(o.features), "labels": _json(o.labels),
                     "outcomes": _json(o.outcomes), "ingested_at": now}
                    for o in batch
                ])
                res = await s.execute(
                    stmt.on_conflict_do_nothing(index_elements=[HypObservationRow.uid])
                )
                written += res.rowcount or 0
    return written


async def upsert_hypotheses(hypotheses: list[Hypothesis]) -> int:
    """Guarda el estado actual de cada hipotesis.

    En conflicto se actualiza TODO menos `created_at`, `discovery_*` y
    `tested_in_pass`. Esos cinco se escriben una sola vez, cuando la hipotesis
    nace, y no se vuelven a tocar:

      - `created_at` es la valla. Si una pasada posterior la moviese, la hipotesis
        se validaria con datos que ya habia visto y el motor dejaria de medir nada.
      - la evidencia de descubrimiento es el registro de POR QUE se propuso, y
        pisarla con la de hoy borraria la unica forma de comparar el hallazgo
        original con su replicacion.
    """
    if not hypotheses:
        return 0
    written = 0
    async with get_sessionmaker()() as s:
        async with s.begin():
            for batch in _chunks(hypotheses):
                stmt = pg_insert(HypothesisRow).values(
                    [_hypothesis_values(h) for h in batch])
                immutable = {"id", "created_at", "discovery_n", "discovery_effect",
                             "discovery_lower", "discovery_upper", "discovery_p",
                             "tested_in_pass"}
                res = await s.execute(stmt.on_conflict_do_update(
                    index_elements=[HypothesisRow.id],
                    set_={c.name: getattr(stmt.excluded, c.name)
                          for c in HypothesisRow.__table__.columns
                          if c.name not in immutable},
                ))
                written += res.rowcount or 0
    return written


async def save_evidence(hypotheses: list[Hypothesis], ts: dt.datetime | None = None) -> int:
    """Un punto de la curva de evidencia por hipotesis. APPEND-ONLY.

    Solo se anotan las que tienen alguna observacion fuera de muestra: un punto
    con n=0 no informa de nada y multiplicaria las filas por el numero de
    hipotesis en `proposed`, que son la mayoria al principio.
    """
    rows = [h for h in hypotheses if h.sample_count > 0]
    if not rows:
        return 0
    moment = ts or _utcnow()
    written = 0
    async with get_sessionmaker()() as s:
        async with s.begin():
            for batch in _chunks(rows):
                stmt = pg_insert(HypEvidenceRow).values([
                    {"hypothesis_id": h.id, "ts": moment, "status": h.status,
                     "confidence": float(h.confidence),
                     "sample_count": int(h.sample_count),
                     "effect": h.test.effect, "lower": h.test.lower,
                     "upper": h.test.upper, "p_value": h.test.p_value}
                    for h in batch
                ])
                res = await s.execute(stmt.on_conflict_do_nothing(
                    index_elements=[HypEvidenceRow.hypothesis_id, HypEvidenceRow.ts]))
                written += res.rowcount or 0
    return written


async def save_transitions(changes: list[dict], ts: dt.datetime | None = None) -> int:
    """Cambios de estado, con su motivo. APPEND-ONLY: es el expediente."""
    if not changes:
        return 0
    moment = ts or _utcnow()
    async with get_sessionmaker()() as s:
        async with s.begin():
            for batch in _chunks(changes):
                await s.execute(pg_insert(HypTransitionRow).values([
                    {"hypothesis_id": c["id"], "ts": moment,
                     "from_status": c["from"], "to_status": c["to"],
                     "sample_count": int(c.get("sample_count", 0)),
                     "confidence": float(c.get("confidence", 0.0)),
                     "reason": c.get("reason", "")}
                    for c in batch
                ]))
    return len(changes)


async def save_snapshot(summary: dict, build_seconds: float = 0.0) -> None:
    """Una fila por pasada: la serie temporal del motor."""
    counts = summary.get("board", {})
    async with get_sessionmaker()() as s:
        async with s.begin():
            s.add(HypSnapshotRow(
                ts=_utcnow(),
                observations=int(summary.get("observations", 0)),
                new_observations=int(summary.get("new_observations", 0)),
                variables=int(summary.get("variables", 0)),
                tested=int(summary.get("tested", 0)),
                proposed=int(counts.get(PROPOSED, 0)),
                testing=int(counts.get(TESTING, 0)),
                validated=int(counts.get(VALIDATED, 0)),
                rejected=int(counts.get(REJECTED, 0)),
                new_proposals=int(summary.get("new_proposals", 0)),
                transitions=int(summary.get("transitions", 0)),
                avg_confidence=float(summary.get("avg_confidence", 0.0)),
                build_seconds=float(build_seconds),
            ))


# --------------------------------------------------------------- lectura ----
async def load_observations(
    source: str, *, since: dt.datetime | None = None, limit: int = 50000,
) -> list[Observation]:
    """Observaciones de una fuente, de la mas antigua a la mas reciente.

    Ascendente y no descendente a proposito: cuando el `limit` recorta, lo que
    interesa conservar es la HISTORIA (para que la ventana de descubrimiento y la
    de prueba existan las dos), no las ultimas mil filas, que serian todas
    posteriores a cualquier hipotesis y dejarian el descubrimiento sin datos.
    """
    async with get_sessionmaker()() as s:
        stmt = select(HypObservationRow).where(HypObservationRow.source == source)
        if since is not None:
            stmt = stmt.where(HypObservationRow.ts >= since)
        stmt = stmt.order_by(HypObservationRow.ts.asc()).limit(limit)
        rows = list((await s.execute(stmt)).scalars().all())
    return [_row_to_observation(r) for r in rows]


async def load_hypotheses(
    *, source: str | None = None, exclude_rejected: bool = True,
) -> list[Hypothesis]:
    """Hipotesis vivas, para reevaluarlas.

    Las rechazadas se excluyen por defecto: son terminales, y volver a medirlas en
    cada pasada gastaria CPU en expedientes cerrados.
    """
    async with get_sessionmaker()() as s:
        stmt = select(HypothesisRow)
        if source:
            stmt = stmt.where(HypothesisRow.source == source)
        if exclude_rejected:
            stmt = stmt.where(HypothesisRow.status != REJECTED)
        rows = list((await s.execute(stmt)).scalars().all())
    return [_row_to_hypothesis(r) for r in rows]


async def known_ids() -> set[str]:
    """Ids de TODAS las hipotesis, incluidas las rechazadas.

    Es el candado contra el blanqueo de hipotesis: el generador redescubre lo
    mismo cada pasada, y sin este conjunto una hipotesis rechazada volveria a
    entrar como `proposed` con la evidencia de descubrimiento de hoy.
    """
    async with get_sessionmaker()() as s:
        rows = (await s.execute(select(HypothesisRow.id))).scalars().all()
    return set(rows)


async def list_hypotheses(
    *, limit: int = 100, status: str | None = None, source: str | None = None,
    form: str | None = None, order_by: str = "confidence",
) -> list[dict]:
    """Listado para el panel."""
    columns = {
        "confidence": desc(HypothesisRow.confidence),
        "samples": desc(HypothesisRow.sample_count),
        "recent": desc(HypothesisRow.updated_at),
        "created": desc(HypothesisRow.created_at),
        "effect": desc(func.abs(HypothesisRow.test_effect)),
    }
    async with get_sessionmaker()() as s:
        stmt = select(HypothesisRow)
        if status:
            stmt = stmt.where(HypothesisRow.status == status)
        if source:
            stmt = stmt.where(HypothesisRow.source == source)
        if form:
            stmt = stmt.where(HypothesisRow.form == form)
        stmt = stmt.order_by(columns.get(order_by, columns["confidence"])).limit(limit)
        rows = list((await s.execute(stmt)).scalars().all())
    return [_row_to_hypothesis(r).to_dict() for r in rows]


async def get_hypothesis(hypothesis_id: str) -> dict | None:
    async with get_sessionmaker()() as s:
        row = (await s.execute(
            select(HypothesisRow).where(HypothesisRow.id == hypothesis_id)
        )).scalar_one_or_none()
    return _row_to_hypothesis(row).to_dict() if row is not None else None


async def list_evidence(hypothesis_id: str, limit: int = 200) -> list[dict]:
    """Curva de evidencia de una hipotesis, en orden cronologico."""
    async with get_sessionmaker()() as s:
        rows = list((await s.execute(
            select(HypEvidenceRow)
            .where(HypEvidenceRow.hypothesis_id == hypothesis_id)
            .order_by(HypEvidenceRow.ts.asc()).limit(limit)
        )).scalars().all())
    return [
        {"ts": r.ts.isoformat() if r.ts else None, "status": r.status,
         "confidence": round(float(r.confidence), 4),
         "sample_count": int(r.sample_count),
         "effect": round(float(r.effect), 6), "lower": round(float(r.lower), 6),
         "upper": round(float(r.upper), 6), "p_value": round(float(r.p_value), 6)}
        for r in rows
    ]


async def list_transitions(
    hypothesis_id: str | None = None, limit: int = 100,
) -> list[dict]:
    async with get_sessionmaker()() as s:
        stmt = select(HypTransitionRow)
        if hypothesis_id:
            stmt = stmt.where(HypTransitionRow.hypothesis_id == hypothesis_id)
        stmt = stmt.order_by(HypTransitionRow.ts.desc()).limit(limit)
        rows = list((await s.execute(stmt)).scalars().all())
    return [
        {"hypothesis_id": r.hypothesis_id,
         "ts": r.ts.isoformat() if r.ts else None,
         "from": r.from_status, "to": r.to_status,
         "sample_count": int(r.sample_count),
         "confidence": round(float(r.confidence), 4), "reason": r.reason}
        for r in rows
    ]


async def board() -> dict[str, int]:
    """Cuantas hipotesis hay en cada estado. Los cuatro estados salen SIEMPRE,
    aunque valgan 0: un tablero al que le falta una columna se lee como si ese
    estado no existiera."""
    async with get_sessionmaker()() as s:
        rows = (await s.execute(
            select(HypothesisRow.status, func.count())
            .group_by(HypothesisRow.status)
        )).all()
    counts = {st: 0 for st in STATUSES}
    for status, n in rows:
        counts[status] = int(n)
    return counts


async def hyp_stats() -> dict:
    """Totales del motor para las tarjetas del panel."""
    counts = await board()
    async with get_sessionmaker()() as s:
        observations = (await s.execute(
            select(func.count()).select_from(HypObservationRow))).scalar() or 0
        per_source = (await s.execute(
            select(HypObservationRow.source, func.count(),
                   func.min(HypObservationRow.ts), func.max(HypObservationRow.ts))
            .group_by(HypObservationRow.source))).all()
        avg_conf = (await s.execute(
            select(func.avg(HypothesisRow.confidence))
            .where(HypothesisRow.status.in_((TESTING, VALIDATED))))).scalar()
        tested = (await s.execute(
            select(func.sum(HypSnapshotRow.tested)))).scalar() or 0
        passes = (await s.execute(
            select(func.count()).select_from(HypSnapshotRow))).scalar() or 0
    total = sum(counts.values())
    decided = counts[VALIDATED] + counts[REJECTED]
    return {
        **{f"{k}": v for k, v in counts.items()},
        "total": total,
        "decided": decided,
        # Tasa de validacion SOBRE LAS DECIDIDAS, no sobre el total: mientras la
        # mayoria sigue en pruebas, dividir por el total daria un numero que solo
        # dice cuantas hipotesis son jovenes.
        "validation_rate": round(counts[VALIDATED] / decided, 4) if decided else None,
        "observations": int(observations),
        "avg_confidence": round(float(avg_conf), 4) if avg_conf is not None else 0.0,
        "contrasts_tested": int(tested),
        "passes": int(passes),
        "sources": [
            {"source": src, "n": int(n),
             "first": first.isoformat() if first else None,
             "last": last.isoformat() if last else None}
            for src, n, first, last in per_source
        ],
    }


async def source_coverage() -> list[dict]:
    """Cuantas hipotesis y cuanta observacion tiene cada fuente."""
    async with get_sessionmaker()() as s:
        rows = (await s.execute(
            select(HypothesisRow.source, HypothesisRow.status, func.count())
            .group_by(HypothesisRow.source, HypothesisRow.status))).all()
    out: dict[str, dict] = {}
    for src, status, n in rows:
        entry = out.setdefault(src, {"source": src, **{st: 0 for st in STATUSES}})
        entry[status] = int(n)
    return sorted(out.values(), key=lambda e: e["source"])


async def timeline(limit: int = 200) -> list[dict]:
    """Serie temporal del motor, de la mas antigua a la mas reciente."""
    async with get_sessionmaker()() as s:
        rows = list((await s.execute(
            select(HypSnapshotRow).order_by(HypSnapshotRow.ts.desc()).limit(limit)
        )).scalars().all())
    rows.reverse()
    return [
        {"ts": r.ts.isoformat() if r.ts else None,
         "observations": r.observations, "new_observations": r.new_observations,
         "variables": r.variables, "tested": r.tested,
         "proposed": r.proposed, "testing": r.testing,
         "validated": r.validated, "rejected": r.rejected,
         "new_proposals": r.new_proposals, "transitions": r.transitions,
         "avg_confidence": round(float(r.avg_confidence), 4),
         "build_seconds": round(float(r.build_seconds), 3)}
        for r in rows
    ]


# ------------------------------------------------------------------- poda ----
async def prune(retention_days: int, observation_retention_days: int) -> dict:
    """Poda los snapshots y las observaciones muy viejas.

    LAS HIPOTESIS NO SE PODAN NUNCA, y tampoco su expediente ni su curva de
    evidencia. Son el conocimiento del motor: una hipotesis rechazada hace un año
    es justo lo que evita volver a proponerla, y borrarla reabriria la puerta al
    blanqueo que `known_ids()` cierra.

    Las observaciones si se podan, con retencion larga y con una consecuencia que
    hay que tener presente: podar por debajo de `created_at` de una hipotesis viva
    no falsea su evidencia (la valla solo mira hacia delante), pero si reduce la
    ventana con la que se propondrian hipotesis NUEVAS.
    """
    now = _utcnow()
    cutoff = now - dt.timedelta(days=max(1, int(retention_days)))
    obs_cutoff = now - dt.timedelta(days=max(1, int(observation_retention_days)))
    async with get_sessionmaker()() as s:
        async with s.begin():
            snaps = await s.execute(
                delete(HypSnapshotRow).where(HypSnapshotRow.ts < cutoff))
            observations = await s.execute(
                delete(HypObservationRow).where(HypObservationRow.ts < obs_cutoff))
    return {"snapshots": snaps.rowcount or 0,
            "observations": observations.rowcount or 0}
