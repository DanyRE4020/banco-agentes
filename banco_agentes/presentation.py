"""Presentación determinista de resultados verificados del chat."""
import re
from decimal import Decimal, localcontext

MONTHS = ("", "enero", "febrero", "marzo", "abril", "mayo", "junio",
          "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre")
DATASETS = {"saldos": "saldos", "movimientos": "movimientos", "clientes": "clientes"}
TABLES = {"cfg.fact_sdos_captacoloca_m": "Saldos mensuales",
          "cliente.saldos": "Saldos de tus cuentas",
          "cliente.movimientos": "Movimientos de tus cuentas",
          "cfg.fact_txn_captacoloca_d": "Movimientos",
          "dim.dim_cliente": "Catálogo de clientes"}
COLUMNS = {"mes": "Mes", "producto": "Producto", "persona": "Tipo de persona",
           "trimestre": "Trimestre calendario", "anio": "Año",
           "segmento": "Segmento", "movimiento": "Movimiento", "valor": "Resultado",
           "desde": "Desde", "hasta": "Hasta", "frecuencia": "Frecuencia",
           "apertura": "Apertura media", "cierre": "Cierre medio"}
METRICS = {"total": "saldo total", "promedio": "saldo medio", "minimo": "saldo mínimo",
           "maximo": "saldo máximo", "mediana": "mediana del saldo",
           "desviacion": "desviación estándar muestral del saldo",
           "neto": "efecto neto de los movimientos",
           "filas": "registros", "cuentas": "cuentas", "clientes": "clientes"}


def decimal_text(value, *, thousands=".", decimals=","):
    """Localiza el valor textual conservando sus decimales, sin pasar por float."""
    if value is None:
        return "Sin valor"
    number = Decimal(str(value))
    if not number.is_finite():
        return "Sin valor"
    whole, dot, fraction = format(number, "f").partition(".")
    sign = "-" if whole.startswith("-") else ""
    digits = whole.lstrip("-")
    groups = []
    while digits:
        groups.insert(0, digits[-3:])
        digits = digits[:-3]
    return sign + thousands.join(groups) + (decimals + fraction if dot else "")


def chat_decimal_text(value):
    return decimal_text(value, thousands=",", decimals=".")


# Las notas documentales ya llevan la moneda. Se conserva su escala y su código.
MONEY_IN_NOTE = re.compile(r"(?<![\d.,])(-?\d+(?:\.\d{3})*(?:,\d+)?)(?= (?:MXN|USD|EUR)\b)")


def document_amounts(text):
    return MONEY_IN_NOTE.sub(lambda match: chat_decimal_text(match[1].replace(".", "").replace(",", ".")), text)


def month_text(value):
    year, month = value[:7].split("-")
    return f"{MONTHS[int(month)]} de {year}"


def row_month(value):
    compact = str(value)
    return month_text(compact[:4] + "-" + compact[4:6])


