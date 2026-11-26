"""Revisión documental por API con archivos ficticios; lector, hub y recuperación dobles."""
import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from banco_agentes.document_review import policy_documentation, review_documents, recognize
from banco_agentes.documents import ROOT, DocumentError, DocumentStore, validate_document, WireExtraction, gemini_document_schema
from web_fixtures import create_app
from test_documents import fixture_decision

FIXTURES = ROOT/"fixtures/documents/revision"
SOURCE = {"title": "Política ficticia de Tarjeta Clásica", "file": "politica_ficticia.pdf", "page": 2,
          "section": "Requisitos", "version": "v-prueba", "consulted_at": "2026-11-18",
          "source_kind": "ejemplo_ilustrativo"}


def policy(closed=True):
    source = {**SOURCE, "categorias_documentales": ["identificacion", "domicilio", "ingresos"],
              "total_documental_definido": closed}
    hit = {"id": "fixture-politica", "source": source,
           "text": "Documentos solicitados: identificación oficial, comprobante de domicilio y comprobante de ingresos.\nOtros requisitos: edad e ingreso mínimo conforme a la política."}
    return {"tipo": "politica", "producto": "clasica", "reglas": [], "fuentes": [],
            "fragment_ids": [hit["id"]], **policy_documentation([hit])}


class Reader:
    def __init__(self):
        raw = json.loads((FIXTURES/"expected.json").read_text(encoding="utf8"))
        self.expected = {hashlib.sha256((FIXTURES/name).read_bytes()).hexdigest(): value for name,value in raw.items()}
        self.calls = []

    async def read(self, data, mime):
        self.calls.append(mime)
        return copy.deepcopy(self.expected[hashlib.sha256(data).hexdigest()]), {}


class DocumentReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.reader = Reader()
        self.store = DocumentStore(Path(self.temp.name)/"uploads", reader=self.reader)
        self.audit = Path(self.temp.name)/"audit.jsonl"
        self.app = create_app(document_store=self.store, audit_path=self.audit)
        for target, value in (("banco_agentes.attachment_hub.decide_attachment", AsyncMock(side_effect=fixture_decision)),
                              ("banco_agentes.steward_rag.document_policy", AsyncMock(side_effect=lambda _: policy())),
                              ("banco_agentes.document_credit.compare_requirements", unittest.mock.Mock(side_effect=AssertionError("No evaluar elegibilidad")))):
            mocked = patch(target, new=value)
            mocked.start()
            self.addCleanup(mocked.stop)

    def ask(self, names, profile="cliente", question="Revisa mi documentación para solicitar la Tarjeta Clásica"):
        client = TestClient(self.app, base_url="http://127.0.0.1:8000")
        password = "ClienteDemo!2026" if profile == "cliente" else "AnalistaDemo!2026"
        client.post("/api/login", json={"username":profile,"password":password})
        cid = client.post("/api/conversations").json()["conversation_id"]
        uploaded = client.post("/api/documents", data={"conversation_id":cid},
            files=[("files", ("adjunto_"+str(i), (FIXTURES/name).read_bytes())) for i,name in enumerate(names)])
        self.assertEqual(uploaded.status_code,200,uploaded.text)
        ids = [d["file_id"] for d in uploaded.json()["documents"]]
        response = client.post("/api/chat",json={"conversation_id":cid,"document_ids":ids,"question":question})
        self.assertEqual(response.status_code,200,response.text)
        self.current = client,cid,ids
        return response.json()

    def test_three_categories_both_profiles_without_eligibility_or_personal_data(self):
        for profile in ("cliente","analista"):
            with self.subTest(profile=profile):
                result = self.ask(["ine_ficticia.pdf","recibo_servicio.png","cfdi_pago.jpg"],profile)
                self.assertTrue(result["answer"].startswith("Reconocí estos documentos que subiste"))
                self.assertTrue(all(r["status"] == "aportada" for r in result["derived"]["categorias"]))
                self.assertEqual(result["agents"],["orquestador","data_steward"])
                self.assertEqual(result["tools"],["leer_documento","recuperar_politica_documental"])
                self.assertTrue(result["metrics"]["calls"])
                self.assertGreater(result["total_seconds"],0)
                self.assertNotIn("requisitos",result["derived"])
                visible = json.dumps([result["answer"],result["attachment_view"]],ensure_ascii=False)
                for private in ("Persona Ejemplo", "Calle Inventada", "D1", "R2", "150.00", "cumple"):
                    self.assertNotIn(private,visible)
                self.assertIn("el banco debe validar los requisitos y evaluar la solicitud",visible)
                self.assertTrue(result["attachment_view"]["policy_requirements"])
                evidence = result["derived"]["reconocimientos"]
                self.assertEqual(evidence[0]["evidencia"][-1]["fuente"]["pagina"],1)
                self.assertTrue(any(e.get("etiqueta")=="Límite de pago" for e in evidence[1]["evidencia"]))
                self.assertTrue(any(e.get("significado")=="neto" for e in evidence[2]["evidencia"]))
        self.assertEqual(set(self.reader.calls),{"application/pdf","image/png","image/jpeg"})
        log=self.audit.read_text(encoding="utf8")
        for private in ("Persona Ejemplo", "Calle Inventada", "150.00", "primera quincena"):
            self.assertNotIn(private,log)

    def test_missing_category_and_duplicates_do_not_inflate_count(self):
        result=self.ask(["ine_ficticia.pdf","recibo_servicio.png"])
        self.assertNotIn("Aportaste",result["answer"])
        self.assertIn("Falta subir: Comprobante de ingresos",result["answer"])
        result=self.ask(["ine_ficticia.pdf","ine_ficticia.png","recibo_servicio.png"])
        self.assertNotIn("Aportaste",result["answer"])
        self.assertEqual(len(result["derived"]["reconocimientos"]),3)
        self.assertEqual(len(result["derived"]["categorias"]),3)

    def test_undefined_total_does_not_show_fraction(self):
        with patch("banco_agentes.steward_rag.document_policy",new=AsyncMock(return_value=policy(False))):
            result=self.ask(["ine_ficticia.pdf"])
        self.assertNotIn("de 3",result["answer"])
        self.assertIn("- INE",result["answer"])
        self.assertIn("Comprobante de ingresos",result["answer"])

    def test_unreadable_required_field_pending_without_spurious_confirmation(self):
        raw=self.reader.expected[hashlib.sha256((FIXTURES/"ine_ficticia.pdf").read_bytes()).hexdigest()]
        raw["campos"]["fecha_vencimiento"]={"valor":None,"lectura":"ilegible","confianza":.1,"pagina":1}
        result=self.ask(["ine_ficticia.pdf","recibo_servicio.png","cfdi_pago.jpg"])
        self.assertNotIn("Aportaste",result["answer"])
        self.assertEqual(result["derived"]["reconocimientos"][0]["estado"],"pendiente")
        self.assertIn("No pude completar la lectura de: Identificación",result["answer"])
        self.assertEqual(result["attachment_view"]["confirmations"],[])

    def test_direct_address_still_bypasses_steward_and_cites_pdf_page(self):
        with patch("banco_agentes.steward_rag.attachment_policy",new_callable=AsyncMock) as steward:
            result=self.ask(["ine_ficticia.pdf"],question="¿Qué domicilio aparece en esta identificación?")
        steward.assert_not_called()
        self.assertIn("Calle Inventada 10",result["answer"])
        self.assertIn("página 1",result["answer"])
        self.assertIsNone(result["derived"])

    def test_policy_failure_preserves_recognition_without_inventing_total(self):
        with patch("banco_agentes.steward_rag.attachment_policy",new=AsyncMock(side_effect=RuntimeError())):
            result=self.ask(["ine_ficticia.pdf"])
        self.assertIsNone(result["policy"])
        self.assertIn("- INE",result["answer"])
        self.assertIn("falta consultar su política",result["attachment_view"]["policy_requirements"][0]["text"])

    def test_partial_failure_marks_only_the_failed_attachment(self):
        read_fixture = self.reader.read

        async def read(data, mime):
            if mime == "image/jpeg":
                raise DocumentError("Fallo simulado del lector.", kind="lectura")
            return await read_fixture(data, mime)

        self.reader.read = read
        result = self.ask(["recibo_servicio.png", "cfdi_pago.jpg"])
        view = result["attachment_view"]
        self.assertEqual(view["document_names"], ["recibo de luz"])
        self.assertIn("La lectura fue parcial.", view["messages"])
        self.assertIn("adjunto_1 está pendiente de lectura.", view["messages"])
        self.assertEqual(result["answer"].count("pendiente de lectura"), 1)
        self.assertNotIn("Falta subir", result["answer"])
        self.assertTrue(all(r.get("source_indices") for r in view["policy_requirements"]))

    def test_total_failure_leaves_missing_documents_indeterminate(self):
        self.reader.read = AsyncMock(side_effect=DocumentError("Fallo simulado del lector.", kind="lectura"))
        for profile in ("cliente", "analista"):
            for recovered in (True, False):
                with self.subTest(profile=profile, policy_recovered=recovered):
                    retrieval = AsyncMock(return_value=policy(), side_effect=None if recovered else RuntimeError())
                    with patch("banco_agentes.steward_rag.document_policy", new=retrieval):
                        result = self.ask(["recibo_servicio.png", "cfdi_pago.jpg"], profile)
                    retrieval.assert_awaited_once()
                    view = result["attachment_view"]
                    self.assertFalse(view["opening"])
                    self.assertEqual(view["document_names"], [])
                    self.assertEqual(view["messages"], [
                        "adjunto_0 está pendiente de lectura.",
                        "adjunto_1 está pendiente de lectura.",
                        "No pude determinar qué documentos faltan.",
                    ])
                    self.assertTrue(all(message in result["answer"] for message in view["messages"]))
                    self.assertEqual(view["reading_notices"], [])
                    self.assertEqual([e["archivo"] for e in result["document_errors"]], ["adjunto_0", "adjunto_1"])
                    self.assertNotIn("Falta subir", result["answer"])
                    self.assertNotIn("Reconocí", result["answer"])
                    if recovered:
                        self.assertIn("Identificación oficial.", [r["text"] for r in view["policy_requirements"]])
                        self.assertTrue(all(r.get("source_indices") == [1] for r in view["policy_requirements"]))
                        self.assertIn("Política ficticia de Tarjeta Clásica, página 2", view["references"][0])
                    else:
                        self.assertIsNone(result["policy"])
                        self.assertIn("falta consultar su política", view["policy_requirements"][0]["text"])


