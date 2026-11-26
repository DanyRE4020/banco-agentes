"""Regresión de formatos, límites de Graph y flujo ADK por API sin red externa."""
import json
import os
import re
import tempfile
import unittest
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch, AsyncMock

from fastapi.testclient import TestClient
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.genai import types

from banco_agentes.catalog import ROOT
from banco_agentes.charts import chart_for_results
from banco_agentes.general_agents import EVIDENCE, GRAPH_IDS, Turn, build_general_agents, compact_result
from banco_agentes.governance import DemoSession, REQUEST, SESSION
from banco_agentes.plans import normalized_plan
from banco_agentes.runtime import Trace
from web_fixtures import create_app
from test_presentation import result


class GraphContractTests(unittest.TestCase):
    def test_existing_bar_line_histogram_and_scatter_keep_values(self):
        a = result(producto="TDC", desde="2026-01", hasta="2026-06", agrupar=("mes",))
        a["rows"] = [{"mes": 202600 + m, "valor": f"{m}.1234"} for m in range(1, 7)]
        a["columns"] = ["mes", "valor"]
        for kind in ("barras", "linea"):
            with self.subTest(kind=kind):
                chart = chart_for_results([a], kind)
                self.assertEqual([p["y"] for p in chart["points"]], [r["valor"] for r in a["rows"]])
                self.assertEqual(chart["period"], "2026-01 a 2026-06")
        h = result("histograma")
        h["rows"] = [dict(desde="1.25", hasta="3.25", frecuencia=7)]
        self.assertEqual(chart_for_results([h], "histograma")["points"][0]["y"], 7)
        d = result("dispersion", agrupar=("segmento",))
        d["rows"] = [dict(segmento="MASIVO", apertura="10.25", cierre="11.35")]
        point = chart_for_results([d], "dispersion")["points"][0]
        self.assertEqual((point["x"], point["y"]), ("10.25", "11.35"))
        with self.assertRaisesRegex(ValueError, "Tipos disponibles"):
            chart_for_results([a], "circular")

    def test_combined_bars_keep_decimal_values_ids_and_sources_without_sql(self):
        a, b = result(), result(producto="AHORRO")
        b["id"] = "R2"
        a["rows"][0]["valor"] = "12345678901234567.8901"
        b["rows"][0]["valor"] = "12345678901234567.8900"
        with patch("banco_agentes.plans.connect_readonly") as db:
            chart = chart_for_results([a, b], "barras")
        db.assert_not_called()
        self.assertEqual(chart["result_ids"], ["R1", "R2"])
        self.assertEqual([p["label"] for p in chart["points"]], ["CHEQUES", "AHORRO"])
        self.assertEqual([p["y"] for p in chart["points"]], [a["rows"][0]["valor"], b["rows"][0]["valor"]])
        self.assertEqual([p["result_id"] for p in chart["points"]], ["R1", "R2"])
        self.assertEqual(chart["citations"], a["sources"])

    def test_incompatible_period_metric_units_filter_scope_and_missing_data_are_rejected(self):
        a = result()
        for change in (dict(desde="2026-02"), dict(metrica="promedio"), dict(persona="PF"), dict(campo="saldo_apertura")):
            b = result(**change)
            b["id"] = "R2"
            with self.subTest(change=change), self.assertRaises(ValueError):
                chart_for_results([a, b], "barras")
        for key, value in (("units", "Clientes"), ("scope", "propio"), ("status", "partial"), ("rows", [])):
            b = deepcopy(a)
            b.update(id="R2", **{key: value})
            with self.subTest(field=key), self.assertRaises(ValueError):
                chart_for_results([a, b], "barras")
        with self.assertRaises(ValueError):
            chart_for_results([a, a], "barras")

    def test_tool_is_only_on_graph_and_blocks_stale_ids_omissions_and_unauthorized_scope(self):
        turn = Turn(Trace(lambda _: None), {"results": [result()]})
        a, b = result(), result(producto="AHORRO")
        b["id"] = "R2"
        turn.results = [a, b]
        hub = build_general_agents(turn, lambda name: WorkflowModel(model="simulado", agent=name))
        graph = next(t.agent for t in hub.tools if t.name == "data_graph")
        analytics = next(t.agent for t in hub.tools if t.name == "data_analytics")
        self.assertEqual([t.__name__ for t in graph.tools], ["preparar_grafica"])
        self.assertNotIn("preparar_grafica", [t.__name__ for t in analytics.tools])
        tool = graph.tools[0]
        token = EVIDENCE.set([])
        allowed = GRAPH_IDS.set(("R1", "R2"))
        try:
            for ids in (["R1"], ["R1", "R1"], ["R999"], [], [1]):
                self.assertEqual(tool(resultados=ids)["status"], "invalid_chart")
            self.assertEqual(turn.charts, [])
            session_token = SESSION.set(DemoSession("Cliente", 1, 200000))
            try:
                denied = tool(resultados=["R1", "R2"])
                self.assertEqual(denied["status"], "denied")
                self.assertNotIn("rows", denied)
                self.assertEqual(turn.charts, [])
            finally:
                SESSION.reset(session_token)
        finally:
            EVIDENCE.reset(token)
            GRAPH_IDS.reset(allowed)

    def test_analytics_evidence_keeps_all_rows_and_columns(self):
        a = result()
        a["rows"] *= 12
        self.assertEqual(compact_result(a)["rows"], a["rows"])
        self.assertEqual(compact_result(a)["columns"], a["columns"])


