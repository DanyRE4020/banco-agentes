"""Presentación y privacidad con ambos perfiles. Todos los documentos/modelos son dobles sintéticos."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from banco_agentes.attachment_hub import AttachmentDecision, evidence_items
from banco_agentes.document_privacy import safe_free_text, requested_fields
from banco_agentes.documents import DocumentStore
from web_fixtures import create_app
from test_documents import FIXTURES, FixtureReader, fixture_decision, policy

CURP = "GODE561231HDFRRN09"
ELECTOR = "ABCDEF12345678H123"
ADDRESS = "Calle Ficticia 123, Ciudad de Prueba, CP 00000"
PRIVATE = f"CURP: {CURP}\nClave de elector: {ELECTOR}\nDomicilio: {ADDRESS}\nCorreo: prueba@example.invalid\nTeléfono: 5512345678"


class PrivateReader(FixtureReader):
    async def read(self, data, mime):
        raw, metrics = await super().read(data, mime)
        raw["tema"] = "Documento de Alex Prueba Ejemplo. " + CURP
        raw["evidencia"] = [{"texto": "Cancelación sin comisión.\n" + PRIVATE,
                              "pagina": 1 if mime == "application/pdf" else None, "confianza": .99}]
        return raw, metrics


class SimpleAttachmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.reader = PrivateReader()
        self.store = DocumentStore(Path(self.temp.name)/"uploads", reader=self.reader)
        self.audit = Path(self.temp.name)/"audit.jsonl"
        self.metrics = Path(self.temp.name)/"metrics.jsonl"
        self.app = create_app(document_store=self.store, audit_path=self.audit, metrics_path=self.metrics)
        for target, value in (("banco_agentes.attachment_hub.decide_attachment", AsyncMock(side_effect=fixture_decision)),
                              ("banco_agentes.steward_rag.document_policy", AsyncMock(side_effect=policy))):
            p = patch(target, new=value)
            p.start()
            self.addCleanup(p.stop)

    def session(self, profile, files):
        client = TestClient(self.app, base_url="http://127.0.0.1:8000")
        password = "ClienteDemo!2026" if profile == "cliente" else "AnalistaDemo!2026"
        self.assertEqual(client.post("/api/login", json={"username": profile, "password": password}).status_code, 200)
        cid = client.post("/api/conversations").json()["conversation_id"]
        response = client.post("/api/documents", data={"conversation_id": cid},
            files=[("files", (name, (FIXTURES/name).read_bytes())) for name in files])
        self.assertEqual(response.status_code, 200, response.text)
        return client, cid, [d["file_id"] for d in response.json()["documents"]]

    def ask(self, client, cid, ids, question):
        response = client.post("/api/chat", json={"conversation_id": cid, "document_ids": ids, "question": question})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_both_profiles_requirements_have_status_reason_no_unneeded_personal_data(self):
        for profile in ("cliente", "analista"):
            with self.subTest(profile=profile):
                client, cid, ids = self.session(profile, ["estado_cuenta.pdf", "identificacion_vigente.png"])
                result = self.ask(client, cid, ids, "¿Qué documentos necesito para solicitar Clásica?")
                view = result["attachment_view"]
                self.assertTrue(view["policy_requirements"])
                self.assertTrue(all(r["text"] for r in view["policy_requirements"]))
                self.assertEqual(view["confirmations"], [])
                self.assertNotIn("sintétic", result["answer"])
                visible = json.dumps({"answer": result["answer"], "view": view}, ensure_ascii=False)
                for forbidden in ("D1", "D2", "R1", "R2", "confianza", "Telemetría", CURP, ELECTOR, ADDRESS, "Alex Prueba Ejemplo"):
                    self.assertNotIn(forbidden, visible)
                # La API tampoco manda nombre, nacimiento o domicilio que no necesita esta consulta.
                self.assertIsNone(result["documents"][0]["campos"]["domicilio"]["valor"])
                self.assertIsNone(result["documents"][1]["campos"]["fecha_nacimiento"]["valor"])
                record = self.store.records[ids[0]]["result"]
                self.assertTrue(record["campos"]["domicilio"]["valor"])  # evidencia privada autorizada conservada
                free = record["tema"] + " ".join(e["texto"] for e in record["evidencia"])
                for forbidden in (CURP, ELECTOR, ADDRESS, "prueba@example.invalid", "5512345678", "Alex Prueba Ejemplo"):
                    self.assertNotIn(forbidden, free)
        logs = self.audit.read_text(encoding="utf8") + self.metrics.read_text(encoding="utf8")
        for forbidden in (CURP, ELECTOR, ADDRESS, "prueba@example.invalid", "Alex Prueba Ejemplo"):
            self.assertNotIn(forbidden, logs)

    def test_both_profiles_only_confirm_needed_field_and_answer_explicit_field(self):
        for profile in ("cliente", "analista"):
            with self.subTest(profile=profile):
                client, cid, ids = self.session(profile, ["estado_cuenta.pdf", "identificacion_ilegible.png"])
                result = self.ask(client, cid, ids, "¿Cuál es el saldo final del estado de cuenta?")
                self.assertIn("7000.00", result["answer"])
                self.assertNotIn("titular", result["answer"])
                self.assertEqual(result["attachment_view"]["confirmations"], [])
                self.assertIn("estado_cuenta.pdf, página 1", result["answer"])
                result = self.ask(client, cid, ids, "¿Cuál es la fecha de vencimiento de mi identificación?")
                self.assertEqual([f["campo"] for f in result["attachment_view"]["confirmations"]], ["fecha_vencimiento"])
                self.assertNotIn("confianza", result["answer"])
                correction = client.post(f"/api/documents/{ids[1]}/corrections", json={"conversation_id": cid,
                    "campo": "fecha_vencimiento", "valor": "2030-12-31"})
                self.assertEqual(correction.status_code, 200)
                result = self.ask(client, cid, ids, "¿Cuándo vence la identificación?")
                self.assertIn("2030-12-31", result["answer"])
                self.assertEqual(result["attachment_view"]["confirmations"], [])

    def test_both_profiles_explicit_name_is_available_without_other_personal_fields(self):
        for profile in ("cliente", "analista"):
            client, cid, ids = self.session(profile, ["identificacion_vigente.png"])
            result = self.ask(client, cid, ids, "¿Cuál es el nombre en la identificación?")
            self.assertIn("Alex Prueba Ejemplo", result["answer"])
            self.assertNotIn("1990-05-20", result["answer"])
            self.assertIsNone(result["documents"][0]["campos"]["fecha_nacimiento"]["valor"])

    def test_free_text_response_redacts_personal_lines_without_losing_banking_clause(self):
        client, cid, ids = self.session("cliente", ["estado_cuenta.pdf"])
        decision = AttachmentDecision(alcance="bancario", accion="responder", evidencia_ids=["D1.E1"])
        with patch("banco_agentes.attachment_hub.decide_attachment", new=AsyncMock(return_value=decision)):
            result = self.ask(client, cid, ids, "¿Qué dice sobre cancelación de la cuenta bancaria?")
        self.assertIn("Cancelación sin comisión", result["answer"])
        self.assertNotIn("CURP", result["answer"])
        self.assertNotIn("elector", result["answer"])
        self.assertNotIn("D1", result["answer"])
        self.assertFalse(any(key.endswith(".fecha_nacimiento") or key.endswith(".titular") for key in evidence_items(result["documents"])))

    def test_unlabelled_identifiers_addresses_and_same_line_financial_values(self):
        text = f"{CURP}\n{ELECTOR}\nRFC: GODE561231GR8\nCalle Alternativa 987, CP 00000\nSaldo final: 7000.00"
        result = safe_free_text(text)
        for value in (CURP, ELECTOR, "GODE561231GR8", "Alternativa 987"):
            self.assertNotIn(value, result)
        self.assertIn("7000.00", result)
        self.assertIn("7000.00", safe_free_text("Domicilio: Calle Alternativa 987 Saldo final: 7000.00"))
        self.assertEqual(safe_free_text("Banamex", known_values=[("nombre", "Ana")]), "Banamex")
        self.assertNotIn("nombre", requested_fields("¿Cuál es el nombre del archivo?"))

    def test_explicit_birth_and_address_are_returned_only_when_requested_and_never_logged(self):
        for profile in ("cliente", "analista"):
            client, cid, ids = self.session(profile, ["estado_cuenta.pdf", "identificacion_vigente.png"])
            result = self.ask(client, cid, ids, "¿Quién es el titular del estado de cuenta?")
            self.assertIn("Alex Prueba Ejemplo", result["answer"])
            result = self.ask(client, cid, ids, "¿Cuál es la fecha de nacimiento en la identificación?")
            self.assertIn("1990-05-20", result["answer"])
            self.assertNotIn("Alex Prueba", result["answer"])
            result = self.ask(client, cid, ids, "¿Cuál es el domicilio en el estado de cuenta?")
            self.assertIn("Calle Ficticia 123", result["answer"])
            self.assertNotIn("1990-05-20", result["answer"])
        logs = self.audit.read_text(encoding="utf8") + self.metrics.read_text(encoding="utf8")
        self.assertNotIn("1990-05-20", logs)
        self.assertNotIn("Calle Ficticia 123", logs)
