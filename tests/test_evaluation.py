import hashlib
import importlib.util
import json
import tempfile
import sys
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx

from banco_agentes.catalog import ROOT
from banco_agentes.evaluation.data import CRITERIA, SMOKE_IDS, Case, load_cases, select_cases
from banco_agentes.evaluation.graders import grade
from banco_agentes.evaluation.publishing import EVALUATORS, match_examples, publish
from banco_agentes.evaluation.reference import CSV_SHA256, sql_reference
from banco_agentes.evaluation.transport import LocalAPI, correlate, sanitize
from banco_agentes.evaluation.ui_context import prepare_ui_context

CASES = {c.id: c for c in load_cases(ROOT / "evaluacion_banco_agentes.csv")}


def pair_output():
    facts = [dict(product=p, month=202605, metric="total", value=v) for p, v in (("CHEQUES", "100.10"), ("AHORRO", "75.05"))]
    plans = [dict(event="validated_plan", status="ok", sql_executed=True, agent="data_analytics", id=f"R{i+1}",
                  plan=dict(metrica="total", producto=f["product"], desde="2026-05", hasta="2026-05", persona=""),
                  rows=[dict(valor=f["value"])], table="cfg.fact_sdos_captacoloca_m", sources=["referencias/test.sql:1-3"]) for i, f in enumerate(facts)]
    trace = [dict(event="agent_started", agent=name) for name in ("orquestador", "data_analytics", "data_graph")]
    trace += [dict(event="tool_completed", agent=a, tool=t) for a, t in (("data_analytics", "consultar_datos"), ("data_graph", "preparar_grafica"))]
    trace += plans
    response = dict(answer="El saldo total mayor corresponde a CHEQUES al cierre de mayo de 2026, con 100,10. AHORRO al cierre de mayo de 2026 registra 75,05; la diferencia es 25,05.",
                    presentation=dict(result_ids=["R1", "R2"]), tables=[], sources=[dict(location="cfg.fact_sdos_captacoloca_m")],
                    charts=[dict(type="barras", period="2026-05", points=[dict(label=f["product"], y=f["value"]) for f in facts])])
    return dict(status="ok", correlation=dict(status="ok"), final=dict(response=response), trace=trace,
                sql_reference=dict(status="ok", facts=facts, derived=dict(winner="CHEQUES", difference="25.05")))


