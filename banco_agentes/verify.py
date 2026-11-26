"""Cuatro pruebas de aceptación con el modelo Ollama REAL, sin sustitutos."""

import asyncio
import argparse
import json
import re
import sys

from .agents import local_settings
from .catalog import ROOT, normalize
from .runtime import ask

CASES = [
    ("tabla_existente", "Describe las columnas y la clave primaria de la tabla dim.dim_cliente. Cita tus fuentes."),
    ("campo_inexistente", "¿Qué significa el campo credit_score de dim.dim_cliente?"),
    ("saludo", "Hola"),
    ("saldo_no_disponible", "¿Cuál es el saldo real del cliente 1 al cierre de junio de 2026?"),
]
VARIANTS = [
    ("saludo_tardes", "Hola, buenas tardes"),
    ("saludo_con_tabla", "Hola, ¿qué columnas tiene dim.dim_cliente?"),
]


def check(case, result):
    case = {"saludo_tardes": "saludo", "saludo_con_tabla": "tabla_existente"}.get(case, case)
    answer = normalize(result["answer"])
    completed = {(entry["agent"], entry.get("tool")) for entry in result["trace"]
                 if entry["event"] == "tool_completed"}
    if case in {"tabla_existente", "campo_inexistente"}:
        assert ("orquestador", "data_steward") in completed, "El hub no invocó AgentTool."
        assert ("data_steward", "buscar_catalogo") in completed, "Data Steward no buscó evidencia."
        assert "001_dimensiones" in answer or "table:dim.dim_cliente" in answer, "Falta una fuente concreta."
    if case == "tabla_existente":
        assert "num_cte" in answer, "No identificó la PK documentada."
    elif case == "campo_inexistente":
        assert "credit_score" in answer and any(term in answer for term in
            ("no documentado", "no esta documentado", "no se encuentra", "no existe", "no aparece", "no esta disponible")), "No reconoció la ausencia de documentación."
    elif case == "saludo":
        assert not any(entry["agent"] == "data_steward" or entry.get("tool") == "data_steward"
                       for entry in result["trace"]), "El saludo invocó innecesariamente al steward."
        calls = [entry for entry in result["trace"] if entry["event"] == "model_call"]
        assert calls and all(entry.get("tools_offered") == [] for entry in calls), "El saludo debe usar el modelo sin herramientas ofrecidas."
    elif case == "saldo_no_disponible":
        assert any(term in answer for term in ("no puedo", "no se puede", "no es posible", "no tengo", "no tenemos")), "No explicó el bloqueo de cálculo."
        assert not re.search(r"\$\s*\d|\b\d[\d,]*\.\d{2}\b", answer), "Posible importe inventado."
        assert "conexion" in answer and any(term in answer for term in ("base de datos", "postgresql")), "No explicó la falta de conexión a la base de datos."


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", choices=[case for case, _ in CASES + VARIANTS])
    args = parser.parse_args()
    _, _, timeout = local_settings()
    results = []
    for case, question in (CASES + VARIANTS if args.case else CASES):
        if args.case and case != args.case:
            continue
        result = {}
        print(f"\nCASO: {case}")
        try:
            result = await ask(question, timeout=timeout)
            print(result["answer"])
            check(case, result)
            result.update(case=case, status="passed")
        except Exception as error:
            result = getattr(error, "run_result", result)
            result.update(case=case, question=question, status="failed",
                          error=f"{type(error).__name__}: {error}")
            print(result["error"])
        results.append(result)
    output = f"verification-{args.case}.json" if args.case else "verification-live.json"
    (ROOT / output).write_text(
        json.dumps({"mode": "real_ollama", "results": results}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("\nLas aserciones son automáticas; revise también significado y citas de las respuestas.")
    return int(any(result["status"] != "passed" for result in results))


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
