"""Piloto real local: retrieval -> Steward -> API. No publica evaluaciones."""
import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

QUESTIONS = ["¿Qué necesito para sacar un crédito de nómina?",
             "¿Cuál es el CAT del crédito de nómina?",
             "¿Cuál es el horario de la sucursal de Marte?"]


async def steward():
    from typing import Any
    from google.adk.agents import BaseAgent, LlmAgent
    from google.adk.events import Event
    from google.adk.runners import InMemoryRunner
    from google.adk.tools.tool_context import ToolContext
    from google.genai import types
    from banco_agentes.general_agents import Turn
    from banco_agentes.runtime import Trace
    from banco_agentes.providers import create_model
    from banco_agentes.steward_rag import answer_documents

    class Driver(BaseAgent):
        turn: Any
        writer: Any
        question: str
        async def _run_async_impl(self, ctx):
            result = await answer_documents(self.writer, self.turn, self.question, ToolContext(ctx))
            yield Event(author=self.name, content=types.Content(role="model",
                parts=[types.Part(text=json.dumps(result,ensure_ascii=False))]))

    reports = []
    os.environ["BANCO_MODEL_PROVIDER"] = "gemini"
    for q in QUESTIONS:
        trace = Trace(lambda _:None)
        turn = Turn(trace,{})
        writer = LlmAgent(name="data_steward",model=create_model("data_steward",trace))
        runner = InMemoryRunner(agent=Driver(name="pilot",turn=turn,writer=writer,question=q))
        session = await runner.session_service.create_session(app_name=runner.app_name,user_id="public-pilot")
        item = {"question":q}
        try:
            async with asyncio.timeout(240):
                async for event in runner.run_async(user_id=session.user_id,session_id=session.id,
                        new_message=types.Content(role="user",parts=[types.Part(text=q)])):
                    if event.author=="pilot":
                        item["result"]=json.loads(event.content.parts[0].text)
            item["status"]="ok"
        except Exception as error:
            item.update(status="technical_failure",error_type=type(error).__name__)
        finally:
            await runner.close()
        item["trace"]=trace.records
        reports.append(item)
        print(json.dumps({k:v for k,v in item.items() if k!="trace"},ensure_ascii=True),flush=True)
    return reports


def retrieve():
    from banco_agentes.rag import Retriever,Settings
    reports=[]
    for mode in ["vectorial","lexico","hibrido"]:
        for q in QUESTIONS:
            r=Retriever(Settings(mode=mode)).search(q)
            reports.append({"question":q,**r})
    return reports


def web(base):
    import httpx
    from banco_agentes.web import ACCOUNTS
    reports=[]
    with httpx.Client(base_url=base,timeout=450,trust_env=False) as c:
        response=c.post("/api/login",json={"username":"cliente","password":ACCOUNTS["cliente"][0]})
        response.raise_for_status()
        for q in QUESTIONS:
            cid=c.post("/api/conversations").json()["conversation_id"]
            response=c.post("/api/chat",json={"question":q,"conversation_id":cid})
            body=response.json()
            item={"question":q,"http_status":response.status_code,"response":body}
            trace_id=body.get("trace_id")
            if trace_id:
                for line in (ROOT/"verification-web.jsonl").read_text(encoding="utf-8").splitlines():
                    record=json.loads(line)
                    if record.get("trace_id")==trace_id:
                        item["audit"]=record
            reports.append(item)
            print(json.dumps({"http_status":response.status_code,"trace_id":trace_id,
                              "agents":body.get("agents"),"answer":body.get("answer",body.get("detail"))},
                             ensure_ascii=True),flush=True)
    return reports


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--stage",choices=["retrieval","steward","web"],required=True)
    parser.add_argument("--base-url",default="http://127.0.0.1:8001")
    parser.add_argument("--questions-file")
    parser.add_argument("--report")
    args=parser.parse_args()
    global QUESTIONS
    if args.questions_file:
        QUESTIONS=json.loads((ROOT/args.questions_file).read_text(encoding="utf-8"))
    if args.stage=="steward":report=asyncio.run(steward())
    elif args.stage=="web":report=web(args.base_url)
    else:report=retrieve()
    path=ROOT/(args.report or f"verification-rag-{args.stage}-real.json")
    path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")


if __name__=="__main__":
    main()
