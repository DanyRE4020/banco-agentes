"""Selección explícita de proveedor, con Ollama como valor predeterminado."""
import asyncio
import json
import math
import re
from contextvars import ContextVar
from email.utils import parsedate_to_datetime
import os
import time
from functools import cached_property
from urllib.parse import urlparse

os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

from dotenv import load_dotenv
from google.adk.models.google_llm import Gemini
from google.adk.models.lite_llm import LiteLlm
from google.genai import Client, types
from pydantic import PrivateAttr

from .catalog import ROOT
from .ollama_client import LocalOllamaClient

GEMINI_MODEL = "gemini-3.5-flash-lite"
MODEL_DEADLINE = ContextVar("banco_model_deadline", default=None)
MAX_MODEL_ATTEMPTS = 3


def rate_limit_details(error, api_key):
    """Extrae solo cuota y espera; excluye mensajes, cabeceras y credenciales."""
    payload = getattr(error, "details", None)
    if isinstance(payload, dict):
        payload = payload.get("error", payload)
        payload = payload.get("details", []) if isinstance(payload, dict) else []
    if not isinstance(payload, list):
        payload = []
    secrets = [api_key] + [value for key, value in os.environ.items()
                          if re.search(r"key|token|password|secret|credential", key, re.I)]

    def safe_quota_value(value):
        if not isinstance(value, (str, int, float)) or isinstance(value, bool):
            return None
        text = str(value)
        if any(secret and secret in text for secret in secrets):
            return "[REDACTADO]"
        if re.fullmatch(r"[A-Za-z0-9_./-]{1,180}", text) and not re.search(r"AIza|lsv2_|eyJ", text):
            return text
        return None

    delays, quotas = [], []
    for item in payload:
        if not isinstance(item, dict):
            continue
        kind = item.get("@type")
        if kind == "type.googleapis.com/google.rpc.RetryInfo":
            value = item.get("retryDelay")
            if isinstance(value, str) and re.fullmatch(r"[0-9]+(?:[.][0-9]+)?s", value):
                delay = float(value[:-1])
                if math.isfinite(delay):
                    delays.append((delay, "RetryInfo"))
        elif kind == "type.googleapis.com/google.rpc.QuotaFailure":
            violations = item.get("violations", [])
            for violation in violations[:10] if isinstance(violations, list) else []:
                if not isinstance(violation, dict):
                    continue
                safe = {key: value for key in ("quotaMetric", "quotaId", "quotaValue")
                        if (value := safe_quota_value(violation.get(key))) is not None}
                if safe:
                    quotas.append(safe)

    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None)
    value = (headers.get("Retry-After") or headers.get("retry-after")) if headers is not None else None
    if isinstance(value, str):
        try:
            delay = float(value)
        except ValueError:
            try:
                date = parsedate_to_datetime(value)
                delay = max(0, date.timestamp() - time.time()) if date.tzinfo is not None else None
            except (ValueError, TypeError, OverflowError):
                delay = None
        if delay is not None and math.isfinite(delay) and delay >= 0:
            delays.append((delay, "Retry-After"))
    result = {"quota": quotas} if quotas else {}
    if delays:
        delay, source = max(delays)
        result.update(retry_delay_seconds=delay, retry_delay_source=source)
    return result


def load_environment():
    path = ROOT / ".env"
    encoding = "utf-8-sig"
    if path.exists() and path.read_bytes()[:2] in (b"\xff\xfe", b"\xfe\xff"):
        encoding = "utf-16"
    load_dotenv(path, override=False, encoding=encoding)


def model_settings(agent_name="orquestador"):
    load_environment()
    provider = os.getenv("BANCO_MODEL_PROVIDER", "ollama").strip().lower()
    if provider not in {"ollama", "gemini"}:
        raise ValueError("BANCO_MODEL_PROVIDER debe ser ollama o gemini.")
    timeout = float(os.getenv("REQUEST_TIMEOUT_SECONDS", "180"))
    if timeout <= 0:
        raise ValueError("REQUEST_TIMEOUT_SECONDS debe ser positivo.")
    if provider == "gemini":
        variable = "BANCO_HUB_MODEL" if agent_name == "orquestador" else "BANCO_SPOKES_MODEL"
        model = os.getenv(variable, "").strip() or GEMINI_MODEL
    else:
        model = os.getenv("OLLAMA_MODEL", "llama3.1:8b")
    return provider, model, timeout


def local_settings():
    load_environment()
    base = os.getenv("OLLAMA_API_BASE", "http://localhost:11434").rstrip("/")
    parsed = urlparse(base)
    if (parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
            or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path):
        raise ValueError("OLLAMA_API_BASE debe ser HTTP local, sin rutas, credenciales ni parámetros.")
    model = os.getenv("OLLAMA_MODEL", "llama3.1:8b")
    if model != "llama3.1:8b":
        raise ValueError("El proveedor Ollama utiliza llama3.1:8b local.")
    os.environ["OLLAMA_API_BASE"] = base
    os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    _, _, timeout = model_settings()
    return base, model, timeout


class ModelCallError(RuntimeError):
    """Error público de proveedor con información de diagnóstico sin credenciales."""


