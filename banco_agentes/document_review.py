"""Reconocimiento documental, sin decisiones de elegibilidad ni datos personales en la vista."""
import re

from .attachment_presentation import reference
from .document_credit import plain

LABELS = {"identificacion": "Identificación", "domicilio": "Comprobante de domicilio",
          "ingresos": "Comprobante de ingresos"}



def policy_documentation(hits):
    """Las categorías y el cierre de la lista proceden de la política recuperada, nunca del producto."""
    categories, requirements = {}, []
    closed_lists = []
    for hit in hits:
        source, text = hit["source"], hit["text"].strip()
        origin = {**source, "fragment_id": hit["id"]}
        requirements.append({"texto": text, "fuente": origin})
        explicit = source.get("categorias_documentales", [])
        for category in explicit:
            item = {"categoria": category} if isinstance(category, str) else category
            key = item.get("categoria")
            if key:
                categories[key] = {**item, "fuente": origin}
        if explicit and source.get("total_documental_definido") is True and not any(
                isinstance(c, dict) and c.get("condicion") for c in explicit):
            closed_lists.append({c if isinstance(c, str) else c["categoria"] for c in explicit})
        # Una mención numérica de ingreso no prueba que se solicite un comprobante.
        patterns = {"identificacion": r"identificacion\s+(?:oficial|vigente)|credencial\s+(?:para\s+votar|ine)",
                    "domicilio": r"comprobante\s+de\s+domicilio",
                    "ingresos": r"comprobante(?:s)?\s+de\s+ingresos|recibo(?:s)?\s+de\s+nomina|cfdi\s+de\s+nomina"}
        for key, pattern in patterns.items():
            if re.search(pattern, plain(text)):
                categories.setdefault(key, {"categoria": key, "fuente": origin})
    # Solo una lista cerrada explícita y coherente permite afirmar un denominador.
    closed = bool(closed_lists) and all(c == closed_lists[0] for c in closed_lists)
    closed = closed and set(categories) == closed_lists[0]
    return {"categorias_documentales": list(categories.values()), "total_documental_definido": bool(closed),
            "requisitos_recuperados": requirements}


def _read(field):
    if not field or field.get("lectura") in {"ausente", "ilegible"} or field.get("confianza", 0) < .85:
        return None
    source = field.get("fuente", {})
    if "pagina" in source and not source["pagina"]:
        return None
    return field.get("valor_original") or field.get("valor")


def _evidence(doc, name, *, meaning=None):
    from .document_privacy import PERSONAL_FIELDS, clean_visible_text, personal_values
    field = doc["campos"][name]
    result = {"documento_id": doc.get("id"), "campo": name, "fuente": field["fuente"]}
    # No copiar domicilio, nombre, importe ni texto completo al resultado derivado.
    if meaning:
        result["significado"] = meaning
    if field.get("etiqueta") and name not in PERSONAL_FIELDS:
        result["etiqueta"] = clean_visible_text(field["etiqueta"], known_values=personal_values([doc]))
    if field.get("precision"):
        result["precision"] = field["precision"]
    return result