class DataEvaluationTests(unittest.TestCase):
    def test_real_csv_ids_questions_references_and_fingerprint(self):
        self.assertEqual(len(CASES), 21)
        self.assertEqual(hashlib.sha256((ROOT / "evaluacion_banco_agentes.csv").read_bytes()).hexdigest(), CSV_SHA256)
        self.assertEqual([c.id for c in select_cases(list(CASES.values()), SMOKE_IDS)], ["BA-006", "BA-002", "BA-012"])
        self.assertEqual(CASES["BA-002"].row["pregunta"], "y en mayo?")
        self.assertEqual(len(CASES["BA-002"].previous), 1)
        self.assertIn("mismo período", CASES["BA-012"].row["responde_correcto"])

    def test_remote_examples_are_checked_without_mutating_dataset(self):
        examples = [SimpleNamespace(id=c.id, inputs={k: c.row[k] for k in ("id", "pregunta", "perfil", "turnos_previos_json")},
                                    outputs={k: v for k, v in c.row.items() if k not in {"id", "pregunta", "perfil", "turnos_previos_json"}}, metadata={}) for c in CASES.values()]
        original = deepcopy(examples)
        self.assertEqual(len(match_examples(examples, list(CASES.values()))), 21)
        self.assertEqual(examples, original)
        examples[0].outputs["responde_correcto"] = "otra referencia"
        with self.assertRaises(ValueError):
            match_examples(examples, list(CASES.values()))

    def test_publication_registers_no_python_evaluators_and_never_uploads_scores(self):
        examples = [SimpleNamespace(id=c.id, inputs=dict(c.row), outputs={}, metadata={}) for c in CASES.values()]
        client = Mock()
        client.list_examples.return_value = examples
        def evaluate(target, **kwargs):
            self.assertEqual(kwargs["evaluators"], [])
            self.assertEqual(kwargs["summary_evaluators"], [])
            self.assertFalse(kwargs["metadata"]["python_scores_enabled"])
            self.assertEqual(kwargs["metadata"]["evaluator_owner"], "langsmith_ui")
            self.assertEqual(kwargs["max_concurrency"], 0)
            rows = []
            for example in kwargs["data"]:
                output = target(example.inputs)
                self.assertNotIn("evaluations", output)
                self.assertFalse(output["python_scores_enabled"])
                rows.append(dict(evaluation_results=dict(results=[])))
            return rows
        client.evaluate.side_effect = evaluate
        target = Mock(return_value=dict(python_scores_enabled=False, contexto_evaluacion="evidencia sin notas"))
        with patch("banco_agentes.evaluation.publishing.EVALUATORS", [Mock(side_effect=AssertionError("No debe calificar"))]):
            publish(list(CASES.values()), [CASES[k] for k in SMOKE_IDS], target, "evaluacion-data-agentes", CSV_SHA256, client=client)
        self.assertEqual(target.call_count, 3)
        client.create_example.assert_not_called()
        client.update_example.assert_not_called()
        client.create_dataset.assert_not_called()

    def test_ui_context_excludes_previous_answers_and_contains_original_references(self):
        output = pair_output()
        output.update(case_id="BA-002", history=[dict(response=dict(answer="RESPUESTA_ANTERIOR_NO_CALIFICAR"))])
        prepared = prepare_ui_context(CASES["BA-002"], output)
        context = json.loads(prepared["contexto_evaluacion"])
        self.assertEqual(context["pregunta"], "y en mayo?")
        self.assertEqual(context["turnos_previos"], CASES["BA-002"].previous)
        self.assertEqual(context["referencias_dataset"]["responde_correcto"], CASES["BA-002"].row["responde_correcto"])
        self.assertNotIn("RESPUESTA_ANTERIOR_NO_CALIFICAR", prepared["contexto_evaluacion"])
        self.assertNotIn("evaluations", prepared)
        self.assertEqual(context["referencia_sql"], output["sql_reference"])
        self.assertEqual(context["traza_ultimo_turno"], output["trace"])
        self.assertFalse(context["calificacion_python_habilitada"])

    def test_ui_context_follows_frontend_visibility_and_does_not_promote_hidden_data(self):
        output = pair_output()
        output["final"]["response"].update(presentation=dict(format="prosa"), query_details="DETALLES_NO_VISIBLES",
                                         tables=[dict(rows=[dict(valor="999.99")])])
        context = json.loads(prepare_ui_context(CASES["BA-002"], output)["contexto_evaluacion"])
        self.assertEqual(context["respuesta_visible"]["graficas"], [])
        self.assertEqual(context["respuesta_visible"]["tablas"], [])
        self.assertNotIn("DETALLES_NO_VISIBLES", json.dumps(context))

    def test_ui_context_refuses_cached_python_grades(self):
        output = pair_output()
        output["evaluations"] = {"responde_correcto": {"score": 1}}
        with self.assertRaises(ValueError):
            prepare_ui_context(CASES["BA-002"], output)

    def test_runner_runs_three_csv_cases_without_calling_python_grading(self):
        spec = importlib.util.spec_from_file_location("eval_runner_test", ROOT / "scripts/evaluar_langsmith.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory(dir=ROOT, prefix="evaluation-test-") as folder:
            path = Path(folder) / "ui.json"
            def api_result(case):
                output = pair_output()
                output.update(case_id=case.id, question=case.row["pregunta"], seconds=.01)
                return output
            with patch.object(sys, "argv", ["evaluar_langsmith.py", "--cases", *SMOKE_IDS, "--report", str(path)]), \
                    patch.object(module, "LocalAPI") as api, patch.object(module, "sql_reference", return_value=dict(status="no_verificable")), \
                    patch("banco_agentes.evaluation.graders.grade", side_effect=AssertionError("La UI califica")) as grader, patch("builtins.print"):
                api.return_value.run.side_effect = api_result
                self.assertEqual(module.main(), 0)
            grader.assert_not_called()
            report = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual([c["case_id"] for c in report["cases"]], list(SMOKE_IDS))
            self.assertFalse(report["python_scores_enabled"])
            self.assertNotIn("criterion_status_counts", report)
            self.assertTrue(all("evaluations" not in c for c in report["cases"]))

    def test_six_ui_rubrics_have_one_mapping_and_separate_feedback_keys(self):
        folder = ROOT / "evaluacion_langsmith"
        manifest = json.loads((folder / "configuracion_ui.json").read_text(encoding="utf-8"))
        self.assertFalse(manifest["python_evaluators_enabled"])
        self.assertEqual([e["feedback_key"] for e in manifest["evaluators"]], list(CRITERIA))
        self.assertEqual(manifest["variable"]["field"], "contexto_evaluacion")
        for evaluator in manifest["evaluators"]:
            prompt = (folder / evaluator["prompt_file"]).read_text(encoding="utf-8")
            self.assertEqual(prompt.count("{{contexto}}"), 1)
            self.assertIn("no_verificable", prompt)
            self.assertIn("no_aplica", prompt)
            self.assertEqual(len(evaluator["categories"]), 4)

    def test_remote_reference_mismatch_prevents_any_api_execution(self):
        examples = [SimpleNamespace(id=c.id, inputs=dict(c.row), outputs={}, metadata={}) for c in CASES.values()]
        examples[0].inputs["responde_correcto"] = "cambio remoto"
        client, target = Mock(), Mock()
        client.list_examples.return_value = examples
        with self.assertRaises(ValueError):
            publish(list(CASES.values()), [CASES[k] for k in SMOKE_IDS], target, "evaluacion-data-agentes", CSV_SHA256, client=client)
        target.assert_not_called()
        client.evaluate.assert_not_called()


class CorrelationTests(unittest.TestCase):
    def row(self, identifier="actual", timestamp="t"):
        return dict(question="q", profile="Analista", langfuse_trace_id=identifier, metrics=dict(timestamp_utc=timestamp), trace=[])

    def test_trace_id_has_priority_over_timestamp(self):
        response = dict(diagnostics=dict(trace_id="actual"), metrics=dict(timestamp_utc="otro"))
        row, status = correlate(response, [self.row(), self.row("ajena", "otro")], question="q", profile="Analista")
        self.assertEqual(row["langfuse_trace_id"], "actual")
        self.assertEqual(status["method"], "trace_id")

    def test_ambiguous_id_is_never_replaced_by_last_record(self):
        row, status = correlate(dict(diagnostics=dict(trace_id="actual")), [self.row(), self.row()], question="q", profile="Analista")
        self.assertIsNone(row)
        self.assertEqual(status["status"], "no_verificable")
        self.assertEqual(status["matches"], 2)

    def test_missing_id_match_does_not_fall_back_to_timestamp(self):
        row, status = correlate(dict(diagnostics=dict(trace_id="inexistente"), metrics=dict(timestamp_utc="t")), [self.row()], question="q", profile="Analista")
        self.assertIsNone(row)
        self.assertEqual(status["matches"], 0)

    def test_timestamp_fallback_requires_profile_question_and_uniqueness(self):
        body = dict(metrics=dict(timestamp_utc="t"))
        self.assertIsNotNone(correlate(body, [self.row()], question="q", profile="Analista")[0])
        self.assertIsNone(correlate(body, [self.row(), self.row()], question="q", profile="Analista")[0])
        self.assertIsNone(correlate(body, [self.row()], question="q", profile="Cliente")[0])
        self.assertIsNone(correlate({}, [self.row()], question="q", profile="Analista")[0])


class TransportTests(unittest.TestCase):
    def test_isolated_login_conversation_order_last_trace_and_no_reference_leak(self):
        with tempfile.TemporaryDirectory(dir=ROOT, prefix="evaluation-test-") as folder:
            audit = Path(folder) / "audit.jsonl"
            sent, profiles = [], []
            def handle(request):
                body = json.loads(request.content)
                if request.url.path == "/api/login":
                    profiles.append(body["username"])
                    return httpx.Response(200, json=dict(profile="Analista"), headers={"set-cookie": "banco_demo_session=SESSION_SECRET; Path=/"})
                if request.url.path == "/api/conversations":
                    return httpx.Response(200, json=dict(conversation_id="a" * 32))
                if request.url.path == "/api/chat":
                    self.assertEqual(set(body), {"question", "conversation_id"})
                    sent.append(body["question"])
                    identifier = f"trace-{len(sent)}"
                    record = dict(question=body["question"], profile="Analista", langfuse_trace_id=identifier,
                                  trace=[dict(event="agent_started", agent="orquestador")])
                    with audit.open("a", encoding="utf-8") as file:
                        file.write(json.dumps(record) + "\n")
                    return httpx.Response(200, json=dict(answer="respuesta", diagnostics=dict(trace_id=identifier)))
                return httpx.Response(200, json=dict(ok=True))
            api = LocalAPI("http://localhost", audit, client_factory=lambda: httpx.Client(transport=httpx.MockTransport(handle), base_url="http://localhost"))
            row = dict(CASES["BA-002"].row, responde_correcto="REFERENCIA_NO_ENVIAR")
            result = api.run(Case(row))
            self.assertEqual(sent, [*CASES["BA-002"].previous, "y en mayo?"])
            self.assertEqual(result["correlation"]["trace_id"], "trace-2")
            self.assertNotIn("REFERENCIA_NO_ENVIAR", json.dumps(sent))
            self.assertNotIn("SESSION_SECRET", json.dumps(result))
            self.assertNotIn("AnalistaDemo!2026", json.dumps(result))
            api.run(CASES["BA-006"])
            self.assertEqual(profiles, ["analista", "analista"])

    def test_failed_history_does_not_send_final_question(self):
        requests = []
        def handle(request):
            requests.append(request.url.path)
            if request.url.path == "/api/login":
                return httpx.Response(200, json=dict(profile="Analista"))
            if request.url.path == "/api/conversations":
                return httpx.Response(200, json=dict(conversation_id="a" * 32))
            return httpx.Response(503 if request.url.path == "/api/chat" else 200, json={})
        api = LocalAPI("http://localhost", ROOT / "no-existe-evaluation-audit", client_factory=lambda: httpx.Client(transport=httpx.MockTransport(handle), base_url="http://localhost"))
        result = api.run(CASES["BA-002"])
        self.assertEqual(result["status"], "historial_incompleto")
        self.assertIsNone(result["final"])
        self.assertEqual(requests.count("/api/chat"), 1)
        self.assertIn("/api/logout", requests)

    def test_secrets_and_credential_keys_are_excluded(self):
        raw = dict(password="p", headers={"authorization": "Bearer secret"}, nested=dict(api_key="lsv2_secret", cookie="c", answer="clave-privada"))
        self.assertEqual(sanitize(raw, ["clave-privada"]), dict(nested=dict(answer="[REDACTADO]")))

    def test_unreadable_trace_preserves_visible_response_and_marks_unverifiable(self):
        def handle(request):
            if request.url.path == "/api/login":
                return httpx.Response(200, json=dict(profile="Analista"))
            if request.url.path == "/api/conversations":
                return httpx.Response(200, json=dict(conversation_id="a" * 32))
            return httpx.Response(200, json=dict(answer="Solo atiendo temas del banco.", diagnostics=dict(trace_id="id")))
        api = LocalAPI("http://localhost", ROOT / "no-existe-evaluation-audit", client_factory=lambda: httpx.Client(transport=httpx.MockTransport(handle), base_url="http://localhost"))
        with patch("banco_agentes.evaluation.transport.appended_records", side_effect=PermissionError):
            result = api.run(CASES["BA-006"])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["final"]["response"]["answer"], "Solo atiendo temas del banco.")
        self.assertEqual(result["correlation"]["status"], "no_verificable")

    def test_remote_api_urls_are_rejected_before_login(self):
        for url in ("https://example.com", "http://localhost@evil.com", "http://localhost/?key=x", "http://u:p@localhost"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                LocalAPI(url, "audit")


class GraderTests(unittest.TestCase):
    def test_correct_may_pair_scores_numerical_and_dates_without_claiming_full_hallucination_check(self):
        scores = grade(CASES["BA-002"], pair_output())
        for key in CRITERIA[:-1]:
            self.assertEqual(scores[key]["score"], 1, (key, scores[key]))
        self.assertEqual(scores["alucinacion"]["status"], "no_verificable")

    def test_wrong_visible_value_and_wrong_month_are_detected_separately(self):
        output = pair_output()
        output["final"]["response"]["charts"][0]["points"][0]["y"] = "999.99"
        output["final"]["response"]["answer"] += " El saldo fue 999,99."
        self.assertEqual(grade(CASES["BA-002"], output)["exactitud_numerica"]["score"], 0)
        self.assertEqual(grade(CASES["BA-002"], output)["alucinacion"]["score"], 1)
        output = pair_output()
        output["final"]["response"]["charts"][0]["period"] = "2026-06"
        self.assertEqual(grade(CASES["BA-002"], output)["manejo_de_fechas"]["score"], 0)

    def test_sql_in_trace_without_visible_values_is_not_success(self):
        output = pair_output()
        output["final"]["response"].update(answer="Resultado obtenido.", charts=[])
        self.assertEqual(grade(CASES["BA-002"], output)["exactitud_numerica"]["score"], 0)

    def test_missing_or_ambiguous_trace_is_not_a_pass(self):
        output = pair_output()
        output["correlation"] = dict(status="no_verificable", matches=2)
        self.assertTrue(all(v["score"] is None for v in grade(CASES["BA-002"], output).values()))

    def test_missing_sql_is_not_a_numerical_failure_or_success(self):
        output = pair_output()
        output["sql_reference"] = dict(status="no_verificable")
        self.assertEqual(grade(CASES["BA-002"], output)["exactitud_numerica"]["status"], "no_verificable")

    def test_scope_rejection_has_no_numeric_or_date_grade(self):
        output = dict(status="ok", correlation=dict(status="ok"), trace=[dict(event="agent_started", agent="orquestador")],
                      final=dict(response=dict(answer="Lo siento, solo puedo atender temas relacionados con el banco, sus productos, datos y documentos.")))
        scores = grade(CASES["BA-006"], output)
        self.assertEqual(scores["enrutamiento_correcto"]["score"], 1)
        self.assertEqual(scores["responde_correcto"]["score"], 1)
        self.assertEqual(scores["exactitud_numerica"]["status"], "no_aplica")
        self.assertEqual(scores["manejo_de_fechas"]["status"], "no_aplica")
        self.assertEqual(scores["alucinacion"]["score"], 0)
        output["final"]["response"]["answer"] += " Hoy está soleado y hace 25 grados."
        self.assertNotEqual(grade(CASES["BA-006"], output)["responde_correcto"]["score"], 1)

    def test_growth_comparison_cannot_be_replaced_by_comparing_closing_balances(self):
        output = pair_output()
        scores = grade(CASES["BA-012"], output)
        self.assertEqual(scores["responde_la_pregunta"]["score"], 0)
        self.assertEqual(scores["responde_correcto"]["score"], 0)

    def test_growth_compares_both_products_on_same_basis_and_demands_declared_method(self):
        output = pair_output()
        facts = [dict(product=p, month=m, metric="total", value=v) for p, m, v in
                 (("AHORRO", 202602, "100.00"), ("AHORRO", 202606, "110.00"),
                  ("TDC", 202602, "200.00"), ("TDC", 202606, "240.00"))]
        old_plan = next(e for e in output["trace"] if e["event"] == "validated_plan")
        output["trace"] = [e for e in output["trace"] if e["event"] != "validated_plan" and e.get("agent") != "data_graph"]
        for i, f in enumerate(facts):
            event = deepcopy(old_plan)
            event.update(id=f"R{i+1}", rows=[dict(valor=f["value"])])
            event["plan"].update(producto=f["product"], desde=f"2026-{f['month']%100:02d}", hasta=f"2026-{f['month']%100:02d}")
            output["trace"].append(event)
        output["sql_reference"] = dict(status="ok", facts=facts, derived={"AHORRO":dict(absolute="10.00",percent="10.00"),"TDC":dict(absolute="40.00",percent="20.00")})
        output["final"]["response"].update(charts=[], presentation=dict(result_ids=["R1","R2","R3","R4"]),
            answer="El saldo total de AHORRO al cierre de junio de 2026 es 110,00. El crecimiento absoluto de TDC entre febrero y junio de 2026 fue 40,00. El crecimiento absoluto de AHORRO entre febrero y junio de 2026 fue 10,00. TDC tuvo el mayor crecimiento absoluto.")
        scores = grade(CASES["BA-012"], output)
        for key in CRITERIA[:-1]:
            self.assertEqual(scores[key]["score"], 1, (key,scores[key]))
        output["final"]["response"]["answer"] = "El saldo de AHORRO al cierre de junio de 2026 es 110,00. TDC creció 40,00 y AHORRO creció 10,00; TDC creció más."
        self.assertEqual(grade(CASES["BA-012"], output)["responde_la_pregunta"]["score"], 0)

    def test_correct_chart_does_not_excuse_a_wrong_amount_in_the_conclusion(self):
        output = pair_output()
        output["final"]["response"]["answer"] = output["final"]["response"]["answer"].replace("con 100,10", "con 75,05")
        self.assertEqual(grade(CASES["BA-002"], output)["exactitud_numerica"]["score"], 0)

    def test_independent_sql_is_parameterized_and_readonly(self):
        db = Mock()
        db.__enter__ = Mock(return_value=db)
        db.__exit__ = Mock(return_value=False)
        db.execute.side_effect = [SimpleNamespace(fetchone=lambda: ("banco_lectura", "on")),
                                 SimpleNamespace(fetchall=lambda: [("CHEQUES", 202605, "100.10", 2, 2), ("AHORRO", 202605, "75.05", 2, 2)])]
        result = sql_reference(CASES["BA-002"], connector=lambda: db)
        query, params = db.execute.call_args.args
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["derived"]["difference"], "25.05")
        self.assertIn("%s", query)
        self.assertNotIn("75.05", query)
        self.assertEqual(params[1], [20260531])
        self.assertEqual(params[0], ["CHEQUES", "AHORRO"])

    def test_client_reference_does_not_execute_global_sql(self):
        connector = Mock()
        case = Case(dict(CASES["BA-002"].row, perfil="Cliente"))
        self.assertEqual(sql_reference(case, connector)["status"], "no_verificable")
        connector.assert_not_called()
