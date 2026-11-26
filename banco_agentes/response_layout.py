"""Decisión de presentación del hub; cifras y vistas derivadas de evidencia autorizada."""
import json
import re
from decimal import Decimal, localcontext

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from typing import Literal

from .client_access import is_employee_purchase_quarter, is_purchase_plan
from .presentation import (chat_decimal_text, documentation_body, metric_text, month_text,
                           period_text, purchase_period_label, result_table, result_text, row_month, scope_text)
from .variation import variation_report, variation_text


class PresentationChoice(BaseModel):
    model_config=ConfigDict(extra="forbid")
    formato: Literal["auto","frase","tabla","prosa","grafica"]="auto"
    objetivo: Literal["auto","resumen","comparar","mayor","menor","tendencia"]="auto"
    resultados: list[str]=Field(default_factory=list,max_length=16)
    tipo_grafica: Literal["auto","barras","linea","histograma","dispersion"]="auto"


def decode_final(answer):
    text=answer.strip()
    if text.startswith("```"):
        text=re.sub(r"^```(?:json)?\s*|\s*```$","",text).strip()
    try:
        payload=json.loads(text)
    except (ValueError,TypeError):
        if text.startswith("{") and '"presentacion"' in text:
            return PresentationChoice(),"No se pudo interpretar la presentación solicitada. Se conserva la evidencia disponible."
        return PresentationChoice(),answer
    if not isinstance(payload,dict) or "presentacion" not in payload:
        return PresentationChoice(),answer
    prose=payload.get("respuesta","")
    prose=prose if isinstance(prose,str) else ""
    try:
        choice=PresentationChoice.model_validate(payload["presentacion"])
        if any(not re.fullmatch(r"R[1-9][0-9]{0,5}",value) for value in choice.resultados):
            raise ValueError("Referencia inválida")
    except (ValidationError,ValueError):
        choice=PresentationChoice()
    return choice,prose


def explicit_format(question):
    text=question.lower()
    if re.search(r"sin (?:una )?(?:gr[aá]fica|tabla)|solo texto|en prosa",text):
        return "prosa"
    if re.search(r"en (?:una |una sola )?frase",text):
        return "frase"
    if re.search(r"en (?:una )?tabla|formato tabular|(?:muestra|presenta|dame).*\btabla\b",text):
        return "tabla"
    if re.search(r"graf[ií]c(?:a|o|alo)|gr[aá]fic[ao]|histograma|dispersi[oó]n",text):
        return "grafica"
    return None


def intent(question, choice):
    text=question.lower()
    # La petición explícita prevalece sobre un objetivo erróneo del modelo.
    if re.search(r"\bmayor\b|m[aá]s alto|m[aá]s grande|\bsaldo m[aá]ximo\b",text):
        return "mayor"
    if re.search(r"menor|m[aá]s bajo",text):
        return "menor"
    if re.search(r"evoluci[oó]n|tendencia|c[oó]mo (?:cambi[oó]|vari[oó]|evolucion[oó])", text):
        return "tendencia"
    if choice.objetivo!="auto":
        return choice.objetivo
    if re.search(r"compar|diferencia",text):
        return "comparar"
    return "resumen"


def numeric_points(results):
    if not results or any(r["status"]!="ok" for r in results):
        return []
    signatures=set()
    points=[]
    for result in results:
        plan=result["plan"]
        metric=plan.get("base","total") if plan["metrica"]=="variacion" else plan["metrica"]
        field={"completitud":"nulos","duplicados":"grupos_duplicados","cobertura":"filas",
               "rangos":"fuera_rango","atipicos":"atipicos","consistencia":"inconsistencias"}.get(metric,"valor") if result.get("agent")=="data_quality" else "valor"
        signatures.add((metric,plan["conjunto"],plan.get("campo"),result["units"],plan["alcance"],field,plan.get("regla"),plan.get("minimo"),plan.get("maximo")))
        for row in result["rows"]:
            if row.get(field) is None:
                return []
            value=Decimal(str(row[field]))
            if not value.is_finite():
                return []
            labels=[str(row[key]) if key!="mes" else row_month(row[key])
                    for key in plan["agrupar"] if row.get(key) is not None]
            label=" / ".join(labels) or scope_text(plan).removeprefix("de ")
            if plan["conjunto"] == "saldos" and row.get("mes") is not None:
                if plan.get("producto") and "producto" not in plan["agrupar"]:
                    label += f" ({plan['producto']})"
            elif not labels or len(results)>1:
                label+=" "+period_text(plan)
            points.append(dict(label=label, value=value, raw=str(row[field]), result=result,
                               product=row.get("producto") or plan.get("producto")))
    return points if len(signatures)==1 else []


