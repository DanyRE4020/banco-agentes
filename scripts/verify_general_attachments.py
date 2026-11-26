"""Prueba real de lector y hub, solo fixtures; no abre .env ni exporta contenido.

Steward se verifica con recuperación local real en el último caso si hay credencial
APP_DB_PASSWORD en el entorno. Salida cerrada: estados, IDs y métricas.
"""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from banco_agentes.conversation import Conversation
from banco_agentes.documents import DocumentStore, ROOT
from banco_agentes.governance import DemoSession
from banco_agentes.providers import MeteredGemini
from banco_agentes.runtime import Trace


async def verify():
    key = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
    output = {"lector": "Gemini real", "hub": "Gemini real", "steward": "RAG local real", "cases": []}
    if not key:
        return {**output, "pending": "Falta credencial Gemini en el entorno autorizado; no se leyeron archivos de secretos."}
    fixtures = ROOT/"fixtures/documents/genericos"
    cases = [("contrato_bancario.pdf", "¿Qué comisión de cancelación establece mi contrato bancario?", "contrato_bancario"),
             ("comprobante.png", "¿Qué importe figura en el comprobante de transferencia bancaria?", "comprobante"),
             ("clima.png", "¿Qué clima hará mañana?", "fuera_de_alcance"),
             ("ilegible.pdf", "¿Qué información bancaria contiene este documento?", "ilegible"),
             ("clima.png", "¿Cómo solicito Crédito Nómina?", "politica")]
    def local_connection():
        import psycopg
        host = os.getenv("APP_DB_HOST", "127.0.0.1")
        if host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("PostgreSQL debe ser local.")
        return psycopg.connect(host=host, port=int(os.getenv("APP_DB_PORT", "55432")),
            dbname=os.getenv("APP_DB_NAME", "banco"), user=os.getenv("APP_DB_USER", "banco_lectura"),
            password=os.environ["APP_DB_PASSWORD"], connect_timeout=3,
            options="-c default_transaction_read_only=on -c statement_timeout=5000")
    with tempfile.TemporaryDirectory() as folder, patch("banco_agentes.rag.connect_readonly", local_connection):
        store = DocumentStore(Path(folder))
        for index, (name, question, expected) in enumerate(cases):
            case = {"case": expected, "ok": False}
            output["cases"].append(case)
            if expected == "politica" and not os.getenv("APP_DB_PASSWORD"):
                case["pending"] = "Falta APP_DB_PASSWORD en el entorno para Steward real; no se leyeron secretos."
                continue
            scope = str(index)
            record = store.save_many("synthetic-pilot", scope, [(fixtures/name).read_bytes()], names=[name])[0]
            trace = Trace(lambda _: None)
            models = []
            def model_factory(agent):
                model = MeteredGemini(agent_name=agent, trace=trace, api_key=key, timeout=90,
                    model=os.getenv("BANCO_HUB_MODEL") or os.getenv("BANCO_DOCUMENT_MODEL", "gemini-3.5-flash-lite"))
                models.append(model)
                return model
            conversation = Conversation(DemoSession(profile="Cliente", customer_id=1, account_id=1), model_factory=model_factory)
            try:
                async with asyncio.timeout(190):
                    result = await conversation.ask_documents(question, store=store, file_ids=[record["file_id"]],
                        owner="synthetic-pilot", conversation_id=scope)
                decision = result["attachment_decision"] or {}
                no_spokes = not any(e.get("agent") == "data_steward" for e in result["trace"])
                docs = result["documents"]
                case.update(file_id=record["file_id"], metrics=store.records[record["file_id"]]["metrics"],
                    error_codes=[400] if any("HTTP 400" in e["mensaje"] for e in result["document_errors"]) else [])
                case["reader_error"] = store.records[record["file_id"]]["error"]
                if expected == "fuera_de_alcance":
                    case["ok"] = decision.get("alcance") == expected and no_spokes and not result["document_errors"]
                elif expected == "ilegible":
                    case["ok"] = any("ilegible" in e["mensaje"] for e in result["document_errors"]) and no_spokes
                elif expected == "politica":
                    case["ok"] = decision.get("alcance") == "bancario" and bool(result["policy"]) and not no_spokes and "no aporta evidencia" in result["answer"]
                else:
                    case["ok"] = bool(docs) and docs[0]["tipo"] == expected and bool(decision.get("evidencia_ids")) and name in result["answer"]
                if not case["ok"]:
                    case["pending"] = "No se confirmó la lectura/decisión esperada; revisar proveedor o RAG local. No se exportó contenido."
            except Exception as error:
                case.update(error_type=type(error).__name__, pending="La prueba real no terminó; no se exportó el error bruto.")
            finally:
                for model in models:
                    if "api_client" in model.__dict__:
                        await model.api_client.aio.aclose()
                        model.api_client.close()
    return output


if __name__ == "__main__":
    result = asyncio.run(verify())
    (ROOT/"verification-general-attachments-real.json").write_text(json.dumps(result, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=True))
    sys.exit(0 if result["cases"] and all(case["ok"] for case in result["cases"]) else 1)
