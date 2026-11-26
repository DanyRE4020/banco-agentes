"""Contenido privado por página y fragmentos verificables. Nunca se exporta texto completo."""
import json
import re
import unicodedata
from typing import Literal

import fitz
from pydantic import BaseModel, ConfigDict, Field

PAGE_CHARS = 20000
TOTAL_CHARS = 120000


class PageText(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pagina: int = Field(ge=1, le=20)
    texto: str
    estado: Literal["leida", "parcial", "ilegible"]


class Quote(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pagina: int = Field(ge=1, le=20)
    texto: str = Field(min_length=1, max_length=1200)


class Fragment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    estado: Literal["encontrado", "ausente", "ilegible"]
    citas: list[Quote] = Field(default_factory=list, max_length=8)


def plain(text):
    return "".join(c for c in unicodedata.normalize("NFKD", text.casefold()) if not unicodedata.combining(c))


def content_question(question):
    from .document_privacy import requested_fields
    from .document_credit import receipt_reference, requested_product
    text = plain(question)
    if re.search(r"\b(?:antiguedad|sirve|acepta|admite|valido|valida|transcurrid[oa]s?)\b"
                 r"|\bcuant[oa]s?\s+(?:tiempo|dias|semanas|meses|anos)\b", text):
        return False
    if requested_product(question) and re.search(r"antiguedad|alcanza|acredit|suficiente|evalu", text):
        return False
    if re.search(r"cumpl|requisit|solicitar|politica|necesit|elegib|puedo (?:pedir|obtener)", text):
        return False
    asks = re.search(r"\b(que|cual|cuando|donde|quien|lee|leer|dime|muestra|extrae|transcribe)\b", text)
    attachment = re.search(r"\b(?:adjunto|archivo|documento|imagen|pdf|pagina|identificacion|recibo|comprobante|contrato)\b", text)
    read_date = re.search(r"\b(?:que|cual)\s+fecha\s+leiste\b", text)
    fields = requested_fields(question)
    if receipt_reference(question) and not (fields or read_date or
            re.search(r"\b(?:leiste|aparece|figura|dice|impreso|impresa|transcribe)\b", text)):
        return False
    return bool(asks and (attachment or read_date or fields))


def ine_validity_question(question):
    text = plain(question)
    return bool(re.search(r"\bine\b", text) and re.search(r"vigencia|vencimiento|vence|caducidad|expira", text)
                and content_question(question))


def prepare_pages(data, mime):
    """PyMuPDF recorre todas las páginas; cualquier imagen requiere además lectura visual."""
    pages = []
    if mime != "application/pdf":
        return [{"pagina": 1, "texto": "", "estado": "no_revisada", "visual": True, "motivo": "requiere_vision"}]
    with fitz.open(stream=data, filetype="pdf") as pdf:
        for number, page in enumerate(pdf, 1):
            try:
                text = page.get_text(sort=True)
                visual = bool(page.get_image_info()) or not text.strip()
                pages.append({"pagina": number, "texto": text, "estado": "parcial" if visual and text.strip() else
                    "no_revisada" if visual else "leida", "visual": visual, "motivo": "requiere_vision" if visual else None})
            except Exception:
                pages.append({"pagina": number, "texto": "", "estado": "no_revisada", "visual": True, "motivo": "error_local"})
    return bounded_pages(pages)


def bounded_pages(pages):
    remaining = TOTAL_CHARS
    for page in pages:
        limit = min(PAGE_CHARS, remaining)
        text = page["texto"]
        page["texto"] = text[:limit]
        remaining -= len(page["texto"])
        if len(text) > limit:
            page.update(estado="parcial" if limit else "no_revisada", motivo="limite_texto")
    return pages


def merge_pages(local, extracted):
    from .documents import DocumentError
    provided = {}
    for raw in extracted:
        page = PageText.model_validate(raw)
        if page.pagina in provided or not 1 <= page.pagina <= len(local):
            raise DocumentError("El lector devolvió páginas de transcripción duplicadas o fuera del archivo.")
        provided[page.pagina] = page
    for page in local:
        if not page["visual"]:
            continue  # El texto digital original tiene prioridad sobre una reescritura del modelo.
        read = provided.get(page["pagina"])
        if read:
            original = page["texto"]
            page.update(texto=read.texto, estado=read.estado, motivo=None if read.estado == "leida" else read.estado)
            # No pierde texto seleccionable de una página mixta si el OCR no lo repite.
            extra = [line for line in original.splitlines() if line.strip() and line.strip() not in read.texto]
            if extra:
                page["texto"] += "\n" + "\n".join(extra)
            if read.estado == "leida" and not page["texto"].strip():
                page.update(estado="ilegible", motivo="ilegible")
            elif read.estado == "leida" and "[[ILEGIBLE]]" in read.texto:
                page.update(estado="parcial", motivo="ilegible")
    return bounded_pages(local)


def coverage(pages, mime):
    return {"completa": all(p["estado"] == "leida" for p in pages),
        "paginas": [{"pagina": p["pagina"] if mime == "application/pdf" else None,
            "estado": p["estado"], "motivo": p.get("motivo")} for p in pages]}


def coverage_notice(pages, mime):
    pending = [p for p in pages if p["estado"] != "leida"]
    if not pending:
        return ""
    labels = {"no_revisada": "sin revisar", "parcial": "incompleta", "ilegible": "ilegible"}
    return "Lectura parcial: " + "; ".join(
        (f"página {p['pagina']}" if mime == "application/pdf" else "imagen") + " " + labels[p["estado"]] +
        (" por límite de texto" if p.get("motivo") == "limite_texto" else " por fallo del proveedor" if p.get("motivo") == "proveedor" else "")
        for p in pending) + "."


def local_fragment(question, pages):
    """Etiquetas explícitas: devuelve solo el valor literal, sin normalizar fechas/importes."""
    from .document_privacy import requested_fields, FIELD_LABELS
    if re.search(r"\b(y|ademas|tambien)\b|,", plain(question)):
        return None  # El selector puede atender varias partes sin omitir campos ajenos al esquema.
    wanted = requested_fields(question)
    aliases = {"domicilio": ["domicilio", "direccion"], "fecha_vencimiento": ["vigencia", "vencimiento", "fecha de vencimiento"],
        "concepto_pago": ["concepto", "concepto de pago"], "periodicidad": ["periodicidad", "periodicidad de pago"],
        "neto_a_pagar": ["neto", "neto a pagar", "total neto"],
        "abonos": ["abonos", "total de abonos"], "cargos": ["cargos", "total de cargos"],
        "nombre": ["nombre", "nombre completo"], "titular": ["titular", "a nombre de"]}
    labels = [label for key in wanted for label in aliases.get(key, [plain(FIELD_LABELS[key])])]
    stop = set("que cual es el la los las de del en un una este esta aparece figura dice indica tiene su por favor documento archivo adjunto imagen identificacion recibo comprobante contrato bancario bancaria pdf pagina muestra dime extrae lee transcribe".split())
    terms = set(re.findall(r"\w+", plain(question))) - stop
    quotes = []
    for page in pages:
        lines = page["texto"].splitlines()
        for index, line in enumerate(lines):
            if ":" not in line:
                continue
            label, value = line.split(":", 1)
            key = plain(label.strip())
            words = set(re.findall(r"\w+", key))
            label_words = words - {"de", "del", "el", "la"}
            matches = any(label_words == set(label.split()) - {"de", "del", "el", "la"} for label in labels) if wanted else bool(terms and terms <= words)
            if matches and value.strip() and len(value.strip()) <= 1200:
                if index+1 < len(lines) and lines[index+1].strip() and ":" not in lines[index+1]:
                    return None  # Puede haber continuación: deja al selector recuperar el fragmento completo.
                quotes.append(Quote(pagina=page["pagina"], texto=value.strip()))
    if quotes and len(quotes) <= 8:
        return Fragment(estado="ilegible" if all("[[ILEGIBLE]]" in q.texto for q in quotes) else "encontrado", citas=quotes)
    return None


def quoted_source(text, page):
    """Devuelve la cita de la página con sus separadores y valores originales."""
    text = text.strip()
    if not text:
        return None
    # Un colon entre el rótulo y una fecha o importe es opcional. Dentro del valor es literal.
    label_separator = r"(?:[^\S\r\n]*:[^\S\r\n]*|\s+)"
    segments = re.split(r"(?<=[^\W\d_])" + label_separator + r"(?=[\d$€])", text)
    pattern = label_separator.join(r"\s+".join(map(re.escape, segment.split())) for segment in segments)
    numeric_start = bool(re.match(r"[$€+−.,/:\-]?[ \t]*\d", text))
    if text[0].isalnum() or numeric_start:
        pattern = r"(?<!\w)" + pattern
    if text[-1].isalnum():
        pattern += r"(?!\w)"
    for match in re.finditer(pattern, page):
        # Una parte de una cifra o una fecha no es un valor respaldado.
        before = page[:match.start()]
        after = page[match.end():]
        if numeric_start and re.search(r"(?:\d(?:[^\S\r\n]*[.,/:\-][^\S\r\n]*|[^\S\r\n]+)|[+−\-][^\S\r\n]*[$€]?[^\S\r\n]*)$", before):
            continue
        if text[-1].isdigit() and re.match(r"(?:[^\S\r\n]*[.,/:\-][^\S\r\n]*\d|[^\S\r\n]+\d{3}(?!\d))", after):
            continue
        if re.search(r"\d[.,/:\-]$", text) and re.match(r"[ \t]*\d", after):
            continue
        return match.group()
    return None


def validate_fragment(raw, pages, *, keep_supported=False):
    from .documents import DocumentError
    result = Fragment.model_validate(raw)
    known = {p["pagina"]: p["texto"] for p in pages}
    if result.estado in {"encontrado", "ilegible"} and not result.citas:
        raise DocumentError("El lector no aportó un fragmento verificable.")
    if result.estado == "ausente" and result.citas:
        raise DocumentError("Respuesta de lectura contradictoria.")
    supported, failures = [], []
    for quote in result.citas:
        original = quoted_source(quote.texto, known.get(quote.pagina, ""))
        if not original:
            failures.append("El fragmento no coincide literalmente con la página leída.")
        elif result.estado == "ilegible" and "[[ILEGIBLE]]" not in original:
            failures.append("La ilegibilidad no está respaldada por la transcripción.")
        else:
            supported.append(Quote(pagina=quote.pagina, texto=original))
    if failures and (not keep_supported or not supported):
        raise DocumentError(failures[0])
    return Fragment(estado=result.estado, citas=supported)


def fragment_answer(result, document, pages, mime, question):
    from .attachment_presentation import reference
    from .document_privacy import clean_visible_text, requested_fields, personal_values
    known = personal_values([document])
    for page in pages:
        for match in re.finditer(r"(?im)^(nombre(?: completo)?|titular|domicilio(?: (?:del? )?servicio| fiscal)?|direcci[oó]n|fecha de nacimiento)\s*:\s*([^\n]+)", page["texto"]):
            label = plain(match[1])
            name = "domicilio_fiscal" if "fiscal" in label else "domicilio_servicio" if "servicio" in label else "domicilio" if label in {"domicilio", "direccion"} else "fecha_nacimiento" if label == "fecha de nacimiento" else "titular" if label == "titular" else "nombre"
            known.append((name, match[2].strip()))
    if result.estado == "encontrado":
        lines = []
        for quote in result.citas:
            source = {"archivo": document["archivo"], "pagina": quote.pagina if mime == "application/pdf" else None}
            text = clean_visible_text(quote.texto, known_values=known, allow_fields=requested_fields(question))
            if "[[ILEGIBLE]]" in text:
                text = "Lectura parcial del dato: " + text.replace("[[ILEGIBLE]]", "[ilegible]")
            lines.append((text or "El fragmento contiene datos personales no solicitados.") + " (" + reference(source) + ").")
        answer = "\n".join(dict.fromkeys(lines))
    elif result.estado == "ilegible":
        locations = list(dict.fromkeys(reference({"archivo": document["archivo"],
            "pagina": q.pagina if mime == "application/pdf" else None}) for q in result.citas))
        answer = "El dato aparece, pero es ilegible (" + "; ".join(locations) + ")."
    elif any(p["estado"] != "leida" for p in pages):
        answer = "No encontré el dato en la parte legible; no puedo afirmar que no aparezca en lo que falta por leer (" + reference({"archivo": document["archivo"]}) + ")."
    else:
        answer = "El dato solicitado no aparece en el archivo (" + reference({"archivo": document["archivo"]}) + ")."
    notice = coverage_notice(pages, mime)
    return answer + ("\n" + notice if notice else "")


def selection_prompt(question, pages):
    return json.dumps({"pregunta": question, "paginas_datos_no_confiables": [
        {"pagina": p["pagina"], "texto": p["texto"]} for p in pages]}, ensure_ascii=False)
