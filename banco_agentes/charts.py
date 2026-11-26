"""Especificaciones de gráficas derivadas exclusivamente de resultados autorizados."""
import math
from datetime import date

from .client_access import is_employee_purchase_quarter, is_purchase_plan
from .presentation import COLUMNS, purchase_period_label

CHART_TYPES = ("barras", "linea", "histograma", "dispersion")
PRODUCT_COLORS = {"CHEQUES": "#007478", "TDC": "#c53656", "AHORRO": "#3459a6", "C_PERSONAL": "#865b28"}


def balance_rows(result):
    """Normaliza las dimensiones de los saldos sin transformar sus valores."""
    plan = result["plan"]
    groups = set(plan["agrupar"])
    if ((plan["conjunto"] != "saldos" and not is_purchase_plan(plan))
            or plan["metrica"] in {"histograma", "dispersion", "variacion", "cuentas_propias"}
            or groups - {"mes", "producto"} or not ("producto" in groups or plan.get("producto"))
            or not ("mes" in groups or plan["desde"] == plan["hasta"])):
        return None
    rows = []
    for row in result["rows"]:
        month = str(row.get("mes", plan["desde"])).replace("-", "")
        rows.append({"mes": f"{month[:4]}-{month[4:]}", "producto": row.get("producto", plan["producto"]),
                     "valor": row["valor"]})
    return rows


def signature(result, *, balances=False):
    plan = result["plan"]
    groups = () if balances else tuple(plan["agrupar"])
    filters = tuple(plan.get(key) for key in (
        "metrica", "base", "conjunto", "campo", "desde", "hasta", "persona", "segmento", "codigo", "alcance"
    ))
    return filters + (groups, result["units"], result["table"], result["scope"])


def balance_chart(results, rows, kind, *, require_value=True):
    if len({signature(result, balances=True) for result in results}) != 1:
        raise ValueError("Los resultados de saldos deben tener métrica, período, unidad, filtros y alcance compatibles.")
    plan = results[0]["plan"]
    start, end = (date.fromisoformat(plan[key] + "-01") for key in ("desde", "hasta"))
    cuts = []
    while start <= end:
        cuts.append(start.strftime("%Y-%m"))
        start = date(start.year + (start.month == 12), start.month % 12 + 1, 1)
    kind = ("linea" if len(cuts) > 1 else "barras") if kind == "auto" else kind
    if kind == "linea" and len(cuts) < 2:
        raise ValueError("Una línea requiere al menos dos cortes.")
    points, cells, products = [], {}, set()
    for result, block in zip(results, rows):
        status = result["status"]
        if status == "insufficient" and not block and result["plan"]["producto"] in PRODUCT_COLORS:
            products.add(result["plan"]["producto"])
            continue
        if status not in {"ok", "partial"} or not block:
            raise ValueError("La gráfica requiere observaciones verificadas.")
        notes = result.get("notes", [])
        if status == "partial" and (
                any(note.startswith("Primeras ") for note in notes)
                or not (any(r["valor"] is None for r in block)
                        or any(note.startswith(("Cortes sin filas:", "Hay importes faltantes")) for note in notes))):
            raise ValueError("La gráfica no puede usar un resultado truncado o sin cobertura conocida.")
        for row in block:
            product, month, value = row["producto"], row["mes"], row["valor"]
            if product not in PRODUCT_COLORS or month not in cuts or (product, month) in cells:
                raise ValueError("Hay productos, meses o categorías repetidas fuera del alcance de la gráfica.")
            if value is not None and not math.isfinite(float(value)):
                raise ValueError("Hay valores no representables.")
            cells[product, month] = (value, result["id"])
            products.add(product)
            label = product if len(cuts) == 1 else f"{product} / {month}"
            points.append({"label": label, "x": None, "y": value, "result_id": result["id"]})
    if not products or (require_value and not any(point["y"] is not None for point in points)):
        raise ValueError("La gráfica requiere al menos un valor verificado.")
    if len(cuts) * len(products) > 60:
        raise ValueError("Reduce el alcance a 60 puntos o menos.")
    series = []
    for product, color in PRODUCT_COLORS.items():
        if product in products:
            values = [cells.get((product, month), (None, None)) for month in cuts]
            series.append({"name": product, "color": color, "x": cuts, "y": [v for v, _ in values],
                           "result_ids": [key for _, key in values]})
    citations = list(dict.fromkeys(source for r in results for source in r["sources"]))
    purchases = is_purchase_plan(plan)
    return {"type": kind, "title": "Compras de TDC por mes" if purchases else f"{plan['metrica'].capitalize()} de saldos",
            "x_label": "Mes", "y_label": "Importe de compras" if purchases else plan["metrica"].capitalize(),
            "period": cuts[0] if len(cuts) == 1 else f"{cuts[0]} a {cuts[-1]}",
            "units": results[0]["units"], "source": results[0]["table"], "citations": citations,
            "points": points, "series": series, "schema_version": 2,
            "result_ids": [r["id"] for r in results], "metric": plan["metrica"]}


