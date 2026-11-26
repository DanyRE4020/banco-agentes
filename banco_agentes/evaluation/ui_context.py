"""Evidencia para los jueces de la interfaz; no calcula ni publica calificaciones."""
import json

from .data import REFERENCE_FIELDS
from .transport import sanitize


def prepare_ui_context(case, output):
    if "evaluations" in output:
        raise ValueError("Esta corrida no admite puntajes de los evaluadores Python.")
    response = ((output.get("final") or {}).get("response") or {})
    mode = (response.get("presentation") or {}).get("format")
    mode = mode if mode in {"frase", "prosa", "tabla", "grafica"} else None
    charts = response.get("charts", []) if mode in {None, "grafica"} else []
    tables = response.get("tables", []) if mode == "tabla" or (mode is None and not response.get("charts")) else []
    visible = sanitize({"texto": response.get("answer", ""), "formato": mode, "tablas": tables,
                        "graficas": charts, "fuentes": response.get("sources", [])})
    context = sanitize({
        "version": "langsmith-ui-v1", "id_caso": case.id, "categoria": case.row["categoria"],
        "pregunta": case.row["pregunta"], "perfil": case.row["perfil"],
        "turnos_previos": case.previous, "respuesta_visible": visible,
        "estado_ejecucion": output["status"], "correlacion": output["correlation"],
        "traza_ultimo_turno": output.get("trace"), "referencia_sql": output.get("sql_reference"),
        "referencias_dataset": {key: case.row[key] for key in REFERENCE_FIELDS},
        "fecha_referencia_dataset": "2026-11-12", "zona_horaria_referencia": "America/Mexico_City",
        "naturaleza_datos": "Datos sintéticos; los importes no tienen moneda definida.",
        "tolerancias_de_presentacion": {"importes": "0.005", "porcentajes": "0.00005", "conteos": "0"},
        "calificacion_python_habilitada": False,
    })
    # Una sola variable Mustache simplifica el mapeo y excluye las respuestas de los turnos previos.
    return {**output, "grading_owner": "langsmith_ui", "python_scores_enabled": False,
            "respuesta_visible": visible, "contexto_evaluacion": json.dumps(context, ensure_ascii=False)}
