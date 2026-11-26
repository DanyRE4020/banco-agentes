"""Renderiza respuestas reales con los componentes de producción; no llama al modelo."""
import argparse
import html
import json
import re
import subprocess
import tempfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
ASSETS=ROOT/"banco_agentes/web_assets"


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--browser", default=r"C:\Program Files\Google\Chrome\Application\chrome.exe")
    args=parser.parse_args()
    cases=json.loads((ROOT/"verification-rag-style-real.json").read_text(encoding="utf-8"))
    for case in cases:
        assert case["http_status"]==200
        trace=case["audit"]["trace"]
        events=[e["event"] for e in trace]
        assert events.index("rag_retrieval")<events.index("rag_writer_started")
        assert set(case["response"]["agents"])=={"orquestador","data_steward"}
        assert all(h["source"]["pilot_allowed"] for e in trace if e["event"]=="rag_retrieval" for h in e["hits"])
        answer=case["response"]["answer"].lower()
        assert not any(s in answer for s in ["resumen académico","vigencia no verificada","fragmentos","source.requirements",".pdf","referencias/"])
    app=(ASSETS/"app.js").read_text(encoding="utf-8")
    answer_function=app[app.index("function answerText("):app.index("$('chat-form').addEventListener")]
    page='''<!doctype html><html lang="es"><meta charset="utf-8"><title>Respuestas reales del chat</title>
<link rel="stylesheet" href="__STYLE__"><link rel="stylesheet" href="__CHART_STYLE__">
<body><main class="conversation" id="messages" style="max-width:1000px;margin:auto;overflow:visible">
<h1>Respuestas reales del chat</h1><p>Piloto Cliente · Gemini y recuperación local</p></main>
<output id="checks" hidden>PENDIENTE</output>
<script src="__CHARTS__"></script><script src="__RESPONSES__"></script><script>
__ANSWER_FUNCTION__
const cases=__CASES__;
for(const item of cases){
 const user=document.createElement("div");user.className="message user";user.textContent=item.question;
 const card=document.createElement("article");card.className="message assistant";
 const heading=document.createElement("div");heading.className="message-heading";heading.textContent="Banco Agentes";
 const answer=document.createElement("div");answer.className="answer";answerText(answer,item.response.answer);
 card.append(heading,answer);renderResponseExtras(card,item.response);
 document.getElementById("messages").append(user,card);
}
const cards=[...document.querySelectorAll("article")];
const checks=cards.map((card,i)=>({
 answer_matches:card.querySelector(".answer").textContent===cases[i].response.answer,
 sources_last:card.lastElementChild.classList.contains("response-sources"),
 source_titles:[...card.querySelectorAll(".response-sources strong")].map(x=>x.textContent),
 metadata_collapsed:[...card.querySelectorAll(".source-details")].every(x=>!x.open),
 metadata_present:card.querySelectorAll(".source-details").length===cases[i].response.sources.length,
 readable_locations:cases[i].response.sources.every(s=>s.location.startsWith("Página "))
}));
document.getElementById("checks").textContent=JSON.stringify({passed:checks.length===4&&checks.every(x=>x.answer_matches&&x.sources_last&&x.metadata_collapsed&&x.metadata_present&&x.readable_locations),checks});
</script></body></html>'''
    for key,file in [("__STYLE__","style.css"),("__CHART_STYLE__","charts.css"),("__CHARTS__","charts.js"),("__RESPONSES__","responses.js")]:
        page=page.replace(key,(ASSETS/file).as_uri())
    # Solo respuestas públicas del piloto; no se incorpora la auditoría en el HTML.
    page=page.replace("__ANSWER_FUNCTION__",answer_function).replace("__CASES__",json.dumps(
        [{"question":x["question"],"response":x["response"]} for x in cases],ensure_ascii=False).replace("<","\\u003c"))
    fixture=ROOT/"verification-rag-chat.html";fixture.write_text(page,encoding="utf-8")
    with tempfile.TemporaryDirectory(prefix="rag-render-",dir=ROOT) as folder:
        run=subprocess.run([args.browser,"--headless=new","--disable-gpu","--disable-background-networking",
            "--no-first-run","--no-default-browser-check",f"--user-data-dir={folder}",
            "--window-size=1200,2600",f"--screenshot={ROOT/'verification-rag-chat.png'}","--dump-dom",fixture.as_uri()],
            capture_output=True,encoding="utf-8",errors="replace",timeout=50)
        match=re.search(r'<output id="checks" hidden="">(.*?)</output>',run.stdout,re.S)
        if not match:
            match=re.search(r'<output id="checks"[^>]*>(.*?)</output>',run.stdout,re.S)
        assert match, "El navegador no devolvió la comprobación DOM."
        report=json.loads(html.unescape(match[1]))
    (ROOT/"verification-rag-render.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(report,ensure_ascii=True,indent=2))
    assert report["passed"]


if __name__=="__main__":
    main()
