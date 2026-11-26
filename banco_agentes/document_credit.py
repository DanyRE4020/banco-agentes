"""Comparaciones deterministas; no mezcla evidencia del usuario con política del RAG."""
import calendar
import re
import unicodedata
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from .documents import effective_document

PRODUCTS = {"nomina": "Crédito Nómina", "clasica": "Tarjeta de Crédito Clásica"}


def plain(text):
    return "".join(c for c in unicodedata.normalize("NFKD", text.lower()) if not unicodedata.combining(c))


def requested_product(question):
    text = plain(question)
    found = [product for product in PRODUCTS if re.search(r"\b" + product + r"\b", text)]
    if len(found) > 1 and re.search(r"(?:cfdi|recibo|comprobante).*nomina", text) and not re.search(r"credito (?:de )?nomina", text):
        found.remove("nomina")
    return found[0] if len(found) == 1 else None


def has_product_name(question):
    return bool(re.search(r"\b(nomina|clasica|personal|hipotecario|oro|platinum)\b", plain(question)))


def document_followup(question):
    text = plain(question).lstrip("¿¡ ")
    return bool(re.search(r"^(y\b|ahora\b|con\b)|identificacion|adjunto|documento|correg|vencid|vigent|reeval", text))


def receipt_reference(question):
    return bool(re.search(r"\b(?:recibo|comprobante|factura)\b", plain(question)))


def compliance_question(question):
    return bool(re.search(r"cumpl|requisit|puedo|calific|elegib|solicit|credito|tarjeta", plain(question)))


def inventory_question(question):
    text = plain(question)
    return bool(re.search(r"\b(?:que|cuales)\s+documentos?\b.{0,45}\b(?:tengo|subi|adjunte|aporte|faltan?)\b", text))


def preliminary_question(question):
    text = plain(question)
    if inventory_question(question):
        return False
    asks_compliance = re.search(r"\bcumpl(?:o|iria|imos|imiento)\b", text)
    asks_requirements = re.search(r"\brequisitos?\b", text)
    if asks_compliance and (asks_requirements or re.search(r"\bclasica\b", text)):
        return True
    asks_to_verify = re.search(r"\b(?:verific|evalu|acredit|valid|demostr|comprob|satisf)\w*", text)
    refers_to_evidence = re.search(
        r"\b(?:est[aeo]s?|mis|los dos)\s+(?:documentos?|adjuntos?|archivos?|pdfs?|ine|cfdi)\b"
        r"|\b(?:documentos?|archivos?|pdfs?)\s+(?:adjuntos?|aportados?|subidos?)\b"
        r"|\b(?:lo que|los que)\s+(?:subi|adjunte|aporte|cargue)\b",
        text,
    )
    return bool(asks_requirements and asks_to_verify and refers_to_evidence)


def client_document_intent(question, previous_product=None):
    """La revisión explícita de Cliente se limita a Clásica."""
    text = plain(question)
    classic = re.search(r"\bclasica\b", text)
    payroll = re.search(r"\b(?:credito (?:de )?|requisitos (?:de|para) |para )nomina\b", text)
    if (classic and payroll) or (not classic and re.search(r"\b(personal|hipotecario|oro|platinum)\b", text)):
        return None, None
    product = "clasica" if classic else "nomina" if payroll else None
    product = product or (previous_product if previous_product in PRODUCTS else None)
    if preliminary_question(question) and product == "clasica":
        return "preliminary", product
    if inventory_question(question):
        return "inventory", product
    return None, product


def receipt_elapsed(documents, *, today=None):
    """Calcula días a partir de la emisión o expedición confirmada en la lectura."""
    from .attachment_presentation import reference
    from .document_evidence import date_span, field_problem
    from .document_financial import proof_date
    from .document_privacy import FIELD_LABELS

    today = today or date.today()
    receipts = [d for d in documents if d["tipo"] in {"comprobante", "recibo_pago"}]
    if not receipts:
        return "Necesito un recibo legible con su fecha de emisión o expedición para calcular los días transcurridos."
    answers = []
    for receipt in receipts:
        names, preliminary = proof_date(receipt)
        name = names[0]
        field = receipt["campos"].get(name)
        source = reference((field or {}).get("fuente") or {"archivo": receipt["archivo"]})
        if preliminary or field_problem(field):
            answers.append(f"Hace falta una fecha de emisión o expedición legible para calcular la antigüedad ({source}).")
            continue
        value = field["valor"]
        issued, _, precision = date_span(value)
        label = FIELD_LABELS[name].capitalize()
        if precision != "dia":
            answers.append(f"{label} leída {value} ({source}). Hace falta el día para calcular los días transcurridos.")
        elif issued > today:
            answers.append(f"{label} leída {issued:%d/%m/%Y} ({source}). La fecha es posterior al {today:%d/%m/%Y}.")
        else:
            days = (today - issued).days
            unit = "día transcurrido" if days == 1 else "días transcurridos"
            answers.append(f"{label} usada {issued:%d/%m/%Y} ({source}). {days} {unit} al {today:%d/%m/%Y}.")
    return "\n".join(answers)


