"""Conexion a Redis (bus de eventos + cache + flags de estado)."""

import redis.asyncio as aioredis

from medusa.config import get_settings

_redis: aioredis.Redis | None = None


def get_redis() -> aioredis.Redis:
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(
            get_settings().redis_url,
            decode_responses=True,
            socket_timeout=5,
            socket_connect_timeout=5,
            health_check_interval=30,
        )
    return _redis


async def check_redis() -> bool:
    """Devuelve True si Redis responde al PING."""
    return bool(await get_redis().ping())


async def close() -> None:
    global _redis
    if _redis is not None:
        await _redis.aclose()
        _redis = None
