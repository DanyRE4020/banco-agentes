"""Verifica Flash-Lite en el hub y Analytics mediante la API web real."""
import os
import argparse
import json
import sys
import time
from contextlib import suppress
from pathlib import Path
from urllib.parse import urlparse

import httpx

ROOT = Path(__file__).resolve().parents[1]
MODEL = "gemini-3.5-flash-lite"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8002")
    parser.add_argument("--report", type=Path, default=ROOT / "verification-model-assignment-api.json")
    args = parser.parse_args()
    url = urlparse(args.base_url)
    if url.scheme != "http" or url.hostname not in {"127.0.0.1", "localhost", "::1"} or url.username or url.password:
        parser.error("La comprobación solo admite una API HTTP local sin credenciales en la URL.")
    question = "¿Cuál fue el saldo total de AHORRO al cierre de junio de 2026?"
    report = {"passed": False, "question": question, "expected_model": MODEL}
    started = time.perf_counter()
    try:
        with httpx.Client(base_url=args.base_url, timeout=450, trust_env=False) as web:
            web.post("/api/login", json={"username": os.environ.get("BANCO_EMPLEADO_USERNAME", "analista"), "password": os.environ["BANCO_EMPLEADO_PASSWORD"]}).raise_for_status()
            try:
                response = web.post("/api/conversations", json={})
                response.raise_for_status()
                conversation = response.json()["conversation_id"]
                response = web.post("/api/chat", json={"question": question, "conversation_id": conversation})
                report.update(http_status=response.status_code, response=response.json())
                response.raise_for_status()
                data = report["response"]
                calls = data["metrics"]["calls"]
                assert {c["agent"] for c in calls} == {"orquestador", "data_analytics"}, "No intervinieron los agentes esperados."
                assert all(c["model"] == MODEL and c["provider"] == "gemini" and c["status"] == "completed"
                           for c in calls), "Modelo, proveedor o estado inesperado en la traza."
                assert "consultar_datos" in data["tools"], "Analytics no completó consultar_datos."
                assert data["metrics"]["status"] == "ok" and data["metrics"]["usage_complete"], "El turno quedó incompleto."
                assert data["answer"] and data["sources"], "Falta una respuesta con fuentes."
                report["passed"] = True
            finally:
                with suppress(httpx.HTTPError):
                    web.post("/api/logout", json={}).raise_for_status()
    except Exception as error:
        # No se vuelcan cookies, cabeceras, entorno ni mensajes arbitrarios del SDK.
        report["error_type"] = type(error).__name__
    finally:
        report["seconds"] = time.perf_counter() - started
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"passed": report["passed"], "seconds": report["seconds"],
                          "http_status": report.get("http_status"), "error_type": report.get("error_type"),
                          "calls": report.get("response", {}).get("metrics", {}).get("calls", []),
                          "report": str(args.report)}, ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