def purchase_period_label(plan, group, value):
    if group == "trimestre":
        year, quarter = divmod(int(value), 10)
        first = 3 * quarter - 2
        start, end, label = first, first + 2, f"T{quarter} {year}"
    else:
        year = int(value)
        start, end, label = 1, 12, str(year)
    months = [period % 100 for period in plan["periodos"]
              if period // 100 == year and start <= period % 100 <= end]
    coverage = MONTHS[months[0]]
    if len(months) > 1:
        coverage += " a " + MONTHS[months[-1]]
    partial = "parcial, " if len(months) < end - start + 1 else ""
    return f"{label} ({partial}{coverage})"


def scope_text(plan):
    scope = f"de {plan['producto']}" if plan.get("producto") else "del conjunto consultado"
    if plan.get("persona") in {"PF", "PM"}:
        scope += " para personas " + ("físicas" if plan["persona"] == "PF" else "morales")
    if plan.get("segmento"):
        scope += f" del segmento {plan['segmento']}"
    if plan.get("alcance") == "propio":
        scope += " en tus cuentas"
    return scope


def period_text(plan):
    start, end = plan.get("desde"), plan.get("hasta") or plan.get("desde")
    if not start:
        return "en el catálogo disponible"
    if start != end:
        return f"entre {month_text(start)} y {month_text(end)}"
    if plan["conjunto"] == "saldos":
        return f"al cierre de {month_text(start)}"
    return f"durante {month_text(start)}"


def metric_text(plan, metric):
    text = METRICS.get(metric, metric)
    if plan["conjunto"] == "movimientos" and metric not in {"neto", "filas", "cuentas", "clientes"}:
        text = text.replace("saldo", "importe de movimientos")
    if plan.get("campo") == "saldo_apertura":
        text += " de apertura"
    return text


def trend_text(result, *, number=decimal_text):
    plan = result["plan"]
    metric = plan.get("base", "total") if plan["metrica"] == "variacion" else plan["metrica"]
    rows = sorted(result["rows"], key=lambda row: row["mes"])
    first, last = rows[0], rows[-1]
    old, new = Decimal(str(first["valor"])), Decimal(str(last["valor"]))
    with localcontext() as context:
        context.prec = max(40, len(str(old)) + len(str(new)) + 8)
        change = new - old
        if change == 0:
            movement = f"se mantuvo en {number(new)}"
        else:
            direction = "aumentó" if change > 0 else "disminuyó"
            movement = f"{direction} de {number(old)} a {number(new)}, una diferencia de {number(abs(change))}"
            from .variation import percentage
            relative = percentage(old, new)
            if relative is not None:
                percent = abs(relative).quantize(Decimal("0.0001"))
                suffix = "%" if number is chat_decimal_text else " %"
                movement += f" (aproximadamente {number(percent)}{suffix})"
    return (f"El {metric_text(plan, metric)} {scope_text(plan)} {movement} "
            f"entre {row_month(first['mes'])} y {row_month(last['mes'])}.")


def quality_text(result, *, number=decimal_text):
    plan, rows = result["plan"], result["rows"]
    scope = f"en {DATASETS[plan['conjunto']]} {scope_text(plan)} {period_text(plan)}"
    metric = plan["metrica"]
    row = rows[0]
    if metric == "duplicados" and "grupos_duplicados" in row:
        key = "cuenta, producto y corte" if plan["conjunto"] == "saldos" else "la clave esperada del conjunto"
        return f"Se encontraron {number(row['grupos_duplicados'])} grupos duplicados {scope}. Se revisó la repetición por {key}."
    if metric == "completitud" and "nulos" in row:
        return f"Se encontraron {number(row['nulos'])} valores nulos en {plan['campo']} {scope}."
    if metric == "completitud" and "incompletos" in row:
        text = (f"{number(row['incompletos'])} de {number(row['filas'])} clientes tienen campos básicos incompletos. "
                "La revisión contó una vez cada registro con al menos un campo básico sin valor.")
        if "faltantes_por_campo" in row:
            missing = [f"{field['campo']} ({number(field['nulos'])})" for field in row["faltantes_por_campo"]
                       if field["nulos"]]
            text += " Los campos con faltantes son " + ", ".join(missing) + "." if missing else " No hay campos básicos con faltantes."
        return text
    if metric == "cobertura":
        missing = [row_month(row["mes"]) for row in rows if row["filas"] == 0]
        if missing:
            return f"Faltan registros {scope} en " + ", ".join(missing) + "."
        return f"Hay registros en todos los meses consultados {scope}."
    if metric in {"rangos", "atipicos", "consistencia"}:
        field = {"rangos": "fuera_rango", "atipicos": "atipicos", "consistencia": "inconsistencias"}[metric]
        if field in row:
            finding = {"rangos": "valores fuera del rango", "atipicos": "valores atípicos", "consistencia": "inconsistencias"}[metric]
            text = f"Se encontraron {number(row[field])} {finding} {scope}."
            if row.get("sin_evidencia"):
                text += f" Quedaron {number(row['sin_evidencia'])} casos sin evidencia suficiente."
            return text
    return f"La comprobación de {metric} {scope} produjo el resultado de la tabla."


def result_text(result, *, number=decimal_text):
    plan, rows = result["plan"], result["rows"]
    scope = f"{scope_text(plan)} {period_text(plan)}"
    if result["status"] == "insufficient" or not rows:
        return f"No hay observaciones suficientes para responder sobre {DATASETS[plan['conjunto']]} {scope}."
    metric = plan["metrica"]
    if metric == "cuentas_propias":
        return f"Tienes {len({row['cuenta'] for row in rows})} cuentas con datos. La tabla separa el saldo a favor de la deuda al último corte disponible por cuenta."
    if result.get("agent") == "data_quality":
        conclusion = quality_text(result, number=number)
    elif metric == "histograma":
        count = sum(int(row["frecuencia"]) for row in rows)
        conclusion = f"La distribución de importes {scope} comprende {number(count)} observaciones en {len(rows)} intervalos."
    elif metric == "dispersion":
        conclusion = f"La comparación de apertura y cierre medios {scope} abarca {len(rows)} grupos."
    elif (metric == "variacion" or tuple(plan["agrupar"]) == ("mes",)) and len(rows) >= 2 and all(row.get("valor") is not None for row in rows):
        conclusion = trend_text(result, number=number)
    elif len(rows) == 1 and all(
        (group == "mes" and plan["desde"] == plan["hasta"]) or plan.get(group)
        for group in plan["agrupar"]
    ):
        value = number(rows[0].get("valor"))
        if rows[0].get("valor") is None:
            return f"No se obtuvo un valor verificable {scope}."
        if metric in {"filas", "cuentas", "clientes"}:
            conclusion = f"Se registran {value} {METRICS[metric]} {scope}."
        else:
            conclusion = f"El {metric_text(plan, metric)} {scope} es {value}."
    else:
        measurable = [row for row in rows if row.get("valor") is not None]
        if measurable:
            best = max(measurable, key=lambda row: Decimal(str(row["valor"])))
            names = " / ".join(str(best[key]) for key in plan["agrupar"])
            conclusion = f"El mayor {metric_text(plan, metric)} {scope} corresponde a {names}, con {number(best['valor'])}."
        else:
            conclusion = f"El resultado {scope} se presenta en la tabla."
    if result["status"] == "partial":
        conclusion = "El resultado es parcial. " + conclusion
    notes = [note for note in result.get("notes", []) if not note.startswith(("Cambio absoluto:", "Cambio relativo:"))]
    if result.get("rule") and result.get("agent") == "data_quality":
        if metric == "rangos":
            basis = "documentado" if plan.get("fuente_regla") else "propuesto"
            notes.insert(0, f"Se aplicó el rango {basis} de {number(plan['minimo'])} a {number(plan['maximo'])}, incluidos ambos límites.")
        elif metric == "atipicos":
            notes.insert(0, f"Se aplicaron cercas de Tukey de {number('1.5')} veces el rango intercuartílico dentro del alcance consultado.")
        elif metric == "consistencia" and plan.get("regla") == "cierre":
            notes.insert(0, "Se comprobó que apertura más efecto neto de movimientos coincida con el cierre.")
    return ("\n\n" if number is chat_decimal_text else "\n").join([conclusion, *notes])


def result_table(result, *, number=decimal_text):
    plan = result["plan"]
    if not result["rows"] or (len(result["rows"]) == 1 and not plan["agrupar"]):
        return None
    columns = [key for key in result["columns"] if key not in {"filas", "observados"}]
    if plan["metrica"] == "cobertura":
        columns = ["mes", "filas"]
    if not columns:
        return None
    labels = [{"key": key, "label": "Registros" if key == "filas" else COLUMNS.get(key, key.replace("_", " ").capitalize())}
              for key in columns]
    rows = []
    for row in result["rows"][:24]:
        cells = {}
        for key in columns:
            value = row[key]
            if key == "mes" and value is not None:
                cells[key] = row_month(value)
            elif key in {"trimestre", "anio"} and value is not None:
                cells[key] = purchase_period_label(plan, key, value)
            elif key == "persona" and value in {"PF", "PM"}:
                cells[key] = "Personas físicas" if value == "PF" else "Personas morales"
            elif value is None:
                cells[key] = "Sin valor"
            elif key in plan["agrupar"] and key != "mes":
                cells[key] = str(value)
            else:
                cells[key] = number(value)
        rows.append(cells)
    return {"title": f"Resultado {scope_text(plan)} {period_text(plan)}", "columns": labels, "rows": rows,
            "truncated": len(result["rows"]) > len(rows)}


def source_entry(citation):
    match = re.match(r"^(.*\.sql)(?::(.*))?$", citation)
    if match:
        path, lines = match.groups()
        filename = path.rsplit("/", 1)[-1]
        title = {"001": "Dimensiones de clientes, productos y movimientos",
                 "002": "Generación de cuentas y saldos de enero",
                 "003": "Generación de movimientos y saldos de febrero a junio"}.get(filename[:3], filename)
        return {"label": title, "location": f"líneas {lines}" if lines else "Definición del esquema",
                "kind": "script", "details": {"Archivo": path, "Tipo de documento": "Script de referencia"}}
    if citation in TABLES:
        return {"label": TABLES[citation], "location": citation, "kind": "table"}
    return {"label": "Referencia documental", "location": citation, "kind": "document"}


def documentation_body(text):
    lines = []
    for line in text.splitlines():
        if line.startswith(("Fuente:", "Fuentes SQL:", "Fuentes consultadas:")):
            continue
        line = line.partition("Fuentes revisadas:")[0].strip()
        line = re.sub(r"\[referencias/[^\]]+\]", "", line).rstrip()
        if line:
            lines.append(line)
    if lines and lines[0].startswith("Columnas de "):
        table = lines[0].split("`")[1]
        count = sum(line.startswith("- ") for line in lines)
        lines[0] = f"La tabla {table} tiene {count} columnas declaradas."
    return "\n".join(lines)
