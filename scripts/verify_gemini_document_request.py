"""Gemini real: PDF, imagen y flujo conjunto. Solo IDs/métricas y errores técnicos permitidos.

No carga archivos .env. No imprime excepciones, respuestas, prompts ni campos extraídos.
"""
import argparse
import asyncio
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch
from collections import Counter
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from banco_agentes.documents import ROOT, DocumentStore, DocumentError
from banco_agentes.conversation import Conversation
from banco_agentes.governance import DemoSession


def technical_error(error):
    """Lista cerrada: nunca copia el mensaje ni sus valores a la salida."""
    message = getattr(error, "message", "")
    message = message if isinstance(message, str) else ""
    tokens = ("additionalProperties", "maxLength", "minLength", "minimum", "maximum", "response_schema",
              "response_json_schema", "generation_config", "generationConfig", "function_calling_config",
              "automatic_function_calling", "tools", "tool_config", "mime_type", "INVALID_ARGUMENT")
    patterns = {"unknown_field": r"unknown name|unknown field|unrecognized field|cannot find field",
                "unsupported": r"not supported|unsupported|not allowed",
                "invalid_key": r"API key not valid|API_KEY_INVALID",
                "schema_complexity": r"schema.*too (?:complex|large)",
                "invalid_argument": r"invalid argument|invalid json payload"}
    code = getattr(error, "code", None)
    return {"error_type": type(error).__name__, "http_code": code if isinstance(code, int) else None,
            "status": getattr(error, "status", None) if getattr(error, "status", None) in {"INVALID_ARGUMENT", "PERMISSION_DENIED", "RESOURCE_EXHAUSTED", "UNAUTHENTICATED"} else None,
            "causes": [type(c).__name__ for c in (getattr(error, "__cause__", None), getattr(error, "os_error", None)) if c],
            "technical_tokens": [t for t in tokens if t.lower().replace("_", "") in message.lower().replace("_", "")],
            "patterns": [name for name, pattern in patterns.items() if re.search(pattern, message, re.I)]}


async def verify(args):
    from google.genai.models import AsyncModels
    result = {"sdk": __import__("importlib.metadata", fromlist=["version"]).version("google-genai"),
              "provider": "Gemini real", "calls": [], "single_documents": []}
    original = AsyncModels.generate_content

    async def observed(client, **kwargs):
        config = kwargs["config"]
        first = kwargs["contents"][0]
        inline = getattr(first, "inline_data", None)
        call = {"mime": inline.mime_type if inline else "structured_hub_context",
                "schema_mode": "json_schema" if config.response_json_schema else "schema",
                "tools_count": len(config.tools or []), "model": kwargs["model"]}
        result["calls"].append(call)
        try:
            response = await original(client, **kwargs)
            call["request_status"] = "ok"
            return response
        except Exception as error:
            call.update(technical_error(error))
            call["request_status"] = "failed"
            raise

    with tempfile.TemporaryDirectory() as folder, patch.object(AsyncModels, "generate_content", observed):
        store = DocumentStore(Path(folder))
        ids = []
        for name in ("estado_cuenta.pdf", "identificacion_vigente.png"):
            item = store.save_many("synthetic-pilot", "diagnostic", [(ROOT/"fixtures/documents"/name).read_bytes()])[0]
            ids.append(item["file_id"])
            single = {"file_id": item["file_id"]}
            result["single_documents"].append(single)
            try:
                document = await store.leer_documento(item["file_id"], "synthetic-pilot", "diagnostic")
                single.update(status="ok", tipo=document["tipo"], pending_fields=sum(f["estado"] == "por_confirmar" for f in document["campos"].values()))
            except DocumentError:
                single["status"] = "failed"
        if args.combined and all(s["status"] == "ok" for s in result["single_documents"]):
            # Mantiene la recuperación real de Steward. Nunca carga secretos de archivos.
            if not os.getenv("APP_DB_PASSWORD"):
                result["pending"] = "Falta APP_DB_PASSWORD en el proceso para probar Steward real sin leer archivos de secretos."
            else:
                import banco_agentes.rag as rag
                import psycopg
                def environment_connection():
                    host = os.getenv("APP_DB_HOST", "127.0.0.1")
                    if host not in {"127.0.0.1", "localhost", "::1"}:
                        raise ValueError("PostgreSQL debe ser local.")
                    return psycopg.connect(host=host, port=int(os.getenv("APP_DB_PORT", "55432")),
                        dbname=os.getenv("APP_DB_NAME", "banco"), user=os.getenv("APP_DB_USER", "banco_lectura"),
                        password=os.environ["APP_DB_PASSWORD"], options="-c default_transaction_read_only=on -c statement_timeout=5000", connect_timeout=3)
                from banco_agentes.providers import MeteredGemini
                from banco_agentes.runtime import Trace
                def hub_model(name):
                    return MeteredGemini(agent_name=name, trace=Trace(lambda _: None),
                        api_key=os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY"), timeout=90,
                        model=os.getenv("BANCO_HUB_MODEL") or os.getenv("BANCO_DOCUMENT_MODEL", "gemini-3.5-flash-lite"))
                conversation = Conversation(DemoSession(profile="Cliente", customer_id=1, account_id=1), model_factory=hub_model)
                with patch.object(rag, "connect_readonly", environment_connection):
                    response = await conversation.ask_documents("¿Cumplo los requisitos de Clásica?", store=store,
                        file_ids=ids, owner="synthetic-pilot", conversation_id="diagnostic")
                result["combined"] = {"document_count": len(response["documents"]),
                    "steward_called": any(e.get("agent") == "data_steward" for e in response["trace"]),
                    "policy_id": (response["policy"] or {}).get("id"), "derived_id": (response["derived"] or {}).get("id"),
                    "requirement_count": len((response["derived"] or {}).get("requisitos", []))}
                if not result["combined"]["requirement_count"]:
                    result["pending"] = "Steward real no devolvió requisitos comparables; revisar recuperación local."
        elif args.combined:
            result["pending"] = "Las lecturas individuales fallaron; no se llamó a Steward."
    return result


