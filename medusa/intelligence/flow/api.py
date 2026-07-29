"""Router HTTP del IFE (se monta en la API existente con `include_router`).

TODOS los endpoints son de lectura salvo `POST /flow/run`, que ejecuta una
pasada. "Pasada" aqui significa leer la cinta publica de Polymarket y escribir en
las tablas `flow_*`: no manda ordenes, no toca posiciones y no puede cambiar una
sola decision del bot. Es el equivalente a pulsar "recalcular" en un informe.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from medusa.intelligence.flow import repository as flow_repo
from medusa.intelligence.flow.service import InformationFlowService

router = APIRouter(prefix="/flow", tags=["flow"])

# El servicio se instancia aqui con el logger de la API. No comparte estado con
# el del engine: cada proceso analiza contra la misma BD, y los upserts son
# idempotentes por diseño.
_service: InformationFlowService | None = None


def get_service(log=None) -> InformationFlowService:
    global _service
    if _service is None:
        if log is None:
            from medusa.config import get_settings
            from medusa.logging_setup import configure_logging

            s = get_settings()
            log = configure_logging(s.log_level, s.log_dir, s.log_json, service="flow")
        _service = InformationFlowService(log)
    return _service


@router.get("/info")
async def flow_info() -> dict:
    """Configuracion del motor y telemetria de la ultima pasada.

    Incluye las definiciones exactas de cada metrica: un numero sin su
    definicion se acaba leyendo como lo que a uno le convenga.
    """
    return {
        **get_service().info(),
        "definitions": {
            "information_speed":
                "Mediana de segundos entre el inicio de la cascada y la entrada "
                "de la wallet. Menos = entra antes.",
            "speed_score":
                "information_speed en forma acotada [0,1] con vida media, para "
                "poder comparar mercados de escalas distintas.",
            "leadership_score":
                "Media de (1 - rango normalizado) en sus cascadas. 0.5 es lo que "
                "se espera por azar bajo intercambiabilidad.",
            "follow_score":
                "El complementario: media del rango normalizado. 1.0 = siempre "
                "el ultimo en entrar.",
            "consensus_delay":
                "Segundos hasta que ha entrado la fraccion de consenso de una "
                "cascada. Metrica de mercado.",
            "propagation_time":
                "Mediana del salto entre entradas consecutivas. Por wallet: "
                "cuanto tarda en entrar el siguiente DESPUES de ella.",
            "early_information_score":
                "De sus entradas tempranas, en que fraccion el lado acabo "
                "teniendo razon (resolucion si la hay; si no, movimiento del "
                "precio posterior).",
            "late_information_score":
                "Lo mismo para sus entradas tardias.",
            "information_edge":
                "early - late. DESCRIPTIVO: dice donde se concentra su acierto, "
                "no que entrar antes lo cause.",
        },
        "warning":
            "Este motor mide PROPAGACION (orden temporal), jamas CAUSALIDAD. "
            "Que una wallet entre despues de otra no significa que la siga.",
    }


@router.get("/stats")
async def flow_stats() -> dict:
    """Totales de lo persistido: cinta, cascadas, eslabones y cobertura."""
    return await flow_repo.flow_stats()


@router.get("/cascades")
async def flow_cascades(
    limit: int = 50, market_id: str | None = None, min_participants: int = 0,
) -> list[dict]:
    """Cascadas detectadas, de la mas reciente a la mas antigua."""
    return await flow_repo.list_cascades(
        limit=limit, market_id=market_id, min_participants=min_participants)


@router.get("/events")
async def flow_events(
    limit: int = 100, wallet: str | None = None, market_id: str | None = None,
    cascade_key: str | None = None, max_hop: int | None = None,
) -> list[dict]:
    """Eventos de propagacion (todos se guardan; aqui se paginan).

    `leader` y `follower` son etiquetas de ORDEN TEMPORAL. El evento no afirma
    influencia de ningun tipo.
    """
    return await flow_repo.list_events(
        limit=limit, wallet=wallet, market_id=market_id,
        cascade_key=cascade_key, max_hop=max_hop)


@router.get("/wallets")
async def flow_wallets(
    limit: int = 50, order_by: str = "leadership", all_wallets: bool = False,
) -> list[dict]:
    """Ranking de wallets por su papel en el flujo.

    Por defecto solo salen las que tienen muestra suficiente. `all_wallets=true`
    incluye las anecdoticas: util para depurar, inutil para concluir.
    """
    valid = {"leadership", "speed", "follow", "early", "edge", "cascades"}
    if order_by not in valid:
        raise HTTPException(400, f"Orden desconocido: {order_by} (validos: {sorted(valid)})")
    return await flow_repo.list_wallets(
        limit=limit, order_by=order_by, only_with_evidence=not all_wallets)


@router.get("/wallets/{wallet}")
async def flow_wallet(wallet: str) -> dict:
    """Ficha de flujo de una wallet + sus ultimos eslabones."""
    data = await flow_repo.get_wallet(wallet)
    if data is None:
        raise HTTPException(404, f"La wallet no aparece en ninguna cascada: {wallet}")
    return {"metrics": data,
            "events": await flow_repo.list_events(limit=50, wallet=wallet)}


@router.get("/markets")
async def flow_markets(limit: int = 50) -> list[dict]:
    """Como se propaga la informacion dentro de cada mercado."""
    return await flow_repo.list_markets(limit=limit)


@router.get("/pairs")
async def flow_pairs(limit: int = 50, min_observations: int = 5) -> list[dict]:
    """Pares (lider, seguidor) que mas veces han coincidido en ese orden.

    NO es una relacion de influencia: es una coincidencia repetida. Se publica
    con `n` por delante para que nadie lea tres casualidades como un patron.
    """
    return await flow_repo.top_pairs(limit=limit, min_observations=min_observations)


@router.get("/lag-histogram")
async def flow_lag_histogram(buckets: int = 12, max_seconds: float = 3600.0) -> list[dict]:
    """Distribucion de las latencias lider->seguidor."""
    if buckets < 1 or buckets > 100:
        raise HTTPException(400, "buckets tiene que estar entre 1 y 100")
    return await flow_repo.lag_histogram(buckets=buckets, max_seconds=max_seconds)


@router.get("/timeline")
async def flow_timeline(limit: int = 200) -> list[dict]:
    """Serie temporal del motor (una fila por pasada)."""
    return await flow_repo.timeline(limit=limit)


@router.post("/run")
async def flow_run(persist: bool = True) -> dict:
    """Ejecuta una pasada AHORA.

    `persist=false` la calcula sobre lo que se acaba de descargar y devuelve el
    resumen sin escribir nada: util para ver que saldria antes de guardarlo.
    """
    try:
        return await get_service().run(persist=persist)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"No se pudo completar la pasada: {exc}")
