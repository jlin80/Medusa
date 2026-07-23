"""Bus de eventos sobre Redis pub/sub.

Canales:
  medusa:events    -> eventos de negocio (trades, oportunidades, sistema)
  medusa:logs      -> lineas de log para la terminal del dashboard
  medusa:commands  -> comandos del dashboard hacia el engine (toggle, kill)
  medusa:heartbeat -> latido del engine
"""

import datetime as dt
import json
from typing import Any

from medusa.infra.redis_bus import get_redis

CH_EVENTS = "medusa:events"
CH_LOGS = "medusa:logs"
CH_COMMANDS = "medusa:commands"
CH_HEARTBEAT = "medusa:heartbeat"


async def publish(channel: str, payload: dict[str, Any]) -> None:
    """Publica un evento serializado en JSON en el canal indicado."""
    await get_redis().publish(channel, json.dumps(payload, default=str))


async def publish_log(
    level: Any, message: str, payload: dict | None = None, source: str = "engine",
) -> None:
    """Registra una linea en la terminal del dashboard.

    Doble destino a proposito: Postgres da el historico (superviviente a
    reinicios) y Redis pub/sub da el tiempo real a los clientes conectados.
    """
    from medusa.data import repositories as repo   # import diferido: evita ciclo

    level_str = getattr(level, "value", str(level))
    entry = {
        "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
        "level": level_str,
        "source": source,
        "message": message,
        "payload": payload or {},
    }
    try:
        await repo.log_event(level_str, source, message, payload)
    except Exception:  # noqa: BLE001 - el log nunca puede romper al que lo llama
        pass
    try:
        await publish(CH_LOGS, entry)
    except Exception:  # noqa: BLE001
        pass