def verify_server(args):
    """Usa el entorno ya autorizado del servidor, sin leer claves Gemini ni de BD."""
    import httpx
    url = urlsplit(args.base_url)
    if url.scheme != "http" or url.hostname not in {"127.0.0.1", "localhost", "::1"} or url.username or url.password or url.path not in {"", "/"} or url.query or url.fragment:
        raise ValueError("El servidor de prueba debe ser HTTP local.")
    result = {"provider": "Gemini real vía API web", "base_url": args.base_url, "single_documents": [], "requests": []}
    with httpx.Client(base_url=args.base_url, timeout=450, trust_env=False) as client:
        def post(path, **kwargs):
            response = client.post(path, **kwargs)
            result["requests"].append({"path": path, "http_status": response.status_code})
            response.raise_for_status()
            return response.json()
        post("/api/login", json={"username": os.environ.get("BANCO_CLIENTE_USERNAME", "cliente"), "password": os.environ["BANCO_CLIENTE_PASSWORD"]})
        conversation = post("/api/conversations")["conversation_id"]
        ids = []
        for name, expected in (("estado_cuenta.pdf", "estado_de_cuenta"), ("identificacion_vigente.png", "identificacion")):
            upload = post("/api/documents", data={"conversation_id": conversation, "synthetic": "true"},
                          files=[("files", (name, (ROOT/"fixtures/documents"/name).read_bytes()))])
            file_id = upload["documents"][0]["file_id"]
            ids.append(file_id)
            response = post("/api/chat", json={"conversation_id": conversation, "question": "Lee el documento sintético", "document_ids": [file_id]})
            documents = response.get("documents") or []
            result["single_documents"].append({"file_id": file_id,
                "status": "ok" if len(documents) == 1 and documents[0]["tipo"] == expected else "failed",
                "provider_codes": [int(match.group(1)) for error in response.get("document_errors") or []
                                   if (match := re.search(r"HTTP (\d+)", error.get("mensaje", "")))],
                "steward_called": "data_steward" in response.get("agents", [])})
        if args.combined and all(d["status"] == "ok" for d in result["single_documents"]):
            response = post("/api/chat", json={"conversation_id": conversation, "question": "¿Cumplo los requisitos de Clásica?", "document_ids": ids})
            policy = response.get("policy") or {}
            derived = response.get("derived") or {}
            rows = derived.get("requisitos", [])
            result["combined"] = {"document_count": len(response.get("documents") or []),
                "steward_called": "data_steward" in response.get("agents", []),
                "policy_id": policy.get("id"), "derived_id": derived.get("id"),
                "requirement_count": len(rows), "status_counts": dict(Counter(row["estado"] for row in rows)),
                "each_requirement_has_evidence_and_policy": all("evidencia" in row and row.get("fuente_politica") for row in rows)}
            if not rows or not result["combined"]["steward_called"]:
                result["pending"] = "No se completó la recuperación real de Steward y la comparación por requisito."
        elif args.combined:
            result["pending"] = "Lecturas individuales fallidas; no se llamó a Steward ni se compararon requisitos."
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--combined", action="store_true")
    parser.add_argument("--base-url", help="API local con el código actualizado; prueba Gemini y Steward en el entorno del servidor.")
    parser.add_argument("--output", type=Path, default=ROOT/"verification-gemini400-request.json")
    args = parser.parse_args()
    try:
        result = verify_server(args) if args.base_url else asyncio.run(verify(args))
    except Exception as error:
        result = {"pending": "No se pudo completar la prueba real.", **technical_error(error)}
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=True))
    return 1 if result.get("pending") or any(s["status"] != "ok" for s in result.get("single_documents", [])) else 0


if __name__ == "__main__":
    sys.exit(main())
