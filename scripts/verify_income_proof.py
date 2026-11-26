"""Gemini + Steward REALES por API local, exclusivamente fixtures ficticios."""
import os
import argparse
import json
from pathlib import Path
from urllib.parse import urlsplit

import httpx

ROOT=Path(__file__).resolve().parent.parent
FIXTURES=ROOT/"fixtures/documents/ingreso_domicilio"


def verify(base, only=None):
    url=urlsplit(base)
    if url.scheme!="http" or url.hostname not in {"127.0.0.1","localhost","::1"} or url.username or url.password or url.query or url.fragment:
        raise ValueError("Solo API local")
    out={"provider":"Gemini real", "policy":"Steward/RAG local real", "cases":[]}
    cases=[("partial",["cfdi_parcial.pdf"],"Evalúa mi ingreso mensual para Tarjeta de Crédito Clásica"),
        ("joint",["identificacion.pdf","cfdi_mensual.pdf","factura_reciente.pdf"],"¿Cumplo los requisitos de Tarjeta de Crédito Clásica con mis adjuntos?"),
        ("below",["cfdi_bajo.pdf"],"Evalúa mi ingreso mensual para Tarjeta de Crédito Clásica"),
        ("old_service",["luz_antigua.pdf"],"Evalúa mi comprobante de domicilio para Tarjeta de Crédito Clásica")]
    if only:
        cases=[case for case in cases if case[0]==only]
    with httpx.Client(base_url=base,timeout=240,trust_env=False) as c:
        def post(path,**kwargs):
            r=c.post(path,**kwargs)
            r.raise_for_status()
            return r.json()
        post("/api/login",json={"username":os.environ.get("BANCO_CLIENTE_USERNAME", "cliente"),"password":os.environ["BANCO_CLIENTE_PASSWORD"]})
        for name,files,question in cases:
            case={"case":name}
            out["cases"].append(case)
            cid=post("/api/conversations")["conversation_id"]
            paths=[ROOT/"fixtures/documents/evidencia/registro_personas.pdf" if f=="identificacion.pdf" else FIXTURES/f for f in files]
            upload=post("/api/documents",data={"conversation_id":cid},files=[("files",(n,p.read_bytes())) for n,p in zip(files,paths)])
            ids=[d["file_id"] for d in upload["documents"]]
            case["file_ids"]=ids
            if name=="partial":
                direct=post("/api/chat",json={"conversation_id":cid,"document_ids":ids,"question":"¿Cuál es el neto a pagar del CFDI?"})
                case["direct"]={"value_found":any(s in direct.get("answer","") for s in ("12000","12,000")),
                    "page_cited":"página 1" in direct.get("answer",""),"no_steward":"data_steward" not in direct.get("agents",[]),
                    "metrics":direct.get("metrics"),"errors":direct.get("document_errors",[])}
            result=post("/api/chat",json={"conversation_id":cid,"document_ids":ids,"question":question})
            policy=result.get("policy") or {}
            derived=result.get("derived") or {}
            rows=derived.get("requisitos",[])
            case.update(agents=result.get("agents"),tools=result.get("tools"),metrics=result.get("metrics"),errors=result.get("document_errors",[]),
                decision=result.get("attachment_decision"),
                coverage=[d.get("lectura") for d in result.get("documents",[])],
                policy_id=policy.get("id"), derived_id=derived.get("id"),
                policy_sources=[{"file":r["fuente"].get("file"),"page":r["fuente"].get("page"),"version":r["fuente"].get("version")} for r in policy.get("reglas",[])],
                requirements=[{"rule":r["regla"],"status":r["estado"],"reason":r.get("motivo"),"threshold":r.get("umbral"),
                    "preliminary_reference":r.get("referencia_preliminar"),"documents":list(dict.fromkeys(e["documento_id"] for e in r["evidencia"]))} for r in rows])
            if not rows:
                case["ok"]=False
                case["pending"]="No llegó a una evaluación; revisar errores/métricas y etapa documental en el stderr local, sin repetir automáticamente."
                break
            by_name={r["regla"]:r for r in rows}
            expected={"partial":{"ingreso_mensual":"cumple","aceptacion_ingreso_comprobable":"no verificable"},
                "joint":{"ingreso_mensual":"cumple","aceptacion_ingreso_comprobable":"no verificable","tipo_servicio_comprobante":"cumple","antiguedad_comprobante_meses":"cumple","edad_anios":"cumple","vigencia_identificacion":"cumple"},
                "below":{"ingreso_mensual":"no cumple","aceptacion_ingreso_comprobable":"no verificable"},
                "old_service":{"tipo_servicio_comprobante":"cumple","antiguedad_comprobante_meses":"no cumple"}}[name]
            case["ok"]=all(by_name.get(k,{}).get("estado")==v for k,v in expected.items()) and "data_steward" in case["agents"] and any(
                str(s["file"]).endswith("DOC-11_Tarjeta_de_credito_Clasica.pdf") for s in case["policy_sources"])
            if name=="partial":
                case["ok"] &= all(case["direct"][k] for k in ("value_found","page_cited","no_steward"))
    out["ok"]=len(out["cases"])==len(cases) and all(case["ok"] for case in out["cases"])
    return out


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url",default="http://127.0.0.1:8004")
    parser.add_argument("--case",choices=["partial","joint","below","old_service"])
    args=parser.parse_args()
    try:
        result=verify(args.base_url,args.case)
    except Exception as error:
        result={"ok":False,"error_type":type(error).__name__,"http_status":getattr(getattr(error,"response",None),"status_code",None),
            "pending":"La corrida no se completó; repetir el comando tras resolver el error indicado. No se guardó el error bruto."}
    output="verification-income-proof-real"+("-"+args.case if args.case else "")+".json"
    (ROOT/output).write_text(json.dumps(result,ensure_ascii=False,indent=2)+"\n",encoding="utf8")
    print(json.dumps({"ok":result.get("ok"),"cases":[{"case":c["case"],"ok":c["ok"],"pending":c.get("pending")} for c in result.get("cases",[])],"error_type":result.get("error_type")},ensure_ascii=True))
    raise SystemExit(0 if result.get("ok") else 1)
