"""Ejecuta seguimientos de desarrollo con Ollama y una sola sesión ADK."""
import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("REQUEST_TIMEOUT_SECONDS", "420")

from banco_agentes.conversation import APP_NAME, Conversation
from banco_agentes.governance import load_session


async def main(resume=False):
    sql = json.loads((ROOT / "verification-general-sql.json").read_text(encoding="utf-8"))
    expected = {case["case"]: case.get("values") for case in sql["cases"]}
    conversation = Conversation(load_session("Analista"))
    turns = [
        ("total", "¿Cuánto suman los saldos de CHEQUES al cierre de marzo de 2026?"),
        ("total_pf", "¿Y solo personas físicas?"),
        ("variacion", "¿Cómo cambió respecto al mes anterior?"),
        ("grafica", "Grafícalo como línea."),
    ]
    if resume:
        prior = [json.loads(line) for line in (ROOT / "verification-general-followups.jsonl").read_text(
            encoding="utf-8").splitlines()]
        passed = [next(result for result in reversed(prior) if result["case"] == case and result["passed"])
                  for case in ("total", "total_pf")]
        session_ids = {event["session_id"] for item in passed for event in item["trace"]
                       if event["event"] == "conversation_session"}
        assert len(session_ids) == 1, "Los resultados iniciales deben proceder de un mismo chat."
        verified = [next(event for event in item["trace"] if event["event"] == "validated_plan"
                         and event["status"] == "ok") for item in passed]
        for result, name in zip(verified, ("total", "total_pf")):
            assert [[row["valor"], row["filas"]] for row in result["rows"]] == expected[name]
        memory = {"results": verified, "last_ids": [verified[-1]["id"]], "next_result": 3,
                  "turns": [{"pregunta": item["question"], "respuesta": item["answer"][:850]}
                            for item in passed]}
        await conversation.service.create_session(app_name=APP_NAME, user_id=conversation.owner,
                                                   session_id=conversation.id, state={"verified_memory": memory})
        turns = turns[2:]
    for index, (name, question) in enumerate(turns):
        start = perf_counter()
        result = {}
        print(f"Turno {index+1}: {question}", flush=True)
        try:
            result = await conversation.ask(question)
            trace = result["trace"]
            session = next(event for event in trace if event["event"] == "conversation_session")
            assert session["session_id"] == conversation.id
            assert session["reused"] == (resume or index > 0)
            plans = [event for event in trace if event["event"] == "validated_plan" and event["status"] == "ok"]
            if name != "grafica":
                assert len(plans) == 1, result["answer"]
                rows = plans[0]["rows"]
                observed = ([[r["mes"], r["valor"]] for r in rows] if name == "variacion"
                            else [[r["valor"], r["filas"]] for r in rows])
                assert expected[name] == observed, (expected[name], observed)
            else:
                assert not any(event["event"] == "validated_plan" and event.get("sql_executed")
                               for event in trace)
                assert len(result["charts"]) == 1 and result["charts"][0]["type"] == "linea", result["answer"]
                assert [p["y"] for p in result["charts"][0]["points"]] == [r[1] for r in expected["variacion"]]
            result["passed"] = True
        except Exception as error:
            if not result:
                result = getattr(error, "run_result", {})
            result.update(passed=False, error=f"{type(error).__name__}: {error}")
        result.update(case=name, question=question, resumed_fixture=resume, elapsed_seconds=perf_counter()-start)
        with (ROOT / "verification-general-followups.jsonl").open("a", encoding="utf-8") as file:
            file.write(json.dumps(result, ensure_ascii=False) + "\n")
        print(f"{name}: passed={result['passed']} en {result['elapsed_seconds']:.3f}s", flush=True)
        if not result["passed"]:
            print(result["error"], flush=True)
            return 1
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--retomar", action="store_true", help="Prueba variación y gráfica con el contexto verificado previo")
    raise SystemExit(asyncio.run(main(parser.parse_args().retomar)))
