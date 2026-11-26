import copy
import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "probar_ollama_tools.py"
SPEC = importlib.util.spec_from_file_location("ollama_probe", SCRIPT)
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


class ProbeTests(unittest.TestCase):
    def assistant(self):
        return {"role": "assistant", "content": "", "tool_calls": [{
            "id": "call_from_model_123", "type": "function", "function": {
                "name": probe.TOOL_NAME, "arguments": "{}"}}]}

    def test_no_execution_without_model_call(self):
        with patch.object(probe, "obtener_estado_prueba") as tool:
            with self.assertRaises(RuntimeError):
                probe.append_tool_result([], {"role": "assistant", "content": "Hola"})
            tool.assert_not_called()

    def test_preserves_assistant_and_call_id(self):
        assistant = self.assistant()
        original = copy.deepcopy(assistant)
        messages = probe.fresh_messages()
        with patch.object(probe, "obtener_estado_prueba", return_value={"estado": probe.EXPECTED}) as tool:
            probe.append_tool_result(messages, assistant)
            tool.assert_called_once_with()
        self.assertEqual(messages[-2], original)
        self.assertEqual(messages[-1]["role"], "tool")
        self.assertEqual(messages[-1]["tool_call_id"], original["tool_calls"][0]["id"])
        self.assertEqual(messages[-1]["name"], probe.TOOL_NAME)
        self.assertEqual(assistant, original)

    def test_rejects_invalid_call_before_execution(self):
        for change in ("id", "name", "arguments"):
            assistant = self.assistant()
            if change == "id":
                assistant["tool_calls"][0]["id"] = ""
            else:
                assistant["tool_calls"][0]["function"][change] = '"invalid"'
            with self.subTest(change=change), patch.object(probe, "obtener_estado_prueba") as tool:
                with self.assertRaises(RuntimeError):
                    probe.append_tool_result([], assistant)
                tool.assert_not_called()

    def test_native_association(self):
        assistant = self.assistant()
        assistant["tool_calls"][0]["function"]["arguments"] = {}
        messages = []
        probe.append_tool_result(messages, assistant, native=True)
        self.assertEqual(messages[-1]["tool_name"], probe.TOOL_NAME)
        self.assertNotIn("tool_call_id", messages[-1])

    def test_fresh_session_and_no_expected_answer_in_prompt(self):
        first = probe.fresh_messages()
        first.append({"role": "assistant", "content": "historial"})
        second = probe.fresh_messages()
        self.assertEqual([message["role"] for message in second], ["system", "user"])
        self.assertNotIn(probe.EXPECTED, str(second))

    def test_refusal_fails_validation(self):
        with self.assertRaises(RuntimeError):
            probe.validate_final({"role": "assistant", "content": "No puedo proporcionar esa información."})


if __name__ == "__main__":
    unittest.main()