def comparison_text(points, objective):
    if len(points)<2:
        return None
    low=min(p["value"] for p in points)
    high=max(p["value"] for p in points)
    metric=points[0]["result"]["plan"]
    kind=metric["metrica"]
    metric=metric_text(metric,metric.get("base","total") if kind=="variacion" else kind)
    if points[0]["result"].get("agent")=="data_quality":
        metric={"completitud":"número de valores nulos","duplicados":"número de grupos duplicados",
                "cobertura":"número de registros","rangos":"número de valores fuera de rango",
                "atipicos":"número de atípicos","consistencia":"número de inconsistencias"}.get(kind,metric)
    if low==high:
        return f"Los resultados comparados tienen el mismo {metric}: {chat_decimal_text(high)} (empate)."
    extreme=low if objective=="menor" else high
    winners=[p["label"] for p in points if p["value"]==extreme]
    noun="menor" if objective=="menor" else "mayor"
    label="; ".join(winners)
    text=f"El {metric} {noun} corresponde a {label}, con {chat_decimal_text(extreme)}."
    if len(winners)>1:
        text+=" Hay empate entre esos resultados."
    if len(points)==2:
        other=next(p for p in points if p["value"]!=extreme)
        with localcontext() as context:
            context.prec=max(40,len(str(low))+len(str(high))+8)
            delta=high-low
        text+=f"\n\n{other['label']} registra {chat_decimal_text(other['value'])}; la diferencia es {chat_decimal_text(delta)}."
    return text


def purchase_peak_text(result):
    rows = result["rows"]
    if not rows:
        return f"No se registraron compras con tus tarjetas de crédito {period_text(result['plan'])}."
    values = [(row, Decimal(str(row["valor"]))) for row in rows if row.get("valor") is not None]
    values = [(row, value) for row, value in values if value.is_finite()]
    if not values:
        return "Se registraron compras con tus tarjetas de crédito, pero faltan importes para compararlas."
    maximum = max(value for _, value in values)
    months = [row_month(row["mes"]) for row, value in values if value == maximum]
    incomplete = len(values) != len(rows) or any(row["observados"] < row["filas"] for row in rows)
    prefix = "Entre los importes disponibles, el" if incomplete else "El"
    text = f"{prefix} mayor gasto en compras fue de {chat_decimal_text(maximum)} en " + " y ".join(months) + "."
    return text + " Hubo empate." if len(months) > 1 else text


