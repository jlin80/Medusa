#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Despliegue/actualizacion de Medusa en CT202.
# Uso: ./scripts/deploy.sh
# ---------------------------------------------------------------------------
set -euo pipefail

if [ ! -f .env ]; then
    echo "[!] Falta .env. Copia .env.example a .env y configura los valores."
    exit 1
fi

echo "[*] Construyendo imagenes..."
docker compose build

# Los contenedores corren como usuario no-root (uid 999). Los bind mounts llegan
# con el propietario del host (root), asi que sin este chown el engine y la api
# mueren al abrir /app/logs/*.log. El chown del Dockerfile no basta: el bind
# mount tapa lo que hubiera dentro de la imagen.
echo "[*] Ajustando permisos de los bind mounts..."
CUID="$(docker run --rm --entrypoint sh medusa-core:latest -c 'id -u medusa' 2>/dev/null || echo 999)"
CGID="$(docker run --rm --entrypoint sh medusa-core:latest -c 'id -g medusa' 2>/dev/null || echo 999)"
mkdir -p logs secrets
chown -R "${CUID}:${CGID}" logs secrets

echo "[*] Levantando servicios (24/7, restart unless-stopped)..."
docker compose up -d

echo "[*] Estado:"
docker compose ps

echo
echo "[*] Verificacion recomendada:"
echo "    docker compose exec engine python scripts/selftest.py"
echo "    curl -s http://127.0.0.1:\${DASHBOARD_PORT:-8080}/api/health"
