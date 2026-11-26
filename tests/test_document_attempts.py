"""Telemetría del lector: SDK real, transporte doble y mensajes hostiles descartados."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from google import genai

from banco_agentes.documents import DEFAULT_DOCUMENT_FALLBACK_MODEL, DEFAULT_DOCUMENT_MODEL, DocumentStore, DocumentError, ROOT
from banco_agentes.attachment_hub import safe_trace
from banco_agentes.telemetry import turn_metrics


class ReaderAttemptTests(unittest.IsolatedAsyncioTestCase):
    def client_factory(self, handler):
        original = genai.Client
        def factory(**kwargs):
            self.assertEqual(kwargs["http_options"].retry_options.attempts, 1)
            self.assertEqual(kwargs["http_options"].timeout, 90000)
            kwargs["http_options"].async_client_args = {"transport": httpx.MockTransport(handler)}
            return original(**kwargs)
        return factory

    async def run_reader(self, status, body, *, fallback=DEFAULT_DOCUMENT_FALLBACK_MODEL):
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(status, json=body)

        with tempfile.TemporaryDirectory() as folder, patch.dict("os.environ", {
            "GOOGLE_API_KEY": "test-only", "BANCO_DOCUMENT_MODEL": DEFAULT_DOCUMENT_MODEL,
            "BANCO_DOCUMENT_FALLBACK_MODEL": fallback,
        }), patch.object(genai, "Client", side_effect=self.client_factory(handler)):
            store = DocumentStore(Path(folder))
            file_id = store.save_many("owner", "chat", [(ROOT/"fixtures/documents/identificacion_vigente.jpg").read_bytes()])[0]["file_id"]
            with self.assertRaises(DocumentError):
                await store.leer_documento(file_id, "owner", "chat")
            # El error cacheado no dispara otro intento ni otra llamada HTTP.
            with self.assertRaises(DocumentError):
                await store.leer_documento(file_id, "owner", "chat")
            record = store.records[file_id]
        self.assertEqual(len(requests), 1)
        return record

    async def test_503_without_fallback_preserves_timings_and_safe_provider_category(self):
        record = await self.run_reader(503, {"error": {"code": 503, "status": "UNAVAILABLE",
            "message": "This model has high demand. SECRET_FILE_TEXT 000000001234 test-only"}}, fallback="")
        metrics = record["metrics"]
        self.assertEqual(metrics["error_code"], 503)
        self.assertEqual(metrics["error_type"], "ServerError")
        diagnostics = metrics["reader_diagnostics"]
        self.assertEqual(len(diagnostics["attempts"]), 1)
        attempt = diagnostics["attempts"][0]
        self.assertEqual(attempt["http_status"], 503)
        self.assertEqual(attempt["provider_status"], "UNAVAILABLE")
        self.assertEqual(attempt["provider_reason"], "high_demand")
        self.assertEqual(diagnostics["failed_stage"], "solicitud")
        self.assertEqual(diagnostics["attempt_limit"], 1)
        self.assertGreaterEqual(diagnostics["timings"]["request_seconds"], 0)
        self.assertGreaterEqual(diagnostics["total_seconds"], attempt["seconds"])
        exported = turn_metrics(safe_trace([metrics]), elapsed_seconds=metrics["seconds"])
        self.assertEqual(exported["calls"][0]["reader_diagnostics"], diagnostics)
        for forbidden in ("SECRET_FILE_TEXT", "000000001234", "test-only", "message", "api_key"):
            self.assertNotIn(forbidden, json.dumps(exported))

    async def test_http_success_invalid_json_is_validation_failure_without_fake_http_503(self):
        record = await self.run_reader(200, {"candidates": [{"content": {"role": "model", "parts": [{"text": "{}"}]}}]})
        metrics = record["metrics"]
        self.assertNotIn("error_code", metrics)
        diagnostic = metrics["reader_diagnostics"]
        self.assertEqual(diagnostic["attempts"][0]["status"], "completed")
        self.assertEqual(diagnostic["attempts"][0]["http_status"], 200)
        self.assertEqual(diagnostic["failed_stage"], "validacion_esquema")

    async def test_failed_attachment_retries_next_turn_and_keeps_successful_attachment_cached(self):
        unavailable = {"error": {"code": 503, "status": "UNAVAILABLE", "message": "High demand"}}
        result = {"candidates": [{"content": {"role": "model", "parts": [{"text":
            json.dumps({"tipo": "identificacion", "legible": True})}]}}]}
        responses = iter([(503, unavailable), (503, unavailable), (200, result), (503, unavailable), (200, result)])
        requests = []

        def handler(request):
            requests.append((request.url.path, json.loads(request.content)["contents"]))
            status, body = next(responses)
            return httpx.Response(status, json=body)

        fixtures = ROOT / "fixtures/documents"
        jpg = (fixtures / "identificacion_vigente.jpg").read_bytes()
        scan = (fixtures / "lectura_completa/escaneado.pdf").read_bytes()
        with tempfile.TemporaryDirectory() as folder, patch.dict("os.environ", {
            "GOOGLE_API_KEY": "test-only", "BANCO_DOCUMENT_MODEL": DEFAULT_DOCUMENT_MODEL,
            "BANCO_DOCUMENT_FALLBACK_MODEL": DEFAULT_DOCUMENT_FALLBACK_MODEL,
        }), patch.object(genai, "Client", side_effect=self.client_factory(handler)):
            store = DocumentStore(Path(folder))
            failed, succeeded = [r["file_id"] for r in store.save_many("owner", "chat", [jpg, scan])]
            with self.assertRaisesRegex(DocumentError, "HTTP 503"):
                await store.leer_documento(failed, "owner", "chat")
            first_metrics = store.records[failed]["metrics"]["reader_diagnostics"]
            self.assertEqual([a["http_status"] for a in first_metrics["attempts"]], [503, 503])
            self.assertTrue(store.records[failed]["retryable"])
            previous_success = await store.leer_documento(succeeded, "owner", "chat")
            with self.assertRaises(DocumentError):
                await store.leer_documento(failed, "someone-else", "chat")
            with self.assertRaises(DocumentError):
                await store.leer_documento(failed, "owner", "other-chat")
            self.assertEqual(len(requests), 3)

            recovered = await store.leer_documento(failed, "owner", "chat")
            self.assertEqual(recovered["tipo"], "identificacion")
            self.assertFalse(store.records[failed]["retryable"])
            metrics = store.records[failed]["metrics"]
            self.assertEqual(metrics["model"], DEFAULT_DOCUMENT_FALLBACK_MODEL)
            self.assertEqual([a["http_status"] for a in metrics["reader_diagnostics"]["attempts"]], [503, 200])
            self.assertEqual(await store.leer_documento(succeeded, "owner", "chat"), previous_success)
            self.assertEqual(await store.leer_documento(failed, "owner", "chat"), recovered)
        self.assertEqual([path.split("/")[-1] for path, _ in requests], [
            model + ":generateContent" for model in (
                DEFAULT_DOCUMENT_MODEL, DEFAULT_DOCUMENT_FALLBACK_MODEL, DEFAULT_DOCUMENT_MODEL,
                DEFAULT_DOCUMENT_MODEL, DEFAULT_DOCUMENT_FALLBACK_MODEL,
            )])
        self.assertEqual(requests[0][1], requests[1][1])
        self.assertEqual(requests[0][1], requests[3][1])
        self.assertEqual(requests[0][1], requests[4][1])
