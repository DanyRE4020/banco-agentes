import unittest
from types import SimpleNamespace

from banco_agentes.conversation import presentation_fields, render_turn
from banco_agentes.governance import REQUEST, SESSION, DemoSession
from banco_agentes.general_agents import plan_options
from banco_agentes.presentation import decimal_text, result_text, result_table, source_entry


def result(metric="total", **changes):
    plan = dict(metrica=metric, base="total", conjunto="saldos", producto="CHEQUES",
                persona="", segmento="", codigo=0, agrupar=(), alcance="global",
                desde="2026-03", hasta="2026-03", campo="saldo_mes")
    return dict(status="ok", id="R1", agent="data_analytics", plan={**plan, **changes},
                rows=[dict(valor="51676391.38", filas=5000, observados=5000)],
                columns=["valor", "filas", "observados"], table="cfg.fact_sdos_captacoloca_m",
                sources=["referencias/002_generar_cuentas_y_saldos_enero_2026 (1).sql:30-43"],
                notes=[], rule=None, scope="global", units="Importe sintético, moneda sin definir")


def turn_with(value):
    return SimpleNamespace(results=[value], policy=None, charts=[], memory={}, sources=[],
                           documentation=[], chart_issues=[], trace=SimpleNamespace(records=[]))


class PresentationTests(unittest.TestCase):
    def test_decimal_preserves_precision_beyond_javascript_integer_limit(self):
        self.assertEqual(decimal_text("12345678901234567.8900"), "12.345.678.901.234.567,8900")
        self.assertEqual(decimal_text("-1000.50"), "-1.000,50")
        self.assertEqual(decimal_text("0.00"), "0,00")

    def test_total_has_conclusion_without_technical_fields(self):
        value = result()
        text, _ = render_turn(turn_with(value), "Un texto inventado por el modelo.")
        self.assertTrue(text.startswith("El saldo total de CHEQUES al cierre de marzo de 2026 es 51,676,391.38."))
        for technical in ("R1", "5000", "observados", "cfg.", ".sql", "inventado"):
            self.assertNotIn(technical, text)
        fields = presentation_fields(turn_with(value))
        self.assertIn("R1", fields["query_details"])
        self.assertIn("observados", fields["query_details"])
        self.assertEqual(fields["tables"], [])
        self.assertTrue(any(source["kind"] == "table" for source in fields["sources"]))
        self.assertIn("líneas 30-43", fields["sources"][1]["location"])

    def test_template_reused_for_other_product_month_and_person_filter(self):
        value = result(producto="AHORRO", desde="2026-04", hasta="2026-04", persona="PF")
        value["rows"][0]["valor"] = "24874054.16"
        self.assertEqual(result_text(value),
                         "El saldo total de AHORRO para personas físicas al cierre de abril de 2026 es 24.874.054,16.")

    def test_comparison_states_direction_and_table_has_readable_values(self):
        value = result("variacion", desde="2026-02", persona="PF", agrupar=("mes",))
        value["rows"] = [dict(mes=202602,valor="49111443.21",filas=4800,observados=4800),
                         dict(mes=202603,valor="49602557.74",filas=4800,observados=4800)]
        value["columns"] = ["mes", "valor", "filas", "observados"]
        text = result_text(value)
        self.assertIn("aumentó de 49.111.443,21 a 49.602.557,74", text)
        self.assertIn("491.114,53", text)
        table = result_table(value)
        self.assertEqual([column["key"] for column in table["columns"]], ["mes", "valor"])
        self.assertEqual(table["rows"][0], {"mes":"febrero de 2026", "valor":"49.111.443,21"})
        value["rows"].reverse()
        self.assertEqual(result_text(value), text)

    def test_zero_base_and_insufficient_results_are_honest(self):
        value = result("variacion", desde="2026-02", agrupar=("mes",))
        value["rows"] = [dict(mes=202602,valor="0.00"),dict(mes=202603,valor="10.00")]
        self.assertNotIn("%", result_text(value))
        value["status"]="insufficient"
        self.assertIn("No hay observaciones suficientes", result_text(value))
        self.assertNotIn("es 0", result_text(value))

    def test_denied_client_response_does_not_leak_in_new_presentation_fields(self):
        a, b = SESSION.set(DemoSession("Cliente",1,200000)), REQUEST.set("Saldo de otra cuenta")
        try:
            turn = turn_with(result(alcance="propio"))
            answer, charts = render_turn(turn, "")
            self.assertIn("Acceso bloqueado", answer)
            self.assertEqual(charts, [])
            self.assertEqual(presentation_fields(turn), {"sources":[],"tables":[],"query_details":""})
        finally:
            SESSION.reset(a)
            REQUEST.reset(b)

    def test_sources_keep_exact_file_and_available_lines(self):
        entry=source_entry("referencias/003_generar_transacciones_y_saldos_feb_jun_2026 (1).sql:324-342")
        self.assertIn("Generación de movimientos", entry["label"])
        self.assertIn("líneas 324-342", entry["location"])



class PlanOptionTests(unittest.TestCase):
    def test_repeated_person_filter_is_rejected_instead_of_overwriting_pf(self):
        with self.assertRaisesRegex(ValueError, "más de una vez"):
            plan_options("persona=PF;persona=PM")

    def test_other_duplicates_and_aliases_are_rejected(self):
        for options in ("codigo=100;codigo=200", "tipo=CHEQUES;producto=AHORRO"):
            with self.assertRaisesRegex(ValueError, "más de una vez"):
                plan_options(options)

    def test_existing_filters_and_quality_options_keep_their_meaning(self):
        self.assertEqual(plan_options("persona=PF;agrupar=mes")["persona"], "PF")
        self.assertEqual(plan_options("")["persona"], "")
        quality = plan_options("campo=saldo_mes;minimo=0;maximo=100;regla=referencias", quality=True)
        self.assertEqual((quality["minimo"], quality["maximo"]), (0.0, 100.0))
