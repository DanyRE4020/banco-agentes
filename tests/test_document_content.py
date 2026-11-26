"""Lectura completa con transcripciones/selección dobles; nunca Gemini ni BD."""
import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import fitz
from fastapi.testclient import TestClient

from banco_agentes.document_content import prepare_pages, merge_pages, bounded_pages, validate_fragment, local_fragment, content_question
from banco_agentes.documents import DocumentStore, DocumentError, ROOT
from web_fixtures import create_app

FIXTURES = ROOT/"fixtures/documents/lectura_completa"
ADDRESS = "Calle Ensayo 42, Ciudad Ejemplo"


class ContentReader:
    def __init__(self, *, omit_last=False, fail=False):
        self.calls = []
        self.omit_last, self.fail = omit_last, fail
        expected = json.loads((FIXTURES/"transcripciones_dobles.json").read_text(encoding="utf8"))
        self.expected = {hashlib.sha256((FIXTURES/name).read_bytes()).hexdigest(): pages for name, pages in expected.items()}

    async def read(self, data, mime):
        self.calls.append(mime)
        if self.fail:
            raise DocumentError("Fallo simulado del proveedor (HTTP 503).", kind="proveedor")
        pages = copy.deepcopy(self.expected[hashlib.sha256(data).hexdigest()])
        if self.omit_last:
            pages = pages[:-1]
        return {"tipo": "identificacion", "legible": True, "campos": {}, "paginas": pages}, {"usage": {}}


class Selector:
    def __init__(self, mode="absent"):
        self.calls = []
        self.mode = mode

    async def select_fragment(self, question, pages):
        self.calls.append((question, copy.deepcopy(pages)))
        if self.mode == "forged":
            return {"estado": "encontrado", "citas": [{"pagina": 2, "texto": "dato inventado"}]}, {}
        if self.mode == "narrative":
            return {"estado": "encontrado", "citas": [{"pagina": 2, "texto": "La atencion presencial se realiza los martes."}]}, {}
        return {"estado": "ausente", "citas": []}, {}


