import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from banco_agentes.auth import password_record
from banco_agentes.web import create_app


RANKING = "Muestra el top 5 de clientes por saldo de AHORRO al cierre de junio de 2026."


class EmployeeGovernanceTests(unittest.TestCase):
    def test_employee_ranking_and_individual_denial(self):
        connection = MagicMock()
        connection.execute.return_value.fetchone.return_value = ("on",)
        connection.execute.return_value.fetchall.return_value = [(Decimal(n),) for n in ("1200", "1200", "1000", "950", "900")]
        connector = MagicMock()
        connector.return_value.__enter__.return_value = connection
        password = "sintetico-comprobacion-local"
        users = {"empleado_prueba": {"rol": "Empleado", "num_cte": None, **password_record(password)}}
        with tempfile.TemporaryDirectory() as folder, \
                patch("banco_agentes.analytics.connect_readonly", connector), \
                patch("banco_agentes.general_agents.create_model", side_effect=AssertionError("Flujo determinista")):
            audit = Path(folder) / "audit.jsonl"
            with TestClient(create_app(users=users, audit_path=audit), base_url="http://127.0.0.1") as client:
                login = client.post("/api/login", json={"username": "empleado_prueba", "password": password})
                login.raise_for_status()
                self.assertEqual(login.json(), {"profile": "Analista"})
                cid = client.post("/api/conversations").json()["conversation_id"]

                def ask(question):
                    response = client.post("/api/chat", json={"conversation_id": cid, "question": question})
                    response.raise_for_status()
                    return response.json()

                posted = ask(RANKING)
                self.assertEqual(posted["agents"], ["data_steward", "data_analytics"])
                self.assertFalse(posted["blocked"])
                self.assertEqual(posted["tables"][0]["rows"][0], {"posicion": 1, "alias": "Titular A", "saldo": "1.200"})
                self.assertEqual([row["alias"] for row in posted["tables"][0]["rows"]],
                                 [f"Titular {letter}" for letter in "ABCDE"])
                self.assertEqual(connection.execute.call_count, 2)
                sql, params = connection.execute.call_args.args
                self.assertIn("GROUP BY num_cte", sql)
                self.assertNotIn("num_cte", sql.split("FROM", 1)[0])
                self.assertEqual(params, ("AHORRO", 20260630))
                for question in ("¿Cuál es el saldo del cliente número 987654321?",
                                 "¿Qué saldo tiene el cliente con ID 987654321?",
                                 "¿Cuál es el saldo exacto del primer puesto?",
                                 "¿Y su nombre?"):
                    denied = ask(question)
                    self.assertTrue(denied["blocked"])
                    self.assertFalse(denied["tables"] or denied["charts"])
                    self.assertEqual(denied["agents"], ["data_steward"])
                    self.assertIn("DG-01", denied["answer"])
                    self.assertNotIn("987654321", denied["answer"])
                self.assertEqual(connection.execute.call_count, 2)
            entries = [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines()]
        for entry in entries[1:]:
            event = next(event for event in entry["trace"] if event["event"] == "access_blocked")
            self.assertFalse(event["sql_executed"])
            self.assertFalse(any(event.get("sql_executed") for event in entry["trace"]))


if __name__ == "__main__":
    unittest.main()
