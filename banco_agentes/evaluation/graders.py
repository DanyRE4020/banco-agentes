"""Seis criterios independientes; reglas conservadoras, sin juez LLM ni referencias en el chat.

Un 1 significa correcto salvo alucinacion (1 = alucinación detectada).
La prosa que estas reglas no pueden demostrar queda no_verificable, nunca aprobada por defecto.
"""
import json
import re
import unicodedata
from decimal import Decimal, InvalidOperation

from .data import CRITERIA
from .reference import SPECS

MONTHS = ("", "enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre")
MONEY = re.compile(r"(?<![\w/-])-?(?:\d{1,3}(?:\.\d{3})+,\d+|\d+,\d+|\d+\.\d+)(?!\w)")
TOOLS = {"data_analytics": {"consultar_datos", "comparar_mes_anterior", "consultar_reglas"},
         "data_graph": {"preparar_grafica"}, "data_quality": {"revisar_calidad"},
         "data_steward": {"buscar_catalogo", "buscar_documentacion", "consultar_reglas"},
         "data_governance": {"explicar_politica_sesion"}}


def norm(value):
    return "".join(c for c in unicodedata.normalize("NFD", str(value).lower()) if not unicodedata.combining(c))


def number(value):
    text = str(value).replace(" ", "").rstrip("%")
    if "," in text:
        text = text.replace(".", "").replace(",", ".")
    result = Decimal(text)
    if not result.is_finite():
        raise InvalidOperation()
    return result


def near(a, b, tolerance="0.005"):
    try:
        return abs(number(a) - number(b)) <= Decimal(tolerance)
    except (InvalidOperation, ValueError, TypeError):
        return False


def verdict(state, reason, score=None, **details):
    return dict(status=state, score=score, reason=reason, details=details)


def checked(ok, reason, **details):
    return verdict("calificado", reason, int(bool(ok)), **details)


def nv(reason):
    return verdict("no_verificable", reason)


def view(output):
    return ((output.get("final") or {}).get("response") or {})


def text(output):
    return view(output).get("answer", "")


def plans(output):
    ids = set((view(output).get("presentation") or {}).get("result_ids", []))
    return [e for e in output.get("trace") or [] if e.get("event") == "validated_plan" and e.get("status") == "ok"
            and e.get("sql_executed") and (not ids or e.get("id") in ids)]


def month_in(value):
    raw = str(value)
    match = re.search(r"2026[-/]?(0[1-9]|1[0-2])(?:\b|[-/])", raw)
    if match:
        return 202600 + int(match[1])
    lower = norm(raw)
    for i, name in enumerate(MONTHS[1:], 1):
        if re.search(r"\b" + name + r"\b", lower) and "2026" in lower:
            return 202600 + i
    return None


def visible_cells(output, spec):
    """Campos mostrados, sin Detalles de la consulta ni valores de turnos previos."""
    cells = []
    for chart in view(output).get("charts", []):
        for point in chart.get("points", []):
            label = str(point.get("label", ""))
            product = next((p for p in spec.products if p in label), spec.products[0] if len(spec.products) == 1 else None)
            month = month_in(label) or month_in(chart.get("period", ""))
            cells.append(dict(product=product, month=month, metric="total", value=point.get("y")))
    aliases = {"total": "total", "saldo total": "total", "mediana": "mediana", "desviacion": "desviacion",
               "completitud (null)": "completitud", "completitud": "completitud", "duplicados": "duplicados"}
    for table in view(output).get("tables", []):
        for raw in table.get("rows", []):
            row = {norm(k): v for k, v in raw.items()}
            product = row.get("producto") or (spec.products[0] if len(spec.products) == 1 else None)
            month = month_in(row.get("mes", row.get("periodo", table.get("title", ""))))
            metric = aliases.get(norm(row.get("control", "total")), "unknown")
            if "valor" in row:
                cells.append(dict(product=product, month=month, metric=metric, value=row["valor"], segment=row.get("segmento")))
            for field, metric in (("nulos", "completitud"), ("grupos_duplicados", "duplicados"), ("mediana", "mediana"), ("desviacion", "desviacion")):
                if field in row and row[field] != "No aplica":
                    cells.append(dict(product=product, month=month, metric=metric, value=row[field], segment=row.get("segmento")))
    return cells


def same_scope(cell, fact):
    return all(cell.get(k) == fact.get(k) for k in ("product", "month", "metric", "segment"))


