"""Fechas y correspondencia deterministas: sin nombres, emisores o archivos privilegiados."""
import calendar
from collections import Counter
from datetime import date
import re
import unicodedata


def tokens(value):
    text = "".join(c for c in unicodedata.normalize("NFKD", str(value).casefold()) if not unicodedata.combining(c))
    return re.findall(r"[^\W_]+", text)


def name_relation(left, right):
    a, b = Counter(tokens(left)), Counter(tokens(right))
    if not a or not b:
        return "no_determinable"
    if a == b:
        return "coincidente"
    if a <= b or b <= a:
        return "parcial"
    return "distinto"


def date_span(value):
    """Intervalo de días posibles; nunca convierte una precisión parcial en un día inventado."""
    if re.fullmatch(r"\d{4}", value):
        year = int(value)
        return date(year, 1, 1), date(year, 12, 31), "anio"
    if re.fullmatch(r"\d{4}-\d{2}", value):
        year, month = map(int, value.split("-"))
        return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1]), "mes"
    day = date.fromisoformat(value)
    return day, day, "dia"


def months_before(day, months):
    total = day.year*12 + day.month-1-int(months)
    year, month = divmod(total, 12)
    month += 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def field_problem(field):
    if not field or field.get("lectura") == "ausente":
        return "ausente"
    if field.get("estado") == "confirmado" and field.get("valor") is not None:
        return None
    if field.get("lectura") == "ilegible":
        return "ilegible"
    if field.get("lectura") == "no_interpretable":
        return "no_interpretable"
    if field.get("valor") is None:
        return "ausente"
    return field.get("motivo") or "baja_confianza"


def is_ine(document):
    field = document.get("campos", {}).get("clase_identificacion")
    return document.get("tipo") == "identificacion" and field_problem(field) is None and tokens(field["valor"]) in (
        ["ine"], ["credencial", "para", "votar", "ine"])


def local_ine(pages):
    """Lee rótulos explícitos y sus páginas sin deducir fechas ni el tipo por el nombre del archivo."""
    from .document_financial import date_value, plain
    header = next((p for p in pages if p["estado"] != "ilegible" and re.search(
        r"(?m)^\s*(?:ine(?:\s|$)|instituto nacional electoral\b)", plain(p["texto"]))), None)
    if not header:
        return None
    fields = {"clase_identificacion": {"valor": "INE", "pagina": header["pagina"], "confianza": .99}}
    labels = {"nombre": r"nombre(?: completo)?", "fecha_nacimiento": r"fecha de nacimiento|nacimiento",
              "fecha_vencimiento": r"vigencia|fecha de vencimiento|vencimiento",
              "domicilio": r"domicilio|direccion"}
    for page in pages:
        if page["estado"] == "ilegible":
            continue
        for line in page["texto"].splitlines():
            label, sep, value = line.partition(":")
            value = value.strip()
            if not sep or not value or len(value) > 500:
                continue
            for name, pattern in labels.items():
                if not re.fullmatch(pattern, plain(label.strip())):
                    continue
                try:
                    parsed = date_value(value) if name in {"fecha_nacimiento", "fecha_vencimiento"} else value
                except ValueError:
                    parsed = value  # Conserva el rótulo y marca el dato no interpretable en el validador.
                item = {"valor": parsed, "texto_original": value, "etiqueta": label.strip(),
                        "pagina": page["pagina"], "confianza": .99}
                if name not in fields:
                    fields[name] = item
                elif fields[name]["valor"] != parsed:
                    fields[name]["confianza"] = .4
    return {"tipo": "identificacion", "legible": True, "campos": fields}