class RecognitionUnitTests(unittest.TestCase):
    def parsed(self, tipo, **values):
        fields={k:{"valor":v,"confianza":.99,"pagina":1} for k,v in values.items()}
        result=validate_document({"tipo":tipo,"legible":True,"campos":fields},
            {"name":"cualquier_archivo.pdf","file_id":"file","mime":"application/pdf","pages":1},check_consistency=False)
        result["id"]="D1"
        return result

    def test_balance_credits_net_and_other_keep_meaning_without_month_or_amount_comparison(self):
        for kind,name,meaning in (("estado_de_cuenta","saldo_final","saldo"),("estado_de_cuenta","abonos","abonos"),
                                  ("recibo_pago","neto_a_pagar","neto"),("recibo_pago","importe_total","otro"),
                                  ("cfdi_nomina","deducciones","deducciones"),("estado_de_cuenta","cargos","cargos")):
            with self.subTest(kind=kind,field=name):
                doc=self.parsed(kind,**{name:"1.00","periodo_documento":"segunda quincena de 2020"})
                result=recognize(doc)
                self.assertEqual(result["estado"],"aportada")
                self.assertEqual(result["evidencia"][0]["significado"],meaning)
        proof=self.parsed("comprobante",domicilio="Calle Inventada",fecha_corte="2020",importe_total="99999.00")
        self.assertEqual(recognize(proof)["categoria"],"domicilio")

    def test_other_date_keeps_its_original_label(self):
        proof=self.parsed("comprobante",domicilio="Calle Inventada",fecha_documento="2020")
        proof["campos"]["fecha_documento"]["etiqueta"]="Fecha de instalación"
        result=recognize(proof)
        self.assertEqual(result["estado"],"aportada")
        self.assertEqual(result["evidencia"][-1]["etiqueta"],"Fecha de instalación")
        self.assertNotIn("emisión",result["motivo"])

    def test_no_assumption_of_ine_from_official_type_or_filename(self):
        result=recognize(self.parsed("identificacion",tipo_identificacion="oficial",fecha_vencimiento="2030"))
        self.assertEqual(result["estado"],"pendiente")
        self.assertIn("INE",result["datos_pendientes"][0])

    def test_address_meaning_is_preserved_and_periodicity_is_not_period(self):
        proof=recognize(self.parsed("comprobante",domicilio_fiscal="Calle Inventada",fecha_corte="2020"))
        self.assertEqual(proof["estado"],"aportada")  # Presencia legible, sin validar su aceptación como comprobante.
        self.assertEqual(proof["evidencia"][0]["campo"],"domicilio_fiscal")
        self.assertEqual(proof["evidencia"][0]["significado"],"domicilio fiscal")
        self.assertEqual(recognize(self.parsed("cfdi_nomina",neto_a_pagar="12000",periodicidad="mensual"))["estado"],"pendiente")

    def test_policy_does_not_infer_closed_total_from_numeric_rules_or_partial_list(self):
        source={**SOURCE,"requirements":[{"campo":"ingreso_mensual","valor":7000}]}
        result=policy_documentation([{"id":"f","source":source,"text":"Identificación oficial vigente. Ingreso mensual mínimo comprobable."}])
        self.assertFalse(result["total_documental_definido"])
        self.assertEqual([c["categoria"] for c in result["categorias_documentales"]],["identificacion"])
        conditional={**SOURCE,"categorias_documentales":[{"categoria":"domicilio","condicion":"si difiere"}],"total_documental_definido":True}
        self.assertFalse(policy_documentation([{"id":"f","source":conditional,"text":"Comprobante de domicilio"}])["total_documental_definido"])

    def test_gemini_schema_keeps_required_wire_contract_without_nullable_explosion(self):
        schema=gemini_document_schema(WireExtraction)
        self.assertNotIn("anyOf",json.dumps(schema))
        self.assertIn("recibo_pago",schema["properties"]["tipo"]["enum"])