def compare_classic(documents, policy, *, errors=(), receipt_only=False, product="clasica"):
    """Aplica solo las reglas recuperadas para los requisitos de esta revisión."""
    from .document_evidence import is_ine
    required = ("antiguedad_comprobante_meses",) if receipt_only else (
        "edad_anios", "vigencia_identificacion", "ingreso_mensual", "antiguedad_comprobante_meses")
    def usable(rule):
        name = rule["campo"]
        if name == "vigencia_identificacion":
            return True
        try:
            limit = Decimal(str(rule.get("valor")))
        except InvalidOperation:
            return False
        if not limit.is_finite() or limit < 0:
            return False
        if name == "antiguedad_comprobante_meses":
            return rule.get("operador") == "<=" and limit == limit.to_integral_value()
        if name == "ingreso_mensual":
            return rule.get("operador") == ">=" and rule.get("moneda") in {"MXN", "USD", "EUR"}
        return rule.get("operador") in {">=", "<="}
    candidates = [r for r in (policy or {}).get("reglas", []) if r["campo"] in required]
    rules = [r for r in candidates if usable(r)]
    rules = [{**r, "valor": int(Decimal(str(r["valor"])))} if r["campo"] == "antiguedad_comprobante_meses" else r for r in rules]
    evidence = [d for d in documents if d["tipo"] != "identificacion" or is_ine(d)]
    result = compare_requirements(evidence, {**(policy or {}), "producto": product, "reglas": rules}, errors=errors)
    for row in result["requisitos"]:
        if row["motivo"] != "documento_ausente":
            continue
        if row["regla"] in {"edad_anios", "vigencia_identificacion"} and any(
                d["tipo"] == "identificacion" for d in documents):
            row.update(motivo="ine_no_acreditada", nota="No pude reconocer una INE entre las identificaciones leídas.")
        if row["regla"] == "antiguedad_comprobante_meses" and not errors:
            row["nota"] = "Falta el comprobante de domicilio."
    recovered = {r["campo"] for r in rules}
    for name in required:
        if name not in recovered:
            source = next((r["fuente"] for r in candidates if r["campo"] == name), {})
            note = ("La regla recuperada no especifica un límite evaluable o los datos necesarios para aplicarlo."
                    if source else "Steward no recuperó la regla aplicable para este requisito.")
            result["requisitos"].append({"regla": name, "estado": "no verificable", "motivo": "regla_ausente",
                "nota": note, "evidencia": [], "fuente_politica": source, "confirmar": []})
    order = ("edad_anios", "vigencia_identificacion", "ingreso_mensual", "aceptacion_ingreso_comprobable",
             "tipo_servicio_comprobante", "antiguedad_comprobante_meses")
    result["requisitos"].sort(key=lambda row: order.index(row["regla"]))
    return result


