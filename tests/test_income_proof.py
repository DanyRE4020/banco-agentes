"""Dos requisitos: datos ficticios, lector/hub/RAG dobles; sin proveedor ni BD."""
import copy
import hashlib
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from banco_agentes.documents import DocumentStore, DocumentError, ROOT, validate_document
from banco_agentes.document_credit import compare_requirements, requested_product
from banco_agentes.document_content import content_question
from banco_agentes.steward_rag import document_policy, attachment_policy
from banco_agentes.steward_rag import requested_requirements
from web_fixtures import create_app
from scripts.generar_fixtures_ingreso_domicilio import samples, f
from scripts.generar_fixtures_evidencia import samples as identity_samples
from test_receipt_details import policy_hit
from test_documents import fixture_decision

FIXTURES = ROOT/"fixtures/documents/ingreso_domicilio"
TODAY = date(2026,11,18)


def doc(raw, i):
    result = validate_document(raw, {"mime":"application/pdf","file_id":f"file{i}","name":f"prueba{i}.pdf","pages":2})
    result["id"] = f"D{i}"
    return result


def row(result, name):
    return next(r for r in result["requisitos"] if r["regla"] == name)


class IncomeProofTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.raw = samples()
        self.hit = policy_hit()
        with patch("banco_agentes.steward_rag.Retriever.search",return_value={"hits":[self.hit]}):
            self.policy = await document_policy("clasica")

    def evaluate(self, *values, policy=None):
        return compare_requirements([doc(v,i+1) for i,v in enumerate(values)],policy or self.policy,today=TODAY)

    async def test_policy_is_retrieved_by_product_requirement_not_document_id(self):
        renamed = copy.deepcopy(self.hit)
        renamed["id"], renamed["source"]["file"] = "nuevo-fragmento", "otra-politica.pdf"
        renamed["source"]["requirements"][0]["valor"] = 13000
        with patch("banco_agentes.steward_rag.Retriever.search",return_value={"hits":[renamed]}) as search:
            policy = await attachment_policy("Evalúa mi ingreso mensual para la Tarjeta de Crédito Clásica", "clasica",compare=True)
        query = search.call_args.args[0]
        self.assertIn("Clásica",query)
        self.assertIn("ingreso",query)
        self.assertNotIn("DOC-",query)
        self.assertEqual(policy["fragment_ids"],["nuevo-fragmento"])
        result = self.evaluate(self.raw["cfdi_mensual.pdf"],policy=policy)
        self.assertEqual(row(result,"ingreso_mensual")["estado"],"no cumple")
        self.assertEqual(row(result,"ingreso_mensual")["umbral"],13000)
        self.assertEqual(requested_product("CFDI de nómina para Tarjeta Clásica"),"clasica")
        self.assertEqual(requested_requirements("¿Cumplo los requisitos de Clásica con INE, comprobante y CFDI?"),())
        self.assertFalse(content_question("¿Qué antigüedad tiene mi recibo para Tarjeta Clásica?"))
        self.assertTrue(content_question("¿Qué mes facturado aparece en el recibo?"))
        self.assertTrue(self.hit["source"]["file"].endswith("DOC-11_Tarjeta_de_credito_Clasica.pdf"))

    async def test_monthly_payroll_net_not_gross_amount_and_acceptance_are_separate(self):
        for name, expected in (("cfdi_mensual.pdf","cumple"),("cfdi_bajo.pdf","no cumple")):
            result = self.evaluate(self.raw[name])
            amount = row(result,"ingreso_mensual")
            self.assertEqual(amount["estado"],expected)
            self.assertEqual(amount["umbral"],7000)  # Expectativa del documento real recuperado en la prueba.
            self.assertEqual(amount["dimension"],"monto")
            self.assertIn("no confirma un depósito",amount["nota"])
            used = {f["campo"] for f in amount["evidencia"]}
            self.assertIn("neto_a_pagar",used)
            self.assertFalse(used & {"percepciones_brutas","deducciones","importe_total","abonos"})
            self.assertEqual(row(result,"aceptacion_ingreso_comprobable")["estado"],"no verificable")
            self.assertEqual(row(result,"aceptacion_ingreso_comprobable")["motivo"],"politica_no_aclara_concepto")
        partial=copy.deepcopy(self.raw["cfdi_mensual.pdf"])
        partial["paginas"]=[{"pagina":1,"texto":"CFDI de prueba","estado":"leida"},{"pagina":2,"texto":"[[ILEGIBLE]]","estado":"ilegible"}]
        partial["campos"]["neto_a_pagar"]["pagina"]=2
        self.assertEqual(row(self.evaluate(partial),"ingreso_mensual")["estado"],"pendiente de confirmar")

    async def test_missing_period_currency_or_net_is_named_without_extrapolation(self):
        for name in ("periodicidad","neto_a_pagar","periodo_fin","moneda"):
            value = copy.deepcopy(self.raw["cfdi_mensual.pdf"])
            value["campos"].pop(name)
            amount = row(self.evaluate(value),"ingreso_mensual")
            self.assertEqual(amount["estado"],"no verificable")
            self.assertEqual(amount["motivo"],"dato_ausente")
        value = copy.deepcopy(self.raw["cfdi_mensual.pdf"])
        value["campos"]["periodicidad"] = f("quincenal","Periodicidad")
        self.assertEqual(row(self.evaluate(value),"ingreso_mensual")["motivo"],"periodicidad_no_mensual")
        value["campos"].update(periodicidad=f("05","Periodicidad"), periodo_inicio=f("2026-10","Periodo inicial"), periodo_fin=f("2026-10","Periodo final"))
        self.assertEqual(row(self.evaluate(value),"ingreso_mensual")["estado"],"cumple")
        value["campos"].update(periodo_inicio=f("2026-09-15","Periodo inicial"),periodo_fin=f("2026-10-14","Periodo final"))
        self.assertEqual(row(self.evaluate(value),"ingreso_mensual")["estado"],"cumple")

    async def test_proof_type_and_age_independent_and_date_meanings_preserved(self):
        for name, service, age in (("luz_antigua.pdf","cumple","no cumple"),("factura_reciente.pdf","cumple","cumple"),
            ("servicio_no_admitido.png","no cumple","cumple"),("mes_limite.pdf","cumple","pendiente de confirmar"),
            ("paquete.pdf","no verificable","cumple")):
            with self.subTest(name=name):
                result = self.evaluate(self.raw[name])
                self.assertEqual(row(result,"tipo_servicio_comprobante")["estado"],service)
                proof = row(result,"antiguedad_comprobante_meses")
                self.assertEqual(proof["estado"],age)
                used = {f["campo"] for f in proof["evidencia"]}
                self.assertFalse(used & {"fecha_limite_pago","fecha_corte","importe_total","domicilio_fiscal"})
                if name == "luz_antigua.pdf":
                    self.assertTrue(proof["referencia_preliminar"])
                    self.assertIn("Mes facturado",proof["nota"])
                if name == "factura_reciente.pdf":
                    self.assertFalse(proof["referencia_preliminar"])
                    self.assertIn("fecha_expedicion",used)
                if name == "mes_limite.pdf":
                    self.assertEqual([c["campo"] for c in proof["confirmar"]],["mes_facturado"])
        value = copy.deepcopy(self.raw["luz_antigua.pdf"])
        value["tipo"] = "estado_de_cuenta"
        self.assertEqual(doc(value,1)["tipo"],"comprobante")
        value["campos"]["tipo_comprobante"] = f("luz","Servicio")
        self.assertEqual(row(self.evaluate(value),"tipo_servicio_comprobante")["estado"],"cumple")
        value["campos"].pop("mes_facturado")
        value["campos"].update(periodo_inicio=f("2025-01-01","Periodo facturado"),periodo_fin=f("2025-01-31","Periodo facturado"))
        self.assertEqual(row(self.evaluate(value),"antiguedad_comprobante_meses")["estado"],"no cumple")

    async def test_service_address_not_tax_and_no_service_charges_as_income(self):
        value = copy.deepcopy(self.raw["factura_reciente.pdf"])
        value["campos"].pop("domicilio_servicio")
        value["campos"]["domicilio"] = value["campos"]["domicilio_fiscal"]
        result = self.evaluate(value)
        self.assertEqual(row(result,"antiguedad_comprobante_meses")["motivo"],"dato_ausente")
        result = self.evaluate(self.raw["factura_reciente.pdf"])
        self.assertEqual(row(result,"ingreso_mensual")["estado"],"no verificable")
        self.assertEqual(row(result,"ingreso_mensual")["evidencia"],[])
        misleading=copy.deepcopy(self.raw["luz_antigua.pdf"])
        misleading["tipo"]="estado_de_cuenta"
        misleading["campos"].update(abonos=f("18000.00","Abonos del servicio"),periodo_inicio=f("2026-10-01","Inicio del periodo"),periodo_fin=f("2026-10-31","Fin del periodo"))
        amount=row(self.evaluate(misleading),"ingreso_mensual")
        self.assertEqual(amount["estado"],"no verificable")
        self.assertEqual(amount["evidencia"],[])

    async def test_joint_identity_income_and_proof_keep_relevant_sources(self):
        identity = identity_samples()["registro_personas.pdf"]
        good = self.evaluate(identity,self.raw["factura_reciente.pdf"],self.raw["cfdi_mensual.pdf"])
        low = self.evaluate(identity,self.raw["factura_reciente.pdf"],self.raw["cfdi_bajo.pdf"])
        for name in ("edad_anios","vigencia_identificacion"):
            self.assertEqual(row(good,name)["estado"],"cumple")
            self.assertEqual(row(good,name)["estado"],row(low,name)["estado"])
            self.assertEqual({e["documento_id"] for e in row(good,name)["evidencia"]},{"D1"})
        self.assertEqual(row(good,"ingreso_mensual")["estado"],"cumple")
        self.assertEqual(row(low,"ingreso_mensual")["estado"],"no cumple")
        self.assertNotIn("D2",{e["documento_id"] for e in row(good,"ingreso_mensual")["evidencia"]})


