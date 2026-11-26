#!/usr/bin/env bash
# El entrypoint oficial ejecuta o carga este archivo solo con PGDATA vacÃ­o.
set -Eeuo pipefail

psql -X -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  -c 'CREATE SCHEMA IF NOT EXISTS cfg;'

for source in \
  '/project/referencias/001_dimensiones_cliente_producto_txn (1).sql' \
  '/project/referencias/002_generar_cuentas_y_saldos_enero_2026 (1).sql' \
  '/project/referencias/003_generar_transacciones_y_saldos_feb_jun_2026 (1).sql'; do
  echo "Cargando referencia original: $source"
  psql -X -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" -f "$source"
done

psql -X -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  -v db="$POSTGRES_DB" -v owner="$POSTGRES_USER" \
  -v app_user="$APP_DB_USER" -v app_password="$APP_DB_PASSWORD" \
  -f /project/postgres/readonly.sql

psql -X -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  -v app_user="$APP_DB_USER" -f /project/postgres/docs.sql

psql -X -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  -f /project/postgres/client_access.sql

# Durante init el servidor solo escucha por socket Unix. La verificaciÃ³n
# posterior usa TCP y contraseÃ±a para comprobar autenticaciÃ³n real.
PGUSER="$APP_DB_USER" PGDATABASE="$POSTGRES_DB" \
  psql -X -v ON_ERROR_STOP=1 -f /project/postgres/verify.sql
touch "$PGDATA/.banco-ready"
