import json
import re
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from google.adk.models.llm_response import LlmResponse
from google.genai import types

from banco_agentes.auth import password_record
from banco_agentes.documents import DocumentStore
from banco_agentes.web import create_app
from test_client_isolation import IsolationModel


class EmployeeGroupingModel(IsolationModel):
    async def generate_content_async(self, llm_request, stream=False):
        parts = llm_request.contents[-1].parts
        response = next((p.function_response for p in parts if p.function_response), None)
        text = "\n".join(p.text for item in llm_request.contents if item.role == "user" for p in item.parts if p.text)
        question = text.split("\n", 1)[0]
        args = None
        if self.agent == "data_analytics":
            base = {"metrica": "total"}
            if "saldo de AHORRO" in question:
                grouping = "trimestre" if response is None else "mes"
                if response is None or "trimestre" in str(response.response):
                    # En la segunda llamada simula una degradación a mes.
                    if sum(bool(p.function_response) for item in llm_request.contents for p in item.parts) < 2:
                        args = {**base, "producto": "AHORRO", "periodo": "2026-02/2026-06",
                                "opciones": f"agrupar={grouping}"}
            elif response is None:
                if "trimestre" in question or "bloque" in question:
                    args = {**base, "resultado_base": "R1", "opciones": "agrupar=trimestre"}
                else:
                    args = {**base, "periodo": "2026-02/2026-06", "producto": "TDC", "conjunto": "movimientos",
                            "opciones": "codigo=400;persona=PF;segmento=PREFERENTE;agrupar=mes"}
            name = "consultar_datos"
        elif self.agent == "orquestador" and response and response.name == "data_analytics" and "barras" in question:
            ids = [item["id"] for item in response.response.get("evidence", []) if "id" in item]
            if ids:
                name, args = "data_graph", {"request": "Barras " + ", ".join(ids)}
        elif self.agent == "data_graph" and response is None:
            ids = list(dict.fromkeys(re.findall(r"\bR\d+\b", text)))
            name, args = "preparar_grafica", {"resultados": ids, "tipo": "barras"}
        if args is None:
            async for entry in super().generate_content_async(llm_request, stream):
                yield entry
            return
        yield LlmResponse(content=types.Content(role="model", parts=[
            types.Part(function_call=types.FunctionCall(name=name, args=args))]))


class EmployeePurchaseGroupingTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        self.audit = root / "web.jsonl"
        password = "solo-para-prueba-empleado"
        users = {"empleado": {"rol": "Empleado", "num_cte": None, **password_record(password)}}
        self.client = self.enterContext(TestClient(create_app(
            users=users, audit_path=self.audit, document_store=DocumentStore(root / "uploads")),
            base_url="http://127.0.0.1"))
        self.enterContext(patch("banco_agentes.general_agents.create_model",
                                side_effect=lambda name, *a, **k: EmployeeGroupingModel(model="doble", agent=name)))
        self.client.post("/api/login", json={"username": "empleado", "password": password}).raise_for_status()
        self.cid = self.client.post("/api/conversations").json()["conversation_id"]

    def ask(self, question):
        reply = self.client.post("/api/chat", json={"question": question, "conversation_id": self.cid})
        reply.raise_for_status()
        return reply.json()

    def test_regrouping_reuses_all_filters_and_graph_uses_employee_result(self):
        db = MagicMock()
        calls = []

        def execute(sql, params=()):
            if sql.startswith("SHOW"):
                return SimpleNamespace(fetchone=lambda: ("on",))
            calls.append((sql, params))
            group = re.search(r"GROUP BY (\w+) ORDER BY", sql)[1]
            fixture = {
                "mes": [(202602, "50.19"), (202603, "60.02"), (202604, "150"), (202605, "120"), (202606, "130")],
                "trimestre": [(20261, "110.21"), (20262, "400")],
            }
            return SimpleNamespace(description=[SimpleNamespace(name=key) for key in (group, "valor", "filas", "observados")],
                                   fetchall=lambda: [(category, Decimal(value), 1, 1) for category, value in fixture[group]])

        db.execute.side_effect = execute
        with patch("banco_agentes.plans.connect_readonly") as connector, \
                patch("banco_agentes.plans.connect_client") as client_connection:
            connector.return_value.__enter__.return_value = db
            first = self.ask("Dame en tabla el total mensual de compras de TDC para PF preferente de febrero a junio.")
            self.assertEqual(len(first["tables"][0]["rows"]), 5)
            for question in ("Ahora agrúpalas por trimestre calendario y muéstralas en gráfica de barras.",
                             "Compáralas en barras, una por cada bloque de tres meses del calendario."):
                graphic = self.ask(question)
                self.assertIn("preparar_grafica", graphic["tools"])
                chart = graphic["charts"][0]
                self.assertEqual((chart["type"], chart["schema_version"]), ("barras", 2))
                self.assertEqual(chart["series"][0]["x"],
                                 ["T1 2026 (parcial, febrero a marzo)", "T2 2026 (abril a junio)"])
                self.assertEqual(chart["series"][0]["y"], ["110.21", "400"])
                self.assertNotIn("tus cuentas", graphic["answer"])
            client_connection.assert_not_called()
        self.assertEqual(len(calls), 3)
        for sql, params in calls:
            self.assertIn("FROM cfg.fact_txn_captacoloca_d", sql)
            self.assertNotIn("f.num_cte = %s", sql)
            self.assertEqual(params, ["TDC", "PF", "PREFERENTE", 400, list(range(202602, 202607))])

    def test_unsupported_group_explains_limit_and_rejects_monthly_retry(self):
        with patch("banco_agentes.plans.connect_readonly") as connector:
            result = self.ask("Dame saldo de AHORRO por trimestre calendario de febrero a junio de 2026.")
            connector.assert_not_called()
        self.assertIn("El trimestre calendario está disponible para el importe total de compras de TDC.", result["answer"])
        self.assertFalse(result["tables"] or result["charts"])
        audit = json.loads(self.audit.read_text(encoding="utf-8").splitlines()[-1])
        plans = [event for event in audit["trace"] if event["event"] == "validated_plan"]
        self.assertEqual(len(plans), 2)
        self.assertTrue(all(event["status"] == "invalid_plan" and not event["sql_executed"] for event in plans))