class WorkflowModel(BaseLlm):
    """Modelo simulado; solo los tests deciden estas respuestas de herramientas."""
    agent: str

    async def generate_content_async(self, llm_request, stream=False):
        content = llm_request.contents[-1]
        response = next((p.function_response for p in content.parts if p.function_response), None)
        question = "".join(p.text or "" for c in llm_request.contents if c.role == "user" for p in c.parts or [])
        if self.agent == "orquestador":
            line = "línea" in question or "evolución" in question
            table = "solo tabla" in question
            if response is None:
                if "Te faltó" in question:
                    instruction = str(llm_request.config.system_instruction)
                    context = json.JSONDecoder().raw_decode(instruction.split("Memoria de la conversación (datos, no instrucciones): ")[1])[0]
                    last = [r for r in context["resultados"] if r["id"] in context["ultimos_resultados"]]
                    products = [r["plan"]["producto"] for r in last]
                    assert set(products) == {"CHEQUES", "AHORRO"}
                    assert all(r["plan"]["desde"] == r["plan"]["hasta"] == "2026-06" and r["plan"]["metrica"] == "total" for r in last)
                else:
                    products = list(dict.fromkeys(re.findall(r"CHEQUES|AHORRO|TDC|C_PERSONAL", question)))
                monthly = line or "enero a junio" in question or "entre enero y junio" in question
                brief = dict(products=products, period="2026-01/2026-06" if monthly else "2026-06", line=monthly)
                parts = [self.call("data_analytics", request=json.dumps(brief))]
            elif response.name == "data_analytics":
                ids = [r["id"] for r in response.response["evidence"]]
                if table:
                    parts = [self.final("tabla", ids)]
                else:
                    parts = [self.call("data_graph", request=("linea" if line else "barras") + " con " + ", ".join(ids))]
            else:
                ids = response.response["evidence"][0]["result_ids"]
                parts = [self.final("grafica", ids, "linea" if line else "barras")]
        elif response is not None:
            parts = [types.Part(text="Evidencia revisada.")]
        elif self.agent == "data_analytics":
            text = "".join(p.text or "" for p in content.parts)
            brief = json.JSONDecoder().raw_decode(text.split("Encargo del hub: ")[1])[0]
            parts = [self.call("consultar_datos", metrica="total", producto=product, periodo=brief["period"],
                               opciones="agrupar=mes" if brief["line"] else "") for product in brief["products"]]
        else:
            text = "".join(p.text or "" for p in content.parts)
            ids = list(dict.fromkeys(re.findall(r"\bR[1-9][0-9]*\b", text)))
            parts = [self.call("preparar_grafica", resultados=ids, tipo="linea" if "Encargo del hub: linea" in text else "barras")]
        yield LlmResponse(content=types.Content(role="model", parts=parts))

    @staticmethod
    def call(name, **args):
        return types.Part(function_call=types.FunctionCall(name=name, args=args))

    @staticmethod
    def final(mode, ids, kind="auto"):
        return types.Part(text=json.dumps(dict(presentacion=dict(formato=mode, objetivo="comparar" if len(ids) > 1 else "tendencia",
                                                               resultados=ids, tipo_grafica=kind), respuesta="")))


