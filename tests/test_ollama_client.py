import json
import unittest
from types import SimpleNamespace

import httpx

from banco_agentes.ollama_client import LocalOllamaClient


class ClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_wire_history_keeps_calls_and_corresponding_results(self):
        messages = [{"role": "assistant", "tool_calls": [
            {"id": "first", "function": {"name": "buscar_catalogo", "arguments": '{"consulta":"dim.dim_cliente"}'}},
            {"id": "second", "function": {"name": "otra", "arguments": '{}'}}]},
            {"role": "tool", "tool_call_id": "second", "content": "segundo resultado"},
            {"role": "tool", "tool_call_id": "first", "content": "primer resultado"}]
        def handler(request):
            self.assertEqual(request.url.path, "/api/chat")
            sent = json.loads(request.content)["messages"]
            self.assertEqual(len(sent[0]["tool_calls"]), 2)
            self.assertEqual(sent[0]["tool_calls"][0]["function"]["arguments"], {"consulta": "dim.dim_cliente"})
            self.assertEqual(sent[1]["tool_name"], "otra")
            self.assertEqual(sent[2]["tool_name"], "buscar_catalogo")
            self.assertEqual(sent[2]["tool_call_id"], "first")
            self.assertEqual(sent[2]["content"], "primer resultado")
            return httpx.Response(200, json={"message": {"role": "assistant", "content": "respuesta"}})
        trace = SimpleNamespace(records=[], emit=lambda _: None)
        client = LocalOllamaClient("http://localhost:11434", "test", trace, httpx.MockTransport(handler))
        await client.acompletion("ollama_chat/llama3.1:8b", messages)
        self.assertIsInstance(messages[0]["tool_calls"][0]["function"]["arguments"], str)
