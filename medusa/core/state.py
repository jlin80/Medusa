"""Estado global de Medusa (modo, kill-switch, heartbeat).

Fuente de verdad definitiva sera la tabla bot_state en Postgres (F1); aqui se
cachea en Redis para lectura de baja latencia por el engine y la API.
"""

import time

from medusa.core.enums import Mode
from medusa.infra.redis_bus import get_redis

KEY_MODE = "medusa:state:mode"
KEY_KILL = "medusa:state:kill_switch"
KEY_HEARTBEAT = "medusa:state:heartbeat"


async def get_mode(default: Mode | None = Mode.PAPER) -> Mode | None:
    value = await get_redis().get(KEY_MODE)
    return Mode(value) if value else default


async def set_mode(mode: Mode) -> None:
    await get_redis().set(KEY_MODE, mode.value)


async def get_kill_switch() -> bool:
    return (await get_redis().get(KEY_KILL)) == "1"


async def set_kill_switch(on: bool) -> None:
    await get_redis().set(KEY_KILL, "1" if on else "0")


async def set_heartbeat(ts: float | None = None) -> None:
    await get_redis().set(KEY_HEARTBEAT, str(ts if ts is not None else time.time()))


async def get_heartbeat() -> float | None:
    value = await get_redis().get(KEY_HEARTBEAT)
    return float(value) if value else None
