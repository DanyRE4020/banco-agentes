"""Una consulta permitida y parametrizada; nunca ejecuta SQL del modelo."""

import calendar
import os
import re
import time
from pathlib import Path

from dotenv import dotenv_values

from .catalog import ROOT, normalize
from .governance import authorize, mask_employee_ranking, SESSION
from .client_access import connect_client

QUESTION = "¿Cuál fue el saldo total de AHORRO al cierre de junio de 2026?"
PARAMS = ("AHORRO", 20260630)
QUERY = (Path(__file__).parent / "sql/saldo_ahorro_junio.sql").read_text(encoding="utf-8")
TABLE = "cfg.fact_sdos_captacoloca_m"


def supports_question(question):
    text = normalize(question)
    words = set(re.findall(r"\w+", text))
    return {"saldo", "total", "ahorro", "cierre", "junio", "2026"} <= words and not (
        words & {"cliente", "clientes", "cuenta", "cuentas", "enero", "febrero", "marzo", "abril", "mayo", "julio",
                 "agosto", "septiembre", "octubre", "noviembre", "diciembre", "cheques", "tdc", "c_personal"})


def connection_settings():
    local = dotenv_values(ROOT / ".env.postgres", encoding="utf-8-sig")
    def value(key, default=None):
        return os.environ.get(key, local.get(key, default))
    host = value("APP_DB_HOST", "127.0.0.1")
    user = value("APP_DB_USER", "banco_lectura")
    password = value("APP_DB_PASSWORD")
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("Analytics solo admite PostgreSQL local.")
    if not password or user == value("POSTGRES_USER", "banco_admin"):
        raise ValueError("Faltan credenciales de lectura o se intentó usar el administrador.")
    return dict(host=host, port=int(value("APP_DB_PORT", value("POSTGRES_PORT", "55432"))),
                dbname=value("APP_DB_NAME", value("POSTGRES_DB", "banco")), user=user,
                password=password, connect_timeout=3,
                options="-c default_transaction_read_only=on -c statement_timeout=5000")


def connect_readonly():
    import psycopg
    connection = psycopg.connect(**connection_settings())
    connection.read_only = True
    return connection


def rank_employee_customers(product, month):
    from .semantics import PRODUCTS

    denial = authorize("employee_ranking")
    if denial:
        return {**denial, "sql_executed": False}
    if product not in PRODUCTS or not re.fullmatch(r"2026-0[1-6]", month):
        return {"status": "invalid_plan", "sql_executed": False}
    number = int(month[-2:])
    cutoff = 20260000 + number * 100 + calendar.monthrange(2026, number)[1]
    sql_executed = False
    try:
        with connect_readonly() as db:
            if db.execute("SHOW transaction_read_only").fetchone()[0] != "on":
                return {"status": "unavailable", "sql_executed": False}
            sql_executed = True
            rows = db.execute("""
                SELECT round(sum(saldo_mes)::numeric, 2) AS saldo
                FROM cfg.fact_sdos_captacoloca_m
                WHERE cve_producto = %s AND fec_informacion = %s
                GROUP BY num_cte
                HAVING count(saldo_mes) > 0
                ORDER BY saldo DESC, num_cte ASC
                LIMIT 5
            """, (product, cutoff)).fetchall()
    except Exception:
        return {"status": "unavailable", "sql_executed": sql_executed}
    return {"status": "ok", "sql_executed": True, "rows": mask_employee_ranking(saldo for (saldo,) in rows)}


def query_balance():
    started = time.perf_counter()
    base = {"product": PARAMS[0], "cutoff": "2026-06-30", "table": TABLE,
            "column": "saldo_mes", "data_kind": "synthetic_test_data"}
    denial = authorize("aggregate_balance")
    if denial:
        return {**base, **denial, "sql_executed": False}
    try:
        with connect_readonly() as conn:
            identity = conn.execute("SELECT current_user, current_setting('transaction_read_only')").fetchone()
            if identity[1] != "on":
                raise ValueError("La transacción no es de solo lectura.")
            total, rows, present, accounts = conn.execute(QUERY, PARAMS).fetchone()
            if not rows or total is None or present != rows or accounts != rows:
                raise ValueError("Sin datos suficientes o con duplicados/nulos para el corte.")
            base.update(status="ok", total=format(total, ".2f"), rows=rows,
                        accounts=accounts, db_user=identity[0], transaction_read_only=identity[1])
    except ImportError:
        base.update(status="unavailable", error_code="missing_driver")
    except (ValueError, TypeError):
        base.update(status="unavailable", error_code="invalid_configuration_or_data")
    except Exception:
        # No se registran excepciones de libpq: podrían contener datos de conexión.
        base.update(status="unavailable", error_code="database_unavailable")
    base["seconds"] = time.perf_counter() - started
    return base


def format_balance(result):
    if result["status"] == "denied":
        return result["message"]
    if result.get("operation") == "own_balance":
        if result["status"] != "ok":
            return "Saldo propio sin verificar: la base no está disponible o no hay registros autorizados."
        return "\n".join(f"Producto: {r['product']}. Corte: {r['cutoff']}. Saldo: {r['balance']}." for r in result["accounts"]) + "\nDatos sintéticos; moneda no definida. Origen: cfg.fact_sdos_captacoloca_m."
    if result["status"] != "ok":
        return ("No puedo calcular el saldo: no hay una conexión utilizable a la base de datos "
                "PostgreSQL o la consulta no dispone de datos válidos. No se obtuvo ningún total. "
                f"Diagnóstico: {result['error_code']}.")
    return (f"Saldo total de {result['product']}: {result['total']}.\n"
            f"Corte: {result['cutoff']}.\n"
            f"Origen: {result['table']}, columna {result['column']}; {result['rows']} cuentas.\n"
            "Datos sintéticos de prueba; moneda no documentada.\n"
            "Definición: referencias/001_dimensiones_cliente_producto_txn (1).sql:95; "
            "referencias/003_generar_transacciones_y_saldos_feb_jun_2026 (1).sql:142-143,324-342.")


def query_own_balance():
    denial = authorize("own_balance")
    if denial:
        return {**denial, "operation": "own_balance", "sql_executed": False}
    result = dict(operation="own_balance", status="unavailable", error_code="database_unavailable")
    try:
        with connect_client() as conn:
            rows = conn.execute("""SELECT cve_producto, fec_informacion, saldo_mes
                FROM cliente.saldos
                WHERE num_cte=%s AND cve_producto=%s AND fec_informacion=%s""",
                (SESSION.get().num_cte, "AHORRO", 20260630)).fetchall()
            if rows and all(r[2] is not None for r in rows):
                result = dict(status="ok", operation="own_balance", accounts=[
                    dict(product=r[0], cutoff="2026-06-30", balance=format(r[2], ".2f")) for r in rows],
                    scope="num_cte = identidad inmutable de sesión", data_kind="synthetic_test_data")
    except Exception:
        pass
    return result
