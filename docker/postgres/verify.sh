#!/usr/bin/env bash
set -Eeuo pipefail
export PGPASSWORD="$APP_DB_PASSWORD"
exec psql -X -h 127.0.0.1 -p 5432 -U "$APP_DB_USER" -d "$POSTGRES_DB" \
  -v ON_ERROR_STOP=1 -f /project/postgres/verify.sql