def chart_for_results(results, kind="auto"):
    """Contrato v1: valores y procedencia del turno; solo barras combinan bloques."""
    if not results or len(results) > 16:
        raise ValueError("Selecciona entre uno y 16 resultados verificados del turno.")
    if any(r.get("agent") != "data_analytics" for r in results):
        raise ValueError("Graph solo representa resultados de Analytics.")
    ids = [r["id"] for r in results]
    if len(set(ids)) != len(ids):
        raise ValueError("No repitas IDs de resultados.")
    plan = results[0]["plan"]
    groups = tuple(plan["agrupar"])
    if len(results) == 1 and (is_employee_purchase_quarter(plan, groups)
                              or (is_purchase_plan(plan) and groups in {("trimestre",), ("anio",)})):
        return purchase_period_chart(results[0], kind)
    rows = [r["series_rows"] if "series_rows" in r else balance_rows(r) for r in results]
    if kind in {"auto", "barras", "linea"} and all(block is not None for block in rows):
        chart = balance_chart(results, rows, kind)
        if len(chart["citations"]) > 8:
            raise ValueError("La gráfica admite hasta ocho citas; reduce el alcance.")
        return chart
    if len(results) == 1:
        chart = chart_for(results[0], kind)
        for point in chart["points"]:
            point["result_id"] = ids[0]
    else:
        if kind not in {"auto", "barras"}:
            raise ValueError("La combinación de varios resultados está disponible solo en barras.")
        if len({signature(r) for r in results}) != 1:
            raise ValueError("Los resultados deben tener métrica, período, unidad, filtros y alcance compatibles.")
        chart = chart_for(results[0], "barras")
        points = []
        for result in results:
            block = chart_for(result, "barras")
            for point in block["points"]:
                product = result["plan"].get("producto")
                label = point["label"]
                if product and "producto" not in result["plan"]["agrupar"]:
                    label = product if label == "Resultado" else f"{product} / {label}"
                elif label == "Resultado":
                    label = result["id"]
                points.append({**point, "label": label, "result_id": result["id"]})
        if len(points) > 60:
            raise ValueError("Reduce el alcance a 60 puntos o menos.")
        if len({p["label"] for p in points}) != len(points):
            raise ValueError("Hay categorías repetidas; aclara qué resultados quieres comparar.")
        chart.update(title="Comparación de resultados verificados", x_label="Producto / grupo", points=points,
                     citations=list(dict.fromkeys(source for r in results for source in r["sources"])))
    if len(chart["citations"]) > 8:
        raise ValueError("La gráfica admite hasta ocho citas; reduce el alcance.")
    return {**chart, "schema_version": 1, "result_ids": ids, "metric": results[0]["plan"]["metrica"]}


