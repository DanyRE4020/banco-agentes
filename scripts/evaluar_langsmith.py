"""Ejecuta el CSV en la API local y publica evidencia; solo la UI de LangSmith califica."""
import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from banco_agentes.evaluation.data import SMOKE_IDS, load_cases, select_cases
from banco_agentes.evaluation.publishing import ConfigurationError, publish
from banco_agentes.evaluation.reference import CSV_SHA256, sql_reference
from banco_agentes.evaluation.transport import LocalAPI, sanitize
from banco_agentes.evaluation.ui_context import prepare_ui_context


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, default=ROOT / "evaluacion_banco_agentes.csv")
    parser.add_argument("--base-url", default="http://127.0.0.1:8001")
    parser.add_argument("--audit-log", type=Path, default=ROOT / "verification-web.jsonl")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--cases", nargs="+", default=None, metavar="BA-NNN")
    selection.add_argument("--all", action="store_true", help="Ejecuta los 21 casos.")
    parser.add_argument("--publish", action="store_true", help="Activa explícitamente la publicación en LangSmith.")
    parser.add_argument("--dataset", default="evaluacion-data-agentes")
    parser.add_argument("--report", type=Path, default=None)
    args = parser.parse_args()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    destination = args.report or ROOT / f"evaluation-results-ui-{stamp}.json"
    if destination.exists():
        parser.error("El reporte ya existe; elige otro --report para conservar la primera medición.")
    report = {"dataset": args.dataset, "published": False, "cases": [], "evidence_version": "langsmith-ui-v1",
              "grading_owner": "langsmith_ui", "python_scores_enabled": False, "ui_grading_status": "no_consultado"}
    started = time.perf_counter()
    def save():
        report["seconds"] = time.perf_counter() - started
        destination.write_text(json.dumps(sanitize(report), ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        digest = hashlib.sha256(args.csv.read_bytes()).hexdigest()
        if digest != CSV_SHA256:
            raise ValueError("El CSV cambió respecto de las rúbricas revisadas; revisa las reglas sin sobrescribir las referencias.")
        cases = load_cases(args.csv)
        if len(cases) != 21:
            raise ValueError("Se esperaban 21 casos.")
        selected = cases if args.all else select_cases(cases, args.cases or SMOKE_IDS)
        report.update(csv_sha256=digest, selected_ids=[c.id for c in selected])
        api = LocalAPI(args.base_url, args.audit_log)
        def target(case):
            tick = time.perf_counter()
            output = api.run(case)
            output["sql_reference"] = sql_reference(case)
            output = prepare_ui_context(case, output)
            output["measurement_seconds"] = time.perf_counter() - tick
            output = sanitize(output)
            report["cases"].append(output)
            save()
            print(json.dumps({"case": case.id, "status": output["status"], "seconds": output["seconds"],
                              "grading_owner": "langsmith_ui", "python_scores_enabled": False}, ensure_ascii=False), flush=True)
            return output
        if args.publish:
            report["publication_requested"] = True
            result = publish(cases, selected, target, args.dataset, digest)
            report["experiment_name"] = getattr(result, "experiment_name", None)
            report["published"] = True
        else:
            for case in selected:
                target(case)
        report["completed"] = len(report["cases"]) == len(selected)
        save()
        # El código de salida describe ejecución/correlación, nunca la nota de los jueces UI.
        return 0 if report["completed"] and all(c["status"] == "ok" and c["correlation"]["status"] == "ok" for c in report["cases"]) else 1
    except Exception as error:
        # No se imprimen mensajes arbitrarios de HTTP/SDK que puedan contener claves.
        report.update(error_type=type(error).__name__, completed=False)
        if isinstance(error, ConfigurationError):
            report["configuration_error"] = str(error)
        save()
        print(json.dumps({"error_type": type(error).__name__, "report": str(destination),
                          "help": "Revisa disponibilidad de API/auditoría, dependencias, variable LANGSMITH_API_KEY y correspondencia del dataset. No se modificaron referencias."}), flush=True)
        return 2


if __name__ == "__main__":
    sys.exit(main())