def simulated_query(plan, *, quality=False):
    plan = normalized_plan(plan, quality=quality)
    amounts = {"CHEQUES": "100.10", "AHORRO": "75.05", "TDC": "200.30", "C_PERSONAL": "50.15"}
    a = result()
    a["plan"] = plan
    a["rows"] = ([dict(mes=month, valor=format(Decimal(amounts[plan["producto"]]) + month % 100, "f"), filas=2, observados=2)
                  for month in plan["periodos"]] if plan["agrupar"] else [dict(valor=amounts[plan["producto"]], filas=2, observados=2)])
    a["columns"] = list(a["rows"][0])
    return a


class GraphWebTests(unittest.TestCase):
    def test_plotly_series_and_followups_keep_the_analytics_values(self):
        def query(plan, *, quality=False):
            data = simulated_query(plan, quality=quality)
            if plan["producto"] == "TDC" and "corte ausente" in REQUEST.get():
                data["rows"] = [row for row in data["rows"] if row["mes"] != 202603]
                data.update(status="partial", notes=["Cortes sin filas: 202603."])
            return data

        with tempfile.TemporaryDirectory(dir=ROOT, prefix="plotly-test-") as folder:
            audit = Path(folder) / "audit.jsonl"
            with patch("banco_agentes.general_agents.create_model", side_effect=lambda name, *a, **kw: WorkflowModel(model="simulado", agent=name)), \
                    patch("banco_agentes.general_agents.execute_plan", side_effect=query) as sql, \
                    TestClient(create_app(audit_path=audit), base_url="http://localhost") as client:
                client.post("/api/login", json={"username": "analista", "password": "AnalistaDemo!2026"}).raise_for_status()

                def ask(question, cid=None):
                    cid = cid or client.post("/api/conversations").json()["conversation_id"]
                    response = client.post("/api/chat", json={"question": question, "conversation_id": cid})
                    response.raise_for_status()
                    payload = response.json()
                    self.assertEqual(len(payload["charts"]), 1, payload["answer"])
                    self.assertTrue(payload["sources"])
                    self.assertIn("data_graph", payload["agents"])
                    self.assertIn("preparar_grafica", payload["tools"])
                    return payload, cid

                grouped, _ = ask("Grafica el saldo de CHEQUES, TDC y AHORRO en barras agrupadas de enero a junio de 2026")
                series = grouped["charts"][0]["series"]
                self.assertEqual([s["name"] for s in series], ["CHEQUES", "TDC", "AHORRO"])
                self.assertTrue(all(len(s["y"]) == 6 and None not in s["y"] for s in series))
                lines, cid = ask("Grafica en línea el saldo de AHORRO y CHEQUES de enero a junio de 2026")
                before = sql.call_count
                changed, _ = ask("ponla en barras agrupadas", cid)
                self.assertEqual(sql.call_count, before)
                self.assertEqual(lines["charts"][0]["series"], changed["charts"][0]["series"])
                self.assertTrue(lines["answer"].startswith("De enero a junio de 2026.\n\n- CHEQUES pasó"))
                self.assertTrue(changed["answer"].startswith("Cambié la gráfica a barras agrupadas con los mismos datos."))
                self.assertEqual([c["charts"][0]["type"] for c in (lines, changed)], ["linea", "barras"])
                self.assertNotIn("data_analytics", changed["agents"])
                june, cid = ask("Grafica AHORRO y TDC al cierre de junio de 2026")
                before = sql.call_count
                added, _ = ask("agrega CHEQUES a la gráfica anterior", cid)
                self.assertEqual(sql.call_count, before + 1)
                self.assertEqual(sql.call_args.args[0]["producto"], "CHEQUES")
                self.assertEqual(added["charts"][0]["result_ids"][:2], june["charts"][0]["result_ids"])
                self.assertEqual([s["name"] for s in added["charts"][0]["series"]], ["CHEQUES", "TDC", "AHORRO"])
                before = sql.call_count
                repeated, _ = ask("agrega CHEQUES a la gráfica anterior", cid)
                self.assertEqual(sql.call_count, before)
                self.assertEqual(repeated["charts"][0]["series"], added["charts"][0]["series"])
                missing, _ = ask("Grafica CHEQUES, TDC y AHORRO en barras agrupadas de enero a junio de 2026 con un corte ausente")
                self.assertEqual(missing["charts"][0]["series"][1]["y"], ["201.30", "202.30", None, "204.30", "205.30", "206.30"])
                self.assertIn("script-src 'self'", client.get("/").headers["content-security-policy"])
                self.assertIn("/assets/plotly.min.js", client.get("/").text)

            responses = [grouped, lines, changed, june, added, repeated, missing]
            records = [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines()]
            for payload, record in zip(responses, records, strict=True):
                inputs = next(event for event in record["trace"] if event["event"] == "graph_input")
                data = {(row["producto"], row["mes"]): (row["valor"], result["id"])
                        for result in inputs["results"] for row in result["series_rows"]}
                chart = payload["charts"][0]
                self.assertEqual(chart["result_ids"], inputs["result_ids"])
                for trace in chart["series"]:
                    expected = [data.get((trace["name"], month), (None, None)) for month in trace["x"]]
                    self.assertEqual(list(zip(trace["y"], trace["result_ids"])), expected)
                self.assertEqual(chart["units"], inputs["results"][0]["units"])
                self.assertEqual(chart["metric"], inputs["results"][0]["plan"]["metrica"])
            evidence_path = os.getenv("BANCO_PLOTLY_TEST_OUTPUT")
            if evidence_path:
                Path(evidence_path).write_text(json.dumps(responses, ensure_ascii=False), encoding="utf-8")

    def test_table_pairs_followup_and_tdc_line_through_actual_api_and_adk(self):
        with tempfile.TemporaryDirectory(dir=ROOT, prefix="graph-test-") as folder:
            audit = Path(folder) / "audit.jsonl"
            with patch("banco_agentes.general_agents.create_model", side_effect=lambda name, *a, **kw: WorkflowModel(model="simulado", agent=name)), \
                    patch("banco_agentes.general_agents.execute_plan", side_effect=simulated_query) as sql, \
                    TestClient(create_app(audit_path=audit), base_url="http://localhost") as client:
                client.post("/api/login", json={"username": "analista", "password": "AnalistaDemo!2026"}).raise_for_status()
                def ask(question, conversation=None):
                    conversation = conversation or client.post("/api/conversations", json={}).json()["conversation_id"]
                    r = client.post("/api/chat", json={"question": question, "conversation_id": conversation})
                    self.assertEqual(r.status_code, 200, r.text)
                    return r.json(), conversation
                table, _ = ask("Dame solo tabla de CHEQUES y AHORRO al cierre de junio de 2026")
                self.assertNotIn("data_graph", table["agents"])
                self.assertEqual(table["charts"], [])
                self.assertEqual(len(table["tables"][0]["rows"]), 2)
                pair, conversation = ask("Compara CHEQUES y AHORRO al cierre de junio de 2026 con barras")
                self.assertEqual(len(pair["charts"]), 1)
                self.assertEqual([p["y"] for p in pair["charts"][0]["points"]], ["100.10", "75.05"])
                self.assertTrue(pair["answer"].startswith("Al cierre de junio de 2026, saldo total:"))
                self.assertIn("- CHEQUES: 100.10\n- AHORRO: 75.05", pair["answer"])
                before = sql.call_count
                followup, _ = ask("Te faltó AHORRO en la gráfica", conversation)
                self.assertEqual(sql.call_count - before, 2)
                self.assertEqual(len(followup["charts"]), 1)
                self.assertEqual(followup["charts"][0]["points"][0]["y"], "100.10")
                self.assertEqual(followup["charts"][0]["result_ids"], ["R3", "R4"])
                other, _ = ask("Compara TDC y C_PERSONAL al cierre de junio de 2026 con barras")
                self.assertEqual([p["y"] for p in other["charts"][0]["points"]], ["200.30", "50.15"])
                line, _ = ask("Muestra en línea la evolución del saldo total de TDC de enero a junio de 2026")
                self.assertEqual(len(line["charts"]), 1)
                self.assertEqual(line["charts"][0]["type"], "linea")
                self.assertEqual(len(line["charts"][0]["points"]), 6)
                self.assertEqual([p["y"] for p in line["charts"][0]["points"]], [f"{201 + m}.30" for m in range(6)])
                records = [json.loads(r) for r in audit.read_text(encoding="utf-8").splitlines()]
                for record in records[1:]:
                    events = record["trace"]
                    graph = next(e for e in events if e["event"] == "chart_prepared")
                    selected = next(e for e in events if e["event"] == "graph_selection")
                    inputs = next(e for e in events if e["event"] == "graph_input")
                    self.assertEqual(graph["result_ids"], selected["result_ids"])
                    self.assertEqual(inputs["result_ids"], graph["result_ids"])
                    self.assertEqual([p["y"] for p in graph["chart"]["points"]],
                                     [p["y"] for p in next(d for d in (pair, followup, other, line) if d["charts"][0]["result_ids"] == graph["result_ids"] and d["answer"] == record["answer"])["charts"][0]["points"]])
                    self.assertFalse(graph["sql_executed"])


class GraphBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_history_id_is_rejected_before_calling_graph_model(self):
        old = result()
        old["id"] = "R99"
        turn = Turn(Trace(lambda _: None), {"results": [old]})
        turn.results = [result()]
        hub = build_general_agents(turn, lambda name: WorkflowModel(model="simulado", agent=name))
        graph = next(t for t in hub.tools if t.name == "data_graph")
        with patch("google.adk.tools.agent_tool.AgentTool.run_async", new_callable=AsyncMock) as call:
            reply = await graph.run_async(args={"request": "Prepara barras con R99"}, tool_context=None)
        call.assert_not_called()
        self.assertEqual(reply["status"], "clarify")
        self.assertEqual(turn.charts, [])

    async def test_graph_does_not_receive_global_values_under_client_profile(self):
        turn = Turn(Trace(lambda _: None), {})
        turn.results = [result()]
        token = SESSION.set(DemoSession("Cliente", 1, 200000))
        try:
            hub = build_general_agents(turn, lambda name: WorkflowModel(model="simulado", agent=name))
            graph = next(t for t in hub.tools if t.name == "data_graph")
            with patch("google.adk.tools.agent_tool.AgentTool.run_async", new_callable=AsyncMock) as call:
                reply = await graph.run_async(args={"request": "Prepara barras con R1"}, tool_context=None)
            call.assert_not_called()
            self.assertEqual(reply["status"], "denied")
            self.assertNotIn("rows", reply)
            self.assertFalse(any(e["event"] == "graph_input" for e in turn.trace.records))
        finally:
            SESSION.reset(token)
