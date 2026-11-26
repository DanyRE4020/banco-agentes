"""Regresiones generales: formatos, precisión y dependencias por requisito. Sin red."""
import copy
import hashlib
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from banco_agentes.documents import DocumentStore, validate_document, ROOT
from banco_agentes.document_credit import compare_requirements
from banco_agentes.document_privacy import public_attachment_data
from banco_agentes.document_evidence import name_relation
from web_fixtures import create_app
from scripts.generar_fixtures_evidencia import samples, field
from test_documents import extraction, policy, fixture_decision

TODAY = date(2026, 11, 17)
FIXTURES = ROOT/"fixtures/documents/evidencia"


def document(raw, number=1):
    result = validate_document(raw, {"mime": "application/pdf", "name": f"ejemplo_{number}.pdf", "file_id": f"file{number}", "pages": 1})
    result["id"] = f"D{number}"
    return result


def rules():
    result = policy("clasica")
    for rule in result["reglas"]:
        if rule["campo"] == "antiguedad_comprobante_meses":
            rule["tipos_comprobante_admitidos"] = ["electricidad", "agua", "gas"]
    return result


def row(result, name):
    return next(r for r in result["requisitos"] if r["regla"] == name)


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.raw = samples()

    def evaluate(self, *raws, policy_value=None):
        return compare_requirements([document(raw, i+1) for i, raw in enumerate(raws)], policy_value or rules(), today=TODAY)

    def test_receipt_fields_survive_classification_and_privacy_without_becoming_income(self):
        receipt = document(self.raw["comprobante_municipal.pdf"])
        self.assertEqual(receipt["campos"]["fecha_emision"]["valor"], "2026-10")
        self.assertEqual(receipt["campos"]["fecha_emision"]["precision"], "mes")
        self.assertEqual(receipt["campos"]["importe_total"]["valor"], "230.00")
        result = compare_requirements([receipt], rules(), today=TODAY)
        self.assertEqual(row(result, "antiguedad_comprobante_meses")["estado"], "cumple")
        self.assertEqual(row(result, "ingreso_mensual")["estado"], "no verificable")
        self.assertEqual(row(result, "ingreso_mensual")["evidencia"], [])
        public_attachment_data({"documents": [receipt], "derived": result}, "¿Cumplo?")
        self.assertTrue(receipt["campos"]["domicilio"]["valor"])
        self.assertEqual(compare_requirements([receipt], rules(), today=TODAY), result)
        generic = copy.deepcopy(self.raw["comprobante_municipal.pdf"])
        generic["tipo"] = "documento"
        kept = document(generic)
        self.assertEqual(kept["campos"]["fecha_emision"]["valor"], "2026-10")
        self.assertEqual(row(compare_requirements([kept], rules(), today=TODAY), "antiguedad_comprobante_meses")["motivo"], "tipo_documental_no_acreditado")

    def test_names_reordered_accents_and_different_holders_affect_only_relationships(self):
        identity = self.raw["registro_personas.pdf"]
        receipt = self.raw["comprobante_municipal.pdf"]
        good = self.evaluate(identity, receipt)
        bad = self.evaluate(identity, self.raw["pago_servicio.png"])
        self.assertEqual(row(good, "antiguedad_comprobante_meses")["estado"], "cumple")
        self.assertEqual(row(bad, "antiguedad_comprobante_meses")["motivo"], "titulares_distintos")
        self.assertEqual(row(bad, "antiguedad_comprobante_meses")["vinculos"][0]["documentos"], ["D2", "D1"])
        for name in ("edad_anios", "vigencia_identificacion"):
            self.assertEqual(row(good, name)["estado"], row(bad, name)["estado"])
            self.assertEqual({e["documento_id"] for e in row(bad, name)["evidencia"]}, {"D1"})
        self.assertEqual(name_relation("Rojas Torres, Lucía Marina", "LUCIA MARINA ROJAS TORRES"), "coincidente")
        self.assertEqual(name_relation("Lucía Rojas Torres", "Lucía Marina Rojas Torres"), "parcial")

    def test_income_owner_mismatch_does_not_invalidate_identity_or_use_receipt_amount(self):
        identity = copy.deepcopy(self.raw["registro_personas.pdf"])
        statement = extraction()
        statement["campos"]["titular"] = field("Rojas Torres Lucia Marina")
        good = self.evaluate(identity, statement)
        statement["campos"]["titular"] = field("Persona Totalmente Distinta")
        bad = self.evaluate(identity, statement)
        self.assertEqual(row(good, "ingreso_mensual")["estado"], "cumple")
        self.assertEqual(row(bad, "ingreso_mensual")["motivo"], "titulares_distintos")
        self.assertEqual(row(bad, "vigencia_identificacion")["estado"], "cumple")
        self.assertEqual(row(bad, "edad_anios")["estado"], "cumple")

    def test_partial_expiry_dates_do_not_invent_days(self):
        for value, expected in (("2032", "cumple"), ("2020", "no cumple"), ("2026", "pendiente de confirmar"),
                                ("2026-10", "no cumple"), ("2026-11", "pendiente de confirmar"), ("2026-11-17", "cumple")):
            with self.subTest(value=value):
                identity = copy.deepcopy(self.raw["registro_personas.pdf"])
                identity["campos"]["fecha_vencimiento"] = field(value)
                parsed = document(identity)
                self.assertEqual(parsed["campos"]["fecha_vencimiento"]["valor"], value)
                result = compare_requirements([parsed], rules(), today=TODAY)
                expiry = row(result, "vigencia_identificacion")
                self.assertEqual(expiry["estado"], expected)
                if expected == "pendiente de confirmar":
                    self.assertEqual([c["campo"] for c in expiry["confirmar"]], ["fecha_vencimiento"])

    def test_absent_unreadable_and_read_but_not_sufficient_are_distinct(self):
        for reading, expected, motive in (("ausente", "no verificable", "dato_ausente"), ("ilegible", "pendiente de confirmar", "ilegible")):
            receipt = copy.deepcopy(self.raw["comprobante_municipal.pdf"])
            receipt["campos"]["fecha_emision"] = field(None, reading=reading)
            result = self.evaluate(receipt)
            self.assertEqual(row(result, "antiguedad_comprobante_meses")["estado"], expected)
            self.assertEqual(row(result, "antiguedad_comprobante_meses")["motivo"], motive)
        receipt = copy.deepcopy(self.raw["comprobante_municipal.pdf"])
        receipt["campos"]["tipo_comprobante"] = field("otro")
        self.assertEqual(row(self.evaluate(receipt), "tipo_servicio_comprobante")["motivo"], "servicio_no_identificado")
        self.assertEqual(row(self.evaluate(receipt), "antiguedad_comprobante_meses")["estado"], "cumple")
        receipt["campos"]["fecha_emision"] = field("2026-13-99")
        self.assertEqual(row(self.evaluate(receipt), "antiguedad_comprobante_meses")["motivo"], "no_interpretable")
        statement = extraction()
        statement["campos"]["moneda"] = field("MONEDA NO RECONOCIDA")
        self.assertEqual(row(self.evaluate(statement), "ingreso_mensual")["motivo"], "no_interpretable")

    def test_issue_date_used_instead_of_due_date_partial_precision_and_future(self):
        receipt = copy.deepcopy(self.raw["comprobante_municipal.pdf"])
        for issue, due, expected in (("2026-10", "2020-01-01", "cumple"), ("2026-01", "2032-01-01", "no cumple"),
                                      ("2026-08", "2032-01-01", "pendiente de confirmar"), ("2026-12", "2032-01-01", "no verificable")):
            receipt["campos"].update(fecha_emision=field(issue), fecha_vencimiento=field(due))
            result = row(self.evaluate(receipt), "antiguedad_comprobante_meses")
            self.assertEqual(result["estado"], expected)
            self.assertNotIn("fecha_vencimiento", {e["campo"] for e in result["evidencia"]})

    def test_partial_birth_boundary_and_public_view_never_mutate_private_evaluation(self):
        identity = copy.deepcopy(self.raw["registro_personas.pdf"])
        identity["campos"]["fecha_nacimiento"] = field("2008")
        parsed = document(identity)
        result = compare_requirements([parsed], rules(), today=TODAY)
        self.assertEqual(row(result, "edad_anios")["estado"], "pendiente de confirmar")
        before = copy.deepcopy(parsed)
        public_attachment_data({"documents": [parsed], "derived": result}, "¿Cumplo los requisitos?")
        self.assertEqual(parsed, before)
        self.assertEqual(compare_requirements([parsed], rules(), today=TODAY), result)

    def test_changing_issuer_or_filename_has_no_effect_and_conditional_rule_uses_only_needed_fields(self):
        identity = copy.deepcopy(self.raw["registro_personas.pdf"])
        receipt = copy.deepcopy(self.raw["comprobante_municipal.pdf"])
        original = self.evaluate(identity, receipt)
        receipt["campos"]["emisor"] = field("Emisor completamente distinto")
        self.assertEqual([r["estado"] for r in original["requisitos"]], [r["estado"] for r in self.evaluate(identity, receipt)["requisitos"]])
        conditional = rules()
        rule = next(r for r in conditional["reglas"] if r["campo"] == "antiguedad_comprobante_meses")
        rule.update(condicion="domicilio distinto de la identificación", comparar_domicilio_identificacion=True)
        receipt["campos"].pop("fecha_emision")
        result = row(self.evaluate(identity, receipt, policy_value=conditional), "antiguedad_comprobante_meses")
        self.assertEqual(result["motivo"], "no_aplica")
        self.assertNotIn("fecha_emision", {f["campo"] for f in result["evidencia"]})


