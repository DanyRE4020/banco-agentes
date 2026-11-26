import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient
from google.adk.models.google_llm import Gemini
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types

from banco_agentes.catalog import ROOT
from banco_agentes.providers import MeteredGemini, ModelCallError, create_model, model_settings
from banco_agentes.telemetry import turn_metrics
from web_fixtures import create_app

FAKE_KEY = "dummy-key-used-only-in-unit-tests"


def trace():
    return SimpleNamespace(records=[], emit=lambda _: None)


class ProviderTests(unittest.TestCase):
    def test_default_ollama_even_when_a_google_key_is_present(self):
        with patch.dict(os.environ, {"GOOGLE_API_KEY":FAKE_KEY}, clear=True), patch("banco_agentes.providers.load_environment"):
            self.assertEqual(model_settings()[0], "ollama")
            model = create_model("orquestador", trace())
            self.assertEqual(model.model, "ollama_chat/llama3.1:8b")

    def test_gemini_uses_native_adk_and_explicit_developer_api(self):
        environment = {"BANCO_MODEL_PROVIDER":"gemini", "GOOGLE_API_KEY":FAKE_KEY,
                       "GOOGLE_GENAI_USE_VERTEXAI":"TRUE", "OLLAMA_API_BASE":"https://invalid.example"}
        with patch.dict(os.environ, environment, clear=True), patch("banco_agentes.providers.load_environment"):
            model = create_model("data_analytics", trace(), num_predict=480)
            self.assertIsInstance(model, Gemini)
            self.assertEqual(model.model, "gemini-3.5-flash-lite")
            self.assertNotIn(FAKE_KEY, repr(model))
            with patch("banco_agentes.providers.Client") as client:
                model.api_client
                self.assertFalse(client.call_args.kwargs["vertexai"])
                self.assertEqual(client.call_args.kwargs["api_key"], FAKE_KEY)

    def test_missing_gemini_key_fails_without_switching_provider(self):
        with patch.dict(os.environ, {"BANCO_MODEL_PROVIDER":"gemini"}, clear=True), patch("banco_agentes.providers.load_environment"):
            with self.assertRaisesRegex(ValueError, "Falta GOOGLE_API_KEY"):
                create_model("orquestador", trace())


class MeteringTests(unittest.IsolatedAsyncioTestCase):
    async def test_configured_models_are_recorded_per_agent_with_simulated_responses(self):
        observed = trace()
        environment = {"BANCO_MODEL_PROVIDER": "gemini", "GOOGLE_API_KEY": FAKE_KEY,
                       "BANCO_HUB_MODEL": "gemini-hub-simulado", "BANCO_SPOKES_MODEL": "gemini-spokes-simulado"}
        async def simulated(self, request, stream=False):
            yield LlmResponse(content=types.Content(role="model", parts=[types.Part(text="Respuesta simulada")]))
        with patch.dict(os.environ, environment, clear=True), patch("banco_agentes.providers.load_environment"), \
                patch.object(Gemini, "generate_content_async", simulated), patch("banco_agentes.providers.Client") as client:
            for agent in ("orquestador", "data_analytics"):
                model = create_model(agent, observed)
                async for _ in model.generate_content_async(LlmRequest()):
                    pass
            client.assert_not_called()
        metrics = turn_metrics(observed.records, elapsed_seconds=.1)
        self.assertEqual(metrics["provider"], "gemini")
        self.assertEqual(metrics["model"], "mixed")
        self.assertEqual([(c["agent"], c["model"]) for c in metrics["calls"]],
                         [("orquestador", "gemini-hub-simulado"), ("data_analytics", "gemini-spokes-simulado")])

    async def test_native_response_and_thought_signature_are_preserved(self):
        observed = trace()
        response = LlmResponse(content=types.Content(role="model",parts=[
            types.Part(text="Respuesta",thought_signature=b"signature")]),
            usage_metadata=types.GenerateContentResponseUsageMetadata(
                prompt_token_count=10,candidates_token_count=3,thoughts_token_count=2,total_token_count=15))
        async def fake(self, request, stream=False):
            yield response
        model=MeteredGemini(agent_name="data_steward",trace=observed,api_key=FAKE_KEY,timeout=3)
        with patch.object(Gemini,"generate_content_async",fake):
            results=[item async for item in model.generate_content_async(LlmRequest())]
        self.assertIs(results[0],response)
        self.assertEqual(results[0].content.parts[0].thought_signature,b"signature")
        summary=turn_metrics(observed.records,elapsed_seconds=.5)
        self.assertEqual((summary["input_tokens"],summary["output_tokens"],summary["thinking_tokens"],summary["total_tokens"]),(10,3,2,15))
        self.assertTrue(summary["usage_complete"])

    async def test_errors_do_not_reveal_credentials_or_fabricate_tokens(self):
        observed=trace()
        async def failing(self, request, stream=False):
            raise ValueError("SDK error including "+FAKE_KEY)
            yield
        model=MeteredGemini(agent_name="orquestador",trace=observed,api_key=FAKE_KEY,timeout=3)
        with patch.object(Gemini,"generate_content_async",failing):
            with self.assertRaises(ModelCallError) as caught:
                async for _ in model.generate_content_async(LlmRequest()):
                    pass
        self.assertNotIn(FAKE_KEY,str(caught.exception))
        self.assertNotIn(FAKE_KEY,json.dumps(observed.records))
        summary=turn_metrics(observed.records,elapsed_seconds=.5,status="error")
        self.assertIsNone(summary["total_tokens"])
        self.assertFalse(summary["usage_complete"])


