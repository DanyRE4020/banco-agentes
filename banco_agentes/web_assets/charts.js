"use strict";

const allowedCharts = new Set(["barras", "linea", "histograma", "dispersion"]);
const chartTextFields = ["title", "x_label", "y_label", "period", "units", "source"];
const svgNamespace = "http://www.w3.org/2000/svg";

function boundedText(value, maxLength) {
  return typeof value === "string" && value.length > 0 && value.length <= maxLength;
}

function numeric(value) {
  return (typeof value === "number" ||
    (typeof value === "string" && /^-?\d+(?:\.\d+)?(?:e[+-]?\d+)?$/i.test(value))) &&
    Number.isFinite(Number(value)) && Math.abs(Number(value)) <= 1e24;
}

function validChart(chart) {
  if (!chart || !allowedCharts.has(chart.type)) return false;
  if (!chartTextFields.every(field => boundedText(chart[field], 250))) return false;
  if (!Array.isArray(chart.citations) || !chart.citations.length || chart.citations.length > 8 ||
      !chart.citations.every(citation => boundedText(citation, 350))) return false;
  if (!Array.isArray(chart.points) || !chart.points.length || chart.points.length > 60) return false;
  if (chart.schema_version === 2) {
    if (!["barras", "linea"].includes(chart.type) || !Array.isArray(chart.series) || !chart.series.length) return false;
    const categories = chart.series[0]?.x;
    const purchases = ["trimestre", "anio"].includes(chart.purchase_group);
    if (purchases && chart.type !== "barras") return false;
    return Array.isArray(categories) && categories.length * chart.series.length <= 60 &&
      categories.length >= (chart.type === "linea" ? 2 : 1) &&
      categories.every(label => purchases ? boundedText(label, 100) :
        typeof label === "string" && /^\d{4}-(0[1-9]|1[0-2])$/.test(label)) &&
      chart.series.every(series => series && /^[A-Z_]+$/.test(series.name) &&
        /^#[a-f0-9]{6}$/i.test(series.color) && Array.isArray(series.x) &&
        series.x.length === categories.length && series.x.every((label, i) => label === categories[i]) &&
        Array.isArray(series.y) && series.y.length === categories.length &&
        series.y.every(value => value === null || numeric(value)));
  }
  if (chart.type === "linea" && chart.points.length < 2) return false;
  const numericalX = ["dispersion", "histograma"].includes(chart.type);
  return chart.points.every(point => point && boundedText(point.label, 180) &&
    numeric(point.y) && (numericalX ? numeric(point.x) : point.x === null) &&
    (chart.type !== "histograma" || (Number.isInteger(Number(point.y)) && Number(point.y) >= 0)));
}

function svgNode(name, attrs, text) {
  const node = document.createElementNS(svgNamespace, name);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
  if (text !== undefined) node.textContent = text;
  return node;
}

function textNode(name, text, className) {
  const node = document.createElement(name);
  node.textContent = text;
  if (className) node.className = className;
  return node;
}

const tickFormat = new Intl.NumberFormat("es-MX", {maximumSignificantDigits: 5});

function drawChart(chart) {
  const plot = svgNode("svg", {viewBox: "0 0 680 320", role: "img",
    "aria-label": `${chart.title}. ${chart.x_label} y ${chart.y_label}. Consulta la tabla de valores.`});
  const left = 93, right = 650, top = 18, bottom = 245;
  const bars = chart.type === "barras" || chart.type === "histograma";
  const scatter = chart.type === "dispersion";
  const values = chart.points.map(point => Number(point.y));
  let low = Math.min(...values), high = Math.max(...values);
  if (bars) { low = Math.min(0, low); high = Math.max(0, high); }
  if (low === high) high += Math.abs(low) * 0.05 || 1;
  const y = value => bottom - ((value - low) / (high - low)) * (bottom - top);
  const xs = scatter ? chart.points.map(point => Number(point.x)) : [];
  const lowX = scatter ? Math.min(...xs) : 0;
  const highX = scatter ? Math.max(...xs) : 0;
  const step = (right - left) / chart.points.length;
  const barWidth = step * (chart.type === "histograma" ? 1 : 0.7);
  const x = (point, index) => {
    if (scatter) {
      const span = highX - lowX;
      return span ? left + 10 + (Number(point.x) - lowX) / span * (right - left - 20) : (left + right) / 2;
    }
    return left + (index + 0.5) * step;
  };
  for (let tick = 0; tick <= 4; tick++) {
    const value = low + (high - low) * tick / 4;
    const level = y(value);
    plot.append(svgNode("line", {x1: left, x2: right, y1: level, y2: level, class: "plot-grid"}),
      svgNode("text", {x: left - 9, y: level + 4, "text-anchor": "end"}, tickFormat.format(value)));
  }
  if (chart.type === "linea") {
    const points = chart.points.map((point, i) => `${x(point, i)},${y(Number(point.y))}`).join(" ");
    plot.append(svgNode("polyline", {points, class: "plot-line"}));
  }
  chart.points.forEach((point, index) => {
    const center = x(point, index), ordinate = y(Number(point.y));
    const mark = bars
      ? svgNode("rect", {x: center - barWidth / 2, y: Math.min(ordinate, y(0)), width: barWidth,
        height: Math.abs(ordinate - y(0)), rx: chart.type === "barras" ? 3 : 0, class: "plot-bar"})
      : svgNode("circle", {cx: center, cy: ordinate, r: 4.5, class: "plot-dot"});
    mark.append(svgNode("title", {}, `${point.label}: ${point.y}`));
    plot.append(mark);
    if (!scatter && index % Math.ceil(chart.points.length / 7) === 0) {
      const label = chart.type === "histograma" ? tickFormat.format(Number(point.x)) : point.label;
      plot.append(svgNode("text", {x: center, y: bottom + 19, "text-anchor": "middle"}, label.slice(0, 16)));
    }
  });
  if (scatter) {
    for (let tick = 0; tick <= (lowX === highX ? 0 : 4); tick++) {
      const value = lowX + (highX - lowX) * tick / 4;
      plot.append(svgNode("text", {x: x({x: value}), y: bottom + 19, "text-anchor": "middle"}, tickFormat.format(value)));
    }
  }
  plot.append(svgNode("text", {x: (left + right) / 2, y: 301, "text-anchor": "middle"}, chart.x_label),
    svgNode("text", {transform: "translate(18 130) rotate(-90)", "text-anchor": "middle"}, chart.y_label));
  return plot;
}

