"""Recuperación local por corpus; pgvector nunca consulta tablas de negocio."""
import hashlib
import json
import math
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx

from .analytics import connect_readonly
from .catalog import ROOT
from .knowledge import tokens

MANIFEST = ROOT / "catalogo/bank_documents_manifest.json"
DIMENSIONS = 1024
ALLOWED_KINDS = {"resumen_academico", "oficial_publico", "ejemplo_ilustrativo"}


class RetrievalError(RuntimeError):
    """Código público fijo, sin URLs, credenciales o excepciones de conexión."""


@dataclass(frozen=True)
class Settings:
    mode: str = "vectorial"
    threshold: float = 0.55
    top_k: int = 5
    model: str = "bge-m3:567m"
    base: str = "http://127.0.0.1:11434"

    @classmethod
    def environment(cls):
        result = cls(mode=os.getenv("BANCO_RAG_MODE", "vectorial"),
                     threshold=float(os.getenv("BANCO_RAG_THRESHOLD", "0.55")),
                     top_k=int(os.getenv("BANCO_RAG_TOP_K", "5")),
                     model=os.getenv("BANCO_EMBED_MODEL", "bge-m3:567m"),
                     base=os.getenv("BANCO_EMBED_BASE", "http://127.0.0.1:11434"))
        if result.mode not in {"lexico", "vectorial", "hibrido"} or not 0 <= result.threshold <= 1 or not 1 <= result.top_k <= 8:
            raise RetrievalError("rag_configuration_invalid")
        return result


class OllamaEmbeddings:
    def __init__(self, settings):
        self.settings = settings
        url = urlparse(settings.base)
        if url.scheme != "http" or url.hostname not in {"localhost", "127.0.0.1", "::1"} or url.username or url.password or url.path not in {"", "/"}:
            raise RetrievalError("embeddings_require_localhost")
        self.metrics = {}

    def identity(self):
        try:
            with httpx.Client(base_url=self.settings.base, timeout=10, trust_env=False) as client:
                response = client.get("/api/tags")
                response.raise_for_status()
                match = next(x for x in response.json()["models"] if x["name"] == self.settings.model)
                return self.settings.model + "@" + match["digest"]
        except Exception:
            raise RetrievalError("embedding_model_unavailable") from None

    def embed(self, texts):
        started = time.perf_counter()
        try:
            with httpx.Client(base_url=self.settings.base, timeout=180, trust_env=False) as client:
                response = client.post("/api/embed", json={"model": self.settings.model,
                    "input": texts, "truncate": False, "keep_alive": "10m"})
                response.raise_for_status()
                data = response.json()
            vectors = data["embeddings"]
            if len(vectors) != len(texts) or any(len(v) != DIMENSIONS or not all(math.isfinite(x) for x in v) or sum(x*x for x in v) == 0 for v in vectors):
                raise ValueError()
            self.metrics = {"seconds": time.perf_counter()-started,
                            "load_seconds": data.get("load_duration", 0)/1e9,
                            "total_seconds": data.get("total_duration", 0)/1e9}
            return vectors
        except Exception:
            raise RetrievalError("local_embedding_failed") from None


def approved_hashes():
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    allowed = []
    base = (ROOT / "referencias/bank_documents").resolve()
    for entry in manifest["documents"]:
        path = (ROOT / entry["file"]).resolve()
        if (path.is_relative_to(base) and path.is_file()
                and entry.get("pilot_allowed") is True and entry.get("source_kind") in ALLOWED_KINDS
                and hashlib.sha256(path.read_bytes()).hexdigest() == entry["sha256"]):
            allowed.append(entry["sha256"])
    return allowed


class PgVectorStore:
    def candidates(self, corpus, identity, vector=None):
        # La pregunta y el vector se parametrizan. No hay SQL del LLM.
        hashes = approved_hashes() if corpus == "bank_documents" else []
        distance = "1 - (embedding OPERATOR(docs.<=>) %s::docs.vector)" if vector is not None else "NULL::double precision"
        params = ([json.dumps(vector)] if vector is not None else []) + [corpus, identity, corpus, hashes]
        sql = f"""SELECT id, text, source, {distance} AS similarity
                  FROM docs.fragments WHERE corpus=%s AND model_identity=%s
                  AND (%s <> 'bank_documents' OR file_hash = ANY(%s::text[]))
                  ORDER BY similarity DESC NULLS LAST, id LIMIT 2000"""
        try:
            with connect_readonly() as conn:
                rows = conn.execute(sql, params).fetchall()
            return [dict(id=r[0], text=r[1], source=r[2], similarity=float(r[3]) if r[3] is not None else None) for r in rows]
        except Exception:
            raise RetrievalError("pgvector_unavailable") from None


