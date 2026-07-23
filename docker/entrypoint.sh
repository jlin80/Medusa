#!/bin/sh
# Entrypoint minimo: simplemente ejecuta el comando del servicio (engine o api).
# El orden de arranque (Postgres/Redis healthy) lo garantiza docker-compose via
# depends_on: condition: service_healthy. El engine ademas reintenta la conexion.
set -e
exec "$@"
