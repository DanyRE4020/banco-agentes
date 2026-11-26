"""Recuperación obligatoria antes del redactor sin tools; el hub recibe solo síntesis y citas."""
import asyncio
import json
from contextlib import nullcontext
from pathlib import Path

from google.adk.agents import LlmAgent
from google.adk.tools.agent_tool import AgentTool
from pydantic import BaseModel, Field

from .catalog import ROOT
from .rag import Retriever, RetrievalError, citation


def employee_access_policy(rule):
    return _registered_policy("catalogo/employee_data_policy.json", rule)


def catalog_completeness_policy():
    return _registered_policy("catalogo/catalog_quality_policy.json", "DQ-01")


def _registered_policy(path, rule):
    data = json.loads((ROOT / path).read_text(encoding="utf-8"))
    return data["rules"][rule], {
        "label": data["title"], "location": f"Regla {rule} · v. {data['version']}",
        "kind": "document", "details": {"Archivo": path, "Tipo de documento": data["kind"],
                                        "Vigente desde": data["effective_from"]},
    }


class StewardAnswer(BaseModel):
    answer: str
    fragment_ids: list[str] = Field(default_factory=list)


async def answer_documents(agent, turn, question, tool_context):
    turn.trace.record("rag_retrieval_started", "data_steward")
    try:
        result = await asyncio.to_thread(Retriever().search, question, "bank_documents")
    except Exception as error:
        code = str(error) if isinstance(error, RetrievalError) else "rag_retrieval_failed"
        # Solo códigos fijos conocidos; nunca la excepción de un driver.
        turn.trace.records.append({"event": "rag_retrieval_failed", "agent": "data_steward",
                                   "error_code": code, "error_type": type(error).__name__})
        answer = "Fallo técnico al recuperar los documentos. No fue posible verificar la respuesta."
        turn.rag_answers.append(answer)
        return {"status": "technical_failure", "review": answer, "citations": []}
    turn.trace.records.append({"event": "rag_retrieval", "agent": "data_steward", **result})
    if not result["hits"]:
        answer = "No tengo información en los documentos para responder a esta pregunta: «" + question.rstrip(" .") + "»."
        turn.rag_answers.append(answer)
        return {"status": "not_found", "review": answer, "citations": []}
    # AgentTool crea una invocación aislada. No se envía memoria de Analytics ni PDF completo.
    writer = LlmAgent(name="data_steward", model=agent.model, tools=[],
        instruction="\n".join(Path(__file__).with_name("prompts").joinpath(name).read_text(encoding="utf-8")
                              for name in ("web_steward_rag.txt", "web_documental_style.txt")),
        output_schema=StewardAnswer, include_contents="none",
        before_agent_callback=turn.trace.before_agent, before_model_callback=turn.before_model)
    # La auditoría conserva todas las métricas y metadatos. El redactor recibe
    # solo contenido pertinente, requisitos y localizadores; no cadenas técnicas de presentación.
    context = [{"id": h["id"], "text": h["text"], "title": h["source"]["title"],
                "page": h["source"]["page"], "section": h["source"].get("section"),
                "source_scope": h["source"]["source_kind"],
                "requirements": h["source"].get("requirements", [])} for h in result["hits"]]
    task = json.dumps({"pregunta": question, "evidencia_no_confiable": context}, ensure_ascii=False)
    turn.trace.record("rag_writer_started", "data_steward")
    try:
        from openinference.instrumentation import suppress_tracing
    except ImportError:
        suppress_tracing = nullcontext
    # Impide exportar el prompt con fragmentos mediante instrumentación opcional.
    with suppress_tracing():
        review = await AgentTool(agent=writer).run_async(args={"request": task}, tool_context=tool_context)
    if isinstance(review, str):
        parsed = StewardAnswer.model_validate_json(review)
    else:
        parsed = StewardAnswer.model_validate(review)
    known = {h["id"]:h for h in result["hits"]}
    if any(key not in known for key in parsed.fragment_ids):
        raise RetrievalError("steward_invalid_citation")
    selected = [known[key] for key in dict.fromkeys(parsed.fragment_ids)]
    citations = [citation(h) for h in selected]
    turn.rag_citations.extend(source_card(h) for h in selected)
    answer = parsed.answer
    turn.rag_answers.append(answer)
    turn.trace.records.append({"event": "rag_steward_result", "agent": "data_steward",
                              "answer": parsed.answer, "fragment_ids": parsed.fragment_ids, "citations": citations})
    return {"status": "found" if selected else "insufficient_evidence",
            "review": parsed.answer, "fragment_ids": parsed.fragment_ids,
            "citations": [source_card(h)["label"] + " · " + source_card(h)["location"] for h in selected]}

def source_card(hit):
    source = hit["source"]
    kind = {"resumen_academico": "Resumen académico", "oficial_publico": "Documento oficial público",
            "ejemplo_ilustrativo": "Ejemplo ilustrativo"}[source["source_kind"]]
    return {"label": source["title"], "location": f"Página {source['page']}" +
            (f" · {source['section']}" if source.get("section") else ""), "kind": "publication",
            "details": {"Archivo": source["file"], "Versión": source.get("version", "No documentada"),
                        "Tipo de documento": kind, "Fecha de consulta": source.get("consulted_at", "No documentada"),
                        "Vigencia": source.get("vigencia", "No documentada"),
                        "Fuente original": source.get("original_source", "No documentada")}}


