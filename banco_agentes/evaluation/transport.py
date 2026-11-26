"""Login por caso, turnos ordenados y correlación conservadora del último turno."""
import json
import os
import re
import time
from contextlib import suppress
from pathlib import Path
from urllib.parse import urlparse

import httpx


def sanitize(value, secrets=()):
    """No exporta cabeceras, contraseñas, cookies ni tokens; tampoco sus valores."""
    forbidden = re.compile(r"password|passwd|secret|cookie|authorization|api.?key|token|headers|credential|jwt|csrf|^auth$|username|db_user", re.I)
    if isinstance(value, dict):
        return {str(k): sanitize(v, secrets) for k, v in value.items() if not forbidden.search(str(k))}
    if isinstance(value, (list, tuple)):
        return [sanitize(v, secrets) for v in value]
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[REDACTADO]")
        value = re.sub(r"(?:lsv2_[\w-]+|AIza[\w-]{20,}|eyJ[\w-]+\.[\w-]+\.[\w-]+|Bearer\s+[^\s\"']+)", "[REDACTADO]", value)
    return value


def trace_id(record):
    return (record.get("diagnostics") or {}).get("trace_id") or record.get("langfuse_trace_id") or record.get("trace_id")


def correlate(response, records, *, question, profile):
    wanted = trace_id(response)
    if wanted:
        candidates = [r for r in records if trace_id(r) == wanted]
        method = "trace_id"
    else:
        timestamp = (response.get("metrics") or {}).get("timestamp_utc")
        if not timestamp:
            return None, {"status": "no_verificable", "reason": "Sin ID de traza ni timestamp del turno."}
        candidates = [r for r in records if (r.get("metrics") or {}).get("timestamp_utc") == timestamp
                      and r.get("question") == question and r.get("profile") == profile]
        method = "timestamp_pregunta_perfil"
    if len(candidates) != 1:
        return None, {"status": "no_verificable", "method": method, "matches": len(candidates),
                      "reason": "Falta una coincidencia única; no se usa la última línea como sustituto."}
    record = candidates[0]
    if record.get("question") != question or record.get("profile") != profile or not isinstance(record.get("trace"), list):
        return None, {"status": "no_verificable", "method": method, "reason": "Identidad del turno o traza incompatible."}
    return record, {"status": "ok", "method": method, "trace_id": wanted}


def appended_records(path, offset):
    path = Path(path)
    if not path.exists() or path.stat().st_size < offset:
        return []
    with path.open("rb") as handle:
        handle.seek(offset)
        data = handle.read().decode("utf-8-sig")
    rows = []
    for line in data.splitlines():
        try:
            value = json.loads(line)
            if isinstance(value, dict):
                rows.append(value)
        except ValueError:
            continue
    return rows


class LocalAPI:
    def __init__(self, url, audit, *, client_factory=None):
        parsed = urlparse(url)
        if (parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
                or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}):
            raise ValueError("La API debe ser HTTP local, sin rutas ni credenciales en la URL.")
        self.url, self.audit = url.rstrip("/"), Path(audit)
        self.client_factory = client_factory or (lambda: httpx.Client(base_url=self.url, timeout=450, trust_env=False, follow_redirects=False))

    def run(self, case):
        profile = case.row["perfil"]
        user = os.getenv(f"BANCO_EVAL_{profile.upper()}_USER", profile.lower())
        password = os.getenv(f"BANCO_EVAL_{profile.upper()}_PASSWORD", f"{profile}Demo!2026")
        secrets = [password, *(os.getenv(k) for k in ("LANGSMITH_API_KEY", "GOOGLE_API_KEY", "APP_DB_PASSWORD"))]
        result = {"case_id": case.id, "question": case.row["pregunta"], "profile": profile,
                  "case_sha256": case.digest, "status": "pendiente", "history": [], "final": None,
                  "trace": None, "correlation": {"status": "no_verificable"}}
        started = time.perf_counter()
        try:
            with self.client_factory() as web:
                response = web.post("/api/login", json={"username": user, "password": password})
                response.raise_for_status()
                try:
                    if response.json().get("profile") != profile:
                        raise ValueError("El servidor no asignó el perfil pedido.")
                    conversation = web.post("/api/conversations", json={})
                    conversation.raise_for_status()
                    conversation_id = conversation.json()["conversation_id"]
                    for index, question in enumerate([*case.previous, case.row["pregunta"]]):
                        final = index == len(case.previous)
                        offset = self.audit.stat().st_size if self.audit.exists() else 0
                        tick = time.perf_counter()
                        # Lista permitida: jamás se adjuntan las columnas de referencias.
                        response = web.post("/api/chat", json={"question": question, "conversation_id": conversation_id})
                        body = response.json()
                        try:
                            records = appended_records(self.audit, offset)
                        except (OSError, UnicodeError):
                            records = []
                        record, match = correlate(body, records, question=question, profile=profile)
                        observation = {"question": question, "http_status": response.status_code,
                                       "seconds": time.perf_counter() - tick, "response": body, "correlation": match}
                        if final:
                            result.update(final=observation, trace=record.get("trace") if record else None, correlation=match)
                        else:
                            result["history"].append(observation)
                        if response.status_code != 200:
                            result["status"] = "fallo_api" if final else "historial_incompleto"
                            break
                        if final:
                            result["status"] = "ok"
                finally:
                    with suppress(httpx.HTTPError):
                        web.post("/api/logout", json={})
        except Exception as error:
            result.update(status="fallo_tecnico", error_type=type(error).__name__)
        result["seconds"] = time.perf_counter() - started
        return sanitize(result, secrets)
