"""Consulta sintética al chat y comprobación de recepción en Langfuse (API v2).
Las claves se leen localmente del .env; nunca se imprimen ni se envían al navegador.
"""
import os
import argparse
import json
import re
import sys
import time
from datetime import datetime,timedelta,timezone
from pathlib import Path

import httpx
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from banco_agentes.analytics import connect_readonly
from banco_agentes.diagnostics import LangfuseSettings,SecretScrubber,ROOT_SPAN
from banco_agentes.presentation import decimal_text

QUESTION="¿Cuál fue el saldo total de AHORRO al cierre de junio de 2026?"


def decode(value):
    if isinstance(value,str):
        try:
            return json.loads(value)
        except ValueError:
            pass
    return value


def trace_checks(observations,trace_id,*,answer=None,question=QUESTION,expected_tokens=None,expected_model_calls=None):
    rows={item["id"]:item for item in observations if item.get("id") and item.get("traceId")==trace_id}
    roots=[item for item in rows.values() if item.get("name")==ROOT_SPAN and not item.get("parentObservationId")]
    checks={"root":len(roots)==1}
    if not roots:
        return checks
    root=roots[0]
    question_io,output=decode(root.get("input")),decode(root.get("output"))
    checks["question"]=isinstance(question_io,dict) and question_io.get("question")==question
    checks["final_answer"]=isinstance(output,dict) and bool(output.get("answer")) and (answer is None or output["answer"]==answer)
    def under_root(item):
        seen=set()
        while item["id"]!=root["id"]:
            if item["id"] in seen:
                return False
            seen.add(item["id"])
            item=rows.get(item.get("parentObservationId"))
            if item is None:
                return False
        return True
    checks["tree_connected"]=all(under_root(item) for item in rows.values())
    checks["durations"]=all(item.get("startTime") and item.get("endTime") for item in rows.values())
    checks["delegation"]=any("data_analytics" in item.get("name","") and str(item.get("type","")).lower()=="tool" for item in rows.values())
    tools=[item for item in rows.values() if "consultar_datos" in item.get("name","")]
    checks["tool_parameters_and_result"]=any(decode(item.get("input")) not in (None,"",{},[]) and
        decode(item.get("output")) not in (None,"",{},[]) for item in tools)
    metrics=decode((root.get("metadata") or {}).get("metrics"))
    generations=[item for item in rows.values() if str(item.get("type","")).lower()=="generation"]
    calls=expected_model_calls if expected_model_calls is not None else (metrics.get("model_calls") if isinstance(metrics,dict) else None)
    checks["generations"]=bool(generations) and (calls is None or len(generations)==calls)
    generation_usage=[decode(item.get("usageDetails")) for item in generations]
    reported=any(isinstance(usage,dict) and any(isinstance(n,(int,float)) and n>0 for n in usage.values()) for usage in generation_usage)
    checks["available_tokens"]=expected_tokens is None or reported or (isinstance(metrics,dict) and metrics.get("total_tokens")==expected_tokens)
    return checks


def fetch(client,base,trace_id,since):
    until=datetime.now(timezone.utc)+timedelta(minutes=1)
    params={"traceId":trace_id,"fields":"core,basic,io,usage,metadata","limit":1000,
            "fromStartTime":since.isoformat(),"toStartTime":until.isoformat(),"expandMetadata":"metrics,attributes"}
    rows=[]
    for _ in range(5):
        response=client.get(base+"/api/public/v2/observations",params=params)
        response.raise_for_status()
        page=response.json();rows.extend(page.get("data",[]))
        cursor=(page.get("meta") or {}).get("cursor")
        if not cursor:
            return rows
        params["cursor"]=cursor
    raise RuntimeError("La traza excede el límite de páginas de esta comprobación.")


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace-id",help="Comprueba una traza existente sin volver a preguntar al agente.")
    args=parser.parse_args()
    if args.trace_id and not re.fullmatch(r"[a-f0-9]{32}",args.trace_id):
        parser.error("trace-id debe tener 32 caracteres hexadecimales.")
    started=time.perf_counter();since=datetime.now(timezone.utc)-timedelta(minutes=5 if not args.trace_id else 60)
    report={"passed":False,"question":QUESTION,"receipt_confirmed":False}
    settings=None
    try:
        settings=LangfuseSettings.from_environment()
        scrubber=SecretScrubber.configured(settings)
        answer=None;expected_tokens=None;expected_model_calls=None
        trace_id=args.trace_id
        if trace_id is None:
            with connect_readonly() as db:
                expected=db.execute("SELECT sum(saldo_mes) FROM cfg.fact_sdos_captacoloca_m WHERE cve_producto=%s AND fec_informacion=%s",("AHORRO",20260630)).fetchone()[0]
            report["sql_expected"]=format(expected,"f")
            with httpx.Client(base_url="http://127.0.0.1:8001",trust_env=False,timeout=450) as web:
                web.post("/api/login",json={"username":os.environ.get("BANCO_EMPLEADO_USERNAME", "analista"),"password":os.environ["BANCO_EMPLEADO_PASSWORD"]}).raise_for_status()
                conversation=web.post("/api/conversations",json={}).json()["conversation_id"]
                response=web.post("/api/chat",json={"question":QUESTION,"conversation_id":conversation})
                report["web_http_status"]=response.status_code
                response.raise_for_status()
                data=response.json();answer=data["answer"]
                report["response"]=data
                assert decimal_text(expected) in answer,"La respuesta no coincide con SQL directo."
                assert "data_analytics" in data["tools"] and "consultar_datos" in data["tools"],"Falta delegación o consulta real."
                trace_id=data.get("diagnostics",{}).get("trace_id")
                assert trace_id,"La web no devuelve ID: comprueba que arrancó con --langfuse."
                report["response"]=data
                expected_tokens=(data.get("metrics") or {}).get("total_tokens")
                expected_model_calls=(data.get("metrics") or {}).get("model_calls")
                web.post("/api/logout",json={}).raise_for_status()
        report["trace_id"]=trace_id
        # Espera acotada: solo lectura; no repite preguntas ni genera más trazas.
        deadline=time.monotonic()+30
        with httpx.Client(auth=(settings.public_key,settings.secret_key),timeout=8,follow_redirects=False) as remote:
            while True:
                observations=fetch(remote,settings.base_url,trace_id,since)
                checks=trace_checks(observations,trace_id,answer=answer,expected_tokens=expected_tokens,expected_model_calls=expected_model_calls)
                checks["no_configured_secrets"]=not scrubber.contains_known_secret(json.dumps(observations,ensure_ascii=False))
                report["checks"]=checks
                report["observations"]=scrubber.clean(observations)
                report["receipt_confirmed"]=bool(observations)
                if checks and all(checks.values()):
                    report["passed"]=True
                    break
                if time.monotonic()>=deadline:
                    break
                time.sleep(3)
    except Exception as error:
        report["error_type"]=type(error).__name__
        if isinstance(error,httpx.HTTPStatusError):
            report["error_http_status"]=error.response.status_code
        if isinstance(error,AssertionError):
            report["assertion"]=str(error)
    finally:
        report["seconds"]=time.perf_counter()-started
        safe=SecretScrubber.configured(settings).clean(report) if settings else {k:v for k,v in report.items() if k not in {"observations","response"}}
        (ROOT/"verification-langfuse-live.json").write_text(json.dumps(safe,indent=2,ensure_ascii=False),encoding="utf-8")
        print(json.dumps({key:report.get(key) for key in ("passed","trace_id","receipt_confirmed","checks","seconds","error_type","error_http_status")},ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__=="__main__":
    raise SystemExit(main())
