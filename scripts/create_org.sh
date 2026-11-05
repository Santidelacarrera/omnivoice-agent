#!/bin/sh
# Crea una organización (tenant). Uso: scripts/create_org.sh "Nombre" [retention_days]
# Imprime el UUID que debe ir en el claim "org" del JWT que emite tu proveedor de identidad.
set -e
NAME="${1:?uso: create_org.sh \"Nombre\" [retention_days]}"
DAYS="${2:-30}"
COMPOSE="docker compose -f docker-compose.yml -f docker-compose.prod.yml"
$COMPOSE exec -T postgres psql -U omni -d omnivoice -tA -v name="$NAME" -v days="$DAYS" \
  -c "INSERT INTO organizations (name, retention_days) VALUES (:'name', :'days'::int) RETURNING id"