def product_hit(hit, product):
    from .document_credit import requested_product
    source = hit["source"]
    identified = source.get("producto") or requested_product(source.get("title", "") + " " + source.get("section", ""))
    return (identified or requested_product(hit["text"])) == product


def requested_requirements(question):
    import re
    from .document_credit import plain
    text = plain(question)
    if re.search(r"\brequisitos\b", text) and not re.search(r"\bsolo\b|unicamente", text):
        return ()
    result = []
    if re.search(r"ingreso|neto|cfdi|mi nomina", text):
        result.append("ingreso_mensual")
    if re.search(r"comprobante|antiguedad|servicio|domicilio", text):
        result.append("antiguedad_comprobante_meses")
    return tuple(result)


async def document_policy(product, *, requirements=()):
    """Ruta determinista de data_steward para el lector; solo recibe el producto, nunca campos del usuario."""
    import re
    from .document_credit import PRODUCTS, plain
    labels = {"ingreso_mensual": "ingreso mensual comprobable", "antiguedad_comprobante_meses": "comprobante domicilio servicios antigüedad"}
    result = await asyncio.to_thread(Retriever().search,
        PRODUCTS[product] + " requisitos " + (" ".join(labels[r] for r in requirements) if requirements else
            "identificación oficial vigente ingreso mensual edad comprobante domicilio"),
        "bank_documents")
    selected = [h for h in result["hits"]
                if product_hit(h, product) and (h["source"].get("requirements") or
                    re.search(r"requisit|document", plain(h["source"].get("section", ""))))]
    rules, sources = [], []
    for hit in selected:
        source = hit["source"]
        sources.append(source_card(hit))
        for rule in source.get("requirements", []):
            structured = {**rule, "fuente": {**source, "fragment_id": hit["id"]}}
            if rule["campo"] == "antiguedad_comprobante_meses":
                from .document_financial import admitted_services
                allowed = admitted_services(hit["text"])
                # La lista procede de la regla recuperada; no de un catálogo de emisores.
                if allowed:
                    structured.setdefault("tipos_comprobante_admitidos", allowed)
                condition = plain(rule.get("condicion", ""))
                if "domicilio" in condition and re.search(r"distint|difer|coincid", condition):
                    structured["comparar_domicilio_identificacion"] = True
            rules.append(structured)
        if re.search(r"identificacion\s+oficial\s+vigente", plain(hit["text"])):
            rules.append({"campo": "vigencia_identificacion", "fuente": {**source, "fragment_id": hit["id"]}})
            rules.append({"campo": "identificacion_oficial", "fuente": {**source, "fragment_id": hit["id"]}})
        if product == "nomina" and "recibir la nomina" in plain(hit["text"]):
            rules.append({"campo": "recepcion_nomina_o_pension", "fuente": {**source, "fragment_id": hit["id"]}})
        if product == "nomina" and "telefono" in plain(hit["text"]):
            rules.append({"campo": "telefono_actualizado", "fuente": {**source, "fragment_id": hit["id"]}})
    unique = {json.dumps({k:v for k,v in r.items() if k != "fuente"}, sort_keys=True): r for r in rules}
    expected = {"ingreso_mensual", "antiguedad_comprobante_meses", "edad_meses" if product == "nomina" else "edad_anios"}
    if requirements:
        unique = {key: r for key, r in unique.items() if r["campo"] in requirements}
        expected &= set(requirements)
    missing = expected - {r["campo"] for r in rules if "operador" in r and "valor" in r}
    from .document_review import policy_documentation
    return {**policy_documentation(selected), "tipo": "politica", "producto": product, "reglas": list(unique.values()), "fuentes": sources,
            "faltantes": ["No se recuperó la regla estructurada de " + field + "; no puede evaluarse." for field in sorted(missing)]
                + ["La lectura no autentica documentos ni acredita origen salarial/pensión, teléfono, invitación u otros requisitos sin evidencia suficiente. La vigencia de la política fuera de su fecha de consulta no está verificada."],
            "fragment_ids": [h["id"] for h in selected]}


async def attachment_policy(question, product="general", *, compare=False, review=False, requirements=None):
    """Steward para políticas generales: recibe la pregunta, nunca el contenido del adjunto."""
    if review and product in {"nomina", "clasica"}:
        return await document_policy(product)
    if compare and product in {"nomina", "clasica"}:
        needed = requested_requirements(question) if requirements is None else requirements
        return await document_policy(product, requirements=needed) if needed else await document_policy(product)
    result = await asyncio.to_thread(Retriever().search, question, "bank_documents")
    hits = result["hits"]
    if product in {"nomina", "clasica"}:
        hits = [hit for hit in hits if product_hit(hit, product)]
    from .document_review import policy_documentation
    return {**policy_documentation(hits), "tipo": "politica", "producto": product, "reglas": [],
            "fuentes": [source_card(hit) for hit in hits], "faltantes": [],
            "fragment_ids": [hit["id"] for hit in hits],
            "extractos": [{"texto": hit["text"], "fuente": hit["source"]} for hit in hits[:3]]}