def trace_cells(output):
    cells = []
    for event in plans(output):
        plan = event["plan"]
        metric = plan.get("base", "total") if plan["metrica"] == "variacion" else plan["metrica"]
        field = {"completitud": "nulos", "duplicados": "grupos_duplicados"}.get(metric, "valor")
        for row in event["rows"]:
            cells.append(dict(product=plan.get("producto") or row.get("producto"),
                              month=row.get("mes") or (month_in(plan.get("desde", "")) if plan.get("desde") == plan.get("hasta") else None),
                              metric=metric, value=row.get(field), segment=row.get("segmento"), person=plan.get("persona", "")))
    return cells


def clauses(answer):
    return re.split(r"\n+|(?<=[.!?])\s+(?=[A-ZÁÉÍÓÚ¿])", answer)


def visible_fact(output, spec, fact, numeric=True):
    matching = [c for c in visible_cells(output, spec) if same_scope(c, fact)]
    if matching:
        return any(c["value"] is not None and (not numeric or near(c["value"], fact["value"])) for c in matching)
    # Un total puede presentarse como frase, pero debe estar ligado al producto y al período.
    for clause in clauses(text(output)):
        if fact["product"] not in clause or month_in(clause) != fact["month"]:
            continue
        if not numeric:
            return bool(MONEY.search(clause))
        if any(near(m[0], fact["value"]) for m in MONEY.finditer(clause)):
            return True
    return False


def has_amount(answer, value, tolerance="0.005"):
    return any(near(m[0], value, tolerance) for m in MONEY.finditer(answer))


def growth_method(answer):
    lower = norm(answer)
    if re.search(r"porcent|relativ|%", lower):
        return "percent"
    if re.search(r"absolut|diferencia|incremento en importe|crecimiento en importe", lower):
        return "absolute"
    return None


def growth_claim(answer, product, derived, numeric):
    return any(product in c and re.search(r"crec|aument|variacion|increment|diferencia", norm(c))
               and (not numeric or any(v is not None and has_amount(c, v, "0.00005" if key == "percent" else "0.005")
                                       for key, v in derived.items())) for c in clauses(answer))


def source_check(output):
    sources = view(output).get("sources", [])
    if not sources:
        return False
    evidence = []
    for event in output.get("trace") or []:
        if event.get("event") == "validated_plan":
            evidence.extend([event.get("table", ""), *event.get("sources", [])])
        elif event.get("event") in {"retrieval_result", "catalog_evidence", "semantic_evidence"}:
            evidence.append(json.dumps(event, ensure_ascii=False))
    for source in sources:
        target = source.get("url") or source.get("location", "").split(" · ")[0]
        if not target or not any(target in entry for entry in evidence):
            return False
        location = source.get("location", "")
        if " · líneas " in location:
            interval = location.split(" · líneas ", 1)[1]
            requested = re.fullmatch(r"(\d+)(?:-(\d+))?", interval)
            if not requested:
                return False
            start, end = int(requested[1]), int(requested[2] or requested[1])
            intervals = [re.fullmatch(re.escape(target) + r":(\d+)(?:-(\d+))?", e) for e in evidence]
            if not any(m and int(m[1]) <= start <= end <= int(m[2] or m[1]) for m in intervals):
                return False
    return True


def routing(case, output):
    expected = case.row["enrutamiento_esperado"]
    if not expected:
        return nv("El CSV no fija el enrutamiento esperado para este caso.")
    events = output["trace"]
    agents = {e["agent"] for e in events if e.get("event") == "agent_started"}
    started = [e for e in events if e.get("event") == "tool_started"]
    completed = [e for e in events if e.get("event") == "tool_completed"]
    if "orquestador" not in agents:
        return nv("No hay evidencia del inicio del hub.")
    wanted = set() if expected == "solo_orquestador" else set(expected.split("|"))
    if agents - {"orquestador"} != wanted:
        return checked(False, "Los especialistas invocados no coinciden con el CSV.", expected=sorted(wanted), actual=sorted(agents))
    if not wanted:
        return checked(not started, "Fuera de alcance/aclaración: solo hub, sin herramientas.")
    for agent in wanted:
        if not any(e.get("agent") == agent and e.get("tool") in TOOLS.get(agent, set()) for e in completed):
            return nv("Falta evidencia de una herramienta completada del especialista esperado.")
    return checked(True, "Especialistas y herramientas reales del último turno coinciden con el CSV.")


