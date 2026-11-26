"""Prueba de desarrollo: prompts de calidad con Gemini real desde la web local.
No invoca especialistas directamente ni modifica datos. Usa la instancia 8001.
"""
import argparse
import calendar
import json
import re
import os
import sys
import time
from collections import Counter
from pathlib import Path

import httpx
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from banco_agentes.analytics import connect_readonly

REPORT=ROOT/"verification-quality-prompts-web.json"
AUDIT=ROOT/"verification-web.jsonl"


def main():
    global REPORT,AUDIT
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case",choices=["periodo_ausente","dos_controles_por_mes","seguimiento_periodo"])
    parser.add_argument("--in-process",action="store_true",help="Usa FastAPI actual con Gemini real, sin servidor externo.")
    args=parser.parse_args()
    if args.in_process:
        REPORT=ROOT/"verification-quality-prompts-current-process.json"
        AUDIT=ROOT/"verification-quality-prompts-current-audit.jsonl"
    started=time.perf_counter()
    report={"passed":False,"cases":[],"selected_case":args.case,"base_url":"http://127.0.0.1:8001",
            "scope":"Prompts actuales leídos por turno; descripciones Python requieren reinicio del servidor existente."}
    def save():
        report["total_seconds"]=time.perf_counter()-started
        REPORT.write_text(json.dumps(report,indent=2,ensure_ascii=False),encoding="utf-8")
    try:
        tick=time.perf_counter()
        expected={}
        with connect_readonly() as db:
            report["db_user"]=db.execute("SELECT current_user").fetchone()[0]
            for product in ("CHEQUES","AHORRO"):
                for month in (2,3):
                    date=20260000+month*100+calendar.monthrange(2026,month)[1]
                    row=db.execute(
                        "WITH base AS (SELECT num_cta,cve_producto,fec_informacion,saldo_mes "
                        "FROM cfg.fact_sdos_captacoloca_m WHERE cve_producto=%s AND fec_informacion=%s), "
                        "grupos AS (SELECT count(*) AS n FROM base "
                        "GROUP BY num_cta,cve_producto,fec_informacion HAVING count(*)>1) "
                        "SELECT (SELECT count(*) FROM base),"
                        "(SELECT count(*) FROM base WHERE saldo_mes IS NULL),"
                        "count(*),coalesce(sum(n-1),0) FROM grupos",(product,date)).fetchone()
                    expected[f"{product}/2026-{month:02d}"]={"filas":row[0],"nulos":row[1],
                        "grupos_duplicados":row[2],"filas_adicionales":int(row[3])}
        report["sql_direct"]={"results":expected,"seconds":time.perf_counter()-tick}
        if args.in_process:
            from fastapi.testclient import TestClient
            from banco_agentes.providers import validate_provider
            from banco_agentes.web import create_app
            os.environ["BANCO_MODEL_PROVIDER"]="gemini"
            report["scope"]="FastAPI y prompts actuales; Gemini real, sin modelos simulados."
            app=create_app(audit_path=AUDIT,metrics_path=ROOT/"turn-metrics-quality-prompts.jsonl",model_info=validate_provider())
            client=TestClient(app,base_url="http://localhost")
        else:
            client=httpx.Client(base_url=report["base_url"],trust_env=False,timeout=450)
        with client as web:
            web.post("/api/login",json={"username":os.environ.get("BANCO_EMPLEADO_USERNAME", "analista"),"password":os.environ["BANCO_EMPLEADO_PASSWORD"]}).raise_for_status()
            conversations=[web.post("/api/conversations",json={}).json()["conversation_id"] for _ in range(2)]
            previous=None
            import hashlib
            report["prompt_hashes"]={name:hashlib.sha256((ROOT/"banco_agentes"/"prompts"/name).read_bytes()).hexdigest()
                                     for name in ("web_orquestador.txt","web_data_quality.txt")}
            for name,question,conversation,product in (
                ("periodo_ausente","Revisa si hay nulos en saldo_mes de AHORRO.",conversations[0],None),
                ("dos_controles_por_mes","Para CHEQUES, revisa nulos en saldo_mes y duplicados de saldos por mes en febrero y marzo de 2026.",conversations[1],"CHEQUES"),
                ("seguimiento_periodo","¿Y los mismos controles para AHORRO?",conversations[1],"AHORRO"),
            ):
                selected={args.case} if args.case else {"periodo_ausente","dos_controles_por_mes","seguimiento_periodo"}
                if args.case=="seguimiento_periodo":
                    selected.add("dos_controles_por_mes")
                if name not in selected:
                    continue
                entry={"case":name,"question":question,"passed":False}
                if args.case=="seguimiento_periodo" and name=="dos_controles_por_mes":
                    entry["setup_for_followup"]=True
                report["cases"].append(entry)
                if name=="seguimiento_periodo" and previous is None:
                    entry.update(status="pending",reason="La revisión anterior no produjo evidencia aprobada para heredar.")
                    save()
                    continue
                offset=len(AUDIT.read_text(encoding="utf-8").splitlines()) if AUDIT.exists() else 0
                tick=time.perf_counter()
                try:
                    response=web.post("/api/chat",json={"question":question,"conversation_id":conversation})
                    entry["response"]=response.json()
                    entry["http_status"]=response.status_code
                    records=[json.loads(line) for line in AUDIT.read_text(encoding="utf-8").splitlines()[offset:]]
                    assert len(records)==1,"Se espera una única auditoría por turno."
                    trace=records[0]["trace"]
                    entry["trace"]=trace
                    tools=[e.get("tool") for e in trace if e["event"]=="tool_started"]
                    entry["tools_started"]=tools
                    response.raise_for_status()
                    data=response.json()
                    assert data["metrics"]["provider"]=="gemini" and data["metrics"]["model"]=="gemini-3.5-flash-lite"
                    plans=[e for e in trace if e["event"]=="validated_plan"]
                    if product is None:
                        assert not plans and "revisar_calidad" not in tools
                        assert not any(e["event"]=="authorization" for e in trace)
                        assert re.search(r"mes|per[ií]odo|fecha",data["answer"],re.I) and "?" in data["answer"]
                    else:
                        assert "data_quality" in tools and tools.count("revisar_calidad")==4
                        assert "data_analytics" not in tools and "data_governance" not in tools
                        assert all(e["status"]=="ok" and e["agent"]=="data_quality" for e in plans)
                        actual=Counter((e["plan"]["metrica"],e["plan"]["desde"]) for e in plans)
                        target=Counter((metric,month) for metric in ("completitud","duplicados") for month in ("2026-02","2026-03"))
                        assert actual==target,f"Combinaciones observadas: {actual}"
                        for e in plans:
                            plan=e["plan"]
                            assert plan["producto"]==product and plan["desde"]==plan["hasta"]
                            assert plan["alcance"]=="global" and plan["campo"]=="saldo_mes"
                            assert plan["agrupar"]==[]
                            assert e["sql_executed"] and e["sources"] and e["rule"]
                            row=e["rows"][0]
                            direct=expected[product+"/"+plan["desde"]]
                            for field in ("filas","nulos") if plan["metrica"]=="completitud" else ("filas","grupos_duplicados","filas_adicionales"):
                                assert int(row[field])==direct[field],f"Difiere {field}: {row}"
                        auth=[e for e in trace if e["event"]=="authorization"]
                        assert len(auth)==4 and all(e["allowed"] and e["operation"]=="quality" for e in auth)
                        assert "febrero" in data["answer"] and "marzo" in data["answer"]
                        assert data["answer"].count("nulos")>=2 and data["answer"].count("grupos duplicados")>=2
                        assert data["sources"] and "0" in data["answer"]
                        session=next(e for e in trace if e["event"]=="conversation_session")
                        if name=="seguimiento_periodo":
                            assert session["reused"] and session["session_id"]==previous["session_id"]
                            snapshot=next(e for e in trace if e["event"]=="context_snapshot")
                            assert len(snapshot["results"])==4 and all(e["plan"]["producto"]=="CHEQUES" for e in snapshot["results"])
                        else:
                            assert not session["reused"]
                            previous=session
                        entry["sql_match"]=True
                    entry.update(passed=True,status="passed")
                except Exception as error:
                    entry.update(status="failed",error_type=type(error).__name__,assertion=str(error) if isinstance(error,AssertionError) else None)
                finally:
                    entry["seconds"]=time.perf_counter()-tick
                    save()
                    print(json.dumps({key:entry.get(key) for key in ("case","status","seconds","tools_started","assertion")},ensure_ascii=False),flush=True)
        report["passed"]=all(case["passed"] for case in report["cases"])
        return 0 if report["passed"] else 1
    except Exception as error:
        report["error_type"]=type(error).__name__
        return 1
    finally:
        save()


if __name__=="__main__":
    raise SystemExit(main())
