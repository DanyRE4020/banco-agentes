"""Consultas de Cliente por la conexión dedicada y el num_cte autenticado."""
import os
from contextlib import contextmanager
from decimal import Decimal

from .governance import SESSION, authorize
from .semantics import DATASETS, DIM

ROLE = "banco_cliente"
CLIENT_TABLES = {"saldos": "cliente.saldos", "movimientos": "cliente.movimientos"}
# Columnas expuestas por las vistas restringidas de docker/postgres/client_access.sql.
CLIENT_FIELDS = {
    "saldos": ("num_cte", "num_cta", "cve_producto", "fec_informacion", "saldo_mes", "saldo_apertura"),
    "movimientos": ("num_cte", "num_cta", "cve_producto", "fec_txn", "cve_mes_txn", "cve_txn", "saldo_txn"),
}
PURCHASE_PRODUCT, PURCHASE_CODE = "TDC", 400
PURCHASE_RULE = (
    "Compras: importes de TDC con código 400 (COMPRA). "
    "Se excluyen pagos (300), disposiciones (450), depósitos (100) y retiros (200)."
)
DEFAULT_PURCHASE_PERIOD = "2026-02/2026-06"


def connect_client_socket():
    import psycopg

    host = os.environ.get("APP_DB_HOST", "127.0.0.1")
    password = os.environ.get("BANCO_CLIENT_DB_PASSWORD")
    if host not in {"127.0.0.1", "localhost", "::1"} or not password:
        raise ValueError("Falta la conexión local dedicada de Cliente.")
    return psycopg.connect(
        host=host, port=int(os.environ.get("APP_DB_PORT", "55432")),
        dbname=os.environ.get("APP_DB_NAME", "banco"), user=ROLE, password=password,
        connect_timeout=3, options="-c default_transaction_read_only=on -c statement_timeout=5000",
    )


@contextmanager
def connect_client():
    identity = SESSION.get()
    if identity is None or identity.profile != "Cliente" or type(identity.num_cte) is not int or identity.num_cte <= 0:
        raise PermissionError("Falta una identidad autenticada de Cliente.")
    with connect_client_socket() as connection:
        connection.read_only = True
        verified = connection.execute(
            "SELECT current_user, current_setting('transaction_read_only')").fetchone()
        if tuple(verified or ()) != (ROLE, "on"):
            raise PermissionError("Se requiere la conexión Cliente de solo lectura.")
        connection.execute("SELECT set_config('app.num_cte', %s, true)", (str(identity.num_cte),))
        yield connection


def own_account_numbers():
    try:
        with connect_client() as connection:
            rows = connection.execute(
                "SELECT DISTINCT num_cta FROM cliente.saldos WHERE num_cte = %s",
                (SESSION.get().num_cte,)).fetchall()
            return frozenset(row[0] for row in rows)
    except Exception:
        return frozenset()


def query_accounts():
    denial = authorize("own_analysis")
    if denial:
        return {**denial, "sql_executed": False}
    try:
        with connect_client() as connection:
            raw = connection.execute("""
                SELECT DISTINCT ON (num_cta, cve_producto)
                       num_cta, cve_producto, fec_informacion, saldo_mes
                FROM cliente.saldos
                WHERE num_cte = %s
                ORDER BY num_cta, cve_producto, fec_informacion DESC
            """, (SESSION.get().num_cte,)).fetchall()
    except Exception:
        return {"status": "unavailable", "sql_executed": False,
                "message": "No se pudieron verificar tus cuentas en PostgreSQL."}

    rows = []
    for account, product, cut, balance in raw:
        cut = str(cut)
        favor, debt = None, None
        if balance is not None:
            net = -Decimal(balance) if product in {"TDC", "C_PERSONAL"} else Decimal(balance)
            if net >= 0:
                favor = format(abs(net), "f")
            else:
                debt = format(-net, "f")
        rows.append({"cuenta": str(account), "producto": product, "corte": f"{cut[:4]}-{cut[4:6]}-{cut[6:]}",
                     "saldo_a_favor": favor, "deuda": debt})
    closing = max((r["corte"][:7] for r in rows), default="")
    plan = dict(metrica="cuentas_propias", conjunto="saldos", producto="", desde=closing, hasta=closing,
                agrupar=("cuenta", "producto", "corte"), alcance="propio", campo="saldo_mes",
                codigo=0, persona="", segmento="")
    return {"status": "ok" if rows else "insufficient", "rows": rows,
            "columns": ["cuenta", "producto", "corte", "saldo_a_favor", "deuda"], "plan": plan,
            "table": CLIENT_TABLES["saldos"], "units": "Importe sintético, moneda sin definir",
            "sources": list(DATASETS["saldos"].sources) + [f"{DIM}:82-98"],
            "rule": "Último corte disponible por cuenta. AHORRO y CHEQUES son captación. TDC y C_PERSONAL son créditos.",
            "notes": [], "sql_executed": True}


def purchase_plan(periodo, agrupar="mes"):
    start, _, end = periodo.strip().partition("/")
    return dict(metrica="total", conjunto="movimientos", producto=PURCHASE_PRODUCT, codigo=PURCHASE_CODE,
                desde=start, hasta=end or start, agrupar=agrupar, alcance="propio", campo="",
                persona="", segmento="", base="total", minimo=None, maximo=None)


def is_purchase_plan(plan):
    return plan.get("alcance") == "propio" and is_purchase_total(plan)


def is_purchase_total(plan):
    return (plan.get("conjunto") == "movimientos"
            and plan.get("metrica") == "total" and plan.get("producto") == PURCHASE_PRODUCT
            and plan.get("codigo") == PURCHASE_CODE)


def is_employee_purchase_quarter(plan, groups):
    return plan.get("alcance") == "global" and tuple(groups) == ("trimestre",) and is_purchase_total(plan)
