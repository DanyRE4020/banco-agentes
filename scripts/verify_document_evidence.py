"""Piloto REAL con fixtures sintéticos: evaluación conjunta y lectura de un campo. No guarda datos personales."""
import os
import argparse
import json
from pathlib import Path
from urllib.parse import urlsplit

import httpx

ROOT = Path(__file__).resolve().parent.parent


def verify(base_url):
    url = urlsplit(base_url)
    if url.scheme != "http" or url.hostname not in {"127.0.0.1", "localhost", "::1"} or url.username or url.password or url.query or url.fragment:
        raise ValueError("Solo API HTTP local.")
    out = {"provider": "Gemini real", "policy": "Steward/RAG local real", "requests": []}
    with httpx.Client(base_url=base_url, timeout=240, trust_env=False) as client:
        def post(path, **kwargs):
            response = client.post(path, **kwargs)
            out["requests"].append({"path": path, "status": response.status_code})
            response.raise_for_status()
            return response.json()
        post("/api/login", json={"username": os.environ.get("BANCO_CLIENTE_USERNAME", "cliente"), "password": os.environ["BANCO_CLIENTE_PASSWORD"]})
        cid = post("/api/conversations")["conversation_id"]
        names = ("registro_personas.pdf", "documento_servicio.jpg")
        upload = post("/api/documents", data={"conversation_id": cid},
            files=[("files", (name, (ROOT/"fixtures/documents/evidencia"/name).read_bytes())) for name in names])
        ids = [d["file_id"] for d in upload["documents"]]
        response = post("/api/chat", json={"conversation_id": cid, "document_ids": ids, "question": "¿Cumplo los requisitos de Clásica?"})
        out.update(file_ids=ids, errors=[{"file_id":e["file_id"], "tipo":e.get("tipo")} for e in response.get("document_errors", [])],
            metrics=response.get("metrics"), classifications=[d["tipo"] for d in response.get("documents", [])],
            policy_id=(response.get("policy") or {}).get("id"), derived_id=(response.get("derived") or {}).get("id"),
            requirements=[{"rule":r["regla"],"status":r["estado"],"reason_code":r.get("motivo"),
                "document_ids":list(dict.fromkeys(e["documento_id"] for e in r["evidencia"]))} for r in (response.get("derived") or {}).get("requisitos", [])])
        if out["errors"]:
            out["pending"] = "El proveedor o la lectura falló; no se reintenta el archivo ni se atribuye el fallo al evaluador."
            return out
        receipt = next((d for d in response.get("documents", []) if d["tipo"] == "comprobante"), None)
        if not receipt or not out["requirements"]:
            out["pending"] = "Falta confirmar clasificación/recuperación de requisitos con el proveedor real."
            return out
        read = post("/api/chat", json={"conversation_id": cid, "document_ids": [receipt["file_id"]],
            "question": "¿Cuál es la fecha de emisión del comprobante?"})
        value = receipt["campos"].get("fecha_emision", {}).get("valor")
        out["read_check"] = {"answers_requested_field": bool(value) and value in read.get("answer", ""),
            "no_evaluation": read.get("derived") is None, "no_unneeded_confirmation": not (read.get("attachment_view") or {}).get("confirmations"),
            "metrics": read.get("metrics")}
        out["ok"] = out["classifications"] == ["identificacion", "comprobante"] and all(
            out["read_check"][key] for key in ("answers_requested_field", "no_evaluation", "no_unneeded_confirmation"))
    return out


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8004")
    args = parser.parse_args()
    try:
        result = verify(args.base_url)
    except Exception as error:
        result = {"error_type": type(error).__name__, "pending": "La prueba real no se completó; no se exportó el error bruto."}
    (ROOT/"verification-document-evidence-real.json").write_text(json.dumps(result, ensure_ascii=False, indent=2)+"\n", encoding="utf8")
    print(json.dumps(result, ensure_ascii=True))
    raise SystemExit(0 if result.get("ok") else 1)