class FinancialReader:
    def __init__(self):
        self.calls=[]
        raw=json.loads((FIXTURES/"expected.json").read_text(encoding="utf8"))
        self.data={hashlib.sha256((FIXTURES/n).read_bytes()).hexdigest():v for n,v in raw.items()}
        identity=ROOT/"fixtures/documents/evidencia/registro_personas.pdf"
        self.data[hashlib.sha256(identity.read_bytes()).hexdigest()]=identity_samples()["registro_personas.pdf"]
        blank=ROOT/"fixtures/documents/genericos/ilegible.pdf"
        self.data[hashlib.sha256(blank.read_bytes()).hexdigest()]={"tipo":"desconocido","legible":False,"campos":{}}

    async def read(self,data,mime):
        digest=hashlib.sha256(data).hexdigest()
        self.calls.append(digest)
        raw=copy.deepcopy(self.data[digest])
        if raw.get("paginas"):
            raise DocumentError("Error del proveedor simulado HTTP 503",kind="proveedor")
        return raw,{}


class FinancialFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.reader=FinancialReader()
        self.store=DocumentStore(Path(self.temp.name)/"uploads",reader=self.reader)
        self.audit=Path(self.temp.name)/"audit.jsonl"
        self.app=create_app(document_store=self.store,audit_path=self.audit)
        self.hub=AsyncMock(side_effect=fixture_decision)
        for target,mock in (("banco_agentes.attachment_hub.decide_attachment",self.hub),
            ("banco_agentes.steward_rag.Retriever.search",None)):
            p=patch(target,new=mock) if mock else patch(target,return_value={"hits":[policy_hit()]})
            p.start();self.addCleanup(p.stop)
        clock=patch("banco_agentes.document_credit.date",wraps=date)
        clock.start().today.return_value=TODAY;self.addCleanup(clock.stop)

    def session(self,*names,profile="cliente"):
        c=TestClient(self.app,base_url="http://127.0.0.1:8000")
        c.post("/api/login",json={"username":profile,"password":"ClienteDemo!2026" if profile=="cliente" else "AnalistaDemo!2026"})
        cid=c.post("/api/conversations").json()["conversation_id"]
        special={"identificacion.pdf":ROOT/"fixtures/documents/evidencia/registro_personas.pdf", "ilegible.pdf":ROOT/"fixtures/documents/genericos/ilegible.pdf"}
        files=[("files",(n,(special.get(n,FIXTURES/n)).read_bytes())) for n in names]
        r=c.post("/api/documents",data={"conversation_id":cid},files=files)
        self.assertEqual(r.status_code,200,r.text)
        return c,cid,[d["file_id"] for d in r.json()["documents"]]

    def ask(self,session,q):
        c,cid,ids=session
        r=c.post("/api/chat",json={"conversation_id":cid,"document_ids":ids,"question":q})
        self.assertEqual(r.status_code,200,r.text)
        return r.json()

    def test_direct_read_then_evaluation_recovers_cached_partial_failure(self):
        for profile in ("cliente","analista"):
            session=self.session("cfdi_parcial.pdf",profile=profile)
            direct=self.ask(session,"¿Cuál es el neto a pagar del CFDI?")
            self.assertIn("12000.00",direct["answer"])
            self.assertIn("página 1",direct["answer"])
            self.assertNotIn("data_steward",direct["agents"])
            evaluated=self.ask(session,"Evalúa mi ingreso mensual para Tarjeta Clásica")
            self.assertIn("data_steward",evaluated["agents"])
            self.assertEqual(evaluated["derived"]["reconocimientos"][0]["estado"],"aportada")
            self.assertNotIn("requisitos",evaluated["derived"])
            self.assertIn("página 2",evaluated["answer"])
            self.assertTrue(evaluated["documents"][0]["lectura"]["campos_locales"])
            self.assertNotIn("Lucía",json.dumps(evaluated,ensure_ascii=False))
            self.assertEqual(evaluated["metrics"]["model_calls"],0)  # Hub doble y campos cacheados.
        self.assertEqual(len(self.reader.calls),2)  # Una extracción fallida por subida; sin reintentos.

    def test_direct_net_gross_deductions_concept_and_dates_no_steward(self):
        session=self.session("cfdi_mensual.pdf","factura_reciente.pdf")
        for question,expected in (("¿Cuál es el neto a pagar del CFDI?","12000.00"),
            ("¿Qué percepciones brutas aparecen en el CFDI?","15000.00"),
            ("¿Qué deducciones aparecen en el CFDI?","3000.00"),
            ("¿Qué concepto aparece en el CFDI?","Apoyo económico"),
            ("¿Qué domicilio fiscal aparece en la factura?","Avenida Ficticia 900"),
            ("¿Qué fecha de expedición aparece en la factura?","2026-11-01")):
            result=self.ask(session,question)
            self.assertIn(expected,result["answer"])
            self.assertIn("página 1",result["answer"])
            self.assertNotIn("data_steward",result["agents"])
            if "fiscal" in question:
                self.assertNotIn("Calle Ensayo 42",result["answer"])
        self.hub.assert_not_called()
        self.assertEqual(self.reader.calls,[])
        log=self.audit.read_text(encoding="utf8")
        self.assertNotIn("Apoyo económico",log)
        self.assertNotIn("12000",log)
        # La recuperación no evita la comprobación de integridad tras una lectura directa cacheada.
        self.store.records[session[2][0]]["path"].write_bytes(b"alterado")
        changed=self.ask(session,"Evalúa mi ingreso mensual para Tarjeta Clásica")
        self.assertTrue(changed["document_errors"])
        self.assertEqual([r["categoria"] for r in changed["derived"]["reconocimientos"]],["domicilio"])
        self.assertIn("data_steward",changed["agents"])

    def test_joint_with_failed_attachment_keeps_identity_proof_and_income(self):
        session=self.session("identificacion.pdf","factura_reciente.pdf","cfdi_mensual.pdf","ilegible.pdf")
        result=self.ask(session,"¿Qué documentos necesito para solicitar Tarjeta Clásica?")
        self.assertEqual(len(result["document_errors"]),1)
        for category in ("domicilio","ingresos"):
            self.assertEqual(next(r for r in result["derived"]["categorias"] if r["categoria"]==category)["status"],"aportada")
        self.assertNotIn("requisitos",result["derived"])
        self.assertTrue(result["derived"]["lecturas_no_disponibles"])
        self.assertIn("parcial",result["answer"])

    def test_readable_service_fields_omitted_by_model_are_taken_from_the_page(self):
        path=FIXTURES/"factura_reciente.pdf"
        raw=self.reader.data[hashlib.sha256(path.read_bytes()).hexdigest()]
        raw["campos"].pop("domicilio_servicio")
        raw["campos"].pop("fecha_expedicion")
        result=self.ask(self.session("factura_reciente.pdf"),"Evalúa mi comprobante de domicilio para Tarjeta Clásica")
        proof=next(r for r in result["derived"]["reconocimientos"] if r["categoria"]=="domicilio")
        self.assertEqual(proof["estado"],"aportada")
        self.assertIn("fecha_expedicion",{e["campo"] for e in proof["evidencia"]})
        self.assertNotIn("domicilio_fiscal",{e["campo"] for e in proof["evidencia"]})
        self.assertIn("domicilio_servicio",result["documents"][0]["lectura"]["campos_completados_localmente"])