def numeric(case, output):
    if not case.row["exactitud_numerica"] or case.id == "BA-015":
        return verdict("no_aplica", "La rúbrica no requiere cifras, o primero debe aclararse el corte (BA-015).")
    ref, spec = output.get("sql_reference", {}), SPECS.get(case.id)
    if not spec or ref.get("status") != "ok":
        return nv("No hay SQL independiente completo para calificar importes.")
    facts = ref["facts"]
    if spec.style == "growth":
        facts = [f for f in facts if f["month"] in {202602, 202606}]
        if {(f["product"], f["month"]) for f in facts} != {(p, m) for p in spec.products for m in (202602, 202606)}:
            return nv("El oráculo no cubre las cuatro bases de crecimiento del caso.")
    observed = trace_cells(output)
    missing = [f for f in facts if not any(same_scope(c, f) and c.get("person", "") == spec.person
                                           and c["value"] is not None and near(c["value"], f["value"]) for c in observed)]
    if missing:
        return checked(False, "Faltan bases verificadas del mismo turno o no coinciden con SQL.", missing=missing)
    if spec.style == "growth":
        june = next(f for f in facts if f["product"] == "AHORRO" and f["month"] == 202606)
        ok = visible_fact(output, spec, june) and all(growth_claim(text(output), p, ref["derived"][p], True) for p in spec.products)
    else:
        ok = all(visible_fact(output, spec, f) for f in facts)
    if spec.style == "pair":
        ok = ok and has_amount(text(output), ref["derived"]["difference"])
    if spec.style == "false_premise":
        ok = ok and growth_claim(text(output), "TDC", ref["derived"]["TDC"], True)
    supported = reference_numbers(ref)
    if any(not any(near(m[0], v) for v in supported) for m in MONEY.finditer(text(output))):
        ok = False
    # No basta una gráfica correcta si la conclusión asigna el importe de un producto a otro.
    for clause in clauses(text(output)):
        products = [p for p in spec.products if p in clause]
        if len(products) != 1 or re.search(r"crec|aument|variacion|increment", norm(clause)):
            continue
        month = month_in(clause)
        candidates = [f for f in ref["facts"] if f["product"] == products[0] and f["month"] == month and f["metric"] == "total"]
        match = MONEY.search(clause.split(products[0], 1)[1])
        if candidates and match and not near(match[0], candidates[0]["value"]):
            ok = False
    return checked(ok, "Cifras visibles y bases del turno contrastadas con SELECT independientes; tolerancia absoluta 0,005 para importes y 0,00005 para porcentajes.")


