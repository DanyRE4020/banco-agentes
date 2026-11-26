"""Regresión del chat con Gemini real: total, seguimiento PF y política explícita.

Usa los endpoints FastAPI con cookies y una sesión nueva mediante TestClient.
No usa dobles de modelo ni llama directamente a especialistas.
"""
import argparse
import json
import os
import sys
import time
import httpx
from decimal import Decimal
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from fastapi.testclient import TestClient
from banco_agentes.analytics import connect_readonly
from banco_agentes.providers import validate_provider
from banco_agentes.web import create_app

REPORT=ROOT/"verification-policy-routing-web.json"
AUDIT=ROOT/"verification-policy-routing-web-audit.jsonl"
METRICS=ROOT/"turn-metrics-policy-routing.jsonl"


def audit_since(start):
    return [json.loads(line) for line in AUDIT.read_text(encoding="utf-8").splitlines()[start:]]


def main():
    global REPORT, AUDIT
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url",choices=["http://127.0.0.1:8001"],help="Comprueba el servidor existente: debe reiniciarse para incorporar cambios Python.")
    args=parser.parse_args()
    if args.base_url:
        REPORT=ROOT/"verification-policy-routing-existing-web.json"
        AUDIT=ROOT/"verification-web.jsonl"
    os.environ["BANCO_MODEL_PROVIDER"]="gemini"
    started=time.perf_counter()
    report={"passed":False,"transport":args.base_url or "FastAPI TestClient; modelo Gemini real", "cases":[],
            "backend_reload_required":bool(args.base_url)}
    def save():
        report["total_seconds"]=time.perf_counter()-started
        REPORT.write_text(json.dumps(report,indent=2,ensure_ascii=False),encoding="utf-8")
    try:
        model_info=validate_provider()
        report["model"]=model_info
        tick=time.perf_counter()
        expected={}
        with connect_readonly() as db:
            report["db_user"]=db.execute("SELECT current_user").fetchone()[0]
            for person in ("","PF"):
                row=db.execute(
                    "SELECT sum(f.saldo_mes), count(*) FROM cfg.fact_sdos_captacoloca_m f "
                    "JOIN dim.dim_cliente c USING(num_cte) WHERE f.cve_producto=%s AND f.fec_informacion=%s "
                    "AND (%s='' OR c.tipo_persona=%s)",("CHEQUES",20260331,person,person)).fetchone()
                expected[person]={"total":format(row[0],"f"),"rows":row[1]}
        report["sql_direct"]={"product":"CHEQUES","cut":20260331,"results":expected,"seconds":time.perf_counter()-tick}
        if args.base_url:
            client=httpx.Client(base_url=args.base_url,trust_env=False,timeout=450)
        else:
            app=create_app(audit_path=AUDIT,metrics_path=METRICS,model_info=model_info)
            client=TestClient(app,base_url="http://localhost")
        with client as web:
            web.post("/api/login",json={"username":os.environ.get("BANCO_EMPLEADO_USERNAME", "analista"),"password":os.environ["BANCO_EMPLEADO_PASSWORD"]}).raise_for_status()
            conversation=web.post("/api/conversations",json={}).json()["conversation_id"]
            previous_trace=None
            first_passed=False
            for name,question,person in (
                ("total","¿Cuál es el saldo total de CHEQUES al cierre de marzo de 2026?",""),
                ("seguimiento_pf","¿Y solo para personas físicas?","PF"),
                ("permisos","¿Qué permisos tiene mi perfil en esta demo?",None),
            ):
                entry={"case":name,"question":question,"passed":False}
                report["cases"].append(entry)
                if name=="seguimiento_pf" and not first_passed:
                    entry.update(status="pending",reason="No hay un total previo aprobado para comprobar el seguimiento.")
                    save()
                    continue
                tick=time.perf_counter()
                offset=len(AUDIT.read_text(encoding="utf-8").splitlines()) if AUDIT.exists() else 0
                try:
                    response=web.post("/api/chat",json={"question":question,"conversation_id":conversation})
                    entry["http_status"]=response.status_code
                    entry["response"]=response.json()
                    records=audit_since(offset)
                    assert len(records)==1,"La consulta debe generar un registro de auditoría."
                    record=records[0]
                    trace=record["trace"]
                    entry["trace"]=trace
                    response.raise_for_status()
                    data=response.json()
                    assert data["metrics"]["provider"]=="gemini"
                    assert data["metrics"]["model"]=="gemini-3.5-flash-lite"
                    tools=[e.get("tool") for e in trace if e["event"]=="tool_started"]
                    entry["tools_started"]=tools
                    if person is not None:
                        assert "data_analytics" in tools and "consultar_datos" in tools
                        assert "data_governance" not in tools and "explicar_politica_sesion" not in tools
                        assert "Perfil de sesión:" not in data["answer"] and "permisos" not in data["answer"].lower()
                        plans=[e for e in trace if e["event"]=="validated_plan" and e["status"]=="ok"]
                        assert len(plans)==1,"Se espera una única ejecución SQL verificada."
                        plan=plans[0]["plan"]
                        assert plan["producto"]=="CHEQUES" and plan["desde"]==plan["hasta"]=="2026-03"
                        assert plan["persona"]==person and plan["alcance"]=="global"
                        row=plans[0]["rows"][0]
                        assert Decimal(row["valor"])==Decimal(expected[person]["total"])
                        assert row["filas"]==expected[person]["rows"]
                        assert any(e["event"]=="authorization" and e["allowed"] for e in trace)
                        integer,fraction=expected[person]["total"].split(".")
                        formatted=f"{int(integer):,}".replace(",",".")+","+fraction
                        assert formatted in data["answer"]
                        assert data["sources"],"Debe conservar fuentes."
                        current=next(e for e in trace if e["event"]=="conversation_session")
                        if person=="PF":
                            assert current["reused"] and current["session_id"]==previous_trace["session_id"]
                            snapshot=next(e for e in trace if e["event"]=="context_snapshot")
                            assert snapshot["last_ids"] and snapshot["results"][0]["plan"]["producto"]=="CHEQUES"
                        else:
                            assert not current["reused"]
                            previous_trace=current
                        entry["sql_match"]=True
                    else:
                        assert "data_governance" in tools and "explicar_politica_sesion" in tools
                        assert "data_analytics" not in tools and "data_quality" not in tools
                        assert not any(e["event"]=="validated_plan" and e.get("sql_executed") for e in trace)
                        assert any(e["event"]=="governance_result" and e["profile"]=="Analista" for e in trace)
                        assert "Perfil de sesión: Analista" in data["answer"]
                    entry.update(passed=True,status="passed")
                except Exception as error:
                    # No registrar el texto de errores SDK ni configuración de credenciales.
                    entry.update(status="failed",error_type=type(error).__name__)
                finally:
                    entry["seconds"]=time.perf_counter()-tick
                    if name=="total":
                        first_passed=entry["passed"]
                    save()
                    print(json.dumps({key:entry.get(key) for key in ("case","status","seconds","http_status","tools_started")},ensure_ascii=False),flush=True)
        report["passed"]=all(case["passed"] for case in report["cases"])
        return 0 if report["passed"] else 1
    except Exception as error:
        report["error_type"]=type(error).__name__
        return 1
    finally:
        save()


if __name__=="__main__":
    raise SystemExit(main())
