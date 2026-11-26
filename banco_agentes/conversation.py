"""Una sesión ADK persistente por conversación autenticada."""
import asyncio
import time
from uuid import uuid4

from google.adk.agents.run_config import RunConfig
from google.adk.events import Event, EventActions
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from .general_agents import Turn, build_general_agents, build_report_agent, requests_quarter
from .governance import (AUDIT, CLIENT_CATALOG_QUALITY_MESSAGE, REQUEST, SESSION, authorize,
                         authorize_technical_catalog, catalog_quality_requested, customer_completeness_rule,
                         foreign_client_alias_requested, format_governance, request_denial, review_policy,
                         technical_catalog_requested)
from .governance import OWN_ACCOUNT_REFS, requested_accounts
from .client_access import own_account_numbers
from .employee_flow import employee_turn
from .runtime import Trace
from .response_layout import decode_final, make_layout
from .presentation import documentation_body, result_text, result_table, source_entry
from .semantics import CUSTOMER_COMPLETENESS_DETAIL
from .steward_rag import catalog_completeness_policy
from .variation import variation_request
from .executive_reports import PERMISSION, ranking_snapshot, report_tool, snapshot

APP_NAME = "banco_agentes_web"


class ConversationSessions(InMemorySessionService):
    def retain_recent_events(self, session, limit=24):
        stored = self.sessions[session.app_name][session.user_id][session.id]
        del stored.events[:-limit]


def format_query_details(result):
    plan = result["plan"]
    scope = [plan["conjunto"]]
    for key in ("producto", "persona", "segmento", "codigo"):
        if plan[key]:
            scope.append(f"{key}={plan[key]}")
    if plan["desde"]:
        scope.append(plan["desde"] if plan["desde"] == plan["hasta"] else f"{plan['desde']} a {plan['hasta']}")
    if plan["alcance"] == "propio":
        scope.append("cuentas de la sesión")
    lines = [f"{result['id']} · {plan['metrica'].capitalize()} · " + " · ".join(scope)]
    if plan["metrica"] == "variacion":
        lines.append(f"Estadística comparada: {plan['base']}.")
    if result["status"] != "ok":
        lines.append({"insufficient": "Observaciones insuficientes.", "partial": "Resultado parcial."}[result["status"]])
    if result["rule"]:
        lines.append(result["rule"])
    if result["rows"]:
        columns = result["columns"]
        lines.append(" | ".join(columns))
        for row in result["rows"][:24]:
            lines.append(" | ".join(str(row[col]) if row[col] is not None else "Sin valor" for col in columns))
        if len(result["rows"]) > 24:
            lines.append(f"Vista de 24 de {len(result['rows'])} filas. Reduce el alcance para ver más detalle.")
    lines.extend(result["notes"])
    lines.append(f"Origen: {result['table']}. Unidad: {result['units']}. Datos sintéticos.")
    lines.append("Fuentes SQL: " + "; ".join(result["sources"]) + ".")
    return "\n".join(lines)


