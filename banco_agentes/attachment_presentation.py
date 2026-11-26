"""Vista breve para ambos perfiles, calculada desde la evidencia autorizada del backend."""
from .documents import effective_document
from .document_privacy import FIELD_LABELS, requested_fields, personal_values, clean_visible_text
from .document_evidence import field_problem


def reference(source):
    label = source.get("archivo") or source.get("title") or source.get("file") or "Archivo adjunto"
    page = source.get("pagina") or source.get("page")
    kind = {"resumen_academico": " · resumen académico", "ejemplo_ilustrativo": " · ejemplo ilustrativo"}.get(source.get("source_kind"), "")
    provenance = " · dato aportado por el usuario" if source.get("tipo") == "aportado_por_usuario" else ""
    return clean_visible_text(label + (f", página {page}" if page else "") + kind + provenance)


def confirmations(documents, keys):
    result = []
    for doc in documents:
        for name, field in effective_document(doc)["campos"].items():
            if (doc["file_id"], name) in keys and (field["estado"] == "por_confirmar" or field.get("precision") in {"mes", "anio"}):
                result.append({"file_id": doc["file_id"], "campo": name,
                    "label": FIELD_LABELS.get(name, name.replace("_", " ")), "reference": reference(field["fuente"])})
    return result


def direct_fields(question, documents):
    wanted = requested_fields(question)
    lines, keys = [], set()
    for doc in documents:
        for name, field in effective_document(doc)["campos"].items():
            if name not in wanted:
                continue
            label = FIELD_LABELS[name].capitalize()
            problem = field_problem(field)
            if problem == "ausente":
                lines.append(f"No consta {FIELD_LABELS[name]} en el archivo ({reference(field['fuente'])}).")
            elif problem:
                keys.add((doc["file_id"], name))
                detail = "es ilegible" if problem == "ilegible" else "no se pudo interpretar" if problem == "no_interpretable" else "requiere revisión"
                lines.append(f"{label}: {detail}. Necesito que lo confirmes para responder.")
            else:
                original = field.get("valor_original") if field["fuente"].get("tipo") != "aportado_por_usuario" else None
                value = clean_visible_text(original or field["valor"], allow_fields=wanted)
                if field.get("precision") in {"mes", "anio"}:
                    value += " (precisión de " + ("mes" if field["precision"] == "mes" else "año") + ")"
                lines.append(f"{label}: {value} ({reference(field['fuente'])}).")
    return "\n".join(lines), confirmations(documents, keys)


def attachment_view(question, documents, decision, derived=None, credit_view=None):
    known = personal_values(documents)
    view = {"requirements": [], "confirmations": [], "references": []}
    if derived:
        rows = derived["requisitos"]
        keys = {(e["file_id"], e["campo"]) for row in rows for e in row.get("confirmar", [])}
        view["confirmations"] = confirmations(documents, keys)
        for group in credit_view["requirements"]:
            notes = list(dict.fromkeys(rows[i]["nota"] for i in group["indices"] if rows[i].get("nota")))
            reason = " ".join(notes)
            if not reason:
                reason = {"cumple": "La evidencia satisface la regla consultada.",
                    "no cumple": "La evidencia no satisface el límite de la regla consultada.",
                    "no verificable": "No hay evidencia suficiente para comprobarlo.",
                    "pendiente de confirmar": "Hace falta confirmar un dato utilizado en este requisito."}[group["status"]]
            units = {"edad_meses": " meses", "edad_anios": " años"}
            thresholds = [f"{rows[i]['operador']} {rows[i]['umbral']}{units.get(rows[i]['regla'], '')}"
                          for i in group["indices"] if rows[i].get("umbral") is not None]
            if thresholds and group["status"] in {"cumple", "no cumple"}:
                reason += " Límites de la regla: " + ", ".join(thresholds) + "."
            view["requirements"].append({"label": group["label"].capitalize(), "status": group["status"],
                "reason": clean_visible_text(reason, known_values=known)})
            for index in group["indices"]:
                source = rows[index]["fuente_politica"]
                policy_reference = reference(source)
                if source.get("version"):
                    policy_reference += " · v. " + source["version"]
                if source.get("consulted_at"):
                    policy_reference += " · consulta " + source["consulted_at"]
                if policy_reference not in view["references"]:
                    view["references"].append(policy_reference)
                for e in rows[index]["evidencia"]:
                    if e.get("valor") is not None:
                        citation = reference(e["fuente"])
                        if citation not in view["references"]:
                            view["references"].append(citation)
    elif decision and decision.alcance == "bancario" and decision.accion == "responder":
        _, view["confirmations"] = direct_fields(question, documents)
    return view
