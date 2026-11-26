"""Redacción para clientes a partir del reconocimiento existente; sin llamadas ni evaluación."""
import re

from .attachment_presentation import reference
from .document_credit import PRODUCTS, plain, requested_product
from .presentation import chat_decimal_text
from .document_privacy import clean_visible_text

OPENING = "Reconocí estos documentos que subiste"
CLOSING = "Reconocí los archivos; el banco debe validar los requisitos y evaluar la solicitud."


def parallel_requirements(text):
    """Separar requisitos enumerados en prosa sin partir alternativas entre paréntesis."""
    starts = r"(?:(?:un|una)\s+)?(?:identificaci[oó]n|comprobante|constancia|recibo|estado de cuenta|ingreso|edad|presentar|contar con|tener|recibir)\b"
    boundaries = re.finditer(r",\s*(?=" + starts + r")|\s+[ye]\s+(?=" + starts + r")", text, re.I)
    parts, start = [], 0
    for boundary in boundaries:
        preceding = text[:boundary.start()]
        if preceding.count("(") != preceding.count(")"):
            continue
        parts.append(text[start:boundary.start()])
        start = boundary.end()
    return [*parts, text[start:]]


def document_name(doc, recognized):
    kind = doc["tipo"]
    if kind == "identificacion":
        return "INE" if any(e["campo"] == "clase_identificacion" for e in (recognized or {}).get("evidencia", [])) else "Identificación"
    if kind == "comprobante":
        service = doc.get("campos", {}).get("tipo_comprobante", {})
        if service.get("estado") == "confirmado":
            names = {"electricidad": "recibo de luz", "agua": "recibo de agua", "gas": "recibo de gas",
                     "telefonia_fija": "recibo de teléfono", "television_paga": "recibo de televisión",
                     "predial": "comprobante de predial"}
            if service.get("valor") in names:
                return names[service["valor"]]
        return "Comprobante de domicilio"
    return {"cfdi_nomina": "recibo de nómina", "recibo_pago": "recibo de pago", "estado_de_cuenta": "estado de cuenta",
            "contrato_bancario": "contrato bancario", "captura": "imagen", "documento": "documento",
            "desconocido": "archivo cuyo tipo no pude reconocer"}.get(kind, "documento")


