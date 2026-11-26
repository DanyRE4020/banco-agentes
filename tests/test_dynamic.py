import unittest
from unittest.mock import MagicMock, patch

from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.genai import types

from banco_agentes.agents import build_agents
from banco_agentes.quality import QUERY, check_duplicates
from banco_agentes.reports import sql_report
from banco_agentes.runtime import Trace, ask
from banco_agentes.verify_dynamic import CASES


class DynamicTests(unittest.TestCase):
    def test_same_tools_regardless_of_case_or_wording(self):
        for _, question, _ in CASES:
            tools = build_agents(Trace(lambda _: None), question=question).tools
            self.assertEqual({t.name for t in tools}, {"data_steward", "data_analytics", "data_quality", "data_governance"})
            self.assertFalse(any(t.skip_summarization for t in tools))

    def test_product_filter_is_bound_as_parameter(self):
        conn = MagicMock()
        conn.execute.return_value.fetchone.side_effect = [("banco_lectura", "on"), (5000, 0, 0, 0, 0)]
        with patch("banco_agentes.quality.connect_readonly") as connect:
            connect.return_value.__enter__.return_value = conn
            result = check_duplicates("AHORRO")
        conn.execute.assert_any_call(QUERY, (20260630, "AHORRO", "AHORRO"))
        self.assertEqual(result["product"], "AHORRO")

    def test_partial_failure_names_unverified_result(self):
        for agent, label in (("data_analytics", "Saldo total"), ("data_quality", "Duplicidad")):
            report = sql_report([dict(event="sql_result", agent=agent, status="unavailable",
                                     error_code="database_unavailable", cutoff="2026-06-30", table="cfg.fact_sdos_captacoloca_m")])
            self.assertIn(label + " sin verificar", report)
            self.assertNotIn("Grupos duplicados: 0", report)
            self.assertNotIn("Saldo total de AHORRO: 0", report)


class MultiStepModel(BaseLlm):
    agent_name: str
    calls: int = 0

    async def generate_content_async(self, llm_request, stream=False):
        self.calls += 1
        if self.agent_name == "orquestador":
            # Orden inverso al habitual para comprobar que el código no impone una secuencia.
            if self.calls <= 2:
                name = ("data_quality", "data_analytics")[self.calls-1]
                part = types.Part(function_call=types.FunctionCall(name=name, args={"request": "AHORRO, junio 2026"}))
            else:
                part = types.Part(text="Ambos reportes recibidos.")
        else:
            name = "consultar_saldo_ahorro_junio_2026" if self.agent_name == "data_analytics" else "comprobar_duplicados_saldos_junio_2026"
            part = types.Part(function_call=types.FunctionCall(name=name, args={} if self.agent_name == "data_analytics" else {"producto": "AHORRO"}))
        yield LlmResponse(content=types.Content(role="model", parts=[part]))


class MultiStepADKTests(unittest.IsolatedAsyncioTestCase):
    async def test_agent_results_do_not_end_turn_early(self):
        analytics = dict(status="ok", product="AHORRO", cutoff="2026-06-30", table="cfg.fact_sdos_captacoloca_m", column="saldo_mes", rows=5000, total="123.45")
        quality = dict(status="ok", product="AHORRO", cutoff="2026-06-30", table="cfg.fact_sdos_captacoloca_m", checked_rows=5000, duplicate_groups=0, duplicate_rows=0, excess_rows=0, null_key_rows=0)
        with patch("banco_agentes.agents.query_balance", return_value=analytics), patch("banco_agentes.agents.check_duplicates", return_value=quality) as check:
            result = await ask(CASES[2][1], emit=lambda _: None, model_factory=lambda name:
                               MultiStepModel(model="scripted-test-only", agent_name=name))
        check.assert_called_once_with("AHORRO")
        self.assertIn("123.45", result["answer"])
        self.assertIn("Grupos duplicados: 0", result["answer"])
        self.assertEqual([e["tool"] for e in result["trace"] if e["event"] == "tool_completed" and e["agent"] == "orquestador"],
                         ["data_quality", "data_analytics"])
