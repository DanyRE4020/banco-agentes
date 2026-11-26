"""Diagnóstico optativo: Google ADK OpenInference -> OTLP oficial de Langfuse.

El exportador recibe únicamente copias saneadas de los spans del turno web.
No se registra un proveedor global ni se modifica el flujo de los agentes.
"""
import base64
import hashlib
import json
import logging
import os
import re
import secrets
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass,field
from importlib.metadata import version,PackageNotFoundError
from urllib.parse import urlsplit

from dotenv import dotenv_values
from opentelemetry.context import Context
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.util.instrumentation import InstrumentationScope
from opentelemetry.sdk.trace import TracerProvider,ReadableSpan,SpanProcessor,Event
from opentelemetry.sdk.trace.export import BatchSpanProcessor,SpanExporter,SpanExportResult
from opentelemetry.trace import Status,StatusCode

from .catalog import ROOT
from .providers import load_environment

ROOT_SPAN="banco-agentes.web.turn"
ACTIVE_TURN=ContextVar("banco_langfuse_turn",default=None)
SENSITIVE=re.compile(r"api[_-]?key|password|passwd|secret|authorization|cookie|credential|private[_-]?key|(?:access|refresh)[_-]?token",re.I)
REDACTED="[REDACTED]"
LOG=logging.getLogger(__name__)


class DiagnosticConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class LangfuseSettings:
    base_url: str
    public_key: str=field(repr=False)
    secret_key: str=field(repr=False)

    @classmethod
    def from_environment(cls):
        load_environment()
        names=("LANGFUSE_BASE_URL","LANGFUSE_PUBLIC_KEY","LANGFUSE_SECRET_KEY")
        missing=[name for name in names if not os.getenv(name,"").strip()]
        if missing:
            raise DiagnosticConfigurationError("Faltan variables en el servidor: "+", ".join(missing))
        base=os.environ["LANGFUSE_BASE_URL"].strip().rstrip("/")
        parsed=urlsplit(base)
        if (not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.scheme not in {"https","http"}
            or (parsed.scheme=="http" and parsed.hostname not in {"localhost","127.0.0.1","::1"})):
            raise DiagnosticConfigurationError("LANGFUSE_BASE_URL requiere HTTPS o HTTP local, sin credenciales ni parámetros.")
        return cls(base,os.environ["LANGFUSE_PUBLIC_KEY"].strip(),os.environ["LANGFUSE_SECRET_KEY"].strip())


class SecretScrubber:
    def __init__(self,values):
        self._values=sorted({str(value) for value in values if value},key=len,reverse=True)

    @classmethod
    def configured(cls,settings):
        values=[value for key,value in os.environ.items() if SENSITIVE.search(key) or key.upper().endswith("_TOKEN")]
        local=ROOT/".env.postgres"
        if local.exists():
            values.extend(value for key,value in dotenv_values(local,encoding="utf-8-sig").items() if SENSITIVE.search(key))
        values.extend([settings.public_key,settings.secret_key])
        encoded=base64.b64encode(f"{settings.public_key}:{settings.secret_key}".encode()).decode()
        values.append(encoded)
        return cls(values)

    def contains_known_secret(self,value):
        return any(secret in value for secret in self._values)

    def text(self,value):
        for secret in self._values:
            value=value.replace(secret,REDACTED)
        value=re.sub(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9_+/=.:-]+",r"\1 [REDACTED]",value)
        value=re.sub(r"(?i)([a-z][a-z0-9+.-]*://)[^/\s@]+:[^/\s@]+@",r"\1[REDACTED]@",value)
        value=re.sub(r"(?i)((?:api[_-]?key|password|secret|access_token|refresh_token)\s*[=:]\s*)[^\s&,;]+",r"\1[REDACTED]",value)
        return value

    def clean(self,value):
        if isinstance(value,dict):
            return {self.text(str(key)):REDACTED if SENSITIVE.search(str(key)) or str(key).lower()=="token" else self.clean(item)
                    for key,item in value.items()}
        if isinstance(value,(tuple,list)):
            return [self.clean(item) for item in value]
        if isinstance(value,str):
            if value.lstrip().startswith(("{","[")):
                try:
                    decoded=json.loads(value)
                except (ValueError,TypeError):
                    pass
                else:
                    return json.dumps(self.clean(decoded),ensure_ascii=False)
            return self.text(value)
        return value

    def attributes(self,attributes):
        return self.clean(dict(attributes or {}))