def requirement_items(text, question):
    """Separa la lista recuperada y omite condiciones ajenas; nunca genera requisitos nuevos."""
    text = text.strip()
    # Algunos fragmentos incluyen más de una sección del PDF. Solo presentar requisitos/documentos.
    heading = re.search(r"(?:^|\n)\s*(?:\d+[.)]\s*)?(?:requisitos|documentaci[oó]n requerida)\s*:?\s*(?:\n|$)", text, re.I)
    if heading:
        text = text[heading.end():]
        text = re.split(r"\n\s*\d+[.)]\s+[A-ZÁÉÍÓÚ]", text, maxsplit=1)[0]
    text = re.sub(r"\s*[•●▪‣]\s*", "\n• ", text)
    text = re.sub(r"(?:^|\n)\s*(?:[*-]|\d+[.)])\s+", "\n• ", text)
    parts = re.split(r"\n•\s*|;\s*|(?<=[.!?])\s+(?=[A-ZÁÉÍÓÚ¿])", text)
    parts = [item for part in parts for item in parallel_requirements(part)]
    age_asked = bool(re.search(r"\bedad|\banos\b|\bedades\b", plain(question)))
    payment_asked = bool(re.search(r"pagos?\s+minimos?", plain(question)))
    result = []
    for part in parts:
        part = re.sub(r"\s+", " ", part).strip(" •-\n")
        part = re.sub(r"^(?:Documentos solicitados|Otros requisitos|Requisitos)\s*:\s*", "", part, flags=re.I)
        if not part:
            continue
        normalized = plain(part)
        # Notas sobre lo que una fuente no explica no son condiciones para solicitar el producto.
        if re.match(r"(?:(?:este|el|esta|la)\s+(?:documento|fuente|fragmento|resumen|texto|pagina|seccion|guia)\s+no\s+|no\s+se\s+)(?:indica|explica|describe|detalla|incluye|especifica|documenta|aclara)\b", normalized):
            continue
        if not payment_asked:
            part = re.sub(r"(?:[,.:]?\s*(?:y\s+)?)(?:el\s+)?pago\s+m[ií]nimo\b.*$", "", part, flags=re.I)
        if not age_asked:
            # Conservar la edad mínima de un rango, sin repetir la máxima no solicitada.
            part = re.sub(r"(?:tener\s+)?(?:una\s+)?edad\s*:?\s*(?:de\s+|entre\s+)?(\d+)\s*(?:a|y|hasta|[-–—])\s*\d+\s*años", r"Tener al menos \1 años", part, flags=re.I)
            part = re.sub(r"(?:tener\s+)?entre\s+(\d+)\s+y\s+\d+\s*años", r"Tener al menos \1 años", part, flags=re.I)
            part = re.sub(r"^de\s+(\d+)\s+a\s+\d+\s*años", r"Tener al menos \1 años", part, flags=re.I)
            part = re.sub(r"(?:[,.:]?\s*(?:y\s+)?)(?:una\s+)?edad\s+m[áa]xima\b[^.;]*[.;]?", "", part, flags=re.I)
            part = re.sub(r"\s+(?:y|a|hasta)\s+(?:de\s+)?\d+\s*años\s+(?:como\s+)?m[áa]xim[oa]", "", part, flags=re.I)
            part = re.sub(r"\s+y\s+hasta\s+\d+\s*años", "", part, flags=re.I)
            if re.fullmatch(r"(?:ser\s+)?(?:menor\s+de|tener\s+(?:menos\s+de|hasta))\s+\d+\s*años[.]?", part, re.I):
                part = ""
        part = part.strip(" ,:;.")
        if part:
            relevant = re.search(r"\b(?:tener|contar con|presentar|acreditar|entregar|recibir|proporcionar|mantener|ser|debe|debera|requiere|requieren|exige|exigen|obligatorio)\b", plain(part))
            relevant = relevant or re.match(r"(?:identificacion|comprobante|constancia|recibo|estado de cuenta|pasaporte|tarjeta de residencia|edad|ingreso|telefono|nacionalidad|residencia)\b", plain(part))
            explicit = (payment_asked and "pago minimo" in plain(part)) or (age_asked and "edad" in plain(part))
            procedural = re.search(r"como\s+(?:solicit|contrat)|pasos|tramite|sucursal", plain(question))
            if not (relevant or explicit or procedural):
                continue
            part = part[0].upper() + part[1:]
            if part + "." not in result:
                result.append(part + ".")
    return result


def structured_requirement(rule, question):
    name, value, operator = rule["campo"], rule.get("valor"), rule.get("operador")
    if rule.get("descripcion") or rule.get("texto"):
        return requirement_items(rule.get("descripcion") or rule["texto"], question)
    if name in {"edad_anios", "edad_meses"}:
        if operator in {"<=", "<"} and not re.search(r"edad|\banos\b", plain(question)):
            return []
        unit = "años" if name == "edad_anios" else "meses"
        qualifier = {">=": "al menos", ">": "más de", "<=": "como máximo", "<": "menos de"}.get(operator, "")
        text = f"Tener {qualifier} {value} {unit}"
    elif name == "ingreso_mensual":
        qualifier = {">=": "al menos", ">": "más de"}.get(operator, "de")
        text = f"Contar con ingresos mensuales comprobables de {qualifier} {chat_decimal_text(value)} {rule.get('moneda', '')}".strip()
    elif name == "antiguedad_comprobante_meses":
        text = f"Presentar un comprobante de domicilio de no más de {value} meses"
    else:
        text = {"vigencia_identificacion": "Presentar una identificación oficial vigente",
                "identificacion_oficial": "Presentar una identificación oficial vigente",
                "recepcion_nomina_o_pension": "Recibir la nómina o pensión",
                "telefono_actualizado": "Tener un teléfono de contacto actualizado"}.get(name,
                    "Requisito de " + name.replace("_", " "))
    if rule.get("condicion"):
        text += " (" + rule["condicion"] + ")"
    return [text + "."]


