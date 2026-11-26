"""Publicación optativa en el dataset existente; no crea ni actualiza ejemplos."""
import hashlib
import json
import os

from .data import REFERENCE_FIELDS


class ConfigurationError(ValueError):
    """Mensaje propio, sin respuestas HTTP ni valores secretos."""


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def match_examples(examples, cases):
    """Mantiene intactos los ejemplos remotos y exige referencias equivalentes al CSV."""
    known = {c.id: c for c in cases}
    matched = {}
    for example in examples:
        merged = {}
        for block in (example.metadata or {}, example.outputs or {}, example.inputs):
            for key, value in block.items():
                if key in merged and key in cases[0].row and merged[key] != value:
                    raise ConfigurationError("Campos remotos contradictorios entre inputs, outputs y metadata.")
                merged[key] = value
        key = merged.get("id")
        if key not in known or key in matched:
            raise ConfigurationError("IDs remotos ausentes, repetidos o distintos del CSV; no se publica.")
        case = known[key]
        if set(example.inputs) - set(case.row):
            raise ConfigurationError("Hay entradas remotas ajenas al CSV; no se exportan campos desconocidos.")
        for field in ("pregunta", "perfil", "turnos_previos_json", *REFERENCE_FIELDS):
            remote, local = merged.get(field), case.row[field]
            if local == "" and remote is None:
                remote = ""  # Ausencia de criterio: CSV vacío y JSON null no cambian la referencia.
            if field == "turnos_previos_json":
                remote = json.loads(remote) if isinstance(remote, str) else remote
                local = json.loads(local)
            if remote != local:
                raise ConfigurationError(f"El dataset remoto no coincide con el CSV en {key}, campo {field}; no se modifica ninguna referencia.")
        matched[key] = example
    if set(matched) != set(known):
        raise ConfigurationError("El dataset remoto no contiene exactamente los 21 IDs del CSV.")
    return matched


def feedback(outputs, name):
    result = outputs["evaluations"][name]
    return {"key": name, "score": result["score"],
            "comment": f"{result['status']}: {result['reason']}",
            "evaluator_info": {"estado": result["status"], "regla": "evaluacion-csv-v1", "details": result.get("details", {})}}


def enrutamiento_correcto(outputs):
    return feedback(outputs, "enrutamiento_correcto")


def exactitud_numerica(outputs):
    return feedback(outputs, "exactitud_numerica")


def responde_la_pregunta(outputs):
    return feedback(outputs, "responde_la_pregunta")


def responde_correcto(outputs):
    return feedback(outputs, "responde_correcto")


def manejo_de_fechas(outputs):
    return feedback(outputs, "manejo_de_fechas")


def alucinacion(outputs):
    return feedback(outputs, "alucinacion")


# Solo desarrollo/regresión. El runner de esta medición no registra estos evaluadores.
EVALUATORS = [enrutamiento_correcto, exactitud_numerica, responde_la_pregunta,
              responde_correcto, manejo_de_fechas, alucinacion]


def publish(cases, selected, target, dataset, csv_digest, *, client=None):
    if client is None:
        if not os.getenv("LANGSMITH_API_KEY"):
            raise ConfigurationError("Configura LANGSMITH_API_KEY en el entorno de esta consola; no se lee .env.")
        from langsmith import Client
        client = Client(api_key=os.environ["LANGSMITH_API_KEY"], api_url=os.getenv("LANGSMITH_ENDPOINT", "https://api.smith.langchain.com"))
    examples = match_examples(list(client.list_examples(dataset_name=dataset)), cases)
    inputs_to_case = {}
    for case in selected:
        key = fingerprint(examples[case.id].inputs)
        if key in inputs_to_case:
            raise ConfigurationError("Entradas remotas ambiguas; no se puede distinguir el caso.")
        inputs_to_case[key] = case
    def api_target(inputs):
        case = inputs_to_case.get(fingerprint(inputs))
        if case is None:
            raise ConfigurationError("Ejemplo no verificado contra el CSV.")
        output = target(case)
        if "evaluations" in output or output.get("python_scores_enabled") is not False:
            raise ConfigurationError("Esta corrida solo publica evidencia; los puntajes se generan en la interfaz.")
        return output
    result = client.evaluate(api_target, data=[examples[c.id] for c in selected], evaluators=[], summary_evaluators=[],
                           experiment_prefix="banco-agentes-ui",
                           metadata={"csv_sha256": csv_digest, "case_ids": [c.id for c in selected],
                                     "evaluator_owner": "langsmith_ui", "python_scores_enabled": False,
                                     "fecha_referencia_csv": "2026-11-12"},
                           max_concurrency=0, num_repetitions=1, blocking=True, upload_results=True)
    completed = 0
    for row in result:
        # Los evaluadores UI son asíncronos y no forman parte de los resultados locales del SDK.
        completed += 1
    if completed != len(selected):
        raise RuntimeError("Publicación parcial: faltan casos del experimento.")
    return result
