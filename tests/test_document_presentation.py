"""Presentación de Nómina con fixtures; lector y Steward dobles, sin red."""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from banco_agentes.conversation import Conversation
from banco_agentes.document_credit import compare_requirements
from banco_agentes.document_presentation import credit_document_view, PRELIMINARY
from banco_agentes.documents import DocumentStore
from banco_agentes.governance import DemoSession
from test_documents import FIXTURES, FixtureReader, policy, fixture_decision


async def nomina_response(*, unreadable=False, correction=None):
    with tempfile.TemporaryDirectory() as folder:
        store=DocumentStore(Path(folder),reader=FixtureReader())
        identification="identificacion_ilegible.png" if unreadable else "identificacion_vigente.png"
        ids=[d["file_id"] for d in store.save_many("owner","conversation",[
            (FIXTURES/name).read_bytes() for name in ("estado_cuenta.pdf",identification)])]
        if correction:
            file_id=ids[1] if correction=="used" else ids[0]
            await store.leer_documento(file_id,"owner","conversation")
            field="fecha_vencimiento" if correction=="used" else "saldo_inicial"
            store.records[file_id]["result"]["campos"][field]["estado"]="por_confirmar"
            store.correct(file_id,"owner","conversation",field,"2030-12-31" if correction=="used" else "1000.00")
        p=policy("nomina")
        source=p["reglas"][-1]["fuente"]
        p["reglas"].extend({"campo":name,"fuente":source} for name in (
            "identificacion_oficial","recepcion_nomina_o_pension","telefono_actualizado"))
        p["fuentes"]=[{"label":source["title"],"location":"Página 1 · 4. Requisitos",
                       "details":{"Versión":source["version"],"Fecha de consulta":source["consulted_at"],
                                  "Tipo de documento":"Resumen académico","Archivo":source["file"]}}]
        # Pruebas unitarias del presentador legado; el chat ya no llama al evaluador.
        documents=[]
        for i,file_id in enumerate(ids,1):
            d=await store.leer_documento(file_id,"owner","conversation")
            d["id"]=f"D{i}"
            documents.append(d)
        derived=compare_requirements(documents,p)
        view=credit_document_view(documents,derived)
        return {"documents":documents,"policy":p,"derived":derived,"document_view":view,"answer":view["summary"]}



class LegacyDocumentPresentationTests(unittest.IsolatedAsyncioTestCase):
    async def test_nomina_both_synthetic_documents_brief_summary_preserves_detailed_evaluation(self):
        response=await nomina_response()
        self.assertEqual([d["tipo"] for d in response["documents"]],["estado_de_cuenta","identificacion"])
        derived=response["derived"]
        before=copy.deepcopy(derived)
        view=credit_document_view(response["documents"],derived)
        self.assertEqual(derived,before)
        self.assertEqual(derived["requisitos"],compare_requirements(response["documents"],response["policy"])["requisitos"])
        self.assertEqual(response["answer"],view["summary"])
        self.assertIn("cumples el rango de edad general y la vigencia",response["answer"])
        self.assertIn("No se pudo comprobar el ingreso mensual",response["answer"])
        self.assertNotIn("No cumples",response["answer"])
        self.assertEqual(response["answer"].count("rango de edad general"),1)
        self.assertEqual(response["answer"].count(PRELIMINARY),1)
        self.assertNotIn("confirmaste",response["answer"])
        general_age=[g for g in view["requirements"] if g["name"]=="edad" and g["condition"]=="general"]
        self.assertEqual(len(general_age),1)
        self.assertEqual(len(general_age[0]["indices"]),2)
        self.assertEqual(view["pending_fields"],[])
        generic=[n for n in view["notes"] if n["text"].startswith("No hay evidencia suficiente")]
        self.assertEqual(len(generic),1)
        self.assertEqual(len(generic[0]["requirements"]),2)  # El tipo de identificación ausente tiene motivo propio.
        self.assertLess(len(response["answer"].split()),115)
        clasica=compare_requirements(response["documents"],policy("clasica"))
        estimated=credit_document_view(response["documents"],clasica)
        self.assertIn("el ingreso mensual estimado por los abonos",estimated["summary"])

    async def test_failed_age_bound_and_pending_field_are_never_presented_as_passed(self):
        response=await nomina_response(unreadable=True)
        self.assertIn("Queda por confirmar la vigencia",response["answer"])
        self.assertEqual(response["document_view"]["pending_fields"],
            [{"file_id":response["documents"][1]["file_id"],"campo":"fecha_vencimiento"}])
        derived=copy.deepcopy(response["derived"])
        general=[r for r in derived["requisitos"] if r["regla"]=="edad_meses" and r["condicion"]=="general"]
        general[0]["estado"]="no cumple"
        view=credit_document_view(response["documents"],derived)
        self.assertIn("No cumples el rango de edad general",view["summary"])
        self.assertNotIn("cumples el rango de edad general y",view["summary"])

    async def test_only_used_user_corrections_are_mentioned(self):
        unused=await nomina_response(correction="unused")
        self.assertTrue(unused["documents"][0]["correcciones"])
        self.assertEqual(unused["document_view"]["used_corrections"],[])
        self.assertNotIn("confirmaste",unused["answer"])
        used=await nomina_response(unreadable=True,correction="used")
        self.assertIn("datos que confirmaste tú",used["answer"])
        self.assertEqual(used["document_view"]["used_corrections"],
            [{"file_id":used["documents"][1]["file_id"],"campo":"fecha_vencimiento"}])
        self.assertEqual(used["document_view"]["pending_fields"],[])