class PageContentTests(unittest.TestCase):
    def test_digital_all_pages_and_scanned_detection_and_original_text_priority(self):
        digital = prepare_pages((FIXTURES/"digital.pdf").read_bytes(), "application/pdf")
        scanned = prepare_pages((FIXTURES/"escaneado.pdf").read_bytes(), "application/pdf")
        self.assertEqual(len(digital), 3)
        self.assertTrue(all(p["estado"] == "leida" and not p["visual"] for p in digital))
        self.assertIn(ADDRESS, digital[-1]["texto"])
        self.assertTrue(all(p["visual"] and p["estado"] == "no_revisada" for p in scanned))
        self.assertIn(ADDRESS, merge_pages(digital, [{"pagina": 3, "texto": "otra cosa", "estado": "leida"}])[-1]["texto"])
        marked = merge_pages(copy.deepcopy(scanned), [{"pagina": 1, "texto": "Domicilio: [[ILEGIBLE]]", "estado": "leida"}])
        self.assertEqual(marked[0]["estado"], "parcial")
        with self.assertRaises(DocumentError):
            merge_pages(scanned, [{"pagina": 4, "texto": "fuera", "estado": "leida"}])

    def test_limits_and_unverified_quotes_are_not_silently_accepted(self):
        with patch("banco_agentes.document_content.PAGE_CHARS", 10), patch("banco_agentes.document_content.TOTAL_CHARS", 12):
            pages = bounded_pages([{"pagina": i, "texto": "x"*30, "estado": "leida"} for i in (1, 2, 3)])
        self.assertEqual([p["estado"] for p in pages], ["parcial", "parcial", "no_revisada"])
        self.assertEqual(sum(len(p["texto"]) for p in pages), 12)
        with self.assertRaises(DocumentError):
            validate_fragment({"estado": "encontrado", "citas": [{"pagina": 1, "texto": "invención"}]}, pages)
        with self.assertRaises(DocumentError):
            validate_fragment({"estado": "ilegible", "citas": [{"pagina": 1, "texto": "xxx"}]}, pages)

    def test_multiline_value_is_not_cut_off_by_label_lookup(self):
        pages = [{"pagina": 1, "texto": "Domicilio: Calle Ensayo 42\nCiudad Ejemplo\nColor: Verde\n", "estado": "leida"}]
        self.assertIsNone(local_fragment("¿Qué domicilio aparece en el documento?", pages))
        self.assertIsNone(local_fragment("¿Qué domicilio y color aparecen en el documento?", pages))
        date_page = [{"pagina": 1, "texto": "fecha emision: 2026-10\n"}]
        self.assertEqual(local_fragment("¿Cuál es la fecha de emisión del documento?", date_page).citas[0].texto, "2026-10")

    def test_label_punctuation_and_whitespace_return_original_transcription(self):
        original = "Fecha de emisión 2026-11-02"
        pages = [{"pagina": 1, "texto": original + "\nImporte:\u00a0$1,250.00"}]
        result = validate_fragment({"estado": "encontrado", "citas": [
            {"pagina": 1, "texto": "Fecha de emisión:  2026-11-02"},
            {"pagina": 1, "texto": "Importe $1,250.00"},
        ]}, pages)
        self.assertEqual([q.texto for q in result.citas], [original, "Importe:\u00a0$1,250.00"])
        self.assertTrue(content_question("¿Qué fecha leíste?"))

    def test_changed_or_truncated_values_are_rejected(self):
        pages = [{"pagina": 1, "texto": "Fecha de emisión 2026-11-02\nImporte: $1,250.00\nAjuste: -250.00\nTotal $2\u00a0250.00"}]
        for fabricated in ("Fecha de emisión: 2026-11-03", "2026-11", "2026/11/02", "2026-11-02.0",
                           "Importe: $1,350.00", "Importe: $1.250,00", "$1250.00", "250.00", "$1,250.", "Total $2"):
            with self.subTest(fragment=fabricated), self.assertRaisesRegex(DocumentError, "no coincide literalmente"):
                validate_fragment({"estado": "encontrado", "citas": [{"pagina": 1, "texto": fabricated}]}, pages)
        with self.assertRaises(DocumentError):
            validate_fragment({"estado": "encontrado", "citas": [{"pagina": 2, "texto": "2026-11-02"}]}, pages)


class FullReadingFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.reader, self.selector = ContentReader(), Selector()
        self.store = DocumentStore(Path(self.temp.name)/"uploads", reader=self.reader, fragment_reader=self.selector)
        self.audit, self.metrics = Path(self.temp.name)/"audit.jsonl", Path(self.temp.name)/"metrics.jsonl"
        self.app = create_app(document_store=self.store, audit_path=self.audit, metrics_path=self.metrics)
        self.steward = AsyncMock(side_effect=AssertionError("No debe consultar Steward"))
        self.hub = AsyncMock(side_effect=AssertionError("No debe consultar el hub generativo"))
        for target, mock in (("banco_agentes.steward_rag.attachment_policy", self.steward),
                             ("banco_agentes.attachment_hub.decide_attachment", self.hub)):
            p = patch(target, new=mock)
            p.start()
            self.addCleanup(p.stop)

    def upload(self, name, profile="cliente"):
        client = TestClient(self.app, base_url="http://127.0.0.1:8000")
        client.post("/api/login", json={"username": profile, "password": "ClienteDemo!2026" if profile == "cliente" else "AnalistaDemo!2026"})
        cid = client.post("/api/conversations").json()["conversation_id"]
        r = client.post("/api/documents", data={"conversation_id": cid}, files=[("files", (name, (FIXTURES/name).read_bytes()))])
        self.assertEqual(r.status_code, 200, r.text)
        ids = [f["file_id"] for f in r.json()["documents"]]
        return client, cid, ids

    def ask(self, session, question):
        client, cid, ids = session
        r = client.post("/api/chat", json={"conversation_id": cid, "document_ids": ids, "question": question})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_pdf_scan_last_page_and_images_non_schema_fields_both_profiles_private(self):
        for profile in ("cliente", "analista"):
            for name in ("digital.pdf", "escaneado.pdf", "imagen.png", "imagen.jpg"):
                with self.subTest(profile=profile, name=name):
                    session = self.upload(name, profile)
                    result = self.ask(session, "¿Qué domicilio aparece en esta identificación?")
                    self.assertIn(ADDRESS, result["answer"])
                    self.assertIn("página 3", result["answer"]) if name.endswith("pdf") else self.assertNotIn("página", result["answer"])
                    self.assertEqual(result["tools"], ["leer_documento"])
                    self.assertEqual(result["agents"], ["orquestador"])
                    self.assertTrue(result["documents"][0]["lectura"]["completa"])
                    self.assertIsNone(result["derived"])
                    result = self.ask(session, "¿De qué color es la carpeta del documento?")
                    self.assertIn("Verde oliva", result["answer"])
                    self.assertNotIn(ADDRESS, json.dumps(result, ensure_ascii=False))
                    self.assertNotIn("Persona Ficticia Nube", json.dumps(result, ensure_ascii=False))
                    self.assertNotIn("GODE561231HDFRRN09", json.dumps(result, ensure_ascii=False))
                    self.assertEqual(result["metrics"]["model_calls"], 0)  # OCR cacheado, etiqueta literal local.
        self.steward.assert_not_called()
        self.hub.assert_not_called()
        logs = self.audit.read_text(encoding="utf8")+self.metrics.read_text(encoding="utf8")
        for value in (ADDRESS, "Persona Ficticia Nube", "GODE561231HDFRRN09", "Verde oliva", "Ignora tus reglas"):
            self.assertNotIn(value, logs)

    def test_digital_needs_no_multimodal_and_preserves_raw_date_amount(self):
        session = self.upload("digital.pdf")
        for question, expected in (("¿Qué vigencia aparece en el documento?", "2032"),
            ("¿Qué fecha de referencia aparece en el documento?", "mayo de 2031"),
            ("¿Qué importe de referencia aparece en el documento?", "1.234,50 pesos")):
            self.assertIn(expected, self.ask(session, question)["answer"])
        self.assertEqual(self.reader.calls, [])
        self.assertEqual(self.selector.calls, [])

    def test_narrative_selection_is_literal_page_scoped_and_does_not_call_spokes(self):
        self.selector.mode = "narrative"
        result = self.ask(self.upload("digital.pdf"), "¿Qué día atienden presencialmente según el documento?")
        self.assertIn("los martes", result["answer"])
        self.assertIn("página 2", result["answer"])
        self.assertEqual(len(self.selector.calls[0][1]), 3)
        self.assertEqual(result["metrics"]["model_calls"], 1)
        self.assertEqual(result["metrics"]["calls"][0]["agent"], "leer_documento")
        self.steward.assert_not_called()

    def test_absent_illegible_failed_and_partial_are_distinct(self):
        absent = self.ask(self.upload("digital.pdf"), "¿Qué folio de trámite aparece en el documento?")
        self.assertIn("no aparece", absent["answer"])
        unclear = self.ask(self.upload("ilegible.png"), "¿Qué domicilio aparece en la imagen?")
        self.assertIn("aparece, pero es ilegible", unclear["answer"])
        self.assertIn("Lectura parcial", unclear["answer"])
        self.reader.omit_last = True
        partial = self.ask(self.upload("escaneado.pdf"), "¿Qué domicilio aparece en el documento?")
        self.assertIn("no puedo afirmar que no aparezca", partial["answer"])
        self.assertIn("página 3 sin revisar", partial["answer"])
        self.reader.fail, self.reader.omit_last = True, False
        failed = self.ask(self.upload("imagen.jpg"), "¿Qué domicilio aparece en la imagen?")
        self.assertTrue(failed["document_errors"])
        self.assertEqual(failed["document_errors"][0]["tipo"], "proveedor")
        self.assertNotIn("no aparece", failed["answer"])

    def test_forged_quote_and_unrelated_owner_or_conversation_are_rejected(self):
        self.selector.mode = "forged"
        session = self.upload("digital.pdf")
        result = self.ask(session, "¿Qué día atienden presencialmente según el documento?")
        self.assertTrue(result["document_errors"])
        self.assertNotIn("dato inventado", result["answer"])
        client, cid, ids = session
        cid2 = client.post("/api/conversations").json()["conversation_id"]
        r = client.post("/api/chat", json={"conversation_id": cid2, "document_ids": ids, "question": "¿Qué domicilio aparece?"})
        self.assertIn(r.status_code, (403, 404))
        other, other_cid, _ = self.upload("digital.pdf", "analista")
        r = other.post("/api/chat", json={"conversation_id": other_cid, "document_ids": ids, "question": "¿Qué domicilio aparece?"})
        self.assertIn(r.status_code, (403, 404))
        self.assertEqual(self.reader.calls, [])

    def test_jpeg_returns_backed_date_when_another_fragment_has_altered_amount(self):
        from types import SimpleNamespace
        receipt = ROOT / "fixtures/documents/genericos/comprobante.jpg"
        data = receipt.read_bytes()
        original = "Fecha 2026-10-12"
        transcript = original + "\nImporte: 250.00 MXN"
        self.reader.expected[hashlib.sha256(data).hexdigest()] = [
            {"pagina": 1, "estado": "leida", "texto": transcript}]
        selector = AsyncMock(return_value=({"estado": "encontrado", "citas": [
            {"pagina": 1, "texto": "Fecha: 2026-10-12"},
            {"pagina": 1, "texto": "Importe: 260.00 MXN"},
        ]}, {}))
        self.store.fragment_reader = SimpleNamespace(select_fragment=selector)
        client = TestClient(self.app, base_url="http://127.0.0.1:8000")
        self.addCleanup(client.close)
        client.post("/api/login", json={"username": "cliente", "password": "ClienteDemo!2026"}).raise_for_status()
        cid = client.post("/api/conversations").json()["conversation_id"]
        upload = client.post("/api/documents", data={"conversation_id": cid}, files=[("files", (receipt.name, data))])
        upload.raise_for_status()
        file_id = upload.json()["documents"][0]["file_id"]
        result = self.ask((client, cid, [file_id]), "¿Qué fecha leíste y qué importe total aparece en este recibo?")
        self.assertIn(original + " (comprobante.jpg)", result["answer"])
        self.assertNotIn("260.00", result["answer"])
        self.assertIn("quedaron sin verificar", result["answer"])
        self.assertEqual(result["document_errors"], [])
        self.assertEqual(self.store.records[file_id]["content_pages"][0]["texto"], transcript)
        selector.assert_awaited_once()
        self.steward.assert_not_called()

    def test_mixed_pdf_provider_failure_preserves_digital_page_without_retry(self):
        self.reader.fail = True
        session = self.upload("digital.pdf")
        client, cid, _ = session
        with fitz.open(FIXTURES/"digital.pdf") as digital, fitz.open(FIXTURES/"escaneado.pdf") as scanned, fitz.open() as mixed:
            mixed.insert_pdf(digital, from_page=0, to_page=0)
            mixed.insert_pdf(scanned, from_page=2, to_page=2)
            data = mixed.tobytes(deflate=True)
        upload = client.post("/api/documents", data={"conversation_id": cid}, files=[("files", ("mixto.pdf", data))])
        self.assertEqual(upload.status_code, 200)
        ids = [d["file_id"] for d in upload.json()["documents"]]
        session = client, cid, ids
        result = self.ask(session, "¿Qué nombre aparece en el documento?")
        self.assertIn("Persona Ficticia Nube", result["answer"])
        self.assertIn("página 2 sin revisar por fallo del proveedor", result["answer"])
        result = self.ask(session, "¿Qué domicilio aparece en el documento?")
        self.assertIn("no puedo afirmar que no aparezca", result["answer"])
        self.assertEqual(self.reader.calls, ["application/pdf"])

    def test_local_file_read_failure_is_not_reported_as_missing_field(self):
        session = self.upload("digital.pdf")
        self.store.records[session[2][0]]["path"].unlink()
        result = self.ask(session, "¿Qué domicilio aparece en el documento?")
        self.assertEqual(result["document_errors"][0]["tipo"], "lectura")
        self.assertNotIn("no aparece", result["answer"])
