"""Lectura estricta del CSV original y separación de entradas y referencias."""
import csv
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

CRITERIA = ("enrutamiento_correcto", "exactitud_numerica", "responde_la_pregunta",
            "responde_correcto", "manejo_de_fechas", "alucinacion")
REFERENCE_FIELDS = ("enrutamiento_esperado", *CRITERIA[1:])
SMOKE_IDS = ("BA-006", "BA-002", "BA-012")


@dataclass(frozen=True)
class Case:
    row: dict

    @property
    def id(self):
        return self.row["id"]

    @property
    def previous(self):
        return json.loads(self.row["turnos_previos_json"])

    @property
    def digest(self):
        return hashlib.sha256(json.dumps(self.row, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def load_cases(path):
    with Path(path).open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        needed = {"id", "categoria", "perfil", "pregunta", "turnos_previos_json", *REFERENCE_FIELDS}
        if not needed <= set(reader.fieldnames or []):
            raise ValueError("El CSV no tiene las columnas requeridas.")
        cases = [Case(dict(row)) for row in reader]
    if len({case.id for case in cases}) != len(cases):
        raise ValueError("IDs repetidos en el CSV.")
    for case in cases:
        if case.row["perfil"] not in {"Cliente", "Analista"} or not case.row["pregunta"].strip():
            raise ValueError(f"Perfil o pregunta inválidos en {case.id}.")
        if not isinstance(case.previous, list) or any(not isinstance(q, str) or not q.strip() for q in case.previous):
            raise ValueError(f"turnos_previos_json debe ser una lista de mensajes de usuario en {case.id}.")
    return cases


def select_cases(cases, ids):
    known = {c.id: c for c in cases}
    if len(set(ids)) != len(ids) or any(key not in known for key in ids):
        raise ValueError("La selección contiene IDs desconocidos o repetidos.")
    return [known[key] for key in ids]
