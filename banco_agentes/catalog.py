"""Extracción estática del subconjunto DDL presente en las referencias."""

import argparse
import hashlib
import json
import re
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = ROOT / "catalogo" / "catalogo.json"
COMMENTS = re.compile(r"/\*.*?\*/|--[^\n]*", re.S)
CREATE = re.compile(
    r"\bCREATE\s+(?:(TEMP|TEMPORARY)\s+)?TABLE\s+"
    r"([a-z_][\w]*(?:\.[a-z_][\w]*)?)\s*\(", re.I
)


def normalize(text: str) -> str:
    return "".join(char for char in unicodedata.normalize("NFKD", text.lower())
                   if not unicodedata.combining(char))


def requested_definition_fields(question: str) -> tuple[str, ...]:
    text = normalize(question)
    identifier = r"\b[a-z][a-z0-9]*(?:[_.:][a-z0-9]+)+\b"
    asks_definition = re.search(r"\b(?:define|definicion|significa|significado|representa|que es|"
                                r"tipo de dato|campo|columna)\b", text)
    only_identifier = re.fullmatch(identifier, text.strip("¿¡?!. `'\"[]\t\n"))
    if not (asks_definition or only_identifier):
        return ()
    return tuple(dict.fromkeys(re.findall(identifier, text)))


def source(text: str, start: int, end: int, path: str) -> dict:
    return {"file": path, "line": text.count("\n", 0, start) + 1,
            "end_line": text.count("\n", 0, end) + 1,
            "excerpt": text[start:end].strip()}


def split_definitions(body: str) -> list[tuple[int, str]]:
    depth = 0
    quoted = False
    start = 0
    parts = []
    for position, char in enumerate(body):
        if char == "'":
            quoted = not quoted
        elif not quoted:
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            elif char == "," and depth == 0:
                parts.append((start, body[start:position]))
                start = position + 1
    parts.append((start, body[start:]))
    return parts


