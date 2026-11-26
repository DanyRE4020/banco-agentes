"""Pruebas con dobles: sin Gemini, BD, .env ni exportadores remotos."""
import copy
import hashlib
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from banco_agentes.documents import (ROOT, DocumentStore, DocumentError, GeminiDocumentReader,
                                     MAX_BYTES, validate_document, effective_document)
from banco_agentes.document_credit import compare_requirements
from web_fixtures import create_app

FIXTURES = ROOT / "fixtures/documents"


def field(value, confidence=.99, page=1):
    return {"valor": value, "confianza": confidence, "pagina": page}


def extraction(tipo="estado_de_cuenta", *, expiry="2030-12-31", unreadable=False):
    fields = ({"titular": "Alex Prueba Ejemplo", "periodo_inicio": "2026-10-01", "periodo_fin": "2026-10-31",
               "saldo_inicial": "1000.00", "saldo_final": "7000.00", "abonos": "9000.00", "cargos": "3000.00",
               "domicilio": "Calle Ficticia 123", "moneda": "MXN"} if tipo == "estado_de_cuenta" else
              {"nombre": "Alex Prueba Ejemplo", "fecha_nacimiento": "1990-05-20",
               "fecha_vencimiento": None if unreadable else expiry, "numero_enmascarado": "000000001234"}
              if tipo == "identificacion" else {})
    return {"tipo": tipo, "legible": True, "campos": {k:field(v, .1 if v is None else .99) for k,v in fields.items()}}


class FixtureReader:
    def __init__(self):
        self.calls = []

    async def read(self, data, mime):
        self.calls.append(hashlib.sha256(data).hexdigest())
        name = json.loads((FIXTURES/"manifest.json").read_text(encoding="utf-8"))[self.calls[-1]]
        if name.startswith("identificacion"):
            raw = extraction("identificacion", expiry="2020-12-31" if "vencida" in name else "2030-12-31", unreadable="ilegible" in name)
        elif name == "no_bancario.pdf":
            raw = extraction("otro")
        else:
            raw = extraction()
        return raw, {"input_tokens": 10, "output_tokens": 5}


def policy(product="clasica"):
    filename = "DOC-11_Tarjeta_de_credito_Clasica.pdf" if product == "clasica" else "DOC-02_Credito_de_nomina.pdf"
    manifest = json.loads((ROOT/"catalogo/bank_documents_manifest.json").read_text(encoding="utf-8"))
    entry = next(e for e in manifest["documents"] if e["file"].endswith(filename))
    source = {**entry, "page": 1, "fragment_id": "fixture:requisitos"}
    rules = [{**rule, "fuente": source} for rules in entry["requirements_by_section"].values() for rule in rules]
    rules.append({"campo": "vigencia_identificacion", "fuente": source})
    return {"producto": product, "tipo": "politica", "reglas": rules, "fuentes": [],
            "faltantes": ["La identificación genérica no acredita carácter oficial."], "fragment_ids": ["fixture:requisitos"]}


async def fixture_decision(question, documents, *, previous_product=None, **kwargs):
    """Doble del hub; sin llamadas a modelos ni reglas nuevas en producción."""
    from banco_agentes.attachment_hub import AttachmentDecision, evidence_items
    from banco_agentes.document_credit import requested_product, compliance_question, document_followup, has_product_name
    if "clima" in question.lower() or "jardiner" in question.lower():
        return AttachmentDecision(alcance="fuera_de_alcance", accion="responder")
    product = requested_product(question)
    if product is None and not has_product_name(question) and document_followup(question):
        product = previous_product
    if compliance_question(question) or has_product_name(question) or (product and document_followup(question)):
        return AttachmentDecision(alcance="bancario", accion="comparar" if product else "aclarar", producto=product or "general")
    return AttachmentDecision(alcance="bancario", accion="responder", evidencia_ids=list(evidence_items(documents))[:1])