class SafeLogFilter(logging.Filter):
    def __init__(self,scrubber):
        super().__init__()
        self.scrubber=scrubber

    def filter(self,record):
        record.msg=self.scrubber.text(record.getMessage())
        record.args=()
        record.exc_info=None
        record.exc_text=None
        return True


class SanitizedExporter(SpanExporter):
    def __init__(self,delegate,scrubber):
        self.delegate=delegate
        self.scrubber=scrubber
        self.exported_spans=0
        self.failed_batches=0
        self._lock=threading.Lock()

    def export(self,spans):
        safe=[]
        try:
            for span in spans:
                clean=self.scrubber
                source_scope=span.instrumentation_scope
                scope=InstrumentationScope(clean.text(source_scope.name),clean.text(source_scope.version) if source_scope.version else None,
                    clean.text(source_scope.schema_url) if source_scope.schema_url else None,clean.attributes(source_scope.attributes)) if source_scope else None
                # Eventos/excepciones y recursos también pueden contener secretos.
                events=[Event(clean.text(e.name),clean.attributes(e.attributes),e.timestamp) for e in span.events]
                safe.append(ReadableSpan(name=clean.text(span.name),context=span.context,parent=span.parent,
                    resource=Resource(clean.attributes(span.resource.attributes),span.resource.schema_url),
                    attributes=clean.attributes(span.attributes),events=events,links=(),kind=span.kind,
                    status=Status(span.status.status_code,clean.text(span.status.description) if span.status.description else None),
                    start_time=span.start_time,end_time=span.end_time,instrumentation_scope=scope))
            result=self.delegate.export(safe)
        except Exception:
            result=SpanExportResult.FAILURE
        with self._lock:
            if result==SpanExportResult.SUCCESS:
                self.exported_spans+=len(safe)
            else:
                self.failed_batches+=1
        if result!=SpanExportResult.SUCCESS:
            LOG.warning("Langfuse: no se confirmó la exportación de un lote; la respuesta del chat se conserva.")
        return result

    def shutdown(self):
        self.delegate.shutdown()

    def force_flush(self,timeout_millis=30000):
        return self.delegate.force_flush(timeout_millis)


class TurnSpanProcessor(SpanProcessor):
    def __init__(self,batch):
        self.batch=batch

    def on_start(self,span,parent_context=None):
        active=ACTIVE_TURN.get()
        if active is not None:
            for key,value in active.items():
                span.set_attribute(key,value)
            span.set_attribute("banco.web.turn",True)

    def on_end(self,span):
        if (span.attributes or {}).get("banco.web.turn") is True:
            self.batch.on_end(span)

    def force_flush(self,timeout_millis=30000):
        return self.batch.force_flush(timeout_millis)

    def shutdown(self):
        self.batch.shutdown()


def load_instrumentor():
    try:
        from openinference.instrumentation.google_adk import GoogleADKInstrumentor
        if version("openinference-instrumentation-google-adk")!="0.1.28":
            raise DiagnosticConfigurationError("Instala la versión compatible fijada en requirements-diagnostics.txt.")
        return GoogleADKInstrumentor()
    except (ImportError,PackageNotFoundError):
        raise DiagnosticConfigurationError("Falta la integración ADK. Ejecuta python -m pip install -r requirements-diagnostics.txt.") from None


