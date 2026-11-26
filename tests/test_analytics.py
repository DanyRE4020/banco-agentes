import unittest
from decimal import Decimal
from unittest.mock import MagicMock, patch

from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.genai import types

from banco_agentes.analytics import QUESTION, QUERY, PARAMS, query_balance, supports_question
from banco_agentes.runtime import ask


class AnalyticsTests(unittest.TestCase):
    def test_query_is_parameterized_and_decimal_is_preserved(self):
        conn = MagicMock()
        conn.execute.return_value.fetchone.side_effect = [("banco_lectura", "on"), (Decimal("123.45"), 2, 2, 2)]
        with patch("banco_agentes.analytics.connect_readonly") as connect:
            connect.return_value.__enter__.return_value = conn
            result = query_balance()
        conn.execute.assert_any_call(QUERY, PARAMS)
        self.assertIn("%s", QUERY)
        self.assertNotIn("AHORRO", QUERY)
        self.assertEqual(result["total"], "123.45")

    def test_connection_failure_has_no_total_or_secret(self):
        with patch("banco_agentes.analytics.connect_readonly", side_effect=OSError("secret-test-password")):
            result = query_balance()
        self.assertEqual(result["error_code"], "database_unavailable")
        self.assertNotIn("total", result)
        self.assertNotIn("secret-test-password", str(result))

    def test_empty_or_duplicate_snapshot_is_not_a_zero_total(self):
        for row in ((None, 0, 0, 0), (Decimal("10"), 2, 2, 1)):
            conn = MagicMock()
            conn.execute.return_value.fetchone.side_effect = [("banco_lectura", "on"), row]
            with patch("banco_agentes.analytics.connect_readonly") as connect:
                connect.return_value.__enter__.return_value = conn
                result = query_balance()
            self.assertEqual(result["status"], "unavailable")
            self.assertNotIn("total", result)

    def test_only_supported_scope(self):
        self.assertTrue(supports_question(QUESTION))
        for q in ("Hola", "Saldo real del cliente 1", QUESTION.replace("AHORRO", "TDC"),
                  QUESTION.replace("junio", "mayo"), QUESTION + " del cliente 1"):
            self.assertFalse(supports_question(q))


class AnalyticsModel(BaseLlm):
    agent_name: str

    async def generate_content_async(self, llm_request, stream=False):
        if self.agent_name == "orquestador" and any(p.function_response for c in llm_request.contents for p in c.parts or []):
            yield LlmResponse(content=types.Content(role="model", parts=[types.Part(text="Resultados recibidos.")]))
            return
        if self.agent_name == "orquestador":
            call = types.FunctionCall(name="data_analytics", args={"request": QUESTION})
        else:
            call = types.FunctionCall(name="consultar_saldo_ahorro_junio_2026", args={})
        yield LlmResponse(content=types.Content(role="model", parts=[types.Part(function_call=call)]))


class AnalyticsADKTests(unittest.IsolatedAsyncioTestCase):
    async def test_dynamic_agent_tool_and_result_delivery(self):
        fixture = dict(status="ok", product="AHORRO", cutoff="2026-06-30", table="cfg.fact_sdos_captacoloca_m",
                       column="saldo_mes", rows=5000, total="123.45")
        with patch("banco_agentes.agents.query_balance", return_value=fixture) as query:
            result = await ask(QUESTION, emit=lambda _: None, model_factory=lambda name:
                               AnalyticsModel(model="scripted-test-only", agent_name=name))
        query.assert_called_once()
        self.assertIn("123.45", result["answer"])
        completed = {(e["agent"], e.get("tool")) for e in result["trace"] if e["event"] == "tool_completed"}
        self.assertIn(("orquestador", "data_analytics"), completed)
        self.assertIn(("data_analytics", "consultar_saldo_ahorro_junio_2026"), completed)
        self.assertFalse(any(e["agent"] == "data_steward" for e in result["trace"]))
