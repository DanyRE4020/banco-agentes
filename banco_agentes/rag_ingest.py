"""Ingesta explícita local de PDF aprobados; reemplazo atómico por archivo."""
import argparse
import csv
import hashlib
import io
import json
import re
from pathlib import Path

from .catalog import ROOT
from .rag import MANIFEST, OllamaEmbeddings, Settings, RetrievalError

CHUNK_VERSION = "sections-page-v1"
MAX_CHARS = 1200
OVERLAP = 160


def digest(data):
    return hashlib.sha256(data).hexdigest()


def sections(path):
    import pymupdf
    result = []
    section = ""
    with pymupdf.open(path) as document:
        if document.needs_pass:
            raise ValueError("encrypted_pdf")
        for number, page in enumerate(document, 1):
            body = []
            def flush():
                if body and section:
                    result.append((number, section, "\n".join(body).strip()))
                body.clear()
            for block in page.get_text("dict", sort=True)["blocks"]:
                for line in block.get("lines", []):
                    spans = line["spans"]
                    text = "".join(s["text"] for s in spans).strip()
                    if not text or text.startswith(("Material académico para evaluar", "DOC-", "Página ")):
                        continue
                    heading = re.match(r"^\d{1,2}\.\s+\S", text) and any(s["flags"] & 16 for s in spans)
                    if heading:
                        flush()
                        section = text
                    elif section:
                        body.append(text)
            flush()
        # PDF sin encabezados: fragmentos de página completa, sección no documentada.
        if not result:
            for number, page in enumerate(document, 1):
                text = page.get_text(sort=True).strip()
                if text:
                    result.append((number, "", text))
    return result


def extract(path, metadata):
    chunks = []
    for page, section, text in sections(path):
        # El solapamiento solo ocurre dentro de la misma sección y página.
        for position, start in enumerate(range(0, len(text), MAX_CHARS-OVERLAP)):
            part = text[start:start+MAX_CHARS].strip()
            if not part:
                continue
            source = {k:v for k,v in metadata.items() if k != "sha256"}
            source.update(file=path.relative_to(ROOT).as_posix(), page=page, section=section,
                          chunk_version=CHUNK_VERSION)
            if "ejemplo ilustrativo" in part.lower():
                source["contains_illustrative_example"] = True
            source["requirements"] = metadata.get("requirements_by_section", {}).get(section, [])
            source.pop("requirements_by_section", None)
            identity = f"{source['file']}|{metadata['sha256']}|{page}|{section}|{position}|{CHUNK_VERSION}"
            chunks.append(dict(id="pdf:"+digest(identity.encode())[:32], corpus="bank_documents",
                file=source["file"], file_hash=metadata["sha256"], text=part, source=source))
            if start+MAX_CHARS >= len(text):
                break
    return chunks


def replace_file(file, chunks, corpus="bank_documents"):
    from scripts.rag_admin import sql
    # COPY transmite JSON como dato; SQL fijo. Una transacción elimina la versión anterior.
    payload = json.dumps({"file": file, "corpus": corpus, "chunks": chunks}, ensure_ascii=False)
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerow([payload])
    command = """BEGIN;
CREATE TEMP TABLE rag_stage (payload jsonb) ON COMMIT DROP;
COPY rag_stage(payload) FROM STDIN WITH (FORMAT csv);
""" + buf.getvalue() + """\\.
DELETE FROM docs.fragments WHERE (corpus,file) IN
 (SELECT payload->>'corpus',payload->>'file' FROM rag_stage);
INSERT INTO docs.fragments(id,corpus,file,file_hash,text,source,model_identity,embedding)
SELECT c->>'id',c->>'corpus',c->>'file',c->>'file_hash',c->>'text',c->'source',
 c->>'model_identity',(c->'embedding')::text::docs.vector
FROM rag_stage, jsonb_array_elements(payload->'chunks') AS c;
COMMIT;
"""
    sql(command)


def index_documents():
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    approved = {x["file"]: x for x in manifest["documents"]}
    embedder = OllamaEmbeddings(Settings.environment())
    identity = embedder.identity()
    report = {"model_identity": identity, "documents": [], "embedding_batches": []}
    for path in sorted((ROOT/"referencias/bank_documents").glob("*.pdf")):
        file = path.relative_to(ROOT).as_posix()
        entry = approved.get(file)
        if not entry or entry.get("pilot_allowed") is not True or entry.get("source_kind") not in {"oficial_publico","resumen_academico","ejemplo_ilustrativo"}:
            report["documents"].append({"file": file, "status": "not_approved"})
            continue
        if digest(path.read_bytes()) != entry["sha256"]:
            report["documents"].append({"file": file, "status": "changed_requires_review"})
            continue
        try:
            chunks = extract(path, entry)
        except Exception as error:
            report["documents"].append({"file": file, "status": "extraction_failed", "error_type": type(error).__name__})
            continue
        for offset in range(0, len(chunks), 8):
            batch = chunks[offset:offset+8]
            vectors = embedder.embed([entry["title"]+"\n"+c["source"]["section"]+"\n"+c["text"] for c in batch])
            report["embedding_batches"].append(embedder.metrics.copy())
            for chunk, vector in zip(batch, vectors):
                chunk.update(embedding=vector, model_identity=identity)
        replace_file(file, chunks)
        report["documents"].append({"file": file, "status": "indexed" if chunks else "no_extractable_text",
                                    "chunks": len(chunks), "sha256": entry["sha256"],
                                    "source_kind": entry["source_kind"]})
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", default="verification-rag-index.json")
    parser.add_argument("--register", help="PDF revisado dentro de referencias/bank_documents")
    parser.add_argument("--kind", choices=["oficial_publico","resumen_academico","ejemplo_ilustrativo"])
    parser.add_argument("--title")
    parser.add_argument("--version")
    parser.add_argument("--consulted-at")
    parser.add_argument("--source")
    parser.add_argument("--confirm-public-or-academic", action="store_true")
    args = parser.parse_args()
    if args.register:
        path = (ROOT/args.register).resolve()
        base = (ROOT/"referencias/bank_documents").resolve()
        if (not args.confirm_public_or_academic or not all([args.kind,args.title,args.version,args.consulted_at,args.source])
                or not path.is_relative_to(base) or path.suffix.lower() != ".pdf" or not path.is_file()):
            parser.error("Revisa y confirma un PDF público/académico dentro del corpus y todos sus metadatos.")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}",args.consulted_at):
            parser.error("La fecha de consulta debe ser AAAA-MM-DD.")
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8")) if MANIFEST.exists() else {"documents":[]}
        file = path.relative_to(ROOT).as_posix()
        previous = next((x for x in manifest["documents"] if x["file"]==file),{})
        entry = dict(file=file,sha256=digest(path.read_bytes()),title=args.title,source_kind=args.kind,
                     origen="publica" if args.kind=="oficial_publico" else "sintetico",
                     pilot_allowed=True,version=args.version,consulted_at=args.consulted_at,
                     original_source=args.source,vigencia="No documentada",
                     requirements_by_section={})
        # Un PDF actualizado requiere volver a revisar las reglas estructuradas.
        if previous.get("sha256")==entry["sha256"]:
            entry["requirements_by_section"]=previous.get("requirements_by_section",{})
        manifest["documents"]=[x for x in manifest["documents"] if x["file"]!=file]+[entry]
        MANIFEST.write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
        print("PDF registrado. Revisa requirements_by_section antes de indexar.")
        return
    report = index_documents()
    (ROOT/args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({**report, "embedding_batches": len(report["embedding_batches"])}, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
