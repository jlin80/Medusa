#!/usr/bin/env python
"""Healthcheck del contenedor engine.

El engine no expone HTTP; su "vivo/caido" se comprueba leyendo la frescura del
heartbeat que publica en Redis. Sale 0 si esta fresco, 1 en caso contrario.
"""

import os
import sys
import time


def main() -> int:
    try:
        import redis

        host = os.getenv("REDIS_HOST", "redis")
        port = int(os.getenv("REDIS_PORT", "6379"))
        db = int(os.getenv("REDIS_DB", "0"))
        client = redis.Redis(
            host=host, port=port, db=db, decode_responses=True, socket_timeout=5
        )

        value = client.get("medusa:state:heartbeat")
        if not value:
            print("sin heartbeat")
            return 1

        age = time.time() - float(value)
        threshold = int(os.getenv("HEARTBEAT_INTERVAL", "15")) * 4
        if age > threshold:
            print(f"heartbeat viejo: {age:.1f}s > {threshold}s")
            return 1

        print(f"ok (age={age:.1f}s)")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"error healthcheck: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