class DiagnosticTurn:
    def __init__(self,span,scrubber):
        self.span=span
        self.scrubber=scrubber
        self.trace_id=format(span.get_span_context().trace_id,"032x")

    def finish(self,result,status):
        # La salida de raíz es la respuesta que construyó el backend para la web,
        # no el JSON intermedio del hub. No duplicar tokens como otra generación.
        execution_status=(result.get("metrics") or {}).get("status",status) if status=="ok" else status
        output={"answer":result.get("answer","") if status=="ok" else "","status":execution_status}
        attributes={"langfuse.observation.output":json.dumps(self.scrubber.clean(output),ensure_ascii=False),
                    "output.value":json.dumps(self.scrubber.clean(output),ensure_ascii=False),
                    "langfuse.observation.metadata.metrics":json.dumps(self.scrubber.clean(result.get("metrics",{})),ensure_ascii=False),
                    "langfuse.observation.metadata.status":execution_status}
        for key,value in attributes.items():
            self.span.set_attribute(key,value)
        if execution_status=="partial":
            self.span.set_attribute("langfuse.observation.level","WARNING")
        if status!="ok":
            self.span.set_status(Status(StatusCode.ERROR,"El turno no se completó: "+status))


class LangfuseDiagnostics:
    def __init__(self,settings,*,exporter=None,instrumentor=None):
        self.settings=settings
        self._salt=secrets.token_bytes(32)
        self.scrubber=SecretScrubber.configured(settings)
        self.instrumentor=instrumentor or load_instrumentor()
        self._log_filter=SafeLogFilter(self.scrubber)
        self._export_log=logging.getLogger("opentelemetry.exporter.otlp.proto.http.trace_exporter")
        self._export_log.addFilter(self._log_filter)
        if exporter is None:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
            auth=base64.b64encode(f"{settings.public_key}:{settings.secret_key}".encode()).decode()
            exporter=OTLPSpanExporter(endpoint=settings.base_url+"/api/public/otel/v1/traces",
                headers={"Authorization":"Basic "+auth,"x-langfuse-ingestion-version":"4"},timeout=3)
        self.exporter=SanitizedExporter(exporter,self.scrubber)
        self.provider=TracerProvider(resource=Resource({"service.name":"banco-agentes-web"}),shutdown_on_exit=False)
        self.provider.add_span_processor(TurnSpanProcessor(BatchSpanProcessor(self.exporter,schedule_delay_millis=1000,
                                      max_queue_size=2048,max_export_batch_size=64,export_timeout_millis=3000)))
        self.tracer=self.provider.get_tracer("banco_agentes.web.diagnostics")
        self.instrumentor.instrument(tracer_provider=self.provider)
        if not self.instrumentor.is_instrumented_by_opentelemetry:
            self.provider.shutdown()
            raise DiagnosticConfigurationError("La integración ADK no pudo activarse con las dependencias instaladas.")

    @contextmanager
    def turn(self,question,session_key,conversation_id,profile):
        session_id=hashlib.sha256(self._salt+session_key.encode()+conversation_id.encode()).hexdigest()
        active={"session.id":session_id,"langfuse.trace.name":ROOT_SPAN,
                "langfuse.trace.tags":["banco-agentes","web","datos-sinteticos"],
                "langfuse.observation.metadata.profile":profile}
        token=ACTIVE_TURN.set(active)
        try:
            # Cada turno es una traza nueva, independiente de headers entrantes.
            with self.tracer.start_as_current_span(ROOT_SPAN,context=Context(),record_exception=False,
                                                 set_status_on_exception=False) as span:
                span.set_attribute("langfuse.observation.type","chain")
                data=json.dumps(self.scrubber.clean({"question":question}),ensure_ascii=False)
                span.set_attribute("langfuse.observation.input",data)
                span.set_attribute("input.value",data)
                yield DiagnosticTurn(span,self.scrubber)
        finally:
            ACTIVE_TURN.reset(token)

    def close(self):
        self.provider.force_flush(timeout_millis=3000)
        self.instrumentor.uninstrument()
        self.provider.shutdown()
        self._export_log.removeFilter(self._log_filter)


def start_diagnostics(enabled=False):
    if not enabled:
        return None
    return LangfuseDiagnostics(LangfuseSettings.from_environment())
