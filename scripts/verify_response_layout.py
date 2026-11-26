"""Contrasta presentación con SQL de solo lectura y prepara una página de QA del renderizador real."""
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from banco_agentes.analytics import connect_readonly
from banco_agentes.conversation import render_turn,presentation_fields
from banco_agentes.general_agents import Turn
from banco_agentes.governance import SESSION,REQUEST,AUDIT,DemoSession
from banco_agentes.response_layout import PresentationChoice
from banco_agentes.runtime import Trace
from banco_agentes.semantics import DATASETS


def main():
    started=time.perf_counter()
    report={"passed":False,"cases":[]}
    a=SESSION.set(DemoSession())
    try:
        with connect_readonly() as db:
            report["db_user"]=db.execute("SELECT current_user").fetchone()[0]
            direct={product:format(db.execute("SELECT sum(saldo_mes) FROM cfg.fact_sdos_captacoloca_m WHERE cve_producto=%s AND fec_informacion=%s",(product,20260630)).fetchone()[0],"f") for product in ("CHEQUES","AHORRO")}
            series=db.execute("SELECT fec_informacion/100 AS mes,sum(saldo_mes) FROM cfg.fact_sdos_captacoloca_m WHERE cve_producto=%s AND fec_informacion BETWEEN %s AND %s GROUP BY 1 ORDER BY 1",("CHEQUES",20260331,20260630)).fetchall()
        report["sql_direct"]={"june_totals":direct,"series":[[m,format(v,"f")] for m,v in series]}
        def plan(product,*,series=False):
            return dict(metrica="total",conjunto="saldos",desde="2026-03" if series else "2026-06",hasta="2026-06",producto=product,
                        persona="",segmento="",agrupar="mes" if series else "",campo="",codigo=0,base="total",minimo=None,maximo=None,alcance="global")
        for name,question,products,choice,series_mode in (
            ("puntual","Saldo total AHORRO junio de 2026",["AHORRO"],PresentationChoice(),False),
            ("comparacion","Compara CHEQUES y AHORRO de junio: ¿cuál fue mayor?",["CHEQUES","AHORRO"],PresentationChoice(formato="tabla",objetivo="comparar"),False),
            ("tabla_explicita","Dame el total de AHORRO de junio en una tabla",["AHORRO"],PresentationChoice(formato="frase"),False),
            ("tendencia","Muéstrame la evolución de CHEQUES de marzo a junio",["CHEQUES"],PresentationChoice(formato="grafica",objetivo="tendencia",tipo_grafica="linea"),True),
            ("conceptual","¿Qué representa el saldo de cierre?",[],PresentationChoice(formato="prosa"),False),
        ):
            tick=time.perf_counter()
            turn=Turn(Trace(lambda _:None),{})
            b,c=REQUEST.set(question),AUDIT.set(turn.trace)
            try:
                for product in products:
                    result=turn.execute(plan(product,series=series_mode))
                    assert result["status"]=="ok",result
                    if series_mode:
                        assert [[row["mes"],row["valor"]] for row in result["rows"]]==report["sql_direct"]["series"]
                    else:
                        assert result["rows"][0]["valor"]==direct[product]
                turn.presentation_choice=choice
                if not products:
                    turn.documentation=["saldo_mes representa el saldo de cierre de cada cuenta, producto y corte mensual. Los cierres son existencias y no se suman entre meses."]
                    turn.sources=list(DATASETS["saldos"].sources)
                answer,charts=render_turn(turn,"")
                fields=presentation_fields(turn)
                payload=dict(answer=answer,charts=charts,**fields,agents=[],tools=[],elapsed_seconds=time.perf_counter()-tick)
                if name=="puntual":
                    assert fields["presentation"]["format"]=="frase" and not fields["tables"]
                if name=="tabla_explicita":
                    assert fields["presentation"]["format"]=="tabla" and len(fields["tables"][0]["rows"])==1
                if name=="comparacion":
                    assert "mayor corresponde a CHEQUES" in answer and len(fields["tables"][0]["rows"])==2
                if name=="tendencia":
                    assert charts[0]["type"]=="linea" and len(charts[0]["points"])==4
                report["cases"].append(dict(case=name,question=question,passed=True,response=payload,trace=turn.trace.records,seconds=time.perf_counter()-tick))
            finally:
                REQUEST.reset(b);AUDIT.reset(c)
        report["passed"]=True
    except Exception as error:
        report["error"]=type(error).__name__+": "+str(error)
    finally:
        SESSION.reset(a)
        report["seconds"]=time.perf_counter()-started
        (ROOT/"verification-response-layout-sql.json").write_text(json.dumps(report,indent=2,ensure_ascii=False),encoding="utf-8")
    if report["passed"]:
        assets=ROOT/"banco_agentes/web_assets"
        html="""<!doctype html><html lang="es"><meta charset="utf-8"><title>Banco Agentes · QA de presentación</title>
<link rel="stylesheet" href="STYLE"><link rel="stylesheet" href="CHARTSTYLE">
<style>body{overflow:auto;display:block;padding:28px;background:#EDF6F7}main{max-width:1040px;margin:auto}.message.assistant{max-width:100%;margin:20px 0;padding:26px}#checks{display:block;white-space:pre-wrap;color:#003746}</style>
<main><h1>Banco Agentes · Presentación</h1><p>Demo académica · Datos sintéticos</p><output id="checks">PENDIENTE</output><div id="cards"></div></main>
<script src="CHARTSCRIPT"></script><script src="RESPONSESCRIPT"></script><script>
const cases=PAYLOAD;
const checks=[];
for(const entry of cases){const data=entry.response;const card=document.createElement('article');card.className='message assistant';card.append(textNode('h2',entry.case));const answer=textNode('div',data.answer,'answer');card.append(answer);renderResponseVisuals(card,data);renderResponseExtras(card,data);document.getElementById('cards').append(card);
const format=data.presentation.format;const sources=card.querySelector('.response-sources');const details=card.querySelector('.query-details');const count=card.querySelectorAll('.response-table tbody tr').length;
checks.push({case:entry.case,format,rows:count,figures:card.querySelectorAll('figure').length,passed:sources===card.lastElementChild&&!sources.open&&!details.open&&((format==='tabla'&&count>0)||(format==='grafica'&&!!card.querySelector('svg[role=img]'))||(['frase','prosa'].includes(format)&&count===0))});}
document.getElementById('checks').textContent=JSON.stringify({passed:checks.every(x=>x.passed),checks});
</script></html>"""
        for token,file in (("CHARTSTYLE","charts.css"),("STYLE","style.css"),("CHARTSCRIPT","charts.js"),("RESPONSESCRIPT","responses.js")):
            html=html.replace(token,(assets/file).as_uri())
        html=html.replace("PAYLOAD",json.dumps(report["cases"],ensure_ascii=False).replace("<","\\u003c"))
        (ROOT/"verification-response-layout.html").write_text(html,encoding="utf-8")
    print(json.dumps({key:report.get(key) for key in ("passed","db_user","sql_direct","seconds","error")},ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__=="__main__":
    raise SystemExit(main())
