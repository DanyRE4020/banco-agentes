import unittest

from banco_agentes.catalog import ROOT, load_catalog
from banco_agentes.columns import requested_table, column_evidence, format_columns
from banco_agentes.runtime import ask
from tests.test_adk import ScriptedModel


class ColumnTests(unittest.TestCase):
    def test_every_column_matches_original_sql_line(self):
        catalog = load_catalog()
        for table in catalog["tables"]:
            report = format_columns(column_evidence(table))
            sql = (ROOT / table["source"]["file"]).read_text(encoding="utf-8").splitlines()
            for column in table["columns"]:
                self.assertEqual(sql[column["source"]["line"] - 1].strip().rstrip(','), column["declaration"])
                self.assertIn(f"`{column['name']}` — `{column['type']}`", report)
                self.assertIn(f"[{column['source']['file']}:{column['source']['line']}]", report)
            self.assertEqual(report.count("; declaración:"), len(table["columns"]))

    def test_scope_is_one_exact_table_and_columns(self):
        catalog = load_catalog()
        self.assertEqual(requested_table("Hola, ¿qué columnas tiene dim.dim_cliente?", catalog)["name"], "dim.dim_cliente")
        for query in ("columnas de dim.inexistente", "columnas de dim_cliente",
                      "columnas de dim.dim_cliente y dim.dim_producto", "Qué significa dim.dim_cliente"):
            self.assertIsNone(requested_table(query, catalog))


class ColumnADKTests(unittest.IsolatedAsyncioTestCase):
    async def test_agent_tool_returns_exact_catalog_report(self):
        question = "Hola, ¿qué columnas tiene dim.dim_cliente?"
        models = {}
        def factory(name):
            models[name] = ScriptedModel(model="test", role_name=name, question=question)
            return models[name]
        result = await ask(question, emit=lambda _: None, model_factory=factory)
        table = requested_table(question, load_catalog())
        self.assertEqual(result["answer"], format_columns(column_evidence(table)))
        self.assertEqual(models["orquestador"].calls, 1)
        self.assertEqual(models["data_steward"].calls, 1)
        self.assertEqual([(e["agent"], e["tool"]) for e in result["trace"] if e["event"] == "tool_completed"],
                         [("data_steward", "buscar_catalogo"), ("orquestador", "data_steward")])
        evidence = next(e["evidence"] for e in result["trace"] if e["event"] == "catalog_evidence")
        self.assertNotIn("documentation", evidence)
        self.assertNotIn("interpretations", evidence)
