"""Asignación de modelos al árbol ADK sin llamadas de red ni secretos reales."""
import os
import unittest
from unittest.mock import patch

from banco_agentes.general_agents import Turn, build_general_agents
from banco_agentes.governance import DemoSession, REQUEST, SESSION
from banco_agentes.providers import GEMINI_MODEL, create_model, validate_provider
from banco_agentes.runtime import Trace


class ModelSelectionTests(unittest.TestCase):
    def assert_tree(self, configuration, hub_model, spokes_model):
        environment = {"BANCO_MODEL_PROVIDER": "gemini", "GOOGLE_API_KEY": "clave-ficticia-de-prueba",
                       **configuration}
        for identity in (DemoSession(), DemoSession("Cliente", 1, 200000)):
            with self.subTest(profile=identity.profile), patch.dict(os.environ, environment, clear=True), \
                    patch("banco_agentes.providers.load_environment"), patch("banco_agentes.providers.Client") as client:
                session_token = SESSION.set(identity)
                request_token = REQUEST.set("")
                try:
                    hub = build_general_agents(Turn(Trace(lambda _: None), {}))
                    self.assertEqual(hub.model.model, hub_model)
                    expected = {"data_steward", "data_analytics", "data_governance", "data_graph"}
                    if identity.profile == "Analista":
                        expected.add("data_quality")
                    self.assertEqual({tool.agent.name for tool in hub.tools}, expected)
                    for tool in hub.tools:
                        self.assertEqual(tool.agent.model.model, spokes_model, tool.agent.name)
                    self.assertEqual(validate_provider(), {"provider": "gemini", "model": hub_model,
                                     "hub_model": hub_model, "spokes_model": spokes_model})
                    client.assert_not_called()
                finally:
                    REQUEST.reset(request_token)
                    SESSION.reset(session_token)

    def test_default_tree_keeps_flash_lite_for_every_agent(self):
        self.assert_tree({}, GEMINI_MODEL, GEMINI_MODEL)

    def test_only_hub_override_does_not_change_spokes(self):
        self.assert_tree({"BANCO_HUB_MODEL": "gemini-hub-simulado"}, "gemini-hub-simulado", GEMINI_MODEL)

    def test_only_spokes_override_does_not_change_hub(self):
        self.assert_tree({"BANCO_SPOKES_MODEL": "gemini-spokes-simulado"}, GEMINI_MODEL, "gemini-spokes-simulado")

    def test_distinct_overrides_reach_the_corresponding_agents(self):
        self.assert_tree({"BANCO_HUB_MODEL": "gemini-hub-simulado", "BANCO_SPOKES_MODEL": "gemini-spokes-simulado"},
                         "gemini-hub-simulado", "gemini-spokes-simulado")

    def test_empty_overrides_keep_independent_defaults(self):
        self.assert_tree({"BANCO_HUB_MODEL": " ", "BANCO_SPOKES_MODEL": ""}, GEMINI_MODEL, GEMINI_MODEL)

    def test_ollama_ignores_gemini_overrides_and_keeps_its_validation(self):
        environment = {"BANCO_HUB_MODEL": "gemini-hub-simulado", "BANCO_SPOKES_MODEL": "gemini-spokes-simulado"}
        with patch.dict(os.environ, environment, clear=True), patch("banco_agentes.providers.load_environment"):
            for name in ("orquestador", "data_steward", "data_analytics", "data_quality", "data_governance"):
                self.assertEqual(create_model(name, Trace(lambda _: None)).model, "ollama_chat/llama3.1:8b")
            self.assertEqual(validate_provider(), {"provider": "ollama", "model": "llama3.1:8b"})
            with patch.dict(os.environ, {"OLLAMA_MODEL": "otro-modelo"}):
                with self.assertRaisesRegex(ValueError, "llama3.1:8b"):
                    create_model("orquestador", Trace(lambda _: None))
