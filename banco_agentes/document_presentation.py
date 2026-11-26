"""Presentación determinista del crédito. No evalúa requisitos ni cambia la evidencia."""
import re

from .documents import effective_document
from .presentation import document_amounts

LABELS = {"edad": "el rango de edad general", "ingreso_mensual": "el ingreso mensual",
          "monto_ingreso_mensual": "el monto mínimo de ingreso", "aceptacion_ingreso_comprobable": "la aceptación como ingreso comprobable",
          "tipo_servicio_comprobante": "el tipo de servicio",
          "vigencia_identificacion": "la vigencia de la identificación",
          "antiguedad_comprobante_meses": "el comprobante de domicilio",
          "identificacion_oficial": "el carácter oficial de la identificación",
          "recepcion_nomina_o_pension": "la recepción de nómina o pensión",
          "telefono_actualizado": "el teléfono actualizado"}
PRELIMINARY = "Revisión preliminar; la aprobación corresponde al banco."


def listed(labels):
    labels = list(dict.fromkeys(labels))
    return labels[0] if len(labels) == 1 else ", ".join(labels[:-1]) + " y " + labels[-1]


def credit_document_view(documents, derived):
    """Resume solo estados ya calculados; conserva índices de todas las filas originales."""
    groups = {}
    rows = derived["requisitos"]
    for index, row in enumerate(rows):
        name = "edad" if row["regla"] in {"edad_anios", "edad_meses"} else row["regla"]
        if name == "ingreso_mensual" and row.get("dimension") == "monto":
            name = "monto_ingreso_mensual"
        condition = row.get("condicion") or "general"
        # Los límites de edad comparten resultado solo dentro de la misma condición.
        key = (name, condition)
        group = groups.setdefault(key, {"name": name, "condition": condition, "indices": [], "states": []})
        group["indices"].append(index)
        group["states"].append(row["estado"])
    priority = {"cumple": 0, "no verificable": 1, "pendiente de confirmar": 2, "no cumple": 3}
    for group in groups.values():
        group["status"] = max(group.pop("states"), key=priority.get)
        name, condition = group["name"], group["condition"]
        label = LABELS.get(name, name.replace("_", " "))
        group["estimated"] = name == "ingreso_mensual" and group["status"] in {"cumple", "no cumple"} and any(
            field.get("campo") == "abonos" for index in group["indices"] for field in rows[index].get("evidencia", []))
        if group["estimated"]:
            label += " estimado por los abonos"
        if name == "edad" and condition != "general":
            label = "la edad para " + condition.replace("pensionados IMSS", "pensionados del IMSS")
        elif condition != "general":
            label += " para " + condition
        group["label"] = label

    # En el resumen, condiciones con el mismo estado se citan una vez por concepto.
    visible = {status: [] for status in priority}
    for group in groups.values():
        siblings = [g for g in groups.values() if g["name"] == group["name"]]
        same_status = len({g["status"] for g in siblings}) == 1
        label = LABELS.get(group["name"], group["label"]) if same_status and group["name"] != "edad" else group["label"]
        if same_status and group["estimated"]:
            label += " estimado por los abonos"
        if label not in visible[group["status"]]:
            visible[group["status"]].append(label)
    sentences = []
    for status, prefix in (("cumple", "Con la evidencia disponible, cumples "), ("no cumple", "No cumples "),
                           ("no verificable", "No se pudo comprobar "), ("pendiente de confirmar", "Queda por confirmar ")):
        if visible[status]:
            sentences.append(prefix + listed(visible[status]) + ".")
    if not sentences:
        sentences.append("No hay requisitos evaluados suficientes para resumir el cumplimiento.")
    if derived.get("limitaciones") and not visible["no verificable"]:
        sentences.append("Otros requisitos quedan sin comprobar por falta de evidencia o reglas disponibles.")

    used = []
    for row in rows:
        for field in row.get("evidencia", []):
            source = field.get("fuente") or {}
            key = {"file_id": source.get("file_id"), "campo": field["campo"]}
            if source.get("tipo") == "aportado_por_usuario" and key not in used:
                used.append(key)
    if used:
        sentences.append("La revisión utiliza datos que confirmaste tú, sin verificación documental.")
    sentences.append(PRELIMINARY)
    from .document_content import coverage_notice
    for document in documents:
        sentences.extend(document.get("advertencias_lectura", []))
        pages = document.get("lectura", {}).get("paginas", [])
        notice = coverage_notice(pages, "application/pdf" if any(p.get("pagina") is not None for p in pages) else "image/png")
        if notice and notice not in sentences:
            sentences.append(notice)

    pending = []
    needed = {(e["file_id"], e["campo"]) for row in rows for e in row.get("confirmar", [])}
    for document in documents:
        effective = effective_document(document)
        for name, field in effective["campos"].items():
            if (document["file_id"], name) in needed:
                pending.append({"file_id": document["file_id"], "campo": name})

    # Cada razón compartida se muestra una vez, con referencias a sus requisitos.
    notes = {}
    for group in groups.values():
        for index in group["indices"]:
            for sentence in re.split(r"(?<=[.!?])\s+", rows[index].get("nota", "").strip()):
                if sentence:
                    labels = notes.setdefault(sentence, [])
                    if group["label"] not in labels:
                        labels.append(group["label"])
    return {"summary": " ".join(sentences), "requirements": list(groups.values()),
            "notes": [{"text": text, "requirements": labels} for text, labels in notes.items()],
            "pending_fields": pending, "used_corrections": used}


