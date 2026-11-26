"""Diagnóstico reproducible de DOC-11 y recibo ficticio; sin Gemini/BD."""
import copy
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock, patch

import fitz

from banco_agentes.conversation import Conversation
from banco_agentes.documents import DocumentStore, ROOT
from banco_agentes.governance import DemoSession
from banco_agentes.steward_rag import document_policy
from banco_agentes.telemetry import turn_metrics
from banco_agentes.web import public_result
from test_document_evidence import MatrixReader, FIXTURES
from test_documents import fixture_decision


def policy_hit():
    manifest = json.loads((ROOT/"catalogo/bank_documents_manifest.json").read_text(encoding="utf8"))
    source = copy.deepcopy(next(d for d in manifest["documents"] if "DOC-11" in d["file"]))
    section, rules = next(iter(source["requirements_by_section"].items()))
    with fitz.open(ROOT/source["file"]) as pdf:
        text = pdf[0].get_text().split("2. Requisitos", 1)[1].split("3. Pasos", 1)[0]
    source.update(page=1, section=section, requirements=rules)
    return {"id": "fixture-doc11-requisitos", "text": text, "source": source}


class ReceiptDetailsTests(unittest.IsolatedAsyncioTestCase):
    async def test_doc11_service_receipt_reason_reaches_chat_without_blanket_rejection(self):
        clock = patch("banco_agentes.document_credit.date", wraps=date)
        clock.start().today.return_value = date(2026, 11, 17)
        self.addCleanup(clock.stop)
        hit = policy_hit()
        with patch("banco_agentes.steward_rag.Retriever.search", return_value={"hits": [hit]}):
            policy = await document_policy("clasica")
        rule = next(r for r in policy["reglas"] if r["campo"] == "antiguedad_comprobante_meses")
        self.assertEqual(set(rule["tipos_comprobante_admitidos"]),
            {"electricidad", "agua", "telefonia_fija", "predial", "television_paga", "gas"})
        for missing in (True, False):
            with self.subTest(missing_issue_date=missing), tempfile.TemporaryDirectory() as folder:
                reader = MatrixReader()
                if missing:
                    for raw in reader.expected.values():
                        if raw["tipo"] == "comprobante":
                            raw["campos"].pop("fecha_emision", None)
                store = DocumentStore(Path(folder), reader=reader)
                item = store.save_many("owner", "chat", [(FIXTURES/"comprobante_municipal.pdf").read_bytes()])[0]
                conversation = Conversation(DemoSession(profile="Cliente", customer_id=1, account_id=1))
                with patch("banco_agentes.steward_rag.Retriever.search", return_value={"hits": [hit]}), patch(
                    "banco_agentes.attachment_hub.decide_attachment", new=AsyncMock(side_effect=fixture_decision)):
                    result = await conversation.ask_documents("¿Qué documentos necesito para solicitar Clásica?", store=store,
                        file_ids=[item["file_id"]], owner="owner", conversation_id="chat")
                document = result["documents"][0]
                self.assertEqual(document["tipo"], "comprobante")
                self.assertEqual(document["campos"]["tipo_comprobante"]["valor"], "electricidad")
                proof = next(r for r in result["derived"]["reconocimientos"] if r["categoria"] == "domicilio")
                self.assertEqual(proof["estado"], "aportada")  # El texto local conserva una fecha, sin exigir emisión.
                visible = next(r for r in result["derived"]["categorias"] if r["categoria"] == "domicilio")
                self.assertIn("fecha", visible["reason"])
                self.assertNotIn("El adjunto no aporta evidencia", result["answer"])
                metrics = turn_metrics(result["trace"], elapsed_seconds=result["total_seconds"])
                public = public_result({**result, "metrics": metrics})
                self.assertEqual(public["agents"], ["orquestador", "data_steward"])
                self.assertEqual(public["tools"], ["leer_documento", "recuperar_politica_documental"])
                self.assertGreater(public["metrics"]["elapsed_seconds"], 0)
