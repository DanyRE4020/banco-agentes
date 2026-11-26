"""Regresión de Graph por API real: SQL directo, trazas, valores y seguimientos."""
import os
import argparse
import json
import sys
import time
from contextlib import suppress
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlparse

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from banco_agentes.analytics import connect_readonly
from banco_agentes.presentation import decimal_text

MODEL = "gemini-3.5-flash-lite"


def sql_reference():
    # Usa la conexión normal de la aplicación; no imprime configuración ni credenciales.
    with connect_readonly() as db:
        user, readonly = db.execute("SELECT current_user, current_setting('transaction_read_only')").fetchone()
        assert user == "banco_lectura" and readonly == "on", "Se requiere banco_lectura y solo lectura."
        rows = db.execute(
            "SELECT cve_producto, fec_informacion / 100 AS mes, sum(saldo_mes) "
            "FROM cfg.fact_sdos_captacoloca_m WHERE cve_producto = ANY(%s) "
            "AND fec_informacion BETWEEN %s AND %s GROUP BY cve_producto, mes ORDER BY cve_producto, mes",
            (["CHEQUES", "AHORRO", "TDC", "C_PERSONAL"], 20260101, 20260630)).fetchall()
    return {f"{product}:{month}": str(value) for product, month, value in rows}


def audit_record(question, timestamp, offset):
    path = ROOT / "verification-web.jsonl"
    if not path.exists():
        return None
    with path.open("rb") as handle:
        handle.seek(offset)
        lines = handle.read().decode("utf-8").splitlines()
    for line in reversed(lines):
        record = json.loads(line)
        if record.get("question") == question and (not timestamp or record.get("metrics", {}).get("timestamp_utc") == timestamp):
            return record
    return None


