"""Cliente local inyectado en ADK: conversión explícita sin parches globales."""

import copy
import json
import os
import time
import uuid

import httpx
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
from google.adk.models.lite_llm import LiteLLMClient
from litellm import ModelResponse


def native_messages(messages):
    result = []
    calls = {}
    for original in messages:
        message = copy.deepcopy(original if isinstance(original, dict) else original.model_dump(exclude_none=True))
        converted = {"role": message["role"], "content": message.get("content") or ""}
        if message.get("tool_calls"):
            converted["tool_calls"] = []
            for call in message["tool_calls"]:
                function = call["function"]
                arguments = function["arguments"]
                if isinstance(arguments, str):
                    arguments = json.loads(arguments)
                calls[call["id"]] = function["name"]
                converted["tool_calls"].append({"id": call["id"], "function": {
                    "name": function["name"], "arguments": arguments}})
        if message["role"] == "tool":
            call_id = message.get("tool_call_id")
            if call_id not in calls:
                raise ValueError("Resultado de herramienta sin llamada previa asociada.")
            converted.update(tool_name=calls[call_id], tool_call_id=call_id)
        result.append(converted)
    return result


class LocalOllamaClient(LiteLLMClient):
    def __init__(self, base, agent, trace, transport=None):
        self.base = base
        self.agent = agent
        self.trace = trace
        self.transport = transport

    async def acompletion(self, model, messages, tools=None, **kwargs):
        if kwargs.get("stream"):
            raise ValueError("Este cliente local solo admite inferencia no streaming.")
        payload = {"model": model.split("/", 1)[-1], "messages": native_messages(messages),
                   "stream": False, "options": {"temperature": kwargs.get("temperature", 0),
                                                "num_ctx": kwargs.get("num_ctx", 16384)}}
        if tools:
            payload["tools"] = tools
        if kwargs.get("num_predict") is not None:
            payload["options"]["num_predict"] = kwargs["num_predict"]
        entry = {"event": "model_call", "agent": self.agent, "status": "started",
                 "provider": "ollama", "model": payload["model"]}
        entry["tools_offered"] = [tool["function"]["name"] for tool in payload.get("tools", [])]
        entry["request_history"] = [{"role": item["role"],
            "calls": [{"id": call.get("id"), "name": call["function"]["name"]}
                      for call in item.get("tool_calls", [])],
            "tool_call_id": item.get("tool_call_id"), "tool_name": item.get("tool_name"),
            "content_chars": len(str(item.get("content", "")))} for item in payload["messages"]]
        self.trace.records.append(entry)
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(transport=self.transport, timeout=kwargs.get("timeout", 180)) as client:
                response = await client.post(self.base + "/api/chat", json=payload)
                response.raise_for_status()
                data = response.json()
            message = data["message"]
            entry["response_message"] = copy.deepcopy(message)
            tool_calls = [{"id": call.get("id") or f"call_{uuid.uuid4().hex}", "type": "function",
                           "function": {"name": call["function"]["name"],
                                        "arguments": json.dumps(call["function"]["arguments"])}}
                          for call in message.get("tool_calls", [])]
            entry.update(status="completed", ollama_metrics={key: data.get(key) for key in (
                "total_duration", "load_duration", "prompt_eval_duration", "eval_duration", "prompt_eval_count", "eval_count")})
            return ModelResponse(model=payload["model"], choices=[{"index": 0,
                "finish_reason": "tool_calls" if tool_calls else "stop",
                "message": {"role": "assistant", "content": message.get("content", ""), "tool_calls": tool_calls or None}}])
        finally:
            entry["seconds"] = time.perf_counter() - started
            if entry["status"] == "started":
                entry["status"] = "failed_or_cancelled"
            self.trace.emit("[modelo] " + json.dumps(entry, ensure_ascii=False))