def balance_chart_text(chart, results, objective):
    """Describe los valores de las series verificadas que verá el usuario."""
    series = [(s["name"], [(month, Decimal(str(value))) for month, value in zip(s["x"], s["y"])
                           if value is not None]) for s in chart["series"]]
    metric = metric_text(results[0]["plan"], chart["metric"])
    if objective in {"mayor", "menor"}:
        points = [(name, month, value) for name, values in series for month, value in values]
        extreme = (max if objective == "mayor" else min)(point[2] for point in points)
        labels = [f"{name} en {month_text(month)}" for name, month, value in points if value == extreme]
        incomplete = any(r["status"] != "ok" for r in results) or any(
            len(values) < len(chart["series"][0]["x"]) for _, values in series)
        qualifier = "Entre los datos disponibles, el" if incomplete else "El"
        text = f"{qualifier} {metric} {objective} corresponde a {' y '.join(labels)}, con {chat_decimal_text(extreme)}."
        return text + (" Hay empate." if len(labels) > 1 else "")

    def span(first, last):
        start = month_text(first)
        if first[:4] == last[:4]:
            start = start.split(" de ", 1)[0]
        return f"De {start} a {month_text(last)}"

    cuts = chart["series"][0]["x"]
    full_span = len(cuts) > 1 and all(values and values[0][0] == cuts[0] and values[-1][0] == cuts[-1]
                                      for _, values in series)
    if full_span:
        changes = [f"{name} pasó de {chat_decimal_text(values[0][1])} a {chat_decimal_text(values[-1][1])}."
                   for name, values in series]
        if len(changes) == 1:
            return f"{span(cuts[0], cuts[-1])}, {changes[0]}"
        return f"{span(cuts[0], cuts[-1])}." + "\n\n" + "\n".join("- " + change for change in changes)
    if len(cuts) == 1:
        values = [f"{name}: {chat_decimal_text(rows[0][1])}" if rows else f"{name}: sin datos" for name, rows in series]
        summary = f"Al cierre de {month_text(cuts[0])}, {metric}:" + "\n\n" + "\n".join("- " + value for value in values)
        if len(series) == 2 and all(rows for _, rows in series):
            first, second = (rows[0][1] for _, rows in series)
            with localcontext() as context:
                context.prec = max(40, len(str(first)) + len(str(second)) + 8)
                difference = abs(first - second)
            summary += f"\n\nLa diferencia es {chat_decimal_text(difference)}."
        return summary
    changes = []
    for name, values in series:
        if not values:
            changes.append(f"No hay datos para {name}.")
        elif len(values) == 1:
            month, value = values[0]
            changes.append(f"Para {name} solo hay un dato en {month_text(month)}: {chat_decimal_text(value)}.")
        else:
            first, start = values[0]
            last, end = values[-1]
            changes.append(f"{span(first, last)}, {name} pasó de {chat_decimal_text(start)} a {chat_decimal_text(end)}.")
    return "\n\n".join(changes)


def combined_table(results):
    labels={"control":"Control","producto":"Producto","periodo":"Período","campo":"Campo",
            "mes":"Mes","persona":"Tipo de persona","segmento":"Segmento","movimiento":"Movimiento",
            "valor":"Resultado","nulos":"Nulos","grupos_duplicados":"Grupos duplicados",
            "filas_adicionales":"Filas adicionales","filas":"Registros revisados","observados":"Observados",
            "fuera_rango":"Fuera de rango","atipicos":"Atípicos","inconsistencias":"Inconsistencias",
            "sin_evidencia":"Sin evidencia","frecuencia":"Frecuencia","desde":"Desde","hasta":"Hasta"}
    controls={"completitud":"Completitud (NULL)","duplicados":"Duplicados"}
    rows=[]
    keys=[]
    for result in results:
        plan=result["plan"]
        quality=result.get("agent")=="data_quality"
        for row in result["rows"]:
            cells={"control":controls.get(plan["metrica"],plan["metrica"].capitalize()),
                   "producto":plan.get("producto") or row.get("producto") or "Todos",
                   "periodo":period_text(plan),"campo":plan.get("campo") or "No aplica"}
            if not quality:
                cells.pop("campo")
            for key,value in row.items():
                if key in {"filas","observados"} and not quality:
                    continue
                if key in {"producto","persona","segmento","movimiento"}:
                    cells[key]=str(value) if value is not None else "Sin valor"
                elif key=="mes" and value is not None:
                    cells[key]=row_month(value)
                else:
                    cells[key]=chat_decimal_text(value)
            rows.append(cells)
            keys.extend(key for key in cells if key not in keys)
    return {"title":"Resultados verificados", "columns":[{"key":key,"label":labels.get(key,key.replace("_"," ").capitalize())} for key in keys],
            "rows":[{key:row.get(key,"No aplica") for key in keys} for row in rows[:24]],
            "truncated":len(rows)>24}


