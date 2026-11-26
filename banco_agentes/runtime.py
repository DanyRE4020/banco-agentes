import asyncio
import json
import time

from google.adk.agents.run_config import RunConfig
from google.adk.runners import InMemoryRunner
from google.genai import types

from .agents import build_agents
from .governance import SESSION, REQUEST, AUDIT, DemoSession, request_denial


class Trace:
    def __init__(self, emit=print):
        self.records = []
        self.emit = emit

    def record(self, kind, agent, tool=None):
        entry = {"event": kind, "agent": agent}
        if tool:
            entry["tool"] = tool
        self.records.append(entry)
        self.emit("[traza] " + json.dumps(entry, ensure_ascii=False))

    def before_agent(self, callback_context):
        self.record("agent_started", callback_context.agent_name)

    def before_tool(self, tool, args, tool_context):
        self.record("tool_started", tool_context.agent_name, tool.name)

    def after_tool(self, tool, args, tool_context, tool_response):
        self.record("tool_completed", tool_context.agent_name, tool.name)


async def ask(question: str, emit=print, model_factory=None, timeout=180, session=None) -> dict:
    trace = Trace(emit)
    identity = session or DemoSession()
    tokens = (SESSION.set(identity), REQUEST.set(question), AUDIT.set(trace))
    started = time.perf_counter()
    runner = None
    answer = ""
    try:
        trace.records.append(dict(event="session_policy", agent="policy_code", profile=identity.profile,
                                  request_denied=request_denial(), identity_source="trusted_session_not_question"))
        if request_denial():
            trace.records.append(dict(event="access_blocked", agent="policy_code", profile=identity.profile,
                                      reason=request_denial(), sql_executed=False))
        runner = InMemoryRunner(agent=build_agents(trace, model_factory, question), app_name="banco_agentes")
        async with asyncio.timeout(timeout):
            adk_session = await runner.session_service.create_session(app_name="banco_agentes", user_id="demo")
            async for event in runner.run_async(user_id="demo", session_id=adk_session.id,
                    new_message=types.Content(role="user", parts=[types.Part(text=question)]),
                    run_config=RunConfig(max_llm_calls=10)):
                if event.author == "orquestador" and event.is_final_response() and event.content:
                    answer = "".join(part.text for part in event.content.parts or []
                                     if part.text and not part.thought)
                    if not answer and event.actions.skip_summarization:
                        for part in event.content.parts or []:
                            response = part.function_response
                            if response and response.name in {"data_steward", "data_analytics", "data_quality", "data_governance"}:
                                value = response.response.get("result")
                                if isinstance(value, str):
                                    answer = value
        if not answer:
            raise RuntimeError("ADK no produjo una respuesta final del orquestador.")
        return {"question": question, "answer": answer, "trace": trace.records,
                "total_seconds": time.perf_counter() - started}
    except Exception as error:
        error.run_result = {"question": question, "answer": answer, "trace": trace.records,
                            "total_seconds": time.perf_counter() - started}
        raise
    finally:
        try:
            if runner:
                await runner.close()
        finally:
            SESSION.reset(tokens[0])
            REQUEST.reset(tokens[1])
            AUDIT.reset(tokens[2])