def render_turn(turn, answer):
    denied = request_denial()
    if denied:
        policy = format_governance(turn.policy or review_policy())
        return policy, []
    sections = []
    verified = presentation_results(turn)
    layout = make_layout(turn, verified, REQUEST.get())
    definitions = getattr(turn, "field_definitions", {})
    if not verified and SESSION.get().profile == "Analista" and requests_quarter(REQUEST.get()):
        issue = next((result["message"] for result in turn.results if result["status"] == "invalid_plan"), None)
        if issue:
            return "No se pudo obtener un resultado verificado. " + issue, []
    if layout["conclusion"]:
        sections.append(layout["conclusion"])
    sections.extend(definitions.values())
    sections.extend(layout["notes"])
    for result in turn.results:
        if result["status"] in {"denied", "unavailable"} or (not verified and result["status"] == "invalid_plan"):
            prefix = "No se pudo obtener un resultado verificado. " if result["status"] in {"invalid_plan", "unavailable"} else ""
            sections.append(prefix + result["message"])
    if turn.policy:
        sections.append(format_governance(turn.policy))
    if getattr(turn, "rag_answers", []):
        sections.extend(turn.rag_answers)
    elif turn.documentation:
        choice = getattr(turn, "presentation_choice", None)
        if not verified and not definitions and choice and choice.formato == "prosa" and answer.strip():
            sections.append(documentation_body(answer))
        else:
            sections.extend(documentation_body(text) for text in dict.fromkeys(turn.documentation))
    elif not verified and not definitions and not turn.policy and answer.strip():
        sections.insert(0, documentation_body(answer))
    sections.extend(turn.chart_issues)
    if not sections:
        # Las respuestas de aclaración y de interpretación documental las formula el hub.
        sections.append(documentation_body(answer))
    if verified or layout["charts"]:
        has_amount = any(r["plan"]["metrica"] not in {"filas", "cuentas", "clientes"}
                         and r.get("agent") != "data_quality" for r in presentation_results(turn))
        sections.append("Datos sintéticos." + (" Los importes no tienen moneda asignada." if has_amount else ""))
    return "\n\n".join(dict.fromkeys(sections)), layout["charts"]


def technical_catalog_denial(question, message, trace, start):
    return {"question": question, "answer": message, "charts": [], "tables": [],
            "presentation": {"format": "prosa", "objective": "resumen", "result_ids": [], "permission_only": True},
            "trace": trace.records, "total_seconds": time.perf_counter() - start}


def can_present_results(turn):
    # La explicación es opcional; cada resultado pasa por execute_plan/authorize.
    return not request_denial()


def presentation_results(turn):
    if not can_present_results(turn):
        return []
    cached = getattr(turn, "selected_presentation_results", None)
    if cached is not None:
        return cached
    results = [result for result in turn.results if "rows" in result]
    chart_ids = {key for chart in turn.charts for key in chart.get("result_ids", [])}
    choice = getattr(turn, "presentation_choice", None)
    requested = set(choice.resultados if choice and choice.resultados else chart_ids)
    selected = [r for r in results if r["id"] in requested]
    if selected:
        results = selected
    authorized = []
    for result in results:
        if authorize("own_analysis" if result["scope"] == "propio" else "aggregate_balance"):
            continue
        authorized.append(result)
    turn.selected_presentation_results = authorized
    return authorized


def presentation_fields(turn):
    if not can_present_results(turn):
        return {"sources": [], "tables": [], "query_details": ""}
    results = presentation_results(turn)
    layout = make_layout(turn, results, REQUEST.get())
    tables = layout["tables"]
    sources = list(getattr(turn, "rag_citations", []))
    for result in results:
        sources.append(source_entry(result["table"]))
        sources.extend(source_entry(citation) for citation in result["sources"])
        if result["plan"].get("fuente_regla"):
            sources.append(source_entry(result["plan"]["fuente_regla"]))
    sources.extend(source_entry(citation) for citation in turn.sources
                   if not citation.startswith(("https://", "http://")) and ".xlsx" not in citation)
    for chart in layout["charts"]:
        sources.append(source_entry(chart["source"]))
        sources.extend(source_entry(citation) for citation in chart["citations"])
    for event in turn.trace.records:
        if event["event"] == "catalog_evidence" and not event["evidence"].get("tables"):
            sources.extend(source_entry(source["file"])
                           for source in event["evidence"].get("searched_sources", []))
        if event["event"] != "retrieval_result":
            continue
        for hit in event["result"]["hits"]:
            source = hit["source"]
            if hit["kind"] == "cards_dictionary":
                sources.append({"label": "Diccionario de variables de tarjetas",
                                "location": f"Hoja {source['sheet']} · fila {source['row']}",
                                "kind": "dictionary", "details": {"Archivo": source["file"], "Tipo de documento": "Diccionario de variables"}})
            else:
                sources.append({"label": source["title"], "location": source["section"],
                                "url": source["url"], "kind": "publication",
                                "details": {"Fecha de consulta": source["consulted_at"], "Tipo de documento": "Página pública"}})
    unique = {(item["label"], item["location"]): item for item in sources}
    return {"sources": list(unique.values()), "tables": tables,
            "presentation": {key: layout[key] for key in ("format", "objective", "result_ids")},
            "query_details": "\n\n".join(format_query_details(result) for result in results)}


