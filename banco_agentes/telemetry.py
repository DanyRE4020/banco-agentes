"""Métricas por turno a partir de los contadores reportados por cada proveedor."""
from datetime import datetime, timezone


def _count(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def turn_metrics(records, *, elapsed_seconds, execution_seconds=None, status="ok", fallback=None):
    fallback = fallback or {}
    calls = [entry for entry in records if entry.get("event") == "model_call"]
    providers = set()
    models = set()
    measurements = []
    for call in calls:
        provider = call.get("provider", "ollama")
        providers.add(provider)
        model = call.get("model") or ("llama3.1:8b" if provider == "ollama" else fallback.get("model"))
        if model:
            models.add(model)
        if provider == "gemini":
            raw = call.get("usage") or {}
            usage = dict(input_tokens=_count(raw.get("prompt_token_count")),
                         output_tokens=_count(raw.get("candidates_token_count")),
                         total_tokens=_count(raw.get("total_token_count")),
                         thinking_tokens=_count(raw.get("thoughts_token_count")),
                         cached_tokens=_count(raw.get("cached_content_token_count")))
        else:
            raw = call.get("ollama_metrics") or {}
            input_tokens, output_tokens = _count(raw.get("prompt_eval_count")), _count(raw.get("eval_count"))
            usage = dict(input_tokens=input_tokens, output_tokens=output_tokens,
                         total_tokens=input_tokens + output_tokens if input_tokens is not None and output_tokens is not None else None,
                         thinking_tokens=None, cached_tokens=None)
        error = {key: call[key] for key in ("error_type", "error_code", "os_error", "reader_diagnostics") if key in call}
        measurements.append({**error, "agent": call.get("agent"), "provider": provider, "model": model,
                             "status": call.get("status", "unknown"), "seconds": round(call.get("seconds", 0), 6), **usage})

    def total(field):
        values = [item[field] for item in measurements if item[field] is not None]
        return sum(values) if values else None

    reported = sum(item["total_tokens"] is not None for item in measurements)
    complete = bool(calls) and reported == len(calls) and all(item["status"] == "completed" for item in measurements)
    if status == "ok" and any(entry.get("event") == "turn_timeout_partial" for entry in records):
        status = "partial"
    return {"timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "provider": next(iter(providers)) if len(providers) == 1 else "mixed" if providers else fallback.get("provider"),
            "model": next(iter(models)) if len(models) == 1 else None if not models else "mixed",
            "status": status, "elapsed_seconds": round(elapsed_seconds, 6),
            "execution_seconds": round(execution_seconds, 6) if execution_seconds is not None else None,
            "model_seconds": round(sum(item["seconds"] for item in measurements), 6),
            "model_calls": len(calls), "calls_with_usage": reported, "usage_complete": complete,
            "input_tokens": total("input_tokens"), "output_tokens": total("output_tokens"),
            "thinking_tokens": total("thinking_tokens"), "cached_tokens": total("cached_tokens"),
            "total_tokens": total("total_tokens"), "calls": measurements}
