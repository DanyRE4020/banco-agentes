"""Chat local: identidad en el servidor, permisos en Governance."""
import argparse
import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager, nullcontext
from dataclasses import dataclass, field
from importlib.util import find_spec
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .auth import SessionTokens, authenticate, read_users, users_path
from .governance import DemoSession
from .telemetry import turn_metrics
from .providers import MODEL_DEADLINE
from .documents import DocumentStore, DocumentError, MAX_BYTES, MAX_FILES, private_document_scope
from .document_privacy import public_attachment_data
from .document_credit import client_document_intent, preliminary_question, receipt_reference
from .document_content import ine_validity_question
from . import executive_reports

ROOT = Path(__file__).resolve().parent.parent
ASSETS = Path(__file__).with_name("web_assets")
COOKIE = "banco_demo_session"


class Login(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(max_length=64)
    password: str = Field(max_length=128)


class Question(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=4000)
    conversation_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    document_ids: list[str] | None = Field(default=None, max_length=MAX_FILES)


class DocumentCorrection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    conversation_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    campo: str = Field(max_length=64)
    valor: str = Field(min_length=1, max_length=500)


@dataclass
class WebSession:
    identity: DemoSession
    expires: float
    busy: bool = False
    conversations: dict = field(default_factory=lambda: {"default": None})
    owner_id: str = field(default_factory=lambda: uuid4().hex)
    document_private: set = field(default_factory=set)
    document_selection: dict = field(default_factory=dict)


async def run_agent(question, *, session, timeout, conversation=None):
    from .conversation import Conversation
    return await (conversation or Conversation(session)).ask(question, timeout=timeout)


def public_result(result):
    trace = result.get("trace", [])
    return dict(answer=result.get("answer", ""), presentation=result.get("presentation"), metrics=result.get("metrics"), sources=result.get("sources", []),
                tables=result.get("tables", []), query_details=result.get("query_details", ""),
                total_seconds=round(result.get("total_seconds", 0), 3),
                agents=list(dict.fromkeys(r["agent"] for r in trace if r.get("event") == "agent_started")),
                tools=list(dict.fromkeys(r["tool"] for r in trace if r.get("event") == "tool_completed")),
                blocked=any(r.get("event") == "access_blocked" or
                            (r.get("event") == "authorization" and r.get("allowed") is False) or
                            (r.get("event") == "validated_plan" and r.get("status") == "denied") for r in trace))


def create_app(runner=run_agent, audit_path=ROOT / "verification-web.jsonl", *, metrics_path=None, model_info=None,
               diagnostics=None, document_store=None, users=None):
    @asynccontextmanager
    async def lifespan(app):
        yield
        if diagnostics is not None:
            try:
                await asyncio.to_thread(diagnostics.close)
            except Exception:
                logging.getLogger(__name__).warning("No se confirmó el cierre del diagnóstico Langfuse.")

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]"])
    sessions = {}
    tokens = SessionTokens()
    if users is None:
        users = read_users(users_path())
    inference = asyncio.Semaphore(1)
    documents = document_store or DocumentStore()

    @app.middleware("http")
    async def local_browser(request: Request, call_next):
        origin = request.headers.get("origin")
        if request.method == "POST" and origin and origin != str(request.base_url).rstrip("/"):
            return JSONResponse({"detail": "Origen no permitido."}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        # Plotly inserta reglas de estilo al dibujar. El permiso solo aplica al HTML del chat.
        styles = "'self' 'unsafe-inline'" if request.url.path == "/" else "'self'"
        response.headers["Content-Security-Policy"] = f"default-src 'self'; style-src {styles}; script-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        return response

    def authenticated(request):
        token = request.cookies.get(COOKIE)
        current = sessions.get(token) if tokens.valid(token) else None
        if current is None or current.expires <= time.monotonic():
            sessions.pop(token, None)
            raise HTTPException(401, "Inicia sesión para continuar.")
        return token, current

    @app.get("/api/session")
    async def session_info(request: Request):
        _, current = authenticated(request)
        return {"profile": current.identity.profile}

    @app.post("/api/login")
    async def login(body: Login, request: Request):
        identity = await asyncio.to_thread(authenticate, body.username.strip().lower(), body.password, users)
        if identity is None:
            raise HTTPException(401, "Usuario o contraseña incorrectos.")
        sessions.pop(request.cookies.get(COOKIE), None)
        # Elimina sesiones caducadas sin conservar identidades indefinidamente.
        for token in list(sessions):
            if sessions[token].expires <= time.monotonic():
                sessions.pop(token, None)
        token = tokens.issue()
        owner_id = tokens.owner(identity)
        sessions[token] = WebSession(identity, time.monotonic() + 8 * 3600, owner_id=owner_id)
        response = JSONResponse({"profile": identity.profile})
        response.set_cookie(COOKIE, token, max_age=8 * 3600, httponly=True, samesite="strict", path="/",
                            secure=request.url.scheme == "https")
        return response

    @app.post("/api/logout")
    async def logout(request: Request):
        sessions.pop(request.cookies.get(COOKIE), None)
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE, path="/")
        return response

    @app.post("/api/conversations")
    async def new_conversation(request: Request):
        _, current = authenticated(request)
        if current.busy:
            raise HTTPException(409, "Espera a que termine la consulta para abrir otra conversación.")
        if len(current.conversations) >= 9:
            oldest = next(key for key in current.conversations if key != "default")
            current.conversations.pop(oldest)
        conversation_id = uuid4().hex
        current.conversations[conversation_id] = None
        return {"conversation_id": conversation_id}

    @app.get("/api/conversations/{conversation_id}/reports/{report_id}")
    async def download_report(conversation_id: str, report_id: str, request: Request):
        _, current = authenticated(request)
        if current.identity.profile != "Analista":
            raise HTTPException(403, executive_reports.PERMISSION)
        conversation = current.conversations.get(conversation_id)
        if conversation is None or not conversation.belongs_to(current.identity, current.owner_id, conversation_id):
            raise HTTPException(404, "Este reporte no está disponible en tu sesión.")
        pdf = conversation.reports.get(report_id)
        if pdf is None:
            raise HTTPException(404, "Este reporte no está disponible en tu sesión.")
        return Response(pdf, media_type="application/pdf",
                        headers={"Content-Disposition": 'attachment; filename="reporte-ejecutivo.pdf"'})

    @app.post("/api/chat")
    async def chat(body: Question, request: Request):
        token, current = authenticated(request)
        question = body.question.strip()
        if not question:
            raise HTTPException(422, "Escribe una pregunta.")
        conversation_id = body.conversation_id or "default"
        if conversation_id not in current.conversations:
            raise HTTPException(404, "Esta conversación no está disponible en tu sesión.")
        existing = current.conversations[conversation_id]
        if existing is not None and not existing.belongs_to(current.identity, current.owner_id, conversation_id):
            raise HTTPException(404, "Esta conversación no está disponible en tu sesión.")
        file_ids = body.document_ids or []
        if "document_ids" not in body.model_fields_set and current.identity.profile == "Cliente":
            if (client_document_intent(question)[0] or preliminary_question(question)
                    or ine_validity_question(question) or receipt_reference(question)):
                file_ids = list(current.document_selection.get(conversation_id, ()))
        if len(file_ids) != len(set(file_ids)):
            raise HTTPException(422, "No repitas adjuntos.")
        try:
            for file_id in file_ids:
                documents.authorized(file_id, current.owner_id, conversation_id)
        except DocumentError as error:
            raise HTTPException(404, str(error)) from None
        document_turn = bool(file_ids)
        report_turn = executive_reports.requested(question)
        # Haber tratado documentos mantiene la privacidad, pero no activa su lector.
        private = conversation_id in current.document_private
        private_telemetry = private or current.identity.profile == "Cliente"
        if current.busy:
            raise HTTPException(409, "Ya hay una consulta en curso en esta sesión.")
        current.busy = True
        started = time.perf_counter()
        status = "ok"
        result = {}
        scope = diagnostics.turn(json.dumps({"file_ids": file_ids}) if private_telemetry else question,
                                 token, conversation_id, current.identity.profile) if diagnostics is not None else nullcontext(None)
        with scope as observation:
            trace_id = observation.trace_id if observation is not None else uuid4().hex
            deadline_token = None
            try:
                async with inference:
                    if sessions.get(token) is not current or current.expires <= time.monotonic():
                        raise HTTPException(401, "La sesión terminó.")
                    try:
                        options = {}
                        if runner is run_agent or document_turn or report_turn:
                            from .conversation import Conversation
                            if current.conversations[conversation_id] is None:
                                current.conversations[conversation_id] = Conversation(
                                    current.identity, owner_id=current.owner_id, conversation_id=conversation_id)
                            options["conversation"] = current.conversations[conversation_id]
                        deadline_token = MODEL_DEADLINE.set(asyncio.get_running_loop().time() + 420)
                        with private_document_scope() if private_telemetry else nullcontext():
                            if report_turn:
                                result = await options["conversation"].ask_report(question, timeout=420)
                            elif document_turn:
                                async with asyncio.timeout(420):
                                    result = await options["conversation"].ask_documents(question, store=documents,
                                        file_ids=file_ids, owner=current.owner_id, conversation_id=conversation_id)
                            else:
                                result = await runner(question, session=current.identity, timeout=420, **options)
                    except Exception as error:
                        status = type(error).__name__
                        result = getattr(error, "run_result", result)
                        logging.getLogger(__name__).error("Falló la consulta web (%s), trace_id=%s", status, trace_id)
                        result["metrics"] = turn_metrics(result.get("trace", []),
                            elapsed_seconds=time.perf_counter()-started, execution_seconds=result.get("total_seconds"),
                            status=status, fallback=model_info)
                        return JSONResponse(status_code=504 if isinstance(error, TimeoutError) else 503,
                            content={
                                "detail": "No se pudo completar la consulta. Inténtalo de nuevo; no hay un resultado verificado.",
                                "trace_id": trace_id,
                                "metrics": result["metrics"],
                            })
                if sessions.get(token) is not current or current.expires <= time.monotonic():
                    raise HTTPException(401, "La sesión terminó.")
                if document_turn or "document_ids" in body.model_fields_set:
                    current.document_selection[conversation_id] = tuple(file_ids)
                result["metrics"] = turn_metrics(result.get("trace", []),
                    elapsed_seconds=time.perf_counter()-started, execution_seconds=result.get("total_seconds"),
                    status=status, fallback=model_info)
                return {**public_result(result), "trace_id": trace_id, "charts": result.get("charts", []),
                        **(public_attachment_data(result, question) if document_turn and not report_turn else {}),
                        **({"report_id": result["report_id"]} if "report_id" in result else {}),
                        **({"diagnostics": {"trace_id": observation.trace_id}} if observation is not None else {}),
                        "elapsed_seconds": round(time.perf_counter() - started, 3)}
            except HTTPException as error:
                if status == "ok":
                    status = f"http_{error.status_code}"
                raise
            except asyncio.CancelledError:
                status = "cancelled"
                raise
            finally:
                if deadline_token is not None:
                    MODEL_DEADLINE.reset(deadline_token)
                current.busy = False
                result.setdefault("metrics", turn_metrics(result.get("trace", []),
                    elapsed_seconds=time.perf_counter()-started, execution_seconds=result.get("total_seconds"),
                    status=status, fallback=model_info))
                if observation is not None:
                    try:
                        observation.finish({"metrics": result["metrics"]} if private_telemetry else result, status)
                        print(f"[langfuse] trace_id={observation.trace_id} estado=encolada", flush=True)
                    except Exception:
                        logging.getLogger(__name__).warning("No se pudo completar el diagnóstico del turno; la respuesta se conserva.")
                if metrics_path is not None:
                    metrics = {"turn_id": trace_id, "profile": current.identity.profile, **result["metrics"]}
                    try:
                        with Path(metrics_path).open("a", encoding="utf-8") as handle:
                            handle.write(json.dumps(metrics, ensure_ascii=False) + "\n")
                        print("[turno] " + json.dumps(metrics, ensure_ascii=False), flush=True)
                    except OSError:
                        logging.getLogger(__name__).exception("No se pudo guardar la métrica del turno")
                if audit_path is not None:
                    record = dict(trace_id=trace_id, profile=current.identity.profile, question=question, status=status,
                                  elapsed_seconds=round(time.perf_counter() - started, 3), **public_result(result))
                    trace = result.get("trace", [])
                    record["conversation_reused"] = next(
                        (event["reused"] for event in trace if event["event"] == "conversation_session"), None
                    )
                    record["plans"] = [
                        {key: event[key] for key in ("agent", "status", "plan", "sql_executed", "seconds") if key in event}
                        for event in trace if event["event"] == "validated_plan"
                    ]
                    record["trace"] = trace
                    if observation is not None:
                        record["langfuse_trace_id"] = observation.trace_id
                    record["charts"] = [{"type": chart["type"], "points": len(chart["points"])}
                                        for chart in result.get("charts", [])]
                    if private:
                        record = {"trace_id": trace_id, "file_ids": file_ids, "metrics": result["metrics"],
                                  "result_ids": (result.get("presentation") or {}).get("result_ids", [])}
                    try:
                        with Path(audit_path).open("a", encoding="utf-8") as handle:
                            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                    except OSError:
                        logging.getLogger(__name__).exception("No se pudo guardar la evidencia web")

    @app.post("/api/documents")
    async def upload_documents(request: Request):
        _, current = authenticated(request)
        if current.busy:
            raise HTTPException(409, "Espera a que termine la consulta.")
        # Límite también sin Content-Length: no se procesa multipart antes de acotar el cuerpo.
        limit = MAX_BYTES * MAX_FILES + 65536
        chunks, size = [], 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > limit:
                raise HTTPException(413, "La subida excede el límite total de 30 MiB.")
            chunks.append(chunk)
        request._body = b"".join(chunks)
        try:
            async with request.form(max_files=MAX_FILES, max_fields=2) as form:
                conversation_id = form.get("conversation_id")
                if not isinstance(conversation_id, str) or conversation_id not in current.conversations:
                    raise HTTPException(404, "Esta conversación no está disponible en tu sesión.")
                files = form.getlist("files")
                if not files or any(not hasattr(f, "read") for f in files):
                    raise HTTPException(422, "Adjunta archivos PDF, PNG o JPG.")
                payloads = [await f.read(MAX_BYTES + 1) for f in files]
                _, active = authenticated(request)
                if active is not current or conversation_id not in current.conversations:
                    raise HTTPException(404, "Esta conversación no está disponible en tu sesión.")
                uploaded = documents.save_many(current.owner_id, conversation_id, payloads, names=[f.filename for f in files])
                current.document_private.add(conversation_id)
                return {"documents": uploaded}
        except DocumentError as error:
            raise HTTPException(422, str(error)) from None

    @app.post("/api/documents/{file_id}/corrections")
    async def correct_document(file_id: str, body: DocumentCorrection, request: Request):
        _, current = authenticated(request)
        if current.busy:
            raise HTTPException(409, "Espera a que termine la consulta.")
        if body.conversation_id not in current.conversations:
            raise HTTPException(404, "Esta conversación no está disponible en tu sesión.")
        try:
            documents.authorized(file_id, current.owner_id, body.conversation_id)
        except DocumentError as error:
            raise HTTPException(404, str(error)) from None
        try:
            corrected = documents.correct(file_id, current.owner_id, body.conversation_id, body.campo, body.valor)
            return public_attachment_data({"documents": [corrected]}, "")["documents"][0]
        except DocumentError as error:
            raise HTTPException(422, str(error)) from None

    @app.get("/")
    async def index():
        return FileResponse(ASSETS / "index.html")

    @app.get("/assets/plotly.min.js")
    def plotly_script():
        package = find_spec("plotly")
        if package and package.origin:
            script = Path(package.origin).parent / "package_data" / "plotly.min.js"
            if script.is_file():
                return FileResponse(script, media_type="application/javascript")
        raise HTTPException(503, "Plotly no está instalado en el entorno local.")

    app.mount("/assets", StaticFiles(directory=ASSETS), name="assets")
    return app


