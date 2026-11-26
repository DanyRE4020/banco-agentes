"""Piloto real local: solo fixtures sintéticos; exporta IDs, comprobaciones y métricas."""
import os
import argparse
import json
from pathlib import Path
from urllib.parse import urlsplit

import httpx

ROOT = Path(__file__).resolve().parent.parent


def verify(base, only=None):
    url = urlsplit(base)
    if url.scheme != "http" or url.hostname not in {"127.0.0.1", "localhost", "::1"} or url.username or url.password or url.query or url.fragment:
        raise ValueError("Solo API HTTP local.")
    out = {"provider": "Gemini real cuando hace falta OCR/selección semántica", "cases": []}
    cases = {
        "digital.pdf": [("¿Qué domicilio aparece en esta identificación?", "Calle Ensayo 42", 3),
            ("¿De qué color es la carpeta del documento?", "Verde oliva", 3),
            ("¿Qué fecha de referencia aparece en el documento?", "mayo de 2031", 2),
            ("¿Qué día atienden presencialmente según el documento?", "martes", 2)],
        "escaneado.pdf": [("¿Qué domicilio aparece en esta identificación?", "Calle Ensayo 42", 3),
            ("¿De qué color es la carpeta del documento?", "Verde oliva", 3)],
        "imagen.png": [("¿Qué domicilio aparece en esta identificación?", "Calle Ensayo 42", None),
            ("¿De qué color es la carpeta del documento?", "Verde oliva", None)],
        "imagen.jpg": [("¿Qué domicilio aparece en esta identificación?", "Calle Ensayo 42", None)],
        "ilegible.png": [("¿Qué domicilio aparece en la imagen?", "ilegible", None)],
    }
    if only:
        cases = {only: cases[only]}
    with httpx.Client(base_url=base, timeout=180, trust_env=False) as client:
        login = client.post("/api/login", json={"username": os.environ.get("BANCO_CLIENTE_USERNAME", "cliente"), "password": os.environ["BANCO_CLIENTE_PASSWORD"]})
        login.raise_for_status()
        for name, queries in cases.items():
            case = {"fixture": name, "turns": []}
            out["cases"].append(case)
            cid = client.post("/api/conversations").json()["conversation_id"]
            upload = client.post("/api/documents", data={"conversation_id": cid}, files=[("files", (name,
                (ROOT/"fixtures/documents/lectura_completa"/name).read_bytes()))])
            case["upload_status"] = upload.status_code
            upload.raise_for_status()
            ids = [d["file_id"] for d in upload.json()["documents"]]
            case["file_ids"] = ids
            for question, expected, page in queries:
                response = client.post("/api/chat", json={"conversation_id": cid, "document_ids": ids, "question": question})
                response.raise_for_status()
                data = response.json()
                answer = data.get("answer", "")
                check = {"http_status": response.status_code,
                    "requested_value_found": expected.casefold() in answer.casefold(),
                    "reference_ok": (f"página {page}" in answer if page else "página" not in answer),
                    "no_spokes": data.get("agents") == ["orquestador"],
                    "reader_only": data.get("tools") == ["leer_documento"],
                    "no_evaluation": data.get("derived") is None,
                    "no_unrequested_personal_data": "Persona Ficticia Nube" not in answer and "GODE561231HDFRRN09" not in answer,
                    "coverage": [d.get("lectura") for d in data.get("documents", [])],
                    "errors": [{"tipo": e.get("tipo"), "mensaje": e.get("mensaje")} for e in data.get("document_errors", [])],
                    "metrics": data.get("metrics")}
                case["turns"].append(check)
                check["ok"] = all(check[k] for k in ("requested_value_found", "reference_ok", "no_spokes", "reader_only", "no_evaluation", "no_unrequested_personal_data")) and not check["errors"]
    out["ok"] = all(turn["ok"] for case in out["cases"] for turn in case["turns"])
    return out


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8004")
    parser.add_argument("--fixture", choices=["digital.pdf", "escaneado.pdf", "imagen.png", "imagen.jpg", "ilegible.png"])
    args = parser.parse_args()
    try:
        result = verify(args.base_url, args.fixture)
    except Exception as error:
        result = {"ok": False, "error_type": type(error).__name__,
            "pending": "No se completó la corrida real. Ejecutar el mismo comando contra la API local; no se guardó el error bruto."}
    output = "verification-full-reading-real" + ("-"+args.fixture.replace(".", "-") if args.fixture else "") + ".json"
    (ROOT/output).write_text(json.dumps(result, ensure_ascii=False, indent=2)+"\n", encoding="utf8")
    print(json.dumps({"ok": result.get("ok"), "cases": [{"fixture": c["fixture"], "checks": [t["ok"] for t in c["turns"]]} for c in result.get("cases", [])],
        "error_type": result.get("error_type")}, ensure_ascii=True))
    raise SystemExit(0 if result.get("ok") else 1)
