"""SQL independiente frente a Data Quality con ADK/Ollama reales."""

import argparse
import asyncio
import json
import os
import time
from unittest.mock import patch

from .agents import local_settings
from .analytics import connect_readonly
from .catalog import ROOT, load_catalog
from .quality import QUESTION, TABLE, KEY, QUERY, PARAMS, format_quality
from .runtime import ask


def direct_check():
    started = time.perf_counter()
    table = next(t for t in load_catalog()["tables"] if t["name"] == TABLE)
    assert set(KEY) <= {c["name"] for c in table["columns"]}
    assert not table["constraints"] and not any(c["primary_key"] or c["unique"] for c in table["columns"])
    with connect_readonly() as conn:
        user, mode = conn.execute("SELECT current_user,current_setting('transaction_read_only')").fetchone()
        assert user == "banco_lectura" and mode == "on"
        constraints = conn.execute("""SELECT c.contype, c.conname FROM pg_catalog.pg_constraint c
            JOIN pg_catalog.pg_class t ON t.oid=c.conrelid
            JOIN pg_catalog.pg_namespace n ON n.oid=t.relnamespace
            WHERE n.nspname=%s AND t.relname=%s AND c.contype IN ('p','u')""",
            ("cfg", "fact_sdos_captacoloca_m")).fetchall()
        assert not constraints, "La base tiene una PK/UNIQUE no documentada en las referencias."
        # Método independiente: funciones ventana, sin usar la consulta GROUP BY de la herramienta.
        row = conn.execute("""WITH marked AS (
            SELECT num_cta,cve_producto,fec_informacion,
                COUNT(*) OVER (PARTITION BY num_cta,cve_producto,fec_informacion) AS n,
                ROW_NUMBER() OVER (PARTITION BY num_cta,cve_producto,fec_informacion) AS rn
            FROM cfg.fact_sdos_captacoloca_m WHERE fec_informacion=%s
        ) SELECT COUNT(*), COUNT(*) FILTER (WHERE n>1 AND rn=1),
            COUNT(*) FILTER (WHERE n>1), COUNT(*) FILTER (WHERE n>1 AND rn>1),
            COUNT(*) FILTER (WHERE num_cta IS NULL OR cve_producto IS NULL) FROM marked""", (PARAMS[0],)).fetchone()
        assert row[0] > 0, "No hay datos para comprobar el corte."
        # Prueba positiva de la consulta real con VALUES, sin INSERT/CREATE ni tocar las tablas.
        fixtures = [(1,"AHORRO",20260630)]*3 + [(1,"CHEQUES",20260630), (1,"AHORRO",20260531)]
        fixtures += [(2,"AHORRO",20260630)]*2 + [(None,"AHORRO",20260630)]*2
        fixture_sql = "WITH fixture(num_cta,cve_producto,fec_informacion) AS (VALUES "
        fixture_sql += ",".join(["(%s::int,%s::varchar,%s::int)"] * len(fixtures)) + "), "
        fixture_sql += QUERY.removeprefix("WITH ").replace(TABLE, "fixture")
        fixture_params = tuple(v for item in fixtures for v in item) + PARAMS
        fixture_result = tuple(int(v) for v in conn.execute(fixture_sql, fixture_params).fetchone())
        assert fixture_result == (8,3,7,4,2), f"Detección positiva incorrecta: {fixture_result}"
    return dict(status="passed", checked_rows=row[0], duplicate_groups=row[1], duplicate_rows=row[2],
                excess_rows=row[3], null_key_rows=row[4], db_user=user, transaction_read_only=mode,
                catalog_source=table["source"], declared_constraints=constraints,
                positive_readonly_fixture=list(fixture_result), seconds=time.perf_counter()-started)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--unavailable", action="store_true")
    args = parser.parse_args()
    report = dict(mode="real_ollama", case="quality_unavailable" if args.unavailable else "quality_duplicados_junio",
                  question=QUESTION, status="failed")
    started = time.perf_counter()
    with patch.dict(os.environ, {"APP_DB_PORT":"1"} if args.unavailable else {}):
        try:
            if not args.unavailable:
                report["direct_sql"] = direct_check()
            _, _, timeout = local_settings()
            result = await ask(QUESTION, timeout=timeout)
            report["orchestrator"] = result
            completed = {(e["agent"],e.get("tool")) for e in result["trace"] if e["event"]=="tool_completed"}
            assert ("orquestador","data_quality") in completed, "Falta AgentTool de Quality."
            assert ("data_quality","comprobar_duplicados_saldos_junio_2026") in completed, "Falta herramienta SQL de Quality."
            sql = [e for e in result["trace"] if e["event"]=="sql_result" and e["agent"]=="data_quality"]
            assert len(sql)==1
            assert format_quality(sql[0]) in result["answer"], "El reporte no conserva el resultado."
            if args.unavailable:
                assert sql[0]["status"]=="unavailable" and sql[0]["error_code"]=="database_unavailable"
                assert "duplicate_groups" not in sql[0]
                assert "no se pudo realizar" in result["answer"]
            else:
                assert sql[0]["status"]=="ok" and sql[0]["db_user"]=="banco_lectura"
                for key in ("checked_rows","duplicate_groups","duplicate_rows","excess_rows","null_key_rows"):
                    assert sql[0][key]==report["direct_sql"][key], f"SQL directo y Quality difieren: {key}"
            report["status"]="passed"
        except Exception as error:
            if hasattr(error,"run_result"):
                report["orchestrator"]=error.run_result
            report["error"] = f"{type(error).__name__}: {error}" if isinstance(error,(AssertionError,ImportError)) else type(error).__name__
    report["total_seconds"]=time.perf_counter()-started
    output=ROOT/("verification-quality-unavailable.json" if args.unavailable else "verification-quality.json")
    output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps(report,ensure_ascii=False,indent=2))
    return int(report["status"]!="passed")


if __name__=="__main__":
    raise SystemExit(asyncio.run(main()))
