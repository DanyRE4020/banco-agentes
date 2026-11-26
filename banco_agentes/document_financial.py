"""Evidencia de nómina/servicios; sin mínimos, fuentes documentales o emisores privilegiados."""
import re
from datetime import date
from decimal import Decimal, InvalidOperation

from .document_evidence import tokens, date_span

# Vocabulario de clasificación y equivalencias, NO lista de servicios admitidos por una política.
SERVICE_WORDS = {"electricidad": r"\bluz\b|electric", "agua": r"\bagua\b", "telefonia_fija": r"telefonia fija|telefono fijo",
    "gas": r"\bgas\b", "predial": r"\bpredial\b", "television_paga": r"television de paga"}


def plain(text):
    from .document_credit import plain as normalize
    return normalize(text)


def service_kind(text):
    value = plain(text)
    found = [key for key, pattern in SERVICE_WORDS.items() if re.search(pattern, value)]
    if len(found) == 1:
        return found[0]
    return "no_determinable" if not value.strip() or re.search(r"paquete|integral|no (?:se )?identifica", value) else "_".join(tokens(value))


def admitted_services(text):
    text = re.sub(r"\s+", " ", plain(text))
    # Solo la enumeración asociada al comprobante; no palabras sueltas de otras secciones.
    match = re.search(r"comprobante.{0,100}?domicilio[^:.]{0,160}:\s*([^.;]+)", text)
    if not match:
        return []
    listing = match[1]
    items = re.split(r",|;|\s+[yo]\s+", listing)
    return list(dict.fromkeys(service_kind(re.sub(r"^(?:recibos?|facturas?|servicios?)\s+de\s+", "", item.strip()))
        for item in items if item.strip()))


def date_value(text):
    value = text.strip()
    if re.fullmatch(r"\d{4}(?:-\d{2})?(?:-\d{2})?", value):
        date_span(value)
        return value
    match = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", value)
    if match:
        day, month, year = map(int, match.groups())
        return date(year, month, day).isoformat()
    months = {name: i for i, name in enumerate("enero febrero marzo abril mayo junio julio agosto septiembre octubre noviembre diciembre".split(), 1)}
    match = re.fullmatch(r"([a-z]+)\s+(?:de\s+)?(\d{4})", plain(value))
    if match and match[1] in months:
        return f"{match[2]}-{months[match[1]]:02}"
    raise ValueError("Fecha sin precisión interpretable")


def amount_value(text):
    value = re.sub(r"MXN|USD|EUR|pesos|\$|\s", "", text, flags=re.I)
    if "," in value and "." in value:
        value = value.replace(",", "") if value.rfind(".") > value.rfind(",") else value.replace(".", "").replace(",", ".")
    elif "," in value:
        value = value.replace(",", "") if re.fullmatch(r"\d{1,3}(?:,\d{3})+", value) else value.replace(",", ".")
    if not re.fullmatch(r"\d{1,12}(?:\.\d{1,2})?", value):
        raise ValueError("Importe ilegible")
    return str(Decimal(value).quantize(Decimal(".01")))


