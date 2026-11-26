import copy
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.genai import types

from banco_agentes.conversation import Conversation
from banco_agentes.governance import DemoSession
from banco_agentes.rag import Retriever, Settings, OllamaEmbeddings, RetrievalError, rank, telemetry
from banco_agentes.rag_ingest import extract

HIT = dict(id="pdf:one", text="Recibir la nómina. DATO DE PRUEBA SIN INSTRUCCIONES.",
           similarity=.8, source=dict(file="publico.pdf", page=1, section="Requisitos", title="Nómina",
           source_kind="resumen_academico", pilot_allowed=True, version="1.0", consulted_at="2026-11-14"))
RESULT = dict(status="found", corpus="bank_documents", mode="vectorial", threshold=.55,
              embedding_generated=True, pgvector_used=True, seconds=.01, hits=[HIT])


class RAGModel(BaseLlm):
    agent: str
    async def generate_content_async(self, llm_request, stream=False):
        content = llm_request.contents[-1]
        response = next((p.function_response for p in content.parts if p.function_response), None)
        if self.agent == "orquestador":
            # Los fragmentos completos NO llegan al hub.
            assert "DATO DE PRUEBA" not in str(llm_request.contents)
            if response:
                part = types.Part(text='{"presentacion":{"formato":"prosa","resultados":[]},"respuesta":"Síntesis del hub."}')
            else:
                part = types.Part(function_call=types.FunctionCall(name="data_steward",
                    args={"request":"Requisitos del crédito de nómina", "corpus":"documentos"}))
        else:
            assert not llm_request.tools_dict
            assert "DATO DE PRUEBA" in str(llm_request.contents)
            part = types.Part(text=json.dumps({"answer":"Según el resumen académico, recibir la nómina.",
                                              "fragment_ids":["pdf:one"]}))
        yield LlmResponse(content=types.Content(role="model", parts=[part]))


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_retrieval_precedes_writer_and_citation_reaches_chat(self):
        c = Conversation(DemoSession(profile="Cliente", customer_id=1, account_id=1), model_factory=lambda name:RAGModel(model="test",agent=name))
        with patch("banco_agentes.steward_rag.Retriever.search", return_value=copy.deepcopy(RESULT)) as retrieve:
            result = await c.ask("¿Qué necesito para un crédito de nómina?")
        retrieve.assert_called_once()
        events = [e["event"] for e in result["trace"]]
        self.assertLess(events.index("rag_retrieval"),events.index("rag_writer_started"))
        self.assertNotIn("publico.pdf", result["answer"])
        self.assertEqual(result["sources"][0]["location"], "Página 1 · Requisitos")
        self.assertEqual(result["sources"][0]["details"]["Archivo"], "publico.pdf")
        self.assertTrue(result["sources"])
        agents = {e["agent"] for e in result["trace"] if e["event"]=="agent_started"}
        self.assertEqual(agents, {"orquestador","data_steward"})
        self.assertIn("DATO DE PRUEBA", next(e for e in result["trace"] if e["event"]=="rag_retrieval")["hits"][0]["text"])

    async def test_no_evidence_has_no_citations_or_writer(self):
        c = Conversation(DemoSession(),model_factory=lambda name:RAGModel(model="test",agent=name))
        with patch("banco_agentes.steward_rag.Retriever.search",return_value={**RESULT,"status":"not_found","hits":[]}):
            r = await c.ask("Horario de una sucursal")
        self.assertIn("No tengo información",r["answer"])
        self.assertIn("Horario de una sucursal",r["answer"])
        self.assertEqual(r["sources"],[])
        self.assertNotIn("rag_writer_started",[e["event"] for e in r["trace"]])

    async def test_technical_error_is_not_absence(self):
        c = Conversation(DemoSession(),model_factory=lambda name:RAGModel(model="test",agent=name))
        with patch("banco_agentes.steward_rag.Retriever.search",side_effect=RetrievalError("pgvector_unavailable")):
            r = await c.ask("Requisitos de nómina")
        self.assertIn("Fallo técnico",r["answer"])
        self.assertEqual(r["sources"],[])
        self.assertNotIn("rag_writer_started",[e["event"] for e in r["trace"]])


class RetrievalTests(unittest.TestCase):
    def test_vector_threshold_and_hybrid_rrf_filter_before_fusion(self):
        rows=[copy.deepcopy(HIT),dict(id="bad",text="nómina "*50,source={},similarity=.3)]
        for mode in ["vectorial","hibrido"]:
            hits=rank("nómina",copy.deepcopy(rows),Settings(mode=mode))
            self.assertEqual([h["id"] for h in hits],["pdf:one"])

    def test_exact_dictionary_name_hybrid_exception_is_explicit(self):
        rows=[dict(id="cards:CHANNEL",text="CHANNEL: medio de apertura",source={"variable":"CHANNEL"},similarity=.1)]
        self.assertEqual(rank("CHANNEL",copy.deepcopy(rows),Settings(mode="vectorial")),[])
        self.assertTrue(rank("CHANNEL",copy.deepcopy(rows),Settings(mode="hibrido"))[0]["exact_name"])

    def test_localhost_only(self):
        with self.assertRaises(RetrievalError):
            OllamaEmbeddings(Settings(base="https://example.com"))

    def test_private_sources_are_filtered(self):
        class Embeddings:
            metrics={}
            def identity(self):return "local@hash"
            def embed(self,texts):return [[1.]*1024]
        class Store:
            def candidates(self,*args):
                return [copy.deepcopy(HIT),dict(id="private",text="secreto",source={"source_kind":"privado"},similarity=.99)]
        result=Retriever(embeddings=Embeddings(),store=Store()).search("nómina")
        self.assertEqual([h["id"] for h in result["hits"]],["pdf:one"])
        self.assertTrue(result["embedding_generated"])
        self.assertTrue(result["pgvector_used"])

    def test_export_telemetry_contains_no_text_or_query(self):
        result=telemetry(RESULT)
        self.assertNotIn("DATO DE PRUEBA",json.dumps(result))
        self.assertNotIn("source",json.dumps(result))

    def test_chunk_identity_page_and_overlap(self):
        from banco_agentes.catalog import ROOT
        metadata=dict(sha256="abc",title="Prueba",source_kind="resumen_academico")
        with patch("banco_agentes.rag_ingest.sections",return_value=[(2,"Requisitos","a"*2500)]):
            chunks=extract(ROOT/"referencias/bank_documents/test.pdf",metadata)
            second=extract(ROOT/"referencias/bank_documents/test.pdf",metadata)
        self.assertEqual([c["id"] for c in chunks],[c["id"] for c in second])
        self.assertEqual(len(set(c["id"] for c in chunks)),len(chunks))
        self.assertTrue(all(c["source"]["page"]==2 for c in chunks))
        self.assertTrue(all(len(c["text"])<=1200 for c in chunks))

    def test_empty_pdf_has_no_chunks(self):
        from banco_agentes.catalog import ROOT
        with patch("banco_agentes.rag_ingest.sections",return_value=[]):
            self.assertEqual(extract(ROOT/"empty.pdf",{"sha256":"abc"}),[])
