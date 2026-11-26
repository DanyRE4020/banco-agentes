// Consume respuestas de la API de test_graph.py y ejecuta la presentación web real.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

class Element {
  constructor(tagName) {
    this.tagName = tagName;
    this.children = [];
  }
  append(...children) { this.children.push(...children); }
  setAttribute() {}
  replaceChildren(...children) { this.children = children; }
}

const root = path.resolve(__dirname, '..');
const render = [];
const context = vm.createContext({
  document: {createElement: name => new Element(name)},
  queueMicrotask: renderFunction => render.push(renderFunction),
  Plotly: {newPlot: (element, data, layout) => {
    assert.ok(element.inChat);
    calls.push(JSON.parse(JSON.stringify({data, layout})));
    return Promise.resolve();
  }},
});
const calls = [];
const payloads = process.argv.slice(2).flatMap(file => JSON.parse(fs.readFileSync(file, 'utf8')));
for (const name of ['charts.js', 'responses.js']) {
  vm.runInContext(fs.readFileSync(path.join(root, 'banco_agentes/web_assets', name), 'utf8'), context);
}
const text = node => [node.textContent || '', ...node.children.map(text)].join(' ');
for (const payload of payloads) {
  const card = new Element('article');
  Object.assign(context, {card, payload});
  vm.runInContext('renderResponseVisuals(card, payload); renderResponseExtras(card, payload);', context);
  assert.equal(card.children[0].tagName, 'figure');
  card.children[0].children[1].inChat = true;
  assert.equal(render.length, 1);
  render.shift()();
  const details = card.children.find(node => node.className === 'activity query-details');
  assert.equal(details, undefined);
  const total = card.children.find(node => node.className === 'response-time');
  assert.match(text(total), /Respondió en \d+\.\d+ s/);
  const sources = card.children.find(node => node.className === 'response-sources');
  assert.match(text(sources), /Fuentes consultadas/);
  assert.ok(!sources.open);
  assert.match(text(sources), /fact_sdos_captacoloca_m/);
  const input = payload.charts[0];
  const figure = calls.at(-1);
  for (const [index, series] of input.series.entries()) {
    const plotted = figure.data[index];
    assert.equal(plotted.name, series.name);
    assert.deepEqual(plotted.x, series.x);
    assert.deepEqual(plotted.y, series.y.map(value => value === null ? null : Number(value)));
  }
  assert.equal(figure.layout.showlegend, true);
  assert.equal(figure.layout.legend.yanchor, 'bottom');
  assert.ok(figure.layout.legend.y > 1);
}

const [grouped, lines, changed, june, added, repeated, missing] = calls;
assert.equal(grouped.layout.barmode, 'group');
assert.deepEqual(grouped.data.map(trace => trace.name), ['CHEQUES', 'TDC', 'AHORRO']);
assert.deepEqual(grouped.layout.xaxis.categoryarray, ['2026-01','2026-02','2026-03','2026-04','2026-05','2026-06']);
assert.ok(grouped.data.every(trace => trace.type === 'bar' && trace.x.length === 6 && !trace.y.includes(null)));
assert.equal(missing.data[1].y[2], null);
assert.equal(new Set(grouped.data.map(trace => trace.marker.color)).size, 3);
assert.ok(lines.data.every(trace => trace.type === 'scatter' && trace.mode === 'lines+markers' && !trace.connectgaps));
assert.equal(lines.data.length, 2);
assert.equal(changed.layout.barmode, 'group');
assert.deepEqual(changed.data.map(trace => trace.y), lines.data.map(trace => trace.y));
assert.equal(june.data.length, 2);
assert.deepEqual(added.data.map(trace => trace.name), ['CHEQUES','TDC','AHORRO']);
assert.deepEqual(added.layout.xaxis.categoryarray, ['2026-06']);
assert.deepEqual(added, repeated);
for (const trace of added.data) {
  const previous = june.data.find(item => item.name === trace.name);
  if (previous) assert.deepEqual(trace.y, previous.y);
  assert.equal(trace.marker.color, grouped.data.find(item => item.name === trace.name).marker.color);
}
if (process.argv[3]) {
  assert.equal(calls.length, 9);
  const [period, monthly] = calls.slice(7);
  assert.equal(period.layout.barmode, 'group');
  assert.deepEqual(period.data.map(series => series.y), [[6.14], [6.14]]);
  assert.ok(monthly.data.every(series => series.type === 'scatter' && !series.connectgaps && series.y[0] === null));
  for (const chart of [period, monthly]) {
    assert.equal(chart.layout.yaxis.ticksuffix, '%');
    assert.equal(chart.layout.yaxis.title.text, 'Variación (%)');
    assert.ok(chart.data.every(series => series.customdata.some(value => value.endsWith('%'))));
  }
  console.log('Dos gráficas porcentuales verificadas con un doble de Plotly.');
} else {
  assert.equal(calls.length, 7);
  console.log('Siete respuestas verificadas en la interfaz con un doble de Plotly.');
}
