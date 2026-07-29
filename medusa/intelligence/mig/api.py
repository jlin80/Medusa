"""Router HTTP del MIG (se monta en la API existente con `include_router`).

TODOS los endpoints son de lectura salvo `POST /mig/build`, que reconstruye el
grafo. "Reconstruir" aqui significa leer tablas de trading y escribir en las
tablas `mig_*`: no manda ordenes, no toca posiciones y no puede cambiar una sola
decision del bot. Es el equivalente a pulsar "recalcular" en un informe.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from medusa.intelligence.mig import repository as mig_repo
from medusa.intelligence.mig.service import MIGService
from medusa.intelligence.mig.types import EdgeType, NodeType

router = APIRouter(prefix="/mig", tags=["mig"])

# El servicio se instancia aqui con el logger de la API. No comparte estado con
# el del engine: cada proceso construye su grafo contra la misma BD, y el upsert
# es idempotente por diseño.
_service: MIGService | None = None


def get_service(log=None) -> MIGService:
    global _service
    if _service is None:
        if log is None:
            from medusa.config import get_settings
            from medusa.logging_setup import configure_logging

            s = get_settings()
            log = configure_logging(s.log_level, s.log_dir, s.log_json, service="mig")
        _service = MIGService(log)
    return _service


@router.get("/info")
async def mig_info() -> dict:
    """Configuracion del MIG y telemetria de la ultima construccion."""
    svc = get_service()
    return {
        **svc.info(),
        "node_types": [t.value for t in NodeType],
        "edge_types": [t.value for t in EdgeType],
    }


@router.get("/stats")
async def mig_stats() -> dict:
    """Estadisticas del grafo persistido: totales, conteos por tipo, densidad."""
    return await mig_repo.graph_stats()


@router.get("/nodes")
async def mig_nodes(
    limit: int = 100, node_type: str | None = None, search: str | None = None,
) -> list[dict]:
    if node_type and node_type not in {t.value for t in NodeType}:
        raise HTTPException(400, f"Tipo de nodo desconocido: {node_type}")
    return await mig_repo.list_nodes(limit=limit, node_type=node_type, search=search)


@router.get("/edges")
async def mig_edges(
    limit: int = 100, edge_type: str | None = None, node: str | None = None,
) -> list[dict]:
    if edge_type and edge_type not in {t.value for t in EdgeType}:
        raise HTTPException(400, f"Tipo de arista desconocido: {edge_type}")
    return await mig_repo.list_edges(limit=limit, edge_type=edge_type, node=node)


@router.get("/neighbors")
async def mig_neighbors(node: str, limit: int = 50) -> dict:
    """Vecindario de un nodo (aristas de entrada y de salida)."""
    data = await mig_repo.neighbors(node, limit=limit)
    if data["node"] is None:
        raise HTTPException(404, f"El nodo no existe en el grafo: {node}")
    return data


@router.get("/discoveries")
async def mig_discoveries(limit: int = 50, kind: str | None = None,
                          history: bool = False) -> list[dict]:
    """Objetos de inteligencia derivados del grafo.

    NO son señales ni features: son patrones con su evidencia. Ver la cabecera
    de `discoveries.py`.
    """
    return await mig_repo.list_discoveries(limit=limit, kind=kind, latest_only=not history)


@router.get("/growth")
async def mig_growth(limit: int = 200) -> list[dict]:
    """Crecimiento del grafo en el tiempo (una fila por construccion)."""
    return await mig_repo.growth(limit=limit)


@router.post("/build")
async def mig_build(persist: bool = True) -> dict:
    """Reconstruye el grafo AHORA.

    `persist=false` lo construye en memoria y devuelve las estadisticas sin
    escribir nada: util para ver que saldria antes de guardarlo.
    """
    svc = get_service()
    try:
        return await svc.build(persist=persist)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"No se pudo construir el grafo: {exc}")
