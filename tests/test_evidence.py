import unittest
from banco_agentes.catalog import load_catalog, search_catalog
from banco_agentes.evidence import compact_evidence


class EvidenceTests(unittest.TestCase):
    def test_structural_view_keeps_all_columns_and_source_lines(self):
        original = search_catalog(load_catalog(), "Describe columnas y clave primaria de dim.dim_cliente")
        compact = compact_evidence(original)
        self.assertEqual(len(compact["tables"][0]["columns"]), 12)
        for before, after in zip(original["tables"][0]["columns"], compact["tables"][0]["columns"]):
            self.assertEqual(before["declaration"], after["declaration"])
            self.assertEqual(before["source"]["line"], after["line"])
        self.assertEqual(original["tables"][0]["source"]["file"], compact["tables"][0]["source"]["file"])
        self.assertEqual(compact["documentation"], [])

    def test_business_question_keeps_documented_monthly_semantics(self):
        original = search_catalog(load_catalog(), "Explica saldo_apertura")
        compact = compact_evidence(original)
        self.assertEqual(original["documentation"], compact["documentation"])
