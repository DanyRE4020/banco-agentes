"""Un único turno real con un adjunto: diagnóstico técnico, sin campos ni contenido."""
import os
import argparse
import json
from pathlib import Path
import time
from urllib.parse import urlsplit

import httpx


def diagnose(base_url, image):
    url = urlsplit(base_url)
    if url.scheme != "http" or url.hostname not in {"127.0.0.1", "localhost", "::1"} or url.username or url.password or url.query or url.fragment:
        raise ValueError("La prueba exige una API HTTP local.")
    report = {"server": base_url, "chat_requests": 0}
    with httpx.Client(base_url=base_url, timeout=180, trust_env=False) as client:
        response = client.post("/api/login", json={"username": os.environ.get("BANCO_CLIENTE_USERNAME", "cliente"), "password": os.environ["BANCO_CLIENTE_PASSWORD"]})
        response.raise_for_status()
        response = client.post("/api/conversations")
        response.raise_for_status()
        conversation = response.json()["conversation_id"]
        response = client.post("/api/documents", data={"conversation_id": conversation},
            files=[("files", (image.name, image.read_bytes(), "image/jpeg"))])
        report["upload_http_status"] = response.status_code
        response.raise_for_status()
        ids = [d["file_id"] for d in response.json()["documents"]]
        started = time.perf_counter()
        report["chat_requests"] += 1
        response = client.post("/api/chat", json={"conversation_id": conversation, "document_ids": ids,
            "question": "Conforme a esta imagen, ¿cómo estuvo el clima?"})
        report.update(chat_http_status=response.status_code, client_seconds=time.perf_counter()-started, file_ids=ids)
        result = response.json()
        metrics = result.get("metrics", {})
        report.update(trace_id=result.get("trace_id"), metrics=metrics,
            document_count=len(result.get("documents") or []),
            document_error_count=len(result.get("document_errors") or []),
            decision_scope=(result.get("attachment_decision") or {}).get("alcance"),
            agents=result.get("agents"))
        attempts = [attempt for call in metrics.get("calls", []) if call.get("agent") == "leer_documento"
                    for attempt in call.get("reader_diagnostics", {}).get("attempts", [])]
        report["observed_reader_attempts"] = len(attempts)
        report["reader_http_statuses"] = [a.get("http_status") for a in attempts]
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8004")
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("verification-reader-503-reproduction.json"))
    args = parser.parse_args()
    try:
        result = diagnose(args.base_url, args.image)
    except Exception as error:
        result = {"error_type": type(error).__name__, "pending": "No se completó el turno diagnóstico; no se exportó el mensaje bruto."}
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=True))
    # Un 503 observado es un diagnóstico válido; no provoca reintento.
    raise SystemExit(1 if "pending" in result else 0)
