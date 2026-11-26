"""Lector Gemini REAL solo con fixtures; hub/política con dobles, sin cargar .env.

Salida: IDs, métricas y comprobaciones booleanas. Nunca texto/valores de los documentos.
"""
import asyncio
import json
import sys
import tempfile
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent/"tests"))
from test_documents import fixture_decision
from banco_agentes.documents import ROOT, DocumentStore, DocumentError
from banco_agentes.conversation import Conversation
from banco_agentes.governance import DemoSession


def policy_fixture(product):
    import fitz
    from banco_agentes.document_credit import requested_product
    manifest = json.loads((ROOT/"catalogo/bank_documents_manifest.json").read_text(encoding="utf-8"))
    entry = next(d for d in manifest["documents"] if requested_product(d["title"]) == product)
    with fitz.open(ROOT/entry["file"]) as pdf:
        assert "vigente" in pdf[0].get_text()
    source = {**entry, "page": 1, "fragment_id": "local:policy-fixture"}
    rules = [{**r,"fuente":source} for rows in entry["requirements_by_section"].values() for r in rows]
    rules.append({"campo":"vigencia_identificacion","fuente":source})
    return {"tipo":"politica","producto":product,"reglas":rules,"fuentes":[],"fragment_ids":["local:policy-fixture"],"faltantes":[]}


async def main():
    import os
    output={"fecha":date.today().isoformat(),"lector":"Gemini real", "politica":"doble local; no prueba RAG/pgvector", "checks":{}, "files":[]}
    if not (os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")):
        output["pendiente"]="Falta credencial Gemini en entorno; no se consultó .env."
    else:
        with tempfile.TemporaryDirectory() as temp:
            store=DocumentStore(Path(temp))
            fixtures=ROOT/"fixtures/documents"
            names=["estado_cuenta.pdf","identificacion_vigente.png","identificacion_vencida.png", "identificacion_ilegible.png","no_bancario.pdf","estado_instrucciones.pdf"]
            uploaded=store.save_many("synthetic-pilot","pilot",[(fixtures/n).read_bytes() for n in names])
            ids=[r["file_id"] for r in uploaded]
            conversation=Conversation(DemoSession(profile="Cliente",customer_id=1,account_id=1))
            async def ask(selected, question="¿Cumplo los requisitos de Clásica?"):
                result = await conversation.ask_documents(question,store=store,
                    file_ids=selected,owner="synthetic-pilot",conversation_id="pilot")
                if result["document_errors"]:
                    raise DocumentError("Lectura real fallida; ver códigos por file_id.")
                return result
            try:
                with patch("banco_agentes.steward_rag.document_policy",new=AsyncMock(return_value=policy_fixture("clasica"))) as steward:
                    current=await ask(ids[:2]); expired=await ask([ids[0],ids[2]])
                    unreadable=await ask([ids[0],ids[3]])
                    count=steward.await_count
                    other=await ask([ids[4]], "Háblame de jardinería")
                    output["checks"]["fuera_de_alcance_sin_spokes"]=steward.await_count==count and other["attachment_decision"]["alcance"]=="fuera_de_alcance"
                    injected=await ask([ids[5],ids[1]])
                outputs=[current,expired,unreadable,other,injected]
                output["checks"]["sin_errores_lectura"]=all(not r["document_errors"] for r in outputs)
                a,b=current["derived"]["requisitos"],expired["derived"]["requisitos"]
                changed=[x["regla"] for x,y in zip(a,b) if x["estado"]!=y["estado"]]
                output["checks"]["solo_cambia_vencimiento"]=changed==["vigencia_identificacion"] and a[-1]["estado"]=="cumple" and b[-1]["estado"]=="no cumple"
                output["checks"]["fecha_ilegible_pendiente"]=unreadable["documents"][1]["campos"]["fecha_vencimiento"]["valor"] is None and unreadable["derived"]["requisitos"][-1]["estado"]=="pendiente de confirmar"
                output["checks"]["inyeccion_no_cambia_resultados"]=[r["estado"] for r in current["derived"]["requisitos"]]==[r["estado"] for r in injected["derived"]["requisitos"]]
                output["checks"]["inyeccion_sin_analytics"]=not any(e.get("agent")=="data_analytics" for e in injected["trace"])
                store.correct(ids[3],"synthetic-pilot","pilot","fecha_vencimiento","2030-12-31")
                with patch("banco_agentes.steward_rag.document_policy",new=AsyncMock(return_value=policy_fixture("clasica"))):
                    corrected=await ask([ids[0],ids[3]])
                output["checks"]["correccion_conserva_original"]=corrected["documents"][1]["campos"]["fecha_vencimiento"]["valor"] is None and corrected["derived"]["requisitos"][-1]["estado"]=="cumple"
                for mime_name in ("identificacion_vigente.jpg","identificacion_vencida.jpg"):
                    # Otra sesión sintética para verificar JPEG sin exceder seis archivos por usuario.
                    record=store.save_many("synthetic-jpg","jpg",[(fixtures/mime_name).read_bytes()])[0]
                    read=await store.leer_documento(record["file_id"],"synthetic-jpg","jpg")
                    output["checks"][mime_name+"_clasificada"]=read["tipo"]=="identificacion"
            except Exception as error:
                output["error_type"]=type(error).__name__
                output["pendiente"]="El piloto real no completó todas las comprobaciones. Revisar proveedor/cuota o contrato; no se exportó el error bruto."
            output["files"]=[{"file_id":r["file_id"],"attempted":r["attempted"],"ok":r["result"] is not None,"error":r["error"],"metrics":r["metrics"]} for r in store.records.values()]
    (ROOT/"verification-documents-gemini.json").write_text(json.dumps(output,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps({k:v for k,v in output.items() if k!="files"},ensure_ascii=True))
    return 0 if output["checks"] and all(output["checks"].values()) and "pendiente" not in output else 1


if __name__=="__main__":
    with patch("banco_agentes.attachment_hub.decide_attachment", new=AsyncMock(side_effect=fixture_decision)):
        sys.exit(asyncio.run(main()))
