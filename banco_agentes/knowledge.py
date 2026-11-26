"""RAG léxico auditable: diccionario de tarjetas y documentación pública separados del DDL."""

import hashlib
import json
import re
import zipfile
import xml.etree.ElementTree as ET
from pathlib import PurePosixPath

from .catalog import ROOT, normalize

DICTIONARY = ROOT / "catalogo/cards_dictionary.json"
PUBLIC = ROOT / "catalogo/public_sources.json"
NS = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def import_dictionary():
    path = ROOT / "referencias/Base Cards para TEC - Diccionario de Variables (1).xlsx"
    entries = []
    with zipfile.ZipFile(path) as archive:
        shared = []
        if "xl/sharedStrings.xml" in archive.namelist():
            shared = ["".join(e.itertext()) for e in ET.fromstring(archive.read("xl/sharedStrings.xml")).findall("s:si", NS)]
        relationships = {e.get("Id"): e.get("Target") for e in ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))}
        for sheet in ET.fromstring(archive.read("xl/workbook.xml")).findall("s:sheets/s:sheet", NS):
            target = relationships[sheet.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")]
            target = target.lstrip('/') if target.startswith('/') else str(PurePosixPath('xl') / target)
            for row in ET.fromstring(archive.read(target)).findall("s:sheetData/s:row", NS):
                values = {}
                for cell in row:
                    column = re.sub(r"\d", "", cell.get("r"))
                    value = cell.find("s:v", NS)
                    text = value.text if value is not None else "".join(cell.itertext())
                    if cell.get("t") == "s":
                        text = shared[int(text)]
                    values[column] = text or ""
                if int(row.get("r")) == 1 or not values.get("B"):
                    continue
                description = values.get("C", "")
                rules = [s.strip() for s in re.split(r"(?<=[.])\s+|\n", description)
                         if re.search(r"\b(debe|deberia|si|puede|igual)\b", normalize(s))]
                entries.append(dict(id=f"cards:{values['B']}", kind="cards_dictionary", name=values["B"],
                    description=description, minimum=values.get("D") or "No documentado",
                    maximum=values.get("E") or "No documentado", rules=rules,
                    source=dict(file=path.relative_to(ROOT).as_posix(), sheet=sheet.get("name"), row=int(row.get("r")))))
    assert len({e["name"] for e in entries}) == len(entries), "Variables repetidas: revisar origen."
    result = dict(kind="cards_dictionary", source_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                  limitation="Solo definiciones de variables de tarjetas; no hay registros para calcular métricas.", entries=entries)
    DICTIONARY.write_text(json.dumps(result, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    return result


STOP = set("que como se la el los las de del al para por una un es en y despues cual necesito solicitar significa calcula".split())


def tokens(text):
    words = set(re.findall(r"[a-z0-9_]+", normalize(text))) - STOP
    # Expansión léxica explícita y general, no una ruta por pregunta.
    return {"recuper" if w.startswith(("recuper", "recover")) else w for w in words}


def search_documents(query):
    dictionary = json.loads(DICTIONARY.read_text(encoding="utf-8"))
    source = ROOT / dictionary["entries"][0]["source"]["file"]
    if hashlib.sha256(source.read_bytes()).hexdigest() != dictionary["source_sha256"]:
        raise ValueError("Diccionario desactualizado: regenere knowledge.")
    public = json.loads(PUBLIC.read_text(encoding="utf-8"))["entries"]
    q = tokens(query)
    ranked = []
    for entry in dictionary["entries"] + public:
        text = entry.get("description", "")
        name = entry.get("name", "")
        score = len(q & tokens(name + " " + text))
        exact = name and re.search(r"\b"+re.escape(name.lower())+r"\b", normalize(query))
        if exact:
            score += 100
        if "calcula" in normalize(query) and "=" in text and score:
            score += 3
        if "nomina" in q and entry.get("product") == "Crédito Personal":
            score = 0
        if "nomina" in q and entry.get("product") == "Crédito Nómina":
            score += 8
        if score:
            ranked.append((score, entry))
    ranked.sort(key=lambda item: item[0], reverse=True)
    if ranked and ranked[0][1]["kind"] == "cards_dictionary":
        hits = [ranked[0][1]]
    else:
        hits = [e for _, e in ranked[:2]]
    return dict(status="found" if hits else "not_found", query=query, hits=hits,
                method="exact_name_and_keywords", limitation=dictionary["limitation"])


def format_documents(result):
    if not result["hits"]:
        return "No documentado en las fuentes locales disponibles."
    lines = []
    for entry in result["hits"]:
        source = entry["source"]
        if entry["kind"] == "cards_dictionary":
            lines += [f"Diccionario de tarjetas — {entry['name']}: {entry['description']}",
                      f"Rango documentado: {entry['minimum']} a {entry['maximum']}.",
                      f"Fuente: {source['file']}, hoja {source['sheet']}, fila {source['row']}.", result["limitation"]]
        else:
            lines += [entry["description"], f"Fuente: {source['title']} — {source['url']} — "
                      f"sección/página: {source['section']}; consulta: {source['consulted_at']}."]
    if any(e.get("product") == "Crédito Nómina" for e in result["hits"]):
        lines.append("Información pública de Crédito Nómina, distinta de Crédito Personal. No se consultaron datos del cliente; no evalúa elegibilidad ni aprobación personal.")
    return "\n".join(lines)


if __name__ == "__main__":
    print(f"Importadas {len(import_dictionary()['entries'])} variables de tarjetas.")
