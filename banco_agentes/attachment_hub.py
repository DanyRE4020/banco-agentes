"""Decisión del hub sobre evidencia leída; sin archivos, SQL ni herramientas del modelo."""
import json
from pathlib import Path
from typing import Literal

from google.adk.models.llm_request import LlmRequest
from google.genai import types
from pydantic import Field

from .documents import StrictModel, DocumentError, gemini_document_schema


class AttachmentDecision(StrictModel):
    alcance: Literal["bancario", "fuera_de_alcance"]
    accion: Literal["responder", "politica", "comparar", "aclarar", "medir_antiguedad", "evaluar_comprobante"]
    producto: Literal["nomina", "clasica", "general"] = "general"
    evidencia_ids: list[str] = Field(default_factory=list, max_length=16)


def evidence_items(documents):
    from .document_privacy import PERSONAL_FIELDS
    items = {}
    for doc in documents:
        for index, evidence in enumerate(doc.get("evidencia", []), 1):
            if evidence["estado"] == "confirmado":
                items[f"{doc['id']}.E{index}"] = evidence
        effective = doc  # El enrutamiento reconoce contenido; no ejecuta validaciones de elegibilidad.
        for name, field in effective["campos"].items():
            if name not in PERSONAL_FIELDS and field["estado"] == "confirmado" and field["valor"] is not None:
                source = {**doc["campos"][name]["fuente"], **field["fuente"]}
                items[f"{doc['id']}.{name}"] = {"texto": name.replace("_", " ") + ": " + field["valor"],
                    "fuente": source, "estado": field["estado"], "confianza": field["confianza"]}
    return items


async def decide_attachment(question, documents, *, previous_product=None, model_factory=None, trace):
    from .providers import create_model
    items = evidence_items(documents)
    instruction = Path(__file__).with_name("prompts").joinpath("web_attachment_hub.txt").read_text(encoding="utf-8")
    # Solo el resultado estructurado, nunca bytes, PDF completo ni instrucciones de ese contenido.
    context = {"pregunta_usuario": question, "producto_anterior": previous_product,
               "adjuntos_datos_no_confiables": [{"id": d["id"], "tipo": d["tipo"], "tema": d.get("tema", ""),
                   "paginas_relevantes": d.get("paginas_relevantes", []),
                   "campos_estado": {name: field["estado"] for name, field in d["campos"].items()}}
                   for d in documents],
               "evidencia_datos_no_confiables": items}
    request = LlmRequest(contents=[types.Content(role="user", parts=[types.Part(text=json.dumps(context, ensure_ascii=False))])],
        config=types.GenerateContentConfig(system_instruction=instruction, tools=[],
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            response_mime_type="application/json", response_json_schema=gemini_document_schema(AttachmentDecision)))
    model = None
    try:
        model = model_factory("orquestador") if model_factory else create_model("orquestador", trace, num_predict=700)
        # Runner suele asignarlo; esta llamada aislada usa directamente el adaptador ADK.
        request.model = model.model
        text = ""
        async for response in model.generate_content_async(request, stream=False):
            if response.error_code:
                raise ValueError()
            if response.content:
                text += "".join(p.text for p in response.content.parts or [] if p.text and not p.thought)
        decision = AttachmentDecision.model_validate_json(text)
        if any(key not in items for key in decision.evidencia_ids):
            raise ValueError()
        return decision
    except Exception:
        raise DocumentError("Error del orquestador al decidir sobre el adjunto; no es un rechazo por alcance ni un archivo ilegible.") from None
    finally:
        # El hub directo no tiene Runner que cierre el cliente creado para este turno.
        client = getattr(model, "__dict__", {}).get("api_client") if not model_factory else None
        if client is not None:
            await client.aio.aclose()
            client.close()


def safe_trace(records):
    """También descarta respuestas/historial que incluya el proveedor local de desarrollo."""
    allowed = {"event", "agent", "tool", "file_id", "cached", "result_id", "fragment_ids",
               "provider", "model", "status", "seconds", "usage", "attempt", "error_type", "error_code", "reader_diagnostics"}
    return [{key: value for key, value in record.items() if key in allowed} for record in records]


def cited_evidence(documents, decision, question=""):
    from .attachment_presentation import direct_fields, reference
    from .document_privacy import clean_visible_text, personal_values
    direct, _ = direct_fields(question, documents) if decision.accion == "responder" else ("", [])
    if direct:
        return direct
    items = evidence_items(documents)
    selected = list(dict.fromkeys(decision.evidencia_ids))
    paragraphs = []
    for key in selected:
        item = items[key]
        source = item["fuente"]
        citation = reference(source)
        paragraphs.append(f"{clean_visible_text(item['texto'], known_values=personal_values(documents))} ({citation})")
    return "\n\n".join(paragraphs)


def policy_text(policy):
    from .attachment_presentation import reference
    from .document_privacy import clean_visible_text
    lines = []
    for item in policy.get("extractos", []):
        s = item["fuente"]
        lines.append(f"{clean_visible_text(item['texto'])} ({reference(s)}; versión {s.get('version', 'no documentada')}; consulta {s.get('consulted_at', 'no documentada')})")
    if not lines:
        for rule in policy["reglas"]:
            label = rule["campo"].replace("_", " ")
            condition = " · " + rule["condicion"] if rule.get("condicion") else ""
            s = rule["fuente"]
            lines.append(f"{label}: {rule.get('operador', '')} {rule.get('valor', '')}{condition} ({reference(s)}; versión {s.get('version', 'no documentada')}; consulta {s.get('consulted_at', 'no documentada')})")
    return "\n".join(lines) or "Steward no encontró evidencia suficiente de la política solicitada."