def compare_requirements(documents, policy, *, today=None, errors=()):
    today = today or date.today()
    from .document_evidence import date_span, months_before, field_problem, name_relation, tokens
    from .document_privacy import FIELD_LABELS
    effective = [effective_document(d) for d in documents]
    rows = []

    def evidence(doc, name):
        if not doc:
            return None
        f = doc["campos"].get(name)
        return {"documento_id": doc["id"], "campo": name, **f} if f else None

    def emit(rule, status, fields, note="", reason="evaluado", **extra):
        fields = [f for f in fields if f]
        titular = next((f for f in fields if f["campo"] == "titular"), None)
        nombre = next((f for f in fields if f["campo"] == "nombre"), None)
        links = ([{"documentos": [titular["documento_id"], nombre["documento_id"]], "campos": ["titular", "nombre"],
                   "estado": name_relation(titular["valor"], nombre["valor"]) if titular.get("valor") and nombre.get("valor") else "no_determinable"}]
                 if titular and nombre else [])
        rows.append({"regla": rule["campo"], "operador": rule.get("operador"), "umbral": rule.get("valor"),
                     "condicion": rule.get("condicion", "general"), "estado": status,
                     "evidencia": fields, "fuente_politica": rule["fuente"], "nota": note, "motivo": reason, "vinculos": links,
                     "confirmar": [{"file_id": f["fuente"].get("file_id"), "campo": f["campo"]} for f in fields
                        if status == "pendiente de confirmar" and (field_problem(f) not in {None, "ausente"}
                            or (reason == "precision_insuficiente" and f.get("precision") in {"mes", "anio"}))], **extra})

    def select(kinds, names, *, pool=None):
        candidates = [d for d in (effective if pool is None else pool) if d["tipo"] in kinds]
        def signature(doc):
            values = []
            for name in names:
                f = doc["campos"].get(name) or {}
                value = f.get("valor")
                values.append(tuple(sorted(tokens(value))) if name in {"nombre", "titular"} and value else value)
            return tuple(values)
        selected = min(candidates, key=lambda d: sum(field_problem(d["campos"].get(n)) is not None for n in names)) if candidates and len({signature(d) for d in candidates}) == 1 else None
        return selected, candidates

    identity, identities = select({"identificacion"}, ("nombre",))

    def need_document(rule, doc, candidates, names):
        if doc:
            return True
        other = [d for d in effective if d not in candidates and (d["campos"].get(names[0]) or {}).get("valor") is not None] if not candidates else []
        fields = [evidence(d, name) for d in candidates for name in names]
        note = "Hay varios documentos pertinentes con datos diferentes; no se elige uno arbitrariamente." if candidates else "Hay datos leídos, pero su tipo documental no acredita el documento requerido por esta regla." if other else "Falta un documento legible pertinente para este requisito."
        if not candidates and errors:
            note += " Hay adjuntos que no pudieron leerse; no se conoce si aportaban este dato."
        emit(rule, "no verificable", fields, note, "documentos_ambiguos" if candidates else "tipo_documental_no_acreditado" if other else "documento_ausente")
        if other:
            rows[-1]["documentos_no_pertinentes"] = [{"documento_id": d["id"], "tipo": d["tipo"],
                "campos_leidos": [n for n in names if (d["campos"].get(n) or {}).get("valor") is not None]} for d in other]
        return False

    def ready(rule, fields):
        problems = [(f, field_problem(f)) for f in fields if field_problem(f)]
        if not problems:
            return True
        notes = []
        for f, problem in problems:
            label = FIELD_LABELS.get(f["campo"], f["campo"].replace("_", " ")) if f else "dato requerido"
            prefix = {"ausente": "No consta", "ilegible": "Aparece, pero es ilegible", "no_interpretable": "Se leyó, pero no se puede interpretar",
                      "inconsistente": "Es inconsistente", "baja_confianza": "Se leyó con baja certeza"}.get(problem, "Requiere confirmación")
            notes.append(prefix + ": " + label + ".")
        missing = any(problem == "ausente" for _, problem in problems)
        emit(rule, "no verificable" if missing else "pendiente de confirmar", fields, " ".join(notes),
             "dato_ausente" if missing else problems[0][1])
        return False

    def associate(rule, doc, fields):
        if not identities:
            return True  # Comparación del dato del documento, sin afirmar identidad del solicitante.
        if not need_document(rule, identity, identities, ("nombre",)):
            return False
        names = [evidence(doc, "titular"), evidence(identity, "nombre")]
        if not ready(rule, fields + names):
            return False
        relation = name_relation(names[0]["valor"], names[1]["valor"])
        if relation != "coincidente":
            note = "Los titulares leídos son distintos; este requisito necesita vincular esos documentos." if relation == "distinto" else "Los nombres son parciales o insuficientes para vincular los documentos sin suponer identidad."
            emit(rule, "no verificable", fields+names, note, "titulares_distintos" if relation == "distinto" else "titular_no_determinable")
            return False
        fields.extend(names)
        return True

    def interval_status(low, high, op, threshold):
        if op == ">=":
            return "cumple" if low >= threshold else "no cumple" if high < threshold else "pendiente de confirmar"
        if op == "<=":
            return "cumple" if high <= threshold else "no cumple" if low > threshold else "pendiente de confirmar"
        return "no verificable"

    def payroll(rule):
        from .presentation import decimal_text
        names = ("neto_a_pagar", "periodicidad", "periodo_inicio", "periodo_fin", "moneda")
        selected, candidates = select({"cfdi_nomina"}, ("titular", *names, "concepto_pago"))
        if not need_document(rule, selected, candidates, names):
            rows[-1]["dimension"] = "monto"
            return
        acceptance = {"campo": "aceptacion_ingreso_comprobable", "fuente": rule["fuente"]}
        concept = evidence(selected, "concepto_pago")
        if ready(acceptance, [concept]):
            admitted = rule.get("conceptos_ingreso_admitidos")
            if admitted is None:
                emit(acceptance, "no verificable", [concept],
                     "La política recuperada no aclara si acepta el concepto «" + concept["valor"] + "» como ingreso comprobable.", "politica_no_aclara_concepto")
            else:
                allowed = {plain(c).strip() for c in admitted}
                emit(acceptance, "cumple" if plain(concept["valor"]).strip() in allowed else "no cumple", [concept],
                    "Se compara el concepto con la lista explícita recuperada; no autentica el CFDI ni confirma un depósito.", "concepto_politica")
        fields = [evidence(selected, n) for n in names]
        if not ready(rule, fields):
            rows[-1]["dimension"] = "monto"
            return
        if fields[1]["valor"] != "mensual":
            emit(rule, "no verificable", fields, "La periodicidad leída no es mensual; no se extrapolan pagos a un mes.", "periodicidad_no_mensual", dimension="monto")
            return
        start, end = date_span(fields[2]["valor"]), date_span(fields[3]["valor"])
        low, high = start[0], end[1]
        if not (start[2] in {"dia", "mes"} and end[2] in {"dia", "mes"}
                and high + timedelta(days=1) == months_before(low, -1) and high <= today):
            emit(rule, "no verificable", fields, "El periodo del CFDI no acredita un mes completo concluido; hacen falta sus límites completos.", "periodo_insuficiente", dimension="monto")
            return
        if fields[4]["valor"] != rule.get("moneda", "MXN"):
            emit(rule, "no verificable", fields, "La moneda del neto no coincide con la regla; no se convierte moneda.", "moneda_no_compatible", dimension="monto")
            return
        if not associate(rule, selected, fields):
            rows[-1]["dimension"] = "monto"
            return
        value, threshold = Decimal(fields[0]["valor"]), Decimal(str(rule["valor"]))
        currency = fields[4]["valor"]
        emit(rule, interval_status(value, value, rule.get("operador"), threshold), fields,
            f"Neto mensual de {decimal_text(value)} {currency} frente al mínimo de {decimal_text(threshold)} {currency}. El CFDI no confirma un depósito.", dimension="monto")

    def proof(rule):
        from .document_financial import proof_date, service_address, service_kind
        pool = [d for d in effective if d["tipo"] == "comprobante" or
                ((d["campos"].get("servicio_descripcion") or {}).get("valor") and d["tipo"] != "cfdi_nomina")]
        selected, candidates = select({d["tipo"] for d in pool}, ("titular", "domicilio", "domicilio_servicio", "tipo_comprobante",
            "fecha_emision", "fecha_expedicion", "mes_facturado", "periodo_inicio", "periodo_fin"), pool=pool)
        type_rule = {"campo": "tipo_servicio_comprobante", "fuente": rule["fuente"]}
        if not need_document(rule, selected, candidates, ("fecha_emision", "domicilio", "tipo_comprobante")):
            return
        address = evidence(selected, service_address(selected))
        names, preliminary = proof_date(selected)
        fields = [evidence(selected, n) for n in names]
        conditional_evidence = []
        condition = rule.get("condicion", "general")
        if condition not in {None, "general"}:
            if not rule.get("comparar_domicilio_identificacion"):
                emit(rule, "no verificable", [], "La condición del comprobante no tiene una comparación estructurada disponible.", "condicion_no_evaluable")
                return
            identified, candidates = select({"identificacion"}, ("nombre", "domicilio"))
            if not need_document(rule, identified, candidates, ("domicilio",)):
                return
            conditional = [address, evidence(identified, "domicilio")]
            if not ready(rule, conditional):
                return
            if tokens(conditional[0]["valor"]) == tokens(conditional[1]["valor"]):
                if associate(rule, selected, conditional):
                    emit(rule, "cumple", conditional, "Los domicilios leídos coinciden; la regla no exige un comprobante adicional en este caso.", "no_aplica")
                return
            conditional_evidence = conditional[1:]
        kind_field = evidence(selected, "tipo_comprobante")
        if ready(type_rule, [kind_field]):
            kind = service_kind(kind_field["valor"])
            allowed = rule.get("tipos_comprobante_admitidos")
            if kind in {"otro", "no_determinable", "paquete"} and not (selected["campos"].get("servicio_descripcion") or {}).get("valor"):
                emit(type_rule, "no verificable", [kind_field], "El paquete o documento no identifica el servicio; hace falta el tipo de suministro o servicio facturado.", "servicio_no_identificado")
            elif kind == "no_determinable":
                emit(type_rule, "no verificable", [kind_field], "El paquete no identifica qué servicio contiene; falta ese dato.", "servicio_no_identificado")
            elif allowed is None:
                emit(type_rule, "no verificable", [kind_field], "La política recuperada no estructura los tipos de comprobante admitidos.", "regla_incompleta")
            else:
                emit(type_rule, "cumple" if kind in {service_kind(k) for k in allowed} else "no cumple", [kind_field],
                    "El servicio leído " + ("está" if kind in {service_kind(k) for k in allowed} else "no está") + " entre los admitidos por la política recuperada.", "tipo_admitido" if kind in {service_kind(k) for k in allowed} else "tipo_no_admitido")
        if not ready(rule, fields + [address] + conditional_evidence):
            if names == ["fecha_emision"] and field_problem(fields[0]) == "ausente":
                rows[-1]["nota"] += " Hace falta una fecha de expedición o un periodo facturado. El límite de pago y el corte tienen otro significado."
            return
        fields.extend([address] + conditional_evidence)
        if not associate(rule, selected, fields):
            return
        if rule.get("operador") != "<=":
            emit(rule, "no verificable", fields, "El operador de antigüedad no está soportado.", "regla_no_evaluable")
            return
        low, high = date_span(fields[0]["valor"])[0], date_span(fields[len(names)-1]["valor"])[1]
        cutoff = months_before(today, rule["valor"])
        if low > today:
            emit(rule, "no verificable", fields, "La fecha de referencia leída es futura; no permite acreditar antigüedad.", "fecha_inconsistente")
            return
        status = "no cumple" if high < cutoff else "cumple" if low >= cutoff and high <= today else "pendiente de confirmar"
        label = fields[0].get("etiqueta") or FIELD_LABELS.get(names[0], names[0])
        original = fields[0]["valor"] if fields[0]["fuente"].get("tipo") == "aportado_por_usuario" else fields[0].get("valor_original") or fields[0]["valor"]
        dates_used = [f"{label} ({original})"]
        if len(names) == 2:
            last = fields[1]
            last_label = last.get("etiqueta") or FIELD_LABELS[names[1]]
            last_value = last["valor"] if last["fuente"].get("tipo") == "aportado_por_usuario" else last.get("valor_original") or last["valor"]
            dates_used.append(f"{last_label} ({last_value})")
        note = ("Referencia preliminar con " if preliminary else "Se usa ") + " y ".join(dates_used)
        note += f". Límite de {rule['valor']} meses al {today.isoformat()}."
        if status == "pendiente de confirmar":
            note += " Hace falta una fecha más precisa para resolver el límite."
        emit(rule, status, fields, note, "precision_insuficiente" if status == "pendiente de confirmar" else "evaluado", referencia_preliminar=preliminary)

    for rule in policy["reglas"]:
        field = rule["campo"]
        condition = rule.get("condicion", "general")
        if field in {"edad_anios", "edad_meses", "ingreso_mensual"} and condition not in {None, "general"}:
            emit(rule, "no verificable", [], "No se acredita la condición de la regla: " + condition + ".", "condicion_no_acreditada")
            continue
        if field == "ingreso_mensual" and any(d["tipo"] == "cfdi_nomina" for d in effective):
            payroll(rule)
            continue
        if field == "antiguedad_comprobante_meses":
            proof(rule)
            continue
        if field in {"edad_anios", "edad_meses"}:
            selected, candidates = select({"identificacion"}, ("nombre", "fecha_nacimiento"))
            if not need_document(rule, selected, candidates, ("fecha_nacimiento",)):
                continue
            fields = [evidence(selected, "fecha_nacimiento")]
        elif field in {"vigencia_identificacion", "identificacion_oficial"}:
            name = "fecha_vencimiento" if field == "vigencia_identificacion" else "tipo_identificacion"
            selected, candidates = select({"identificacion"}, ("nombre", name))
            if not need_document(rule, selected, candidates, (name,)):
                continue
            fields = [evidence(selected, name)]
        elif field == "ingreso_mensual":
            statements = [d for d in effective if not (d["campos"].get("servicio_descripcion") or {}).get("valor")
                          and not (d["campos"].get("tipo_comprobante") or {}).get("valor")]
            selected, candidates = select({"estado_de_cuenta"}, ("titular", "abonos", "periodo_inicio", "periodo_fin", "moneda"), pool=statements)
            if not need_document(rule, selected, candidates, ("abonos", "periodo_inicio", "periodo_fin", "moneda")):
                continue
            fields = [evidence(selected, n) for n in ("abonos", "periodo_inicio", "periodo_fin", "moneda")]
        else:
            emit(rule, "no verificable", [], "No hay evidencia suficiente ni evaluador para este requisito.", "sin_evaluador")
            continue
        if not ready(rule, fields):
            continue
        if field in {"edad_anios", "edad_meses"}:
            start, end, _ = date_span(fields[0]["valor"])
            if end > today:
                emit(rule, "no verificable", fields, "La fecha de nacimiento leída no permite calcular una edad al día de hoy.", "fecha_inconsistente")
                continue
            def age(birth):
                months = (today.year-birth.year)*12 + today.month-birth.month - (today.day < birth.day)
                return months if field == "edad_meses" else months//12
            low, high = age(end), age(start)
            status = interval_status(low, high, rule.get("operador"), Decimal(str(rule["valor"])))
            value = str(low) if low == high else f"entre {low} y {high}"
            unit = "años" if field == "edad_anios" else "meses"
            note = f"Edad calculada al {today.isoformat()}: {value} {unit}."
            if status == "pendiente de confirmar":
                note += " La fecha parcial no permite resolver este límite."
            emit(rule, status, fields, note, "precision_insuficiente" if status == "pendiente de confirmar" else "evaluado")
        elif field == "vigencia_identificacion":
            start, end, _ = date_span(fields[0]["valor"])
            status = "cumple" if start >= today else "no cumple" if end < today else "pendiente de confirmar"
            expiry = fields[0]
            printed = expiry["valor"] if expiry["fuente"].get("tipo") == "aportado_por_usuario" else expiry.get("valor_original") or expiry["valor"]
            note = f"Vigencia leída: {printed}. Comparada al {today.isoformat()}."
            if status == "pendiente de confirmar":
                note += " La precisión impresa no permite decidir si sigue vigente."
            emit(rule, status, fields, note,
                 "precision_insuficiente" if status == "pendiente de confirmar" else "evaluado")
        elif field == "identificacion_oficial":
            kind = plain(fields[0]["valor"]).replace(" ", "_")
            status = "cumple" if kind == "oficial" else "no cumple" if kind == "no_oficial" else "no verificable"
            emit(rule, status, fields, "Se compara el tipo de identificación declarado por el documento; no se verifica su autenticidad.", "tipo_documental")
        elif field == "ingreso_mensual":
            start, end = (date_span(f["valor"]) for f in fields[1:3])
            if not (start[2] == end[2] == "dia" and start[0].day == 1 and start[0].year == end[0].year
                    and start[0].month == end[0].month and end[0].day == calendar.monthrange(end[0].year, end[0].month)[1] and end[0] <= today):
                emit(rule, "no verificable", fields, "El periodo leído no acredita un único mes calendario completo ya concluido.", "periodo_insuficiente")
                continue
            if fields[3]["valor"] != rule.get("moneda", "MXN"):
                emit(rule, "no verificable", fields, "La moneda leída no coincide con la regla; no se convierte moneda.", "moneda_no_compatible")
                continue
            if not associate(rule, selected, fields):
                continue
            value = Decimal(fields[0]["valor"])
            emit(rule, interval_status(value, value, rule.get("operador"), Decimal(str(rule["valor"]))), fields,
                 "Abonos del mes completo usados únicamente como estimación de ingreso; no acreditan ingreso comprobable ni origen salarial."
                 + (" No se cotejó titularidad con una identificación." if not identities else " La coincidencia nominal no autentica identidad."))
    return {"tipo": "derivado", "producto": policy["producto"], "fecha_servidor": today.isoformat(),
            "requisitos": rows, "limitaciones": policy.get("faltantes", []),
            "lecturas_no_disponibles": [{"file_id": e["file_id"], "tipo": e.get("tipo", "lectura")} for e in errors],
            "aviso": "Referencia preliminar; no es una aprobación. La aprobación corresponde al banco. Las correcciones son datos aportados por el usuario, sin verificación documental."}