function displayDecimal(value) {
  if (value === null || value === undefined) return "Sin valor";
  const text = String(value);
  if (!/^-?\d+(?:\.\d+)?$/.test(text)) return text;
  const [integer, fraction] = text.split(".");
  const whole = integer.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  return whole + (fraction === undefined ? "" : "." + fraction);
}

function chartTable(chart) {
  const table = document.createElement("table");
  const caption = textNode("caption", `Valores de ${chart.title}`);
  const header = document.createElement("thead");
  const titles = document.createElement("tr");
  const includeX = chart.type === "dispersion";
  const labels = includeX ? ["Grupo", chart.x_label, chart.y_label] : [chart.x_label, chart.y_label];
  for (const label of labels) {
    const th = textNode("th", label);
    th.scope = "col";
    titles.append(th);
  }
  header.append(titles);
  const body = document.createElement("tbody");
  const points = chart.schema_version === 2
    ? chart.series.flatMap(series => series.x.map((month, i) => ({label: `${month} · ${series.name}`, y: series.y[i]})))
    : chart.points;
  for (const point of points) {
    const row = document.createElement("tr");
    const label = textNode("th", point.label);
    label.scope = "row";
    row.append(label);
    if (includeX) row.append(textNode("td", displayDecimal(point.x)));
    row.append(textNode("td", displayDecimal(point.y) + (point.y !== null && chart.units === "%" ? "%" : "")));
    body.append(row);
  }
  table.append(caption, header, body);
  const wrapper = document.createElement("div");
  wrapper.className = "chart-table";
  wrapper.append(table);
  return wrapper;
}

function drawBalanceChart(chart) {
  const plot = document.createElement("div");
  plot.className = "balance-plot";
  plot.setAttribute("role", "group");
  plot.setAttribute("aria-label", `${chart.title}. Consulta la tabla para conocer los valores.`);
  const unavailable = () => {
    plot.replaceChildren(textNode("p", "Plotly no pudo cargar la gráfica. Los datos están en la tabla.", "muted"));
  };
  if (!globalThis.Plotly) {
    unavailable();
    return plot;
  }
  const bars = chart.type === "barras";
  const data = chart.series.map(series => ({
    type: bars ? "bar" : "scatter",
    name: series.name,
    x: series.x,
    y: series.y.map(value => value === null ? null : Number(value)),
    customdata: series.y.map(value => displayDecimal(value) + (value !== null && chart.units === "%" ? "%" : "")),
    marker: {color: series.color},
    ...(bars ? {offsetgroup: series.name} : {mode: "lines+markers", line: {color: series.color}, connectgaps: false}),
    hovertemplate: "%{x}<br>%{customdata}<extra>%{fullData.name}</extra>",
  }));
  const months = chart.series[0].x;
  const layout = {
    ...(bars ? {barmode: "group"} : {}),
    xaxis: {title: {text: chart.x_label}, type: "category", categoryorder: "array", categoryarray: months, automargin: true},
    yaxis: {title: {text: chart.y_label}, automargin: true, ...(chart.units === "%" ? {ticksuffix: "%"} : {})},
    showlegend: true,
    legend: {orientation: "h", traceorder: "normal", x: 0, xanchor: "left", y: 1.02, yanchor: "bottom"},
    margin: {t: 54, b: 66, l: 65, r: 20},
    paper_bgcolor: "rgba(0,0,0,0)",
    plot_bgcolor: "rgba(0,0,0,0)",
  };
  // Plotly necesita calcular dimensiones cuando el contenedor ya está en el chat.
  queueMicrotask(() => {
    try {
      Promise.resolve(globalThis.Plotly.newPlot(plot, data, layout, {responsive: true, displayModeBar: false}))
        .catch(unavailable);
    } catch {
      unavailable();
    }
  });
  return plot;
}

function renderCharts(container, charts) {
  if (!Array.isArray(charts)) return;
  for (const chart of charts.slice(0, 2)) {
    if (!validChart(chart)) {
      container.append(textNode("p", "No se pudo representar esta gráfica.", "muted"));
      continue;
    }
    const figure = document.createElement("figure");
    figure.className = "chat-chart";
    const caption = document.createElement("figcaption");
    caption.append(textNode("strong", chart.title),
      textNode("p", `${chart.period} · ${chart.units}`, "muted"));
    const values = document.createElement("details");
    values.append(textNode("summary", "Ver tabla de valores"), chartTable(chart));
    figure.append(caption, chart.schema_version === 2 ? drawBalanceChart(chart) : drawChart(chart), values);
    container.append(figure);
  }
}
