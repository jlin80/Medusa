"""Router HTTP de Wallet Intelligence (`/wallets/*`).

Todos los endpoints son de lectura salvo `POST /wallets/build`, que recalcula
los perfiles. "Recalcular" aqui significa leer la Data API publica y escribir en
las tablas `wi_*`: ni manda ordenes, ni toca posiciones, ni puede cambiar una
decision del bot.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from medusa.intelligence.wallet import repository as wi_repo
from medusa.intelligence.wallet.service import WalletIntelligenceService
from medusa.intelligence.wallet.types import DNA_DEFINITIONS, DNA_FEATURES

router = APIRouter(prefix="/wallets", tags=["wallet-intelligence"])

_service: WalletIntelligenceService | None = None


def get_service(log=None) -> WalletIntelligenceService:
    global _service
    if _service is None:
        if log is None:
            from medusa.config import get_settings
            from medusa.logging_setup import configure_logging

            s = get_settings()
            log = configure_logging(s.log_level, s.log_dir, s.log_json, service="wallet")
        _service = WalletIntelligenceService(log)
    return _service


@router.get("/info")
async def wallet_info() -> dict:
    """Configuracion, definicion de las 19 metricas y ultima pasada."""
    return get_service().info()


@router.get("/dna/definitions")
async def dna_definitions() -> dict:
    """Que significa cada numero del ADN. Fuente unica: el dashboard no
    duplica las definiciones, las lee de aqui."""
    return {"features": list(DNA_FEATURES), "definitions": DNA_DEFINITIONS}


@router.get("/stats")
async def wallet_stats() -> dict:
    return await wi_repo.stats()


@router.get("")
async def wallet_explorer(
    limit: int = 100, order_by: str = "reputation", cluster: int | None = None,
    search: str | None = None, min_closed: int = 0,
) -> list[dict]:
    """Wallet Explorer: listado ordenable y filtrable."""
    if order_by not in ("reputation", "score", "n_closed", "updated_at"):
        raise HTTPException(400, f"Orden desconocido: {order_by}")
    return await wi_repo.list_profiles(
        limit=limit, order_by=order_by, cluster=cluster, search=search,
        min_closed=min_closed,
    )


@router.get("/reputation")
async def wallet_reputation(limit: int = 25, min_closed: int = 1) -> list[dict]:
    """Ranking por reputacion (score castigado por muestra, frescura y estabilidad)."""
    return await wi_repo.top_reputation(limit=limit, min_closed=min_closed)


@router.get("/clusters")
async def wallet_clusters() -> list[dict]:
    """Clusters de la ultima pasada. El cluster es un ENTERO: su significado son
    los numeros de su centroide, no una etiqueta."""
    return await wi_repo.latest_clusters()


@router.get("/feature-importance")
async def wallet_feature_importance() -> list[dict]:
    """Importancia descriptiva de cada metrica del ADN en la ultima pasada.

    Es asociacion en la muestra, NO causalidad ni poder predictivo.
    """
    run = await wi_repo.latest_run()
    return (run or {}).get("feature_importance") or []


@router.get("/runs")
async def wallet_runs(limit: int = 100) -> list[dict]:
    """Historial de pasadas (para ver el crecimiento de la poblacion)."""
    return await wi_repo.runs(limit=limit)


@router.get("/{wallet}")
async def wallet_profile(wallet: str) -> dict:
    """Wallet DNA: perfil completo de una wallet."""
    profile = await wi_repo.get_profile(wallet)
    if profile is None:
        raise HTTPException(404, f"Wallet sin perfil: {wallet}")
    return {**profile, "definitions": DNA_DEFINITIONS}


@router.get("/{wallet}/history")
async def wallet_evolution(wallet: str, limit: int = 200) -> list[dict]:
    """Wallet Evolution: como se ha movido su ADN en el tiempo."""
    return await wi_repo.wallet_history(wallet, limit=limit)


@router.get("/{wallet}/similar")
async def wallet_similar(wallet: str, limit: int = 10) -> list[dict]:
    """Wallet Similarity: perfiles mas parecidos. No es una sugerencia de copia."""
    return await wi_repo.similar_to(wallet, limit=limit)


@router.post("/build")
async def wallet_build(persist: bool = True) -> dict:
    """Recalcula los perfiles AHORA. `persist=false` no escribe nada."""
    try:
        return await get_service().run(persist=persist)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"No se pudieron construir los perfiles: {exc}")