def recognize(doc):
    fields, kind = doc.get("campos", {}), doc["tipo"]
    def available(names):
        return next((n for n in names if _read(fields.get(n))), None)
    evidence, missing = [], []
    if kind == "identificacion":
        category = "identificacion"
        ine = _read(fields.get("clase_identificacion"))
        if not ine or plain(str(ine)) not in {"ine", "credencial para votar ine"}:
            missing.append("reconocerla como INE por su contenido")
        else:
            evidence.append(_evidence(doc, "clase_identificacion"))
        expiry = _read(fields.get("fecha_vencimiento"))
        if not expiry or not re.search(r"\b(?:19|20|21)\d{2}\b", str(expiry)):
            missing.append("año o rango de vigencia legible")
        else:
            evidence.append(_evidence(doc, "fecha_vencimiento", meaning="vigencia impresa"))
    elif kind == "comprobante":
        category = "domicilio"
        address = available(("domicilio_servicio", "domicilio", "domicilio_fiscal"))
        if address:
            evidence.append(_evidence(doc, address, meaning=address.replace("_", " ")))
        else:
            missing.append("domicilio legible en el comprobante")
        dates = ("fecha_emision", "fecha_expedicion", "mes_facturado", "periodo_inicio", "periodo_fin",
                 "fecha_limite_pago", "fecha_corte", "fecha_vencimiento", "fecha_documento")
        readable = [n for n in dates if _read(fields.get(n)) and re.search(r"\d", str(_read(fields[n])))]
        if readable:
            evidence.extend(_evidence(doc, n, meaning=n.replace("_", " ")) for n in readable)
        else:
            missing.append("al menos una fecha legible con su etiqueta")
    elif kind in {"cfdi_nomina", "recibo_pago", "estado_de_cuenta"}:
        category = "ingresos"
        amounts = (("neto_a_pagar", "neto"), ("abonos", "abonos"), ("saldo_final", "saldo"),
                   ("saldo_inicial", "saldo"), ("importe_total", "otro"), ("percepciones_brutas", "percepciones brutas"),
                   ("deducciones", "deducciones"), ("cargos", "cargos"))
        amount = next(((n, meaning) for n, meaning in amounts if _read(fields.get(n))), None)
        if amount:
            evidence.append(_evidence(doc, amount[0], meaning=amount[1]))
        else:
            missing.append("monto legible y su significado")
        if available(("periodo_documento",)):
            evidence.append(_evidence(doc, "periodo_documento", meaning="periodo impreso"))
        elif all(_read(fields.get(n)) for n in ("periodo_inicio", "periodo_fin")):
            evidence.extend(_evidence(doc, n, meaning="periodo impreso") for n in ("periodo_inicio", "periodo_fin"))
        else:
            missing.append("periodo legible (la periodicidad por sí sola no indica qué periodo cubre)")
    else:
        return None
    details = []
    for item in evidence:
        label = item.get("etiqueta") or item.get("significado")
        if label and label not in details:
            details.append(label)
    source = next((e["fuente"] for e in evidence), next(
        (f["fuente"] for f in fields.values() if f.get("fuente")), {"archivo": "Archivo adjunto"}))
    reason = ("Pendiente: " + "; ".join(missing) if missing else "Contenido y datos necesarios legibles")
    if details:
        reason += " · " + ", ".join(details)
    reason += " (" + reference(source) + ")."
    return {"categoria": category, "estado": "pendiente" if missing else "aportada", "motivo": reason,
            "documento_id": doc.get("id"), "evidencia": evidence, "datos_pendientes": missing}


def review_documents(documents, policy, *, errors=(), question=""):
    recognized = [r for d in documents if (r := recognize(d))]
    requested = {c["categoria"]: c for c in (policy or {}).get("categorias_documentales", [])}
    rows = []
    for key in dict.fromkeys([*requested, *(r["categoria"] for r in recognized)]):
        candidates = [r for r in recognized if r["categoria"] == key]
        complete = next((r for r in candidates if r["estado"] == "aportada"), None)
        selected = complete or (candidates[0] if candidates else None)
        rows.append({"categoria": key, "label": LABELS.get(key, requested.get(key, {}).get("etiqueta", key.replace("_", " "))),
                     "status": selected["estado"] if selected else "pendiente" if errors else "ausente",
                     "reason": selected["motivo"] if selected else "Sin evidencia legible suficiente en los adjuntos." if errors else "No se aportó esta categoría solicitada."})
    from .document_review_presentation import client_view
    summary, view = client_view(documents, policy, recognized, rows, errors=errors, question=question)
    return {"tipo": "revision_documental", "categorias": rows, "reconocimientos": recognized,
            "lecturas_no_disponibles": list(errors), "summary": summary, "view": view}