def preliminary_reason(name, evidence, missing):
    reason = " ".join(dict.fromkeys(row["nota"] for row in evidence if row.get("nota")))
    if name == "edad":
        reason = re.sub(r"Edad calculada al (\d{4}-\d{2}-\d{2}): ([^.]+)\.",
                        r"La fecha de nacimiento de la INE da una edad de \2 al \1.", reason)
        qualifiers = {">=": "al menos", "<=": "como máximo"}
        limits = [f"{qualifiers[row['operador']]} {row['umbral']} "
                  f"{'años' if row['regla'] == 'edad_anios' else 'meses'}"
                  for row in evidence if row.get("operador") in qualifiers and row.get("umbral") is not None]
        if limits:
            reason += " La política pide tener " + " y ".join(limits) + "."
    elif name == "vigencia_identificacion" and any(row["fuente_politica"] for row in evidence):
        if "Comparada al " in reason:
            reason = reason.replace("Vigencia leída: ", "La INE muestra la vigencia ", 1)
            reason = reason.replace("Comparada al ", "La política exige que la identificación esté vigente al ", 1)
        elif all(row["motivo"] != "regla_ausente" for row in evidence):
            reason += " La política pide una identificación oficial vigente."
    elif name == "monto_ingreso_mensual" and reason.startswith("Neto mensual de "):
        reason = reason.replace("Neto mensual de ", "La nómina indica un neto mensual de ", 1)
        reason = reason.replace(" frente al mínimo de ", ". El mínimo aplicable es ", 1)
    elif name == "aceptacion_ingreso_comprobable":
        for row in evidence:
            if row["motivo"] == "politica_no_aclara_concepto":
                concept = next((f["valor"] for f in row["evidencia"] if f["campo"] == "concepto_pago"), None)
                if concept:
                    return (f"La nómina registra el concepto «{concept}». La política pide ingresos comprobables "
                            "y no especifica si acepta ese concepto.")
    elif name == "antiguedad_comprobante_meses" and missing:
        limit = next((row["umbral"] for row in evidence if row.get("operador") == "<="
                      and row.get("umbral") is not None), None)
        reason = "No se aportó un comprobante."
        if limit is not None:
            reason += f" La política pide una antigüedad máxima de {limit} meses."
    return reason.strip()


def preliminary_summary(title, findings):
    confirmed, missing, unmet, pending = (list(dict.fromkeys(findings[state])) for state in (
        "cumple según los datos leídos", "falta documento", "no cumple", "no se puede verificar"))
    scope = f" para la {title}" if title else ""
    clauses = []
    if confirmed:
        clauses.append(f"La evidencia respalda {listed(confirmed)}{scope}.")
    if missing:
        clauses.append(("Falta " if len(missing) == 1 else "Faltan ") + listed(missing) + ".")
    if unmet:
        clauses.append("Aún necesitas cumplir " + listed(unmet) + ".")
    if pending:
        clauses.append("Queda por confirmar " + listed(pending) + ".")
    return " ".join(clauses) or "La evidencia disponible no permite completar la revisión."


