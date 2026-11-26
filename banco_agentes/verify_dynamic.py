"""Las preguntas son la única entrada al hub; los agentes esperados son aserciones."""

import argparse
import asyncio
import json
import time
from decimal import Decimal

from .agents import local_settings
from .analytics import connect_readonly
from .catalog import ROOT
from .runtime import ask

CASES = [
    ("solo_saldo", "¿Cuál fue el saldo total de AHORRO al cierre de junio de 2026?",
     {"data_analytics"}),
    ("conjunta", "¿Cuál fue el saldo total de AHORRO al cierre de junio de 2026 y hay registros duplicados que puedan afectar ese total?",
     {"data_analytics", "data_quality"}),
    ("parafrasis", "Para AHORRO, dime cuánto suman los saldos al 30 de junio de 2026 y revisa si alguna cuenta tiene más de un registro en ese corte",
     {"data_analytics", "data_quality"}),
]


def direct_sql():
    started = time.perf_counter()
    with connect_readonly() as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        user, readonly = conn.execute("SELECT current_user,current_setting('transaction_read_only')").fetchone()
        assert user == "banco_lectura" and readonly == "on"
        row = conn.execute("""WITH marked AS (
            SELECT saldo_mes,num_cta,cve_producto,fec_informacion,
                   COUNT(*) OVER (PARTITION BY num_cta,cve_producto,fec_informacion) AS n,
                   ROW_NUMBER() OVER (PARTITION BY num_cta,cve_producto,fec_informacion) AS rn
            FROM cfg.fact_sdos_captacoloca_m WHERE cve_producto=%s AND fec_informacion=%s
        ) SELECT SUM(saldo_mes),COUNT(*),COUNT(saldo_mes),
                 COUNT(*) FILTER (WHERE n>1 AND rn=1),COUNT(*) FILTER (WHERE n>1),
                 COUNT(*) FILTER (WHERE n>1 AND rn>1),
                 COUNT(*) FILTER (WHERE num_cta IS NULL OR cve_producto IS NULL)
          FROM marked""", ("AHORRO", 20260630)).fetchone()
        assert row[0] is not None and row[1] == row[2] and row[1] > 0
    return dict(total=format(row[0], ".2f"), rows=row[1], duplicate_groups=row[3],
                duplicate_rows=row[4], excess_rows=row[5], null_key_rows=row[6],
                product="AHORRO", cutoff="2026-06-30", db_user=user, transaction_read_only=readonly,
                seconds=time.perf_counter()-started)


def check(expected, result, baseline):
    completed = [e for e in result["trace"] if e["event"] == "tool_completed"]
    actual = {e["tool"] for e in completed if e["agent"] == "orquestador"}
    assert actual == expected, f"Delegación esperada {sorted(expected)}, observada {sorted(actual)}"
    sources = [e for e in result["trace"] if e["event"] == "sql_result"]
    assert len(sources) == len(expected), "Se esperaban consultas únicas de los agentes elegidos."
    first = next(e for e in result["trace"] if e["event"] == "model_call" and e["agent"] == "orquestador")
    assert set(first["tools_offered"]) == {"data_steward", "data_analytics", "data_quality", "data_governance"}
    for entry in result["trace"]:
        if entry["event"] != "model_call":
            continue
        calls = {}
        for message in entry["request_history"]:
            for call in message.get("calls", []):
                calls[call["id"]] = call["name"]
            if message["role"] == "tool":
                assert calls.get(message["tool_call_id"]) == message["tool_name"], "Historial sin llamada asociada."
    for source in sources:
        assert source["agent"] in expected and source["status"] == "ok"
        assert source["product"] == "AHORRO" and source["cutoff"] == "2026-06-30"
        assert source["db_user"] == "banco_lectura" and source["transaction_read_only"] == "on"
        if source["agent"] == "data_analytics":
            assert Decimal(source["total"]) == Decimal(baseline["total"])
            assert source["rows"] == baseline["rows"]
            assert any(e["agent"] == "data_analytics" and e["tool"] == "consultar_saldo_ahorro_junio_2026" for e in completed)
        else:
            assert source["checked_rows"] == baseline["rows"]
            for key in ("duplicate_groups", "duplicate_rows", "excess_rows", "null_key_rows"):
                assert source[key] == baseline[key], f"Quality difiere del SQL directo: {key}"
            assert any(e["agent"] == "data_quality" and e["tool"] == "comprobar_duplicados_saldos_junio_2026" for e in completed)
    answer = result["answer"]
    assert baseline["total"] in answer and "2026-06-30" in answer and "cfg.fact_sdos_captacoloca_m" in answer
    assert "Datos sintéticos" in answer and "moneda no documentada" in answer
    if "data_quality" in expected:
        assert f"Grupos duplicados: {baseline['duplicate_groups']}." in answer
        assert f"Filas comprobadas: {baseline['rows']}" in answer


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", choices=[case for case, _, _ in CASES])
    args = parser.parse_args()
    _, _, timeout = local_settings()
    reports = []
    for case, question, expected in CASES:
        if args.case and args.case != case:
            continue
        print(f"CASO: {case}", flush=True)
        started = time.perf_counter()
        report = dict(case=case, question=question, expected_agents=sorted(expected), status="failed")
        try:
            report["direct_sql"] = direct_sql()
            # Única entrada al orquestador: texto del usuario, sin rutas ni agentes sugeridos.
            result = await ask(question, timeout=timeout)
            report["orchestrator"] = result
            check(expected, result, report["direct_sql"])
            report["status"] = "passed"
        except Exception as error:
            if hasattr(error, "run_result"):
                report["orchestrator"] = error.run_result
            report["error"] = f"{type(error).__name__}: {error}" if isinstance(error, AssertionError) else type(error).__name__
        report["total_seconds"] = time.perf_counter()-started
        reports.append(report)
        (ROOT/f"verification-dynamic-{case}.json").write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
        (ROOT/"verification-dynamic.json").write_text(json.dumps(dict(mode="real_ollama", results=reports), ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
        print(f"{case}: {report['status']} ({report['total_seconds']:.3f} s)", flush=True)
    return int(any(r["status"] != "passed" for r in reports))


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
