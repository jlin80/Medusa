"""Router HTTP del HE (se monta en la API existente con `include_router`).

TODOS los endpoints son de lectura salvo `POST /hypotheses/run`, que ejecuta una
pasada. "Pasada" aqui significa leer tablas propias y ajenas, proponer hipotesis
y escribir en las tablas `hyp_*`: no manda ordenes, no toca posiciones y no puede
cambiar una sola decision del bot. Es el equivalente a pulsar "recalcular" en un
informe.

No hay endpoint para CREAR una hipotesis a mano, y su ausencia es la funcionalidad:
el enunciado del motor es que las hipotesis salen de los datos observados. Un
`POST /hypotheses` seria la puerta por la que entraria la primera hipotesis
escrita por una persona, y con ella se perderia la unica garantia que hace
interesante a lo que hay guardado.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from medusa.intelligence.hypothesis import repository as hyp_repo
from medusa.intelligence.hypothesis import sources
from medusa.intelligence.hypothesis.evaluator import REFERENCE_EFFECT
from medusa.intelligence.hypothesis.service import HypothesisService
from medusa.intelligence.hypothesis.types import FORMS, STATUSES

router = APIRouter(prefix="/hypotheses", tags=["hypotheses"])

# El servicio se instancia aqui con el logger de la API. No comparte estado con el
# del engine: cada proceso analiza contra la misma BD, y los upserts son
# idempotentes por diseño.
_service: HypothesisService | None = None


def get_service(log=None) -> HypothesisService:
    global _service
    if _service is None:
        if log is None:
            from medusa.config import get_settings
            from medusa.logging_setup import configure_logging

            s = get_settings()
            log = configure_logging(s.log_level, s.log_dir, s.log_json,
                                    service="hypothesis")
        _service = HypothesisService(log)
    return _service


@router.get("/info")
async def hypothesis_info() -> dict:
    """Configuracion del motor, su gramatica y telemetria de la ultima pasada.

    Incluye las definiciones exactas de cada campo: un numero sin su definicion se
    acaba leyendo como lo que a uno le convenga, y `confidence` es el candidato
    perfecto a que se lea como "probabilidad de que sea verdad".
    """
    return {
        **get_service().info(),
        "statuses": list(STATUSES),
        "forms": list(FORMS),
        "reference_effect": REFERENCE_EFFECT,
        "definitions": {
            "id":
                "Hash del ENUNCIADO (forma + fuente + predictor + nivel + "
                "outcome). No incluye la direccion a proposito: si la incluyera, "
                "un cambio de signo crearia una hipotesis virgen y el motor "
                "podria tirar la moneda hasta que saliera cara.",
            "description":
                "Frase GENERADA a partir de la forma y de los nombres reales de "
                "las variables. No hay ninguna hipotesis escrita en el codigo.",
            "status":
                "proposed (encontrada, sin evidencia nueva) -> testing (llegan "
                "datos que no pudo ver) -> validated | rejected. `rejected` es "
                "terminal; `validated` no lo es.",
            "confidence":
                "NO es la probabilidad de que la hipotesis sea cierta. Es la "
                "fuerza de la evidencia FUERA DE MUESTRA en [0,1]: crece con el "
                "efecto que sostiene la cota inferior y con la muestra, y vale "
                "0.0 si el intervalo cruza el nulo o si el signo observado es el "
                "contrario al afirmado.",
            "sample_count":
                "Observaciones POSTERIORES a `created_at`. No es el total de la "
                "fuente ni el tamaño de la ventana de descubrimiento.",
            "created_at":
                "La VALLA. Solo las observaciones posteriores a este instante "
                "cuentan para validar o rechazar.",
            "discovery":
                "El efecto en la ventana que la propuso. Es DENTRO de muestra, "
                "asi que no es evidencia: son los mismos datos que eligieron el "
                "enunciado.",
            "test":
                "El efecto fuera de muestra. Es la unica evidencia del motor.",
            "tested_in_pass":
                "Cuantas relaciones se contrastaron en la pasada que la propuso. "
                "Es el denominador sin el que la significancia no se puede juzgar.",
        },
        "warning":
            "Este motor observa ASOCIACION, jamas CAUSALIDAD. No hay "
            "contrafactual, no hay asignacion aleatoria y no hay control de "
            "confusores: por eso los enunciados dicen «va con» y nunca «reduce» "
            "ni «mejora». Y una hipotesis solo cuenta como evidencia con datos "
            "posteriores a su creacion.",
    }


@router.get("/stats")
async def hypothesis_stats() -> dict:
    """Totales del motor: tablero, observaciones y contrastes acumulados."""
    return await hyp_repo.hyp_stats()


@router.get("/board")
async def hypothesis_board() -> dict[str, int]:
    """Cuantas hipotesis hay en cada estado. Los cuatro salen siempre."""
    return await hyp_repo.board()


@router.get("/sources")
async def hypothesis_sources() -> list[dict]:
    """Las fuentes declaradas, con su unidad de observacion y sus parejas
    bloqueadas. Es el lineage del motor: que columna es condicion, que columna es
    resultado y que parejas estan ligadas por definicion."""
    return [spec.to_dict() for spec in sources.SPECS.values()]


@router.get("/coverage")
async def hypothesis_coverage() -> list[dict]:
    """Hipotesis por fuente y por estado."""
    return await hyp_repo.source_coverage()


@router.get("/timeline")
async def hypothesis_timeline(limit: int = 200) -> list[dict]:
    """Serie temporal del motor (una fila por pasada)."""
    return await hyp_repo.timeline(limit=limit)


@router.get("/transitions")
async def hypothesis_transitions(limit: int = 100) -> list[dict]:
    """Ultimos cambios de estado de todo el motor, con su motivo."""
    return await hyp_repo.list_transitions(limit=limit)


@router.get("")
async def hypothesis_list(
    limit: int = 100, status: str | None = None, source: str | None = None,
    form: str | None = None, order_by: str = "confidence",
) -> list[dict]:
    """Listado de hipotesis con filtros."""
    if status and status not in STATUSES:
        raise HTTPException(400, f"Estado desconocido: {status} (validos: {list(STATUSES)})")
    if form and form not in FORMS:
        raise HTTPException(400, f"Forma desconocida: {form} (validas: {list(FORMS)})")
    valid_order = {"confidence", "samples", "recent", "created", "effect"}
    if order_by not in valid_order:
        raise HTTPException(
            400, f"Orden desconocido: {order_by} (validos: {sorted(valid_order)})")
    return await hyp_repo.list_hypotheses(
        limit=limit, status=status, source=source, form=form, order_by=order_by)


@router.get("/{hypothesis_id}")
async def hypothesis_detail(hypothesis_id: str) -> dict:
    """Expediente completo de una hipotesis: estado, evidencia y transiciones."""
    data = await hyp_repo.get_hypothesis(hypothesis_id)
    if data is None:
        raise HTTPException(404, f"No existe la hipotesis {hypothesis_id}")
    return {
        "hypothesis": data,
        "evidence": await hyp_repo.list_evidence(hypothesis_id),
        "transitions": await hyp_repo.list_transitions(hypothesis_id, limit=50),
    }


@router.post("/run")
async def hypothesis_run(persist: bool = True) -> dict:
    """Ejecuta una pasada AHORA.

    `persist=false` la calcula sobre lo que se acaba de cosechar y devuelve el
    resumen sin escribir nada. Util para ver que saldria, pero hay que leerlo
    sabiendo que una vista previa no puede validar NADA: sus hipotesis nacen con
    `created_at` = ahora y por tanto con cero observaciones fuera de muestra.
    """
    try:
        return await get_service().run(persist=persist)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"No se pudo completar la pasada: {exc}")
