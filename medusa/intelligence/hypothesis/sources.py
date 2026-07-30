"""FUENTES: de donde salen las observaciones. Solo LECTURA sobre tablas ajenas.

El motor no sabe nada de Polymarket, de estrategias ni de wallets. Sabe leer
observaciones: casos con condiciones y con un resultado. Este fichero es el unico
sitio donde se traduce el esquema real de Medusa a ese formato, y su contenido es
LINEAGE DE DATOS, no hipotesis:

    declarar "el spread es una condicion y el ROI un resultado"   -> es esquema
    afirmar  "el spread alto va con un ROI bajo"                  -> es hipotesis

Lo primero lo sabe quien escribio la tabla y esta aqui. Lo segundo sale de los
datos en `generator.py`, y no aparece escrito en ninguna parte del paquete.

---------------------------------------------------------------------------
LA UNIDAD DE OBSERVACION (`unit`), que es la decision menos vistosa y la que
mas cambia los resultados
---------------------------------------------------------------------------

Las tablas de Medusa son de dos tipos y no se pueden cosechar igual:

    unit="event"    una fila = un hecho que paso una vez y no cambia (una señal
                    resuelta, un trade cerrado). Su `uid` sale del id de la fila.
    unit="entity"   una fila = el ESTADO ACTUAL de algo, reescrito en cada pasada
                    del motor que la alimenta (`flow_wallet_metrics`,
                    `flow_market_metrics`). Su `uid` sale de la ENTIDAD.

Con las de estado hay una trampa que hay que ver de frente: si se cosechasen en
cada pasada, la misma wallet entraria cuarenta veces al dia con valores algo
distintos, `sample_count` contaria cuarenta observaciones donde hay una sola
wallet, y cualquier intervalo de confianza saldria absurdamente estrecho. Es
pseudo-replicacion, y hace parecer significativo casi todo.

La solucion es que el `uid` de una fuente de estado sea la entidad y que la
escritura sea `on conflict do nothing`: se queda la PRIMERA vez que se vio a esa
wallet con resultado completo, y no se vuelve a tocar. Una entidad, una
observacion. Se pierde el refinamiento de sus metricas posteriores; se gana que el
`n` signifique lo que dice.

---------------------------------------------------------------------------
PAREJAS BLOQUEADAS (`blocked_pairs`)
---------------------------------------------------------------------------

Algunas parejas (condicion, resultado) estan ligadas por DEFINICION: `roi` se
calcula dividiendo por `entry_price`, asi que "a menor precio de entrada, mayor
ROI" saldria con un efecto enorme y un intervalo estrechisimo, y no seria un
hallazgo sino una division. Un motor que propone solo es exactamente la clase de
sistema que encuentra estas primero, porque son las relaciones mas fuertes del
esquema.

Cada pareja bloqueada lleva su motivo escrito al lado. Bloquear no es esconder:
lo que se bloquea es el contraste TAUTOLOGICO, nunca uno incomodo.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from dataclasses import dataclass, field

from sqlalchemy import select

from medusa.data.db_models import MarketRow, StrategySignalRow, TradeRow
from medusa.infra.db import get_sessionmaker
from medusa.intelligence.hypothesis.types import Observation

UTC = dt.timezone.utc

EVENT = "event"
ENTITY = "entity"


def _uid(source: str, key: str) -> str:
    return hashlib.sha1(f"{source}|{key}".encode("utf-8")).hexdigest()[:40]


def _f(value) -> float | None:
    """Float o None. NUNCA 0.0 por defecto.

    Un NULL en `strategy_signals.spread` significa "no se registro" (las filas
    anteriores al 2026-07-16 no lo tienen). Convertirlo en 0.0 diria "operar aqui
    era gratis", que es una afirmacion falsa metida en los datos: el hueco se
    propaga como hueco y el descarte por parejas se encarga.
    """
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out or out in (float("inf"), float("-inf")):
        return None
    return out


def _clean(values: dict) -> dict:
    """Quita los huecos del saco. Una clave ausente es un hueco explicito."""
    return {k: v for k, v in values.items() if v is not None}


@dataclass(frozen=True)
class SourceSpec:
    """Declaracion de una fuente. Sin logica: solo lo que el motor debe saber."""

    name: str
    unit: str
    title: str
    blocked_pairs: tuple[tuple[str, str], ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict:
        return {
            "name": self.name, "unit": self.unit, "title": self.title,
            "blocked_pairs": [list(p) for p in self.blocked_pairs],
            "notes": list(self.notes),
        }


SPECS: dict[str, SourceSpec] = {
    "signals": SourceSpec(
        name="signals", unit=EVENT,
        title="Señales de estrategia ya resueltas",
        blocked_pairs=(
            # roi = pnl_per_share / entry_price. La relacion es una division.
            ("entry_price", "roi"),
            # pnl_per_share = settle_price - entry_price. Idem.
            ("entry_price", "pnl_per_share"),
            # calibration_error = |signal_prob - resultado|. Correlacionar la
            # probabilidad con el error de esa misma probabilidad mide la forma
            # del valor absoluto, no la calidad de la estrategia.
            ("signal_prob", "calibration_error"),
        ),
        notes=(
            "El instante de la observacion es `resolved_at`, no el del disparo: "
            "hasta que el mercado no resuelve no hay resultado, asi que la "
            "observacion no existia y el generador no pudo verla.",
        ),
    ),
    "trades": SourceSpec(
        name="trades", unit=EVENT,
        title="Operaciones cerradas de Medusa",
        blocked_pairs=(
            ("entry_price", "roi"), ("entry_price", "pnl"),
            # El pnl es NETO de costes y escala con el tamaño: las dos parejas
            # son aritmetica de la propia columna.
            ("cost", "pnl"), ("size", "pnl"),
        ),
    ),
    "flow_wallets": SourceSpec(
        name="flow_wallets", unit=ENTITY,
        title="Wallets del Information Flow Engine",
        notes=(
            "`follow_score` no se cosecha: es 1 - leadership_score y daria una "
            "hipotesis gemela con el signo al reves, o sea el mismo hallazgo "
            "contado dos veces. Por lo mismo entra `speed_score` y no "
            "`information_speed`, que es su transformada monotona.",
        ),
    ),
    "flow_markets": SourceSpec(
        name="flow_markets", unit=ENTITY,
        title="Mercados del Information Flow Engine",
        blocked_pairs=(
            # information_speed del mercado se calcula como participantes por
            # hora dentro de sus cascadas: lleva dentro a n_wallets y a
            # n_cascades.
            ("n_wallets", "information_speed"),
            ("n_cascades", "information_speed"),
        ),
    ),
}


# ------------------------------------------------------------------ señales ----
def _consensus_index(rows: list) -> dict[int, int]:
    """Cuantas ESTRATEGIAS DISTINTAS coincidieron en el mismo mercado y lado.

    Se cuenta sobre la ventana de cosecha y por (mercado, lado): es el "consenso"
    observable del sistema sobre una apuesta concreta. Distintas y no señales
    totales -- una estrategia que dispara tres veces sobre el mismo mercado no es
    un acuerdo, es la misma opinion repetida.

    Es una feature DERIVADA, y por tanto candidata a predictor como cualquier
    otra: si el consenso va con algo o no va con nada lo dira el generador.
    """
    groups: dict[tuple[str, str], set[str]] = {}
    for row in rows:
        key = (row.market_id or "", row.outcome or "")
        groups.setdefault(key, set()).add(row.strategy or "")
    return {
        id(row): len(groups.get((row.market_id or "", row.outcome or ""), ()))
        for row in rows
    }


async def _load_signals(since: dt.datetime, limit: int) -> list[Observation]:
    """Señales resueltas. Lectura pura de `strategy_signals`."""
    async with get_sessionmaker()() as s:
        result = await s.execute(
            select(StrategySignalRow)
            .where(StrategySignalRow.status == "resolved")
            .where(StrategySignalRow.resolved_at.is_not(None))
            .where(StrategySignalRow.resolved_at >= since)
            .order_by(StrategySignalRow.resolved_at.desc())
            .limit(limit)
        )
        rows = list(result.scalars().all())

    consensus = _consensus_index(rows)
    out: list[Observation] = []
    for row in rows:
        won = None if row.won is None else (1.0 if row.won else 0.0)
        prob = _f(row.signal_prob)
        # Error de calibracion: la distancia entre lo que la estrategia dijo que
        # pasaria y lo que paso. Solo existe si hay las dos cosas.
        calibration = None if (won is None or prob is None) else abs(prob - won)
        hours_to_end = None
        if row.end_date is not None and row.ts is not None:
            hours_to_end = max(0.0, (row.end_date - row.ts).total_seconds() / 3600.0)
        out.append(Observation(
            source="signals", entity=str(row.market_id or ""),
            ts=row.resolved_at,
            features=_clean({
                "spread": _f(row.spread),
                "liquidity": _f(row.liquidity),
                "signal_prob": prob,
                "market_prob": _f(row.market_prob),
                "edge": _f(row.edge),
                "confidence": _f(row.confidence),
                "score": _f(row.score),
                "entry_price": _f(row.entry_price),
                "hours_to_end": hours_to_end,
                "strategy_consensus": float(consensus.get(id(row), 1)),
            }),
            labels=_clean({
                "strategy": row.strategy or None,
                "category": row.category or None,
                "side": row.outcome or None,
            }),
            outcomes=_clean({
                "roi": _f(row.roi),
                "pnl_per_share": _f(row.pnl_per_share),
                "won": won,
                "calibration_error": calibration,
            }),
        ))
    return out


# --------------------------------------------------------------- operaciones ---
async def _load_trades(since: dt.datetime, limit: int) -> list[Observation]:
    """Operaciones cerradas. Lectura pura de la tabla de operaciones."""
    async with get_sessionmaker()() as s:
        result = await s.execute(
            select(TradeRow)
            .where(TradeRow.closed_at >= since)
            .order_by(TradeRow.closed_at.desc())
            .limit(limit)
        )
        rows = list(result.scalars().all())
    return [
        Observation(
            source="trades", entity=str(row.market_id or ""), ts=row.closed_at,
            features=_clean({
                "entry_price": _f(row.entry_price),
                "size": _f(row.size),
                "cost": _f(row.cost),
                "edge": _f(row.edge),
            }),
            labels=_clean({
                "strategy": row.strategy or None,
                "mode": row.mode or None,
                "side": row.outcome or None,
            }),
            outcomes=_clean({
                "roi": _f(row.roi),
                "pnl": _f(row.pnl),
                "won": 1.0 if row.won else 0.0,
            }),
        )
        for row in rows
    ]


# --------------------------------------------------- Information Flow Engine ---
async def _load_flow_wallets(limit: int) -> list[Observation]:
    """Wallets del IFE. Fuente de ESTADO: una wallet, una observacion.

    Si el IFE no esta instalado o su tabla no existe, se devuelve una lista vacia:
    una fuente ausente no puede impedir que el motor trabaje con las demas.
    """
    try:
        from medusa.intelligence.flow.models import FlowWalletRow
    except Exception:  # noqa: BLE001 - paquete opcional
        return []
    async with get_sessionmaker()() as s:
        result = await s.execute(
            select(FlowWalletRow)
            .where(FlowWalletRow.enough_samples.is_(True))
            .order_by(FlowWalletRow.last_seen.desc())
            .limit(limit)
        )
        rows = list(result.scalars().all())
    return [
        Observation(
            source="flow_wallets", entity=str(row.wallet or ""), ts=row.last_seen,
            features=_clean({
                "n_cascades": _f(row.n_cascades),
                "n_markets": _f(row.n_markets),
                "leadership_score": _f(row.leadership_score),
                "speed_score": _f(row.speed_score),
                "propagation_time": _f(row.propagation_time),
            }),
            labels={},
            outcomes=_clean({
                "early_information_score": _f(row.early_information_score),
                "late_information_score": _f(row.late_information_score),
                "information_edge": _f(row.information_edge),
            }),
        )
        for row in rows
    ]


async def _load_flow_markets(limit: int) -> list[Observation]:
    """Mercados del IFE con el contexto de mercado. Fuente de ESTADO.

    Se cruza con la tabla de mercados para traer la categoria y la
    microestructura: sin ellas el motor no podria contrastar un grupo de mercados
    contra el resto, que es la mitad de su gramatica.
    """
    try:
        from medusa.intelligence.flow.models import FlowMarketRow
    except Exception:  # noqa: BLE001 - paquete opcional
        return []
    async with get_sessionmaker()() as s:
        result = await s.execute(
            select(FlowMarketRow, MarketRow)
            .join(MarketRow, MarketRow.id == FlowMarketRow.market_id)
            .order_by(FlowMarketRow.last_seen.desc())
            .limit(limit)
        )
        rows = list(result.all())
    return [
        Observation(
            source="flow_markets", entity=str(fm.market_id or ""), ts=fm.last_seen,
            features=_clean({
                "n_cascades": _f(fm.n_cascades),
                "n_wallets": _f(fm.n_wallets),
                "n_events": _f(fm.n_events),
                "avg_cascade_size": _f(fm.avg_cascade_size),
                "spread": _f(mk.spread),
                "liquidity": _f(mk.liquidity),
                "volume_24h": _f(mk.volume_24h),
                "opportunity_score": _f(mk.opportunity_score),
            }),
            labels=_clean({
                "category": mk.medusa_category or None,
                "gamma_category": mk.category or None,
            }),
            outcomes=_clean({
                "consensus_delay": _f(fm.consensus_delay),
                "propagation_time": _f(fm.propagation_time),
                "information_speed": _f(fm.information_speed),
            }),
        )
        for fm, mk in rows
    ]


# ------------------------------------------------------------------ cosecha ----
async def harvest(
    *, lookback_days: float = 180.0, max_rows_per_source: int = 5000,
) -> dict[str, list[Observation]]:
    """Todas las fuentes, por nombre. Una fuente que falle sale vacia.

    Aislada por fuente a proposito: si `strategy_signals` diera un error, las
    otras tres siguen produciendo observaciones. Al contrario que en el IFE -- donde
    una cinta a medias produce cascadas FALSAS y por eso el mercado entero se cae
    de la pasada -- aqui las fuentes son independientes: cada hipotesis vive dentro
    de UNA fuente, asi que perder otra no corrompe su estimacion, solo la deja sin
    proponer esa vez.
    """
    since = dt.datetime.now(UTC) - dt.timedelta(days=lookback_days)
    out: dict[str, list[Observation]] = {}
    loaders = {
        "signals": lambda: _load_signals(since, max_rows_per_source),
        "trades": lambda: _load_trades(since, max_rows_per_source),
        "flow_wallets": lambda: _load_flow_wallets(max_rows_per_source),
        "flow_markets": lambda: _load_flow_markets(max_rows_per_source),
    }
    for name, loader in loaders.items():
        try:
            out[name] = [o for o in await loader() if o.ts is not None]
        except Exception:  # noqa: BLE001 - una fuente caida no tumba la pasada
            out[name] = []
    return out


def observation_uid(obs: Observation, external_id: str = "") -> str:
    """Huella de la observacion segun la UNIDAD declarada por su fuente.

    Para una fuente de eventos, el id de la fila (o su entidad + instante) hace
    que cada hecho entre una vez. Para una fuente de estado, la huella es SOLO la
    entidad: asi la segunda vez que se cosecha la misma wallet choca con la
    primera y no entra, que es lo que evita la pseudo-replicacion.
    """
    spec = SPECS.get(obs.source)
    if spec is not None and spec.unit == ENTITY:
        return _uid(obs.source, obs.entity)
    key = external_id or f"{obs.entity}|{obs.ts.isoformat()}"
    return _uid(obs.source, key)
