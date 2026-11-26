"""Prueba real por HTTP del mismo endpoint que utiliza el navegador."""
import os
import json
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent


def main():
    started = time.perf_counter()
    report = {"transport": "HTTP (no sustituye inspección visual en navegador)", "passed": False}
    try:
        with httpx.Client(base_url="http://127.0.0.1:8000", timeout=450, trust_env=False) as client:
            assert client.get("/").status_code == 200
            for prefix, user, profile in [("EMPLEADO", "analista", "Analista"), ("CLIENTE", "cliente", "Cliente")]:
                username = os.environ.get(f"BANCO_{prefix}_USERNAME", user)
                password = os.environ[f"BANCO_{prefix}_PASSWORD"]
                response = client.post("/api/login", json={"username":username, "password":password})
                response.raise_for_status()
                assert response.json() == {"profile":profile}
                assert client.get("/api/session").json() == {"profile":profile}
                if user == "analista":
                    client.post("/api/logout").raise_for_status()
                    assert client.get("/api/session").status_code == 401
            question = "¿Qué significa NCLAMTS?"
            report["question"] = question
            response = client.post("/api/chat", json={"question":question})
            report["http_status"] = response.status_code
            report["result"] = response.json()
            response.raise_for_status()
            result = response.json()
            assert "data_steward" in result["agents"], result
            assert "data_steward" in result["tools"], result
            assert any("document" in tool for tool in result["tools"]), result
            for fragment in ("NCLAMTS", "GCLAMTS-RECOVERYAMTS", "Sheet1", "16", ".xlsx"):
                assert fragment in result["answer"], (fragment, result)
            assert "trace" not in result
            client.post("/api/logout").raise_for_status()
            assert client.get("/api/session").status_code == 401
            report["passed"] = True
    except Exception as error:
        report["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        report["elapsed_seconds"] = round(time.perf_counter()-started, 3)
        (ROOT / "verification-web-http.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
