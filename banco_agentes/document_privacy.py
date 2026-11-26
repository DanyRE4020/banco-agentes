"""Minimización del texto libre de adjuntos; independiente del modelo y de los permisos."""
import re
import unicodedata

OMITTED = "[dato personal omitido]"
PERSONAL_FIELDS = {"nombre", "titular", "domicilio", "domicilio_servicio", "domicilio_fiscal", "fecha_nacimiento"}
FIELD_LABELS = {"titular": "titular", "nombre": "nombre", "domicilio": "domicilio",
    "fecha_nacimiento": "fecha de nacimiento", "fecha_vencimiento": "fecha de vencimiento",
    "saldo_inicial": "saldo inicial", "saldo_final": "saldo final", "abonos": "total de abonos",
    "cargos": "total de cargos", "periodo_inicio": "inicio del periodo", "periodo_fin": "fin del periodo",
    "moneda": "moneda", "numero_enmascarado": "número de identificación enmascarado"}
FIELD_LABELS.update(fecha_emision="fecha de emisión", tipo_comprobante="tipo de comprobante", tipo_identificacion="tipo de identificación", emisor="emisor", importe_total="importe total")
FIELD_LABELS.update(periodicidad="periodicidad", concepto_pago="concepto de pago", neto_a_pagar="neto a pagar",
    percepciones_brutas="percepciones brutas", deducciones="deducciones", domicilio_servicio="domicilio del servicio",
    domicilio_fiscal="domicilio fiscal", servicio_descripcion="servicio", fecha_expedicion="fecha de expedición",
    mes_facturado="mes facturado", fecha_limite_pago="límite de pago", fecha_corte="fecha de corte")


def requested_fields(question):
    text = "".join(c for c in unicodedata.normalize("NFKD", question.lower()) if not unicodedata.combining(c))
    patterns = {"nombre": r"\bnombre\b", "titular": r"\btitular\b|a nombre de",
        "domicilio": r"(?:cual|dime|indica|muestra|extrae|que)\b[^?¿\n]{0,35}\b(?:domicilio|direccion)\b",
        "fecha_nacimiento": r"fecha de nacimiento|cuando nacio",
        "fecha_vencimiento": r"vencimiento|cuando vence|caducidad|cuando expira|\bvigencia\b",
        "fecha_emision": r"fecha de emision|cuando se emitio|fecha de expedicion",
        "tipo_comprobante": r"tipo de comprobante", "tipo_identificacion": r"tipo de identificacion",
        "emisor": r"\bemisor\b|quien (?:emitio|expidio)", "importe_total": r"importe total|total a pagar|monto del recibo",
        "neto_a_pagar": r"\bneto\b", "concepto_pago": r"\bconcepto\b", "periodicidad": r"\bperiodicidad\b",
        "percepciones_brutas": r"percepciones|\bbruto\b", "deducciones": r"deducciones",
        "domicilio_servicio": r"domicilio (?:del? )?servicio|direccion del servicio", "domicilio_fiscal": r"domicilio fiscal",
        "fecha_expedicion": r"fecha de expedicion|cuando se expidio", "mes_facturado": r"mes facturado|periodo facturado",
        "fecha_limite_pago": r"limite de pago|pagar antes", "fecha_corte": r"fecha de corte",
        "saldo_inicial": r"saldo (?:inicial|de apertura)", "saldo_final": r"saldo (?:final|al cierre)",
        "abonos": r"\babonos\b", "cargos": r"\bcargos\b", "moneda": r"\bmoneda\b",
        "periodo_inicio": r"inicio del periodo|periodo inicial", "periodo_fin": r"fin del periodo|periodo final",
        "numero_enmascarado": r"numero de (?:identificacion|documento)"}
    wanted = {field for field, pattern in patterns.items() if re.search(pattern, text)}
    if wanted & {"domicilio_servicio", "domicilio_fiscal"}:
        wanted.discard("domicilio")
    if re.search(r"\bnombre\s+(?:del?|de la)\s+(?:archivo|documento|producto|banco|credito|tarjeta)\b", text):
        wanted.discard("nombre")
    return wanted


def personal_values(documents):
    return [(name, field["valor"]) for doc in documents for name, field in doc.get("campos", {}).items()
            if name in PERSONAL_FIELDS and field.get("valor")]


