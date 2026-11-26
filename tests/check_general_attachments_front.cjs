// DOM/fetch dobles: ejecuta el JavaScript real sin navegador ni red.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const path = require('node:path');
class Element {
  constructor(tag='div') {this.tagName=tag;this.children=[];this.events={};this.style={};this.classList={add(){},remove(){},toggle(){}};this.value='';this.files=[];this.checked=false;}
  append(...items) {this.children.push(...items);}
  replaceChildren(...items) {this.children=items;}
  addEventListener(name, handler) {(this.events[name]??=[]).push(handler);}
  setAttribute() {}
  focus() {}
  click() {}
}
async function run(root=path.resolve(__dirname,'..'), calculation=null) {
  const nodes={}, requests=[];
  const document={getElementById:id=>nodes[id]??=(new Element()),querySelectorAll:()=>[],createElement:tag=>new Element(tag),createTextNode:text=>({textContent:text})};
  class FormData {constructor(){this.items=[];} append(...item){this.items.push(item);}}
  let chatReply = {answer:'Respuesta bancaria normal',agents:[],tools:[],sources:[],charts:[]};
  async function fetch(url, options={}) {
    if(url==='/api/session') return new Promise(()=>{});
    requests.push({url,...options});
    const body=url==='/api/conversations'?{conversation_id:'chat-new'}:url==='/api/documents'?
      {documents:[{file_id:'f1',archivo:'contrato_bancario.pdf'},{file_id:'f2',archivo:'comprobante.png'}]}:
      chatReply;
    return {ok:true,json:async()=>body};
  }
  const context=vm.createContext({document,fetch,FormData,AbortController,setInterval,clearInterval,performance,console,renderCharts(){},
    textNode:(tag,text)=>{const node=new Element(tag);node.textContent=text;return node;}});
  vm.runInContext(fs.readFileSync(path.join(root,'banco_agentes/web_assets/responses.js'),'utf8'),context);
  const appSource=fs.readFileSync(path.join(root,'banco_agentes/web_assets/app.js'),'utf8');
  assert.ok(!appSource.includes('document-test'),'El flujo no busca la casilla eliminada');
  assert.ok(!fs.readFileSync(path.join(root,'banco_agentes/web_assets/index.html'),'utf8').includes('document-test'));
  vm.runInContext(appSource,context);
  const element=id=>document.getElementById(id);
  element('document-files').files=[{name:'contrato_bancario.pdf'},{name:'comprobante.png'}];
  for(const history of [false,true]) {
    vm.runInContext(`conversationId=${history?'"chat-history"':'null'}; attachments=[];`,context);
    requests.length=0;
    await element('document-files').events.change[0]();
    assert.equal(requests.filter(r=>r.url==='/api/conversations').length,history?0:1);
    const upload=requests.find(r=>r.url==='/api/documents');
    assert.equal(upload.method,'POST');
    assert.ok(!upload.body.items.some(([name])=>name==='synthetic'),'El POST no exige declaración sintética');
    assert.equal(upload.body.items.filter(([name])=>name==='files').length,2);
    assert.equal(upload.body.items.find(([name])=>name==='conversation_id')[1],history?'chat-history':'chat-new');
    assert.equal(element('attachments').children.length,2);
    assert.ok(element('attachments').children.every(li=>li.children[0].children[0].checked));
  }
  vm.runInContext('attachments.forEach(item=>item.selected=false)',context);
  element('question').value='¿Qué productos ofrece el banco?';
  await element('chat-form').events.submit[0]({preventDefault(){}});
  const question=JSON.parse(requests.find(r=>r.url==='/api/chat').body);
  assert.deepEqual(question.document_ids,[]);
  const allText=node=>(node.textContent||'')+(node.children||[]).map(allText).join(' ');
  if (calculation) {
    chatReply = calculation;
    element('question').value = calculation.question;
    await element('chat-form').events.submit[0]({preventDefault(){}});
    const answer = element('messages').children.at(-1).children.find(node => node.className === 'answer pending');
    assert.ok(answer, 'El chat debe crear el bloque de respuesta');
    const visible = answer.children.map(allText).join('\n\n');
    assert.equal(visible, calculation.answer);
    for (const product of ['CHEQUES', 'AHORRO']) {
      const detail = visible.split('\n\n').find(part => part.startsWith(product + '.'));
      assert.match(detail, /Saldo inicial \(enero de 2026\): [\d.,]+\. Saldo final \(junio de 2026\): [\d.,]+\. \([\d.,]+ − [\d.,]+\) \/ [\d.,]+ × 100 = 6\.14%\./);
    }
  }
  for(const role of ['Cliente','Analista']) {
    const card=new Element();context.card=card;context.testRole=role;
    vm.runInContext(`profile=testRole; renderDocumentReport(card, {documents:[{id:'D1',campos:{curp:{valor:'GODE561231HDFRRN09'}}}],attachment_view:{requirements:[{label:'Vigencia de identificación',status:'cumple',reason:'El documento está vigente.'}],confirmations:[],references:['identificacion.png']}}); renderResponseExtras(card,{documents:[{id:'D1'}],agents:['orquestador','data_steward'],tools:['leer_documento','recuperar_politica_documental'],total_seconds:8,query_details:'GODE561231HDFRRN09 domicilio privado',metrics:{elapsed_seconds:9.125,calls:[{agent:'leer_documento',seconds:3.229},{agent:'orquestador',seconds:1.020}],total_tokens:99,reader_diagnostics:{private:'domicilio privado'}}})`,context);
    assert.equal(card.children[0].tagName,'section');
    assert.equal(card.children.length,3);
    const details=card.children[1];
    assert.equal(details.tagName,'details');
    assert.ok(!details.open,'Las fuentes empiezan plegadas');
    assert.match(allText(details), /Fuentes consultadas.*identificacion.png/);
    assert.equal(allText(card.children[2]), 'Respondió en 9.1 s');
    for(const forbidden of ['Detalles de la consulta', 'Agentes:', 'Herramientas:', 'Llamada al modelo', 'Tokens']) {
      assert.ok(!allText(card).includes(forbidden));
    }
    assert.ok(!allText(card).includes('domicilio privado'));
    assert.ok(allText(card).includes('cumple'));
    assert.ok(allText(card).includes('El documento está vigente.'));
    for(const forbidden of ['D1','R1','confianza','Telemetría','Tokens','Campos extraídos','GODE561231HDFRRN09']) assert.ok(!allText(card).includes(forbidden));
    const review=new Element();context.review=review;
    vm.runInContext(`renderDocumentReport(review,{documents:[{id:'D2',campos:{domicilio:{valor:'domicilio privado'}}}],policy:{extractos:[{texto:'Este documento no indica cómo se calcula'}]},attachment_view:{style:'client_recognition',opening:'Reconocí estos documentos que subiste',document_names:['INE','recibo de luz','recibo de nómina'],messages:['No falta ninguno de los tipos de documentos solicitados que pude recuperar de la política.'],policy_title:'Requisitos para solicitar Tarjeta de Crédito Clásica',requirements:[{label:'Neto a pagar',status:'aportada',reason:'Límite de pago'}],policy_requirements:[{text:'Presentar identificación oficial vigente.',source_indices:[2]},{text:'Presentar comprobante de domicilio.',source_indices:[2]},{text:'Contar con ingresos comprobables.',source_indices:[2]}],closing:'El banco debe validar los requisitos y evaluar la solicitud.',references:['ejemplo.pdf, página 1','Política ficticia, página 2 · versión v1']}});renderResponseExtras(review,{documents:[{id:'D2'}],agents:['orquestador','data_steward'],tools:['leer_documento','recuperar_politica_documental'],metrics:{elapsed_seconds:1.25,calls:[{agent:'leer_documento',seconds:0.75}]}})`,context);
    assert.equal(review.children[0].textContent,'Reconocí estos documentos que subiste');
    assert.equal(review.children[1].tagName,'ul');assert.equal(review.children[1].children.length,3);
    assert.ok(allText(review.children[2]).includes('No falta ninguno'));
    assert.ok(allText(review.children[3]).includes('Requisitos para solicitar Tarjeta de Crédito Clásica'));
    assert.equal(review.children[3].children[1].tagName,'ul');
    assert.equal(review.children[3].children[1].children.length,3);
    assert.ok(review.children[3].children[1].children.every(item=>item.tagName==='li' && allText(item).includes('[2]')));
    assert.equal(allText(review).split('El banco debe validar').length-1,1);
    assert.ok(allText(review.children[5]).includes('Fuentes consultadas'));
    assert.ok(allText(review.children[5]).includes('[2] Política ficticia, página 2'));
    assert.ok(!allText(review.children[3]).includes('Política ficticia'));
    assert.ok(!allText(review).includes('Este documento no indica cómo se calcula'));
    assert.equal(review.children[5].tagName,'details');assert.ok(!review.children[5].open);
    assert.equal(allText(review.children[6]), 'Respondió en 1.3 s');
    for(const forbidden of ['D2','domicilio privado','cumple','Campos extraídos','confianza','aportada','Límite de pago','Neto a pagar']) assert.ok(!allText(review).includes(forbidden));
    const failed = new Element();
    context.failed = failed;
    context.failedView = {
      style: 'client_recognition', opening: '', document_names: [],
      messages: ['recibo.png está pendiente de lectura.', 'nomina.jpg está pendiente de lectura.', 'No pude determinar qué documentos faltan.'],
      policy_title: 'Requisitos para solicitar Tarjeta de Crédito Clásica',
      policy_requirements: [{text: 'Presentar identificación oficial vigente.', source_indices: [1]}],
      closing: 'El banco debe validar los requisitos y evaluar la solicitud.',
      references: ['Política ficticia, página 2'],
    };
    vm.runInContext('renderDocumentReport(failed, {attachment_view: failedView})', context);
    assert.deepEqual(failed.children.slice(0, 3).map(node => node.textContent), context.failedView.messages);
    assert.ok(failed.children.every(node => node.tagName !== 'ul' && allText(node)));
    assert.match(allText(failed), /Presentar identificación oficial vigente\. \[1\]/);
    assert.match(allText(failed), /\[1\] Política ficticia, página 2/);
    assert.doesNotMatch(allText(failed), /Reconocí/);
  }
  const empty=new Element();context.empty=empty;
  vm.runInContext(`renderResponseExtras(empty,{documents:[],agents:[],tools:[]})`,context);
  assert.equal(empty.children.length, 0);
  return {new_chat_multiselect:true,history_multiselect:true,deselected_normal_chat:true,simple_requirements_both_profiles:true,no_checkbox_or_declaration:true,total_time_only_both_profiles:true,missing_metrics_not_invented:true,document_review_then_policy_both_profiles:true,irrelevant_fragment_hidden_with_page_citations_and_details:true,failed_attachments_pending_with_policy_both_profiles:true};
}
module.exports=run;
if(require.main===module) {
  const calculation = process.argv[2] ? JSON.parse(fs.readFileSync(process.argv[2],'utf8')) : null;
  run(undefined, calculation).then(result=>console.log(JSON.stringify({...result, ...(calculation ? {inline_calculation_visible:true} : {})}))).catch(error=>{console.error(error);process.exitCode=1;});
}
