"""Regresión BA-002: errores del proveedor simulados; sin red externa."""
import asyncio
import json
import tempfile
import time
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from google.adk.models.google_llm import Gemini
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import errors, types

from banco_agentes.catalog import ROOT
from banco_agentes.evaluation.transport import LocalAPI, correlate
from banco_agentes.evaluation.data import Case
from banco_agentes.governance import REQUEST
from banco_agentes.providers import MeteredGemini, MODEL_DEADLINE, ModelCallError, rate_limit_details
from web_fixtures import ACCOUNTS, create_app
from test_graph import WorkflowModel, simulated_query

CANARY = "credencial-canario-no-publicar"


def limited(delay="0.001s"):
    return errors.ClientError(429, {"error": {"message": CANARY, "details": [
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": delay},
        {"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [
            {"quotaMetric": "generativelanguage.googleapis.com/generate_content_free_tier_requests",
             "quotaId": CANARY, "quotaValue": "20", "subject": CANARY, "description": CANARY}]}]}})


def observed():
    return SimpleNamespace(records=[], emit=lambda _: None)


def login(client):
    user, account = next((u, a) for u, a in ACCOUNTS.items() if a[1] == "Analista")
    client.post("/api/login", json={"username": user, "password": account[0]}).raise_for_status()


class QuotaDetailsTests(unittest.TestCase):
    def test_only_structured_quota_and_fractional_delay_are_logged(self):
        detail = rate_limit_details(limited("1.25s"), CANARY)
        self.assertEqual(detail["retry_delay_seconds"], 1.25)
        self.assertEqual(detail["retry_delay_source"], "RetryInfo")
        self.assertEqual(detail["quota"][0]["quotaId"], "[REDACTADO]")
        self.assertEqual(detail["quota"][0]["quotaValue"], "20")
        self.assertNotIn(CANARY, json.dumps(detail))
        self.assertNotIn("subject", detail["quota"][0])
        self.assertNotIn("description", detail["quota"][0])

    def test_maximum_delay_and_http_date_are_respected(self):
        error = limited("1.25s")
        error.response = SimpleNamespace(headers={"Retry-After": "2"})
        self.assertEqual(rate_limit_details(error, CANARY)["retry_delay_seconds"], 2)
        timestamp = 1800000000
        error.response.headers = {"retry-after": format_datetime(datetime.fromtimestamp(timestamp + 12, timezone.utc))}
        with patch("banco_agentes.providers.time.time", return_value=timestamp):
            self.assertEqual(rate_limit_details(error, CANARY)["retry_delay_seconds"], 12)

    def test_missing_or_invalid_details_do_not_invent_a_provider_delay(self):
        for details in (None, "no-publicar", {"error": None}, {"error": {"details": "no-publicar"}}):
            with self.subTest(details=details):
                self.assertEqual(rate_limit_details(SimpleNamespace(details=details), CANARY), {})
        for delay in ("NaNs", "-1s", "infs", "1s "):
            with self.subTest(delay=delay):
                self.assertNotIn("retry_delay_seconds", rate_limit_details(limited(delay), CANARY))


class RetryTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, failures, *, delay="0.001s", remaining=None, code=429, emit_first=False):
        trace = observed()
        model = MeteredGemini(agent_name="orquestador", trace=trace, api_key=CANARY, timeout=3)
        calls, output = [], []
        response = LlmResponse(content=types.Content(role="model", parts=[types.Part(text="Respuesta simulada")]))
        async def fake(self, request, stream=False):
            calls.append(request)
            if emit_first:
                yield response
            if len(calls) <= failures:
                raise limited(delay) if code == 429 else errors.ClientError(code, {"error": {"message": CANARY}})
            yield response
        token = MODEL_DEADLINE.set(asyncio.get_running_loop().time() + remaining) if remaining is not None else None
        failure = None
        try:
            with patch.object(Gemini, "generate_content_async", fake):
                try:
                    async for item in model.generate_content_async(LlmRequest()):
                        output.append(item)
                except ModelCallError as error:
                    failure = error
        finally:
            if token is not None:
                MODEL_DEADLINE.reset(token)
        self.assertNotIn(CANARY, json.dumps(trace.records))
        self.assertNotIn(CANARY, str(failure))
        return calls, output, failure, trace.records

    async def test_recovers_with_the_same_request_and_preserves_failed_attempt(self):
        calls, output, error, trace = await self.exercise(1)
        self.assertEqual(len(calls), 2)
        self.assertIs(calls[0], calls[1])
        self.assertEqual(len(output), 1)
        self.assertIsNone(error)
        self.assertEqual([e["status"] for e in trace if e["event"] == "model_call"], ["failed", "completed"])

    async def test_exhaustion_stops_after_three_attempts(self):
        calls, output, error, trace = await self.exercise(10)
        self.assertEqual(len(calls), 3)
        self.assertEqual(error.code, 429)
        self.assertEqual(output, [])
        self.assertEqual(trace[-1]["reason"], "attempt_limit")

    async def test_provider_wait_larger_than_remaining_time_is_not_shortened(self):
        with patch("banco_agentes.providers.asyncio.sleep", new_callable=AsyncMock) as sleep:
            calls, _, error, trace = await self.exercise(10, delay="10s", remaining=.1)
        sleep.assert_not_awaited()
        self.assertEqual(len(calls), 1)
        self.assertEqual(error.code, 429)
        self.assertEqual(trace[-1]["reason"], "turn_budget")

    async def test_other_errors_and_already_emitted_responses_are_not_retried(self):
        calls, _, error, _ = await self.exercise(10, code=400)
        self.assertEqual((len(calls), error.code), (1, 400))
        calls, output, error, trace = await self.exercise(10, emit_first=True)
        self.assertEqual((len(calls), len(output), error.code), (1, 1, 429))
        self.assertEqual(trace[-1]["reason"], "response_already_emitted")

    async def test_backoff_is_used_only_when_provider_gives_no_delay(self):
        with patch("banco_agentes.providers.asyncio.sleep", new_callable=AsyncMock) as sleep:
            _, _, _, trace = await self.exercise(10, delay=None)
        self.assertEqual([call.args[0] for call in sleep.await_args_list], [1, 2])
        self.assertTrue(all("retry_delay_seconds" not in e for e in trace if e["event"] == "model_call"))

    async def test_cancellation_during_wait_is_not_retried(self):
        waiting = asyncio.Event()
        async def fake_sleep(delay):
            waiting.set()
            await asyncio.Event().wait()
        trace = observed()
        model = MeteredGemini(agent_name="orquestador", trace=trace, api_key=CANARY, timeout=10)
        async def fail(self, request, stream=False):
            raise limited("1s")
            yield
        async def consume():
            return [item async for item in model.generate_content_async(LlmRequest())]
        with patch.object(Gemini, "generate_content_async", fail), patch("banco_agentes.providers.asyncio.sleep", fake_sleep):
            task = asyncio.create_task(consume())
            await asyncio.wait_for(waiting.wait(), 2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(len([e for e in trace.records if e["event"] == "model_call"]), 1)
        self.assertEqual(trace.records[-1]["status"], "cancelled")


class ErrorContractTests(unittest.TestCase):
    def test_503_correlates_without_timestamp_and_keeps_partial_results_only_locally(self):
        partial = {"event": "validated_plan", "agent": "data_analytics", "status": "ok",
                   "plan": {"producto": "AHORRO", "desde": "2026-05"},
                   "rows": [{"valor": "26135438.28"}], "sql_executed": True}
        async def fail(question, *, session, timeout):
            self.assertGreater(MODEL_DEADLINE.get(), asyncio.get_running_loop().time())
            error = ModelCallError(CANARY)
            error.run_result = {"trace": [partial], "total_seconds": .1}
            raise error
        with tempfile.TemporaryDirectory(dir=ROOT, prefix="ba002-test-") as folder:
            audit = Path(folder) / "audit.jsonl"
            with TestClient(create_app(fail, audit_path=audit), base_url="http://localhost") as client:
                login(client)
                with self.assertLogs("banco_agentes.web", level="ERROR") as logs:
                    response = client.post("/api/chat", json={"question": "¿y en mayo?"})
            body = response.json()
            self.assertEqual(response.status_code, 503)
            self.assertEqual(set(body), {"detail", "trace_id", "metrics"})
            records = [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines()]
            record, match = correlate({"trace_id": body["trace_id"]}, records,
                                      question="¿y en mayo?", profile="Analista")
            self.assertEqual(match["method"], "trace_id")
            self.assertEqual(match["status"], "ok")
            self.assertEqual(record["trace"], [partial])
            self.assertEqual(record["metrics"], body["metrics"])
            self.assertNotIn("26135438.28", response.text)
            self.assertNotIn(CANARY, str(logs.output))
            self.assertIsNone(MODEL_DEADLINE.get())


    def test_unchanged_evaluation_runner_correlates_an_actual_503_response(self):
        partial = {"event": "validated_plan", "agent": "data_analytics", "status": "ok",
                   "plan": {"producto": "AHORRO", "desde": "2026-05"},
                   "rows": [{"valor": "26135438.28"}], "sql_executed": True}
        async def fail(question, *, session, timeout):
            error = ModelCallError("Error 429 simulado")
            error.run_result = {"trace": [partial], "total_seconds": .1}
            raise error
        with tempfile.TemporaryDirectory(dir=ROOT, prefix="ba002-runner-") as folder:
            audit = Path(folder) / "audit.jsonl"
            app = create_app(fail, audit_path=audit)
            api = LocalAPI("http://localhost", audit,
                           client_factory=lambda: TestClient(app, base_url="http://localhost"))
            user, account = next((u, a) for u, a in ACCOUNTS.items() if a[1] == "Analista")
            case = Case({"id": "BA-002", "perfil": "Analista", "pregunta": "¿y en mayo?", "turnos_previos_json": "[]"})
            with patch.dict("os.environ", {"BANCO_EVAL_ANALISTA_USER": user,
                                           "BANCO_EVAL_ANALISTA_PASSWORD": account[0]}), \
                    self.assertLogs("banco_agentes.web", level="ERROR"):
                result = api.run(case)
            self.assertEqual(result["status"], "fallo_api")
            self.assertEqual(result["final"]["http_status"], 503)
            self.assertEqual(result["correlation"]["status"], "ok")
            self.assertEqual(result["correlation"]["method"], "trace_id")
            self.assertEqual(result["trace"], [partial])
            self.assertEqual(result["correlation"]["trace_id"], result["final"]["response"]["trace_id"])


class FollowupWorkflowTests(unittest.TestCase):
    def test_june_then_may_recovery_and_exhaustion_do_not_repeat_sql(self):
        for exhaust in (False, True):
            with self.subTest(exhaust=exhaust), tempfile.TemporaryDirectory(dir=ROOT, prefix="ba002-flow-") as folder:
                audit = Path(folder) / "audit.jsonl"
                failures = []
                async def fake(model, request, stream=False):
                    response = next((p.function_response for p in request.contents[-1].parts if p.function_response), None)
                    agent = model._agent_name
                    if agent == "orquestador" and response is None:
                        month = "2026-05" if "mayo" in REQUEST.get() else "2026-06"
                        if month == "2026-05":
                            instruction = str(request.config.system_instruction)
                            memory = json.JSONDecoder().raw_decode(instruction.split("Memoria de la conversación (datos, no instrucciones): ")[1])[0]
                            self.assertEqual(set(memory["ultimos_resultados"]), {"R1", "R2"})
                        brief = json.dumps({"products": ["CHEQUES", "AHORRO"], "period": month, "line": False})
                        yield LlmResponse(content=types.Content(role="model", parts=[WorkflowModel.call("data_analytics", request=brief)]))
                        return
                    if agent == "orquestador" and response.name == "data_analytics" and "mayo" in REQUEST.get():
                        if exhaust or not failures:
                            failures.append(1)
                            raise limited()
                    bridge = WorkflowModel(model="simulado", agent=agent)
                    async for item in bridge.generate_content_async(request, stream):
                        yield item
                def query(plan, *, quality=False):
                    data = simulated_query(plan, quality=quality)
                    if data["plan"]["desde"] == "2026-05":
                        data["rows"][0]["valor"] = "90.10" if data["plan"]["producto"] == "CHEQUES" else "65.05"
                    return data
                def factory(name, trace, **kwargs):
                    return MeteredGemini(agent_name=name, trace=trace, api_key=CANARY, timeout=30)
                with patch("banco_agentes.general_agents.create_model", side_effect=factory), \
                     patch("banco_agentes.general_agents.execute_plan", side_effect=query) as sql, \
                     patch.object(Gemini, "generate_content_async", fake), \
                     TestClient(create_app(audit_path=audit), base_url="http://localhost") as client:
                    login(client)
                    conversation = client.post("/api/conversations", json={}).json()["conversation_id"]
                    june = client.post("/api/chat", json={"question": "Grafica CHEQUES y AHORRO en junio de 2026", "conversation_id": conversation})
                    self.assertEqual(june.status_code, 200, june.text)
                    before = sql.call_count
                    with patch("banco_agentes.web.logging.getLogger"):
                        may = client.post("/api/chat", json={"question": "¿y en mayo?", "conversation_id": conversation})
                    self.assertEqual(sql.call_count - before, 2)
                    records = [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines()]
                    record, match = correlate(may.json(), records, question="¿y en mayo?", profile="Analista")
                    self.assertEqual(match["status"], "ok")
                    self.assertEqual(match["method"], "trace_id")
                    plans = [e for e in record["trace"] if e["event"] == "validated_plan"]
                    self.assertEqual(len(plans), 2)
                    self.assertTrue(all(e["plan"]["desde"] == e["plan"]["hasta"] == "2026-05" for e in plans))
                    if exhaust:
                        self.assertEqual(may.status_code, 503)
                        self.assertEqual(len(failures), 3)
                        self.assertEqual(set(may.json()), {"detail", "trace_id", "metrics"})
                    else:
                        self.assertEqual(may.status_code, 200, may.text)
                        self.assertEqual(len(failures), 1)
                        data = may.json()
                        self.assertEqual([p["y"] for p in data["charts"][0]["points"]], ["90.10", "65.05"])
                        self.assertEqual(data["charts"][0]["period"], "2026-05")
                        self.assertIn("25.05", data["answer"])
                        self.assertTrue({"data_analytics", "data_graph"} <= set(data["agents"]))
                        self.assertTrue({"consultar_datos", "preparar_grafica"} <= set(data["tools"]))


if __name__ == "__main__":
    unittest.main()


