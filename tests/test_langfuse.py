import asyncio
import json
import os
import unittest
from importlib.util import find_spec
from unittest.mock import patch

from fastapi.testclient import TestClient
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import Event,ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter,SpanExportResult
from opentelemetry.trace import Status,StatusCode,SpanContext,TraceFlags

from banco_agentes.diagnostics import (LangfuseDiagnostics,LangfuseSettings,SecretScrubber,
    SanitizedExporter,DiagnosticConfigurationError,ROOT_SPAN,start_diagnostics)
from web_fixtures import create_app


class MemoryExporter(SpanExporter):
    def __init__(self,fail=False):
        self.spans=[]
        self.fail=fail
    def export(self,spans):
        if self.fail:
            raise RuntimeError("exporter unavailable")
        self.spans.extend(spans)
        return SpanExportResult.SUCCESS
    def shutdown(self):
        pass
    def force_flush(self,timeout_millis=30000):
        return True


class FakeInstrumentor:
    is_instrumented_by_opentelemetry=False
    def instrument(self,**options):
        self.is_instrumented_by_opentelemetry=True
    def uninstrument(self):
        self.is_instrumented_by_opentelemetry=False


def diagnostics(exporter=None):
    return LangfuseDiagnostics(LangfuseSettings("http://localhost:3000","test-public-key","test-secret-key"),
                              exporter=exporter or MemoryExporter(),instrumentor=FakeInstrumentor())


def snapshot(span):
    return dict(name=span.name,attributes=dict(span.attributes),resource=dict(span.resource.attributes),
                events=[dict(name=e.name,attributes=dict(e.attributes or {})) for e in span.events],status=span.status.description)


class LangfuseConfigurationTests(unittest.TestCase):
    def test_disabled_does_not_load_keys_or_instrumentor(self):
        with patch("banco_agentes.diagnostics.LangfuseSettings.from_environment") as config,patch("banco_agentes.diagnostics.load_instrumentor") as instrument:
            self.assertIsNone(start_diagnostics())
        config.assert_not_called();instrument.assert_not_called()

    def test_only_environment_keys_do_not_enable_sending(self):
        with patch.dict(os.environ,{"LANGFUSE_ENABLED":"true","LANGFUSE_SECRET_KEY":"fake"}),patch("banco_agentes.diagnostics.LangfuseDiagnostics") as client:
            self.assertIsNone(start_diagnostics(False))
        client.assert_not_called()

    def test_configuration_fails_safely_and_rejects_credentials_in_url(self):
        with patch.dict(os.environ,{},clear=True),patch("banco_agentes.diagnostics.load_environment"):
            with self.assertRaisesRegex(DiagnosticConfigurationError,"LANGFUSE_BASE_URL"):
                LangfuseSettings.from_environment()
        environment=dict(LANGFUSE_BASE_URL="https://name:unsafe-secret@host.invalid",LANGFUSE_PUBLIC_KEY="dummy",LANGFUSE_SECRET_KEY="unsafe-secret")
        with patch.dict(os.environ,environment,clear=True),patch("banco_agentes.diagnostics.load_environment"):
            with self.assertRaises(DiagnosticConfigurationError) as error:
                LangfuseSettings.from_environment()
        self.assertNotIn("unsafe-secret",str(error.exception))

    def test_settings_repr_does_not_contain_keys(self):
        self.assertNotIn("the-secret",repr(LangfuseSettings("https://host.invalid","the-public","the-secret")))


class SecretTests(unittest.TestCase):
    def test_export_sanitizes_native_attributes_json_events_resource_and_status(self):
        scrub=SecretScrubber(["API-CANARY-731","DB-CANARY-91"])
        memory=MemoryExporter()
        exporter=SanitizedExporter(memory,scrub)
        context=SpanContext(1,2,False,TraceFlags(1))
        span=ReadableSpan("tool",context=context,attributes={"input.value":json.dumps({"api_key":"unknown-key","question":"API-CANARY-731", "n":2}),
            "tool.parameters":json.dumps({"password":"DB-CANARY-91","producto":"AHORRO"}),"llm.token_count.prompt":15},
            resource=Resource({"secret":"DB-CANARY-91"}),events=[Event("exception",{"message":"Bearer not-a-real-token"},1)],
            status=Status(StatusCode.ERROR,"API-CANARY-731"),start_time=1,end_time=2)
        self.assertEqual(exporter.export([span]),SpanExportResult.SUCCESS)
        value=json.dumps(snapshot(memory.spans[0]))
        for forbidden in ("API-CANARY-731","DB-CANARY-91","unknown-key","not-a-real-token"):
            self.assertNotIn(forbidden,value)
        self.assertIn("AHORRO",value)
        self.assertIn('"llm.token_count.prompt": 15',value)
        self.assertIn("API-CANARY-731",span.attributes["input.value"])

    def test_unknown_password_fields_and_uri_credentials_are_redacted(self):
        scrub=SecretScrubber([])
        value=scrub.clean(dict(password="not-in-env",headers={"Authorization":"Basic fake-auth"},
                              uri="postgresql://user:password@localhost/banco"))
        self.assertEqual(value["password"],"[REDACTED]")
        self.assertNotIn("user:password",value["uri"])
        self.assertNotIn("fake-auth",json.dumps(value))


