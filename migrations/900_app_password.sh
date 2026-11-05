#!/bin/sh
# Primer arranque de PostgreSQL (tras las migraciones .sql): fija la clave real del rol omni_app.
# Sin OMNI_APP_PASSWORD (desarrollo) no hace nada y el rol conserva la clave de ejemplo.
if [ -n "$OMNI_APP_PASSWORD" ]; then
  psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v pw="$OMNI_APP_PASSWORD" <<'SQL'
ALTER ROLE omni_app PASSWORD :'pw';
SQL
fi