def coverage(case, output):
    spec, answer, data = SPECS.get(case.id), text(output), view(output)
    lower = norm(answer)
    if case.id == "BA-006":
        ok = bool(re.fullmatch(r"(?:lo siento[,.:]?\s*)?(?:solo|unicamente) (?:puedo )?(?:atender|atiendo|responder|respondo|ayudar con)(?: preguntas| consultas| temas)? (?:relacionad[oa]s? con el banco|del banco|bancarios)(?:, sus productos, datos y documentos)?\.?", lower.strip()))
        if not ok and re.search(r"\d.*(?:°|grados)|esta (?:soleado|lloviendo|nublado)|temperatura es", lower):
            return checked(False, "Desarrolla contenido meteorológico en lugar de una negativa de alcance.")
        return checked(ok, "Reconoce brevemente el alcance bancario sin desarrollar el clima; no basta mencionar la palabra banco.") if ok else nv("La negativa no coincide con las formas verificables; revisar la respuesta visible.")
    if spec:
        cells = visible_cells(output, spec)
        if spec.style == "pair":
            charts = data.get("charts", [])
            ok = len(charts) == 1 and charts[0].get("type") == "barras" and len(charts[0].get("points", [])) == 2
            ok = ok and {c["product"] for c in cells} == set(spec.products) and "mayor" in lower and "diferencia" in lower
            return checked(ok, "Dos productos en una sola gráfica, conclusión de mayor y diferencia visibles.")
        if spec.style == "line":
            charts = data.get("charts", [])
            ok = len(charts) == 1 and charts[0].get("type") == "linea" and len(charts[0].get("points", [])) == 6
            ok = ok and {c["month"] for c in cells} == {202600 + m for m in spec.months}
            ok = ok and bool(re.search(r"mayor|maxim", lower))
            return checked(ok, "Serie de seis meses y mes de máximo explícito; una tendencia sola no satisface el máximo.")
        if spec.style == "growth":
            ref = output.get("sql_reference", {})
            june = dict(product="AHORRO", month=202606, metric="total", value="0")
            growth = all(growth_claim(answer, p, {}, False) for p in spec.products)
            ok = visible_fact(output, spec, june, False) and growth and growth_method(answer) is not None
            ok = ok and bool(re.search(r"crecio mas|mayor crecimiento|crecimiento mayor", lower))
            return checked(ok, "Saldo de AHORRO, crecimiento de ambos productos, período común, definición de crecimiento y comparación explícita; comparar solo cierres no satisface la pregunta.")
        if spec.style in {"quality", "duplicates", "statistics"}:
            expected = {(p, 202600 + m, metric) for p in spec.products for m in spec.months
                        for metric in (("completitud", "duplicados") if spec.style == "quality" else ("duplicados",) if spec.style == "duplicates" else ("mediana", "desviacion"))}
            actual = {(c["product"], c["month"], c["metric"]) for c in cells}
            ok = expected <= actual
            if spec.style in {"quality", "duplicates"}:
                ok = ok and all(re.search(term, lower) for term in (r"cuenta|num_cta", r"producto|cve_producto", r"corte|fec_informacion"))
            return checked(ok, "Controles/métricas solicitados por corte y regla de duplicidad por cuenta, producto y corte cuando aplica.")
        if spec.style == "false_premise":
            return checked(bool(re.search(r"aument|no (?:cayo|disminuyo)|crecio", lower)) and "febrero" in lower and "marzo" in lower,
                           "Corrige la premisa y compara febrero con marzo.")
        return checked(bool(answer) and bool(MONEY.search(answer) or cells), "Da un resultado visible para el cálculo solicitado.")
    if case.id in {"BA-009", "BA-015", "BA-019"}:
        return checked("?" in answer and bool(re.search(r"que|cual|indica|especific|aclar", lower)) and not MONEY.search(answer), "Pide una aclaración sin inventar un cálculo.")
    if case.id in {"BA-005", "BA-010", "BA-014"}:
        return checked(bool(re.search(r"no (?:hay|esta|estan|se dispone)|fuera|invalido|disponible|cobertura", lower)), "Explica el límite de fecha/entrada en la respuesta visible.")
    return nv("La cobertura conceptual de esta rúbrica requiere revisión semántica; no se aprueba por palabras clave.")


def dates(case, output):
    if not case.row["manejo_de_fechas"]:
        return verdict("no_aplica", "No hay criterio de fechas en el CSV.")
    spec, lower = SPECS.get(case.id), norm(text(output))
    if spec:
        selected = plans(output)
        if not selected:
            return nv("Faltan planes del mismo turno para verificar el alcance temporal.")
        allowed = {202600 + m for m in spec.months}
        actual = set()
        for event in selected:
            plan = event["plan"]
            start, end = month_in(plan.get("desde", "")), month_in(plan.get("hasta", ""))
            if start is None or end is None or start > end:
                return checked(False, "Período inválido o ausente en el plan ejecutado.")
            actual.update(range(start, end + 1))
        # Una revisión febrero-junio puede consultar endpoints o toda la serie.
        ok = actual <= allowed and min(actual) == min(allowed) and max(actual) == max(allowed)
        visible_months = {c["month"] for c in visible_cells(output, spec) if c["month"] is not None}
        if visible_months and not visible_months <= allowed:
            ok = False
        return checked(ok, "Período del último turno frente al criterio del CSV; los turnos previos no se califican como si fueran el final.")
    if case.id in {"BA-015", "BA-019"}:
        return checked("?" in text(output) and not plans(output), "Sin corte inequívoco se pide aclaración antes de consultar.")
    if case.id in {"BA-005", "BA-010"}:
        requested = "2025" if case.id == "BA-005" else "octubre"
        ok = requested in lower and "2026" in lower and "junio" in lower and not plans(output)
        return checked(ok, "Respeta 2025 fuera de cobertura o octubre de 2026 según la fecha de referencia fija del CSV (12/11/2026); no sustituye el corte.")
    if case.id == "BA-014":
        return checked("2026-13" in lower and "2027" in lower and bool(re.search(r"inval|fuera|no disponible", lower)), "Señala mes inválido y año fuera de cobertura.")
    return nv("No hay una comprobación temporal formal para esta rúbrica.")


