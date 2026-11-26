import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from banco_agentes.auth import password_record
from banco_agentes.semantics import CUSTOMER_BASIC_FIELDS
from banco_agentes.web import create_app


DETAIL_QUESTION = "¿Cuántos clientes tienen información incompleta y cuáles son los campos con más datos faltantes?"
CATALOG_QUESTIONS = (
    "¿Cuántos valores nulos hay en saldo_mes de AHORRO al cierre de junio de 2026 en el catálogo general?",
    "¿Cuántos clientes tienen información incompleta en sus registros?",
    DETAIL_QUESTION,
)
MESSAGE = ("Con tu perfil Cliente puedes consultar tus propios saldos y movimientos. "
           "La revisión de calidad del catálogo general está reservada al perfil Empleado")


class CatalogQualityPermissionTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.audit = Path(folder.name) / "audit.jsonl"
        self.password = "sintetica-prueba-calidad"
        credentials = password_record(self.password)
        users = {"cliente": {"rol": "Cliente", "num_cte": 1, **credentials},
                 "empleado": {"rol": "Empleado", "num_cte": None, **credentials}}
        self.client = self.enterContext(TestClient(create_app(users=users, audit_path=self.audit),
                                                   base_url="http://127.0.0.1"))

    def login(self, user):
        self.client.post("/api/login", json={"username": user, "password": self.password}).raise_for_status()
        return self.client.post("/api/conversations").json()["conversation_id"]

    def ask(self, cid, question):
        response = self.client.post("/api/chat", json={"conversation_id": cid, "question": question})
        response.raise_for_status()
        return response.json()

    def test_client_quality_denial_has_only_permission_text_and_no_sql(self):
        cid = self.login("cliente")
        with patch("banco_agentes.plans.connect_readonly") as global_connection, \
                patch("banco_agentes.client_access.connect_client_socket") as client_connection, \
                patch("banco_agentes.general_agents.create_model",
                      side_effect=AssertionError("El bloqueo de Calidad no consulta al modelo")):
            for question in CATALOG_QUESTIONS:
                with self.subTest(question=question):
                    result = self.ask(cid, question)
                    self.assertEqual(result["answer"], MESSAGE)
                    self.assertTrue(result["blocked"])
                    self.assertEqual((result["query_details"], result["tables"], result["charts"]), ("", [], []))
                    self.assertEqual(result["presentation"]["result_ids"], [])
                    self.assertTrue(result["presentation"]["permission_only"])
            global_connection.assert_not_called()
            client_connection.assert_not_called()
        entries = [json.loads(line) for line in self.audit.read_text(encoding="utf-8").splitlines()]
        for entry in entries:
            denial = next(event for event in entry["trace"] if event["event"] == "authorization")
            self.assertEqual((denial["operation"], denial["allowed"], denial["sql_executed"]),
                             ("quality", False, False))
            policy = next(event for event in entry["trace"] if event["event"] == "governance_result")
            self.assertIn("num_cte de sesión", policy["rule"])

    def test_employee_counts_affected_customer_records_once(self):
        cid = self.login("empleado")
        connection = MagicMock()
        cursor = connection.execute.return_value
        cursor.fetchone.return_value = ("on",)
        cursor.description = [SimpleNamespace(name=name) for name in ("filas", "incompletos")]
        cursor.fetchall.return_value = [(5000, 3)]
        with patch("banco_agentes.plans.connect_readonly") as connector:
            connector.return_value.__enter__.return_value = connection
            data = self.ask(cid, CATALOG_QUESTIONS[1])
        self.assertFalse(data["blocked"])
        self.assertIn("3 de 5,000", data["answer"])
        self.assertIn("data_quality", data["agents"])
        sql, params = connection.execute.call_args.args
        self.assertIn("count(*) FILTER (WHERE num_cte IS NULL OR fec_alta IS NULL", sql)
        self.assertNotIn("cve_sucursal IS NULL", sql)
        self.assertEqual(params, [])
        trace = json.loads(self.audit.read_text(encoding="utf-8"))["trace"]
        plan = next(event for event in trace if event.get("event") == "validated_plan")
        self.assertTrue(plan["sql_executed"])
        self.assertEqual(plan["rows"], [{"filas": 5000, "incompletos": 3}])

    def test_employee_reports_total_breakdown_and_steward_policy(self):
        for size, incomplete, counts in ((4, 1, {"fec_nac": 1, "segmento_cte": 1}), (5000, 0, {})):
            with self.subTest(size=size):
                connection = MagicMock()
                connection.__enter__.return_value = connection
                cursor = connection.execute.return_value
                cursor.fetchone.return_value = ("on",)
                cursor.description = [SimpleNamespace(name=name)
                                      for name in ("filas", "incompletos", "faltantes_por_campo")]
                breakdown = [{"campo": field, "nulos": counts.get(field, 0)}
                             for field in sorted(CUSTOMER_BASIC_FIELDS, key=lambda key: (-counts.get(key, 0), key))]
                cursor.fetchall.return_value = [(size, incomplete, breakdown)]
                with patch("banco_agentes.plans.connect_readonly", return_value=connection), \
                        patch("banco_agentes.general_agents.create_model", side_effect=AssertionError("Ruta determinista")):
                    data = self.ask(self.login("empleado"), DETAIL_QUESTION)
                self.assertFalse(data["blocked"])
                self.assertIn("1 de 4" if incomplete else "0 de 5,000", data["answer"])
                expected = "fec_nac (1), segmento_cte (1)" if incomplete else "No hay campos básicos con faltantes."
                self.assertIn(expected, data["answer"])
                self.assertIn("data_steward", data["agents"])
                source = next(card for card in data["sources"] if "DQ-01" in card["location"])
                self.assertEqual(source["location"], "Regla DQ-01 · v. 1.0")
                self.assertEqual(source["details"]["Vigente desde"], "2026-11-23")
                self.assertEqual(source["details"]["Archivo"], "catalogo/catalog_quality_policy.json")
                self.assertEqual(connection.execute.call_count, 2)
                query, params = connection.execute.call_args.args
                for field in CUSTOMER_BASIC_FIELDS:
                    self.assertIn(f"{field} IS NULL", query)
                    self.assertIn(f"count(*) - count({field})", query)
                self.assertIn("ORDER BY nulos DESC, campo", query)
                self.assertEqual(params, [])
