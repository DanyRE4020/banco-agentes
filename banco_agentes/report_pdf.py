"""Composición local del PDF a partir de resultados y citas verificados."""
import io
import math
from html import escape

from .presentation import decimal_text, month_text

CSS = """
body {font-family: sans-serif; font-size: 10.5pt; color: #172b42; line-height: 1.4}
h1 {font-size: 29pt; color: #102c4d; margin: 10pt 0 7pt}
h2 {font-size: 17pt; color: #102c4d; margin: 13pt 0 6pt}
h3 {font-size: 12.5pt; color: #17676c; margin: 11pt 0 4pt}
p {margin: 0 0 9pt}
.kicker {font-size: 9pt; color: #17676c; font-weight: bold; letter-spacing: 0.9pt}
.meta, .caption {color: #42556b; font-size: 9pt}
table {width: 100%; border-collapse: collapse; margin: 4pt 0 10pt; font-size: 9pt}
th {color: #102c4d; text-align: left}
td, th {border: 1px solid #cddce5; padding: 4pt}
li {margin-bottom: 4pt}
.next-visual {page-break-before: always}
"""


def references(analyses):
    items = {}
    numbers = []
    for analysis in analyses:
        used = []
        for source in analysis.sources:
            details = {**source.get("details", {})}
            origin = details.get("Archivo") or source.get("url")
            key = (source["label"], origin or source.get("location"))
            if key not in items:
                items[key] = (len(items) + 1, dict(source, locations=set(), details=details))
            number, entry = items[key]
            if source.get("location"):
                entry["locations"].add(source["location"])
            entry["details"].update(details)
            if number not in used:
                used.append(number)
        numbers.append(used)
    return [entry for _, entry in items.values()], numbers


def period_label(analyses):
    periods = sorted({month for analysis in analyses for month in analysis.periods})
    if not periods:
        return "Sin cortes temporales en los análisis seleccionados"
    return (month_text(periods[0]).capitalize() if len(periods) == 1
            else f"{month_text(periods[0]).capitalize()} a {month_text(periods[-1])}")


def table_html(table):
    columns = table["columns"]
    headings = "".join(f"<th>{escape(col['label'])}</th>" for col in columns)
    rows = "".join("<tr>" + "".join(f"<td>{escape(str(row[col['key']]))}</td>" for col in columns) + "</tr>"
                   for row in table["rows"])
    note = '<p class="caption">Vista parcial de las filas del análisis.</p>' if table.get("truncated") else ""
    return f"<table><tr>{headings}</tr>{rows}</table>{note}"


def chart_png(fitz, chart):
    """Usa las coordenadas de Graph. Los valores ausentes dejan espacios en la serie."""
    kind = chart["type"]
    if kind not in {"linea", "barras"}:
        return None
    series = chart.get("series")
    if series:
        cuts = series[0]["x"]
        labels = [month_text(cut).split(" de ", 1)[0] if len(cut) == 7 and cut[4] == "-" else cut
                  for cut in cuts]
        blocks = [(block["name"], block["color"], block["y"]) for block in series]
    else:
        labels = [point["label"] for point in chart["points"]]
        blocks = [("", "#17676c", [point["y"] for point in chart["points"]])]
    values = [float(value) for _, _, points in blocks for value in points if value is not None]
    if not values or len(labels) > 12 or any(not math.isfinite(n) for n in values):
        return None
    low, high = min(0, *values), max(0, *values)
    span = high - low or 1
    low, high = low - (span * .15 if low < 0 else 0), high + span * .17
    color = (.24, .33, .42)
    with fitz.open() as doc:
        page = doc.new_page(width=500, height=200)
        left, right, top, bottom = 75, 487, 30, 155
        unit = chart.get("units")
        caption = chart["title"] + (f" · {unit}" if unit else "")
        page.insert_text((5, 14), caption, fontsize=10.5, color=color)
        width = (right - left) / len(labels)
        def locate(value):
            return bottom - (float(value) - low) / (high - low) * (bottom - top)
        for tick in range(4):
            number = low + (high - low) * tick / 3
            y = locate(number)
            page.draw_line((left, y), (right, y), color=(.80, .85, .89), width=.6)
            page.insert_text((5, y + 3), decimal_text(f"{number:.2f}"), fontsize=8, color=color)
        for i, label in enumerate(labels):
            center = left + (i + .5) * width
            page.insert_textbox(fitz.Rect(center-width/2, bottom+4, center+width/2, bottom+28),
                                str(label), fontsize=8, align=1, color=color)
        for j, (name, hex_color, points) in enumerate(blocks):
            rgb = tuple(int(hex_color[i:i+2], 16) / 255 for i in (1, 3, 5))
            size = width * .74 / len(blocks)
            previous = None
            for i, value in enumerate(points):
                if value is None:
                    previous = None
                    continue
                x, y = left + (i + .5) * width, locate(value)
                if kind == "linea":
                    page.draw_circle((x, y), 2.8, color=rgb, fill=rgb)
                    if previous is not None:
                        page.draw_line(previous, (x, y), color=rgb, width=1.7)
                    previous = (x, y)
                else:
                    x += (j - (len(blocks)-1)/2) * size
                    zero = locate(0)
                    if abs(y-zero) > .5:
                        page.draw_rect((x-size*.44, min(zero,y), x+size*.44, max(zero,y)),
                                       color=None, fill=rgb)
                    if len(labels) == 1 or (len(blocks) == 1 and len(labels) <= 6):
                        page.insert_textbox((x-size*.7, max(y-15, 0), x+size*.7, max(y-15, 0)+14),
                                            decimal_text(value), fontsize=8, align=1, color=color)
            if name:
                x = left + j * 98
                page.draw_rect((x, 184, x+9, 191), color=None, fill=rgb)
                page.insert_text((x+13, 191), name, fontsize=8.5, color=color)
        return page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False).tobytes("png")


