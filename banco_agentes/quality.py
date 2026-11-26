"""Comprobación acotada de duplicidad lógica; no impone una clave al esquema."""

import re
import time
from pathlib import Path

from .analytics import connect_readonly
from .catalog import normalize
from .governance import authorize

QUESTION = "¿Hay registros duplicados de saldos por cuenta al cierre de junio de 2026?"
TABLE = "cfg.fact_sdos_captacoloca_m"
KEY = ("num_cta", "cve_producto", "fec_informacion")
PARAMS = (20260630, None, None)
QUERY = (Path(__file__).parent / "sql/duplicados_saldos_junio.sql").read_text(encoding="utf-8")
RULE = "Más de una fila con igual (num_cta, cve_producto, fec_informacion)."


def supports_question(question):
    words = set(re.findall(r"\w+", normalize(question)))
    return {"duplicados", "saldos", "cuenta", "cierre", "junio", "2026"} <= words and not words.intersection(
        {"enero", "febrero", "marzo", "abril", "mayo", "julio", "agosto", "septiembre",
         "octubre", "noviembre", "diciembre", "ahorro", "cheques", "tdc", "c_personal"})


def check_duplicates(product=None):
    started = time.perf_counter()
    result = dict(table=TABLE, cutoff="2026-06-30", cutoff_db=PARAMS[0], key=list(KEY),
                  product=product, rule=RULE, rule_kind="logical_not_enforced", null_grouping="NULL se agrupa con NULL")
    denial = authorize("quality")
    if denial:
        return {**result, **denial, "sql_executed": False}
    try:
        if product not in {None, "AHORRO", "CHEQUES", "TDC", "C_PERSONAL"}:
            raise ValueError("Producto no soportado.")
        with connect_readonly() as conn:
            user, mode = conn.execute("SELECT current_user, current_setting('transaction_read_only')").fetchone()
            if mode != "on":
                raise ValueError("Se requiere transacción de lectura.")
            rows, groups, affected, excess, nulls = conn.execute(QUERY, (PARAMS[0], product, product)).fetchone()
            if rows == 0:
                result.update(status="unavailable", error_code="no_data_for_cutoff")
            else:
                result.update(status="ok", checked_rows=int(rows), duplicate_groups=int(groups),
                              duplicate_rows=int(affected), excess_rows=int(excess), null_key_rows=int(nulls),
                              db_user=user, transaction_read_only=mode)
    except ImportError:
        result.update(status="unavailable", error_code="missing_driver")
    except (ValueError, TypeError):
        result.update(status="unavailable", error_code="invalid_configuration_or_data")
    except Exception:
        result.update(status="unavailable", error_code="database_unavailable")
    result["seconds"] = time.perf_counter() - started
    return result


def format_quality(result):
    if result["status"] == "denied":
        return result["message"]
    lines = [f"Regla aplicada: {RULE}",
             "Regla lógica derivada de los scripts; no es una PK/UNIQUE declarada.",
             f"Corte: {result['cutoff']}. Tabla: {result['table']}."]
    lines.append(f"Producto: {result.get('product') or 'todos los productos'}.")
    if result["status"] != "ok":
        lines.append("La comprobación no se pudo realizar: PostgreSQL no está disponible o no hay datos válidos para el corte. "
                     f"Diagnóstico: {result['error_code']}. No se obtuvo un número de grupos duplicados.")
    else:
        lines += [f"Grupos duplicados: {result['duplicate_groups']}.",
                  f"Filas comprobadas: {result['checked_rows']}; filas en grupos duplicados: {result['duplicate_rows']}; "
                  f"filas excedentes: {result['excess_rows']}.",
                  f"Filas con clave incompleta: {result['null_key_rows']} (NULL se agrupa con NULL).",
                  "El resultado se limita a esta regla y corte; no acredita la calidad global de los datos."]
    lines.append("Fuentes: referencias/002_generar_cuentas_y_saldos_enero_2026 (1).sql:30-40,83-105; "
                 "referencias/003_generar_transacciones_y_saldos_feb_jun_2026 (1).sql:142-143,324-340,383-390; "
                 "catálogo: table:cfg.fact_sdos_captacoloca_m.")
    return "\n".join(lines)
