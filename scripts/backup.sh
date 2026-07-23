#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Backup de Medusa: dump de Postgres + copia del volumen de Redis (AOF) + .env.
# Uso: ./scripts/backup.sh [destino]   (por defecto ./backups)
# Pensado para ejecutarse por cron dentro de CT202.
# ---------------------------------------------------------------------------
set -euo pipefail

DEST="${1:-./backups}"
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="${DEST}/medusa-backup-${STAMP}"
mkdir -p "${OUT}"

echo "[*] Backup Postgres..."
docker exec medusa-postgres pg_dump -U "${DB_USER:-medusa}" "${DB_NAME:-medusa}" \
    | gzip > "${OUT}/postgres.sql.gz"

echo "[*] Backup Redis (AOF/RDB)..."
docker run --rm -v medusa_redisdata:/data -v "$(pwd)/${OUT}":/backup alpine \
    sh -c "cd /data && tar czf /backup/redisdata.tgz ."

echo "[*] Copia de .env (contiene secretos: mantener seguro)..."
cp -f .env "${OUT}/.env.bak" 2>/dev/null || echo "  (sin .env)"

echo "[+] Backup completo en: ${OUT}"
