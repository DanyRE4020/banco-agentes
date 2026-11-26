"""Compara SQL directo con ADK/Ollama real, o comprueba un fallo de conexión."""

import argparse
import asyncio
import json
import os
import time
from decimal import Decimal
from unittest.mock import patch

from .analytics import QUESTION, PARAMS, TABLE, connect_readonly
from .catalog import ROOT
from .agents import local_settings
from .runtime import ask


def direct_check():
    started = time.perf_counter()
    with connect_readonly() as conn:
        identity = conn.execute("SELECT current_user, current_setting('transaction_read_only')").fetchone()
        columns = conn.execute("""SELECT column_name, data_type FROM information_schema.columns
            WHERE table_schema=%s AND table_name=%s AND column_name IN
            ('saldo_mes','cve_producto','fec_informacion') ORDER BY column_name""",
            ("cfg", "fact_sdos_captacoloca_m")).fetchall()
        product = conn.execute("SELECT cve_producto, descripcion FROM dim.dim_producto WHERE cve_producto=%s",
                               (PARAMS[0],)).fetchall()
        # Consulta independiente de la herramienta: agrupa por producto y corte.
        row = conn.execute("""SELECT cve_producto, fec_informacion, SUM(saldo_mes), COUNT(*),
            COUNT(saldo_mes), COUNT(DISTINCT num_cta)
            FROM cfg.fact_sdos_captacoloca_m WHERE fec_informacion=%s
            GROUP BY cve_producto, fec_informacion HAVING cve_producto=%s""",
            (PARAMS[1], PARAMS[0])).fetchone()
        assert dict(columns) == {"saldo_mes": "numeric", "cve_producto": "character varying", "fec_informacion": "integer"}
        assert product and product[0][0] == "AHORRO"
        assert identity[1] == "on"
        assert row and row[3] == row[4] == row[5] == 5000
        return {"status": "passed", "total": format(row[2], ".2f"), "product": row[0],
                "cutoff_db": row[1], "rows": row[3], "db_user": identity[0],
                "transaction_read_only": identity[1], "columns": columns, "product_definition": product,
                "seconds": time.perf_counter() - started}


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--unavailable", action="store_true",
                        help="Usa puerto local 1 solo en este proceso para comprobar rechazo real de conexión.")
    args = parser.parse_args()
    report = {"mode": "real_ollama", "case": "analytics_unavailable" if args.unavailable else "analytics_ahorro_junio",
              "question": QUESTION, "status": "failed"}
    started = time.perf_counter()
    result = {}
    # No cambia archivos de configuración, el servicio ni su puerto real.
    with patch.dict(os.environ, {"APP_DB_PORT": "1"} if args.unavailable else {}):
        try:
            if not args.unavailable:
                report["direct_sql"] = direct_check()
            _, _, timeout = local_settings()
            result = await ask(QUESTION, timeout=timeout)
            report["orchestrator"] = result
            completed = {(e["agent"], e.get("tool")) for e in result["trace"] if e["event"] == "tool_completed"}
            assert ("orquestador", "data_analytics") in completed, "No se completó AgentTool de Analytics."
            assert ("data_analytics", "consultar_saldo_ahorro_junio_2026") in completed, "No se ejecutó la herramienta SQL."
            sql = [e for e in result["trace"] if e["event"] == "sql_result"]
            assert len(sql) == 1, "Se esperaba una consulta SQL."
            if args.unavailable:
                assert sql[0]["status"] == "unavailable" and sql[0]["error_code"] == "database_unavailable"
                assert "total" not in sql[0]
                assert "No puedo calcular" in result["answer"] and "conexión" in result["answer"]
            else:
                assert sql[0]["status"] == "ok"
                assert Decimal(report["direct_sql"]["total"]) == Decimal(sql[0]["total"]), "Totales SQL y Analytics distintos."
                assert sql[0]["total"] in result["answer"]
                assert "2026-06-30" in result["answer"] and TABLE in result["answer"]
            report["status"] = "passed"
        except Exception as error:
            if "orchestrator" not in report and hasattr(error, "run_result"):
                report["orchestrator"] = error.run_result
            # Mensajes de aserción propios son seguros; no volcar DSN/excepciones libpq.
            report["error"] = f"{type(error).__name__}: {error}" if isinstance(error, (AssertionError, ImportError)) else type(error).__name__
    report["total_seconds"] = time.perf_counter() - started
    output = ROOT / ("verification-analytics-unavailable.json" if args.unavailable else "verification-analytics.json")
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return int(report["status"] != "passed")


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