def purchase_period_chart(result, kind):
    plan = result["plan"]
    group = plan["agrupar"][0]
    if kind not in {"auto", "barras"}:
        raise ValueError("Las compras trimestrales o anuales se representan en barras.")
    chart = chart_for(result, "barras")
    for row, point in zip(result["rows"], chart["points"]):
        point.update(label=purchase_period_label(plan, group, row[group]), result_id=result["id"])
    chart.update(title=f"Compras de TDC por {COLUMNS[group].lower()}", x_label=COLUMNS[group],
                 y_label="Importe de compras", schema_version=2, purchase_group=group, metric=plan["metrica"],
                 result_ids=[result["id"]], series=[{"name": "TDC", "color": PRODUCT_COLORS["TDC"],
                     "x": [point["label"] for point in chart["points"]],
                     "y": [point["y"] for point in chart["points"]]}])
    return chart


def chart_for(result, kind):
    if result["status"] != "ok" or not result["rows"]:
        raise ValueError("La gráfica requiere un resultado completo y con observaciones.")
    plan = result["plan"]
    groups = plan["agrupar"]
    metric = plan["metrica"]
    plotted_metric = plan.get("base", "total") if metric == "variacion" else metric
    if kind == "auto":
        kind = {"histograma": "histograma", "dispersion": "dispersion"}.get(
            metric, "linea" if groups == ("mes",) else "barras")
    if kind not in CHART_TYPES:
        raise ValueError("Tipos disponibles: barras, linea, histograma y dispersion.")
    if (metric == "histograma") != (kind == "histograma") or (metric == "dispersion") != (kind == "dispersion"):
        raise ValueError("El resultado debe haberse calculado para el tipo de gráfica solicitado.")
    if kind == "linea" and groups != ("mes",):
        raise ValueError("Una línea requiere una serie ordenada por mes, con los demás grupos filtrados.")
    points = []
    for row in result["rows"]:
        label = " / ".join(str(row[group]) if row[group] is not None else "Sin valor" for group in groups)
        if kind == "histograma":
            label = f"{row['desde']} a {row['hasta']}"
            x, y = row["desde"], row["frecuencia"]
        elif kind == "dispersion":
            x, y = row["apertura"], row["cierre"]
        else:
            x, y = None, row["valor"]
        if y is None or (kind in {"dispersion", "histograma"} and x is None):
            raise ValueError("Hay valores faltantes en la serie.")
        if not all(math.isfinite(float(value)) for value in (y, x) if value is not None):
            raise ValueError("Hay valores no representables.")
        points.append({"label": label or "Resultado", "x": x, "y": y})
    if len(points) > 60:
        raise ValueError("Reduce el alcance a 60 puntos o menos.")
    if kind == "linea" and len(points) < 2:
        raise ValueError("Se requieren al menos dos cortes para trazar una línea.")
    if kind == "linea":
        start, end = (int(plan[key].replace("-", "")) for key in ("desde", "hasta"))
        if [int(point["label"]) for point in points] != list(range(start, end + 1)):
            raise ValueError("Faltan cortes intermedios. Revisa la cobertura antes de graficar la serie.")
    axis = {
        "histograma": ("Intervalo de importe", "Frecuencia"),
        "dispersion": ("Apertura promedio", "Cierre promedio"),
    }
    x_label, y_label = axis.get(kind, (" / ".join(groups) or "Resultado", plotted_metric.capitalize()))
    period = "Dimensión estática"
    if plan["desde"]:
        period = plan["desde"] if plan["desde"] == plan["hasta"] else f"{plan['desde']} a {plan['hasta']}"
    filters = ", ".join(str(plan[key]) for key in ("producto", "persona", "segmento") if plan[key])
    title = (f"Evolución de {plotted_metric} de {plan['conjunto']}" if metric == "variacion"
             else f"{plotted_metric.capitalize()} de {plan['conjunto']}")
    return {"type": kind, "title": title + (f" · {filters}" if filters else ""),
            "x_label": x_label, "y_label": y_label, "period": period,
            "units": "Frecuencia (registros) por intervalo de importe sin moneda definida" if kind == "histograma" else result["units"],
            "source": result["table"], "citations": result["sources"], "points": points}
