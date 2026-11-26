import unittest

from banco_agentes.agents import build_agents
from banco_agentes.routing import is_simple_greeting, requests_real_balance
from banco_agentes.runtime import Trace


class RoutingTests(unittest.TestCase):
    def test_non_greetings_offer_the_same_capabilities(self):
        question = "¿Cuál es el saldo real del cliente 1 al cierre de junio de 2026?"
        self.assertTrue(requests_real_balance(question))
        self.assertEqual({t.agent.name for t in build_agents(Trace(lambda _: None), question=question).tools},
                         {"data_steward", "data_analytics", "data_quality", "data_governance"})
        question = "¿Qué significa el campo saldo_apertura?"
        self.assertFalse(requests_real_balance(question))
        self.assertEqual(len(build_agents(Trace(lambda _: None), question=question).tools), 4)

    def test_only_complete_greetings_match(self):
        for text in ("Hola", "¡Hola, buenas tardes!", "Buenos días"):
            self.assertTrue(is_simple_greeting(text), text)
        for text in ("Hola, ¿qué columnas tiene dim.dim_cliente?", "Hola credit_score",
                     "Buenas tardes, necesito mi saldo", "", "Hola\nconsulta"):
            self.assertFalse(is_simple_greeting(text), text)

    def test_agent_tools_depend_on_whole_question(self):
        # Inspecciona configuración real ADK, sin ejecutar el modelo.
        for question, expected in (("Hola, buenas tardes", 0),
                                   ("Hola, ¿qué columnas tiene dim.dim_cliente?", 4)):
            agent = build_agents(Trace(lambda _: None), question=question)
            self.assertEqual(len(agent.tools), expected)
            if expected:
                self.assertEqual(agent.tools[0].agent.name, "data_steward")
