#!/bin/sh
# Restaura una copia en una base NUEVA (no pisa la de producción) para ensayar o recuperar.
# Uso: scripts/restore.sh ruta/copia.sql.gz [nombre_bd=omnivoice_restore]
set -e
F="${1:?uso: restore.sh copia.sql.gz [nombre_bd]}"; DB="${2:-omnivoice_restore}"
gzip -t "$F"
C="docker compose -f docker-compose.yml -f docker-compose.prod.yml exec -T postgres"
$C psql -U omni -d postgres -v ON_ERROR_STOP=1 -c "DROP DATABASE IF EXISTS \"$DB\"" -c "CREATE DATABASE \"$DB\""
gunzip -c "$F" | $C psql -U omni -d "$DB" -v ON_ERROR_STOP=1 -q
echo "Restaurado en la base '$DB'. Comprueba: $C psql -U omni -d $DB -c 'SELECT count(*) FROM organizations'"
