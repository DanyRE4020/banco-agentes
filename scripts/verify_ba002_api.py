"""Verificación real BA-002 en API local; no publica experimentos ni calificaciones."""
import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from banco_agentes.evaluation.data import Case, load_cases
from banco_agentes.evaluation.reference import sql_reference
from banco_agentes.evaluation.transport import LocalAPI, correlate, sanitize
from banco_agentes.presentation import decimal_text


def validate(observation, record, reference, month):
    assert observation["http_status"] == 200, "La API no completó el turno."
    assert record is not None, "No se encontró la auditoría del turno."
    assert reference["status"] == "ok", "No se obtuvo referencia independiente de PostgreSQL."
    data, events = observation["response"], record["trace"]
    assert not any(e["event"] == "turn_timeout_partial" for e in events), "Turno parcial."
    assert {"data_analytics", "data_graph"} <= set(data["agents"]), "Faltan especialistas."
    assert {"consultar_datos", "preparar_grafica"} <= set(data["tools"]), "Faltan herramientas."
    assert len(data["charts"]) == 1, "Se requiere una sola gráfica."
    chart = data["charts"][0]
    assert chart["type"] == "barras" and len(chart["points"]) == 2, "Se requieren dos barras."
    assert chart["period"] == f"2026-{month:02d}", "Corte incorrecto."
    values = {f["product"]: Decimal(f["value"]) for f in reference["facts"]}
    assert {p["label"] for p in chart["points"]} == {"CHEQUES", "AHORRO"}, "Productos incorrectos."
    assert all(Decimal(str(p["y"])) == values[p["label"]] for p in chart["points"]), "Importes distintos de SQL."
    difference = abs(values["CHEQUES"] - values["AHORRO"])
    assert decimal_text(difference) in data["answer"] and "CHEQUES" in data["answer"], "Falta la diferencia o el mayor."
    if month == 5:
        assert difference == Decimal("26579648.90"), "La diferencia SQL no coincide con la referencia solicitada."
    ids = set(chart["result_ids"])
    plans = [e for e in events if e["event"] == "validated_plan" and e.get("id") in ids]
    assert len(plans) == 2 and all(e["status"] == "ok" and e["sql_executed"] for e in plans)
    assert all(e["plan"]["desde"] == e["plan"]["hasta"] == f"2026-{month:02d}" for e in plans)
    assert {e["plan"]["producto"] for e in plans} == {"CHEQUES", "AHORRO"}
    graph_input = [e for e in events if e["event"] == "graph_input"][-1]
    selection = [e for e in events if e["event"] == "graph_selection"][-1]
    prepared = [e for e in events if e["event"] == "chart_prepared"][-1]
    assert ids == set(graph_input["result_ids"]) == set(selection["result_ids"])
    assert prepared["chart"] == chart and prepared["sql_executed"] is False
    return {"passed": True, "difference": str(difference), "period": chart["period"],
            "values": {k: str(v) for k, v in values.items()}, "result_ids": sorted(ids)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8001")
    parser.add_argument("--report", type=Path, default=ROOT / "verification-ba002-live.json")
    args = parser.parse_args()
    cases = {c.id: c for c in load_cases(ROOT / "evaluacion_banco_agentes.csv")}
    # Variante pedida para esta comprobación; no escribe ni publica el dataset.
    case = Case({**cases["BA-002"].row, "pregunta": "¿y en mayo?"})
    report = {"timestamp_utc": datetime.now(timezone.utc).isoformat(),
              "base_url": args.base_url, "published": False,
              "csv_sha256": hashlib.sha256((ROOT / "evaluacion_banco_agentes.csv").read_bytes()).hexdigest(),
              "input_variant": "Mismos turnos previos; seguimiento exacto solicitado: ¿y en mayo?",
              "sql_reference": {}, "turns": [], "passed": False}
    for name, number in (("june", "BA-001"), ("may", "BA-002")):
        report["sql_reference"][name] = sql_reference(cases[number])
    audit = ROOT / "verification-web.jsonl"
    result = LocalAPI(args.base_url, audit).run(case)
    report["runner_result"] = result
    # Correlación independiente con la misma función que utiliza el runner.
    records = []
    if audit.exists():
        for line in audit.read_text(encoding="utf-8").splitlines():
            try:
                records.append(json.loads(line))
            except ValueError:
                continue
    observations = [("june", 6, o) for o in result.get("history", [])]
    if result.get("final"):
        observations.append(("may", 5, result["final"]))
    for name, month, observation in observations:
        record, match = correlate(observation["response"], records,
                                  question=observation["question"], profile="Analista")
        entry = {"turn": name, "http_status": observation["http_status"],
                 "seconds": observation["seconds"], "correlation": match,
                 "response": observation["response"], "trace": record["trace"] if record else [],
                 "passed": False}
        entry["provider_429"] = [{k: e[k] for k in ("agent", "attempt", "error_type", "error_code",
                                                   "retry_delay_seconds", "retry_delay_source", "quota") if k in e}
                                 for e in entry["trace"] if e.get("event") == "model_call" and e.get("error_code") == 429]
        entry["retries"] = [e for e in entry["trace"] if e["event"] == "model_retry"]
        entry["query_count"] = sum(e["event"] == "validated_plan" and e.get("sql_executed") is True for e in entry["trace"])
        if observation["http_status"] == 503:
            entry["error_contract_ok"] = set(observation["response"]) == {"detail", "trace_id", "metrics"}
        try:
            assert match["status"] == "ok" and match["method"] == "trace_id", "No se pudo correlacionar por trace_id."
            entry["checks"] = validate(observation, record, report["sql_reference"][name], month)
            entry["passed"] = True
        except (AssertionError, KeyError, IndexError) as error:
            entry["check_error"] = str(error)
        report["turns"].append(entry)
        print(json.dumps({k: entry[k] for k in ("turn", "http_status", "seconds", "correlation", "passed", "provider_429", "retries")}, ensure_ascii=True), flush=True)
    if len(report["turns"]) == 2 and all(t["passed"] for t in report["turns"]):
        june, may = report["turns"]
        session = lambda t: next(e["session_id"] for e in t["trace"] if e["event"] == "conversation_session")
        report["same_conversation"] = session(june) == session(may)
        report["fresh_result_ids"] = not set(june["checks"]["result_ids"]) & set(may["checks"]["result_ids"])
        report["passed"] = report["same_conversation"] and report["fresh_result_ids"]
    elif not result.get("final"):
        report["pending"] = "El turno inicial no terminó correctamente; no se envió mayo sin un contexto válido."
    args.report.write_text(json.dumps(sanitize(report), ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(args.report), "passed": report["passed"]}, ensure_ascii=True))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