def report_charts(analyses):
    """Conserva la gráfica del chat cuando el alcance incluye toda su evidencia."""
    positions = {key: index for index, analysis in enumerate(analyses)
                 if analysis.topic == "saldos" for key in analysis.ids}
    figures = [analysis.charts for analysis in analyses]
    used = set()
    for analysis in analyses:
        for chart in analysis.shared_charts:
            ids = tuple(chart["result_ids"])
            if ids in used or not set(ids) <= positions.keys():
                continue
            used.add(ids)
            for index in {positions[key] for key in ids}:
                figures[index] = tuple(c for c in figures[index] if set(c["result_ids"]) - set(ids))
            index = max(positions[key] for key in ids)
            figures[index] = (*figures[index], chart)
    return figures


def render(analyses, paragraphs):
    import fitz

    summary, *findings, conclusion = paragraphs
    refs, links = references(analyses)
    archive = fitz.Archive()
    parts = ['<p class="kicker">BANCO AGENTES · DATOS SINTÉTICOS</p>',
             "<h1>Reporte ejecutivo</h1>",
             f'<p class="meta">Período del reporte · {escape(period_label(analyses))}</p>',
             "<h2>Resumen ejecutivo</h2>", f"<p>{escape(summary)}</p>", "<h2>Hallazgos</h2>"]
    visual_sections = 0
    for i, (analysis, prose, numbers, figures) in enumerate(
            zip(analyses, findings, links, report_charts(analyses), strict=True), 1):
        citation = " · ".join(f"[{n:02d}]" for n in numbers)
        period = period_label((analysis,)) if analysis.periods else "Consulta sin filtro temporal"
        if analysis.partial:
            period += " · período o cobertura parcial"
        visual_sections += bool(figures)
        class_name = ' class="next-visual"' if figures and visual_sections == 2 else ""
        parts.extend([f"<h3{class_name}>{escape(analysis.title)}</h3>",
                      f'<p class="meta">{escape(period)} · {escape(citation)}</p>', f"<p>{escape(prose)}</p>"])
        parts.extend(table_html(table) for table in analysis.tables)
        for j, chart in enumerate(figures, 1):
            png = chart_png(fitz, chart)
            if png:
                name = f"figura-{i}-{j}.png"
                archive.add((png, name))
                parts.append(f'<p class="caption"><img src="{name}" width="425"></p>')
    parts.extend(["<h2>Conclusión</h2>", f"<p>{escape(conclusion)}</p>"])
    if any(a.topic in {"evolucion", "saldos", "movimientos"} for a in analyses):
        parts.append("<p>La evidencia permite describir los cambios observados. "
                     "La causa de esos cambios queda por determinar.</p>")
    if any(a.topic in {"saldos", "evolucion", "movimientos", "ranking"} for a in analyses):
        parts.append('<p class="meta">Los importes de la base sintética se expresan sin moneda asignada.</p>')
    parts.append("<h2>Referencias</h2><ol>")
    for entry in refs:
        details = [*sorted(entry["locations"]), *(value for key, value in entry["details"].items()
                   if key != "Tipo de documento")]
        if entry.get("url"):
            details.append(entry["url"])
        parts.append(f"<li><strong>{escape(entry['label'])}</strong> · "
                     f"{escape(' · '.join(dict.fromkeys(str(item) for item in details)))}</li>")
    parts.append("</ol>")

    page = fitz.paper_rect("a4")
    content = fitz.Rect(49, 49, page.width - 49, page.height - 54)
    buffer = io.BytesIO()
    writer = fitz.DocumentWriter(buffer)
    fitz.Story(html="".join(parts), user_css=CSS, archive=archive).write(writer, lambda *_: (page, content, None))
    writer.close()
    with fitz.open(stream=buffer.getvalue(), filetype="pdf") as doc:
        doc.set_metadata({"title": "Reporte ejecutivo — Banco Agentes"})
        for number, sheet in enumerate(doc, 1):
            sheet.draw_line((49, page.height-38), (page.width-49, page.height-38), color=(.80, .85, .89))
            sheet.insert_text((49, page.height-24), f"Banco Agentes | {number} / {len(doc)}",
                              fontsize=8, color=(.24, .33, .42))
        return doc.tobytes(garbage=4, deflate=True)