def correctness(case, output, scores):
    if SPECS.get(case.id):
        checks = [scores[k] for k in ("responde_la_pregunta", "exactitud_numerica", "manejo_de_fechas")]
        if any(c["score"] == 0 for c in checks):
            return checked(False, "La respuesta incumple contenido, cifras o período de la referencia.")
        if any(c["status"] == "no_verificable" for c in checks):
            return nv("Faltan comprobaciones para demostrar corrección completa.")
        if not source_check(output):
            return checked(False, "Faltan fuentes visibles sustentadas por la evidencia del mismo turno.")
        if re.search(r"porque|debido a|causad|provocad|se espera|pronostic|elegibil|aprobacion|inflacion|mercado", norm(text(output))):
            return nv("Hay afirmaciones causales, predictivas o de elegibilidad que estas reglas no pueden demostrar.")
        ref, spec = output["sql_reference"], SPECS[case.id]
        if spec.style == "pair":
            winner = ref["derived"]["winner"]
            if not any(winner in c and "mayor" in norm(c) for c in clauses(text(output))):
                return checked(False, "La conclusión no identifica el ganador calculado por SQL.")
        if spec.style == "growth":
            method = growth_method(text(output))
            winner = max(spec.products, key=lambda p: Decimal(ref["derived"][p][method]))
            if not any(winner in c and re.search(r"crecio mas|mayor crecimiento|crecimiento mayor", norm(c)) for c in clauses(text(output))):
                return checked(False, "No identifica quién creció más según la definición declarada.")
        if spec.style == "line":
            maximum = max(ref["facts"], key=lambda f: Decimal(f["value"]))
            if not any(re.search(r"mayor|maxim", norm(c)) and month_in(c) == maximum["month"]
                       and has_amount(c, maximum["value"]) for c in clauses(text(output))):
                return checked(False, "El mes y el saldo del máximo no están explícitos y sustentados en la conclusión.")
        return checked(True, "Cumple las comprobaciones formales de la rúbrica, cifras, período y fuentes; no se usa igualdad literal con una respuesta modelo.")
    if case.id == "BA-006":
        route, content = scores["enrutamiento_correcto"], scores["responde_la_pregunta"]
        if route["score"] == 0 or content["score"] == 0:
            return checked(False, "El caso de clima ejecutó herramientas/especialistas o desarrolló contenido ajeno al banco.")
        if route["score"] == content["score"] == 1:
            return checked(True, "Negativa breve de alcance y ausencia de herramientas demostradas.")
    return nv("Corrección semántica no demostrada por las reglas conservadoras; requiere revisión humana.")


def reference_numbers(ref):
    supported = [f["value"] for f in ref["facts"]]
    for value in ref.get("derived", {}).values():
        if isinstance(value, dict):
            supported.extend(v for v in value.values() if v is not None)
        elif isinstance(value, str) and re.fullmatch(r"-?\d+(?:\.\d+)?", value):
            supported.append(value)
    return supported


def hallucination(case, output, scores):
    if case.id == "BA-006" and scores["responde_la_pregunta"]["score"] == 1:
        return checked(False, "No afirma condiciones meteorológicas; solo comunica el alcance.")
    ref = output.get("sql_reference", {})
    if ref.get("status") != "ok":
        return nv("No hay evidencia suficiente para descartar o confirmar afirmaciones no sustentadas.")
    supported = reference_numbers(ref)
    unknown = [m[0] for m in MONEY.finditer(text(output)) if not any(near(m[0], v) for v in supported)]
    if unknown:
        return checked(True, "Se detectaron cifras decimales visibles sin sustento SQL independiente.", unsupported=unknown)
    # Verificar cifras no demuestra que toda la prosa carezca de causas o hechos inventados.
    return nv("Las cifras comprobadas tienen sustento; falta revisión semántica para descartar otras afirmaciones o causas inventadas.")


def grade(case, output):
    scores = {}
    unavailable = output.get("status") != "ok" or output.get("correlation", {}).get("status") != "ok" or not output.get("trace")
    for name, fn in (("enrutamiento_correcto", routing), ("exactitud_numerica", numeric),
                     ("responde_la_pregunta", coverage), ("manejo_de_fechas", dates)):
        not_applicable = (name == "exactitud_numerica" and (not case.row[name] or case.id == "BA-015")) or (name == "manejo_de_fechas" and not case.row[name])
        scores[name] = verdict("no_aplica", "Criterio sin aplicación en esta rúbrica.") if not_applicable else nv("Sin respuesta final y traza única del mismo turno; no se inventa una nota.") if unavailable else fn(case, output)
    scores["responde_correcto"] = nv("Sin evidencia única del turno final.") if unavailable else correctness(case, output, scores)
    scores["alucinacion"] = nv("Sin evidencia única del turno final.") if unavailable else hallucination(case, output, scores)
    return {key: scores[key] for key in CRITERIA}