class TurnMetricTests(unittest.TestCase):
    def test_sums_hub_and_spoke_usage_without_double_counting_thinking(self):
        records=[
            dict(event="model_call",agent="orquestador",provider="gemini",model="gemini-3.5-flash-lite",status="completed",seconds=.1,
                 usage=dict(prompt_token_count=10,candidates_token_count=2,total_token_count=12)),
            dict(event="model_call",agent="data_analytics",provider="gemini",model="gemini-3.5-flash-lite",status="completed",seconds=.2,
                 usage=dict(prompt_token_count=20,candidates_token_count=3,thoughts_token_count=4,total_token_count=27)),
        ]
        result=turn_metrics(records,elapsed_seconds=.4)
        self.assertEqual(result["total_tokens"],39)
        self.assertEqual(result["input_tokens"],30)
        self.assertEqual(result["output_tokens"],5)
        self.assertEqual(result["thinking_tokens"],4)
        self.assertEqual(result["model_calls"],2)
        self.assertTrue(result["usage_complete"])

    def test_legacy_trace_without_agent_or_status_remains_partial(self):
        result=turn_metrics([dict(event="model_call",seconds=.2)],elapsed_seconds=.3)
        self.assertIsNone(result["calls"][0]["agent"])
        self.assertEqual(result["calls"][0]["status"],"unknown")
        self.assertIsNone(result["total_tokens"])
        self.assertFalse(result["usage_complete"])

    def test_ollama_uses_native_counts_and_reports_missing_usage(self):
        records=[dict(event="model_call",agent="orquestador",status="completed",seconds=.1,
                      ollama_metrics=dict(prompt_eval_count=7,eval_count=4))]
        result=turn_metrics(records,elapsed_seconds=.2)
        self.assertEqual(result["total_tokens"],11)
        self.assertEqual(result["provider"],"ollama")
        records.append(dict(event="model_call",agent="data_steward",status="failed_or_cancelled",seconds=.1))
        result=turn_metrics(records,elapsed_seconds=.3)
        self.assertEqual(result["total_tokens"],11)
        self.assertFalse(result["usage_complete"])


class WebMetricTests(unittest.TestCase):
    def test_each_successful_and_failed_turn_records_time_and_usage(self):
        async def reply(question,*,session,timeout):
            call=dict(event="model_call",agent="orquestador",provider="gemini",model="gemini-3.5-flash-lite",
                      status="completed",seconds=.01,usage=dict(prompt_token_count=8,candidates_token_count=2,total_token_count=10))
            if question=="fallar":
                error=TimeoutError()
                error.run_result=dict(trace=[{**call,"status":"cancelled","usage":None}],total_seconds=.02)
                raise error
            return dict(answer="Respuesta",trace=[call],total_seconds=.02)
        with tempfile.TemporaryDirectory(dir=ROOT,prefix="metrics-test-") as folder:
            path=Path(folder).resolve()
            self.assertTrue(path.is_relative_to(ROOT))
            output=path/"metrics.jsonl"
            app=create_app(reply,audit_path=None,metrics_path=output,
                           model_info={"provider":"gemini","model":"gemini-3.5-flash-lite"})
            with TestClient(app,base_url="http://localhost") as client:
                self.assertEqual(client.post("/api/login",json={"username":"analista","password":"AnalistaDemo!2026"}).status_code,200)
                result=client.post("/api/chat",json={"question":"prueba"})
                self.assertEqual(result.json()["metrics"]["total_tokens"],10)
                with self.assertLogs("banco_agentes.web",level="ERROR"):
                    failed=client.post("/api/chat",json={"question":"fallar"})
                self.assertEqual(failed.status_code,504)
            lines=[json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(lines),2)
            self.assertEqual(lines[0]["total_tokens"],10)
            self.assertIsNone(lines[1]["total_tokens"])
            self.assertFalse(lines[1]["usage_complete"])
            self.assertNotIn("question",lines[0])
            self.assertNotIn(FAKE_KEY,output.read_text(encoding="utf-8"))