def preliminary_view(documents, derived):
    from .attachment_presentation import confirmations, reference
    from .document_credit import PRODUCTS
    from .document_privacy import clean_visible_text, personal_values

    rows = derived["requisitos"]
    compact = credit_document_view(documents, derived)
    known = personal_values(documents)
    rows_visible, refs = [], []
    labels = {"edad": "Edad", "vigencia_identificacion": "Vigencia de la INE",
              "ingreso_mensual": "Ingreso mensual", "monto_ingreso_mensual": "Ingreso mensual · monto mínimo",
              "aceptacion_ingreso_comprobable": "Ingreso mensual · aceptación del concepto",
              "tipo_servicio_comprobante": "Domicilio · tipo de comprobante",
              "antiguedad_comprobante_meses": "Domicilio · antigüedad del comprobante"}
    summary_labels = {"edad": "el requisito de edad", "vigencia_identificacion": "la vigencia de la INE",
                      "aceptacion_ingreso_comprobable": "la acreditación del ingreso"}
    missing_documents = {"edad": "la INE", "vigencia_identificacion": "la INE",
                         "ingreso_mensual": "el comprobante de ingresos",
                         "monto_ingreso_mensual": "el comprobante de ingresos",
                         "antiguedad_comprobante_meses": "el comprobante de domicilio"}
    findings = {state: [] for state in ("cumple según los datos leídos", "falta documento",
                                      "no cumple", "no se puede verificar")}
    for group in compact["requirements"]:
        evidence = [rows[i] for i in group["indices"]]
        status = group["status"]
        if status == "no verificable" and not derived["lecturas_no_disponibles"] and all(
                row["motivo"] == "documento_ausente" for row in evidence):
            visible = "falta documento"
        else:
            visible = {"cumple": "cumple según los datos leídos", "no cumple": "no cumple",
                       "no verificable": "no se puede verificar", "pendiente de confirmar": "no se puede verificar"}[status]
        name = group["name"]
        summary_label = summary_labels.get(name, group["label"])
        if visible == "falta documento":
            summary_label = missing_documents.get(name, "la documentación de este requisito")
        findings[visible].append(summary_label)
        reason = preliminary_reason(group["name"], evidence, visible == "falta documento")
        cites = []
        for row in evidence:
            for field in row["evidencia"]:
                if field["campo"] not in {"nombre", "titular"} and field.get("fuente", {}).get("archivo"):
                    cites.append(reference(field["fuente"]))
            source = row["fuente_politica"]
            if source:
                cites.append(reference(source) + (f" · v. {source['version']}" if source.get("version") else "")
                             + (f" · consulta {source['consulted_at']}" if source.get("consulted_at") else ""))
        indices = []
        for citation in dict.fromkeys(cites):
            if citation not in refs:
                refs.append(citation)
            indices.append(refs.index(citation) + 1)
        label = labels[group["name"]]
        if group["name"] == "antiguedad_comprobante_meses" and visible == "falta documento":
            label = "Comprobante de domicilio"
        rows_visible.append({"label": label, "status": visible,
                             "reason": document_amounts(clean_visible_text(reason, known_values=known)), "sources": indices})
    title = PRODUCTS.get(derived.get("producto"))
    closing = "La revisión es preliminar. El banco valida los requisitos y evalúa la solicitud."
    groups = (
        ("Qué cumples", {"cumple según los datos leídos"}, "Ningún requisito acreditado con la evidencia disponible."),
        ("Qué falta", {"falta documento", "no cumple"}, "Ningún documento faltante ni condición incumplida en esta revisión."),
        ("Qué queda por confirmar", {"no se puede verificar"}, "Ningún requisito pendiente de confirmar."),
    )
    lines = [preliminary_summary(title, findings)]
    for heading, states, empty in groups:
        bullets = []
        for row in rows_visible:
            if row["status"] not in states:
                continue
            verdict = "No cumple. " if row["status"] == "no cumple" else ""
            citations = " [" + ", ".join(map(str, row["sources"])) + "]" if row["sources"] else ""
            bullets.append(f"- {row['label']} — {verdict}{row['reason']}{citations}")
        if bullets or rows_visible:
            lines.append(heading + "\n" + ("\n".join(bullets) if bullets else empty))
    if compact["used_corrections"]:
        lines.append("La revisión utilizó datos que confirmaste.")
    lines.append(closing)
    keys = {(e["file_id"], e["campo"]) for r in rows for e in r.get("confirmar", [])}
    pending = confirmations(documents, keys)
    for item in pending:
        if item["campo"] == "moneda":
            item["options"] = ["MXN", "USD", "EUR"]
    return "\n\n".join(lines), {"style": "preliminary_credit", "requirements": rows_visible, "confirmations": pending,
                              "references": refs, "closing": closing}