def build_catalog(reference_dir: Path = ROOT / "referencias") -> dict:
    files = sorted(reference_dir.glob("*.sql"))
    if not files:
        raise ValueError("Faltan referencias SQL: no se puede generar el catálogo.")
    catalog = {"version": 1, "sources": [], "tables": [], "documentation": [],
               "interpretations": [], "limitations": [
                   "Catálogo estático: no demuestra existencia de tablas ni filas en PostgreSQL.",
                   "Los scripts disponibles no declaran FOREIGN KEY.",
                   "No hay glosario de negocio completo; un nombre no demuestra su significado.",
                   "Los comentarios son documentación, no restricciones del motor.",
               ]}
    texts = {}
    for path in files:
        text = path.read_text(encoding="utf-8-sig")
        relative = f"referencias/{path.name}"
        texts[path.name[:3]] = (text, relative)
        catalog["sources"].append({"file": relative,
                                   "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
        clean = COMMENTS.sub(lambda match: "".join("\n" if char == "\n" else " "
                                                   for char in match.group()), text)
        for match in COMMENTS.finditer(text):
            catalog["documentation"].append({
                "id": f"doc:{path.name}:{text.count(chr(10), 0, match.start()) + 1}",
                "kind": "explicit_comment", "source": source(text, match.start(), match.end(), relative)})
        for match in CREATE.finditer(clean):
            depth = 1
            position = match.end()
            quoted = False
            while position < len(clean) and depth:
                char = clean[position]
                if char == "'":
                    quoted = not quoted
                elif not quoted:
                    depth += (char == "(") - (char == ")")
                position += 1
            if depth:
                raise ValueError(f"DDL incompleto en {relative}")
            name = match[2].lower()
            body = clean[match.end():position - 1]
            table = {"id": f"table:{name}", "name": name,
                     "temporary": bool(match[1]), "kind": "explicit_ddl",
                     "source": source(text, match.start(), position, relative),
                     "columns": [], "constraints": [], "foreign_keys": [],
                     "business_definition": "No documentada como definición formal de tabla."}
            for offset, definition in split_definitions(body):
                definition = definition.strip()
                if not definition:
                    continue
                if re.match(r"^(CHECK|PRIMARY|UNIQUE|FOREIGN|CONSTRAINT)\b", definition, re.I):
                    table["constraints"].append(definition)
                    if re.search(r"\bFOREIGN\s+KEY\b", definition, re.I):
                        table["foreign_keys"].append(definition)
                    continue
                column = re.fullmatch(
                    r"([a-z_]\w*)\s+((?:INTEGER|INT4|DATE|BOOLEAN|VARCHAR\s*\(\d+\)|"
                    r"NUMERIC\s*\(\d+\s*,\s*\d+\)))(.*)", definition, re.I | re.S)
                if not column:
                    raise ValueError(f"DDL no soportado en {relative}: {definition}")
                column_start = match.end() + offset
                column_start += len(body[offset:]) - len(body[offset:].lstrip())
                tail = column[3].strip()
                table["columns"].append({
                    "id": f"column:{name}.{column[1].lower()}", "name": column[1].lower(),
                    "type": column[2].upper(), "declaration": definition,
                    "nullable": not bool(re.search(r"NOT\s+NULL|PRIMARY\s+KEY", tail, re.I)),
                    "primary_key": bool(re.search(r"PRIMARY\s+KEY", tail, re.I)),
                    "unique": bool(re.search(r"\bUNIQUE\b|PRIMARY\s+KEY", tail, re.I)),
                    "business_definition": "No documentada en el DDL; consultar comentarios y evidencias.",
                    "source": source(text, column_start, column_start + len(definition), relative)})
            catalog["tables"].append(table)
    tables = {table["name"]: table for table in catalog["tables"]}
    for fact in ("cfg.fact_sdos_captacoloca_m", "cfg.fact_txn_captacoloca_d"):
        for column, dimension in (("num_cte", "dim.dim_cliente"),
                                  ("cve_producto", "dim.dim_producto")):
            if fact in tables and dimension in tables:
                evidence = [entry["source"] for table_name in (fact, dimension)
                            for entry in tables[table_name]["columns"] if entry["name"] == column]
                if len(evidence) == 2:
                    catalog["interpretations"].append({
                        "id": f"inferred:{fact}.{column}", "kind": "inferred_not_enforced",
                        "relationship": f"{fact}.{column} -> {dimension}.{column}",
                        "reason": "Candidata por nombres y tipos compatibles; no es una FK declarada ni validada.",
                        "sources": evidence})
    for prefix, anchor, description in (
        ("002", "    saldo_inicial,\n    saldo_inicial,", "En enero saldo_apertura y saldo_mes reciben saldo_inicial."),
        ("003", "            c.saldo_anterior,\n            c.saldo_objetivo,", "Desde febrero saldo_apertura recibe saldo_anterior y saldo_mes recibe saldo_objetivo."),
        ("002", "        p.dominio\n", "En enero dominio procede de parámetros CAPTACION/COLOCACION."),
        ("003", "            c.cve_dominio\n", "Desde febrero dominio recibe cve_dominio: DEPOSITS/LENDING/TDC; difiere de enero."),
    ):
        if prefix in texts:
            text, relative = texts[prefix]
            if anchor in text:
                start = text.index(anchor)
                catalog["documentation"].append({"id": f"mapping:{prefix}:{start}",
                    "kind": "explicit_sql_mapping", "description": description,
                    "source": source(text, start, start + len(anchor), relative)})
    return catalog


def load_catalog(path: Path = CATALOG_PATH) -> dict:
    catalog = json.loads(path.read_text(encoding="utf-8"))
    for entry in catalog["sources"]:
        original = ROOT / entry["file"]
        if not original.is_file() or hashlib.sha256(original.read_bytes()).hexdigest() != entry["sha256"]:
            raise ValueError("Catálogo desactualizado: ejecute python -m banco_agentes.catalog")
    return catalog


def search_catalog(catalog: dict, query: str) -> dict:
    normalized = normalize(query)
    identifiers = re.findall(r"[a-z_]\w*(?:\.[a-z_]\w*)*", normalized)
    tables = catalog["tables"]
    known = {table["name"] for table in tables} | {table["name"].split(".")[-1] for table in tables}
    known |= {column["name"] for table in tables for column in table["columns"]}
    known |= {f"{table['name']}.{column['name']}" for table in tables for column in table["columns"]}
    known |= {f"{table['name'].split('.')[-1]}.{column['name']}" for table in tables for column in table["columns"]}
    requested = [word for word in identifiers if "_" in word or "." in word]
    requested += re.findall(r"\b(?:campo|columna|tabla)\s+[`\"']?([a-z_]\w*(?:\.[a-z_]\w*)*)", normalized)
    requested = [word for word in requested if word not in {"de", "la", "el", "que", "inexistente"}]
    unknown = [word for word in requested if word not in known]
    selected = [table for table in tables if table["name"] in identifiers
                or table["name"].split(".")[-1] in identifiers
                or any(column["name"] in identifiers or f"{table['name']}.{column['name']}" in identifiers
                       or f"{table['name'].split('.')[-1]}.{column['name']}" in identifiers
                       for column in table["columns"])]
    tokens = {word for word in identifiers if len(word) > 3}
    def relevance(entry):
        searchable = normalize(entry.get("description", "") + " " + entry["source"]["excerpt"])
        return sum(token in searchable for token in tokens)

    documentation = sorted(catalog["documentation"], key=relevance, reverse=True)
    documentation = [entry for entry in documentation if any(
        token in normalize(entry.get("description", "") + " " + entry["source"]["excerpt"])
        for token in tokens)][:6]
    status = "not_found" if unknown else "found" if selected or documentation else "not_found"
    return {"status": status, "query": query, "unknown_identifiers": unknown,
            "message": "No documentado en las referencias disponibles." if status == "not_found" else "Evidencia estática, no datos consultados.",
            "tables": selected, "documentation": documentation,
            "interpretations": [entry for entry in catalog["interpretations"] if any(
                table["name"] in entry["relationship"] for table in selected)],
            "available_tables": [table["name"] for table in tables if not table["temporary"]],
            "searched_sources": catalog["sources"], "limitations": catalog["limitations"]}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    catalog = build_catalog()
    if args.check:
        if load_catalog() != catalog:
            raise ValueError("El catálogo no coincide con la extracción actual.")
        print("Catálogo y hashes verificados.")
    else:
        CATALOG_PATH.parent.mkdir(exist_ok=True)
        CATALOG_PATH.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"Catálogo: {len(catalog['tables'])} tablas, incluidas las temporales.")


if __name__ == "__main__":
    main()