def local_financial_document(pages):
    """Rescate determinista de etiquetas claras del texto local. No hace otra llamada al proveedor."""
    text = "\n".join(p["texto"] for p in pages)
    normalized = plain(text)
    payroll = bool(re.search(r"\bcfdi\b[\s\S]{0,80}\bnomina\b|comprobante fiscal digital[\s\S]{0,80}nomina", normalized))
    service = bool(re.search(r"(?m)^\s*(?:servicio|tipo de servicio|suministro)\s*:", normalized))
    if not payroll and not service:
        return None
    fields = {}
    labels = {
        "titular": r"titular|receptor|nombre del receptor|empleado",
        "periodicidad": r"periodicidad(?: de pago)?", "concepto_pago": r"concepto(?: de pago)?",
        "neto_a_pagar": r"neto(?: a pagar)?|total neto", "percepciones_brutas": r"percepciones(?: brutas| totales)?|total percepciones",
        "deducciones": r"deducciones|total deducciones", "moneda": r"moneda",
        "periodo_inicio": r"inicio del periodo|periodo inicio|fecha (?:inicial|de inicio) de pago", "periodo_fin": r"fin del periodo|periodo fin|fecha (?:final|de fin) de pago",
        "tipo_comprobante": r"servicio|tipo de servicio|suministro",
        "domicilio_servicio": r"domicilio (?:del? )?servicio|direccion del servicio|domicilio de suministro",
        "domicilio_fiscal": r"domicilio fiscal", "domicilio": r"domicilio",
        "fecha_emision": r"fecha de emision|fecha emision|emision",
        "fecha_expedicion": r"fecha de expedicion|fecha expedicion|expedicion",
        "mes_facturado": r"mes facturado|periodo facturado|mes de facturacion",
        "fecha_limite_pago": r"fecha limite de pago|limite de pago|pagar antes de",
        "fecha_corte": r"fecha de corte|corte", "importe_total": r"importe total|total a pagar|importe",
    }
    dates = {"periodo_inicio", "periodo_fin", "fecha_emision", "fecha_expedicion", "mes_facturado", "fecha_limite_pago", "fecha_corte"}
    amounts = {"neto_a_pagar", "percepciones_brutas", "deducciones", "importe_total"}
    for page in pages:
        if page["estado"] == "ilegible":
            continue
        lines = [line.strip() for line in page["texto"].splitlines() if line.strip()]
        pairs = []
        for i, line in enumerate(lines):
            if ":" in line:
                label, original = line.split(":", 1)
                if not original.strip() and i+1 < len(lines):
                    original = lines[i+1]
            elif any(re.fullmatch(pattern, plain(line)) for pattern in labels.values()) and i+1 < len(lines):
                label, original = line, lines[i+1]
                if original == "$" and i+2 < len(lines):
                    original += lines[i+2]
            else:
                continue
            if ":" in original or any(re.fullmatch(pattern, plain(original)) for pattern in labels.values()):
                continue
            pairs.append((label, original))
        for label, original in pairs:
            original = original.strip()
            if len(label) > 80 or len(original) > 500:
                continue
            for name, pattern in labels.items():
                if not re.fullmatch(pattern, plain(label.strip())) or not original:
                    continue
                try:
                    value = date_value(original) if name in dates else amount_value(original) if name in amounts else service_kind(original) if name == "tipo_comprobante" else original
                except (ValueError, InvalidOperation):
                    if name == "mes_facturado":
                        parts = re.split(r"\s+(?:al|a|hasta|[-–—])\s+", original, maxsplit=1)
                        if len(parts) == 2:
                            try:
                                start, end = (date_value(part) for part in parts)
                            except ValueError:
                                continue
                            for boundary, value, literal in zip(("inicio", "fin"), (start, end), parts):
                                fields.setdefault("periodo_" + boundary, {"valor": value, "texto_original": literal,
                                    "etiqueta": f"{label.strip()} ({boundary})", "pagina": page["pagina"],
                                    "confianza": .99, "lectura": "leido"})
                    continue  # No sustituye una fecha ambigua por otra de significado distinto.
                item = {"valor": value, "texto_original": original, "etiqueta": label.strip(), "pagina": page["pagina"], "confianza": .99, "lectura": "leido"}
                if name not in fields:
                    fields[name] = item
                elif fields[name]["valor"] != value:
                    fields[name]["confianza"] = .4  # No decide entre valores contradictorios.
                if name == "tipo_comprobante":
                    fields["servicio_descripcion"] = {**item, "valor": original}
    if payroll and "moneda" not in fields:
        currencies = set(re.findall(r"\b(?:MXN|USD|EUR)\b", text))
        if len(currencies) == 1:
            code = next(iter(currencies))
            source = next(p for p in pages if code in p["texto"])
            fields["moneda"] = {"valor":code,"texto_original":code,"etiqueta":"Moneda impresa", "pagina":source["pagina"],"confianza":.99,"lectura":"leido"}
        elif "$" in fields.get("neto_a_pagar", {}).get("texto_original", ""):
            fields["moneda"] = {"valor": "$", "etiqueta": "Símbolo del neto", "pagina": fields["neto_a_pagar"]["pagina"],
                                "confianza": .99, "lectura": "leido"}
    if payroll and "periodicidad" in fields:
        value = plain(fields["periodicidad"]["valor"])
        if value in {"05", "mensual", "mes"}:
            fields["periodicidad"]["valor"] = "mensual"
    return {"tipo": "cfdi_nomina" if payroll else "comprobante", "legible": bool(fields), "campos": fields}


def proof_date(doc):
    """Devuelve nombre(s), campos e intervalo; pago/corte nunca sustituyen emisión/facturación."""
    from .document_evidence import field_problem
    fields = doc["campos"]
    preferred = ("fecha_emision", "fecha_expedicion", "mes_facturado")
    for name in preferred:
        if field_problem(fields.get(name)) is None:
            return [name], name not in {"fecha_emision", "fecha_expedicion"}
    if all(field_problem(fields.get(n)) is None for n in ("periodo_inicio", "periodo_fin")):
        return ["periodo_inicio", "periodo_fin"], True
    for name in preferred:
        if field_problem(fields.get(name)) != "ausente":
            return [name], name == "mes_facturado"
    if any(field_problem(fields.get(n)) != "ausente" for n in ("periodo_inicio", "periodo_fin")):
        return ["periodo_inicio", "periodo_fin"], True
    return ["fecha_emision"], False


def service_address(doc):
    fields = doc["campos"]
    if (fields.get("domicilio_servicio") or {}).get("valor") or (fields.get("domicilio_fiscal") or {}).get("valor"):
        return "domicilio_servicio"
    return "domicilio"
