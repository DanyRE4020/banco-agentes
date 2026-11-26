"""Tasas, brechas y regresión de Graph por la API con saldos sintéticos."""
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from banco_agentes.variation import percentage, variation_report, variation_request, variation_text
from web_fixtures import create_app
from test_graph import WorkflowModel, simulated_query
from test_presentation import result

BALANCES = {
    "CHEQUES": (50160000, 50661600, 51168216, 52000000, 53000000, 53239824),
    "AHORRO": (24870000, 25118700, 25369887, 25400000, 26000000, 26397018),
}
INLINE_CALCULATION = "¿Cuál creció más en porcentaje, AHORRO o CHEQUES, entre enero y junio de 2026? Muéstrame el cálculo"
SHOWN_GROWTH = ("Con los saldos ya mostrados, calcula el crecimiento porcentual de CHEQUES, TDC y AHORRO entre "
                "el cierre de enero y el de junio de 2026. Muestra los tres en una gráfica e indica cuál creció más.")


class VariationTests(unittest.TestCase):
    def test_shown_balances_reuse_three_products_and_reject_missing_or_different_period(self):
        figures = {**BALANCES, "TDC": (10000, 10100, 10400, 10500, 10900, 11237)}

        def query(plan, *, quality=False):
            evidence = simulated_query(plan, quality=quality)
            for row, value in zip(evidence["rows"], figures[plan["producto"]], strict=True):
                row["valor"] = str(value)
            return evidence

        with patch("banco_agentes.general_agents.create_model",
                   side_effect=lambda name, *a, **kw: WorkflowModel(model="simulado", agent=name)) as model, \
                patch("banco_agentes.general_agents.execute_plan", side_effect=query) as sql, \
                TestClient(create_app(audit_path=None), base_url="http://localhost") as client:
            client.post("/api/login", json={"username": "analista", "password": "AnalistaDemo!2026"}).raise_for_status()
            cid = client.post("/api/conversations").json()["conversation_id"]

            def ask(question):
                response = client.post("/api/chat", json={"question": question, "conversation_id": cid})
                response.raise_for_status()
                return response.json()

            monthly = ask("Grafica en línea los saldos de CHEQUES, TDC y AHORRO de enero a junio de 2026")["charts"][0]
            self.assertEqual(sql.call_count, 3)
            model.reset_mock()
            growth = ask(SHOWN_GROWTH)
            chart = growth["charts"][0]
            self.assertEqual(chart["result_ids"], monthly["result_ids"])
            self.assertEqual({s["name"]: s["y"] for s in chart["series"]},
                             {"CHEQUES": ["6.14"], "TDC": ["12.37"], "AHORRO": ["6.14"]})
            self.assertTrue(growth["answer"].startswith("TDC fue el producto que más creció, con 12.37%.\n\n"))
            self.assertEqual(growth["tables"][0]["rows"], [
                {"producto": "CHEQUES", "crecimiento": "6.14%"},
                {"producto": "TDC", "crecimiento": "12.37%"},
                {"producto": "AHORRO", "crecimiento": "6.14%"},
            ])
            self.assertIn("data_graph", growth["agents"])
            for question, detail in (
                (SHOWN_GROWTH.replace("AHORRO", "C_PERSONAL"), "C_PERSONAL"),
                (SHOWN_GROWTH.replace("enero", "febrero"), "no cubren el periodo solicitado"),
            ):
                rejected = ask(question)
                self.assertEqual(rejected["charts"], [])
                self.assertIn(detail, rejected["answer"])
                self.assertNotIn("producto que más creció", rejected["answer"])
            self.assertEqual(sql.call_count, 3)
            model.assert_not_called()

    def test_inline_calculation_uses_the_requested_period_and_prints_both_substitutions(self):
        request = variation_request(INLINE_CALCULATION, previous_mode="mensual", has_balances=True)
        self.assertEqual(request.mode, "periodo")
        self.assertTrue(request.calculation and request.formula)
        self.assertFalse(request.followup)

        def query(plan, *, quality=False):
            evidence = simulated_query(plan, quality=quality)
            for row, value in zip(evidence["rows"], BALANCES[plan["producto"]], strict=True):
                row["valor"] = str(value)
            return evidence

        with patch("banco_agentes.general_agents.create_model", side_effect=lambda name, *a, **kw: WorkflowModel(model="simulado", agent=name)), \
                patch("banco_agentes.general_agents.execute_plan", side_effect=query) as sql, \
                TestClient(create_app(audit_path=None), base_url="http://localhost") as client:
            client.post("/api/login", json={"username": "analista", "password": "AnalistaDemo!2026"}).raise_for_status()
            cid = client.post("/api/conversations").json()["conversation_id"]
            response = client.post("/api/chat", json={"question": INLINE_CALCULATION, "conversation_id": cid})
            self.assertEqual(response.status_code, 200, response.text)
            data = response.json()
        self.assertEqual(sql.call_count, 2)
        self.assertEqual(data["charts"], [])
        self.assertIn("(saldo final − saldo base) / saldo base × 100", data["answer"])
        self.assertIn("CHEQUES. Saldo inicial (enero de 2026): 50,160,000. Saldo final (junio de 2026): 53,239,824.", data["answer"])
        self.assertIn("(53,239,824 − 50,160,000) / 50,160,000 × 100 = 6.14%", data["answer"])
        self.assertIn("AHORRO. Saldo inicial (enero de 2026): 24,870,000. Saldo final (junio de 2026): 26,397,018.", data["answer"])
        self.assertIn("(26,397,018 − 24,870,000) / 24,870,000 × 100 = 6.14%", data["answer"])
        export = os.getenv("BANCO_INLINE_CALCULATION_TEST_OUTPUT")
        if export:
            Path(export).write_text(json.dumps({"question": INLINE_CALCULATION, **data}, ensure_ascii=False), encoding="utf-8")

    def test_period_and_monthly_holes_and_zero_base(self):
        evidence = result(desde="2026-01", hasta="2026-06", agrupar=("mes",))
        evidence["rows"] = [
            {"mes": 202601, "valor": "0"}, {"mes": 202602, "valor": "10"},
            {"mes": 202604, "valor": "20"}, {"mes": 202605, "valor": "25"},
            {"mes": 202606, "valor": "30", "filas": 2, "observados": 1},
        ]
        evidence["status"] = "partial"
        period = variation_report([evidence], "periodo")
        self.assertEqual(period["series"][0]["y"], [None])
        self.assertIn("no calculable", variation_text(period, False))
        monthly = variation_report([evidence], "mensual")
        self.assertEqual(monthly["series"][0]["y"], [None, None, None, None, "25.00", None])
        self.assertEqual(monthly["series"][0]["reasons"][1], "saldo base cero")
        self.assertEqual(monthly["series"][0]["reasons"][3], "falta un saldo necesario")
        self.assertEqual(monthly["citations"], evidence["sources"])
        self.assertEqual(percentage("20", "18"), -10)
        self.assertIsNone(percentage(None, "18"))

    def test_brecha_uses_percentage_points_and_only_explains_formula_on_request(self):
        a, b = (result(producto=p, desde="2026-01", hasta="2026-02", agrupar=("mes",)) for p in BALANCES)
        b["id"] = "R2"
        for evidence, final in ((a, 106), (b, 104)):
            evidence["rows"] = [{"mes": 202601, "valor": "100"}, {"mes": 202602, "valor": str(final)}]
        report = variation_report([a, b], "periodo")
        question = variation_request("Compara la variación porcentual de saldos y explica la fórmula")
        answer = variation_text(report, question.formula)
        self.assertIn("CHEQUES: 6,00 %", answer)
        self.assertIn("AHORRO: 4,00 %", answer)
        self.assertIn("2,00 puntos porcentuales", answer)
        self.assertIn("(saldo final − saldo base) / saldo base × 100", answer)
        self.assertNotIn("Fórmula", variation_text(report, False))
        monthly = variation_report([a, b], "mensual")
        self.assertIn("Brecha mensual en puntos porcentuales: febrero 2,00", variation_text(monthly, False, compare=True))
        self.assertIsNone(variation_request("¿Qué tasa de interés ofrece el producto AHORRO?"))

    def test_calculation_then_percentages_then_exact_monthly_followup_reuses_balances(self):
        def query(plan, *, quality=False):
            evidence = simulated_query(plan, quality=quality)
            values = list(BALANCES[plan["producto"]])
            values[1] = values[0] * 102 // 100
            for row, value in zip(evidence["rows"], values, strict=True):
                row["valor"] = str(value)
            return evidence

        with patch("banco_agentes.general_agents.create_model", side_effect=lambda name, *a, **kw: WorkflowModel(model="simulado", agent=name)) as model, \
                patch("banco_agentes.general_agents.execute_plan", side_effect=query) as sql, \
                TestClient(create_app(audit_path=None), base_url="http://localhost") as client:
            client.post("/api/login", json={"username": "analista", "password": "AnalistaDemo!2026"}).raise_for_status()
            cid = client.post("/api/conversations").json()["conversation_id"]

            def ask(question):
                response = client.post("/api/chat", json={"question": question, "conversation_id": cid})
                self.assertEqual(response.status_code, 200, response.text)
                return response.json()

            initial = ask("Compara el crecimiento del periodo de AHORRO y CHEQUES de enero a junio de 2026")
            self.assertTrue(initial["answer"].startswith("CHEQUES y AHORRO comparten el mayor crecimiento, con 6.14%."))
            self.assertEqual([r["crecimiento"] for r in initial["tables"][0]["rows"]], ["6.14%", "6.14%"])
            self.assertEqual(sql.call_count, 2)
            model.reset_mock()
            explanation = ask("Muéstrame el cálculo")
            self.assertEqual(explanation["charts"], [])
            self.assertIn("(saldo final − saldo base) / saldo base × 100", explanation["answer"])
            self.assertIn("CHEQUES. Saldo inicial (enero de 2026): 50,160,000. Saldo final (junio de 2026): 53,239,824.", explanation["answer"])
            self.assertIn("(53,239,824 − 50,160,000) / 50,160,000 × 100 = 6.14%", explanation["answer"])
            self.assertIn("AHORRO. Saldo inicial (enero de 2026): 24,870,000. Saldo final (junio de 2026): 26,397,018.", explanation["answer"])
            self.assertIn("(26,397,018 − 24,870,000) / 24,870,000 × 100 = 6.14%", explanation["answer"])
            self.assertTrue(explanation["sources"])

            bars = ask("Grafica esos porcentajes")["charts"][0]
            self.assertEqual(bars["rate_mode"], "periodo")
            self.assertEqual(bars["type"], "barras")
            self.assertEqual([s["y"] for s in bars["series"]], [["6.14"], ["6.14"]])
            response = ask("Ahora grafica la variación porcentual mes a mes")
            monthly = response["charts"][0]
            self.assertEqual(monthly["rate_mode"], "mensual")
            self.assertEqual(monthly["type"], "linea")
            self.assertEqual(monthly["units"], "%")
            self.assertEqual(monthly["y_label"], "Variación (%)")
            self.assertEqual(monthly["result_ids"], bars["result_ids"])
            self.assertEqual(monthly["citations"], bars["citations"])
            self.assertEqual([s["name"] for s in monthly["series"]], ["CHEQUES", "AHORRO"])
            self.assertTrue(all(s["x"] == [f"2026-{m:02d}" for m in range(1, 7)] for s in monthly["series"]))
            self.assertEqual([s["y"][:2] for s in monthly["series"]], [[None, "2.00"], [None, "2.00"]])
            self.assertIn("data_graph", response["agents"])
            self.assertNotIn("data_analytics", response["agents"])
            self.assertEqual(sql.call_count, 2)
            model.assert_not_called()

    def test_percent_comparison_then_two_graphs_in_same_conversation(self):
        def query(plan, *, quality=False):
            evidence = simulated_query(plan, quality=quality)
            for row, value in zip(evidence["rows"], BALANCES[plan["producto"]], strict=True):
                row["valor"] = str(value)
            return evidence

        with patch("banco_agentes.general_agents.create_model", side_effect=lambda name, *a, **kw: WorkflowModel(model="simulado", agent=name)), \
                patch("banco_agentes.general_agents.execute_plan", side_effect=query) as sql, \
                TestClient(create_app(audit_path=None), base_url="http://localhost") as client:
            client.post("/api/login", json={"username": "analista", "password": "AnalistaDemo!2026"}).raise_for_status()
            cid = client.post("/api/conversations").json()["conversation_id"]

            def ask(question):
                response = client.post("/api/chat", json={"question": question, "conversation_id": cid})
                self.assertEqual(response.status_code, 200, response.text)
                return response.json()

            balances = ask("Grafica en línea el saldo de AHORRO y CHEQUES de enero a junio de 2026")
            self.assertEqual(sql.call_count, 2)
            text = ask("Compara el porcentaje de crecimiento de AHORRO y CHEQUES de enero a junio de 2026")
            self.assertEqual(sql.call_count, 2)
            self.assertEqual(text["charts"], [])
            self.assertEqual([r["crecimiento"] for r in text["tables"][0]["rows"]], ["6.14%", "6.14%"])
            self.assertIn("0.00 puntos porcentuales", text["answer"])
            self.assertNotIn("Fórmula", text["answer"])

            bars = ask("grafica esos porcentajes")
            monthly = ask("grafica el crecimiento mes a mes")
            self.assertEqual(sql.call_count, 2)
            period_chart, monthly_chart = bars["charts"][0], monthly["charts"][0]
            self.assertEqual((period_chart["type"], monthly_chart["type"]), ("barras", "linea"))
            self.assertEqual(period_chart["result_ids"], balances["charts"][0]["result_ids"])
            self.assertEqual(monthly_chart["result_ids"], period_chart["result_ids"])
            self.assertEqual([s["y"] for s in period_chart["series"]], [["6.14"], ["6.14"]])
            self.assertEqual([s["y"] for s in monthly_chart["series"]], [
                [None, "1.00", "1.00", "1.63", "1.92", "0.45"],
                [None, "1.00", "1.00", "0.12", "2.36", "1.53"],
            ])
            for response in (bars, monthly):
                self.assertEqual(response["charts"][0]["units"], "%")
                self.assertIn("data_graph", response["agents"])
                self.assertIn("preparar_grafica", response["tools"])
                self.assertTrue(response["sources"])
            formula = ask("¿Cuál es la fórmula?")
            self.assertIn("El saldo base es el del mes anterior.", formula["answer"])
            self.assertEqual(sql.call_count, 2)
            alternate = ask("grafica el crecimiento mes a mes en barras agrupadas")
            self.assertEqual(alternate["charts"][0]["series"], monthly_chart["series"])
            self.assertEqual(alternate["charts"][0]["type"], "barras")
            self.assertEqual(sql.call_count, 2)
            cid = client.post("/api/conversations").json()["conversation_id"]
            fresh = ask("Compara el crecimiento del periodo de AHORRO y CHEQUES de enero a junio de 2026")
            self.assertEqual([r["crecimiento"] for r in fresh["tables"][0]["rows"]], ["6.14%", "6.14%"])
            self.assertEqual(sql.call_count, 4)
            export = os.getenv("BANCO_VARIATION_TEST_OUTPUT")
            if export:
                Path(export).write_text(json.dumps([bars, monthly], ensure_ascii=False), encoding="utf-8")