class DiagnosticWebTests(unittest.TestCase):
    def test_each_turn_has_root_final_answer_metrics_and_no_cookie(self):
        memory=MemoryExporter()
        diag=diagnostics(memory)
        async def reply(question,*,session,timeout):
            with diag.tracer.start_as_current_span("data_analytics") as spoke:
                with diag.tracer.start_as_current_span("consultar_datos") as tool:
                    tool.set_attribute("input.value",json.dumps({"producto":"AHORRO","api_key":"test-secret-key"}))
                    tool.set_attribute("output.value",json.dumps({"valor":"26.00"}))
            return dict(answer="El saldo sintético es 26,00.",total_seconds=.01,trace=[dict(event="model_call",agent="orquestador",provider="gemini",model="gemini-3.5-flash-lite",status="completed",seconds=.01,usage={"prompt_token_count":5,"candidates_token_count":2,"total_token_count":7})])
        app=create_app(reply,audit_path=None,diagnostics=diag)
        global_provider=trace.get_tracer_provider()
        with TestClient(app,base_url="http://localhost") as client:
            self.assertEqual(client.post("/api/chat",json={"question":"prelogin"}).status_code,401)
            client.post("/api/login",json={"username":"analista","password":"AnalistaDemo!2026"})
            cookie=next(iter(client.cookies.values()))
            conversation=client.post("/api/conversations",json={}).json()["conversation_id"]
            results=[client.post("/api/chat",json={"question":q,"conversation_id":conversation}).json() for q in ("primera pregunta","segunda pregunta")]
        roots=[s for s in memory.spans if s.name==ROOT_SPAN]
        self.assertEqual(len(roots),2)
        self.assertNotEqual(roots[0].context.trace_id,roots[1].context.trace_id)
        self.assertEqual(roots[0].attributes["session.id"],roots[1].attributes["session.id"])
        for root,response in zip(roots,results):
            self.assertEqual(response["diagnostics"]["trace_id"],format(root.context.trace_id,"032x"))
            self.assertEqual(json.loads(root.attributes["langfuse.observation.output"])["answer"],response["answer"])
            self.assertEqual(json.loads(root.attributes["langfuse.observation.metadata.metrics"])["total_tokens"],7)
            self.assertGreater(root.end_time,root.start_time)
            child=next(s for s in memory.spans if s.name=="data_analytics" and s.context.trace_id==root.context.trace_id)
            self.assertEqual(child.parent.span_id,root.context.span_id)
            tool=next(s for s in memory.spans if s.name=="consultar_datos" and s.context.trace_id==root.context.trace_id)
            self.assertEqual(tool.parent.span_id,child.context.span_id)
            self.assertIn("AHORRO",tool.attributes["input.value"])
            self.assertIn("26.00",tool.attributes["output.value"])
        serialized=json.dumps([snapshot(s) for s in memory.spans])
        self.assertNotIn(cookie,serialized)
        self.assertNotIn("test-secret-key",serialized)
        self.assertNotIn("AnalistaDemo!2026",serialized)
        self.assertIs(global_provider,trace.get_tracer_provider())

    def test_failed_turn_is_recorded_and_export_failure_does_not_change_answer(self):
        memory=MemoryExporter()
        diag=diagnostics(memory)
        async def fail(question,*,session,timeout):
            raise TimeoutError("test-secret-key")
        with TestClient(create_app(fail,audit_path=None,diagnostics=diag),base_url="http://localhost") as client:
            client.post("/api/login",json={"username":"analista","password":"AnalistaDemo!2026"})
            with self.assertLogs("banco_agentes.web",level="ERROR"):
                self.assertEqual(client.post("/api/chat",json={"question":"test"}).status_code,504)
        root=next(s for s in memory.spans if s.name==ROOT_SPAN)
        self.assertEqual(root.status.status_code,StatusCode.ERROR)
        self.assertNotIn("test-secret-key",json.dumps(snapshot(root)))
        memory=MemoryExporter(fail=True)
        diag=diagnostics(memory)
        async def ok(question,*,session,timeout):
            return {"answer":"Respuesta intacta"}
        with self.assertLogs("banco_agentes.diagnostics",level="WARNING"):
            with TestClient(create_app(ok,audit_path=None,diagnostics=diag),base_url="http://localhost") as client:
                client.post("/api/login",json={"username":"analista","password":"AnalistaDemo!2026"})
                response=client.post("/api/chat",json={"question":"test"})
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json()["answer"],"Respuesta intacta")

    def test_partial_turn_is_labeled_without_inventing_tokens(self):
        memory=MemoryExporter();diag=diagnostics(memory)
        with diag.turn("question","opaque-cookie","conversation","Analista") as turn:
            turn.finish({"answer":"Resultado parcial","metrics":{"status":"partial","total_tokens":None}},"ok")
        diag.close()
        root=memory.spans[0]
        self.assertEqual(root.attributes["langfuse.observation.level"],"WARNING")
        output=json.loads(root.attributes["output.value"])
        self.assertEqual(output["status"],"partial")
        self.assertEqual(output["answer"],"Resultado parcial")
        self.assertIsNone(json.loads(root.attributes["langfuse.observation.metadata.metrics"])["total_tokens"])

    def test_outside_turn_spans_are_not_exported(self):
        memory=MemoryExporter();diag=diagnostics(memory)
        with diag.tracer.start_as_current_span("unrelated"):
            pass
        with diag.turn("question","opaque-cookie","conversation","Analista") as turn:
            turn.finish({"answer":"OK"},"ok")
        diag.close()
        self.assertEqual([s.name for s in memory.spans],[ROOT_SPAN])


