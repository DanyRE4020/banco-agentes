// Prueba de los renderizadores reales con DOM doble y respuestas sintéticas del backend.
// El runner recibe node:vm, fuentes JS, fixtures y node:assert/strict; no usa navegador/red.
async function verifyCreditSummaryDom(vm, assets, fixtures, assert) {
  class Element {
    constructor(tag) {
      this.tagName = tag.toUpperCase(); this.children = []; this.value = ''; this.hidden = false;
      this.open = false; this.style = {}; this.listeners = {}; this.classList = {remove() {}, add() {}, toggle() {}};
      this.ownText = '';
    }
    set textContent(value) {this.ownText = String(value); this.children = [];}
    get textContent() {return this.ownText + this.children.map(child => child.textContent).join(' ');}
    append(...children) {this.children.push(...children);}
    replaceChildren(...children) {this.children = children; this.ownText = '';}
    addEventListener(type, listener) {(this.listeners[type] ||= []).push(listener);}
    setAttribute(name, value) {this[name] = value;}
    focus() {}
  }
  const ids = {};
  const document = {
    createElement: tag => new Element(tag), createTextNode: text => {const n = new Element('text'); n.textContent = text; return n;},
    getElementById: id => ids[id] ||= new Element('div'), querySelectorAll: () => []
  };
  const context = vm.createContext({document, clearInterval() {}, setInterval() {},
    fetch: async () => ({ok: false, status: 401, json: async () => ({detail: 'Sesión sintética de prueba'})}),
    textNode: (tag, text, className) => {const node = document.createElement(tag); node.textContent = text; if (className) node.className = className; return node;}});
  vm.runInContext(assets.responses, context);
  await vm.runInContext(assets.app, context);
  function walk(node) {return [node, ...node.children.flatMap(walk)];}
  function visible(node) {
    if (node.hidden) return '';
    if (node.tagName === 'DETAILS' && !node.open) return node.children[0]?.textContent || '';
    return node.ownText + node.children.map(visible).join(' ');
  }
  const results = [];
  for (const [name, data] of Object.entries(fixtures)) {
    const original = JSON.stringify(data);
    const card = document.createElement('article');
    const answer = document.createElement('div'); answer.textContent = data.answer; card.append(answer);
    context.renderDocumentReport(card, data);
    context.renderResponseExtras(card, data);
    const nodes = walk(card), shown = visible(card);
    assert.ok(shown.startsWith(data.answer), 'El resumen aparece primero');
    assert.ok(!shown.includes('saldo inicial:'), 'Los importes extraídos no se despliegan por defecto');
    assert.ok(!shown.includes('titular:'), 'El titular extraído queda plegado');
    assert.equal((card.textContent.match(/la aprobación corresponde al banco/g) || []).length, 1, 'Aviso preliminar único');
    const folds = nodes.filter(n => n.tagName === 'DETAILS' && n.className === 'document-report');
    assert.equal(folds.length, 4, 'Dos documentos, política y evidencia están plegados');
    assert.ok(folds.every(n => !n.open), 'Los detalles empiezan cerrados');
    const telemetry = nodes.find(n => n.className === 'activity query-details');
    assert.equal(telemetry.tagName, 'DETAILS'); assert.equal(telemetry.open, false);
    assert.equal(telemetry.children[0].textContent, 'Telemetría');
    assert.ok(shown.includes('Página 1 · 4. Requisitos'), 'Cita compacta visible');
    assert.ok(shown.includes('Versión: 1.0'), 'Versión visible');
    assert.ok(shown.includes('Fecha de consulta:'), 'Fecha de la cita visible');
    if (name === 'pending') {
      assert.ok(shown.includes('Campos por confirmar'), 'La confirmación no está oculta');
      assert.ok(shown.includes('fecha vencimiento: Sin valor'), 'Se conserva el campo ilegible');
      assert.ok(shown.includes('Guardar corrección'), 'El formulario sigue visible');
    }
    if (name === 'unused') {
      assert.ok(!card.textContent.includes('Valor aportado por ti utilizado'), 'No se mencionan correcciones sin usar');
      assert.ok(!shown.includes('confirmaste'), 'El resumen no atribuye datos sin usar');
    }
    if (name === 'used') {
      assert.ok(card.textContent.includes('Valor aportado por ti utilizado'), 'La procedencia utilizada sigue accesible');
      assert.ok(shown.includes('datos que confirmaste tú'), 'Se explica el uso de la corrección');
      assert.ok(!shown.includes('Campos por confirmar'), 'No vuelve a pedir un campo ya confirmado');
    }
    assert.equal(JSON.stringify(data), original, 'El renderizador no modifica el JSON detallado');
    results.push({case: name, folds: folds.length, visible_characters: shown.length, ok: true});
  }
  return results;
}
