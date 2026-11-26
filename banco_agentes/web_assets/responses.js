"use strict";

function responseTable(table) {
  const wrapper = document.createElement("div");
  wrapper.className = "response-table chart-table";
  const element = document.createElement("table");
  element.append(textNode("caption", table.title));
  const head = document.createElement("thead");
  const header = document.createElement("tr");
  for (const column of table.columns) {
    const th = textNode("th", column.label);
    th.scope = "col";
    header.append(th);
  }
  head.append(header);
  const body = document.createElement("tbody");
  for (const values of table.rows) {
    const row = document.createElement("tr");
    for (const column of table.columns) {
      const value = String(values[column.key] ?? "");
      // El ranking comparte su tabla original con el PDF.
      const display = column.key === "saldo" && values.alias
        ? displayDecimal(value.replace(/\./g, "").replace(",", "."))
        : value;
      row.append(textNode("td", display));
    }
    body.append(row);
  }
  element.append(head, body);
  wrapper.append(element);
  if (table.truncated) wrapper.append(textNode("p", "La tabla muestra las primeras 24 filas.", "muted"));
  return wrapper;
}

function responseFormat(data) {
  const format = data.presentation?.format;
  return new Set(["frase", "tabla", "prosa", "grafica"]).has(format) ? format : null;
}

function renderResponseVisuals(card, data) {
  const format = responseFormat(data);
  if (format === "grafica" || format === null) renderCharts(card, data.charts);
}

function renderResponseExtras(card, data) {
  if (data.presentation?.permission_only) return;
  const attached = Boolean(data.attachment_view || Array.isArray(data.documents) || data.document_errors);
  if (!attached) {
    for (const table of (data.tables || [])) card.append(responseTable(table));
  }
  const total = data.metrics?.elapsed_seconds ?? data.elapsed_seconds;
  if (Number.isFinite(total) && total >= 0) {
    const digits = total > 0 && total < 0.05 ? 3 : 1;
    card.append(textNode("p", `Respondió en ${total.toFixed(digits)} s`, "response-time"));
  }
  if (data.attachment_view?.references?.length || (attached && !(data.sources || []).length)) return;

  const compact = Boolean(data.documents?.length);
  const sources = document.createElement("details");
  sources.className = "response-sources" + (compact ? " credit-citations" : "");
  sources.append(textNode("summary", "Fuentes consultadas"));
  if (!(data.sources || []).length) {
    sources.append(textNode("p", "Sin fuentes documentales registradas para este turno.", "muted"));
  } else {
    const list = document.createElement("ul");
    for (const source of data.sources) {
      const item = document.createElement("li");
      item.append(textNode("strong", source.label));
      item.append(textNode("div", source.location));
      if (compact && source.details) {
        const info = ["Tipo de documento", "Versión", "Fecha de consulta"].filter(key => source.details[key]);
        if (info.length) item.append(textNode("div", info.map(key => `${key}: ${source.details[key]}`).join(" · ")));
      }
      if (source.details && typeof source.details === "object") {
        const detail = document.createElement("details");
        detail.className = "source-details";
        detail.append(textNode("summary", "Detalle de la fuente"));
        for (const [label, value] of Object.entries(source.details)) {
          detail.append(textNode("p", label + ": " + String(value)));
        }
        item.append(detail);
      }
      if (typeof source.url === "string" && /^https?:\/\//i.test(source.url)) {
        const link = textNode("a", "Abrir publicación");
        link.href = source.url;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        item.append(link);
      }
      list.append(item);
    }
    sources.append(list);
  }
  card.append(sources);
}