class OfficialInstrumentorTests(unittest.IsolatedAsyncioTestCase):
    @unittest.skipUnless(find_spec("openinference") is not None,"Falta la dependencia optativa OpenInference")
    async def test_official_adk_captures_delegation_and_actual_tool_io(self):
        from banco_agentes.conversation import Conversation
        from banco_agentes.governance import DemoSession
        from test_general import PlannedModel,simulated_plan
        memory=MemoryExporter()
        diag=LangfuseDiagnostics(LangfuseSettings("http://localhost:3000","test-public","test-secret"),exporter=memory)
        try:
            with diag.turn("Total de CHEQUES marzo","session","conversation","Analista") as root:
                conversation=Conversation(DemoSession(),model_factory=lambda name:PlannedModel(model="test",agent=name))
                with patch("banco_agentes.general_agents.execute_plan",side_effect=simulated_plan):
                    result=await conversation.ask("Total de CHEQUES marzo")
                root.finish(result,"ok")
        finally:
            diag.close()
        serialized=json.dumps([snapshot(s) for s in memory.spans])
        self.assertIn("data_analytics",serialized)
        tools=[s for s in memory.spans if "consultar_datos" in s.name]
        self.assertTrue(tools)
        self.assertTrue(any("input.value" in s.attributes and "output.value" in s.attributes for s in tools))
        self.assertEqual(len({s.context.trace_id for s in memory.spans}),1)


class OTLPSerializationTests(unittest.TestCase):
    def test_real_http_exporter_sends_only_sanitized_protobuf(self):
        from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
        import threading
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.exporter.otlp.proto.http import Compression
        from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
        from google.protobuf.json_format import MessageToDict
        bodies=[]
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                bodies.append(self.rfile.read(int(self.headers["Content-Length"])))
                self.send_response(200);self.end_headers()
            def log_message(self,*args):
                pass
        server=ThreadingHTTPServer(("127.0.0.1",0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        exporter=OTLPSpanExporter(endpoint=f"http://127.0.0.1:{server.server_port}/api/public/otel/v1/traces",headers={"Authorization":"Basic fake"},timeout=1,compression=Compression.NoCompression)
        diag=diagnostics(exporter)
        try:
            with diag.turn("Pregunta test-secret-key", "private-cookie", "test-conversation", "Analista") as root:
                root.finish({"answer":"Respuesta test-secret-key","metrics":{"total_tokens":7}},"ok")
            diag.close()
        finally:
            server.shutdown();server.server_close();thread.join(timeout=2)
        self.assertTrue(bodies)
        request=ExportTraceServiceRequest();request.ParseFromString(bodies[0])
        text=json.dumps(MessageToDict(request))
        self.assertIn(ROOT_SPAN,text)
        self.assertNotIn("test-secret-key",text)
        self.assertNotIn("private-cookie",text)
        self.assertIn("[REDACTED]",text)


class RemoteReceiptTests(unittest.TestCase):
    def test_receipt_requires_final_output_connected_tools_and_usage(self):
        import importlib.util
        from pathlib import Path
        spec=importlib.util.spec_from_file_location("langfuse_probe",Path(__file__).parents[1]/"scripts/verify_langfuse_web.py")
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        trace_id="a"*32
        def row(id,name,parent,**data):
            return dict(id=id,name=name,parentObservationId=parent,traceId=trace_id,startTime="start",endTime="end",**data)
        rows=[row("root",ROOT_SPAN,None,input=json.dumps({"question":module.QUESTION}),output=json.dumps({"answer":"Respuesta"}),metadata={"metrics":{"total_tokens":7}}),
              row("agent","data_analytics","root",type="TOOL"),row("tool","consultar_datos","agent",input={"producto":"AHORRO"},output={"valor":"26"}),
              row("llm","call_llm","agent",type="GENERATION",usageDetails={"input":5,"output":2,"total":7})]
        self.assertTrue(all(module.trace_checks(rows,trace_id,answer="Respuesta",expected_tokens=7).values()))
        rows[2]["parentObservationId"]="missing"
        self.assertFalse(module.trace_checks(rows,trace_id,answer="Respuesta",expected_tokens=7)["tree_connected"])
        rows[2]["output"]=None
        self.assertFalse(module.trace_checks(rows,trace_id)["tool_parameters_and_result"])