class MatrixReader:
    def __init__(self):
        self.calls = 0
        self.expected = {hashlib.sha256((FIXTURES/name).read_bytes()).hexdigest(): raw for name, raw in samples().items()}

    async def read(self, data, mime):
        self.calls += 1
        return copy.deepcopy(self.expected[hashlib.sha256(data).hexdigest()]), {}


class PolicyEvidenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_receipt_types_come_from_retrieved_rule_not_issuer_or_guessed_defaults(self):
        from banco_agentes.steward_rag import document_policy
        source = copy.deepcopy(policy()["reglas"][0]["fuente"])
        source.update(section="2. Requisitos", requirements=[{"campo": "antiguedad_comprobante_meses", "operador": "<=", "valor": 3}])
        hit = {"id": "fragmento-requisitos", "source": source,
               "text": "Comprobante de domicilio: agua, energía eléctrica y gas. Identificación oficial vigente."}
        with patch("banco_agentes.steward_rag.Retriever.search", return_value={"hits": [hit]}):
            result = await document_policy("clasica")
        rule = next(r for r in result["reglas"] if r["campo"] == "antiguedad_comprobante_meses")
        self.assertEqual(set(rule["tipos_comprobante_admitidos"]), {"electricidad", "agua", "gas"})
        self.assertEqual(rule["fuente"]["fragment_id"], "fragmento-requisitos")
        source["requirements"][0]["condicion"] = "domicilio distinto de la identificación"
        hit["text"] = "Comprobante cuando los domicilios no coinciden."
        with patch("banco_agentes.steward_rag.Retriever.search", return_value={"hits": [hit]}):
            result = await document_policy("clasica")
        rule = result["reglas"][0]
        self.assertTrue(rule["comparar_domicilio_identificacion"])
        self.assertNotIn("tipos_comprobante_admitidos", rule)


class FormatFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.reader = MatrixReader()
        self.store = DocumentStore(Path(self.temp.name), reader=self.reader)
        self.app = create_app(document_store=self.store, audit_path=None)
        clock = patch("banco_agentes.document_credit.date", wraps=date)
        clock.start().today.return_value = TODAY
        self.addCleanup(clock.stop)
        for target, value in (("banco_agentes.attachment_hub.decide_attachment", AsyncMock(side_effect=fixture_decision)),
                              ("banco_agentes.steward_rag.document_policy", AsyncMock(side_effect=lambda _: rules()))):
            mocked = patch(target, new=value)
            mocked.start()
            self.addCleanup(mocked.stop)

    def run_flow(self, profile, names, question):
        client = TestClient(self.app, base_url="http://127.0.0.1:8000")
        client.post("/api/login", json={"username": profile, "password": "ClienteDemo!2026" if profile == "cliente" else "AnalistaDemo!2026"})
        cid = client.post("/api/conversations").json()["conversation_id"]
        upload = client.post("/api/documents", data={"conversation_id": cid}, files=[("files", ("archivo_renombrado_"+str(i), (FIXTURES/n).read_bytes(), "text/plain")) for i,n in enumerate(names)])
        self.assertEqual(upload.status_code, 200, upload.text)
        ids = [d["file_id"] for d in upload.json()["documents"]]
        response = client.post("/api/chat", json={"conversation_id": cid, "document_ids": ids, "question": question})
        self.assertEqual(response.status_code, 200, response.text)
        self.current = client, cid, ids
        return response.json()

    def test_partial_date_confirmation_preserves_original_and_links_user_evidence(self):
        result = self.run_flow("cliente", ["registro_personas.pdf", "documento_servicio.jpg"], "¿Qué documentos necesito para solicitar Clásica?")
        self.assertEqual([c["campo"] for c in result["attachment_view"]["confirmations"]], [])
        client, cid, ids = self.current
        correction = client.post(f"/api/documents/{ids[1]}/corrections", json={"conversation_id": cid, "campo": "fecha_emision", "valor": "2026-08-31"})
        self.assertEqual(correction.status_code, 200, correction.text)
        self.assertEqual(self.store.records[ids[1]]["result"]["campos"]["fecha_emision"]["valor"], "2026-08")
        updated = client.post("/api/chat", json={"conversation_id": cid, "document_ids": ids, "question": "¿Qué documentos necesito para solicitar Clásica?"}).json()
        proof = next(r for r in updated["derived"]["reconocimientos"] if r["categoria"] == "domicilio")
        self.assertEqual(proof["estado"], "aportada")
        source = next(e["fuente"] for e in proof["evidencia"] if e["campo"] == "fecha_emision")
        self.assertEqual(source["file_id"], ids[1])
        self.assertNotIn("tipo",source)  # Se reconoce lo impreso, no la corrección.
        self.assertEqual(updated["attachment_view"]["confirmations"], [])

    def test_pdf_png_jpg_and_several_issuers_for_both_profiles(self):
        for profile in ("cliente", "analista"):
            for names in (("registro_personas.pdf", "comprobante_municipal.pdf"), ("tarjeta_vecinal.png", "documento_servicio.jpg"), ("ficha_registro.jpg", "pago_servicio.png")):
                with self.subTest(profile=profile, names=names):
                    result = self.run_flow(profile, names, "¿Qué documentos necesito para solicitar Clásica?")
                    self.assertEqual([d["id"] for d in result["documents"]], ["D1", "D2"])
                    self.assertEqual(next(r for r in result["derived"]["categorias"] if r["categoria"] == "identificacion")["status"], "pendiente")  # Estas fichas no son INE.
                    self.assertTrue(all(r["reason"] for r in result["derived"]["categorias"]))
                    self.assertNotIn("D1", result["answer"])
                    self.assertNotIn("R1", result["answer"])

    def test_reading_a_field_is_not_evaluation_and_unreadable_file_does_not_block_identity(self):
        for profile in ("cliente", "analista"):
            read = self.run_flow(profile, ["comprobante_municipal.pdf"], "¿Cuál es la fecha de emisión del comprobante?")
            self.assertIn("2026-10", read["answer"])
            self.assertIsNone(read["derived"])
            self.assertEqual(read["attachment_view"]["confirmations"], [])
            result = self.run_flow(profile, ["archivo_vacio.pdf", "registro_personas.pdf"], "¿Qué documentos necesito para solicitar Clásica?")
            self.assertEqual(len(result["documents"]), 1)
            self.assertEqual(result["document_errors"][0]["tipo"], "ilegible")
            self.assertEqual(next(r for r in result["derived"]["categorias"] if r["categoria"] == "identificacion")["status"], "pendiente")  # Estas fichas no son INE.
            self.assertTrue(result["derived"]["lecturas_no_disponibles"])