def main():
    import os
    import uvicorn
    parser = argparse.ArgumentParser(description="Chat local de Banco Agentes")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--provider", choices=["ollama", "gemini"], help="Proveedor de modelo. Por defecto se usa Ollama.")
    parser.add_argument("--metrics-log", type=Path, default=Path("turn-metrics.jsonl"), help="Archivo JSONL con tiempo y tokens por turno.")
    parser.add_argument("--langfuse", action="store_true", help="Activa explícitamente el diagnóstico ADK hacia Langfuse.")
    args = parser.parse_args()
    if args.provider:
        os.environ["BANCO_MODEL_PROVIDER"] = args.provider
    os.environ.setdefault("REQUEST_TIMEOUT_SECONDS", "420")
    from .providers import validate_provider
    model_info = validate_provider()
    diagnostics = None
    if args.langfuse:
        from .diagnostics import start_diagnostics, DiagnosticConfigurationError
        try:
            diagnostics = start_diagnostics(enabled=True)
        except DiagnosticConfigurationError as error:
            parser.error(str(error))
        print("Langfuse habilitado: exportación de turnos ADK con filtrado de secretos.", flush=True)
    models = (f"hub={model_info['hub_model']} | spokes={model_info['spokes_model']}"
              if model_info['provider'] == "gemini" else model_info['model'])
    print(f"Banco Agentes | {model_info['provider']} | {models} | métricas: {args.metrics_log}", flush=True)
    uvicorn.run(create_app(metrics_path=args.metrics_log, model_info=model_info, diagnostics=diagnostics), host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