class Conversation:
    def __init__(self, identity, *, model_factory=None, owner_id=None, conversation_id=None):
        self.identity = identity
        self.owner = owner_id or uuid4().hex
        self.id = conversation_id or uuid4().hex
        self.web_bound = owner_id is not None
        self.service = ConversationSessions()
        self.model_factory = model_factory
        self.lock = asyncio.Lock()
        self.report_analyses = []
        self.reports = {}

    def belongs_to(self, identity, owner_id, conversation_id):
        return self.identity == identity and self.owner == owner_id and self.id == conversation_id

    def verified_memory(self, session):
        memory = session.state.get("verified_memory", {})
        if self.web_bound and memory.get("owner") != [self.owner, self.id]:
            return {}
        return memory

    async def ask(self, question, *, timeout=420, emit=lambda _: None):
        async with self.lock:
            return await self._ask(question, timeout, emit)

    async def ask_report(self, question, *, timeout=420):
        async with self.lock:
            trace = Trace(lambda _: None)
            if self.identity.profile != "Analista" or not self.web_bound:
                trace.records.append({"event": "access_blocked", "agent": "policy_code",
                                      "reason": "employee_only", "sql_executed": False})
                return {"answer": PERMISSION, "trace": trace.records}
            started = time.perf_counter()
            outcome = {}
            result = {"trace": trace.records}
            scope = dict(app_name=APP_NAME, user_id=self.owner, session_id=self.id)
            service = ConversationSessions()
            await service.create_session(**scope)
            runner = None
            try:
                tool = report_tool(self, question, trace, outcome)
                runner = Runner(app_name=APP_NAME, agent=build_report_agent(trace, tool, self.model_factory),
                                session_service=service)
                async with asyncio.timeout(timeout):
                    async for _ in runner.run_async(
                        user_id=self.owner, session_id=self.id,
                        new_message=types.Content(role="user", parts=[types.Part(text=question)]),
                        run_config=RunConfig(max_llm_calls=1)):
                        pass
                if not outcome:
                    raise ValueError("El orquestador no invocó la herramienta de reporte.")
                return {**result, **outcome, "total_seconds": time.perf_counter() - started}
            except Exception as error:
                error.run_result = {**result, "total_seconds": time.perf_counter() - started}
                raise
            finally:
                if runner is not None:
                    await runner.close()

    async def ask_documents(self, question, *, store, file_ids, owner, conversation_id):
        """Adjuntos autorizados → lectura aislada → decisión del hub sobre evidencia.

        La memoria ADK conserva exclusivamente IDs; los bytes nunca llegan al hub.
        """
        from .documents import DocumentError, private_document_scope
        from .document_review import review_documents
        from .document_credit import client_document_intent, compare_classic, inventory_question, receipt_elapsed, requested_product
        from .document_presentation import preliminary_view
        from .attachment_hub import AttachmentDecision, decide_attachment, cited_evidence, safe_trace
        from .document_content import content_question, ine_validity_question
        from .document_evidence import is_ine
        from .steward_rag import attachment_policy
        from .document_privacy import safe_free_text, clean_visible_text, requested_fields, personal_values
        from .attachment_presentation import attachment_view
        if self.web_bound and not self.belongs_to(self.identity, owner, conversation_id):
            raise DocumentError("Esta conversación no está disponible en tu sesión.")
        if foreign_client_alias_requested(question, self.identity):
            return await self.ask(question)
        question = safe_free_text(question)
        direct_read = content_question(question) and not inventory_question(question)
        async with self.lock:
            started = time.perf_counter()
            scope = dict(app_name=APP_NAME, user_id=self.owner, session_id=self.id)
            session = await self.service.get_session(**scope)
            if session is None:
                session = await self.service.create_session(**scope)
            refs = dict(session.state.get("document_refs", {}))
            memory = dict(self.verified_memory(session))
            credit_product = session.state.get("document_credit_product")
            client_mode, product = client_document_intent(question, credit_product)
            mode = client_mode if self.identity.profile == "Cliente" else None
            compare_documents = mode == "preliminary"
            hub_trace = Trace(lambda _: None)
            trace = hub_trace.records
            trace.append({"event": "agent_started", "agent": "orquestador"})
            documents, errors, sources = [], [], []
            # Autoriza TODO el lote antes de iniciar una lectura o llamada externa.
            records = [store.authorized(file_id, owner, conversation_id) for file_id in file_ids]
            ine_field = direct_read and ine_validity_question(question)
            if ine_field:
                ine_ids = [r["file_id"] for r in records if is_ine(r.get("result") or {})]
                file_ids = ine_ids or [r["file_id"] for r in records if not r["attempted"] or r["error"]]
            with private_document_scope():
                for file_id in file_ids:
                    read_started = time.perf_counter()
                    record = store.authorized(file_id, owner, conversation_id)
                    cached = record["attempted"] and not record.get("retryable")
                    try:
                        document = await store.leer_documento(file_id, owner, conversation_id,
                            **({"question": question} if direct_read else
                               {"recognition_only": not compare_documents, "prefer_text": bool(mode)}))
                        if file_id not in refs:
                            refs[file_id] = f"D{len(refs)+1}"
                        document["id"] = refs[file_id]
                        documents.append(document)
                        pages = document.get("paginas_relevantes", [])
                        location = "Páginas " + ", ".join(map(str, pages)) if pages else "Archivo adjunto"
                        sources.append({"label": document["id"] + " · " + record["name"], "location": location,
                                        "details": {"file_id": file_id}})
                    except DocumentError as error:
                        errors.append({"file_id": file_id, "archivo": record["name"], "mensaje": str(error), "tipo": error.kind})
                    if not cached and record["metrics"]:
                        trace.append(record["metrics"])
                    if direct_read:
                        trace.extend(record.get("content_metrics", []))
                    trace.append({"event": "tool_completed", "agent": "orquestador", "tool": "leer_documento",
                                  "file_id": file_id, "cached": cached, "seconds": time.perf_counter()-read_started})
                policy = derived = None
                decision = None
                if direct_read and documents:
                    answer = "\n".join(d["respuesta_lectura"] for d in documents)
                    decision = AttachmentDecision(alcance="bancario", accion="responder")
                elif ine_field and not file_ids:
                    answer = "No encontré una INE reconocida entre los adjuntos seleccionados."
                    decision = AttachmentDecision(alcance="bancario", accion="responder")
                elif mode and product:
                    answer = ""
                    decision = AttachmentDecision(alcance="bancario", accion="comparar" if compare_documents else "politica",
                                                  producto=product)
                elif mode == "inventory":
                    number = memory.get("next_result", 1)
                    derived = review_documents(documents, None, errors=errors, question=question)
                    derived.update(id=f"R{number}", documentos=[d["id"] for d in documents], politica_id=None)
                    memory["next_result"] = number + 1
                elif not documents:
                    answer = ("No se pudo completar la lectura. Revisa el error del archivo o del proveedor; no es un rechazo por alcance."
                              if errors else "Selecciona un adjunto mediante el botón. Un ID escrito en el mensaje no permite leer archivos.")
                else:
                    question = safe_free_text(question, known_values=personal_values(documents))
                    try:
                        decision = await decide_attachment(question, documents, previous_product=credit_product,
                            model_factory=self.model_factory, trace=hub_trace)
                        if decision.alcance == "fuera_de_alcance":
                            answer = "Esta consulta está fuera del alcance bancario de Banco Agentes."
                        elif decision.accion == "medir_antiguedad":
                            decision.producto = "general"
                            answer = receipt_elapsed(documents)
                        elif decision.accion == "evaluar_comprobante" and product:
                            mode = "recency"
                            compare_documents = True
                            decision.accion = "comparar"
                            decision.producto = product
                            answer = ""
                        elif decision.accion in {"aclarar", "evaluar_comprobante"} or (
                                decision.accion == "comparar" and not product):
                            decision.accion = "aclarar"
                            decision.producto = "general"
                            answer = "¿Para qué trámite o requisito quieres que lo revise?"
                        elif decision.accion in {"politica", "comparar"} or requested_product(question):
                            answer = ""  # La revisión se presenta desde el resultado en código, sin interpretar cifras.
                        else:
                            answer = cited_evidence(documents, decision, question) or "El adjunto no aporta evidencia para responder esta pregunta bancaria."
                    except DocumentError as error:
                        answer = str(error)
                if not mode and not direct_read and not documents and requested_product(question):
                    decision = AttachmentDecision(alcance="bancario", accion="comparar", producto=requested_product(question))
                if not direct_read and decision and decision.alcance == "bancario" and (
                        decision.accion in {"politica", "comparar"} or
                        (decision.accion == "responder" and requested_product(question))):
                    if not mode:
                        decision.producto = product or requested_product(question) or "general"
                    trace.append({"event": "agent_started", "agent": "data_steward"})
                    policy_started = time.perf_counter()
                    number = memory.get("next_result", 1)
                    try:
                        policy_options = {"compare": True, "requirements": ("antiguedad_comprobante_meses",)} if mode == "recency" else {"review": True}
                        policy = await attachment_policy(question, decision.producto, **policy_options)
                        policy["id"] = f"R{number}"
                        trace.append({"event": "tool_completed", "agent": "data_steward", "tool": "recuperar_politica_documental",
                                      "seconds": time.perf_counter()-policy_started,
                                      "result_id": policy["id"], "fragment_ids": policy["fragment_ids"]})
                        sources.extend(policy["fuentes"])
                    except Exception as error:
                        trace.append({"event": "document_politica_failed", "agent": "data_steward",
                                      "seconds": time.perf_counter()-policy_started, "error_type": type(error).__name__})
                        policy = None
                    if product and decision.producto == product:
                        credit_product = product
                    if compare_documents:
                        derived = compare_classic(documents, policy, errors=errors, receipt_only=mode == "recency",
                                                  product=decision.producto)
                        derived["summary"], derived["view"] = preliminary_view(documents, derived)
                    else:
                        derived = review_documents(documents, policy, errors=errors, question=question)
                    derived.update(id=f"R{number+1}", documentos=[d["id"] for d in documents], politica_id=policy["id"] if policy else None)
                    memory["next_result"] = number+2
            await self.service.append_event(session, Event(author="orquestador", actions=EventActions(
                state_delta={"document_refs": refs, "verified_memory": memory, "document_credit_product": credit_product})))
            document_view = None
            if derived:
                document_view = {"summary": derived["summary"]}
                answer = derived["summary"]
            if errors and (not derived or derived["tipo"] != "revision_documental"):
                answer += "\n\n" + ("La lectura fue parcial. " if documents else "Falló la lectura de los adjuntos. ")
                answer += " ".join(dict.fromkeys(e["archivo"] + ": " + e["mensaje"] for e in errors))
            if not direct_read:
                from .document_content import coverage_notice
                for document in documents:
                    record = store.authorized(document["file_id"], owner, conversation_id)
                    if document.get("advertencias_lectura"):
                        answer += "\n" + " ".join(document["advertencias_lectura"])
                    notice = coverage_notice(record.get("content_pages", []), record["mime"])
                    if notice:
                        answer += "\n" + notice
            view = derived["view"] if derived else attachment_view(question, documents, decision, derived, document_view)
            if derived:
                # Conservar los avisos existentes de lectura antes de la única frase de cierre.
                notices = answer[len(derived["summary"]):].strip()
                view["reading_notices"] = [notices] if notices else []
                if notices:
                    answer = derived["summary"].removesuffix(view["closing"]) + notices + "\n\n" + view["closing"]
            if direct_read:
                view = {"requirements": [], "confirmations": [c for d in documents for c in d.get("confirmaciones_lectura", [])], "references": []}
            allowed = requested_fields(question) if decision and decision.accion == "responder" else ()
            answer = clean_visible_text(answer, known_values=personal_values(documents), allow_fields=allowed)
            return {"answer": answer, "documents": documents, "document_errors": errors, "policy": policy, "derived": derived,
                    "attachment_view": view,
                    "document_view": document_view, "attachment_decision": decision.model_dump() if decision else None,
                    "sources": sources, "trace": safe_trace(trace), "total_seconds": time.perf_counter()-started,
                    "presentation": {"format": "prosa" if decision and decision.alcance == "fuera_de_alcance" else "reporte",
                                     "result_ids": [] if decision and decision.alcance == "fuera_de_alcance" else
                                     [d["id"] for d in documents] + ([policy["id"]] if policy else []) + ([derived["id"]] if derived else [])}}

    async def _ask(self, question, timeout, emit):
        trace = Trace(emit)
        tokens = SESSION.set(self.identity), REQUEST.set(question), AUDIT.set(trace)
        accounts_token = OWN_ACCOUNT_REFS.set(None)
        start = time.perf_counter()
        runner = None
        session = None
        answer = ""
        scope = dict(app_name=APP_NAME, user_id=self.owner, session_id=self.id)
        try:
            if foreign_client_alias_requested(question, self.identity):
                denial = authorize("own_analysis")
                trace.records.append({"event": "access_blocked", "agent": "policy_code", "profile": "Cliente",
                                      "reason": denial["error_code"], "sql_executed": False})
                return {"question": question,
                        "answer": "Con tu perfil Cliente solo puedes consultar tus propios saldos y movimientos",
                        "charts": [], "tables": [],
                        "presentation": {"format": "prosa", "objective": "resumen", "result_ids": [],
                                         "permission_only": True},
                        "trace": trace.records, "total_seconds": time.perf_counter() - start}
            if self.identity.profile == "Cliente" and catalog_quality_requested(question):
                denial = authorize("quality")
                trace.records.append({"event": "access_blocked", "agent": "policy_code", "profile": "Cliente",
                                      "reason": denial["error_code"], "sql_executed": False})
                trace.records.append({**review_policy(), "event": "governance_result", "agent": "policy_code",
                                      "status": "denied", "reason": denial["error_code"]})
                return {"question": question, "answer": CLIENT_CATALOG_QUALITY_MESSAGE, "charts": [], "tables": [],
                        "presentation": {"format": "prosa", "objective": "resumen", "result_ids": [],
                                         "permission_only": True},
                        "trace": trace.records, "total_seconds": time.perf_counter() - start}
            if self.identity.profile == "Cliente" and technical_catalog_requested(question):
                denial = authorize_technical_catalog()
                return technical_catalog_denial(question, denial["message"], trace, start)
            if self.identity.profile == "Cliente" and self.identity.principal and requested_accounts(question):
                accounts = await asyncio.to_thread(own_account_numbers)
                OWN_ACCOUNT_REFS.set((self.identity, accounts))
            session = await self.service.get_session(**scope)
            reused = session is not None
            if session is None:
                session = await self.service.create_session(**scope)
            after_ranking = session.state.get("employee_ranking", False)
            employee = await employee_turn(question, trace, after_ranking=after_ranking)
            if employee is not None:
                if employee["tables"]:
                    memory = dict(self.verified_memory(session))
                    number = memory.get("next_result", 1)
                    employee["presentation"]["result_ids"] = [f"R{number}"]
                    memory.update(next_result=number + 1, owner=[self.owner, self.id])
                    await self.service.append_event(session, Event(author="data_steward",
                        actions=EventActions(state_delta={"employee_ranking": True, "verified_memory": memory})))
                    if self.web_bound and self.identity.profile == "Analista":
                        plan = next(record["plan"] for record in trace.records
                                    if record["event"] == "validated_plan")
                        self.report_analyses.extend(ranking_snapshot(employee, plan))
                return {"question": question, **employee, "trace": trace.records,
                        "total_seconds": time.perf_counter() - start}
            denied = request_denial()
            memory = {} if denied else self.verified_memory(session)
            trace.records.append({"event": "conversation_session", "session_id": self.id,
                                  "reused": reused, "memory_results": len(memory.get("results", []))})
            if denied:
                trace.records.append({"event": "access_blocked", "agent": "policy_code", "reason": denied,
                                      "sql_executed": False, "profile": self.identity.profile})
            turn = Turn(trace, memory)
            turn.variation = variation_request(question, memory.get("variation_scope", {}).get("rate_mode"),
                has_balances=bool(memory.get("balance_chart") or memory.get("last_ids")))
            trace.records.append({"event": "context_snapshot", "agent": "orquestador",
                                  "profile": self.identity.profile,
                                  "last_ids": memory.get("last_ids", []),
                                  "results": [{"id": result["id"], "plan": result["plan"]}
                                              for result in memory.get("results", [])]})
            timed_out = False
            revision = None if denied else turn.reuse_variation()
            completeness = customer_completeness_rule(question) if not denied and self.identity.profile == "Analista" else None
            if completeness:
                trace.records.append({"event": "agent_started", "agent": "data_quality"})
                plan = dict(metrica="completitud", regla=completeness, conjunto="clientes", alcance="global",
                            desde="", hasta="", producto="", persona="", segmento="", agrupar="", campo="", codigo=0)
                await asyncio.to_thread(turn.execute, plan, quality=True)
                trace.records.append({"event": "tool_completed", "agent": "data_quality", "tool": "revisar_calidad"})
                if completeness == CUSTOMER_COMPLETENESS_DETAIL:
                    trace.records.append({"event": "agent_started", "agent": "data_steward"})
                    _, citation = catalog_completeness_policy()
                    turn.rag_citations.append(citation)
                    trace.records.append({"event": "tool_completed", "agent": "data_steward",
                                          "tool": "consultar_politica_calidad", "rule": "DQ-01", "citation": citation})
                revision = ""
            if revision is None and not denied:
                revision = await asyncio.to_thread(turn.revise_balance_chart, question)
            if revision is None:
                runner = Runner(app_name=APP_NAME, agent=build_general_agents(turn, self.model_factory),
                                session_service=self.service)
                try:
                    async with asyncio.timeout(timeout):
                        async for event in runner.run_async(
                            user_id=self.owner, session_id=self.id,
                            new_message=types.Content(role="user", parts=[types.Part(text=question)]),
                            run_config=RunConfig(max_llm_calls=12),
                        ):
                            if event.author == "orquestador" and event.is_final_response() and event.content:
                                answer = "".join(part.text for part in event.content.parts or [] if part.text and not part.thought)
                except TimeoutError:
                    if not (any("rows" in r for r in turn.results) or turn.documentation or turn.field_definitions
                            or turn.rag_answers or turn.charts or turn.catalog_denial):
                        raise
                    timed_out = True
                    trace.record("turn_timeout_partial", "orquestador")
                if turn.catalog_denial:
                    return technical_catalog_denial(question, turn.catalog_denial, trace, start)
                if not answer:
                    answer = "No se obtuvo una respuesta final verificable. Reformula el alcance de la pregunta."
                turn.presentation_choice, answer = decode_final(answer)
                if turn.variation and turn.variation.plot and not turn.charts and not turn.chart_issues:
                    selected = turn.presentation_choice.resultados
                    candidates = [r for r in turn.graph_results() if not selected or r["id"] in selected]
                    if candidates:
                        turn.graph_percentage(candidates)
            else:
                answer = revision
            if turn.rag_answers:
                trace.records.append({"event": "rag_hub_review", "agent": "orquestador", "answer": answer})
            answer, charts = render_turn(turn, answer)
            if timed_out:
                answer += "\n\nEl turno alcanzó su límite de tiempo. La revisión quedó parcial."
            if not denied:
                await self._save(session, memory, turn, question, answer)
            fields = presentation_fields(turn)
            if self.web_bound and self.identity.profile == "Analista" and can_present_results(turn):
                self.report_analyses.extend(snapshot(presentation_results(turn), charts,
                                                     turn.rag_citations, turn.variation))
            return {"question": question, "answer": answer, "charts": charts, "trace": trace.records,
                    **fields, "total_seconds": time.perf_counter() - start}
        except Exception as error:
            error.run_result = {"question": question, "answer": "", "trace": trace.records,
                                "total_seconds": time.perf_counter() - start}
            raise
        finally:
            try:
                if session is not None:
                    self.service.retain_recent_events(session)
                if runner is not None:
                    await runner.close()
            finally:
                SESSION.reset(tokens[0])
                REQUEST.reset(tokens[1])
                AUDIT.reset(tokens[2])
                OWN_ACCOUNT_REFS.reset(accounts_token)

    async def _save(self, session, memory, turn, question, answer):
        current = turn.rememberable()
        verified = [r for r in turn.results if r["status"] == "ok" and "rows" in r]
        last_ids = [r["id"] for r in current] if current else memory.get("last_ids", [])
        visible = turn.response_layout["charts"]
        previous = memory.get("balance_chart", {})
        calculated = getattr(turn, "variation_scope", None)
        variation_scope = calculated or memory.get("variation_scope", {})
        if calculated:
            last_ids = calculated["result_ids"]
        if visible:
            chart = visible[0]
            previous = {key: chart[key] for key in ("result_ids", "type", "rate_mode")
                        if key in chart} if chart.get("series") and not chart.get("purchase_group") else {}
        graph_ids = set(previous.get("result_ids", [])) | set(variation_scope.get("result_ids", []))
        available = {r["id"]: r for r in memory.get("results", []) + verified + turn.graph_results()}
        recent_results = {r["id"]: r for r in (memory.get("results", []) + verified)}
        keep = graph_ids | set(list(recent_results)[-5:])
        results = [r for key, r in available.items() if key in keep]
        visible_answer = "" if self.identity.profile == "Cliente" and self.web_bound else answer[:850]
        recent_turn = {"pregunta": question[:500], "respuesta": visible_answer,
                       "fuentes": list(dict.fromkeys(turn.sources))[:6]}
        recent = (memory.get("turns", []) + [recent_turn])[-3:]
        state = {"turns": recent, "results": results, "last_ids": last_ids, "owner": [self.owner, self.id],
                 "next_result": turn.next_result, "balance_chart": previous, "variation_scope": variation_scope}
        delta = {"verified_memory": state}
        if session.state.get("employee_ranking"):
            delta["employee_ranking"] = False
        if self.identity.profile == "Cliente" and turn.rag_citations:
            from .document_credit import client_document_intent, requested_product
            _, product = client_document_intent(question)
            if product and any(requested_product(c.get("label", "")) == product for c in turn.rag_citations):
                delta["document_credit_product"] = product
        await self.service.append_event(session, Event(author="orquestador",
            actions=EventActions(state_delta=delta)))
