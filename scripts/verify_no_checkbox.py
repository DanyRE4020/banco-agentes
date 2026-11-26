"""Prueba real vía API local: images.jpg sin casilla; clima y requisitos de Clásica.

No lee secretos ni guarda texto, campos o números del adjunto en el diagnóstico.
"""
import os
import argparse
import json
from pathlib import Path
from urllib.parse import urlsplit

import httpx

ROOT = Path(__file__).resolve().parent.parent


def verify(base_url, image):
    url = urlsplit(base_url)
    if url.scheme != "http" or url.hostname not in {"127.0.0.1", "localhost", "::1"} or url.username or url.password or url.query or url.fragment:
        raise ValueError("La prueba exige una API HTTP local.")
    report = {"server": base_url, "reader": "Gemini real", "hub": "Gemini real", "steward": "RAG local real", "requests": [], "cases": []}
    with httpx.Client(base_url=base_url, timeout=210, trust_env=False) as client:
        def post(path, **kwargs):
            response = client.post(path, **kwargs)
            report["requests"].append({"path": path, "status": response.status_code})
            response.raise_for_status()
            return response.json()
        post("/api/login", json={"username": os.environ.get("BANCO_CLIENTE_USERNAME", "cliente"), "password": os.environ["BANCO_CLIENTE_PASSWORD"]})
        html = client.get("/").text
        report["no_checkbox"] = 'id="document-test"' not in html
        report["gemini_notice"] = "Gemini para su lectura" in html
        cid = post("/api/conversations")["conversation_id"]
        upload = post("/api/documents", data={"conversation_id": cid},
                      files=[("files", (image.name, image.read_bytes(), "image/jpeg"))])
        ids = [d["file_id"] for d in upload["documents"]]
        report["upload_without_declaration"] = len(ids) == 1
        for expected, question in (("clima", "Conforme a esta imagen, ¿cómo estuvo el clima?"),
                                   ("clasica", "¿Cuáles son los requisitos de la Tarjeta Clásica?")):
            result = post("/api/chat", json={"conversation_id": cid, "question": question, "document_ids": ids})
            decision = result.get("attachment_decision") or {}
            policy = result.get("policy") or {}
            answer = result.get("answer", "")
            case = {"case": expected, "file_ids": ids, "document_count": len(result.get("documents") or []),
                    "read_error_count": len(result.get("document_errors") or []), "scope": decision.get("alcance"),
                    "action": decision.get("accion"), "selected_evidence_count": len(decision.get("evidencia_ids") or []),
                    "agents": result.get("agents"), "policy_id": policy.get("id"),
                    "fragment_ids": policy.get("fragment_ids"), "derived_present": bool(result.get("derived")),
                    "metrics": result.get("metrics")}
            if expected == "clima":
                case["ok"] = (decision.get("alcance") == "fuera_de_alcance" and
                    "fuera del alcance bancario" in answer and "data_steward" not in (case["agents"] or []) and
                    case["read_error_count"] == 0)
            else:
                case["ok"] = (decision.get("alcance") == "bancario" and decision.get("accion") == "politica" and
                    decision.get("producto") == "clasica" and case["selected_evidence_count"] == 0 and
                    "data_steward" in (case["agents"] or []) and bool(policy.get("extractos") or policy.get("reglas")) and
                    "no aporta evidencia" in answer and case["read_error_count"] == 0)
            report["cases"].append(case)
        normal = post("/api/chat", json={"conversation_id": cid, "question": "Hola", "document_ids": []})
        report["normal_without_attachments"] = "documents" not in normal and "leer_documento" not in normal.get("tools", [])
    report["ok"] = (report["no_checkbox"] and report["gemini_notice"] and report["upload_without_declaration"] and
                    report["normal_without_attachments"] and all(case["ok"] for case in report["cases"]))
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8003")
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=ROOT/"verification-no-checkbox-api-after.json")
    args = parser.parse_args()
    try:
        result = verify(args.base_url, args.image)
    except Exception as error:
        result = {"ok": False, "error_type": type(error).__name__, "pending": "No se completó la prueba real; revisar disponibilidad de la API/modelo/RAG local."}
        if isinstance(error, httpx.HTTPStatusError):
            result["http_status"] = error.response.status_code
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=True))
    raise SystemExit(0 if result["ok"] else 1)
