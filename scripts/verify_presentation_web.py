"""Prueba la presentación real por la API del chat web, con Ollama y SQL directo."""
import os
import argparse
import json
import time
from decimal import Decimal
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]


def main():
    import sys
    sys.path.insert(0, str(ROOT))
    from banco_agentes.analytics import connect_readonly

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=("ahorro",), help="Repite solo la comprobación independiente")
    selected = parser.parse_args().case
    output = ROOT / ("verification-presentation-live-" + selected + ".json" if selected else "verification-presentation-live.json")
    report = {"passed": False, "transport": "API del chat web", "cases": [], "sql": {}}
    started = time.perf_counter()
    try:
        with connect_readonly() as db:
            report["db_user"], report["read_only"] = db.execute(
                "SELECT current_user,current_setting('transaction_read_only')").fetchone()
            assert (report["db_user"], report["read_only"]) == ("banco_lectura", "on")
            report["read_permissions"] = list(db.execute(
                "SELECT has_table_privilege(current_user,'dim.dim_cliente','SELECT'),"
                "has_table_privilege(current_user,'cfg.fact_sdos_captacoloca_m','SELECT')").fetchone())
            report["views"] = db.execute("SELECT table_schema,table_name FROM information_schema.views "
                                        "WHERE table_schema IN ('dim','cfg','public')").fetchall()
            for label, product, cut, person in (
                ("cheques", "CHEQUES", 20260331, ""),
                ("cheques_pf", "CHEQUES", 20260331, "PF"),
                ("ahorro", "AHORRO", 20260430, ""),
            ):
                value, rows = db.execute(
                    "SELECT sum(b.saldo_mes),count(*) FROM cfg.fact_sdos_captacoloca_m b "
                    "JOIN dim.dim_cliente c ON b.num_cte=c.num_cte "
                    "WHERE b.cve_producto=%s AND b.fec_informacion=%s AND (%s='' OR c.tipo_persona=%s)",
                    (product, cut, person, person)).fetchone()
                report["sql"][label] = {"total": format(value, "f"), "rows": rows}
        with httpx.Client(base_url="http://127.0.0.1:8000", trust_env=False, timeout=480) as web:
            response = web.post("/api/login", json={"username": os.environ.get("BANCO_EMPLEADO_USERNAME", "analista"), "password": os.environ["BANCO_EMPLEADO_PASSWORD"]})
            response.raise_for_status()
            chat = web.post("/api/conversations", json={}).json()["conversation_id"]
            cases = [
                ("cheques", "¿Cuál es el saldo total de CHEQUES al cierre de marzo de 2026?"),
                ("cheques_pf", "¿Y solo para personas físicas?"),
                ("ahorro", "¿Cuál es el saldo total de AHORRO al cierre de abril de 2026 para todos los tipos de persona?"),
            ]
            if selected:
                cases = [case for case in cases if case[0] == selected]
            for label, question in cases:
                if label == "ahorro":
                    chat = web.post("/api/conversations", json={}).json()["conversation_id"]
                entry = {"case": label, "question": question, "passed": False}
                tick = time.perf_counter()
                print("Ejecutando " + question, flush=True)
                try:
                    response = web.post("/api/chat", json={"question": question, "conversation_id": chat})
                    entry["http_status"] = response.status_code
                    entry["response"] = response.json()
                    response.raise_for_status()
                    data = response.json()
                    audits = [json.loads(line) for line in (ROOT / "verification-web.jsonl").read_text(encoding="utf-8").splitlines()]
                    entry["audit"] = next(row for row in reversed(audits) if row["question"] == question)
                    verified = [row for row in entry["audit"]["trace"]
                                if row["event"] == "validated_plan" and row["status"] == "ok"]
                    assert len(verified) == 1, data["answer"]
                    actual = verified[0]
                    assert actual["rows"][0]["valor"] == report["sql"][label]["total"], actual
                    assert actual["rows"][0]["filas"] == report["sql"][label]["rows"], actual
                    assert actual["plan"]["persona"] == ("PF" if label == "cheques_pf" else "")
                    expected_number = report["sql"][label]["total"]
                    integer, _, fraction = expected_number.partition(".")
                    grouped = f"{int(integer):,}".replace(",", ".") + "," + fraction
                    assert grouped in data["answer"], data["answer"]
                    assert data["answer"].startswith("El saldo total de "), data["answer"]
                    assert all(word not in data["answer"] for word in ("R1", "R2", "observados", "cfg.", "data_analytics", "Fuentes SQL"))
                    assert data["sources"] and data["query_details"]
                    assert any(source["kind"] == "table" for source in data["sources"])
                    assert ("R1" in data["query_details"]) or ("R2" in data["query_details"])
                    if label == "cheques_pf":
                        assert entry["audit"]["conversation_reused"] is True
                        snapshot = next(row for row in entry["audit"]["trace"] if row["event"] == "context_snapshot")
                        assert snapshot["results"][-1]["plan"]["producto"] == "CHEQUES"
                        assert snapshot["results"][-1]["plan"]["hasta"] == "2026-03"
                    entry["passed"] = True
                except Exception as error:
                    entry["error"] = f"{type(error).__name__}: {error}"
                entry["elapsed_seconds"] = time.perf_counter()-tick
                report["cases"].append(entry)
                output.write_text(json.dumps(report,ensure_ascii=False,indent=2), encoding="utf-8")
                print(json.dumps({key: entry[key] for key in ("case","passed","elapsed_seconds")}), flush=True)
                print(entry.get("response",{}).get("answer",entry.get("error","")), flush=True)
            web.post("/api/logout", json={}).raise_for_status()
        report["passed"] = all(entry["passed"] for entry in report["cases"])
    finally:
        report["total_seconds"] = time.perf_counter()-started
        output.write_text(json.dumps(report,ensure_ascii=False,indent=2), encoding="utf-8")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