def product_comparison_table(points):
    products = {point["product"] for point in points}
    if not (2 <= len(products) == len(points) and all(products)):
        return None
    return {"title": "Comparación por producto", "truncated": False,
            "columns": [{"key": "producto", "label": "Producto"}, {"key": "valor", "label": "Resultado"}],
            "rows": [{"producto": point["product"], "valor": chat_decimal_text(point["raw"])} for point in points]}


def variation_table(report):
    monthly = report["rate_mode"] == "mensual"
    cuts = report["cuts"] if monthly else ["crecimiento"]
    columns = [{"key": "producto", "label": "Producto"}]
    columns.extend({"key": key, "label": month_text(key) if monthly else "Crecimiento"} for key in cuts)
    rows = [{"producto": series["name"], **{
        key: chat_decimal_text(value) + "%" if value is not None else f"No calculable ({reason})"
        for key, value, reason in zip(cuts, series["y"], series["reasons"], strict=True)}}
        for series in report["series"]]
    return {"title": report["title"], "columns": columns, "rows": rows, "truncated": False}


def make_layout(turn, results, question):
    cached=getattr(turn,"response_layout",None)
    if cached is not None:
        return cached
    choice=getattr(turn,"presentation_choice",PresentationChoice())
    objective=intent(question,choice)
    purchase = None
    if len(results) == 1:
        purchase_plan = results[0]["plan"]
        if is_purchase_plan(purchase_plan) or is_employee_purchase_quarter(purchase_plan, purchase_plan["agrupar"]):
            purchase = results[0]
    grouping = tuple(purchase["plan"]["agrupar"]) if purchase else ()
    purchases = grouping == ("mes",)
    period_purchases = grouping in {("trimestre",), ("anio",)}
    if purchases and re.search(r"\bgast[ée]\s+m[aá]s\b", question, re.I):
        objective = "mayor"
    points=numeric_points(results)
    charts=list(turn.charts)
    selected_ids = {r["id"] for r in results}
    charts = [chart for chart in charts if set(chart.get("result_ids", [])) == selected_ids]
    requested_format = explicit_format(question)
    mode=requested_format or choice.formato
    variation = getattr(turn, "variation", None)
    if variation and requested_format not in {"tabla", "frase", "prosa"}:
        mode = "grafica" if variation.plot else "prosa"
    requested=mode
    if mode=="auto":
        mode="grafica" if charts else "tabla" if sum(len(r["rows"]) for r in results)>1 else "frase" if results else "prosa"
    if results and not requested_format and any(r["plan"]["metrica"] == "cuentas_propias" for r in results):
        mode = "tabla"
    if purchases and not results[0]["rows"]:
        mode = "frase"
    elif purchases and objective == "mayor" and not requested_format:
        mode = "tabla"
    if period_purchases and purchase["rows"] and mode != "grafica" and not requested_format:
        mode = "tabla"
    if not results:
        mode="prosa"
    notes=[]
    unsupported=re.search(r"(?:en|formato)\s+(?:formato\s+)?(csv|json|excel|pdf)\b",question,re.I)
    if unsupported:
        notes.append(f"El formato {unsupported[1].upper()} no está disponible en el chat; se utiliza una vista compatible.")
    balance_chart = next((chart for chart in charts if chart.get("series") and not chart.get("purchase_group")),
                         None) if mode == "grafica" else None
    explicit_extreme = intent(question, PresentationChoice())
    rate_table = None
    if variation:
        try:
            if balance_chart and balance_chart.get("rate_mode") and not variation.calculation:
                rates = balance_chart
            else:
                rates = variation_report(results, variation.mode, include_calculation=variation.calculation,
                                         products=variation.products)
            turn.variation_scope = {"result_ids": rates["result_ids"], "rate_mode": variation.mode}
            if mode == "tabla" or (len(rates["series"]) > 1 and requested_format not in {"frase", "prosa"}):
                rate_table = variation_table(rates)
            conclusion = getattr(turn, "chart_notice", "") or variation_text(
                rates, variation.formula, compare=variation.compare, show_max=variation.show_max, chat=True,
                with_table=rate_table is not None)
        except ValueError as error:
            conclusion = str(error) if results else ""
    elif purchases and (objective == "mayor" or not results[0]["rows"]):
        conclusion = purchase_peak_text(results[0])
    elif period_purchases:
        if purchase["rows"]:
            plan = purchase["plan"]
            group = grouping[0]
            amounts = [f"{purchase_period_label(plan, group, row[group])} — {chat_decimal_text(row['valor'])}."
                       for row in purchase["rows"]]
            description = "Compras de TDC en tus cuentas." if is_purchase_plan(plan) else f"Compras {scope_text(plan)}."
            conclusion = description + "\n\n" + "\n".join("- " + amount for amount in amounts)
        else:
            conclusion = purchase_peak_text(purchase)
    elif balance_chart:
        # Una intención genérica del modelo no convierte una evolución en una búsqueda del máximo.
        objective = explicit_extreme if explicit_extreme in {"mayor", "menor"} else (
            "tendencia" if len(balance_chart["series"][0]["x"]) > 1 else "resumen")
        conclusion = getattr(turn, "chart_notice", "") or balance_chart_text(balance_chart, results, objective)
    elif objective in {"mayor","menor","comparar"}:
        conclusion=comparison_text(points,objective)
        if conclusion is None and results:
            conclusion="Los resultados disponibles no permiten determinar un mayor comparable; faltan datos completos o las métricas son distintas."
    elif len(results)==1:
        conclusion=result_text(results[0], number=chat_decimal_text)
    elif results and all(r.get("agent")=="data_quality" for r in results):
        conclusion=f"Se obtuvieron {len(results)} comprobaciones de calidad; el detalle por control y período se muestra a continuación."
    else:
        conclusion=comparison_text(points,objective) if len(points)>1 else None
    if not conclusion and results:
        conclusion="\n\n".join(result_text(r, number=chat_decimal_text))
    if not variation and mode in {"frase","prosa"} and len(results)>1 and not points:
        conclusion="\n\n".join(result_text(r, number=chat_decimal_text))
    kind=choice.tipo_grafica
    for pattern,value in ((r"histograma","histograma"),(r"dispersi[oó]n","dispersion"),(r"l[ií]neas?","linea"),(r"barras?","barras")):
        if re.search(pattern,question,re.I):
            kind=value
            break
    if variation:
        kind = variation.graph_kind
        if mode == "grafica" and variation.mode == "periodo" and variation.kind == "linea":
            notes.append("El crecimiento del periodo da una tasa por producto y se muestra en barras.")
    if mode=="grafica" and kind!="auto" and any(c["type"]!=kind for c in charts):
        charts=[]
    if mode=="grafica" and not charts and results:
        mode="tabla"
        notes.append("Graph no preparó una gráfica compatible con todos los resultados seleccionados. Se presenta la evidencia disponible en tabla.")
    if mode!="grafica":
        charts=[]
    tables=[]
    comparison = None
    if objective == "comparar" and not variation and requested_format not in {"frase", "prosa"}:
        comparison = product_comparison_table(points)
    if rate_table or comparison:
        tables = [rate_table or comparison]
    elif mode == "tabla" and results:
        table = result_table(results[0], number=chat_decimal_text) if len(results) == 1 else None
        tables = [table or combined_table(results)]
    for result in results:
        for note in result.get("notes",[]):
            if not note.startswith(("Cambio absoluto:","Cambio relativo:")) and note not in (conclusion or ""):
                notes.append(note)
    layout=dict(format=mode,requested_format=requested,objective=objective,result_ids=[r["id"] for r in results],
                conclusion=conclusion or "",tables=tables,charts=charts,notes=list(dict.fromkeys(notes)))
    turn.response_layout=layout
    turn.trace.records.append(dict(event="presentation_selected",agent="orquestador",
                                   choice=choice.model_dump(),format=mode,objective=objective,
                                   result_ids=layout["result_ids"]))
    return layout
