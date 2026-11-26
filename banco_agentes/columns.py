"""Listado determinista de columnas para una tabla identificada exactamente."""

import re
from .catalog import normalize


def requested_table(question, catalog):
    text = normalize(question)
    if not re.search(r"\bcolumnas\b", text) or any(
        word in text for word in ("significa", "relacion", "join", "negocio", "regla")):
        return None
    identifiers = set(re.findall(r"\b[a-z_]\w*\.[a-z_]\w*(?:\.[a-z_]\w*)*\b", text))
    matches = [table for table in catalog["tables"] if identifiers == {table["name"]}]
    return matches[0] if len(matches) == 1 else None


def column_evidence(table):
    return {"kind": "explicit_ddl", "id": table["id"], "name": table["name"],
            "source": table["source"], "columns": table["columns"]}


def format_columns(evidence):
    lines = [f"Columnas de `{evidence['name']}` (DDL explícito):"]
    for column in evidence["columns"]:
        source = column["source"]
        lines.append(f"- `{column['name']}` — `{column['type']}`; "
                     f"declaración: `{column['declaration']}` "
                     f"[{source['file']}:{source['line']}]")
    lines.append("Definiciones de negocio: no documentadas en el DDL.")
    return "\n".join(lines)