class DocumentAPITests(unittest.TestCase):
    def setUp(self):
        hub = patch("banco_agentes.attachment_hub.decide_attachment", new=AsyncMock(side_effect=fixture_decision))
        self.hub = hub.start()
        self.addCleanup(hub.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.reader = FixtureReader()
        self.store = DocumentStore(Path(self.temp.name)/"uploads", reader=self.reader)
        self.audit = Path(self.temp.name)/"audit.jsonl"
        self.metrics = Path(self.temp.name)/"metrics.jsonl"
        self.app = create_app(audit_path=self.audit, metrics_path=self.metrics, document_store=self.store)
        self.client = TestClient(self.app, base_url="http://127.0.0.1:8000")
        self.login(self.client)
        self.cid = self.client.post("/api/conversations").json()["conversation_id"]

    def login(self, client):
        self.assertEqual(client.post("/api/login", json={"username":"cliente", "password":"ClienteDemo!2026"}).status_code, 200)

    def upload(self, *names, client=None, conversation=None):
        return (client or self.client).post("/api/documents", data={"conversation_id":conversation or self.cid, "synthetic":"true"},
            files=[("files", (name, (FIXTURES/name).read_bytes(), "text/plain")) for name in names])

    def chat(self, ids, question="¿Qué documentos necesito para solicitar Clásica?", client=None, conversation=None):
        return (client or self.client).post("/api/chat", json={"question": question, "conversation_id":conversation or self.cid, "document_ids":ids})

    def test_expiry_does_not_change_recognition_and_cache(self):
        upload = self.upload("estado_cuenta.pdf", "identificacion_vigente.png", "identificacion_vencida.png")
        self.assertEqual(upload.status_code, 200)
        ids = [d["file_id"] for d in upload.json()["documents"]]
        with patch("banco_agentes.steward_rag.document_policy", new=AsyncMock(return_value=policy())) as steward:
            current = self.chat(ids[:2]).json()
            expired = self.chat([ids[0],ids[2]]).json()
            self.chat([ids[0],ids[2]])
        self.assertEqual(steward.await_count, 3)
        self.assertEqual(len(self.reader.calls), 3)
        self.assertEqual([d["id"] for d in current["documents"]], ["D1", "D2"])
        self.assertEqual([d["id"] for d in expired["documents"]], ["D1", "D3"])
        a,b = current["derived"]["categorias"], expired["derived"]["categorias"]
        self.assertEqual([(r["categoria"],r["status"]) for r in a],[(r["categoria"],r["status"]) for r in b])
        self.assertEqual(current["derived"]["tipo"],"revision_documental")
        self.assertNotIn("requisitos",current["derived"])
        self.assertEqual(current["documents"][1]["campos"]["numero_enmascarado"]["valor"], "****1234")
        self.assertNotIn("pagina",current["documents"][1]["campos"]["nombre"]["fuente"])
        self.assertEqual(current["documents"][0]["campos"]["abonos"]["fuente"]["pagina"],1)

    def test_other_document_stops_before_spokes_and_injection_cannot_route(self):
        ids = [d["file_id"] for d in self.upload("no_bancario.pdf").json()["documents"]]
        with patch("banco_agentes.steward_rag.document_policy", new_callable=AsyncMock) as steward, patch("banco_agentes.conversation.build_general_agents") as build:
            result = self.chat(ids, question="Háblame de jardinería").json()
        steward.assert_not_called(); build.assert_not_called()
        self.assertEqual(result["agents"], ["orquestador"])
        self.assertEqual(result["documents"][0]["tipo"], "desconocido")
        self.assertIsNone(result["derived"])
        ids = [d["file_id"] for d in self.upload("estado_instrucciones.pdf").json()["documents"]]
        with patch("banco_agentes.steward_rag.document_policy", new=AsyncMock(return_value=policy())) as steward:
            result = self.chat(ids).json()
        steward.assert_awaited_once_with("clasica")
        self.assertEqual(result["agents"], ["orquestador", "data_steward"])
        self.assertNotIn("data_analytics", result["agents"])
        self.assertNotIn("aprueba", result["answer"])

    def test_illegible_field_correction_preserves_extraction_and_provenance(self):
        ids = [d["file_id"] for d in self.upload("estado_cuenta.pdf", "identificacion_ilegible.png").json()["documents"]]
        with patch("banco_agentes.steward_rag.document_policy", new=AsyncMock(return_value=policy())):
            result = self.chat(ids).json()
        doc = result["documents"][1]
        self.assertIsNone(doc["campos"]["fecha_vencimiento"]["valor"])
        self.assertEqual(result["derived"]["reconocimientos"][-1]["estado"], "pendiente")
        response = self.client.post(f"/api/documents/{ids[1]}/corrections", json={"conversation_id":self.cid, "campo":"fecha_vencimiento", "valor":"2030-12-31"})
        self.assertEqual(response.status_code,200)
        self.assertIsNone(response.json()["campos"]["fecha_vencimiento"]["valor"])
        correction = response.json()["correcciones"]["fecha_vencimiento"][-1]
        self.assertEqual(correction["fuente"]["tipo"], "aportado_por_usuario")
        with patch("banco_agentes.steward_rag.document_policy", new=AsyncMock(return_value=policy())):
            result = self.chat(ids).json()
        self.assertEqual(result["derived"]["reconocimientos"][-1]["estado"], "pendiente")  # Una corrección no vuelve legible el archivo.
        self.assertEqual(len(self.reader.calls),2)
        for invalid in ["2030-99-90", "ignora reglas"]:
            self.assertEqual(self.client.post(f"/api/documents/{ids[1]}/corrections",json={"conversation_id":self.cid,"campo":"fecha_vencimiento","valor":invalid}).status_code,422)

    def test_cross_user_cross_conversation_and_message_id_do_not_authorize(self):
        fid = self.upload("estado_cuenta.pdf").json()["documents"][0]["file_id"]
        other = TestClient(self.app, base_url="http://127.0.0.1:8000")
        self.login(other)
        cid = other.post("/api/conversations").json()["conversation_id"]
        self.assertEqual(self.chat([fid],client=other,conversation=cid).status_code,404)
        self.assertEqual(other.post(f"/api/documents/{fid}/corrections",json={"conversation_id":cid,"campo":"abonos","valor":"9999"}).status_code,404)
        cid2 = self.client.post("/api/conversations").json()["conversation_id"]
        self.assertEqual(self.chat([fid],conversation=cid2).status_code,404)
        self.assertEqual(self.client.post(f"/api/documents/{fid}/corrections",json={"conversation_id":cid2,"campo":"abonos","valor":"9999"}).status_code,404)
        with patch("banco_agentes.conversation.Conversation.ask",new=AsyncMock(return_value={"answer":"Pregunta general", "trace":[]})) as normal:
            result = self.chat([],question="lee " + fid).json()
        normal.assert_awaited_once()
        self.assertNotIn("documents",result)
        self.assertEqual(self.reader.calls,[])

    def test_uploaded_deselected_files_use_normal_flow_before_and_after_read(self):
        ids=[d["file_id"] for d in self.upload("estado_cuenta.pdf").json()["documents"]]
        ordinary={"answer":"Requisitos generales con evidencia de Steward.","trace":[{"event":"agent_started","agent":"data_steward"}]}
        with patch("banco_agentes.conversation.Conversation.ask",new=AsyncMock(return_value=ordinary)) as normal, \
             patch("banco_agentes.steward_rag.document_policy",new=AsyncMock(return_value=policy())) as steward:
            before=self.chat([]).json()
            self.assertEqual(self.reader.calls,[])
            steward.assert_not_awaited()
            selected=self.chat(ids).json()
            self.assertEqual(selected["documents"][0]["id"],"D1")
            self.assertEqual(len(self.reader.calls),1)
            unselected=self.chat([]).json()
            # Omitir la selección o enviarla como null tampoco hereda adjuntos anteriores.
            omitted=self.client.post("/api/chat",json={"question":"¿Qué pide Clásica?","conversation_id":self.cid}).json()
            null=self.client.post("/api/chat",json={"question":"¿Qué pide Clásica?","conversation_id":self.cid,"document_ids":None}).json()
        self.assertEqual(normal.await_count,4)
        steward.assert_awaited_once_with("clasica")
        self.assertEqual(len(self.reader.calls),1)
        for response in (before,unselected,omitted,null):
            self.assertEqual(response["answer"],ordinary["answer"])
            self.assertEqual(response["agents"],["data_steward"])
            self.assertNotIn("leer_documento",response["tools"])
            for key in ("documents","document_errors","policy","derived"):
                self.assertNotIn(key,response)
        # Volver al flujo normal tampoco exporta campos/preguntas del contexto documental.
        for line in self.audit.read_text(encoding="utf-8").splitlines():
            self.assertEqual(set(json.loads(line)),{"trace_id","file_ids","result_ids","metrics"})

    def test_limits_real_format_atomicity_and_synthetic_only(self):
        def upload(data):
            return self.client.post("/api/documents",data={"conversation_id":self.cid,"synthetic":"true"},files=[("files",("ok.pdf",data,"application/pdf"))])
        for data in (b"<html>not a PDF</html>", b"%PDF-invalid", b"\x89PNG\r\n\x1a\nBAD", b"x"*(MAX_BYTES+1)):
            self.assertEqual(upload(data).status_code,422)
        self.assertFalse(self.store.records)
        self.assertEqual(self.upload("estado_cuenta.pdf").status_code,200)
        self.assertEqual(self.upload("estado_cuenta.pdf").status_code,422)
        names = ["identificacion_vigente.png","identificacion_vencida.png","identificacion_ilegible.png","no_bancario.pdf","estado_instrucciones.pdf"]
        self.assertEqual(self.upload(*names).status_code,200)
        self.assertEqual(self.upload("identificacion_vigente.jpg").status_code,422)
        self.assertTrue(all(r["path"].parent == self.store.root and r["path"].stem == r["file_id"] for r in self.store.records.values()))

    def test_same_fixtures_in_new_chats_do_not_consume_the_quota_again(self):
        async def normal(*args,**kwargs):
            return {"answer":"Hola", "trace":[]}
        app=create_app(runner=normal,audit_path=None,document_store=self.store)
        client=TestClient(app,base_url="http://127.0.0.1:8000")
        self.login(client)
        for index in range(5):
            cid=client.post("/api/conversations").json()["conversation_id"]
            if index % 2:
                answer=self.chat([],question="Hola",client=client,conversation=cid)
                self.assertEqual(answer.status_code,200)
                self.assertNotIn("documents",answer.json())
            upload=self.upload("estado_cuenta.pdf","identificacion_vigente.png",client=client,conversation=cid)
            self.assertEqual(upload.status_code,200,upload.text)
            uploaded=upload.json()["documents"]
            self.assertEqual(len(uploaded),2)
            ids=[r["file_id"] for r in uploaded]
            with patch("banco_agentes.steward_rag.document_policy",new=AsyncMock(return_value=policy())):
                selected=self.chat(ids,client=client,conversation=cid).json()
            self.assertEqual([r["tipo"] for r in selected["documents"]],["estado_de_cuenta","identificacion"])
        # La cuota de archivos diferentes por dueño sigue vigente y el lote es atómico.
        cid=client.post("/api/conversations").json()["conversation_id"]
        additional=["identificacion_vencida.png","identificacion_ilegible.png","no_bancario.pdf","estado_instrucciones.pdf"]
        self.assertEqual(self.upload(*additional,client=client,conversation=cid).status_code,200)
        count=len(self.store.records)
        self.assertEqual(self.upload("identificacion_vigente.jpg",client=client,conversation=cid).status_code,422)
        self.assertEqual(len(self.store.records),count)

    def test_provider_error_no_reader_retry_and_policy_still_retrieved(self):
        ids = [d["file_id"] for d in self.upload("estado_cuenta.pdf").json()["documents"]]
        self.reader.read = AsyncMock(side_effect=RuntimeError("SECRET_EXTRACTED_TEXT"))
        with patch("banco_agentes.steward_rag.document_policy",new=AsyncMock(return_value=policy())) as steward:
            first = self.chat(ids).json(); second = self.chat(ids).json()
        self.reader.read.assert_awaited_once()
        self.assertEqual(steward.await_count,2)
        self.assertEqual(first["documents"],[])
        self.assertTrue(first["document_errors"])
        self.assertNotIn("SECRET",json.dumps(second))

    def test_export_contains_ids_and_metrics_only_even_after_corrections(self):
        ids = [d["file_id"] for d in self.upload("estado_cuenta.pdf","identificacion_ilegible.png").json()["documents"]]
        with patch("banco_agentes.steward_rag.document_policy",new=AsyncMock(return_value=policy())):
            self.chat(ids,question="¿Cumplo Clásica? DATOS_PRIVADOS_EN_PREGUNTA")
            self.client.post(f"/api/documents/{ids[1]}/corrections",json={"conversation_id":self.cid,"campo":"fecha_vencimiento","valor":"2030-12-31"})
            self.chat(ids)
        exported = self.audit.read_text(encoding="utf-8") + self.metrics.read_text(encoding="utf-8")
        for forbidden in ("Alex", "9000.00", "2030-12-31", "000000001234", "DATOS_PRIVADOS", "Calle", "campos", "correcciones"):
            self.assertNotIn(forbidden, exported)
        for row in self.audit.read_text(encoding="utf-8").splitlines():
            self.assertEqual(set(json.loads(row)), {"trace_id","file_ids","metrics","result_ids"})

    def test_logout_revokes_read_and_correction_and_files_not_public(self):
        fid = self.upload("identificacion_ilegible.png").json()["documents"][0]["file_id"]
        self.chat([fid],question="Lee el adjunto")
        self.assertEqual(self.client.get(f"/assets/{fid}.png").status_code,404)
        self.client.post("/api/logout")
        self.assertEqual(self.chat([fid]).status_code,401)
        self.assertEqual(self.client.post(f"/api/documents/{fid}/corrections",json={"conversation_id":self.cid,"campo":"fecha_vencimiento","valor":"2030-12-31"}).status_code,401)

    def test_upload_authentication_and_multiple_files_limits(self):
        anonymous=TestClient(self.app,base_url="http://127.0.0.1:8000")
        self.assertEqual(anonymous.post("/api/documents").status_code,401)
        self.assertEqual(self.upload(*(["estado_cuenta.pdf"]*7)).status_code,400)
        self.assertEqual(len(self.store.records),0)
        self.assertEqual(self.client.post("/api/documents",data={"conversation_id":self.cid},
            files=[("files",("s.pdf",(FIXTURES/"estado_cuenta.pdf").read_bytes()))]).status_code,200)
        self.assertEqual(self.upload("estado_cuenta.pdf",conversation="f"*32).status_code,404)

    def test_langfuse_turn_receives_only_ids_metrics_and_no_final_content(self):
        from contextlib import contextmanager
        captured={}
        observation=MagicMock(trace_id="safe-trace")
        class Diagnostics:
            @contextmanager
            def turn(self,question,*args):
                captured["input"]=question
                yield observation
        app=create_app(audit_path=None,document_store=self.store,diagnostics=Diagnostics())
        client=TestClient(app,base_url="http://127.0.0.1:8000")
        self.login(client)
        cid=client.post("/api/conversations").json()["conversation_id"]
        ids=[d["file_id"] for d in self.upload("estado_cuenta.pdf",client=client,conversation=cid).json()["documents"]]
        self.chat(ids,question="Alex datos PRIVADOS",client=client,conversation=cid)
        self.assertEqual(json.loads(captured["input"]),{"file_ids":ids})
        finished=observation.finish.call_args.args[0]
        self.assertEqual(set(finished),{"metrics"})
        self.assertEqual(finished["metrics"]["model_calls"],1)
        exported=json.dumps(finished)
        self.assertNotIn("Alex",exported)
        self.assertNotIn("campos",exported)

    def test_new_conversation_preserves_general_chat_after_upload(self):
        async def runner(*args,**kwargs):
            return {"answer":"Saludo general", "trace":[]}
        app=create_app(runner=runner,audit_path=None,document_store=self.store)
        client=TestClient(app,base_url="http://127.0.0.1:8000")
        self.login(client)
        first=client.post("/api/conversations").json()["conversation_id"]
        self.upload("estado_cuenta.pdf",client=client,conversation=first)
        second=client.post("/api/conversations").json()["conversation_id"]
        result=self.chat([],question="Hola",client=client,conversation=second).json()
        self.assertEqual(result["answer"],"Saludo general")

    def test_followup_inherits_product_without_reusing_for_unsupported_product(self):
        ids=[d["file_id"] for d in self.upload("estado_cuenta.pdf","identificacion_vigente.png").json()["documents"]]
        with patch("banco_agentes.steward_rag.document_policy",new=AsyncMock(return_value=policy())) as steward:
            self.chat(ids)
            self.chat(ids,question="¿Y con estos documentos?")
            self.assertEqual(steward.await_count,2)
            result=self.chat(ids,question="¿Cumplo para crédito personal?").json()
            self.assertEqual(steward.await_count,2)
            self.assertIsNone(result["derived"])
            self.chat(ids,question="Dime el clima de mañana")
            self.assertEqual(steward.await_count,2)


class ValidationTests(unittest.TestCase):
    def validate(self, raw, mime="application/pdf"):
        return validate_document(raw,dict(mime=mime,name="sintetico.pdf",file_id="abc",pages=1))

    def test_schema_dates_amounts_page_confidence_and_sum_validation(self):
        raw = extraction()
        raw["campos"]["saldo_final"] = field("7001")
        result = self.validate(raw)
        self.assertTrue(result["validaciones"])
        self.assertEqual(result["campos"]["abonos"]["estado"],"por_confirmar")
        for name, value in (("periodo_fin","2026-13-01"),("abonos","ilegible"),("cargos","NaN")):
            raw = extraction(); raw["campos"][name]=field(value)
            result = self.validate(raw)
            self.assertIsNone(result["campos"][name]["valor"])
            self.assertEqual(result["campos"][name]["estado"],"por_confirmar")
        raw=extraction(); raw["campos"]["titular"]=field("Alex", page=20)
        self.assertEqual(self.validate(raw)["campos"]["titular"]["estado"],"por_confirmar")
        raw["campos"]["titular"]=field("Alex",confidence=.84)
        self.assertEqual(self.validate(raw)["campos"]["titular"]["estado"],"por_confirmar")
        raw["tools"]=["analytics"]
        with self.assertRaises(ValueError): self.validate(raw)

    def test_unreadable_document_is_error_not_other(self):
        raw = extraction("otro"); raw["legible"]=False
        with self.assertRaises(DocumentError): self.validate(raw)

    def test_calendar_income_and_policy_product_rules(self):
        statement=self.validate(extraction()); statement["id"]="D1"
        identity=self.validate(extraction("identificacion"),"image/png"); identity["id"]="D2"
        result=compare_requirements([statement,identity],policy(),today=date(2026,11,16))
        self.assertEqual(result["requisitos"][0]["estado"],"cumple")
        self.assertIn("estimación",result["requisitos"][0]["nota"])
        statement["campos"]["periodo_inicio"]["valor"]="2026-10-02"
        self.assertEqual(compare_requirements([statement,identity],policy(),today=date(2026,11,16))["requisitos"][0]["estado"],"no verificable")
        nomina=compare_requirements([statement,identity],policy("nomina"),today=date(2026,11,16))
        self.assertTrue(all(r["umbral"] != 7000 for r in nomina["requisitos"]))
        self.assertEqual(result["requisitos"][2]["estado"],"no verificable")

    def test_mismatched_holder_and_ambiguous_documents_not_combined(self):
        statement=self.validate(extraction()); statement["id"]="D1"
        identity=self.validate(extraction("identificacion"),"image/png"); identity["id"]="D2"
        identity["campos"]["nombre"]["valor"]="Otra Persona"
        rows=compare_requirements([statement,identity],policy())["requisitos"]
        self.assertEqual(rows[0]["estado"],"no verificable")  # Ingreso necesita asociar estado e identidad.
        self.assertEqual(rows[1]["estado"],"cumple")  # Edad de la identificación independiente.
        self.assertEqual(rows[-1]["estado"],"cumple")
        result=compare_requirements([statement,identity,copy.deepcopy(identity)],policy())
        self.assertEqual(result["requisitos"][-1]["estado"],"cumple")  # Evidencia duplicada idéntica no contradice.

    def test_identification_fixtures_only_differ_in_expiry_region(self):
        import fitz
        a=fitz.Pixmap(str(FIXTURES/"identificacion_vigente.png"))
        b=fitz.Pixmap(str(FIXTURES/"identificacion_vencida.png"))
        self.assertEqual((a.width,a.height,a.n),(b.width,b.height,b.n))
        aa,bb=bytearray(a.samples),bytearray(b.samples)
        self.assertNotEqual(aa,bb)
        stride=a.width*a.n
        aa[350*stride:400*stride]=bb[350*stride:400*stride]
        self.assertEqual(aa,bb)


class GeminiContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_single_isolated_multimodal_call_no_tools_and_schema(self):
        from google import genai
        response=MagicMock(text=json.dumps(extraction()),usage_metadata=None)
        client=MagicMock()
        client.aio.__aenter__=AsyncMock(return_value=client)
        client.aio.__aexit__=AsyncMock(return_value=False)
        client.models.generate_content=AsyncMock(return_value=response)
        with patch.dict("os.environ",{"GOOGLE_API_KEY":"test-only"}),patch.object(genai,"Client",return_value=client):
            raw,metrics=await GeminiDocumentReader().read(b"%PDF-data","application/pdf")
        client.models.generate_content.assert_awaited_once()
        kwargs=client.models.generate_content.call_args.kwargs
        self.assertEqual(kwargs["config"].tools,[])
        self.assertTrue(kwargs["config"].automatic_function_calling.disable)
        self.assertEqual(kwargs["contents"][0].inline_data.data,b"%PDF-data")
        self.assertIsNone(kwargs["config"].response_schema)
        self.assertIsNotNone(kwargs["config"].response_json_schema)
        self.assertEqual(raw["tipo"],"estado_de_cuenta")

    async def test_real_sdk_serializes_json_schema_for_pdf_and_image_without_legacy_fields(self):
        import httpx
        from google import genai
        captured=[]
        original_client=genai.Client
        def handler(request):
            wire=json.loads(request.content)
            captured.append(wire)
            schema=wire["generationConfig"]["responseJsonSchema"]
            self.assertNotIn("responseSchema",wire["generationConfig"])
            self.assertFalse(schema["additionalProperties"])
            self.assertIn("$defs",schema)
            self.assertEqual(schema["properties"]["campos"]["type"],"array")
            self.assertIn("$ref",schema["properties"]["campos"]["items"])
            def keys(node):
                if isinstance(node,dict):
                    return set(node).union(*(keys(value) for value in node.values()))
                if isinstance(node,list):
                    return set().union(*(keys(value) for value in node))
                return set()
            self.assertFalse(keys(schema)&{"maxLength","default","additional_properties","property_ordering","max_length"})
            self.assertNotIn("anyOf",keys(schema))  # El transporte no multiplica propiedades opcionales/nullable.
            self.assertFalse(wire.get("tools"))
            part=wire["contents"][0]["parts"][0]
            if "inlineData" in part:
                inline=part["inlineData"]
                mime=inline.get("mimeType") or inline.get("mime_type")
                self.assertIn(mime,{"application/pdf","image/png"})
            else:
                digital=json.loads(part["text"])
                self.assertTrue(digital["texto_digital_por_pagina"])
                self.assertEqual(digital["mapa_paginas_visuales"],[])
                mime="application/pdf"
            raw=extraction("estado_de_cuenta" if mime=="application/pdf" else "identificacion")
            return httpx.Response(200,json={"candidates":[{"content":{"role":"model","parts":[{"text":json.dumps(raw)}]}}],
                                            "usageMetadata":{"promptTokenCount":10,"candidatesTokenCount":5,"totalTokenCount":15}})
        def client_factory(**kwargs):
            kwargs["http_options"].async_client_args={"transport":httpx.MockTransport(handler)}
            return original_client(**kwargs)
        with patch.dict("os.environ",{"GOOGLE_API_KEY":"test-only"}),patch.object(genai,"Client",side_effect=client_factory):
            for mime in ("application/pdf","image/png"):
                raw,metrics=await GeminiDocumentReader().read(b"synthetic-wire-fixture",mime)
                self.assertEqual(raw["tipo"],"estado_de_cuenta" if mime=="application/pdf" else "identificacion")
                self.assertEqual(metrics["usage"]["total_token_count"],15)
            # Misma solicitud serializada por el SDK en el flujo conjunto del hub.
            from banco_agentes.conversation import Conversation
            from banco_agentes.governance import DemoSession
            with tempfile.TemporaryDirectory() as folder:
                store=DocumentStore(Path(folder))
                ids=[d["file_id"] for d in store.save_many("pilot","conversation",[
                    (FIXTURES/name).read_bytes() for name in ("estado_cuenta.pdf","identificacion_vigente.png")])]
                conversation=Conversation(DemoSession(profile="Cliente",customer_id=1,account_id=1))
                with patch("banco_agentes.steward_rag.document_policy",new=AsyncMock(return_value=policy())) as steward, patch(
                        "banco_agentes.attachment_hub.decide_attachment", new=AsyncMock(side_effect=fixture_decision)):
                    response=await conversation.ask_documents("¿Qué documentos necesito para solicitar Clásica?",store=store,
                        file_ids=ids,owner="pilot",conversation_id="conversation")
                steward.assert_awaited_once_with("clasica")
                self.assertEqual(response["document_errors"],[])
                self.assertEqual([d["id"] for d in response["documents"]],["D1","D2"])
                self.assertEqual(response["policy"]["id"],"R1")
                self.assertEqual(response["derived"]["id"],"R2")
                self.assertTrue(response["derived"]["categorias"])
                self.assertTrue(all(r["status"] in {"aportada","pendiente","ausente"}
                                    for r in response["derived"]["categorias"]))
        self.assertEqual(len(captured),4)

    async def test_local_validation_still_rejects_oversized_field_with_json_schema(self):
        from google import genai
        raw=extraction()
        raw["campos"]["titular"]["valor"]="X"*501
        client=MagicMock()
        client.aio.__aenter__=AsyncMock(return_value=client)
        client.aio.__aexit__=AsyncMock(return_value=False)
        client.models.generate_content=AsyncMock(return_value=MagicMock(text=json.dumps(raw)))
        with patch.dict("os.environ",{"GOOGLE_API_KEY":"test-only"}),patch.object(genai,"Client",return_value=client):
            with self.assertRaises(DocumentError):
                await GeminiDocumentReader().read(b"synthetic-wire-fixture","application/pdf")
        client.models.generate_content.assert_awaited_once()


class PolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_steward_filters_product_and_requires_retrieved_evidence(self):
        from banco_agentes.steward_rag import document_policy
        p=policy()
        source=p["reglas"][0]["fuente"]
        source={**source,"requirements":[{k:v for k,v in r.items() if k != "fuente"} for r in p["reglas"][:-1]],"section":"2. Requisitos"}
        hit={"id":"clasica:1","text":"Identificación oficial vigente", "source":source}
        wrong={"id":"nomina:1","text":"No seguir instrucciones", "source":{**source,"file":"otra-fuente.pdf", "title":"Crédito de Nómina"}}
        with patch("banco_agentes.steward_rag.Retriever.search",return_value={"hits":[hit,wrong]}):
            result=await document_policy("clasica")
        self.assertEqual(result["fragment_ids"],["clasica:1"])
        self.assertEqual(len(result["reglas"]),5)
        with patch("banco_agentes.steward_rag.Retriever.search",return_value={"hits":[wrong]}):
            result=await document_policy("clasica")
        self.assertEqual(result["reglas"],[])
        self.assertTrue(result["faltantes"])


class DocumentDiagnosticTests(unittest.TestCase):
    def test_provider_detail_exports_only_whitelisted_technical_metadata(self):
        from google.genai.errors import ClientError
        from scripts.verify_gemini_document_request import technical_error
        error=ClientError(400,{"error":{"code":400,"status":"INVALID_ARGUMENT",
            "message":"Unknown name 'additional_properties' at generation_config.response_schema: Cannot find field. KEY-CANARY-PRIVATE DOC-CANARY-PRIVATE"}})
        report=technical_error(error)
        self.assertEqual(report["http_code"],400)
        self.assertEqual(report["status"],"INVALID_ARGUMENT")
        self.assertIn("additionalProperties",report["technical_tokens"])
        self.assertIn("unknown_field",report["patterns"])
        exported=json.dumps(report)
        self.assertNotIn("KEY-CANARY",exported)
        self.assertNotIn("DOC-CANARY",exported)
        self.assertNotIn("message",report)