def safe_free_text(text, *, known_values=(), allow_fields=()):
    """No conserva texto removido. Las excepciones solo se usan para campos pedidos explícitamente."""
    text = str(text)
    allow_fields = set(allow_fields)
    if "domicilio" in allow_fields:
        allow_fields.update({"domicilio_servicio", "domicilio_fiscal"})
    if allow_fields & {"nombre", "titular"}:
        allow_fields.update({"nombre", "titular"})
    # OCR puede juntar varios campos en una línea: conserva los hechos financieros separados.
    text = re.sub(r"\s+(?=(?:saldo\s+(?:inicial|final)|total\s+de\s+(?:abonos|cargos)|comisi[oó]n|CURP|RFC|clave\s+de\s+elector)\s*:)", "\n", text, flags=re.I)
    for name, value in sorted(known_values, key=lambda item: len(item[1]), reverse=True):
        if name not in allow_fields:
            text = re.sub(r"(?<![^\W_])" + re.escape(value) + r"(?![^\W_])", lambda _: OMITTED, text, flags=re.I)
    # Identificadores mexicanos, incluso sin etiqueta o dentro de nombres de archivo.
    for pattern in (r"(?<![A-Z0-9])[A-ZÑ]{4}\d{6}[HMX][A-ZÑ]{5}[A-Z0-9]\d(?![A-Z0-9])",
                    r"(?<![A-Z0-9])[A-ZÑ&]{3,4}\d{6}[A-Z0-9]{3}(?![A-Z0-9])",
                    r"(?<![A-Z0-9])[A-Z]{6}\d{8}[HM]\d{3}(?![A-Z0-9])",
                    r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b",
                    r"(?<![\w.])\d{12,20}(?![\w.])"):
        text = re.sub(pattern, lambda _: OMITTED, text, flags=re.I)
    labels = r"CURP|RFC|clave\s+(?:de\s+)?elector|OCR|CIC|pasaporte|CLABE|(?:n[uú]mero\s+de\s+)?(?:cuenta|tarjeta)|tel[eé]fono|celular|correo(?:\s+electr[oó]nico)?"
    text = re.sub(rf"\b({labels})\s*[:=]\s*[^\n;,]+", lambda m: m[1]+": "+OMITTED, text, flags=re.I)
    if not allow_fields & {"domicilio", "domicilio_servicio", "domicilio_fiscal"}:
        text = re.sub(r"\b(domicilio(?:\s+(?:del?\s+)?servicio|\s+fiscal)?|direcci[oó]n)\s*[:=]\s*[^\n;]+", lambda m: m[1]+": "+OMITTED, text, flags=re.I)
        text = re.sub(r"\b(?:calle|avenida|av\.|boulevard|blvd\.|calzada|privada|carretera)\s+[^\n;]{0,100}?\d[^\n;]*", lambda _: OMITTED, text, flags=re.I)
    if not {"nombre", "titular"} & set(allow_fields):
        text = re.sub(r"\b(nombre(?:\s+completo)?|titular|beneficiario)\s*[:=]\s*[^\n;,]+", lambda m: m[1]+": "+OMITTED, text, flags=re.I)
    if "fecha_nacimiento" not in allow_fields:
        text = re.sub(r"\bfecha\s+de\s+nacimiento\s*[:=]\s*[^\n;,]+", lambda _: "Fecha de nacimiento: "+OMITTED, text, flags=re.I)
    lines = []
    for line in text.splitlines():
        # Un rótulo personal ya filtrado no aporta nada a la respuesta ni a la evidencia guardada.
        if OMITTED in line and re.fullmatch(r"[\w\s/().:-]*\[dato personal omitido\][\s.,;]*", line):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def clean_visible_text(text, **kwargs):
    # Los IDs internos se excluyen al construir la presentación, sin borrar códigos reales del archivo.
    return safe_free_text(text, **kwargs)


def public_attachment_data(result, question):
    """Mantiene el contrato detallado sin enviar datos personales que el chat no necesita."""
    import copy
    keys = ("documents", "document_errors", "policy", "derived", "document_view", "attachment_decision", "attachment_view")
    payload = copy.deepcopy({key: result.get(key) for key in keys})
    decision = result.get("attachment_decision") or {}
    wanted = requested_fields(question) if decision.get("alcance") == "bancario" and decision.get("accion") == "responder" else set()
    for doc in payload["documents"] or []:
        doc.pop("respuesta_lectura", None)
        doc.pop("confirmaciones_lectura", None)
        for name, field in doc["campos"].items():
            if name in PERSONAL_FIELDS and name not in wanted:
                field["valor"] = None
                field.pop("etiqueta", None)
                if "valor_original" in field:
                    field["valor_original"] = None
                doc.get("correcciones", {}).pop(name, None)
            elif field.get("valor") is not None:
                field["valor"] = safe_free_text(field["valor"], allow_fields=wanted)
    for row in (payload["derived"] or {}).get("requisitos", []):
        for field in row["evidencia"]:
            if field["campo"] in PERSONAL_FIELDS and field["campo"] not in wanted:
                field["valor"] = None
                field.pop("etiqueta", None)
                if "valor_original" in field:
                    field["valor_original"] = None
    return payload
