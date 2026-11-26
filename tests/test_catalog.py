import tempfile
import unittest
from pathlib import Path

from banco_agentes.catalog import ROOT, build_catalog, load_catalog, search_catalog


class CatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = build_catalog()

    def test_reproducible_and_original_hashes(self):
        self.assertEqual(self.catalog, load_catalog())

    def test_only_actual_tables_and_columns(self):
        self.assertEqual({table["name"]: len(table["columns"]) for table in self.catalog["tables"]}, {
            "dim.dim_cliente": 12, "dim.dim_producto": 6, "dim.dim_txn": 5,
            "cfg.fact_sdos_captacoloca_m": 9, "cfg.fact_txn_captacoloca_d": 10,
            "tmp_calculo_mes": 13})
        self.assertEqual([table["name"] for table in self.catalog["tables"] if table["temporary"]],
                         ["tmp_calculo_mes"])

    def test_existing_table(self):
        result = search_catalog(self.catalog, "Describe la tabla dim.dim_cliente")
        self.assertEqual(result["status"], "found")
        self.assertEqual(result["tables"][0]["columns"][0]["name"], "num_cte")
        self.assertTrue(result["tables"][0]["columns"][0]["primary_key"])

    def test_unknown_column_does_not_become_supported_by_known_table(self):
        for query, field in (("Describe el campo credit_score de dim.dim_cliente", "credit_score"),
                             ("Describe el campo email de dim.dim_cliente", "email"),
                             ("Describe dim.dim_cliente.credit_score", "dim.dim_cliente.credit_score")):
            with self.subTest(query=query):
                result = search_catalog(self.catalog, query)
                self.assertEqual(result["status"], "not_found")
                self.assertIn(field, result["unknown_identifiers"])

    def test_txn_has_no_unique_key_or_foreign_keys(self):
        for table in self.catalog["tables"]:
            self.assertEqual(table["foreign_keys"], [])
            if table["name"] == "dim.dim_txn":
                self.assertFalse(any(column["unique"] for column in table["columns"]))
        self.assertTrue(all(entry["kind"] == "inferred_not_enforced"
                            for entry in self.catalog["interpretations"]))

    def test_all_citations_match_original_text(self):
        entries = []
        for table in self.catalog["tables"]:
            entries.append(table["source"])
            entries.extend(column["source"] for column in table["columns"])
        entries.extend(entry["source"] for entry in self.catalog["documentation"])
        for citation in entries:
            lines = (ROOT / citation["file"]).read_text(encoding="utf-8-sig").splitlines()
            excerpt = "\n".join(lines[citation["line"] - 1:citation["end_line"]])
            self.assertIn(citation["excerpt"], excerpt)

    def test_monthly_semantics_are_evidence_not_guess(self):
        mappings = [entry for entry in self.catalog["documentation"]
                    if entry["kind"] == "explicit_sql_mapping"]
        self.assertEqual(len(mappings), 4)
        result = search_catalog(self.catalog, "saldo_apertura")
        self.assertTrue(any("saldo_anterior" in entry.get("description", "")
                            for entry in result["documentation"]))

    def test_missing_references_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "Faltan referencias SQL"):
                build_catalog(Path(directory))

    def test_numeric_comma_and_check_values_are_preserved(self):
        table = next(table for table in self.catalog["tables"]
                     if table["name"] == "cfg.fact_sdos_captacoloca_m")
        self.assertEqual(next(column["type"] for column in table["columns"]
                              if column["name"] == "saldo_mes"), "NUMERIC(18, 2)")
        client = next(table for table in self.catalog["tables"] if table["name"] == "dim.dim_cliente")
        self.assertIn("CHECK (tipo_persona IN ('PF','PM'))", client["constraints"])


if __name__ == "__main__":
    unittest.main()