def validate(entry, reference, products, kind, initial=None):
    data = entry["response"]
    calls = data["metrics"]["calls"]
    assert calls and all(c["model"] == MODEL and c["provider"] == "gemini" and c["status"] == "completed" for c in calls), "Modelo o estado inesperado."
    assert data["metrics"]["status"] == "ok" and data["metrics"]["usage_complete"], "Ejecución parcial."
    events = entry["trace"]
    attempts = [e for e in events if e["event"] == "validated_plan" and e.get("agent") == "data_analytics"]
    selected_ids = set(data.get("presentation", {}).get("result_ids", []))
    plans = [e for e in attempts if e.get("id") in selected_ids]
    entry["unselected_attempts"] = [e for e in attempts if e.get("id") not in selected_ids]
    assert plans and {e["id"] for e in plans} == selected_ids, "Faltan IDs seleccionados en la evidencia del turno."
    assert all(e["status"] == "ok" and e["sql_executed"] for e in plans), "Faltan consultas verificadas."
    assert all(e["plan"]["metrica"] == "total" and e["plan"]["campo"] == "saldo_mes" for e in plans), "Métrica incorrecta."
    assert reference, "SQL directo no disponible: los importes quedan sin contraste independiente."
    for plan in plans:
        p = plan["plan"]
        assert p["producto"] in products, "Producto inesperado."
        for row in plan["rows"]:
            month = row.get("mes", 202606)
            assert Decimal(str(row["valor"])) == Decimal(reference[f"{p['producto']}:{month}"]), "Importe distinto del SQL directo."
    if kind == "tabla":
        assert "data_graph" not in data["agents"] and "preparar_grafica" not in data["tools"], "Una tabla llamó a Graph."
        assert not data["charts"] and data["tables"], "Falta la tabla."
        rows = data["tables"][0]["rows"]
        assert {row["producto"] for row in rows} == set(products)
        for row in rows:
            assert row["valor"] == decimal_text(reference[f"{row['producto']}:202606"])
        return
    assert "data_graph" in data["agents"] and "preparar_grafica" in data["tools"], "Falta la delegación real a Graph."
    assert len(data["charts"]) == 1, "Se requiere una sola gráfica."
    chart = data["charts"][0]
    assert chart["type"] == kind
    selected = [e for e in events if e["event"] == "graph_selection"][-1]["result_ids"]
    inputs = [e for e in events if e["event"] == "graph_input"][-1]
    prepared = [e for e in events if e["event"] == "chart_prepared"][-1]
    ids = [e["id"] for e in plans]
    assert set(ids) == set(selected) == set(inputs["result_ids"]) == set(chart["result_ids"]), "Se perdió un ID entre Analytics, hub y Graph."
    assert prepared["chart"] == chart and prepared["sql_executed"] is False, "La web recibió otra gráfica."
    assert all("rows" in r and "columns" in r and "units" in r and "sources" in r for r in inputs["results"])
    entry["analytics_result_ids"] = ids
    entry["hub_selected_ids"] = selected
    # charts.js construye la tabla accesible directamente de estos mismos puntos.
    entry["web_values_for_chart_and_accessible_table"] = chart["points"]
    if kind == "barras":
        assert len(chart["points"]) == 2 and {p["label"] for p in chart["points"]} == set(products)
        assert all(p["plan"]["desde"] == p["plan"]["hasta"] == "2026-06" for p in plans)
        for point in chart["points"]:
            assert Decimal(str(point["y"])) == Decimal(reference[f"{point['label']}:202606"])
        values = {p: Decimal(reference[f"{p}:202606"]) for p in products}
        winner = max(values, key=values.get)
        difference = abs(values[products[0]] - values[products[1]])
        assert winner in data["answer"] and "mayor" in data["answer"] and decimal_text(difference) in data["answer"]
        if initial:
            old = initial["response"]["charts"][0]
            assert {p["label"]: p["y"] for p in old["points"]} == {p["label"]: p["y"] for p in chart["points"]}
            assert not set(old["result_ids"]) & set(chart["result_ids"]), "Se reutilizaron IDs históricos."
            assert entry["audit"]["conversation_reused"] is True
    else:
        assert chart["period"] == "2026-01 a 2026-06" and len(chart["points"]) == 6
        for point in chart["points"]:
            assert Decimal(str(point["y"])) == Decimal(reference[f"TDC:{point['label']}"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8003")
    parser.add_argument("--report", type=Path, default=ROOT / "verification-graph-api.json")
    args = parser.parse_args()
    url = urlparse(args.base_url)
    if url.scheme != "http" or url.hostname not in {"localhost", "127.0.0.1", "::1"} or url.username or url.password:
        parser.error("La API debe ser HTTP local sin credenciales en la URL.")
    started = time.perf_counter()
    report = {"passed": False, "cases": [], "sql_direct": None}
    def save():
        report["total_seconds"] = time.perf_counter() - started
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        try:
            report["sql_direct"] = sql_reference()
        except Exception as error:
            report["sql_error_type"] = type(error).__name__
        save()
        with httpx.Client(base_url=args.base_url, trust_env=False, timeout=450) as web:
            web.post("/api/login", json={"username": os.environ.get("BANCO_EMPLEADO_USERNAME", "analista"), "password": os.environ["BANCO_EMPLEADO_PASSWORD"]}).raise_for_status()
            pair_conversation = None
            initial = None
            cases = [
                ("solo_tabla", "Dame solo una tabla con el saldo total de CHEQUES y AHORRO al cierre de junio de 2026.", ["CHEQUES", "AHORRO"], "tabla"),
                ("cheques_ahorro", "Compara en una sola gráfica de barras el saldo total de CHEQUES y AHORRO al cierre de junio de 2026. Indica cuál fue mayor y la diferencia.", ["CHEQUES", "AHORRO"], "barras"),
                ("seguimiento_ahorro", "Te faltó AHORRO en la gráfica", ["CHEQUES", "AHORRO"], "barras"),
                ("otro_par", "Compara en una sola gráfica de barras el saldo total de TDC y C_PERSONAL al cierre de junio de 2026. Indica cuál fue mayor y la diferencia.", ["TDC", "C_PERSONAL"], "barras"),
                ("linea_tdc", "Muestra en una gráfica de línea la evolución del saldo total de TDC de enero a junio de 2026.", ["TDC"], "linea"),
            ]
            try:
                for name, question, products, kind in cases:
                    entry = {"case": name, "question": question, "passed": False}
                    tick = time.perf_counter()
                    try:
                        if name == "seguimiento_ahorro" and initial is None:
                            entry["pending"] = "La solicitud inicial no terminó con gráfica; falta el contexto válido del seguimiento."
                            continue
                        conversation = pair_conversation if name == "seguimiento_ahorro" else web.post("/api/conversations", json={}).json()["conversation_id"]
                        if name == "cheques_ahorro":
                            pair_conversation = conversation
                        audit = ROOT / "verification-web.jsonl"
                        offset = audit.stat().st_size if audit.exists() else 0
                        response = web.post("/api/chat", json={"question": question, "conversation_id": conversation})
                        entry.update(http_status=response.status_code, response=response.json())
                        record = audit_record(question, response.json().get("metrics", {}).get("timestamp_utc"), offset)
                        entry["audit"] = record
                        entry["trace"] = record.get("trace", []) if record else []
                        response.raise_for_status()
                        if name == "cheques_ahorro" and response.json().get("charts"):
                            initial = entry
                        validate(entry, report["sql_direct"], products, kind, initial if name == "seguimiento_ahorro" else None)
                        entry["passed"] = True
                    except Exception as error:
                        entry["error_type"] = type(error).__name__
                        if isinstance(error, AssertionError):
                            entry["assertion"] = str(error)
                    finally:
                        entry["seconds"] = time.perf_counter() - tick
                        report["cases"].append(entry)
                        save()
                        print(json.dumps({k: entry[k] for k in ("case", "passed", "seconds", "error_type", "pending", "assertion") if k in entry}, ensure_ascii=False), flush=True)
            finally:
                with suppress(httpx.HTTPError):
                    web.post("/api/logout", json={})
        report["passed"] = all(c["passed"] for c in report["cases"]) and len(report["cases"]) == 5
    except Exception as error:
        report["error_type"] = type(error).__name__
    finally:
        save()
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
