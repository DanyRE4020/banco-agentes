"""Diagnóstico real de tool calling con sesiones independientes y trazas JSON."""

import argparse
import asyncio
import copy
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

IMPORT_STARTED = time.perf_counter()
from banco_agentes.agents import local_settings
from litellm import acompletion
from litellm.llms.ollama.chat.transformation import OllamaChatConfig
IMPORT_SECONDS = time.perf_counter() - IMPORT_STARTED

TOOL_NAME = "obtener_estado_prueba"
EXPECTED = "LOCAL_TOOL_OK_731"
TOOLS = [{"type": "function", "function": {
    "name": TOOL_NAME,
    "description": "Devuelve una etiqueta de diagnóstico de esta comprobación local de software.",
    "parameters": {"type": "object", "properties": {}, "required": []},
}}]
OPTIONS = {"temperature": 0, "seed": 42, "num_ctx": 4096}


def fresh_messages():
    prompts = Path(__file__).parent / "prompts"
    return [{"role": role, "content": (prompts / f"prueba_herramienta_{role}.txt").read_text(encoding="utf-8").strip()}
            for role in ("system", "user")]


def obtener_estado_prueba():
    return {"estado": EXPECTED}


def append_tool_result(messages, assistant, native=False):
    if assistant.get("role") != "assistant":
        raise RuntimeError("La respuesta no tiene role=assistant.")
    calls = assistant.get("tool_calls") or []
    if len(calls) != 1:
        raise RuntimeError("Se esperaba exactamente una llamada real de herramienta del modelo.")
    call = calls[0]
    function = call.get("function", {})
    if function.get("name") != TOOL_NAME:
        raise RuntimeError("El modelo solicitó una herramienta inesperada.")
    arguments = function.get("arguments")
    if not native:
        if not isinstance(arguments, str) or not call.get("id") or call.get("type") != "function":
            raise RuntimeError("Llamada LiteLLM sin argumentos JSON, tipo function o ID válido.")
        arguments = json.loads(arguments)
    if arguments != {}:
        raise RuntimeError("La herramienta no acepta argumentos.")
    messages.append(copy.deepcopy(assistant))
    print(f"tool_started: {TOOL_NAME}; id={call.get('id', 'nativo-por-nombre')}")
    result = obtener_estado_prueba()
    print("tool_completed: " + json.dumps(result))
    response = {"role": "tool", "content": json.dumps(result)}
    if native:
        response["tool_name"] = function["name"]
    else:
        response.update(tool_call_id=call["id"], name=function["name"])
    messages.append(response)


def validate_final(message):
    if message.get("tool_calls"):
        raise RuntimeError("El modelo pidió otra herramienta en lugar de integrar el resultado.")
    answer = message.get("content") or ""
    print("Respuesta: " + answer)
    if EXPECTED not in answer:
        raise RuntimeError("La respuesta no contiene el resultado de la herramienta.")


def projected_payload(model, messages):
    return OllamaChatConfig().transform_request(model=model,
        messages=copy.deepcopy(messages), optional_params={**OPTIONS, "tools": copy.deepcopy(TOOLS)},
        litellm_params={}, headers={})


async def native_request(base, payload, timeout):
    def post():
        request = urllib.request.Request(base + "/api/chat",
            data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    return await asyncio.to_thread(post)


async def run_probe(mode, base, model, timeout):
    probe_started = time.perf_counter()
    messages = fresh_messages()
    report = {"mode": mode, "model": model, "requests": [], "responses": []}
    try:
        async with asyncio.timeout(timeout):
            for turn in (1, 2):
                payload = {"model": model, "messages": copy.deepcopy(messages),
                           "tools": copy.deepcopy(TOOLS), "options": OPTIONS.copy(), "stream": False}
                entry = {"turn": turn, "payload": payload}
                if mode == "litellm":
                    projection_started = time.perf_counter()
                    entry["projected_ollama_payload"] = projected_payload(model, messages)
                    entry["projection_seconds"] = time.perf_counter() - projection_started
                    entry["assistant_calls_input"] = sum(len(item.get("tool_calls") or []) for item in messages)
                    entry["assistant_calls_projected"] = sum(len(item.get("tool_calls") or []) for item in entry["projected_ollama_payload"]["messages"])
                report["requests"].append(entry)
                print(f"\n{mode} solicitud {turn}: " + json.dumps(entry, ensure_ascii=False))
                request_started = time.perf_counter()
                entry["request_started_offset_seconds"] = request_started - probe_started
                if mode == "litellm":
                    response = await acompletion(model=f"ollama_chat/{model}", api_base=base,
                        messages=copy.deepcopy(messages), tools=copy.deepcopy(TOOLS),
                        **OPTIONS, timeout=timeout, num_retries=0)
                    message = response.choices[0].message.model_dump(exclude_none=True)
                else:
                    response = await native_request(base, payload, timeout)
                    message = response["message"]
                    entry["ollama_metrics"] = {key: response.get(key) for key in (
                        "total_duration", "load_duration", "prompt_eval_duration", "eval_duration",
                        "prompt_eval_count", "eval_count")}
                entry["request_seconds"] = time.perf_counter() - request_started
                if mode != "litellm" and response.get("total_duration") is not None:
                    entry["wall_minus_server_seconds"] = entry["request_seconds"] - response["total_duration"] / 1e9
                print(f"TIEMPO {mode} llamada {turn}: {entry['request_seconds']:.3f} s", flush=True)
                report["responses"].append(copy.deepcopy(message))
                print(f"{mode} respuesta {turn}: " + json.dumps(message, ensure_ascii=False))
                if turn == 1:
                    tool_started = time.perf_counter()
                    append_tool_result(messages, message, native=mode == "ollama_directo")
                    entry["tool_dispatch_seconds"] = time.perf_counter() - tool_started
                else:
                    validate_final(message)
        report["status"] = "passed"
        print(f"OK: {mode}, herramienta real y resultado integrado.")
    except Exception as error:
        if report["requests"] and "request_seconds" not in report["requests"][-1]:
            report["requests"][-1]["failed_request_seconds"] = time.perf_counter() - request_started
        report.update(status="failed", error=f"{type(error).__name__}: {error}")
        print(report["error"])
    report["elapsed_seconds"] = time.perf_counter() - probe_started
    return report


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--comparar", action="store_true", help="Ejecutar también Ollama directo aunque LiteLLM pase.")
    args = parser.parse_args()
    base, model, timeout = local_settings()
    results = [await run_probe("litellm", base, model, timeout)]
    if args.comparar or results[0]["status"] != "passed":
        results.append(await run_probe("ollama_directo", base, model, timeout))
    report_path = ROOT / "verification-tools.json"
    report_path.write_text(json.dumps({"import_seconds": IMPORT_SECONDS, "results": results}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\nInforme: {report_path}")
    print("La proyección del adaptador no es una captura HTTP; cada comparación usa mensajes nuevos.")
    return int(any(result["status"] != "passed" for result in results))


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
