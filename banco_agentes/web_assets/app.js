const $ = id => document.getElementById(id);
let profile = null, controller = null, conversationId = null;
let attachments = [], uploading = false;
async function api(path, body, signal) {
  const response = await fetch(path, {method: body === undefined ? 'GET' : 'POST', credentials: 'same-origin', headers: body === undefined ? {} : {'Content-Type':'application/json'}, body: body === undefined ? undefined : JSON.stringify(body), signal});
  const data = await response.json();
  if (!response.ok) {const error = new Error(typeof data.detail === 'string' ? data.detail : 'Revisa los datos enviados.'); error.status = response.status; throw error;}
  return data;
}
function showLogin() {
  controller?.abort(); controller = null;
  profile = null; conversationId = null; $('new-chat').disabled = false; $('messages').replaceChildren(); $('question').value = ''; $('send').disabled = false;
  $('chat-view').hidden = true; $('login-view').hidden = false; $('password').value = ''; $('welcome').hidden = false;
  setPasswordVisible(false);
  attachments = []; renderAttachments();
}
function showChat(value) {
  profile = value; $('profile-name').textContent = value; $('avatar').textContent = value[0];
  $('login-view').hidden = true; $('chat-view').hidden = false;
  const choices = [value === 'Cliente' ? '¿Cuál era el saldo de mi cuenta de ahorro al cierre de junio de 2026?' : '¿Cuál fue el saldo total de AHORRO al cierre de junio de 2026?', '¿Qué significa NCLAMTS?', value === 'Cliente' ? '¿Qué documentos necesito para solicitar Crédito Nómina?' : '¿Hay registros duplicados de saldos por cuenta al cierre de junio de 2026?'];
  const labels = [value === 'Cliente' ? 'Consulta mi saldo' : 'Explora el saldo total', 'Entiende una variable', value === 'Cliente' ? 'Revisa documentos públicos' : 'Revisa la calidad de los datos'];
  $('suggestions').replaceChildren(...choices.map((text, index) => {
    const button = document.createElement('button');
    button.type = 'button';
    const icon = document.createElement('span');
    icon.textContent = ['↗', 'Aa', '◎'][index];
    icon.setAttribute('aria-hidden', 'true');
    const label = document.createElement('strong');
    label.textContent = labels[index];
    const hint = document.createElement('small');
    hint.textContent = 'Usar esta pregunta ↗';
    button.append(icon, label, hint);
    button.addEventListener('click', () => {$('question').value = text; $('question').focus(); resize();});
    return button;
  }));
  $('question').focus();
}
function setPasswordVisible(visible) {
  $('password').type = visible ? 'text' : 'password';
  const button = $('show-password');
  button.textContent = visible ? 'Ocultar' : 'Mostrar';
  button.setAttribute('aria-label', visible ? 'Ocultar contraseña' : 'Mostrar contraseña');
}
$('show-password').addEventListener('click', () => setPasswordVisible($('password').type === 'password'));
$('login-form').addEventListener('submit',async event=>{event.preventDefault();$('login-button').disabled=true;$('login-error').textContent='';try{const data=await api('/api/login',{username:$('username').value,password:$('password').value});$('password').value='';showChat(data.profile);}catch(error){$('login-error').textContent=error.message;}finally{$('login-button').disabled=false;}});
$('logout').addEventListener('click',async()=>{try{await api('/api/logout',{});showLogin();}catch(error){alert('No se pudo cerrar la sesión. Inténtalo de nuevo.');}});
$('new-chat').addEventListener('click', () => {
  if (controller || uploading) return;
  conversationId = null;
  attachments = []; renderAttachments();
  $('messages').replaceChildren();
  $('welcome').hidden = false;
  $('question').value = '';
  resize();
  $('question').focus();
});
function resize(){const field=$('question');field.style.height='auto';field.style.height=Math.min(field.scrollHeight,160)+'px';}
$('question').addEventListener('input',resize);
$('question').addEventListener('keydown',event=>{if(event.key==='Enter'&&!event.shiftKey&&!event.isComposing){event.preventDefault();if(!$('send').disabled)$('chat-form').requestSubmit();}});
function scroll(){ $('conversation').scrollTop=$('conversation').scrollHeight; }
function appendAnswerLinks(container,text){
  // Texto del modelo nunca se interpreta como HTML. Solo enlaces HTTP(S).
  const pattern=/https?:\/\/[^\s<>\])]+/g;let position=0;
  for(const match of text.matchAll(pattern)){container.append(document.createTextNode(text.slice(position,match.index)));const link=document.createElement('a');link.href=match[0];link.textContent=match[0];link.target='_blank';link.rel='noopener noreferrer';container.append(link);position=match.index+match[0].length;}
  container.append(document.createTextNode(text.slice(position)));
}
function answerText(container, text) {
  const headings = new Set(['Qué cumples', 'Qué falta', 'Qué queda por confirmar']);
  for (const block of text.split(/\n\s*\n/)) {
    const lines = block.split('\n');
    if (headings.has(lines[0]) || lines[0].startsWith('Detalle por requisito · ')) {
      container.append(textNode('h3', lines.shift()));
    }
    if (!lines.length) continue;
    if (lines.every(line => line.startsWith('- '))) {
      const list = document.createElement('ul');
      for (const line of lines) {
        const item = document.createElement('li');
        appendAnswerLinks(item, line.slice(2));
        list.append(item);
      }
      container.append(list);
    } else {
      const paragraph = document.createElement('p');
      appendAnswerLinks(paragraph, lines.join('\n'));
      container.append(paragraph);
    }
  }
}
$('chat-form').addEventListener('submit',async event=>{
  event.preventDefault();const question=$('question').value.trim();if(!question||controller||uploading)return;
  $('welcome').hidden=true;const user=document.createElement('div');user.className='message user';user.textContent=question;$('messages').append(user);
  const card=document.createElement('article');card.className='message assistant';const heading=document.createElement('div');heading.className='message-heading';heading.textContent='Banca Inteligente';const answer=document.createElement('div');answer.className='answer pending';const pending=document.createElement('div');pending.className='pending-text';const spinner=document.createElement('span');spinner.className='spinner';spinner.setAttribute('aria-hidden','true');pending.append(spinner,document.createTextNode('Preparando tu respuesta…'));pending.setAttribute('role','status');answer.append(pending);card.append(heading,answer);$('messages').append(card);
  const started=performance.now();
  controller=new AbortController();const active=controller;$('send').disabled=true;$('question').value='';resize();scroll();
  $('new-chat').disabled = true; setAttachmentControls(true);
  try {
    if (!conversationId) {
      const created = await api('/api/conversations', {}, active.signal);
      if (controller !== active) return;
      conversationId = created.conversation_id;
    }
    const data = await api('/api/chat', {question, conversation_id: conversationId,
      document_ids: attachments.map(item => item.file_id)}, active.signal);
    if (controller !== active) return;
    answer.replaceChildren();
    answer.classList.remove('pending');
    if (data.attachment_view?.style === 'client_recognition') renderDocumentReport(answer, data);
    else answerText(answer, data.answer);
    if (/^[a-f0-9]{32}$/.test(data.report_id || '')) {
      const download = document.createElement('a');
      download.href = `/api/conversations/${conversationId}/reports/${data.report_id}`;
      download.textContent = 'Descargar reporte ejecutivo (PDF)';
      answer.append(document.createElement('br'), download);
    }
    renderResponseVisuals(card, data);
    if (data.attachment_view?.style !== 'client_recognition') renderDocumentReport(card, data);
    renderResponseExtras(card, data);
  }catch(error){if(error.name==='AbortError')return;if(error.status===401){showLogin();$('login-error').textContent='Tu sesión terminó. Inicia sesión de nuevo.';}else{answer.replaceChildren();answer.className='answer error';answer.textContent=error.message;
    renderResponseExtras(card, {sources: [], elapsed_seconds: (performance.now()-started)/1000});
}}
  finally{if(controller===active){controller=null;$('send').disabled=false;$('new-chat').disabled=false;setAttachmentControls(false);$('question').focus();scroll();}}
});

