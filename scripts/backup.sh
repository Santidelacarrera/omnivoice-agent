#!/bin/sh
# Copia lógica de PostgreSQL con rotación. Uso (cron diario): scripts/backup.sh [directorio] [días_a_conservar]
set -e
DIR="${1:-./backups}"; KEEP="${2:-14}"
mkdir -p "$DIR"
F="$DIR/omnivoice-$(date +%Y%m%d-%H%M%S).sql.gz"
docker compose -f docker-compose.yml -f docker-compose.prod.yml exec -T postgres pg_dump -U omni -d omnivoice | gzip > "$F"
[ -s "$F" ] || { echo "copia vacía" >&2; rm -f "$F"; exit 1; }
find "$DIR" -name 'omnivoice-*.sql.gz' -mtime +"$KEEP" -delete
echo "$F"
