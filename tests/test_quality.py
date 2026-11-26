import unittest
from unittest.mock import MagicMock, patch

from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.genai import types

from banco_agentes.quality import QUESTION, PARAMS, QUERY, check_duplicates, format_quality, supports_question
from banco_agentes.runtime import ask


class QualityTests(unittest.TestCase):
    def test_parameterized_query_and_counted_groups_are_delivered(self):
        for groups in (0, 3):
            conn=MagicMock()
            conn.execute.return_value.fetchone.side_effect=[("banco_lectura","on"),(20000,groups,groups*2,groups,0)]
            with patch("banco_agentes.quality.connect_readonly") as connect:
                connect.return_value.__enter__.return_value=conn
                result=check_duplicates()
            conn.execute.assert_any_call(QUERY,PARAMS)
            self.assertEqual(result["duplicate_groups"],groups)
            self.assertIn(f"Grupos duplicados: {groups}.",format_quality(result))

    def test_failure_or_empty_cutoff_is_not_zero_duplicates(self):
        with patch("banco_agentes.quality.connect_readonly",side_effect=OSError("secret-password")):
            result=check_duplicates()
        self.assertEqual(result["error_code"],"database_unavailable")
        self.assertNotIn("secret-password",str(result))
        self.assertNotIn("duplicate_groups",result)
        self.assertIn("no se pudo realizar",format_quality(result))
        conn=MagicMock()
        conn.execute.return_value.fetchone.side_effect=[("banco_lectura","on"),(0,0,0,0,0)]
        with patch("banco_agentes.quality.connect_readonly") as connect:
            connect.return_value.__enter__.return_value=conn
            result=check_duplicates()
        self.assertEqual(result["error_code"],"no_data_for_cutoff")
        self.assertNotIn("duplicate_groups",result)

    def test_scope_does_not_replace_other_agents(self):
        self.assertTrue(supports_question(QUESTION))
        for text in ("Hola",QUESTION.replace("junio","mayo"),
                     "¿Cuál fue el saldo total de AHORRO al cierre de junio de 2026?"):
            self.assertFalse(supports_question(text))


class QualityModel(BaseLlm):
    agent_name: str

    async def generate_content_async(self,llm_request,stream=False):
        if self.agent_name == "orquestador" and any(p.function_response for c in llm_request.contents for p in c.parts or []):
            yield LlmResponse(content=types.Content(role="model",parts=[types.Part(text="Resultados recibidos.")]))
            return
        call=types.FunctionCall(name="data_quality",args={"request":QUESTION}) if self.agent_name=="orquestador" else types.FunctionCall(name="comprobar_duplicados_saldos_junio_2026",args={})
        yield LlmResponse(content=types.Content(role="model",parts=[types.Part(function_call=call)]))


class QualityADKTests(unittest.IsolatedAsyncioTestCase):
    async def test_agent_tool_runs_quality_and_returns_its_result(self):
        fixture=dict(status="ok",cutoff="2026-06-30",table="cfg.fact_sdos_captacoloca_m",
                     checked_rows=20000,duplicate_groups=0,duplicate_rows=0,excess_rows=0,null_key_rows=0)
        with patch("banco_agentes.agents.check_duplicates",return_value=fixture) as query:
            result=await ask(QUESTION,emit=lambda _:None,model_factory=lambda name:
                             QualityModel(model="scripted-test-only",agent_name=name))
        query.assert_called_once()
        self.assertIn(format_quality(fixture),result["answer"])
        completed={(e["agent"],e.get("tool")) for e in result["trace"] if e["event"]=="tool_completed"}
        self.assertIn(("orquestador","data_quality"),completed)
        self.assertIn(("data_quality","comprobar_duplicados_saldos_junio_2026"),completed)
        self.assertFalse(any(e["agent"] in {"data_steward","data_analytics"} for e in result["trace"]))
