"""Reproduce cuentas o compras con la conexión de Cliente, sin mostrar sus filas."""
import argparse
import os
import sys
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from banco_agentes.auth import read_users, users_path
from banco_agentes.client_access import DEFAULT_PURCHASE_PERIOD, ROLE, connect_client, purchase_plan
from banco_agentes.governance import DemoSession, SESSION
from banco_agentes.plans import MAX_ROWS, analytics_query, base_query, normalized_plan


def report_postgres_error(error):
    print(f"PostgreSQL: {type(error).__name__}, SQLSTATE={error.sqlstate or 'no disponible'}")
    primary = error.diag.message_primary
    # El SQL es fijo. Solo se imprimen diagnósticos de esquema, permisos o autenticación.
    if primary and error.sqlstate in {"28000", "28P01", "3D000", "3F000", "42501", "42703", "42704", "42P01", "57014"}:
        print(primary)
        return
    # libpq puede devolver errores de conexión sin diag ni SQLSTATE.
    for message in (
        f'password authentication failed for user "{ROLE}"',
        f'role "{ROLE}" does not exist',
        f'role "{ROLE}" is not permitted to log in',
        "fe_sendauth: no password supplied",
        "Connection refused",
        "timeout expired",
    ):
        if message.casefold() in str(error).casefold():
            print(message)
            return


def diagnose(identity, *, compras=False):
    scope = SESSION.set(identity)
    stage = "conexión y configuración de la transacción"
    try:
        with connect_client() as db:
            stage = "contexto de la transacción"
            role, read_only, customer = db.execute("""
                SELECT current_user, current_setting('transaction_read_only'),
                       current_setting('app.num_cte', true)
            """).fetchone()
            scoped = (role, read_only, customer) == (ROLE, "on", str(identity.num_cte))
            print(f"Transacción: rol={role}, solo lectura={read_only}, num_cte={customer}")
            if not scoped:
                return 1

            stage = "catálogo de vistas y privilegios"
            views = db.execute("""
                SELECT expected.name, c.relkind = 'v', has_schema_privilege(n.oid, 'USAGE'),
                       has_table_privilege(c.oid, 'SELECT'),
                       COALESCE('security_barrier=true' = ANY(c.reloptions), false),
                       COALESCE(strpos(pg_get_viewdef(c.oid), 'app.num_cte') > 0, false)
                FROM (VALUES ('saldos'), ('movimientos')) AS expected(name)
                LEFT JOIN pg_namespace n ON n.nspname = 'cliente'
                LEFT JOIN pg_class c ON c.relnamespace = n.oid AND c.relname = expected.name
                ORDER BY expected.name
            """).fetchall()
            for name, view, usage, select, barrier, filter_present in views:
                print(f"cliente.{name}: vista={bool(view)}, USAGE={bool(usage)}, SELECT={bool(select)}, "
                      f"barrera={barrier}, filtro de sesión={filter_present}")

            if compras:
                stage = "compilación del plan de compras mensuales"
                plan = normalized_plan(purchase_plan(DEFAULT_PURCHASE_PERIOD))
                prefix, params, _ = base_query(plan)
                query = prefix + analytics_query(plan) + f" LIMIT {MAX_ROWS + 1}"
                stage = "SELECT de mis_gastos_por_mes (TDC, código 400, 2026-02 a 2026-06)"
                rows = db.execute(query, params).fetchall()
                print(f"mis_gastos_por_mes: SELECT ejecutado, meses con compras registradas={len(rows)}")
            else:
                stage = "SELECT de mis_cuentas"
                # Misma consulta y parámetro de sesión que query_accounts, sin imprimir filas.
                accounts = db.execute("""
                    SELECT DISTINCT ON (num_cta, cve_producto)
                           num_cta, cve_producto, fec_informacion, saldo_mes
                    FROM cliente.saldos
                    WHERE num_cte = %s
                    ORDER BY num_cta, cve_producto, fec_informacion DESC
                """, (SESSION.get().num_cte,)).fetchall()
                print(f"mis_cuentas: SELECT ejecutado, filas={len(accounts)}")
            stage = "alcance de las vistas sin filtro de la aplicación"
            foreign = db.execute("""
                SELECT EXISTS (SELECT FROM cliente.saldos WHERE num_cte <> current_setting('app.num_cte')::integer)
                    OR EXISTS (SELECT FROM cliente.movimientos WHERE num_cte <> current_setting('app.num_cte')::integer)
            """).fetchone()[0]
            print(f"Filas de otro titular visibles en las vistas: {foreign}")
            return int(foreign or not all(all(checks) for _, *checks in views))
    except psycopg.Error as error:
        print(f"Etapa del fallo: {stage}")
        report_postgres_error(error)
    except (ValueError, PermissionError) as error:
        print(f"Etapa del fallo: {stage}. {type(error).__name__}")
        if not os.getenv("BANCO_CLIENT_DB_PASSWORD"):
            print("Falta BANCO_CLIENT_DB_PASSWORD en este proceso. El conector no abrió PostgreSQL.")
    finally:
        SESSION.reset(scope)
    return 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("username", help="Usuario Cliente del registro local de la aplicación.")
    parser.add_argument("--compras", action="store_true", help="Comprueba la consulta mensual de compras de TDC.")
    args = parser.parse_args()
    username = args.username.strip().lower()
    try:
        entry = read_users(users_path()).get(username)
    except (OSError, ValueError):
        parser.error("No se pudo leer el registro de usuarios configurado.")
    number = entry.get("num_cte") if isinstance(entry, dict) and entry.get("rol") == "Cliente" else None
    if type(number) is not int or not 0 < number <= 2147483647:
        parser.error("El registro no tiene una identidad Cliente válida para ese usuario.")
    print(f"Identidad del registro local para la prueba: Cliente, num_cte={number}")
    return diagnose(DemoSession("Cliente", number, principal=username), compras=args.compras)


if __name__ == "__main__":
    raise SystemExit(main())
