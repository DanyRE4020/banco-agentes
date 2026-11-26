import unittest
from unittest.mock import patch

from google.adk.models.llm_response import LlmResponse
from google.genai import types

from banco_agentes.conversation import Conversation
from banco_agentes.governance import DemoSession
from test_general import PlannedModel, simulated_plan


QUESTION = "¿Qué significa saldo_apertura y cuál fue su total para AHORRO en abril de 2026?"
PERMISSION = "El catálogo técnico está reservado al perfil Empleado"


class FieldModel(PlannedModel):
    async def generate_content_async(self, llm_request, stream=False):
        response = next((part.function_response for part in llm_request.contents[-1].parts
                         if part.function_response), None)
        if self.agent == "orquestador" and response is None:
            part = self.call("data_steward", request="Define el campo.", corpus="tecnico")
        elif self.agent == "orquestador" and response.name == "data_steward":
            part = self.call("data_analytics", request="Total de saldo_apertura de AHORRO en abril de 2026")
        elif self.agent == "data_steward" and response is None:
            part = self.call("buscar_catalogo", consulta="saldo_apertura")
        elif self.agent == "data_steward" and response.response["query"] == "saldo_apertura":
            part = self.call("buscar_catalogo", consulta="saldo_inicial saldo_anterior")
        elif self.agent == "data_analytics" and response is None:
            part = self.call("consultar_datos", metrica="total", conjunto="saldos", producto="AHORRO",
                             periodo="2026-04", opciones="campo=saldo_apertura")
        else:
            part = types.Part(text="Identificadores sin evidencia: saldo_inicial")
        yield LlmResponse(content=types.Content(role="model", parts=[part]))


class TechnicalCatalogTests(unittest.IsolatedAsyncioTestCase):
    async def test_client_field_questions_are_denied_before_models_and_sql(self):
        session = DemoSession("Cliente", 1, principal="cliente")
        with patch("banco_agentes.conversation.build_general_agents") as build, \
                patch("banco_agentes.conversation.own_account_numbers") as accounts:
            conversation = Conversation(session)
            for question in ("¿Qué representa saldo_apertura?", QUESTION, "`saldo_apertura`"):
                with self.subTest(question=question):
                    result = await conversation.ask(question)
                    self.assertEqual(result["answer"], PERMISSION)
                    self.assertTrue(result["presentation"]["permission_only"])
                    self.assertEqual([event["event"] for event in result["trace"]],
                                     ["authorization", "access_blocked", "governance_result"])
                    event = result["trace"][0]
                    self.assertEqual((event["operation"], event["profile"], event["allowed"], event["sql_executed"]),
                                     ("technical_catalog", "Cliente", False, False))
            build.assert_not_called()
            accounts.assert_not_called()

    async def test_client_technical_delegation_is_denied_even_without_explicit_field(self):
        session = DemoSession("Cliente", 1, principal="cliente")
        conversation = Conversation(session, model_factory=lambda name: FieldModel(model="test", agent=name))
        with patch("banco_agentes.general_agents.load_catalog") as catalog, \
                patch("banco_agentes.general_agents.execute_plan") as sql:
            # El selector del hub elige el corpus técnico aunque el mensaje no trae un identificador.
            result = await conversation.ask("Explícame ese término.")
        catalog.assert_not_called()
        sql.assert_not_called()
        self.assertEqual(result["answer"], PERMISSION)
        self.assertFalse(any(event["event"] == "catalog_evidence" for event in result["trace"]))

    async def test_exact_definition_survives_missing_alternate_in_mixed_turn(self):
        conversation = Conversation(DemoSession(), model_factory=lambda name: FieldModel(model="test", agent=name))
        with patch("banco_agentes.general_agents.execute_plan", side_effect=simulated_plan) as sql:
            result = await conversation.ask(QUESTION)
        sql.assert_called_once()
        self.assertEqual(sql.call_args.args[0]["campo"], "saldo_apertura")
        lookups = [event["evidence"] for event in result["trace"] if event["event"] == "catalog_evidence"]
        self.assertEqual([lookup["query"] for lookup in lookups], ["saldo_apertura", "saldo_inicial saldo_anterior"])
        self.assertEqual(lookups[1]["unknown_identifiers"], ["saldo_inicial"])
        self.assertIn("saldo_apertura es el saldo al inicio del período", result["answer"])
        self.assertIn("al cierre del mes anterior", result["answer"])
        self.assertIn("abril de 2026 es 20.", result["answer"])
        self.assertNotIn("saldo_inicial", result["answer"])
        self.assertTrue(any(source["location"] == "líneas 169-170,336-337" for source in result["sources"]))