def client_view(documents, policy, recognized, rows, *, errors=(), question=""):
    policy = policy or {}
    by_id = {r["documento_id"]: r for r in recognized}
    names = list(dict.fromkeys(document_name(d, by_id.get(d.get("id"))) for d in documents))
    all_failed = bool(errors) and not documents
    opening = "" if all_failed else OPENING
    closing = "El banco debe validar los requisitos y evaluar la solicitud." if all_failed else CLOSING
    requested = {c["categoria"] for c in policy.get("categorias_documentales", [])}
    present = {r["categoria"] for r in recognized}
    missing, pending, uncertain = [], [], []
    for row in rows:
        if row["status"] == "ausente":
            missing.append(row["label"])
        elif row["status"] == "pendiente":
            (pending if row["categoria"] in present else uncertain).append(row["label"])
    messages = ["La lectura fue parcial."] if documents and errors else []
    messages.extend(f"{error['archivo']} está pendiente de lectura." for error in errors)
    if all_failed:
        messages.append("No pude determinar qué documentos faltan.")
    else:
        if not names:
            messages.append("No pude reconocer los documentos con la lectura disponible.")
        if missing:
            messages.append("Falta subir: " + ", ".join(missing) + ".")
        if uncertain:
            messages.append("No pude determinar si faltan: " + ", ".join(uncertain) + "; hay archivos sin reconocer.")
        if not missing and not uncertain:
            if requested and requested <= present:
                messages.append("No falta ninguno de los tipos de documentos solicitados que pude recuperar de la política.")
            else:
                messages.append("No pude determinar si falta algún tipo de documento porque no recuperé la lista solicitada en la política.")
        if pending:
            messages.append("No pude completar la lectura de: " + ", ".join(pending) + ".")
        if documents and not recognized:
            messages.append("El adjunto no aporta evidencia de los documentos solicitados para este trámite.")
    product = requested_product(question) or policy.get("producto")
    product_name = PRODUCTS.get(product) or policy.get("nombre_producto")
    if not product_name and product == "general":
        # Otros productos conservan el nombre de la política recuperada, sin catálogo nuevo.
        titles = {r.get("fuente", {}).get("title") for r in
                  (policy.get("requisitos_recuperados") or policy.get("extractos", []))} - {None, ""}
        if len(titles) == 1:
            product_name = next(iter(titles))
    product_name = product_name or (product.replace("_", " ") if product and product != "general" else "el producto consultado")
    title = "Requisitos para solicitar " + product_name
    requirements, references = [], []
    def cite(source):
        ref = reference(source)
        if source.get("version"):
            ref += " · versión " + str(source["version"])
        if source.get("consulted_at"):
            ref += " · consulta " + str(source["consulted_at"])
        if ref not in references:
            references.append(ref)
        return references.index(ref) + 1
    for doc in documents:
        record = by_id.get(doc.get("id"), {})
        sources = [e["fuente"] for e in record.get("evidencia", [])]
        if not sources:
            sources = [f["fuente"] for f in doc.get("campos", {}).values() if f.get("fuente")][:1]
        if not sources:
            sources = [e["fuente"] for e in doc.get("evidencia", [])][:1]
        for source in sources:
            cite(source)
    extracts = policy.get("requisitos_recuperados") or policy.get("extractos", [])
    for extract in extracts:
        items = requirement_items(extract["texto"], question)
        if items:
            index = cite(extract["fuente"])
            requirements.extend({"text": clean_visible_text(t), "source_indices": [index]} for t in items)
    if not extracts:
        for rule in policy.get("reglas", []):
            items = structured_requirement(rule, question)
            if items:
                index = cite(rule.get("fuente", {}))
                requirements.extend({"text": clean_visible_text(t), "source_indices": [index]} for t in items)
    unique = {}
    for item in requirements:
        stored = unique.setdefault(item["text"], {"text": item["text"], "source_indices": []})
        stored["source_indices"] = list(dict.fromkeys(stored["source_indices"] + item["source_indices"]))
    requirements = list(unique.values())
    if not requirements:
        requirements = [{"text": "No pude recuperar los requisitos del producto; falta consultar su política."}]
    view = {"style": "client_recognition", "opening": opening, "document_names": names, "messages": messages,
            "policy_title": title, "policy_requirements": requirements, "closing": closing,
            "requirements": [], "confirmations": [], "references": references}
    sections = []
    if opening:
        sections.append("\n".join([opening, *("- " + name for name in names)]))
    if messages:
        sections.append("\n".join(messages))
    sections.append(title + "\n" + "\n".join("- " + r["text"] + (
        " [" + ", ".join(map(str, r["source_indices"])) + "]" if r.get("source_indices") else "") for r in requirements))
    sections.append(closing)
    return "\n\n".join(sections), view