class MeteredGemini(Gemini):
    _trace = PrivateAttr()
    _agent_name = PrivateAttr()
    _api_key = PrivateAttr()
    _timeout = PrivateAttr()

    def __init__(self, *, agent_name, trace, api_key, timeout, model=GEMINI_MODEL):
        super().__init__(model=model)
        self._agent_name = agent_name
        self._trace = trace
        self._api_key = api_key
        self._timeout = timeout

    @cached_property
    def api_client(self):
        return Client(vertexai=False, api_key=self._api_key, http_options=types.HttpOptions(
            timeout=int(self._timeout * 1000), retry_options=types.HttpRetryOptions(attempts=1),
            headers=self._tracking_headers()))

    async def generate_content_async(self, llm_request, stream=False):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._timeout
        turn_deadline = MODEL_DEADLINE.get()
        if turn_deadline is not None:
            deadline = min(deadline, turn_deadline)
        async with asyncio.timeout_at(deadline):
            for attempt in range(1, MAX_MODEL_ATTEMPTS + 1):
                emitted = False
                try:
                    # Repite solo Gemini; Runner, AgentTool y SQL no se reinician.
                    async for response in self._generate_once(llm_request, stream, attempt):
                        emitted = True
                        yield response
                    return
                except ModelCallError as error:
                    if getattr(error, "code", None) != 429:
                        raise
                    details = error.rate_limit
                    delay = details.get("retry_delay_seconds", float(2 ** (attempt - 1)))
                    retry = {"event": "model_retry", "agent": self._agent_name,
                             "attempt": attempt, "delay_seconds": delay,
                             "delay_source": details.get("retry_delay_source", "backoff")}
                    self._trace.records.append(retry)
                    if emitted or attempt == MAX_MODEL_ATTEMPTS or delay >= deadline - loop.time():
                        retry["status"] = "skipped"
                        retry["reason"] = ("response_already_emitted" if emitted else
                                           "attempt_limit" if attempt == MAX_MODEL_ATTEMPTS else "turn_budget")
                        raise
                retry["status"] = "waiting"
                try:
                    await asyncio.sleep(delay)
                except asyncio.CancelledError:
                    retry["status"] = "cancelled"
                    raise
                retry["status"] = "completed"

    async def _generate_once(self, llm_request, stream, attempt):
        if llm_request.config.max_output_tokens is None:
            llm_request.config.max_output_tokens = 2048
        entry = {"event": "model_call", "agent": self._agent_name, "provider": "gemini",
                 "model": self.model, "attempt": attempt, "status": "started", "usage": None}
        self._trace.records.append(entry)
        started = time.perf_counter()
        try:
            async for response in super().generate_content_async(llm_request, stream=stream):
                if response.usage_metadata is not None:
                    entry["usage"] = response.usage_metadata.model_dump(mode="json", exclude_none=True)
                if response.error_code:
                    entry["status"] = "failed"
                    entry["error_code"] = str(response.error_code)[:50]
                yield response
            if entry["status"] == "started":
                entry["status"] = "completed"
        except asyncio.CancelledError:
            entry["status"] = "cancelled"
            raise
        except Exception as error:
            entry["status"] = "failed"
            entry["error_type"] = type(error).__name__
            cause = getattr(error, "os_error", None)
            if cause is not None:
                entry["os_error"] = {"type": type(cause).__name__, "errno": getattr(cause, "errno", None),
                                     "winerror": getattr(cause, "winerror", None)}
            code = getattr(error, "code", None)
            if isinstance(code, int):
                entry["error_code"] = code
            details = rate_limit_details(error, self._api_key) if code == 429 else {}
            entry.update(details)
            failure = ModelCallError(
                f"Gemini no completó la llamada ({type(error).__name__}, "
                f"código {code if isinstance(code, int) else 'no disponible'}).")
            failure.code = code
            failure.rate_limit = details
            raise failure from None
        finally:
            if entry["status"] == "started":
                entry["status"] = "cancelled"
            entry["seconds"] = time.perf_counter() - started
            self._trace.emit("[modelo] " + json.dumps(entry, ensure_ascii=False))


def create_local_model(name, trace, *, num_predict=None):
    base, model, timeout = local_settings()
    options = {} if num_predict is None else {"num_predict": num_predict}
    return LiteLlm(model=f"ollama_chat/{model}", api_base=base,
                   llm_client=LocalOllamaClient(base, name, trace),
                   temperature=0, timeout=timeout, num_ctx=16384, **options)


def create_model(name, trace, *, num_predict=None):
    provider, model, timeout = model_settings(name)
    if provider == "ollama":
        return create_local_model(name, trace, num_predict=num_predict)
    key = os.getenv("GOOGLE_API_KEY", "").strip()
    if not key:
        raise ValueError("Falta GOOGLE_API_KEY en el .env del proyecto o en el entorno del proceso.")
    return MeteredGemini(agent_name=name, trace=trace, api_key=key, timeout=timeout, model=model)


def validate_provider():
    provider, model, _ = model_settings()
    if provider == "gemini" and not os.getenv("GOOGLE_API_KEY", "").strip():
        raise ValueError("Falta GOOGLE_API_KEY en el .env del proyecto o en el entorno del proceso.")
    if provider == "ollama":
        local_settings()
    if provider == "gemini":
        return {"provider": provider, "model": model, "hub_model": model,
                "spokes_model": model_settings("data_analytics")[1]}
    return {"provider": provider, "model": model}
