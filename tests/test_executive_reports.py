import json
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

import fitz
from fastapi.testclient import TestClient
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.genai import types

from banco_agentes.auth import password_record
from banco_agentes.executive_reports import Analysis, Draft, requested, select, verified_draft
from banco_agentes.report_pdf import report_charts
from banco_agentes.web import create_app


REQUEST = "Genera un reporte ejecutivo en PDF con estos análisis"
COUNT = Analysis(("R1",), "clientes", "Clientes del catálogo", (), frozenset(),
                 "El catálogo contiene 5.000 clientes.", (), (), ())
BALANCES = Analysis(("R2",), "saldos", "Saldos por producto", ("2026-06",), frozenset({"AHORRO", "TDC"}),
                    "El saldo de AHORRO fue 125,00.", (), (), ())
GROWTH = Analysis(("R2", "R3"), "evolucion", "Crecimiento", ("2026-01", "2026-06"),
                  frozenset({"AHORRO", "TDC"}), "El crecimiento fue 25 %.", (), (), ())


class ReportHubModel(BaseLlm):
    async def generate_content_async(self, llm_request, stream=False):
        if set(llm_request.tools_dict) != {"generar_reporte_pdf"}:
            raise AssertionError("La exportación solo puede invocar generar_reporte_pdf.")
        call = types.FunctionCall(name="generar_reporte_pdf", args={})
        yield LlmResponse(content=types.Content(role="model", parts=[types.Part(function_call=call)]))