def rank(query, rows, settings):
    terms = tokens(query)
    if re.search(r"\b(necesito|necesita|piden|requiere)\b", query, re.I):
        terms.add("requisitos")
    for row in rows:
        row["lexical_score"] = len(terms & tokens(row["text"] + " " + row["source"].get("title", "") + " " + row["source"].get("section", "")))
        name = row["source"].get("variable")
        row["exact_name"] = bool(name and re.search(r"\b"+re.escape(name)+r"\b", query, re.I))
        if row["exact_name"]:
            row["lexical_score"] += 100
    lexical = sorted((r for r in rows if r["lexical_score"]), key=lambda r: (-r["lexical_score"], r["id"]))
    vector = sorted((r for r in rows if r["similarity"] is not None and r["similarity"] >= settings.threshold),
                    key=lambda r: (-r["similarity"], r["id"]))
    lr = {r["id"]: i for i, r in enumerate(lexical, 1)}
    vr = {r["id"]: i for i, r in enumerate(vector, 1)}
    if settings.mode == "lexico":
        selected = lexical
    elif settings.mode == "vectorial":
        selected = vector
    else:
        # El umbral se aplica ANTES de fusionar. Excepción explícita:
        # nombre exacto del diccionario, nunca una coincidencia parcial.
        selected = [r for r in rows if r["id"] in vr or r["exact_name"]]
        for r in selected:
            r["rrf_score"] = (1/(60+lr[r["id"]]) if r["id"] in lr else 0) + (1/(60+vr[r["id"]]) if r["id"] in vr else 0)
        selected.sort(key=lambda r: (-int(r["exact_name"]), -r["rrf_score"], r["id"]))
    return [{**r, "rank": i, "lexical_rank": lr.get(r["id"]), "vector_rank": vr.get(r["id"])}
            for i, r in enumerate(selected[:settings.top_k], 1)]


class Retriever:
    def __init__(self, settings=None, embeddings=None, store=None):
        self.settings = settings or Settings.environment()
        self.embeddings = embeddings or OllamaEmbeddings(self.settings)
        self.store = store or PgVectorStore()

    def search(self, query, corpus="bank_documents"):
        if corpus not in {"bank_documents", "cards_dictionary"}:
            raise RetrievalError("invalid_corpus")
        started = time.perf_counter()
        identity = self.embeddings.identity()
        vector = None
        if self.settings.mode != "lexico":
            vector = self.embeddings.embed([query])[0]
        rows = self.store.candidates(corpus, identity, vector)
        # Defensa adicional ante metadatos incoherentes.
        if corpus == "bank_documents":
            rows = [r for r in rows if r["source"].get("pilot_allowed") is True
                    and r["source"].get("source_kind") in ALLOWED_KINDS]
        hits = rank(query, rows, self.settings)
        return dict(status="found" if hits else "not_found", corpus=corpus, mode=self.settings.mode,
                    threshold=self.settings.threshold, model_identity=identity,
                    embedding_generated=vector is not None, pgvector_used=vector is not None,
                    embedding_metrics=self.embeddings.metrics if vector is not None else {},
                    hits=hits, seconds=time.perf_counter()-started)


def citation(hit):
    s = hit["source"]
    location = (f"hoja {s['sheet']}, fila {s['row']}" if "sheet" in s else
                f"página {s['page']}" + (f", sección {s['section']}" if s.get("section") else ""))
    return (f"{s['file']} · {location} · {s['source_kind']} · versión {s.get('version', 'no documentada')}"
            f" · consulta {s.get('consulted_at', 'no documentada')} · vigencia {s.get('vigencia', 'no documentada')}")


def telemetry(result):
    """Única representación apta para exportación: sin pregunta ni textos."""
    return {key: result[key] for key in ("status", "corpus", "mode", "threshold", "seconds") if key in result} | {
        "hits": [{key: h.get(key) for key in ("id", "rank", "similarity", "rrf_score", "lexical_score")}
                 for h in result.get("hits", [])]}