function setAttachmentControls(disabled) {
  $('attach').disabled = disabled;
  for (const item of $('attachments').children) item.lastElementChild.disabled = disabled;
}

function renderAttachments() {
  $('attachments').replaceChildren(...attachments.map(item => {
    const li = document.createElement('li');
    const filename = document.createElement('span');
    filename.textContent = item.archivo;
    filename.title = item.archivo;
    const remove = document.createElement('button');
    remove.type = 'button';
    remove.textContent = '×';
    remove.setAttribute('aria-label', `Quitar ${item.archivo}`);
    remove.addEventListener('click', () => {
      if (controller || uploading) return;
      const adjacent = li.nextElementSibling || li.previousElementSibling;
      attachments = attachments.filter(file => file.file_id !== item.file_id);
      li.remove();
      (adjacent?.lastElementChild || $('attach')).focus();
    });
    li.append(filename, remove);
    return li;
  }));
  setAttachmentControls(Boolean(controller || uploading));
  $('document-error').textContent = '';
}
$('attach').addEventListener('click', () => {if (!controller && !uploading) $('document-files').click();});
$('document-files').addEventListener('change', async () => {
  const files = Array.from($('document-files').files); if (!files.length || controller || uploading) return;
  uploading = true; setAttachmentControls(true); $('send').disabled = true; $('new-chat').disabled = true;
  try {
    if (!conversationId) conversationId = (await api('/api/conversations', {})).conversation_id;
    const form = new FormData(); form.append('conversation_id', conversationId);
    files.forEach(file => form.append('files', file));
    const response = await fetch('/api/documents', {method: 'POST', body: form, credentials: 'same-origin'});
    const data = await response.json(); if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'No se pudo subir el archivo.');
    for (const item of data.documents) {
      if (!attachments.some(file => file.file_id === item.file_id)) attachments.push(item);
    }
    renderAttachments();
  } catch (error) {$('document-error').textContent = error.message;}
  finally {uploading = false; setAttachmentControls(false); $('send').disabled = false; $('new-chat').disabled = false; $('document-files').value = '';}
});
function documentSection(parent, title, folded = false) {
  const section = document.createElement(folded ? 'details' : 'section'); section.className = 'document-report';
  const heading = document.createElement(folded ? 'summary' : 'h3'); heading.textContent = title;
  section.append(heading); parent.append(section); return section;
}
function renderDocumentReport(card, data) {
  const view = data.attachment_view;
  if (!view) return;
  if (view.style === 'client_recognition') {
    if (view.opening) {
      const opening = document.createElement('p'); opening.textContent = view.opening; card.append(opening);
    }
    if (view.document_names.length) {
      const documents = document.createElement('ul');
      for (const name of view.document_names) {
        const item = document.createElement('li'); item.textContent = name; documents.append(item);
      }
      card.append(documents);
    }
    for (const message of [...view.messages, ...(view.reading_notices || [])]) {
      const p = document.createElement('p'); p.textContent = message; card.append(p);
    }
    const section = documentSection(card, view.policy_title), requirements = document.createElement('ul');
    for (const row of view.policy_requirements) {
      const item = document.createElement('li');
      item.textContent = row.text + (row.source_indices?.length ? ` [${row.source_indices.join(', ')}]` : ''); requirements.append(item);
    }
    section.append(requirements);
    const closing = document.createElement('p'); closing.textContent = view.closing; card.append(closing);
    if (view.references.length) {
      const sources = documentSection(card, 'Fuentes consultadas', true), list = document.createElement('ul');
      for (const [index, ref] of view.references.entries()) {
        const item = document.createElement('li'); item.textContent = `[${index + 1}] ${ref}`; list.append(item);
      }
      sources.append(list);
    }
    return;
  }
  if (view.requirements.length && view.style !== 'preliminary_credit') {
    const section = documentSection(card, view.title || 'Evaluaci\u00f3n de requisitos');
    for (const row of view.requirements) {
      const p = document.createElement('p'), status = document.createElement('strong');
      status.textContent = `${row.label}: ${row.status}. `;
      p.append(status, document.createTextNode(row.reason)); section.append(p);
    }
  }
  if (view.policy_requirements?.length) {
    const section = documentSection(card, 'Requisitos recuperados de la pol\u00edtica');
    for (const row of view.policy_requirements) {
      const p = document.createElement('p');
      p.textContent = row.text + (row.reference ? ` (${row.reference})` : ''); section.append(p);
    }
  }
  for (const item of view.confirmations) {
    const section = documentSection(card, `Confirma ${item.label}`);
    const reference = document.createElement('p'); reference.textContent = item.reference; section.append(reference);
    const form = document.createElement('form'), input = document.createElement(item.options ? 'select' : 'input'), save = document.createElement('button'), message = document.createElement('span');
    input.required = true; input.setAttribute('aria-label', `Confirmar ${item.label}`);
    if (item.options) {
      for (const value of ['', ...item.options]) {
        const option = document.createElement('option'); option.value = value; option.textContent = value || 'Selecciona la moneda'; input.append(option);
      }
    } else {
      input.type = 'text'; input.maxLength = 500;
      input.placeholder = item.campo.includes('fecha') || item.campo.includes('periodo') ? 'AAAA-MM-DD' : 'Valor confirmado por ti';
    }
    save.type = 'submit'; save.textContent = 'Guardar dato'; form.append(input, save, message); section.append(form);
    const documentConversation = conversationId;
    form.addEventListener('submit', async event => {
      event.preventDefault(); save.disabled = true;
      try {
        await api(`/api/documents/${item.file_id}/corrections`, {conversation_id: documentConversation, campo: item.campo, valor: input.value});
        message.textContent = 'Dato guardado. Vuelve a consultar para reevaluar.';
      } catch (error) {message.textContent = error.message;}
      finally {save.disabled = false;}
    });
  }
  if (view.references.length) {
    const p = document.createElement('p'); p.className = 'credit-citations';
    p.textContent = view.style === 'preliminary_credit' ?
      view.references.map((reference, index) => `[${index + 1}] ${reference}`).join(' \u00b7 ') : view.references.join(' \u00b7 ');
    const sources = documentSection(card, 'Fuentes consultadas', true);
    sources.append(p);
  }
}
api('/api/session').then(data=>showChat(data.profile)).catch(()=>showLogin());