class ExecutiveReportTests(unittest.TestCase):
    def test_direct_pdf_and_singular_reference_use_the_previous_matching_analysis(self):
        earlier = replace(GROWTH, ids=("R0",), periods=("2026-02", "2026-03"))
        analyses = (COUNT, earlier, BALANCES, GROWTH)
        for question in ("hazme un pdf de esta informacion",
                         "Genera un reporte ejecutivo en PDF con este análisis de crecimiento",
                         "Por favor, dame un archivo PDF de este resultado"):
            with self.subTest(question=question):
                self.assertTrue(requested(question))
                self.assertEqual(select(question, analyses), (GROWTH,))
                self.assertEqual(select(question, ()), ())
        self.assertEqual(select(REQUEST, analyses), analyses)
        self.assertEqual(select("Genera un PDF con este análisis de movimientos", analyses), ())
        for question in ("¿Qué es un PDF?", "Lee este PDF", "¿Cómo genero un PDF?"):
            self.assertFalse(requested(question))

    def test_explicit_scope_and_missing_evidence(self):
        analyses = (COUNT, BALANCES, GROWTH)
        examples = {
            REQUEST: analyses,
            "Prepara un informe en PDF del conteo de clientes.": (COUNT,),
            "Elabora un reporte en PDF sobre saldos de junio de 2026.": (BALANCES,),
            "Haz un informe en PDF del análisis R2.": (BALANCES,),
            "Haz un informe en PDF del último análisis.": (GROWTH,),
            "Genera un reporte en PDF sobre los movimientos.": (),
            "Dame un informe en PDF de saldos de junio de 2025.": (),
            "Dame un informe en PDF de saldos de AHORRO.": (),
            "Haz un informe en PDF de R1 y R99.": (),
        }
        for question, expected in examples.items():
            with self.subTest(question=question):
                self.assertEqual(select(question, analyses), expected)

    def test_only_verified_values_and_order_enter_narrative(self):
        draft = Draft(resumen="El análisis establece la cobertura del catálogo.",
                      hallazgos=["La base de clientes da contexto a los saldos.",
                                 "La revisión continúa con el saldo por producto."],
                      conclusion="El alcance queda delimitado por las consultas seleccionadas.")
        self.assertEqual(verified_draft(draft, (COUNT, BALANCES))[1].split(". ")[-1], "El catálogo contiene 5.000 clientes.")
        for text in ("Se analizaron 900 clientes.", "Los saldos crecieron debido a nueva captación.",
                     "La revisión utiliza {una fuente}.", ""):
            with self.subTest(text=text), self.assertRaises(ValueError):
                verified_draft(draft.model_copy(update={"hallazgos": [text, draft.hallazgos[1]]}), (COUNT, BALANCES))

    def test_saved_chart_requires_every_result_in_its_scope(self):
        grouped = {"result_ids": ["R2", "R3"]}
        chart_a = {"result_ids": ["R2"]}
        chart_b = {"result_ids": ["R3"]}
        first = replace(BALANCES, charts=(chart_a,), shared_charts=(grouped,))
        second = replace(first, ids=("R3",), charts=(chart_b,))
        self.assertEqual(report_charts([first, second]), [(), (grouped,)])
        self.assertEqual(report_charts([second]), [(chart_b,)])

    def test_masked_ranking_and_download_bound_to_login_and_conversation(self):
        users = {username: {"rol": role, "num_cte": customer, **password_record("clave-sintetica")}
                 for username, role, customer in (("empleado_prueba", "Empleado", None), ("cliente_prueba", "Cliente", 1))}
        rows = [{"posicion": i, "alias": f"Titular {letter}", "saldo": 1000 - i,
                 "num_cte": "IDENTIFICADOR-PRIVADO"} for i, letter in enumerate("ABCDE", 1)]
        with TemporaryDirectory() as folder, \
                patch("banco_agentes.employee_flow.rank_employee_customers",
                      return_value={"status": "ok", "sql_executed": True, "rows": rows}), \
                patch("banco_agentes.general_agents.create_model", return_value=ReportHubModel(model="test-report-hub")), \
                patch("banco_agentes.web.executive_reports.compose", new_callable=AsyncMock) as compose:
            compose.return_value = ["El informe reúne el ranking autorizado.",
                                    "Los puestos se conservan en su orden original.",
                                    "La tabla utiliza los alias autorizados."]
            app = create_app(users=users, audit_path=Path(folder) / "audit.jsonl")
            with TestClient(app, base_url="http://127.0.0.1") as author, \
                    TestClient(app, base_url="http://127.0.0.1") as second, \
                    TestClient(app, base_url="http://127.0.0.1") as client, \
                    TestClient(app, base_url="http://127.0.0.1") as anonymous:
                for browser, username in ((author, "empleado_prueba"), (second, "empleado_prueba"),
                                          (client, "cliente_prueba")):
                    browser.post("/api/login", json={"username": username, "password": "clave-sintetica"}).raise_for_status()
                cid, other = (author.post("/api/conversations").json()["conversation_id"] for _ in range(2))
                rank = author.post("/api/chat", json={"conversation_id": cid,
                    "question": "¿Cuáles fueron los cinco clientes con mayor saldo total en AHORRO al cierre de junio de 2026?"})
                rank.raise_for_status()
                self.assertNotIn("report_id", author.post("/api/chat", json={
                    "conversation_id": other, "question": REQUEST}).json())
                report = author.post("/api/chat", json={"conversation_id": cid, "question": "hazme un pdf de esta informacion"})
                report.raise_for_status()
                rid = report.json()["report_id"]
                self.assertEqual(report.json()["tools"], ["generar_reporte_pdf"])
                records = (Path(folder) / "audit.jsonl").read_text(encoding="utf-8").splitlines()
                entry = next(json.loads(line) for line in records if json.loads(line)["trace_id"] == report.json()["trace_id"])
                selection = next(event for event in entry["trace"] if event["event"] == "report_selection")
                self.assertEqual(selection["evidencia"], [
                    {"ids": ["R1"], "tema": "ranking", "periodo": ["2026-06"], "productos": ["AHORRO"]}
                ])
                self.assertFalse(selection["sql_executed"])
                self.assertFalse(entry["plans"])
                path = f"/api/conversations/{cid}/reports/{rid}"
                download = author.get(path)
                self.assertEqual(download.status_code, 200)
                self.assertEqual(download.headers["content-type"], "application/pdf")
                self.assertEqual(download.headers["cache-control"], "no-store")
                with fitz.open(stream=download.content, filetype="pdf") as doc:
                    content = "".join(page.get_text() for page in doc)
                self.assertTrue(all(f"Titular {letter}" in content for letter in "ABCDE"))
                self.assertNotIn("IDENTIFICADOR-PRIVADO", content)
                self.assertNotIn("num_cte", content)
                self.assertEqual(author.get(f"/api/conversations/{other}/reports/{rid}").status_code, 404)
                self.assertEqual(second.get(path).status_code, 404)
                self.assertEqual(anonymous.get(path).status_code, 401)
                self.assertEqual(client.get(path).status_code, 403)
                self.assertTrue(client.post("/api/chat", json={"question": REQUEST}).json()["blocked"])
                author.post("/api/logout").raise_for_status()
                self.assertEqual(author.get(path).status_code, 401)
                self.assertEqual(compose.await_count, 1)
                self.assertNotIn("IDENTIFICADOR-PRIVADO", str(compose.await_args))
