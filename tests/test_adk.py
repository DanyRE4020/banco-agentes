import json
import unittest

from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.genai import types

from banco_agentes.runtime import ask


class ScriptedModel(BaseLlm):
    role_name: str
    question: str
    calls: int = 0

    async def generate_content_async(self, llm_request, stream=False):
        self.calls += 1
        if self.role_name == "orquestador" and self.question == "Hola":
            part = types.Part(text="Hola, puedo orientarte sobre el catálogo bancario.")
        elif self.role_name == "orquestador" and "saldo real" in self.question:
            part = types.Part(text="No se puede calcular: no hay conexión a PostgreSQL ni capacidad analítica.")
        elif self.calls == 1:
            name = "data_steward" if self.role_name == "orquestador" else "buscar_catalogo"
            argument = "request" if self.role_name == "orquestador" else "consulta"
            part = types.Part(function_call=types.FunctionCall(name=name, args={argument: self.question}))
        else:
            responses = [part.function_response for content in llm_request.contents
                         for part in content.parts or [] if part.function_response]
            if not responses:
                raise AssertionError("El modelo de prueba no recibió el resultado real de la herramienta.")
            part = types.Part(text=json.dumps(responses[-1].response, ensure_ascii=False))
        yield LlmResponse(content=types.Content(role="model", parts=[part]))


class ADKIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def run_question(self, question):
        return await ask(question, emit=lambda _: None, model_factory=lambda name:
                         ScriptedModel(model="scripted-test-only", role_name=name, question=question))

    async def test_existing_table_goes_through_real_agent_tool_and_search(self):
        result = await self.run_question("Describe la tabla dim.dim_cliente")
        completed = [(entry["agent"], entry.get("tool")) for entry in result["trace"]
                     if entry["event"] == "tool_completed"]
        self.assertEqual(completed, [("data_steward", "buscar_catalogo"), ("orquestador", "data_steward")])
        self.assertIn("num_cte", result["answer"])
        self.assertIn("001_dimensiones", result["answer"])

    async def test_nonexistent_field_returns_negative_evidence(self):
        result = await self.run_question("Describe el campo credit_score de dim.dim_cliente")
        self.assertIn("not_found", result["answer"])
        self.assertIn("unknown_identifiers", result["answer"])

    async def test_greeting_does_not_invoke_steward(self):
        result = await self.run_question("Hola")
        self.assertEqual([e for e in result["trace"] if e["event"] == "agent_started"], [{"event": "agent_started", "agent": "orquestador"}])
        self.assertFalse(any(e["event"] == "tool_started" for e in result["trace"]))

    async def test_balance_has_no_calculation_tool(self):
        result = await self.run_question("Dame el saldo real del cliente 1")
        self.assertIn("No se puede calcular", result["answer"])
        self.assertFalse(any(entry["event"] == "tool_completed" for entry in result["trace"]))


if __name__ == "__main__":
    unittest.main()
