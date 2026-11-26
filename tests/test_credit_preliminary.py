"""Turnos de Cliente con documentos ficticios y fuentes de política recuperadas por Steward."""
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import fitz
from fastapi.testclient import TestClient

from banco_agentes.attachment_hub import AttachmentDecision
from banco_agentes.auth import password_record
from banco_agentes.documents import DocumentStore
from banco_agentes.web import create_app
from test_receipt_details import policy_hit


INVENTORY = "¿Qué documentos tengo y cuáles me faltan para la Tarjeta Clásica?"
ASSESS = "¿Qué requisitos de la Tarjeta Clásica cumplo con estos documentos?"
INE = "INE FICTICIA\nNombre: Persona Prueba Alfa\nFecha de nacimiento: 1990-04-10\nVigencia: 2022 - 2032"
PAYROLL = ("CFDI de nómina\nReceptor: Prueba Alfa Persona\nPeriodicidad: mensual\n"
           "Inicio del periodo: 2026-10-01\nFin del periodo: 2026-10-31\n"
           "Moneda: MXN\nConcepto: Apoyo económico\nNeto a pagar: 12000.00\n"
           "Percepciones brutas: 15000.00\nDeducciones: 3000.00")
RECEIPT = ("Recibo de servicios\nServicio: luz\nTitular: Prueba Alfa Persona\n"
           "Domicilio del servicio: Calle Inventada 48\nFecha de expedición: 2026-11-01\n"
           "Mes facturado: 2026-10\nLímite de pago: 2026-12-01\nCorte: 2026-10-31")


def document(text, *, image=False):
    with fitz.open() as pdf:
        page = pdf.new_page()
        page.insert_text((48, 64), "DOCUMENTO SINTÉTICO SIN VALIDEZ\n" + text, fontsize=13)
        return page.get_pixmap().tobytes("png") if image else pdf.tobytes()


def requirement(result, name):
    return next(r for r in result["derived"]["requisitos"] if r["regla"] == name)


class CreditPreliminaryTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        # El modelo entrega OCR y omite nacimiento/vigencia del esquema. El lector conserva ambos desde el OCR.
        self.read = AsyncMock(return_value=({"tipo": "identificacion", "legible": True,
            "paginas": [{"pagina": 1, "estado": "leida", "texto": INE}]}, {}))
        self.store = DocumentStore(Path(folder.name) / "uploads", reader=SimpleNamespace(read=self.read))
        password = "sintetico-prueba-1234"
        users = {name: {"rol": "Cliente", "num_cte": number, **password_record(password)}
                 for number, name in enumerate(("sintetico_a", "sintetico_b"), 1)}
        self.app = create_app(document_store=self.store, audit_path=None, users=users)
        self.password = password
        self.client, self.cid = self.login("sintetico_a")
        self.retrieve = self.enterContext(patch("banco_agentes.steward_rag.Retriever.search", return_value={"hits": [policy_hit()]}))
        self.enterContext(patch("banco_agentes.document_credit.date", wraps=date)).today.return_value = date(2026, 11, 19)

    def login(self, name):
        client = TestClient(self.app, base_url="http://127.0.0.1:8000")
        self.addCleanup(client.close)
        client.post("/api/login", json={"username": name, "password": self.password}).raise_for_status()
        return client, client.post("/api/conversations").json()["conversation_id"]

    def upload(self, *files):
        response = self.client.post("/api/documents", data={"conversation_id": self.cid},
                                    files=[("files", (name, raw)) for name, raw in files])
        self.assertEqual(response.status_code, 200, response.text)
        return [d["file_id"] for d in response.json()["documents"]]

    def ask(self, question, **kwargs):
        response = self.client.post("/api/chat", json={"conversation_id": self.cid, "question": question, **kwargs})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_verify_classic_requirements_with_two_documents(self):
        ids = self.upload(("ine.pdf", document(INE)), ("nomina.pdf", document(PAYROLL)))
        for question in (
            "¿Qué requisitos de la Tarjeta Clásica puedo verificar con estos documentos y cuáles quedan por verificar?",
            "Con lo que subí, ¿qué requisitos de Clásica puedo acreditar?",
            "¿Cuáles requisitos para la Clásica se pueden evaluar con los documentos adjuntos?",
        ):
            with self.subTest(question=question):
                result = self.ask(question, document_ids=ids)
                self.assertIn("recuperar_politica_documental", result["tools"])
                self.assertEqual(result["attachment_view"]["style"], "preliminary_credit")
                for name in ("edad_anios", "vigencia_identificacion", "ingreso_mensual"):
                    row = requirement(result, name)
                    self.assertEqual(row["estado"], "cumple")
                    self.assertEqual(row["fuente_politica"]["page"], 1)
                    self.assertEqual(row["evidencia"][0]["fuente"]["pagina"], 1)
                intro, meets, missing, pending, closing = result["answer"].split("\n\n")
                self.assertEqual(intro, "La evidencia respalda el requisito de edad, la vigencia de la INE y el monto "
                                 "mínimo de ingreso para la Tarjeta de Crédito Clásica. Falta el comprobante de "
                                 "domicilio. Queda por confirmar la acreditación del ingreso.")
                self.assertTrue(meets.startswith("Qué cumples\n"))
                self.assertIn("36 años", meets)
                self.assertIn("al menos 18 años. [1, 2]", meets)
                self.assertIn("La INE muestra la vigencia 2022 - 2032.", meets)
                self.assertIn("12,000.00 MXN. El mínimo aplicable es 7,000 MXN.", meets)
                self.assertTrue(missing.startswith("Qué falta\n- Comprobante de domicilio"))
                self.assertIn("antigüedad máxima de 3 meses. [2]", missing)
                self.assertTrue(pending.startswith("Qué queda por confirmar\n"))
                self.assertIn("«Apoyo económico»", pending)
                self.assertIn("ingresos comprobables y no especifica si acepta ese concepto. [3, 2]", pending)
                self.assertIn("El banco valida los requisitos", closing)
                self.assertEqual(requirement(result, "antiguedad_comprobante_meses")["motivo"], "documento_ausente")

    def test_inventory_then_assessment_without_upload_then_receipt_and_direct_ine(self):
        ids = self.upload(("ine.png", document(INE, image=True)), ("nomina.pdf", document(PAYROLL)))
        inventory = self.ask(INVENTORY, document_ids=ids)
        self.assertEqual(inventory["derived"]["tipo"], "revision_documental")
        self.assertIn("falta subir: comprobante de domicilio", inventory["answer"].lower())
        self.assertNotIn("requisitos", inventory["derived"])

        assessment = self.ask("¿Qué requisitos cumplo con estos documentos?")
        self.assertEqual(assessment["attachment_view"]["style"], "preliminary_credit")
        for name in ("edad_anios", "vigencia_identificacion", "ingreso_mensual"):
            self.assertEqual(requirement(assessment, name)["estado"], "cumple")
        self.assertEqual(requirement(assessment, "aceptacion_ingreso_comprobable")["estado"], "no verificable")
        self.assertIn("Comprobante de domicilio — No se aportó un comprobante.", assessment["answer"])
        self.assertIn("36 años", assessment["answer"])
        self.assertIn("Apoyo económico", assessment["answer"])
        self.assertIn("nomina.pdf, página 1", assessment["attachment_view"]["references"])
        self.assertIn("data_steward", assessment["agents"])
        self.assertIn("leer_documento", assessment["tools"])
        self.assertGreater(assessment["metrics"]["elapsed_seconds"], 0)
        private = self.store.records[ids[0]]["result"]["campos"]
        self.assertEqual(private["fecha_nacimiento"]["valor"], "1990-04-10")
        self.assertIsNone(requirement(assessment, "edad_anios")["evidencia"][0]["valor"])
        self.assertNotIn("Persona Prueba Alfa", assessment["answer"])

        receipt = self.upload(("luz.pdf", document(RECEIPT)))
        complete = self.ask(ASSESS, document_ids=[*ids, *receipt])
        self.assertEqual(requirement(complete, "antiguedad_comprobante_meses")["estado"], "cumple")
        self.assertIn("expedición (2026-11-01)", complete["answer"])
        self.assertNotIn("Calle Inventada 48", complete["answer"])
        self.assertNotIn("2026-12-01", complete["answer"])
        self.assertIn("luz.pdf, página 1", complete["attachment_view"]["references"])

        direct = self.ask("¿Qué vigencia aparece en mi INE?")
        self.assertIn("2022 - 2032", direct["answer"])
        self.assertEqual([d["file_id"] for d in direct["documents"]], ids[:1])
        self.assertEqual(direct["agents"], ["orquestador"])
        self.assertEqual(direct["tools"], ["leer_documento"])
        self.assertIsNone(direct["derived"])
        self.read.assert_awaited_once()

    def test_receipt_intent_depends_on_the_question_and_the_conversation(self):
        receipt = RECEIPT.replace("Fecha de expedición: 2026-11-01", "Fecha de emisión: 2026-10-17")
        with fitz.open(stream=document(receipt), filetype="pdf") as pdf:
            jpeg = pdf[0].get_pixmap().tobytes("jpeg")
        self.read.return_value = ({"tipo": "comprobante", "legible": True,
            "paginas": [{"pagina": 1, "estado": "leida", "texto": receipt}]}, {})
        ids = self.upload(("luz.jpg", jpeg))
        measure = AttachmentDecision(alcance="bancario", accion="medir_antiguedad")
        clarify = AttachmentDecision(alcance="bancario", accion="aclarar")
        check = AttachmentDecision(alcance="bancario", accion="evaluar_comprobante")
        hub = AsyncMock(side_effect=[measure.model_copy(), measure.model_copy(), measure.model_copy(),
                                     clarify, check.model_copy(), check.model_copy(),
                                     check.model_copy(), measure.model_copy()])
        with patch("banco_agentes.attachment_hub.decide_attachment", hub):
            for question in ("¿Puedes verificar la antigüedad del comprobante?", "¿Cuántos días tiene el recibo?",
                             "¿Qué tan viejo es este recibo?"):
                with self.subTest(question=question):
                    result = self.ask(question, document_ids=ids)
                    self.assertIn("17/10/2026", result["answer"])
                    self.assertIn("33 días transcurridos", result["answer"])
                    self.assertIn("luz.jpg", result["answer"])
                    self.assertIsNone(result["policy"])
                    self.assertEqual(result["tools"], ["leer_documento"])
            unclear = self.ask("¿Esto cumple?", document_ids=ids)
            self.assertEqual(unclear["answer"], "¿Para qué trámite o requisito quieres que lo revise?")
            unscoped = self.ask("¿Es válido para una tarjeta?", document_ids=ids)
            self.assertEqual(unscoped["answer"], "¿Para qué trámite o requisito quieres que lo revise?")
            self.retrieve.assert_not_called()
            explicit = self.ask("¿El recibo sirve para la Tarjeta Clásica?", document_ids=ids)
            self.assertEqual(requirement(explicit, "antiguedad_comprobante_meses")["estado"], "cumple")
            self.assertEqual(explicit["policy"]["producto"], "clasica")
            self.retrieve.assert_called_once()

            self.cid = self.client.post("/api/conversations").json()["conversation_id"]
            ids = self.upload(("luz.jpg", jpeg))
            context = self.ask(INVENTORY, document_ids=ids)
            self.assertEqual(context["policy"]["producto"], "clasica")
            self.retrieve.reset_mock()
            assessment = self.ask("¿Y este recibo sí sirve?")
            self.retrieve.assert_called_once()
            self.assertEqual(hub.await_args.kwargs["previous_product"], "clasica")
            self.retrieve.reset_mock()
            measured = self.ask("¿Cuánto tiempo tiene la factura?")
            self.assertIn("33 días transcurridos", measured["answer"])
            self.retrieve.assert_not_called()

        row = requirement(assessment, "antiguedad_comprobante_meses")
        self.assertEqual(row["estado"], "cumple")
        self.assertEqual({r["regla"] for r in assessment["derived"]["requisitos"]},
                         {"tipo_servicio_comprobante", "antiguedad_comprobante_meses"})
        self.assertIn("emisión (2026-10-17)", assessment["answer"])
        self.assertIn("Límite de 3 meses", assessment["answer"])
        self.assertNotIn("2026-12-01", assessment["answer"])
        self.assertIn("luz.jpg", assessment["attachment_view"]["references"])
        self.assertEqual(row["fuente_politica"]["page"], 1)
        self.assertIn("data_steward", assessment["agents"])
        self.assertEqual(self.read.await_count, 2)
        self.assertEqual(hub.await_count, 8)
        self.assertTrue(all(call.kwargs["previous_product"] is None for call in hub.await_args_list[:6]))

    def test_billed_month_and_ambiguous_currency_correction(self):
        ids = self.upload(("ine.pdf", document(INE)), ("ambigua.pdf", document(PAYROLL.replace("Moneda: MXN", "Moneda: $"))),
                          ("periodo.pdf", document(RECEIPT.replace("Fecha de expedición: 2026-11-01\n", ""))))
        result = self.ask(ASSESS, document_ids=ids)
        amount = requirement(result, "ingreso_mensual")
        self.assertEqual(amount["estado"], "pendiente de confirmar")
        self.assertEqual(amount["motivo"], "no_interpretable")
        currency = next(c for c in result["attachment_view"]["confirmations"] if c["campo"] == "moneda")
        self.assertEqual(currency["options"], ["MXN", "USD", "EUR"])
        proof = requirement(result, "antiguedad_comprobante_meses")
        self.assertTrue(proof["referencia_preliminar"])
        self.assertIn("Mes facturado (2026-10)", proof["nota"])
        self.assertEqual(requirement(result, "vigencia_identificacion")["evidencia"][0]["fuente"]["pagina"], 1)

        path = f"/api/documents/{ids[1]}/corrections"
        body = {"conversation_id": self.cid, "campo": "moneda"}
        invalid = self.client.post(path, json={**body, "valor": "$"})
        self.assertEqual(invalid.status_code, 422)
        self.assertIn("MXN", invalid.json()["detail"])
        self.client.post(path, json={**body, "valor": "pesos mexicanos"}).raise_for_status()
        corrected = self.ask(ASSESS)
        used = next(e for e in requirement(corrected, "ingreso_mensual")["evidencia"] if e["campo"] == "moneda")
        self.assertEqual((used["valor"], used["fuente"]["tipo"]), ("MXN", "aportado_por_usuario"))
        self.assertEqual(corrected["documents"][1]["campos"]["moneda"]["valor_original"], "$")
        self.assertEqual(requirement(corrected, "ingreso_mensual")["estado"], "cumple")
        self.assertIn("datos que confirmaste", corrected["answer"])
        self.read.assert_not_awaited()

    def test_ambiguous_expiry_and_due_date_do_not_qualify_and_selection_stays_private(self):
        receipt = RECEIPT.replace("Fecha de expedición: 2026-11-01\n", "").replace("Mes facturado: 2026-10\n", "")
        ids = self.upload(("ine.pdf", document(INE.replace("2022 - 2032", "2016 - 2026"))),
                          ("solo-pago.pdf", document(receipt)))
        result = self.ask(ASSESS, document_ids=ids)
        self.assertEqual(requirement(result, "vigencia_identificacion")["motivo"], "precision_insuficiente")
        self.assertEqual(requirement(result, "antiguedad_comprobante_meses")["motivo"], "dato_ausente")
        self.assertNotIn("2026-12-01", result["answer"])
        other, other_cid = self.login("sintetico_b")
        self.assertEqual(other.post("/api/chat", json={"conversation_id": self.cid, "question": ASSESS}).status_code, 404)
        self.assertEqual(other.post("/api/chat", json={"conversation_id": other_cid, "question": ASSESS,
                                                       "document_ids": ids}).status_code, 404)
        normal = AsyncMock(return_value={"answer": "Chat normal.", "trace": []})
        with patch("banco_agentes.conversation.Conversation.ask", normal):
            self.assertNotIn("documents", self.ask(ASSESS, document_ids=[]))
            self.assertNotIn("documents", self.ask(ASSESS))
        self.assertEqual(normal.await_count, 2)

    def test_expired_ine_and_low_income_with_incomplete_recency_rule(self):
        hit = policy_hit()
        rule = next(r for r in hit["source"]["requirements"] if r["campo"] == "antiguedad_comprobante_meses")
        del rule["valor"]
        ids = self.upload(("ine.pdf", document(INE.replace("2022 - 2032", "2014 - 2024"))),
                          ("bajo.pdf", document(PAYROLL.replace("12000.00", "6500.00"))),
                          ("luz.pdf", document(RECEIPT)))
        with patch("banco_agentes.steward_rag.Retriever.search", return_value={"hits": [hit]}):
            result = self.ask(ASSESS, document_ids=ids)
        summary = result["answer"].split("\n\n", 1)[0]
        self.assertIn("Aún necesitas cumplir la vigencia de la INE y el monto mínimo de ingreso.", summary)
        self.assertIn("Queda por confirmar la acreditación del ingreso y el comprobante de domicilio.", summary)
        missing = next(block for block in result["answer"].split("\n\n") if block.startswith("Qué falta\n"))
        self.assertIn("Vigencia de la INE — No cumple.", missing)
        self.assertIn("Ingreso mensual · monto mínimo — No cumple.", missing)
        proof = requirement(result, "antiguedad_comprobante_meses")
        self.assertEqual(proof["estado"], "no verificable")
        self.assertEqual(proof["motivo"], "regla_ausente")
        self.assertEqual(proof["fuente_politica"]["page"], 1)
