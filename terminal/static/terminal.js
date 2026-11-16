'use strict';
/* PDV2 por CrownTech · Caixa Rápido. Pure helpers live in terminal-core.js (window.PDVCore). */
const C = window.PDVCore;
const {esc, money, moneyPlain, qtyText, documentText, parseMoney, parsePercent, parseQty} = C;
const $ = id => document.getElementById(id);
const el = (tag, className = '', html = '') => { const node = document.createElement(tag); if (className) node.className = className; if (html) node.innerHTML = html; return node; };
const ico = (name, extra = '') => `<svg class="ico ${extra}" aria-hidden="true"><use href="#i-${name}"/></svg>`;
const prefs = {
  get(key, fallback) { try { return localStorage.getItem('pdv2.' + key) ?? fallback; } catch { return fallback; } },
  set(key, value) { try { localStorage.setItem('pdv2.' + key, value); } catch { /* storage unavailable */ } },
};
const uuid = () => (crypto.randomUUID ? crypto.randomUUID() : ([1e7] + -1e3 + -4e3 + -8e3 + -1e11).replace(/[018]/g, c => (c ^ crypto.getRandomValues(new Uint8Array(1))[0] & 15 >> c / 4).toString(16)));
const fold = text => String(text ?? '').normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLowerCase();
const timeText = value => new Date(Number(value)).toLocaleTimeString('pt-BR', {hour: '2-digit', minute: '2-digit'});
const dateTimeText = value => new Date(Number(value)).toLocaleString('pt-BR');
const emptyCustomer = () => ({name: '', document: '', id: '', sellerId: ''});

const roleNames = {owner: 'Proprietário', admin: 'Administrador', manager: 'Gerente', cashier: 'Operador de caixa', inventory: 'Estoque', finance: 'Financeiro', viewer: 'Consulta'};
const saleStatusNames = {completed: ['Concluída', 'ok'], voided: ['Cancelada', 'bad']};
const jobStatusNames = {queued: ['Na fila', 'warn'], sending: ['Enviando', 'warn'], printed: ['Impresso', 'ok'], uncertain: ['Resultado incerto', 'bad'], blocked: ['Bloqueado', 'bad']};
const driverNames = {file: 'Arquivo de teste', cups: 'CUPS', tcp: 'Rede (TCP)', windows_raw: 'Fila do Windows'};
const canSell = role => ['owner', 'admin', 'manager', 'cashier'].includes(role);
const canManage = role => ['owner', 'admin', 'manager'].includes(role);
const canAdjustStock = role => canManage(role) || role === 'inventory';

// No manager PIN is ever kept here: authorizations are asked for at the moment they are needed.
const state = {
  session: null, csrf: '', tab: 'checkout', indicator: null, pendingSince: 0,
  cart: [], selectedId: null, lastItemId: null, flashId: null, flashAt: 0, pendingQty: null,
  quote: null, saleRequest: null, discount: null, customer: emptyCustomer(), customers: [],
  pay: {active: false, method: null, other: null}, payOptions: [], payOptionsSig: '',
  done: null, doneRendered: null, lastSale: null, submitting: false, scanBusy: false,
  suspendedCount: 0, printer: null, categories: [], stockProducts: [], cartRestored: false,
  bootstrapRefreshInFlight: false, lastOperatorsRefreshAt: 0, loginLockedUntil: 0,
  restore: {open: false, running: false, timer: null, finished: null},
  scale: {port: null, reader: null, buffer: '', awaitingId: null, gate: C.createWeightGate(2), settleTimer: null,
          barcodeMode: prefs.get('scale.barcodeMode', 'weight'), baudRate: prefs.get('scale.baudRate', '9600')},
};
const role = () => state.session?.operator?.role;

/* ---------- feedback ---------- */
let toastTimer;
function toast(message, error = false) {
  const box = $('toast');
  box.textContent = message;
  box.className = 'show' + (error ? ' error' : '');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { box.className = ''; }, 5000);
}
let audioContext = null;
function beep(kind = 'ok') {
  if (prefs.get('sound', 'on') !== 'on') return;
  try {
    audioContext ||= new (window.AudioContext || window.webkitAudioContext)();
    if (audioContext.state === 'suspended') audioContext.resume();
    const oscillator = audioContext.createOscillator(), gain = audioContext.createGain();
    const start = audioContext.currentTime, length = kind === 'error' ? .3 : .07;
    oscillator.type = kind === 'error' ? 'square' : 'sine';
    oscillator.frequency.value = kind === 'error' ? 196 : 1320;
    gain.gain.setValueAtTime(.0001, start);
    gain.gain.exponentialRampToValueAtTime(kind === 'error' ? .1 : .07, start + .012);
    gain.gain.exponentialRampToValueAtTime(.0001, start + length);
    oscillator.connect(gain).connect(audioContext.destination);
    oscillator.start(start);
    oscillator.stop(start + length + .03);
  } catch { /* audio is optional */ }
}

async function api(path, method = 'GET', data) {
  const options = {method, credentials: 'same-origin', headers: {}};
  if (method !== 'GET') {
    options.headers['Content-Type'] = 'application/json';
    options.headers['X-CSRF-Token'] = state.csrf;
    options.body = JSON.stringify(data ?? {});
  }
  let response;
  try { response = await fetch(path, options); } catch { throw Error('Sem resposta do terminal local. Confira o serviço.'); }
  let body = {};
  try { body = await response.json(); } catch { /* empty body */ }
  if (!response.ok) {
    const error = Error(body.error || `Erro ${response.status}`);
    error.status = response.status;
    throw error;
  }
  return body;
}

/* ---------- barcode reader bursts ---------- */
const typing = {times: []};
function noteKey(event) {
  if (event.key.length !== 1 || event.ctrlKey || event.metaKey || event.altKey) return;
  typing.times.push(performance.now());
  if (typing.times.length > 8) typing.times.shift();
}
// A reader types faster than a person: four keys under 50 ms apart, just now.
function scannerBurst() {
  const times = typing.times;
  if (times.length < 4 || performance.now() - times[times.length - 1] > 120) return false;
  for (let index = times.length - 3; index < times.length; index++) if (times[index] - times[index - 1] > 50) return false;
  return true;
}

/* ---------- one dialog component ---------- */
const modalStack = [];
let modalSeq = 0;
const focusables = root => [...root.querySelectorAll('button,input,select,textarea,a[href],[tabindex]:not([tabindex="-1"])')].filter(node => !node.disabled && node.offsetParent !== null);
function openModal({title, eyebrow = '', text = '', className = '', backdropClass = '', dismissible = true, onKey = null} = {}) {
  const previous = document.activeElement;
  const backdrop = el('div', 'modal-backdrop ' + backdropClass);
  const dialog = el('section', 'modal ' + className);
  const head = el('header', 'modal-head'), body = el('div', 'modal-body'), error = el('p', 'modal-error'), actions = el('div', 'modal-actions');
  const heading = el('h2'), lead = el('p');
  heading.id = 'modal-title-' + (++modalSeq);
  heading.textContent = title;
  lead.textContent = text;
  if (eyebrow) head.append(Object.assign(el('span', 'eyebrow'), {textContent: eyebrow}));
  head.append(heading);
  if (text) head.append(lead);
  dialog.setAttribute('role', 'dialog');
  dialog.setAttribute('aria-modal', 'true');
  dialog.setAttribute('aria-labelledby', heading.id);
  error.setAttribute('role', 'alert');
  dialog.append(head, body, error, actions);
  backdrop.append(dialog);
  let settled = false, busy = false, resolve;
  const modal = {
    backdrop, dialog, head, heading, lead, body, actions, primary: null,
    promise: new Promise(done => { resolve = done; }),
    error(message) {
      error.textContent = message || '';
      error.classList.toggle('show', !!message);
      if (message) beep('error');
    },
    close(value = null) {
      if (settled) return;
      settled = true;
      modalStack.splice(modalStack.indexOf(modal), 1);
      backdrop.remove();
      if (modalStack.length) (focusables(modalStack.at(-1).dialog)[0] || modalStack.at(-1).dialog).focus?.();
      else if (previous && previous !== document.body && previous.isConnected && !previous.disabled && previous.offsetParent !== null) previous.focus();
      else focusHome();
      resolve(value);
    },
    action(label, kind, handler) {
      const button = el('button', 'btn ' + (kind || ''));
      button.type = 'button';
      button.innerHTML = label;
      button.addEventListener('click', () => modal.run(handler));
      actions.append(button);
      if (/\bprimary\b/.test(kind || '')) modal.primary = button;
      return button;
    },
    async run(handler) {
      if (busy || settled) return;
      busy = true;
      modal.error('');
      dialog.classList.add('busy');
      try { await handler(modal); } catch (failure) { modal.error(failure?.message || 'Não foi possível concluir'); } finally { busy = false; dialog.classList.remove('busy'); }
    },
    handleKey(event) {
      if (onKey && onKey(event, modal) === true) return;
      if (event.key === 'Escape') { event.preventDefault(); if (dismissible && !busy) modal.close(null); return; }
      if (event.key === 'Enter') {
        // The reader ends every code with Enter; it must never confirm a dialog.
        if (scannerBurst()) { event.preventDefault(); event.stopPropagation(); return; }
        if (event.target.matches?.('input:not([type="checkbox"]):not([type="radio"]),select') && modal.primary && !modal.primary.disabled) { event.preventDefault(); modal.primary.click(); }
        return;
      }
      if (event.key !== 'Tab') return;
      const nodes = focusables(dialog);
      if (!nodes.length) { event.preventDefault(); return; }
      if (event.shiftKey && document.activeElement === nodes[0]) { event.preventDefault(); nodes.at(-1).focus(); }
      else if (!event.shiftKey && document.activeElement === nodes.at(-1)) { event.preventDefault(); nodes[0].focus(); }
      else if (!dialog.contains(document.activeElement)) { event.preventDefault(); nodes[0].focus(); }
    },
  };
  backdrop.addEventListener('mousedown', event => { if (event.target === backdrop && dismissible && !busy) modal.close(null); });
  modalStack.push(modal);
  document.body.append(backdrop);
  return modal;
}
// Arrow keys walk through the option buttons of a dialog.
function arrowNav(selector) {
  return (event, modal) => {
    const forward = event.key === 'ArrowDown' || event.key === 'ArrowRight';
    if (!forward && event.key !== 'ArrowUp' && event.key !== 'ArrowLeft') return false;
    if (event.target.matches?.('select,textarea,input')) return false;
    const nodes = [...modal.dialog.querySelectorAll(selector)].filter(node => !node.disabled);
    if (!nodes.length) return false;
    event.preventDefault();
    const index = nodes.indexOf(document.activeElement);
    const next = index < 0 ? (forward ? 0 : nodes.length - 1) : (index + (forward ? 1 : -1) + nodes.length) % nodes.length;
    nodes[next].focus();
    return true;
  };
}
function confirmDialog(title, text, acceptLabel = 'Confirmar', {danger = false, cancelLabel = 'Cancelar'} = {}) {
  const modal = openModal({title, text, eyebrow: 'Confirmação necessária'});
  modal.action(cancelLabel, '', () => modal.close(false));
  modal.action(acceptLabel, danger ? 'danger primary' : 'primary', () => modal.close(true)).focus();
  return modal.promise.then(Boolean);
}
function textDialog(title, text, {eyebrow = 'Informação necessária', fieldLabel = 'Informação', placeholder = '', type = 'text', inputMode = '', acceptLabel = 'Confirmar', multiline = false, minLength = 1, maxLength = 120, value = '', validate = null} = {}) {
  const modal = openModal({title, text, eyebrow});
  const field = el('label', 'field');
  const input = document.createElement(multiline ? 'textarea' : 'input');
  if (!multiline) input.type = type;
  if (inputMode) input.inputMode = inputMode;
  input.maxLength = maxLength;
  input.placeholder = placeholder;
  input.autocomplete = 'off';
  input.value = value;
  field.append(Object.assign(el('span'), {textContent: fieldLabel}), input);
  modal.body.append(field);
  modal.action('Cancelar', '', () => modal.close(null));
  modal.action(acceptLabel, 'primary', async () => {
    const typed = input.value.trim();
    if (typed.length < minLength) { input.focus(); throw Error(minLength > 1 ? `Informe ao menos ${minLength} caracteres` : 'Preencha o campo para continuar'); }
    try { if (validate) await validate(typed); } catch (failure) { input.focus(); input.select?.(); throw failure; }
    modal.close(typed);
  });
  input.focus();
  input.select?.();
  return modal.promise;
}
function pinDialog(title, text, {acceptLabel = 'Autorizar', validate = null} = {}) {
  return textDialog(title, text, {eyebrow: 'Autorização do gerente', fieldLabel: 'PIN do gerente', placeholder: 'Digite o PIN', type: 'password', inputMode: 'numeric', acceptLabel, maxLength: 12, validate});
}
// Removing or reducing what was already rung up needs a supervisor: PIN for the cashier, audit for everyone.
async function authorizeRemoval(title, text) {
  if (role() === 'cashier') {
    const pin = await pinDialog(title, text, {validate: value => api('/api/cart/items/authorize-remove', 'POST', {manager_pin: value})});
    return pin !== null;
  }
  if (!(await confirmDialog(title.replace(/^Autorizar /, 'Confirmar '), text, 'Confirmar', {danger: true, cancelLabel: 'Manter'}))) return false;
  await api('/api/cart/items/authorize-remove', 'POST', {manager_pin: ''});
  return true;
}

/* ---------- views, status bar ---------- */
function showView(view) { for (const id of ['pairView', 'restoreView', 'loginView', 'damagedView', 'appView']) $(id).classList.toggle('hidden', id !== view); }
// shortText is what narrow monitors show while the state is the normal one.
function setChip(id, chipState, text, title = '', shortText = '') {
  const chip = $(id), label = chip.lastElementChild;
  chip.dataset.state = chipState;
  label.textContent = text;
  if (shortText) label.dataset.short = shortText; else delete label.dataset.short;
  chip.title = title;
  chip.setAttribute('aria-label', text);
}
function connectionLabel(connection) {
  if (!connection?.paired) return 'Empresa não vinculada';
  return [connection.tenant_name || 'Empresa', connection.shop_name || 'Loja', connection.terminal_name || 'Caixa'].join(' · ');
}
// The backend reports the events kept for review as sync.quarantined.
const syncIndicator = (sync, now) => C.syncIndicator({...sync, quarantined_count: Number(sync?.quarantined) || 0}, now);
function renderStatus() {
  const session = state.session;
  if (!session) return;
  const connection = session.connection || {}, license = session.license || {}, operator = session.operator, now = Date.now();
  const indicator = state.indicator = syncIndicator(session.sync, now);
  if (indicator.pending > 0) state.pendingSince ||= now; else state.pendingSince = 0;
  $('identityPlace').textContent = [connection.shop_name || connection.tenant_name || 'Loja', connection.terminal_name || 'Caixa'].join(' · ');
  $('identityOperator').textContent = '· ' + (operator ? operator.name : 'Sem operador');
  $('identityText').title = [connection.tenant_name, connection.shop_name, connection.terminal_name, operator ? `${operator.name} (${roleNames[operator.role] || operator.role})` : ''].filter(Boolean).join(' · ');
  setChip('netChip', indicator.state === 'online' ? 'ok' : indicator.state === 'offline' ? 'warn' : 'muted', indicator.label, indicator.detail);
  setChip('queueChip', !indicator.pending ? 'ok' : (indicator.syncProblem || indicator.state === 'offline') ? 'warn' : 'muted', indicator.queueLabel,
    indicator.pending ? `${indicator.pending} registro(s) aguardando envio ao servidor da empresa.` : 'Nada aguardando envio.');
  $('reviewChip').classList.toggle('hidden', !indicator.review);
  setChip('reviewChip', 'warn', indicator.reviewLabel, 'Registros que o servidor separou para revisão do gestor. As vendas seguintes continuam sendo enviadas. Toque para ver os detalhes.');
  const leaseText = license.clock ? 'Relógio incorreto' : license.warning ? leaseWarningText(license)
    : {active: 'Licença ativa', expired: 'Licença vencida', clock_rollback: 'Relógio incorreto', invalid: 'Licença inválida', transferred: 'Caixa transferido'}[license.state] || 'Licença pendente';
  // Warn: about to expire, or expired while the open cashier keeps selling.
  const leaseState = !license.can_sell ? 'danger' : (license.warning || license.state !== 'active') ? 'warn' : 'ok';
  setChip('leaseChip', leaseState, leaseText, license.reason || '', leaseState === 'ok' ? 'Licença' : '');
  setChip('cashChip', !operator ? 'muted' : session.cash ? 'ok' : 'danger', session.cash ? 'Caixa aberto' : 'Caixa fechado',
    session.cash ? `Aberto às ${timeText(session.cash.opened_at)} com ${money(session.cash.opening_cents)}` : 'Abra o caixa (F12) para vender.', session.cash ? 'Caixa' : '');
  const hours = session.hours || {};
  $('hoursChip').classList.toggle('hidden', !hours.configured || hours.open);
  setChip('hoursChip', 'warn', 'Fora do horário', 'Fora do horário de funcionamento da empresa. Um caixa já aberto continua ativo.');
  const update = session.update || {}, staged = update.state === 'staged' && update.staged_version;
  $('releaseChip').classList.toggle('hidden', !staged && !update.available);
  setChip('releaseChip', 'muted', staged ? `Atualização ${update.staged_version} pronta` : update.available ? `Versão ${update.available.version} disponível` : '',
    staged ? 'A nova versão já foi baixada. Com o caixa fechado, o gerente toca em Atualizar agora, na aba Sistema.' : 'Há uma atualização do terminal. Veja em Sistema.');
  renderPrinterChip();
  renderScaleChip();
  $('statusBar').dataset.tone = C.statusTone({loggedIn: !!operator, sells: canSell(operator?.role), canSell: !!license.can_sell,
    cashOpen: !!session.cash, indicator, pendingForMs: state.pendingSince ? now - state.pendingSince : 0});
  const label = connectionLabel(connection);
  $('connectionIdentity').innerHTML = connection.paired
    ? `<strong>${esc(label)}</strong><small>${esc(indicator.label)} · ${esc(indicator.queueLabel)} · ${esc(leaseText)} · licença •••• ${esc(connection.license_hint || '—')}</small>`
    : '<strong>Este terminal ainda não está conectado</strong><small>Informe a chave da licença e o código criado no painel da empresa.</small>';
  const count = session.operators?.length || 0, at = session.sync?.bootstrap_at;
  $('operatorSyncStatus').textContent = session.paired ? `${count} ${count === 1 ? 'operador disponível' : 'operadores disponíveis'} neste caixa${at ? ' · atualizado em ' + dateTimeText(at) : ''}. Os logins já baixados funcionam sem internet.` : '';
}
const leaseWarningText = license => (license.days_left >= 1 ? `Licença vence em ${license.days_left} dia${license.days_left > 1 ? 's' : ''}` : 'Licença vence hoje');
// What the sale panel shows while the license (or the clock it depends on) stops the cashier.
function licenseBlock(license = {}) {
  // The shop moved to another computer: this one only finishes sending what it had.
  if (license.state === 'transferred') {
    const pending = Number(state.session?.sync?.pending) || 0;
    return {title: 'Este caixa foi transferido', recheck: false,
      text: 'A loja passou a usar outro computador, e este não vende mais. '
        + (pending ? `Ainda faltam ${pending} registro(s) para enviar: deixe este computador ligado e com internet até a fila zerar.`
          : 'Tudo o que foi vendido aqui já está guardado no servidor da empresa.'),
      steps: ['Se o caixa ainda estiver aberto, feche-o (F12) para o fechamento subir também.',
        'Quando o indicador mostrar que não há nada aguardando envio, este computador pode ser desligado.']};
  }
  if (license.clock) {
    const computer = dateTimeText(license.clock.computer_time * 1000).slice(0, -3), reference = dateTimeText(license.clock.reference_time * 1000).slice(0, -3);
    return {title: 'Acerte a data e a hora deste computador', recheck: true,
      text: license.clock.problem === 'behind'
        ? `O relógio está atrasado: este computador marca ${computer}, mas o caixa já registrou atividade em ${reference}.`
        : `O relógio está adiantado: este computador marca ${computer}, mas a hora certa é perto de ${reference}.`,
      steps: ['Clique com o botão direito no relógio do Windows, no canto da tela, e escolha “Ajustar data e hora”.',
        'Ligue “Definir horário automaticamente” e toque em “Sincronizar agora”. Sem internet, acerte a data e a hora à mão.',
        'Volte a esta tela e toque em “Verificar de novo”. Nenhuma venda é perdida.']};
  }
  if (license.state === 'expired') return {title: 'Licença offline vencida', recheck: true,
    text: 'O prazo para vender sem internet terminou. Conecte este computador à internet e toque em “Verificar de novo”: a licença é renovada sozinha e nada do que foi vendido se perde.'};
  return {title: 'Vendas bloqueadas', recheck: true, text: license.reason || 'Licença indisponível. Conecte o terminal à internet para renovar.'};
}
async function recheckLicense() {
  const result = await api('/api/license/renew', 'POST', {});
  await poll();
  const license = state.session?.license || {};
  if (license.can_sell && !license.clock) toast(result.renewed ? 'Licença renovada. Caixa liberado.' : 'Tudo certo. Caixa liberado.');
  else toast(result.error ? `Ainda bloqueado: ${result.error}` : `Ainda bloqueado: ${license.reason || 'licença indisponível'}`, true);
  if (state.tab === 'system') await loadSystem();
}
function renderDamaged(info) {
  $('damagedDatabase').textContent = info.database || 'terminal.sqlite3';
  $('damagedProblem').textContent = info.problem || 'sem detalhe';
  const copies = info.backups || [];
  $('damagedBackups').innerHTML = copies.length
    ? copies.map((copy, index) => `<div class="mini-row"><span><strong>${esc(copy.name)}</strong>${index ? '' : ' · mais recente'}<br><small>${esc(copy.path)}</small></span><small>${copy.created_at ? dateTimeText(copy.created_at) : ''}</small></div>`).join('')
    : `<p class="list-empty">Nenhum backup local encontrado em ${esc(info.backup_directory || '')}. Chame o suporte: o histórico enviado ao servidor pode ser recuperado pelo painel.</p>`;
}
function renderPrinterChip() {
  const printer = state.printer;
  if (!printer) return setChip('printerChip', 'muted', 'Impressora…', 'Consultando a impressora de recibos.');
  if (!printer.configured) return setChip('printerChip', 'muted', 'Sem impressora', 'Nenhuma impressora de recibos configurada. O comprovante pode ser impresso pelo sistema (F9).');
  if (printer.uncertain) return setChip('printerChip', 'warn', 'Impressora: verificar', 'A última impressão não foi confirmada. Verifique papel e conexão.');
  setChip('printerChip', 'ok', 'Impressora ok', `Recibos em ${printer.target}${printer.queued ? ` · ${printer.queued} na fila` : ''}`, 'Impressora');
}
function renderScaleChip() {
  const scale = state.scale;
  if (scale.awaitingId) return setChip('scaleChip', 'warn', 'Aguardando peso', 'Coloque o produto na balança ou informe o peso com F3.');
  if (scale.port) return setChip('scaleChip', 'ok', 'Balança ligada', `Balança serial conectada a ${scale.baudRate} bps.`);
  setChip('scaleChip', 'muted', 'Balança manual', 'Peso digitado ou etiqueta de balança. Toque para conectar uma balança serial.', 'Balança');
}
function tickClock() { $('clock').textContent = new Date().toLocaleTimeString('pt-BR', {hour: '2-digit', minute: '2-digit'}); }
async function loadPrinterStatus() { applyPrinterData(await api('/api/print/settings')); }
function applyPrinterData(data) {
  const receipt = (data.settings || []).find(setting => setting.kind === 'receipt');
  const jobs = (data.jobs || []).filter(job => job.kind === 'receipt');
  state.printer = {configured: !!receipt, target: receipt?.target || '', uncertain: jobs[0]?.status === 'uncertain', queued: jobs.filter(job => job.status === 'queued').length};
  renderPrinterChip();
}

/* ---------- session ---------- */
async function refreshOperators(silent = false) {
  if (state.bootstrapRefreshInFlight) return;
  state.bootstrapRefreshInFlight = true;
  try {
    await api('/api/bootstrap', 'POST', {});
    if (!silent) toast('Operadores e catálogo atualizados');
  } finally { state.bootstrapRefreshInFlight = false; }
}
function renderOperatorSelect() {
  const select = $('operatorSelect'), operators = state.session.operators || [];
  const signature = JSON.stringify(operators.map(operator => [operator.id, operator.name, operator.role]));
  // Rebuild only when the list changed, and never move the name already chosen.
  if (select.dataset.signature !== signature) {
    const previous = select.value;
    select.innerHTML = operators.length ? operators.map(operator => `<option value="${esc(operator.id)}">${esc(operator.name)} · ${esc(roleNames[operator.role] || operator.role)}</option>`).join('') : '<option value="">Nenhum operador com PIN cadastrado</option>';
    select.value = C.keepSelection(previous, operators.map(operator => operator.id), prefs.get('operator', ''));
    select.dataset.signature = signature;
  }
  select.disabled = !operators.length;
  $('operatorEmptyHint').classList.toggle('hidden', !!operators.length);
  $('loginTitle').textContent = state.session.license?.state === 'transferred' ? 'Este caixa foi transferido' : state.session.cash ? 'Caixa bloqueado' : 'Pronto para vender';
}
// Session answers are applied in the order they were asked for, so a slow poll cannot bring back an older state.
let sessionAsked = 0, sessionApplied = 0;
async function fetchSession() {
  const ticket = ++sessionAsked;
  const session = await api('/api/session');
  if (ticket < sessionApplied) return null;
  sessionApplied = ticket;
  return session;
}
function applySession(session) {
  state.session = session;
  state.csrf = session.csrf;
  $('deviceId').textContent = session.device_id;
  renderStatus();
}
async function loadSession() {
  const fresh = await fetchSession();
  if (!fresh) return;
  if (fresh.damaged) { state.csrf = fresh.csrf; renderDamaged(fresh.damaged); showView('damagedView'); return; }
  applySession(fresh);
  const session = state.session;
  // A new computer brings the shop's data back before anyone logs in.
  if (session.restore?.active || state.restore.finished) { renderRestore(session.restore); return; }
  if (!session.paired) { if (state.restore.open) renderRestore(session.restore); else showView('pairView'); return; }
  if (!session.operator) {
    renderOperatorSelect();
    const first = $('loginView').classList.contains('hidden');
    showView('loginView');
    if (first) (session.operators?.length ? $('operatorPin') : $('refreshBootstrap')).focus();
    // People can be added in the panel after pairing, but a full bootstrap every poll is wasteful.
    if (Date.now() - state.lastOperatorsRefreshAt > 300000) {
      state.lastOperatorsRefreshAt = Date.now();
      refreshOperators(true).then(async () => { const updated = await fetchSession(); if (!updated) return; applySession(updated); if (!updated.operator) renderOperatorSelect(); }).catch(() => {});
    }
    return;
  }
  showView('appView');
  applyRoleUi();
  renderPaymentMethods();
  loadCustomers().catch(() => {});
  loadPrinterStatus().catch(() => {});
  loadSuspendedCount().catch(() => {});
  await restoreCart();
  renderSale();
  setTab(allowedTab(state.tab));
  focusHome();
}
async function poll() {
  if (state.session?.operator) {
    const previousBootstrap = state.session.sync?.bootstrap_at;
    const session = await fetchSession();
    if (!session) return;
    if (!session.operator) { await loadSession(); return; }
    applySession(session);
    renderPaymentMethods();
    renderPanels();
    renderKeybar();
    if (session.sync?.bootstrap_at && session.sync.bootstrap_at !== previousBootstrap) {
      loadCustomers().catch(() => {});
      await refreshCartProducts();
    }
  } else if (state.session?.paired) await loadSession();
}
function applyRoleUi() {
  const current = role(), manager = canManage(current);
  document.body.dataset.canSell = String(canSell(current));
  document.querySelector('[data-tab="checkout"]').classList.toggle('hidden', !canSell(current));
  document.querySelector('[data-tab="stock"]').classList.toggle('hidden', !canAdjustStock(current));
  document.querySelectorAll('.admin-only').forEach(node => node.classList.toggle('hidden', !manager));
  $('stockAdjustRow').classList.toggle('hidden', !canAdjustStock(current));
  $('printerForm').classList.toggle('hidden', !manager);
  $('flushPrintBtn').classList.toggle('hidden', !canSell(current));
  $('syncNowBtn').classList.toggle('hidden', !manager);
  $('exportLink').classList.toggle('hidden', !manager);
  $('backupBtn').classList.toggle('hidden', !manager);
  $('mirrorChooseBtn').classList.toggle('hidden', !manager);
  $('mirrorSnapshotBtn').classList.toggle('hidden', !manager);
}
function allowedTab(tab) {
  const current = role();
  const allowed = {checkout: canSell(current), sales: true, stock: canAdjustStock(current), printers: canManage(current), system: canManage(current)};
  if (allowed[tab]) return tab;
  return canSell(current) ? 'checkout' : canAdjustStock(current) ? 'stock' : 'sales';
}
function setTab(tab) {
  if (document.body.classList.contains('focus-mode') && canSell(role())) tab = 'checkout';
  state.tab = tab;
  document.body.dataset.tab = tab;
  document.querySelectorAll('.tab').forEach(button => button.classList.toggle('active', button.dataset.tab === tab));
  document.querySelectorAll('.tab-panel').forEach(panel => panel.classList.add('hidden'));
  $(tab + 'Tab').classList.remove('hidden');
  const loader = {sales: loadSales, stock: loadStock, printers: loadPrinters, system: async () => { await loadSystem(); await loadBackups(); await loadMirror(); }}[tab];
  if (loader) loader().catch(error => toast(error.message, true));
  renderKeybar();
  if (tab === 'checkout') focusHome();
}
function focusScan() {
  if (modalStack.length || state.tab !== 'checkout' || !canSell(role())) return;
  $('productSearch').focus({preventScroll: true});
}
function focusPay() {
  const method = state.pay.method;
  if (method === 'cash') $('payCash').focus();
  else if (method === 'split') focusables($('paySplitBox'))[0]?.focus();
  else $('paymentMethods').focus();
}
function focusHome() {
  if (modalStack.length || $('appView').classList.contains('hidden')) return;
  if (state.tab === 'checkout' && state.pay.active) focusPay(); else focusScan();
}

/* ---------- cart ---------- */
const quoteKey = () => JSON.stringify(state.cart.map(item => [item.id, item.quantity_milli]));
const quoteValid = () => state.quote?.key === quoteKey();
const quoteLine = item => (quoteValid() ? state.quote.lines[item.id] : null);
const lineGross = item => quoteLine(item)?.gross_cents ?? Math.round(item.price_cents * item.quantity_milli / 1000);
const lineNet = item => lineGross(item) - (quoteLine(item)?.promo_discount_cents || 0);
const cartGross = () => state.cart.reduce((sum, item) => sum + lineGross(item), 0);
const cartPromo = () => (quoteValid() ? state.quote.promo : 0);
const cartBase = () => cartGross() - cartPromo();
const cartDiscount = () => C.discountCents(state.discount, cartBase());
const cartTotal = () => Math.max(0, cartBase() - cartDiscount());
const isOut = product => product.stock_controlled !== false && Number(product.stock_milli) <= 0;
function stockLimitError(product, quantity) {
  if (product.stock_controlled === false) return '';
  const available = Math.max(0, Number(product.stock_milli) || 0);
  return quantity <= available ? '' : `Estoque insuficiente: ${product.name} tem ${qtyText(available, product.unit)} ${product.unit} disponíveis`;
}
let quoteTimer = null;
async function refreshQuote(force = false) {
  const key = quoteKey();
  if (!state.cart.some(item => item.quantity_milli > 0)) { state.quote = {key, promo: 0, lines: {}}; return; }
  if (!force && state.quote?.key === key) return;
  const run = async () => {
    try {
      const data = await api('/api/cart/quote', 'POST', {items: state.cart.filter(item => item.quantity_milli > 0).map(item => ({product_id: item.id, quantity_milli: item.quantity_milli}))});
      if (key !== quoteKey()) return;
      state.quote = {key, promo: data.promo_discount_cents, lines: Object.fromEntries(data.lines.map(line => [line.product_id, line]))};
      renderSale();
    } catch { /* list prices stay on screen; the sale itself is priced by the terminal backend */ }
  };
  clearTimeout(quoteTimer);
  if (force) return run();
  quoteTimer = setTimeout(run, 100);
}
function touch(id) {
  state.lastItemId = id;
  state.selectedId = id;
  state.flashId = id;
  state.flashAt = Date.now();
}
function cartChanged() {
  state.saleRequest = null;
  renderSale();
}
function persistCart() {
  try {
    if (!state.session?.operator) return;
    if (!state.cart.length) { sessionStorage.removeItem('pdv2.cart'); return; }
    sessionStorage.setItem('pdv2.cart', JSON.stringify({operator: state.session.operator.id,
      items: state.cart.map(item => ({id: item.id, quantity_milli: item.quantity_milli})), discount: state.discount, customer: state.customer}));
  } catch { /* storage unavailable */ }
}
// A reload (or a lost session) must not be a way to throw a sale away.
async function restoreCart() {
  if (state.cartRestored) return;
  state.cartRestored = true;
  try {
    const saved = JSON.parse(sessionStorage.getItem('pdv2.cart') || 'null');
    if (!saved || saved.operator !== state.session.operator.id || !saved.items?.length || state.cart.length) return;
    await loadCartSnapshot(saved);
    toast('Venda em andamento recuperada');
  } catch { /* nothing to restore */ }
}
function normalizeDiscount(discount) {
  if (discount && typeof discount === 'object' && ['percent', 'value'].includes(discount.mode) && Number(discount.value) > 0) return {mode: discount.mode, value: Number(discount.value)};
  if (typeof discount === 'string' && discount.trim()) {
    try { const percent = parsePercent(discount); return percent > 0 ? {mode: 'percent', value: percent} : null; } catch { return null; }
  }
  return null;
}
async function fetchProductsByIds(ids) {
  const found = new Map();
  for (let start = 0; start < ids.length; start += 60) {
    const data = await api('/api/products/batch?ids=' + encodeURIComponent(ids.slice(start, start + 60).join(',')));
    for (const product of data.products || []) found.set(product.id, product);
  }
  return found;
}
async function loadCartSnapshot(snapshot) {
  const wanted = (snapshot.items || []).map(item => ({id: String(item.id || item.product_id || ''), quantity_milli: Number(item.quantity_milli)})).filter(item => item.id && item.quantity_milli > 0);
  const fresh = await fetchProductsByIds(wanted.map(item => item.id));
  const cart = wanted.filter(item => fresh.has(item.id)).map(item => ({...fresh.get(item.id), quantity_milli: item.quantity_milli}));
  state.cart = cart;
  state.discount = normalizeDiscount(snapshot.discount);
  state.customer = {...emptyCustomer(), ...(snapshot.customer && typeof snapshot.customer === 'object' ? snapshot.customer : {name: String(snapshot.customer || '')})};
  state.lastItemId = state.selectedId = cart.at(-1)?.id || null;
  state.quote = null;
  state.done = null;
  cartChanged();
  if (cart.length < wanted.length) toast(`${wanted.length - cart.length} item(ns) saíram do catálogo e foram retirados da venda`, true);
}
// Prices and stock on screen follow the catalog after each bootstrap instead of the copy taken at scan time.
async function refreshCartProducts() {
  if (!state.cart.length) return;
  try {
    const fresh = await fetchProductsByIds(state.cart.map(item => item.id));
    for (const item of state.cart) if (fresh.has(item.id)) Object.assign(item, fresh.get(item.id), {quantity_milli: item.quantity_milli});
    state.quote = null;
    renderSale();
  } catch { /* keep what is on screen */ }
}
async function addProduct(product, quantity = null) {
  if (isOut(product)) throw Error(`${product.name} está sem estoque neste terminal`);
  if (state.done) state.done = null;
  if (state.pay.active) cancelPayment();
  if (quantity === null && state.pendingQty) quantity = state.pendingQty;
  state.pendingQty = null;
  renderPendingQty();
  const existing = state.cart.find(item => item.id === product.id);
  if (product.unit === 'KG' && quantity === null) {
    if (state.scale.port) {
      // The line waits for the scale; nothing else can receive that weight.
      if (existing) Object.assign(existing, product, {quantity_milli: existing.quantity_milli}); else state.cart.push({...product, quantity_milli: 0});
      state.scale.awaitingId = product.id;
      state.scale.gate.reset();
      touch(product.id);
      cartChanged();
      scanStatus('', `Aguardando peso de ${product.name} · coloque o produto na balança`);
      return;
    }
    const raw = await textDialog('Informar peso do produto', `${product.name} é vendido por quilo. Digite o peso mostrado na balança.`, {eyebrow: 'Venda por peso', fieldLabel: 'Peso (kg)', placeholder: 'Ex.: 0,350', inputMode: 'decimal', acceptLabel: 'Lançar',
      validate: async value => { const parsed = parseQty(value); if (parsed <= 0) throw Error('Informe um peso maior que zero'); if (parsed > 1000000) throw Error('Peso acima do limite permitido'); }});
    if (raw === null) return;
    quantity = parseQty(raw);
  }
  if (quantity === null) quantity = 1000;
  if (!Number.isSafeInteger(quantity) || quantity <= 0) throw Error('Quantidade inválida');
  if (product.unit === 'UN' && quantity % 1000) throw Error(`${product.name} é vendido por unidade; use quantidade inteira`);
  const total = (existing?.quantity_milli || 0) + quantity;
  const stockError = stockLimitError(product, total);
  if (stockError) throw Error(stockError);
  if (existing) Object.assign(existing, product, {quantity_milli: total}); else state.cart.push({...product, quantity_milli: quantity});
  if (state.scale.awaitingId === product.id) state.scale.awaitingId = null;
  touch(product.id);
  cartChanged();
  beep('ok');
  focusScan();
}
function moveSelection(step) {
  if (!state.cart.length) return;
  const index = state.cart.findIndex(item => item.id === state.selectedId);
  const next = Math.max(0, Math.min(state.cart.length - 1, (index < 0 ? state.cart.length - 1 : index) + step));
  state.selectedId = state.cart[next].id;
  renderCart();
}
async function changeQuantity(id = state.selectedId || state.lastItemId) {
  const item = state.cart.find(row => row.id === id);
  if (!item) return;
  const byWeight = item.unit === 'KG';
  const raw = await textDialog(byWeight ? 'Peso do item' : 'Quantidade do item', item.name, {eyebrow: 'Quantidade · F3', fieldLabel: byWeight ? 'Peso (kg)' : `Quantidade (${item.unit})`,
    placeholder: byWeight ? 'Ex.: 0,350' : 'Ex.: 3', inputMode: 'decimal', acceptLabel: 'Aplicar', value: item.quantity_milli ? qtyText(item.quantity_milli, item.unit) : '',
    validate: async value => {
      const parsed = parseQty(value);
      if (parsed <= 0) throw Error(byWeight ? 'Informe um peso maior que zero' : 'Informe uma quantidade maior que zero. Para retirar o item, use F8.');
      if (item.unit === 'UN' && parsed % 1000) throw Error('Este produto aceita somente quantidade inteira');
      const stockError = stockLimitError(item, parsed);
      if (stockError) throw Error(stockError);
    }});
  if (raw === null) return;
  const quantity = parseQty(raw);
  if (quantity < item.quantity_milli && !(await authorizeRemoval('Autorizar redução de quantidade', `${item.name}: de ${qtyText(item.quantity_milli, item.unit)} para ${qtyText(quantity, item.unit)} ${item.unit}.`))) return;
  item.quantity_milli = quantity;
  if (state.scale.awaitingId === item.id) state.scale.awaitingId = null;
  touch(item.id);
  cartChanged();
  focusHome();
}
async function removeItem(id = state.selectedId || state.lastItemId) {
  const item = state.cart.find(row => row.id === id);
  if (!item) return;
  if (item.quantity_milli > 0 && !(await authorizeRemoval('Autorizar cancelamento do item', `${item.name} · ${qtyText(item.quantity_milli, item.unit)} ${item.unit} · ${money(lineNet(item))} sai da venda.`))) return;
  const index = state.cart.indexOf(item);
  state.cart.splice(index, 1);
  if (state.scale.awaitingId === item.id) state.scale.awaitingId = null;
  state.selectedId = state.cart[Math.min(index, state.cart.length - 1)]?.id || null;
  if (state.lastItemId === item.id) state.lastItemId = state.cart.at(-1)?.id || null;
  if (state.pay.active && !state.cart.length) cancelPayment();
  cartChanged();
  focusHome();
}
function clearLocalSale() {
  state.cart = [];
  state.selectedId = state.lastItemId = null;
  state.discount = null;
  state.customer = emptyCustomer();
  state.pay = {active: false, method: null, other: null};
  state.pendingQty = null;
  state.scale.awaitingId = null;
  state.saleRequest = null;
  state.quote = null;
  for (const id of ['payCash', 'paySplitCash', 'paySplitPix', 'paySplitDebit', 'paySplitCredit', 'productSearch']) $(id).value = '';
  payError('');
  renderPendingQty();
}
function resetSale({keepDone = false} = {}) {
  clearLocalSale();
  if (!keepDone) state.done = null;
  renderSale();
  focusScan();
}
async function discardSale() {
  if (!state.cart.length) { resetSale(); return; }
  if (!(await authorizeRemoval('Autorizar cancelamento da venda', `A venda atual, com ${state.cart.length} item(ns) e total de ${money(cartTotal())}, será descartada.`))) return;
  resetSale();
  toast('Venda cancelada');
}

/* ---------- sale rendering ---------- */
function promoTag(item) {
  const line = quoteLine(item);
  return line?.promo_discount_cents ? `<span class="promo-tag">${ico('tag')}${esc(line.promotion_label || line.promotion_name)} · −${money(line.promo_discount_cents)}</span>` : '';
}
function renderCart() {
  const list = $('cartItems');
  if (!state.cart.length) {
    list.innerHTML = `<div class="items-empty">${state.done ? 'Venda concluída. Leia o primeiro item da próxima venda.' : 'Nenhum item lançado. Leia o código de barras ou tecle F2 para buscar no catálogo.'}</div>`;
  } else {
    const flashAge = Date.now() - state.flashAt;
    list.innerHTML = state.cart.map((item, index) => {
      const selected = item.id === state.selectedId, waiting = item.quantity_milli <= 0;
      const flash = item.id === state.flashId && flashAge < 1500 ? ` flash" style="animation-delay:-${flashAge}ms` : '';
      const code = item.barcode || item.sku || '';
      return `<div class="item-row${selected ? ' selected' : ''}${waiting ? ' waiting' : ''}${flash}" role="option" aria-selected="${selected}" data-id="${esc(item.id)}">
        <span class="ir-n">${index + 1}</span>
        <span class="ir-name"><strong>${esc(item.name)}</strong><small>${money(item.price_cents)}/${esc(item.unit)}${code ? ' · ' + esc(code) : ''} ${promoTag(item)}</small></span>
        <button type="button" class="ir-qty" data-qty="${esc(item.id)}" title="Alterar quantidade (F3)">${waiting ? 'Aguardando peso' : `${qtyText(item.quantity_milli, item.unit)} ${esc(item.unit)}`}</button>
        <span class="ir-total">${money(lineNet(item))}</span>
        <button type="button" class="ir-remove" data-remove="${esc(item.id)}" title="Cancelar item (F8)" aria-label="Cancelar item ${esc(item.name)}">${ico('x')}</button>
      </div>`;
    }).join('');
    list.querySelector('.item-row.selected')?.scrollIntoView({block: 'nearest'});
  }
  const last = state.cart.find(item => item.id === state.lastItemId), box = $('lastItem');
  if (!last) {
    box.className = 'last-item empty';
    box.innerHTML = `<span class="li-kicker">Último item</span><span class="li-name">${state.done ? 'Aguardando a próxima venda' : 'Aguardando leitura'}</span><span class="li-qty">Dica: digite 3*código para lançar 3 unidades de uma vez.</span>`;
  } else {
    box.className = 'last-item';
    box.innerHTML = `<span class="li-kicker">Último item</span><span class="li-name">${esc(last.name)}</span>
      <div class="li-row"><span class="li-qty">${last.quantity_milli > 0 ? `${qtyText(last.quantity_milli, last.unit)} ${esc(last.unit)} × ${money(last.price_cents)}` : 'Aguardando peso da balança'} ${promoTag(last)}</span><strong class="li-total">${money(lineNet(last))}</strong></div>`;
  }
}
function discountLabel() {
  const discount = state.discount;
  if (!discount) return '';
  return discount.mode === 'percent' ? discount.value.toLocaleString('pt-BR', {maximumFractionDigits: 2}) + '%' : 'em valor';
}
function customerSummary() {
  const customer = state.customer, registered = state.customers.find(row => row.id === customer.id);
  const seller = (state.session?.operators || []).find(operator => operator.id === customer.sellerId);
  return [registered ? registered.name : customer.name, customer.document ? 'CPF/CNPJ ' + documentText(customer.document) : '', seller ? 'Vendedor ' + seller.name : ''].filter(Boolean).join(' · ');
}
function renderTotals() {
  const total = cartTotal(), text = moneyPlain(total), node = $('cartTotal');
  node.innerHTML = `<span class="cur">R$</span>${text}`;
  node.dataset.len = text.length >= 9 ? 'long' : '';
  const count = state.cart.length;
  $('cartCount').textContent = count ? `${count} ${count === 1 ? 'item' : 'itens'}` : 'Nenhum item';
  const promo = cartPromo(), discount = cartDiscount(), parts = [];
  if (promo || discount) parts.push(`<span>Subtotal ${money(cartGross())}</span>`);
  if (promo) parts.push(`<span class="saving">${ico('tag')} Economia nas promoções −${money(promo)}</span>`);
  if (discount) parts.push(`<span class="saving">Desconto ${esc(discountLabel())} −${money(discount)}${C.discountNeedsPin(role(), discount, cartBase()) ? ' ' + ico('lock') : ''}</span>`);
  const customer = customerSummary();
  if (customer) parts.push(`<span class="customer">${ico('user')} ${esc(customer)}</span>`);
  $('totalsMeta').innerHTML = parts.join('');
}
function payBlockReason() {
  const session = state.session;
  if (!canSell(role())) return 'Seu perfil não realiza vendas';
  if (!session.license?.can_sell) return session.license?.reason || 'Licença indisponível';
  if (!session.cash) return 'Abra o caixa antes de vender (F12)';
  if (!state.cart.length) return 'Nenhum item na venda';
  const waiting = state.cart.find(item => item.quantity_milli <= 0);
  return waiting ? `Informe o peso de ${waiting.name} (F3)` : '';
}
function renderPaymentMethods() {
  const options = C.paymentOptions(state.session?.payment_methods), signature = JSON.stringify(options);
  if (signature !== state.payOptionsSig) {
    state.payOptionsSig = signature;
    state.payOptions = options;
    $('paymentMethods').innerHTML = options.map((option, index) => `<button type="button" class="pay-method" data-pay="${index}" title="${esc(option.label)}">${option.key ? `<kbd>${option.key}</kbd>` : ''}<span>${esc(option.short || option.label)}</span></button>`).join('')
      + '<button type="button" class="pay-method" data-pay="split" title="Dividir o pagamento em mais de uma forma"><kbd>0</kbd><span>Dividir</span></button>';
    const enabled = method => options.some(option => option.method === method);
    for (const [id, method] of [['paySplitCash', 'cash'], ['paySplitPix', 'pix'], ['paySplitDebit', 'card_debit'], ['paySplitCredit', 'card_credit']]) $(id).closest('.field').classList.toggle('hidden', !enabled(method));
  }
  const {method, other} = state.pay;
  if (method && method !== 'split' && !options.some(option => option.method === method && (method !== 'other' || option.id === other?.id))) state.pay = {active: state.pay.active, method: null, other: null};
}
function paymentState() {
  const total = cartTotal(), method = state.pay.method;
  try {
    if (method === 'cash') {
      const raw = $('payCash').value.trim();
      return raw ? {paid: parseMoney(raw), change: parseMoney(raw) - total} : {paid: total, change: 0};
    }
    if (method === 'split') {
      const parts = [['paySplitCash', 'cash'], ['paySplitPix', 'pix'], ['paySplitDebit', 'card_debit'], ['paySplitCredit', 'card_credit']]
        .map(([id, kind]) => ({method: kind, amount_cents: parseMoney($(id).value)})).filter(part => part.amount_cents > 0);
      const paid = parts.reduce((sum, part) => sum + part.amount_cents, 0);
      return {paid, change: paid - total, parts, cash: parts.find(part => part.method === 'cash')?.amount_cents || 0};
    }
  } catch (error) { return {paid: 0, change: 0, error: error.message}; }
  return {paid: method ? total : 0, change: 0};
}
function buildPayments() {
  const total = cartTotal(), method = state.pay.method, current = paymentState();
  if (!method) throw Error('Escolha a forma de pagamento');
  if (current.error) throw Error(current.error);
  if (method === 'cash') {
    if (current.change < 0) throw Error(`Falta ${money(-current.change)} para fechar a venda`);
    return [{method: 'cash', amount_cents: current.paid}];
  }
  if (method === 'split') {
    if (!current.parts.length) throw Error('Informe os valores do pagamento dividido');
    if (current.change < 0) throw Error(`Falta ${money(-current.change)} para fechar a venda`);
    if (current.change > current.cash) throw Error('O troco só pode sair do valor em dinheiro');
    return current.parts;
  }
  if (method === 'other') return [{method: 'other', payment_method_id: state.pay.other.id, amount_cents: total}];
  return [{method, amount_cents: total}];
}
function payError(message) {
  const box = $('payError');
  box.textContent = message || '';
  box.classList.toggle('show', !!message);
  if (message) beep('error');
}
function renderPay() {
  const {active, method, other} = state.pay, total = cartTotal(), options = state.payOptions;
  $('paymentMethods').querySelectorAll('[data-pay]').forEach(button => {
    const option = button.dataset.pay === 'split' ? {method: 'split'} : options[Number(button.dataset.pay)];
    button.classList.toggle('selected', active && !!method && option?.method === method && (method !== 'other' || option.id === other?.id));
  });
  $('payHint').textContent = !active ? 'F10 para pagar' : method ? 'Enter confirma · Esc volta' : 'Escolha a forma pelo número';
  $('payCashBox').classList.toggle('hidden', !(active && method === 'cash'));
  $('paySplitBox').classList.toggle('hidden', !(active && method === 'split'));
  const info = $('payInfo'), customer = state.customers.find(row => row.id === state.customer.id);
  let text, kind = 'ready';
  if (!active) { text = state.cart.length ? 'Tecle F10 ou toque na forma de pagamento.' : 'Lance os itens e tecle F10 para receber.'; kind = ''; }
  else if (!method) { text = 'Escolha a forma de pagamento.'; kind = 'pending'; }
  else if (method === 'cash') text = '';
  else if (method === 'pix') text = `Confira o recebimento de ${money(total)} no Pix e tecle Enter.`;
  else if (method === 'card_debit' || method === 'card_credit') text = `Passe ${money(total)} na maquininha (${method === 'card_debit' ? 'débito' : 'crédito'}) e tecle Enter.`;
  else if (method === 'other') text = `${other?.name || 'Outra forma'}: ${money(total)}. Tecle Enter para confirmar.`;
  else if (method === 'split') text = '';
  else if (!customer) { text = 'Escolha o cliente cadastrado (F4) para esta forma.'; kind = 'pending'; }
  else if (method === 'credit_account') { const over = total > customer.available_credit_cents; text = `A prazo para ${customer.name} · disponível ${money(customer.available_credit_cents)}${over ? ' · acima do limite: pede o PIN do gerente' : ''}`; if (over) kind = 'pending'; }
  else { const short = total > customer.store_credit_cents; text = `Crédito na loja de ${customer.name} · saldo ${money(customer.store_credit_cents)}${short ? ' · saldo insuficiente' : ''}`; if (short) kind = 'pending'; }
  info.textContent = text;
  info.className = 'pay-info ' + kind + (text ? '' : ' hidden');
  const current = paymentState(), change = $('changeBlock');
  const shown = active && (method === 'cash' || method === 'split') ? current.change : 0;
  $('changeLabel').textContent = shown < 0 ? 'Falta' : 'Troco';
  $('changeValue').textContent = money(Math.abs(shown));
  change.className = 'change-block' + (shown > 0 ? ' positive' : shown < 0 ? ' negative' : '');
  const button = $('sellBtn'), blocked = payBlockReason();
  const label = state.submitting ? 'Registrando a venda…' : active ? (method ? 'Confirmar venda <kbd>Enter</kbd>' : 'Escolha a forma de pagamento') : 'Pagar <kbd>F10</kbd>';
  if (button.dataset.label !== label) { button.innerHTML = label; button.dataset.label = label; }
  button.disabled = state.submitting || (active ? !method : !!blocked);
  button.title = !active && blocked ? blocked : '';
}
function renderDone() {
  const done = state.done;
  if (state.doneRendered === done.stamp) return;
  state.doneRendered = done.stamp;
  $('donePanel').innerHTML = `<span class="done-mark">${ico('check')}</span><h2>Venda concluída</h2>
    <span class="done-label">Troco</span><strong class="done-change">${money(done.sale.change_cents)}</strong>
    <span class="done-meta">Total ${money(done.sale.total_cents)} · ${esc(done.paymentsText)}</span>
    <span class="done-print">${esc(done.printStatus)}</span>
    <div class="done-actions"><button type="button" class="btn" data-action="reprint">Comprovante <kbd>F9</kbd></button><button type="button" class="btn primary" data-done-dismiss>Nova venda <kbd>Enter</kbd></button></div>`;
}
function renderPanels() {
  const session = state.session;
  if (!session?.operator) return;
  let blocked = null;
  if (!session.license?.can_sell) blocked = licenseBlock(session.license);
  else if (!session.cash && canSell(role())) blocked = {title: 'Caixa fechado', text: 'Informe o dinheiro em espécie da gaveta para abrir o caixa e começar a vender.', open: true};
  const signature = JSON.stringify(blocked);
  const panel = $('blockedPanel');
  panel.classList.toggle('hidden', !blocked);
  if (blocked && panel.dataset.signature !== signature) {
    panel.dataset.signature = signature;
    panel.innerHTML = `<h2>${esc(blocked.title)}</h2><p>${esc(blocked.text)}</p>${blocked.steps ? `<ol class="guide-steps">${blocked.steps.map(step => `<li>${esc(step)}</li>`).join('')}</ol>` : ''}${blocked.recheck ? '<button type="button" class="btn primary big" data-action="recheckLicense">Verificar de novo</button>' : ''}${blocked.open ? '<button type="button" class="btn primary big" data-action="closeCash">Abrir caixa <kbd>F12</kbd></button>' : ''}`;
  }
  const done = !blocked && state.done;
  $('donePanel').classList.toggle('hidden', !done);
  if (done) renderDone(); else state.doneRendered = null;
  $('payPanel').classList.toggle('hidden', !!blocked || !!done);
  renderPay();
}
function renderPendingQty() {
  const badge = $('pendingQtyBadge');
  badge.classList.toggle('hidden', !state.pendingQty);
  badge.textContent = state.pendingQty ? `${qtyText(state.pendingQty)} ×` : '';
}
function renderSale() {
  renderCart();
  renderTotals();
  renderPanels();
  renderKeybar();
  renderScaleChip();
  persistCart();
  refreshQuote();
}

/* ---------- reading products ---------- */
function scanStatus(kind, text) {
  const box = $('scanStatus');
  box.dataset.kind = kind;
  box.lastElementChild.textContent = text;
}
function scanError(message) {
  scanStatus('error', message);
  beep('error');
  const input = $('productSearch');
  if (document.activeElement === input) input.select();
}
async function lookupCode(code) {
  const result = await api('/api/products?q=' + encodeURIComponent(code));
  const wanted = code.toLowerCase();
  const exact = (result.products || []).find(product => [product.barcode, product.sku, product.id].some(value => value != null && String(value).toLowerCase() === wanted));
  if (exact) return {product: exact, matches: []};
  const label = C.scaleLabel(code);
  if (label) {
    // Resolved by the terminal backend over the whole catalog, not over the first page of products.
    try {
      const found = await api('/api/products/scale/' + label.scaleCode);
      const quantity = C.scaleLabelQuantity(label, found.product, state.scale.barcodeMode);
      if (quantity) return {product: found.product, quantity, label: true, matches: []};
    } catch (error) { if (error.status !== 404) throw error; }
  }
  return {product: null, matches: /^\d{8,14}$/.test(code) ? [] : result.products || []};
}
async function processScan() {
  const input = $('productSearch'), raw = input.value.trim();
  if (!raw) { if (state.done) dismissDone(); return; }
  if (state.scanBusy) return;
  state.scanBusy = true;
  try {
    const parsed = C.parseScanInput(raw);
    if (parsed.quantityMilli !== null && !parsed.code) {
      state.pendingQty = parsed.quantityMilli;
      input.value = '';
      renderPendingQty();
      scanStatus('', `Próxima leitura entra com quantidade ${qtyText(parsed.quantityMilli)}`);
      return;
    }
    const found = await lookupCode(parsed.code);
    if (found.product) {
      input.value = '';
      await addProduct(found.product, parsed.quantityMilli ?? found.quantity ?? null);
      scanStatus('ok', found.label ? `${found.product.name} · etiqueta da balança lida` : `${found.product.name} lançado`);
    } else if (found.matches.length) {
      input.value = '';
      scanStatus('', `Vários produtos para “${parsed.code}” · escolha no catálogo`);
      await openSearch(parsed.code, parsed.quantityMilli);
    } else scanError(`Código não encontrado: ${parsed.code}`);
  } catch (error) { scanError(error.message); } finally { state.scanBusy = false; }
}
function groupProducts(products) {
  const groups = {}, singles = [];
  for (const product of products) { if (product.group_id) (groups[product.group_id] ||= []).push(product); else singles.push(product); }
  return [...singles, ...Object.values(groups).map(list => (list.length === 1 ? list[0] : {...list[0], __group: list}))]
    .sort((a, b) => String(a.__group ? a.group_name : a.name).localeCompare(String(b.__group ? b.group_name : b.name), 'pt-BR'));
}
function productImageUrl(product) {
  const source = String(product?.image_url || '');
  if (!source) return '/static/product-images/generic.svg';
  return source.startsWith('/assets/product-images/') ? source.replace('/assets/product-images/', '/static/product-images/') : source;
}
function productCard(product, index, active) {
  const image = `<img class="product-image" src="${esc(productImageUrl(product))}" alt="" loading="lazy" onerror="this.onerror=null;this.src='/static/product-images/generic.svg'">`;
  if (product.__group) {
    const prices = product.__group.map(variant => Number(variant.price_cents)), low = Math.min(...prices), high = Math.max(...prices);
    return `<button type="button" class="product${active ? ' active' : ''}" role="option" aria-selected="${active}" data-index="${index}">${image}<span><span class="p-name">${esc(product.group_name || product.name)}</span><span class="p-meta">${product.__group.length} variações · escolha a grade</span><span class="p-price">${low === high ? money(low) : `${money(low)} a ${money(high)}`}</span></span></button>`;
  }
  const out = isOut(product), code = product.barcode ? `EAN ${product.barcode}` : product.sku || '';
  const stock = out ? 'Sem estoque' : product.stock_controlled === false ? 'Venda liberada' : `${qtyText(product.stock_milli, product.unit)} ${product.unit} em estoque`;
  return `<button type="button" class="product${active ? ' active' : ''}${out ? ' out' : ''}" role="option" aria-selected="${active}" data-index="${index}">${image}<span><span class="p-name">${esc(product.name)}</span><span class="p-meta">${esc(code || stock)}</span><span class="p-price">${money(product.price_cents)}/${esc(product.unit)}${code ? ` <small>· ${esc(stock)}</small>` : ''}</span></span></button>`;
}
// Catalog overlay: typing filters, arrows choose, Enter adds and closes; a tap adds and keeps it open.
async function openSearch(query = '', quantity = null) {
  const modal = openModal({title: 'Catálogo de produtos', eyebrow: 'Buscar · F2', className: 'xl catalog', onKey: (event, self) => {
    if (['ArrowDown', 'ArrowUp', 'PageDown', 'PageUp'].includes(event.key)) {
      event.preventDefault();
      const step = {ArrowDown: 1, ArrowUp: -1, PageDown: 6, PageUp: -6}[event.key];
      setActive(Math.max(0, Math.min(items.length - 1, active + step)));
      return true;
    }
    if (event.key === 'Enter' && (event.target === input || event.target.classList?.contains('product'))) {
      event.preventDefault();
      self.run(() => pick(active, false));
      return true;
    }
    if (event.key.length === 1 && !event.ctrlKey && !event.metaKey && !event.altKey && !event.target.matches?.('input')) input.focus();
    return false;
  }});
  const tools = el('div', 'catalog-tools', `<label class="search-field">${ico('search')}<input placeholder="Digite nome, SKU ou código de barras" autocomplete="off" spellcheck="false" aria-label="Buscar produto"></label><div class="chips-row" role="group" aria-label="Categorias"></div>`);
  const input = tools.querySelector('input'), chips = tools.querySelector('.chips-row'), note = el('p', 'hint'), grid = el('div', 'products-grid');
  grid.setAttribute('role', 'listbox');
  modal.body.append(tools, note, grid);
  modal.action('Fechar <kbd>Esc</kbd>', '', () => modal.close());
  let items = [], active = 0, category = '', sequence = 0, timer = null;
  input.value = query;
  const setActive = index => {
    active = index;
    grid.querySelectorAll('.product').forEach((card, position) => { card.classList.toggle('active', position === active); card.setAttribute('aria-selected', position === active); });
    grid.querySelector('.product.active')?.scrollIntoView({block: 'nearest'});
  };
  const render = () => {
    chips.innerHTML = ['', ...state.categories].map(name => `<button type="button" class="cat-chip${name === category ? ' active' : ''}" data-category="${esc(name)}">${esc(name || 'Todos')}</button>`).join('');
    const term = input.value.trim();
    grid.innerHTML = items.length ? items.map((product, index) => productCard(product, index, index === active)).join('')
      : `<div class="pick-empty">${term ? `Nada encontrado para “${esc(term)}”. Confira o nome ou o código.` : 'Nenhum produto neste terminal. Use Menu › Atualizar catálogo.'}</div>`;
  };
  const load = async () => {
    const mine = ++sequence;
    const data = await api('/api/products?q=' + encodeURIComponent(input.value.trim()) + (category ? '&category=' + encodeURIComponent(category) : ''));
    if (mine !== sequence) return;
    state.categories = data.categories || [];
    items = groupProducts(data.products || []);
    active = 0;
    const shown = (data.products || []).length, total = Number(data.total) || shown;
    note.textContent = total > shown ? `Mostrando ${shown} de ${total} produtos. Digite parte do nome ou do código para refinar.` : items.length ? 'Setas escolhem · Enter lança e fecha · toque lança e mantém aberto' : '';
    render();
  };
  const pick = async (index, keepOpen) => {
    const chosen = items[index];
    if (!chosen) return;
    const product = chosen.__group ? await variantPicker(chosen.__group) : chosen;
    if (!product) return;
    if (isOut(product)) throw Error(`${product.name} está sem estoque neste terminal`);
    if (!keepOpen) modal.close();
    try {
      await addProduct(product, quantity);
      quantity = null;
      if (keepOpen) note.textContent = `${product.name} lançado · ${state.cart.length} item(ns) na venda`;
      else scanStatus('ok', `${product.name} lançado`);
    } catch (error) { if (keepOpen) throw error; scanError(error.message); }
  };
  input.addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(() => load().catch(error => modal.error(error.message)), 150); });
  chips.addEventListener('click', event => { const chip = event.target.closest('[data-category]'); if (!chip) return; category = chip.dataset.category; load().catch(error => modal.error(error.message)); input.focus(); });
  grid.addEventListener('click', event => { const card = event.target.closest('.product'); if (!card) return; setActive(Number(card.dataset.index)); modal.run(() => pick(active, true)); });
  input.focus();
  input.select();
  await load().catch(error => modal.error(error.message));
  await modal.promise;
  focusHome();
}
function variantPicker(variants) {
  const sorted = [...variants].sort((a, b) => String(a.variant || a.name).localeCompare(String(b.variant || b.name), 'pt-BR', {numeric: true}));
  const modal = openModal({title: sorted[0].group_name || sorted[0].name, eyebrow: 'Escolha a variação', className: 'wide', onKey: arrowNav('.variant-option')});
  const grid = el('div', 'variant-grid', sorted.map((variant, index) => `<button type="button" class="variant-option" data-index="${index}" ${isOut(variant) ? 'disabled' : ''}><strong>${esc(variant.variant || variant.name)}</strong><small>${money(variant.price_cents)} · ${isOut(variant) ? 'sem estoque' : `${qtyText(variant.stock_milli, variant.unit)} ${esc(variant.unit)}`}</small></button>`).join(''));
  modal.body.append(grid);
  modal.action('Voltar', '', () => modal.close(null));
  grid.addEventListener('click', event => { const button = event.target.closest('.variant-option'); if (button) modal.close(sorted[Number(button.dataset.index)]); });
  (grid.querySelector('.variant-option:not([disabled])') || modal.actions.firstElementChild).focus();
  return modal.promise;
}

/* ---------- payment on the sale screen ---------- */
function cancelPayment() {
  state.pay = {active: false, method: null, other: null};
  for (const id of ['payCash', 'paySplitCash', 'paySplitPix', 'paySplitDebit', 'paySplitCredit', 'productSearch']) $(id).value = '';
  payError('');
}
function startPayment() {
  const reason = payBlockReason();
  if (reason) throw Error(reason);
  state.pay = {active: true, method: null, other: null};
  state.done = null;
  payError('');
  // The reading field leaves the scene: the digits typed next choose the payment form.
  const scan = $('productSearch');
  scan.value = '';
  scan.blur();
  renderSale();
  $('paymentMethods').focus();
}
async function selectPayMethod(option) {
  const reason = payBlockReason();
  if (reason) { toast(reason, true); beep('error'); return; }
  cancelPayment();
  state.done = null;
  state.pay = option === 'split' ? {active: true, method: 'split', other: null}
    : {active: true, method: option.method, other: option.method === 'other' ? {id: option.id, name: option.label} : null};
  state.pay.pickedAt = performance.now();
  renderSale();
  // Focus right away (same key event), so the next digits already land in the amount field.
  if (['cash', 'split'].includes(state.pay.method)) focusPay();
  if (['credit_account', 'store_credit'].includes(state.pay.method) && !state.customer.id) await customerDialog('customer');
  renderSale();
  focusPay();
}
let payBurstUntil = 0;
// A digit picks the payment form in the same key event, so nothing typed next is lost or leaks to
// another field. The digits of a barcode read while paying are dropped (too fast for a person), and
// the Enter that ends the reading undoes whatever its first digit picked (handlePayKey).
function pickPayDigit(key) {
  const times = typing.times, now = performance.now();
  const gap = times.length >= 2 ? times[times.length - 1] - times[times.length - 2] : Infinity;
  if (gap < 60 || now < payBurstUntil) { payBurstUntil = now + 300; return; }
  const option = key === '0' ? 'split' : state.payOptions.find(row => row.key === key);
  if (option) selectPayMethod(option); else beep('error');
}
function paymentName(method, configuredId, names) {
  if (method === 'other') return (names || {})[configuredId] || (state.session?.payment_methods || []).find(row => row.id === configuredId)?.name || 'Outra forma';
  return ({cash: 'Dinheiro', pix: 'Pix', card: 'Cartão', card_credit: 'Crédito', card_debit: 'Débito', credit_account: 'A prazo (fiado)', store_credit: 'Crédito na loja'})[method] || method || 'Pagamento';
}
async function confirmSale() {
  if (state.submitting) return;
  payError('');
  let payments;
  try {
    const reason = payBlockReason();
    if (reason) throw Error(reason);
    if (['credit_account', 'store_credit'].includes(state.pay.method) && !state.customer.id) throw Error('Escolha o cliente cadastrado (F4) para esta forma de pagamento');
    payments = buildPayments();
  } catch (error) { payError(error.message); return; }
  state.submitting = true;
  renderPay();
  try {
    // Always requote: prices and promotions may have changed since the items were read.
    const shown = cartTotal();
    await refreshQuote(true);
    if (cartTotal() !== shown) throw Error(`O total foi atualizado para ${money(cartTotal())}. Confira e confirme novamente.`);
    const base = cartBase(), discount = cartDiscount();
    let pin = '';
    if (C.discountNeedsPin(role(), discount, base)) {
      pin = await pinDialog('Autorizar desconto', `Desconto de ${money(discount)} sobre ${money(base)} está acima do limite deste operador.`,
        {validate: value => api('/api/discount/authorize', 'POST', {pin: value, gross_cents: base, discount_cents: discount})});
      if (!pin) throw Error('Venda não registrada: o desconto precisa da autorização do gerente.');
    }
    state.saleRequest ||= uuid();
    const request = {request_id: state.saleRequest, items: state.cart.map(item => ({product_id: item.id, quantity_milli: item.quantity_milli})), discount_cents: discount,
      customer_name: state.customer.name.trim(), customer_document: state.customer.document.trim(), customer_id: state.customer.id || null, seller_id: state.customer.sellerId || null, payments};
    let sale;
    try { sale = await api('/api/sales', 'POST', {...request, manager_pin: pin}); } catch (error) {
      if (error.status !== 403 || pin || !/PIN de gerente/.test(error.message)) throw error;
      pin = await pinDialog('Autorização do gerente', error.message);
      if (!pin) throw Error('Venda não registrada: a autorização do gerente foi cancelada.');
      sale = await api('/api/sales', 'POST', {...request, manager_pin: pin});
    }
    pin = '';
    finishSale(sale);
  } catch (error) { payError(error.message); } finally {
    state.submitting = false;
    renderPay();
    if (state.pay.active) focusPay();
  }
}
function finishSale(sale) {
  state.lastSale = sale;
  state.done = {sale, stamp: sale.id + ':0', printStatus: 'Enviando o comprovante…', paymentsText: (sale.payments || []).map(payment => paymentName(payment.method, payment.payment_method_id, sale.payment_names)).join(' + ')};
  resetSale({keepDone: true});
  scanStatus('', 'Leitor pronto');
  beep('ok');
  poll().catch(() => {});
  loadCustomers().catch(() => {});
  printAfterSale().then(plan => {
    if (state.done?.sale === sale) { state.done.printStatus = plan.status; state.done.stamp = sale.id + ':1'; renderPanels(); }
    if (plan.preview) receiptDialog(sale, plan.status, {autoPrint: plan.autoPrint}).catch(() => {});
  });
}
// Background printing. The system print dialog is never opened behind the operator's back when a
// thermal printer is configured, so a CUPS queue cannot print the same receipt twice.
async function printAfterSale() {
  const preview = prefs.get('receiptPreview', 'off') === 'on';
  try {
    const data = await api('/api/print/settings');
    applyPrinterData(data);
    const receipt = (data.settings || []).find(setting => setting.kind === 'receipt');
    if (!receipt) return {status: 'Sem impressora de recibos configurada. F9 abre o comprovante para imprimir pelo sistema.', preview, autoPrint: preview};
    const result = await api('/api/print/flush', 'POST', {});
    loadPrinterStatus().catch(() => {});
    if (result.status === 'uncertain') return {status: 'A impressora não confirmou o comprovante. Verifique o equipamento; F9 reimprime.', preview};
    return {status: `Comprovante enviado para ${receipt.target || 'a impressora térmica'}.`, preview};
  } catch { return {status: 'Venda gravada. Não foi possível consultar a impressora; F9 abre o comprovante.', preview}; }
}
function dismissDone() {
  state.done = null;
  renderSale();
  focusScan();
}

/* ---------- receipt ---------- */
function legacyReceiptMarkup(sale) {
  const connection = state.session?.connection || {}, issuer = connection.issuer || {};
  const company = issuer.trade_name || issuer.legal_name || connection.tenant_name || 'PDV2';
  const legalName = issuer.trade_name && issuer.legal_name && issuer.trade_name !== issuer.legal_name ? issuer.legal_name : '';
  const date = sale.created_at ? dateTimeText(sale.created_at) : new Date().toLocaleString('pt-BR');
  const address = issuer.address || {};
  const addressText = [address.line, address.number, address.complement, address.neighborhood, address.city, address.state].filter(Boolean).join(', ');
  const fiscal = sale.fiscal || {}, authorized = Boolean(fiscal.authorized && fiscal.access_key);
  const items = (sale.items || []).map(item => `<div class="receipt-line"><div><strong>${esc(item.name)}</strong><small>${item.sku ? `Cód. ${esc(item.sku)} · ` : ''}${qtyText(item.quantity_milli, item.unit || 'UN')} ${esc(item.unit || 'UN')} × ${money(item.unit_price_cents)}</small></div><strong>${money(item.line_total_cents ?? item.gross_cents)}</strong></div>`).join('') || '<p>Nenhum item registrado.</p>';
  const payments = (sale.payments || []).map(payment => `<div class="receipt-summary-row"><span>${esc(paymentName(payment.method, payment.payment_method_id, sale.payment_names))}</span><strong>${money(payment.amount_cents)}</strong></div>`).join('');
  const fiscalBlock = authorized ? `<section class="receipt-fiscal"><strong>DANFE NFC-e autorizada</strong><span>Número ${esc(fiscal.number || '—')} · Série ${esc(fiscal.series || '—')}</span><span>Chave de acesso: ${esc(fiscal.access_key)}</span>${fiscal.protocol ? `<span>Protocolo: ${esc(fiscal.protocol)}</span>` : ''}</section>` : '<section class="receipt-fiscal"><strong>NFC-e não emitida</strong><span>Comprovante de venda sem valor fiscal.</span></section>';
  return `<div class="receipt-paper"><header class="receipt-header"><strong>${esc(company)}</strong>${legalName ? `<span>${esc(legalName)}</span>` : ''}${issuer.document ? `<span>CNPJ/CPF: ${esc(documentText(issuer.document))}</span>` : ''}${addressText ? `<span>${esc(addressText)}</span>` : ''}${connection.shop_name ? `<span>${esc(connection.shop_name)}</span>` : ''}<small>${authorized ? 'DANFE NFC-e' : 'Comprovante de venda · não fiscal'}</small></header><div class="receipt-meta"><span>${esc(date)}</span><span>${connection.terminal_name ? `Caixa ${esc(connection.terminal_name)}` : ''}</span></div><div class="receipt-items">${items}</div><div class="receipt-totals"><div class="receipt-summary-row"><span>Subtotal</span><strong>${money(sale.gross_cents)}</strong></div>${Number(sale.discount_cents) > 0 ? `<div class="receipt-summary-row"><span>Desconto</span><strong>− ${money(sale.discount_cents)}</strong></div>` : ''}<div class="receipt-summary-row receipt-total"><span>Total</span><strong>${money(sale.total_cents)}</strong></div>${payments}${Number(sale.change_cents) > 0 ? `<div class="receipt-summary-row"><span>Troco</span><strong>${money(sale.change_cents)}</strong></div>` : ''}</div>${sale.customer_name || sale.customer_document ? `<div class="receipt-customer">${sale.customer_name ? `Cliente: ${esc(sale.customer_name)}<br>` : ''}${sale.customer_document ? `CPF/CNPJ: ${esc(documentText(sale.customer_document))}` : ''}</div>` : ''}${fiscalBlock}<footer>Obrigado pela preferência!<small>PDV2 - crowntech.com.br</small></footer></div>`;
}
function receiptMarkup(sale) {
  const model = sale.receipt;
  if (!model || !Array.isArray(model.blocks)) return legacyReceiptMarkup(sale);
  const size = C.receiptDimensions(sale);
  return `<article class="receipt-paper receipt-paper-canonical" aria-label="Comprovante em bobina de ${size.width} milímetros" style="--receipt-paper-mm:${size.width}mm;--receipt-printable-mm:${size.printable}mm;--receipt-columns:${size.columns};--receipt-feed-mm:${size.feed}mm;--receipt-screen-inset:${(size.width - size.printable) * 50 / size.width}%;--receipt-feed-ratio:${size.feed / size.printable}"><div class="receipt-content">${C.receiptBlocksMarkup(model, size)}</div><div class="receipt-bottom-feed" aria-hidden="true"></div></article>`;
}
async function receiptDialog(sale, printStatus = '', options = {}) {
  if (!sale.receipt && sale.id) {
    try { sale = (await api('/api/sales/' + encodeURIComponent(sale.id))).sale || sale; } catch { /* show what we have */ }
  }
  const size = C.receiptDimensions(sale);
  const modal = openModal({title: options.title || 'Comprovante da venda', eyebrow: sale.status === 'voided' ? 'Venda cancelada' : options.eyebrow || 'Venda registrada', text: 'Confira a via do cliente antes de entregar.', className: 'receipt-dialog', backdropClass: 'receipt-backdrop'});
  modal.dialog.style.setProperty('--receipt-paper-mm', size.width + 'mm');
  const stage = el('div', 'receipt-preview-stage', `<div class="receipt-preview-label"><span>Prévia de impressão</span><span>Bobina ${size.width} mm</span></div><div class="receipt-preview-paper">${receiptMarkup(sale)}</div>`);
  stage.tabIndex = 0;
  stage.setAttribute('role', 'region');
  stage.setAttribute('aria-label', 'Prévia do comprovante. Role para conferir todos os itens.');
  modal.body.append(stage);
  const status = el('p', 'receipt-print-status');
  status.setAttribute('role', 'status');
  const setStatus = (text, warning = false) => { status.textContent = text; status.className = 'receipt-print-status ' + (warning ? 'warning' : 'success'); };
  setStatus(printStatus || 'O comprovante está pronto para impressão.', /configur|não|incert|falha/i.test(printStatus));
  modal.dialog.insertBefore(status, modal.actions);
  const pageStyle = document.createElement('style');
  let printTimer;
  const prepare = () => {
    const paper = stage.querySelector('.receipt-paper'), content = paper?.querySelector('.receipt-content') || paper;
    const heightMm = Math.ceil((content?.scrollHeight || 900) * size.printable / (content?.offsetWidth || 396) + size.feed + 4);
    pageStyle.textContent = `@media print{@page{size:${size.width}mm ${Math.max(100, heightMm + 5)}mm;margin:0;}}`;
    if (!pageStyle.isConnected) document.head.append(pageStyle);
  };
  const systemPrint = () => { setStatus('Abrindo a impressão do sistema…'); prepare(); printTimer = window.setTimeout(() => window.print(), 120); };
  window.addEventListener('beforeprint', prepare);
  modal.action('Fechar', '', () => modal.close());
  const system = modal.action('Imprimir no sistema', '', systemPrint);
  const thermal = sale.id ? modal.action('Reimprimir na térmica', 'primary', async () => {
    try {
      await api('/api/sales/' + encodeURIComponent(sale.id) + '/reprint', 'POST', {});
      setStatus('Comprovante colocado na fila da impressora térmica.');
      api('/api/print/flush', 'POST', {}).then(loadPrinterStatus).catch(() => {});
    } catch (error) { setStatus(error.status === 409 ? 'Configure uma impressora de recibos antes de reenviar para a fila.' : error.message, true); }
  }) : null;
  (thermal && state.printer?.configured ? thermal : system).focus();
  if (options.autoPrint) printTimer = window.setTimeout(systemPrint, 180);
  await modal.promise;
  window.clearTimeout(printTimer);
  window.removeEventListener('beforeprint', prepare);
  pageStyle.remove();
}
async function salePicker({title, text, eyebrow, onlyCompleted = false}) {
  const data = await api('/api/sales');
  const sales = (data.sales || []).filter(sale => !onlyCompleted || sale.status === 'completed');
  if (!sales.length) { toast(onlyCompleted ? 'Nenhuma venda concluída neste terminal' : 'Nenhuma venda neste terminal', true); return null; }
  const modal = openModal({title, text, eyebrow, className: 'wide', onKey: arrowNav('.pick-row')});
  const list = el('div', 'pick-list', sales.map(sale => `<button type="button" class="pick-row" data-id="${esc(sale.id)}"><span><strong>${money(sale.total_cents)} · ${esc(sale.customer_name || 'Consumidor não identificado')}</strong><small>${dateTimeText(sale.created_at)} · venda ${esc(sale.id.slice(0, 8))} · ${esc((saleStatusNames[sale.status] || [sale.status])[0])}</small></span><span class="pick-side">Abrir</span></button>`).join(''));
  modal.body.append(list);
  modal.action('Cancelar', '', () => modal.close(null));
  list.addEventListener('click', event => { const row = event.target.closest('.pick-row'); if (row) modal.close(row.dataset.id); });
  list.firstElementChild.focus();
  return modal.promise;
}
async function reprint() {
  if (state.done?.sale) { await receiptDialog(state.done.sale, state.done.printStatus); return; }
  const id = await salePicker({title: 'Reimprimir comprovante', text: 'Escolha a venda. A mais recente vem primeiro.', eyebrow: 'Reimprimir · F9'});
  if (!id) return;
  const detail = await api('/api/sales/' + encodeURIComponent(id));
  await receiptDialog(detail.sale, 'Comprovante pronto para reimpressão.');
}

/* ---------- customer, discount, hold, cash ---------- */
async function loadCustomers(query = '') {
  const data = await api('/api/customers' + (query ? '?q=' + encodeURIComponent(query) : ''));
  if (!query) state.customers = data.customers || [];
  return data.customers || [];
}
const customerOptionText = customer => `${customer.name}${customer.credit_limit_cents ? ' · fiado disp. ' + money(customer.available_credit_cents) : ''}${customer.store_credit_cents > 0 ? ' · crédito ' + money(customer.store_credit_cents) : ''}`;
async function customerDialog(focusField = 'document') {
  await loadCustomers().catch(() => {});
  const modal = openModal({title: 'Cliente e CPF na nota', eyebrow: 'Cliente · F4', text: 'Opcional. Informe somente o que o cliente pedir.', className: 'wide'});
  modal.body.innerHTML = `<label class="field"><span>CPF/CNPJ na nota</span><input data-field="document" inputmode="numeric" maxlength="30" autocomplete="off" placeholder="Somente números"></label>
    <label class="field"><span>Nome para identificação (opcional)</span><input data-field="name" maxlength="160" autocomplete="off"></label>
    <label class="field"><span>Buscar cliente cadastrado</span><input data-field="find" autocomplete="off" placeholder="Nome ou documento"></label>
    <label class="field"><span>Cliente cadastrado (fiado e crédito na loja)</span><select data-field="customer"></select></label>
    <label class="field"><span>Vendedor</span><select data-field="seller"></select></label>`;
  const field = name => modal.body.querySelector(`[data-field="${name}"]`);
  const fill = rows => {
    const chosen = field('customer').value || state.customer.id;
    const current = state.customers.find(row => row.id === chosen);
    const list = current && !rows.some(row => row.id === current.id) ? [current, ...rows] : rows;
    field('customer').innerHTML = '<option value="">Consumidor não identificado</option>' + list.map(row => `<option value="${esc(row.id)}">${esc(customerOptionText(row))}</option>`).join('');
    field('customer').value = list.some(row => row.id === chosen) ? chosen : '';
  };
  fill(state.customers);
  const operators = state.session?.operators || [];
  field('seller').innerHTML = '<option value="">Sem vendedor</option>' + operators.map(operator => `<option value="${esc(operator.id)}">${esc(operator.name)}</option>`).join('');
  field('seller').value = operators.some(operator => operator.id === state.customer.sellerId) ? state.customer.sellerId : '';
  field('document').value = state.customer.document;
  field('name').value = state.customer.name;
  let timer = null;
  field('find').addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(async () => { try { fill(await loadCustomers(field('find').value.trim())); } catch { /* keep list */ } }, 180); });
  modal.action('Receber fiado', 'spacer', async () => { modal.close(false); await receiveCreditDialog(); });
  modal.action('Limpar', '', () => { state.customer = emptyCustomer(); modal.close(true); });
  modal.action('Cancelar', '', () => modal.close(false));
  modal.action('Salvar', 'primary', async () => {
    const id = field('customer').value;
    if (id && !state.customers.some(row => row.id === id)) state.customers.push(...(await loadCustomers(field('find').value.trim())).filter(row => row.id === id));
    state.customer = {document: field('document').value.trim(), name: field('name').value.trim(), id, sellerId: field('seller').value};
    modal.close(true);
  });
  field(focusField).focus();
  const saved = await modal.promise;
  if (saved) state.saleRequest = null;
  renderSale();
  focusHome();
  return saved;
}
async function discountDialog() {
  const base = cartBase();
  let mode = state.discount?.mode || 'percent';
  const modal = openModal({title: 'Desconto na venda', eyebrow: 'Desconto · F5', text: `Aplicado sobre ${money(base)}${cartPromo() ? ' (já com as promoções)' : ''}.`});
  modal.body.innerHTML = `<div class="segmented" role="group" aria-label="Tipo de desconto"><button type="button" data-mode="percent">Percentual (%)</button><button type="button" data-mode="value">Valor (R$)</button></div>
    <label class="field"><span data-label></span><input data-field="value" class="money-input" inputmode="decimal" autocomplete="off"></label><p class="hint" data-preview></p>`;
  const input = modal.body.querySelector('[data-field="value"]'), preview = modal.body.querySelector('[data-preview]');
  const read = () => {
    if (!input.value.trim()) return null;
    const value = mode === 'percent' ? parsePercent(input.value) : parseMoney(input.value);
    return value > 0 ? {mode, value} : null;
  };
  const refresh = () => {
    modal.body.querySelectorAll('[data-mode]').forEach(button => button.setAttribute('aria-pressed', button.dataset.mode === mode));
    modal.body.querySelector('[data-label]').textContent = mode === 'percent' ? 'Percentual de desconto' : 'Valor do desconto (R$)';
    input.placeholder = mode === 'percent' ? 'Ex.: 5' : 'Ex.: 2,50';
    try {
      const cents = C.discountCents(read(), base);
      preview.textContent = cents ? `Desconto de ${money(cents)} · novo total ${money(base - cents)}${C.discountNeedsPin(role(), cents, base) ? ' · acima do limite deste operador: o PIN do gerente será pedido ao confirmar a venda.' : ''}` : 'Informe o desconto e tecle Enter.';
    } catch (error) { preview.textContent = error.message; }
  };
  if (state.discount) input.value = state.discount.mode === 'percent' ? state.discount.value.toLocaleString('pt-BR', {maximumFractionDigits: 2}) : moneyPlain(state.discount.value);
  modal.body.querySelector('.segmented').addEventListener('click', event => { const button = event.target.closest('[data-mode]'); if (!button) return; mode = button.dataset.mode; input.value = ''; refresh(); input.focus(); });
  input.addEventListener('input', refresh);
  if (state.discount) modal.action('Remover desconto', 'spacer', () => { state.discount = null; modal.close(true); });
  modal.action('Cancelar', '', () => modal.close(false));
  modal.action('Aplicar', 'primary', () => {
    const discount = read();
    if (!discount) throw Error('Informe um desconto maior que zero');
    if (discount.mode === 'percent' ? discount.value >= 100 : discount.value >= base) throw Error('O desconto deve ser menor que o total da venda');
    if (!C.discountCents(discount, base)) throw Error('O desconto informado é menor que um centavo nesta venda');
    state.discount = discount;
    modal.close(true);
  });
  refresh();
  input.focus();
  input.select();
  if (await modal.promise) cartChanged();
  focusHome();
}
async function loadSuspendedCount() {
  if (!canSell(role())) return;
  state.suspendedCount = ((await api('/api/carts/suspended')).carts || []).length;
  renderKeybar();
}
async function holdDialog() {
  const carts = (await api('/api/carts/suspended')).carts || [];
  state.suspendedCount = carts.length;
  const busy = state.cart.length > 0;
  const modal = openModal({title: 'Vendas em espera', eyebrow: 'Em espera · F6', className: 'wide', onKey: arrowNav('.pick-row'),
    text: busy ? 'Guarde a venda atual para atender o próximo cliente. Para retomar outra, guarde ou finalize esta primeiro.' : 'Escolha a venda guardada para retomar.'});
  let label = null;
  if (busy) {
    const field = el('label', 'field', '<span>Identificação da venda atual</span>');
    label = Object.assign(document.createElement('input'), {maxLength: 80, autocomplete: 'off', placeholder: 'Ex.: nome do cliente', value: customerSummary().split(' · ')[0] || `Venda das ${timeText(Date.now())}`});
    field.append(label);
    modal.body.append(field);
  }
  const list = el('div', 'pick-list', carts.length ? carts.map(cart => `<button type="button" class="pick-row" data-id="${esc(cart.id)}" ${busy ? 'disabled' : ''}><span><strong>${esc(cart.label)}</strong><small>Guardada em ${dateTimeText(cart.created_at)} · ${(cart.cart?.items || []).length} item(ns)</small></span><span class="pick-side">Retomar</span></button>`).join('') : '<p class="pick-empty">Nenhuma venda em espera.</p>');
  modal.body.append(list);
  modal.action('Fechar', '', () => modal.close(false));
  if (busy) modal.action('Colocar em espera', 'primary', async () => {
    const name = label.value.trim();
    if (!name) throw Error('Informe um nome para localizar a venda depois');
    await api('/api/carts/suspended', 'POST', {label: name, cart: {items: state.cart.map(item => ({id: item.id, name: item.name, unit: item.unit, quantity_milli: item.quantity_milli})), discount: state.discount, customer: state.customer}});
    modal.close(true);
    resetSale();
    toast(`Venda “${name}” colocada em espera`);
  });
  list.addEventListener('click', event => {
    const row = event.target.closest('.pick-row');
    if (!row) return;
    modal.run(async () => {
      const response = await api('/api/carts/suspended/' + encodeURIComponent(row.dataset.id) + '/resume', 'POST', {});
      modal.close(true);
      await loadCartSnapshot(response.cart);
      toast('Venda retomada');
    });
  });
  if (busy) { label.focus(); label.select(); } else (list.querySelector('.pick-row') || modal.actions.firstElementChild).focus();
  await modal.promise;
  loadSuspendedCount().catch(() => {});
  focusHome();
}
async function cashMoveDialog() {
  let kind = 'withdrawn';
  const modal = openModal({title: 'Sangria ou suprimento', eyebrow: 'Movimento de caixa · F7', text: 'Registre toda retirada ou entrada de dinheiro da gaveta. O motivo fica na auditoria.'});
  modal.body.innerHTML = `<div class="segmented" role="group" aria-label="Tipo de movimento"><button type="button" data-kind="withdrawn">Sangria (retirada)</button><button type="button" data-kind="supplied">Suprimento (entrada)</button></div>
    <label class="field"><span>Valor (R$)</span><input data-field="amount" class="money-input" inputmode="decimal" autocomplete="off" placeholder="Ex.: 200,00"></label>
    <label class="field"><span>Motivo</span><input data-field="reason" maxlength="240" autocomplete="off" placeholder="Ex.: recolhimento para o cofre"></label>`;
  const amount = modal.body.querySelector('[data-field="amount"]'), reason = modal.body.querySelector('[data-field="reason"]');
  const refresh = () => modal.body.querySelectorAll('[data-kind]').forEach(button => button.setAttribute('aria-pressed', button.dataset.kind === kind));
  modal.body.querySelector('.segmented').addEventListener('click', event => { const button = event.target.closest('[data-kind]'); if (!button) return; kind = button.dataset.kind; refresh(); amount.focus(); });
  modal.action('Cancelar', '', () => modal.close(false));
  modal.action('Registrar', 'primary', async () => {
    const cents = parseMoney(amount.value);
    if (cents <= 0) { amount.focus(); throw Error('Informe um valor maior que zero'); }
    if (reason.value.trim().length < 3) { reason.focus(); throw Error('Descreva o motivo do movimento'); }
    await api('/api/cash/movement', 'POST', {kind, amount_cents: cents, reason: reason.value.trim()});
    modal.close(true);
    toast(kind === 'withdrawn' ? `Sangria de ${money(cents)} registrada` : `Suprimento de ${money(cents)} registrado`);
  });
  refresh();
  amount.focus();
  if (await modal.promise) poll().catch(() => {});
  focusHome();
}
async function openCash() {
  const raw = await textDialog('Abrir caixa', 'Informe quanto existe em dinheiro na gaveta antes da primeira venda. Use 0,00 se o caixa começar sem espécie.', {eyebrow: 'Início do turno · F12', fieldLabel: 'Dinheiro inicial (R$)', placeholder: 'Ex.: 150,00', inputMode: 'decimal', acceptLabel: 'Abrir caixa',
    validate: async value => { await api('/api/cash/open', 'POST', {opening_cents: parseMoney(value)}); }});
  if (raw === null) return;
  toast(`Caixa aberto com ${money(parseMoney(raw))}`);
  await poll();
  renderSale();
  focusHome();
}
function cashSummaryMarkup(summary, result) {
  const c = summary.categories || {};
  const rows = [['Saldo inicial', money(summary.opening_cents)], ['Vendas do turno', `${summary.sales_count || 0} · ${money(summary.sales_total_cents || 0)}`], ['Pix', money(c.pix_cents || 0)], ['Débito', money(c.card_debit_cents || 0)], ['Crédito', money(c.card_credit_cents || 0)],
    ['Suprimentos', money(c.supplied_cents || 0)], ['Sangrias', '− ' + money(c.withdrawn_cents || 0)], ['Devoluções em dinheiro', '− ' + money(c.cash_refunds_cents || 0)],
    ['Fiado recebido em dinheiro', money(c.credit_received_cash_cents || 0)], ['Fiado recebido em outras formas', money(c.credit_received_other_cents || 0)]];
  if (result) rows.splice(2, 0, ['Dinheiro das vendas', money(c.cash_sales_net_cents || 0)]);
  let head = '';
  if (result) {
    const difference = result.difference_cents;
    head = `<div class="summary-item emphasis ${difference === 0 ? '' : difference > 0 ? 'positive' : 'negative'}"><span>${difference === 0 ? 'Fechamento exato' : difference > 0 ? 'Sobra no caixa' : 'Falta no caixa'}</span><strong>${difference === 0 ? 'Conferido' : (difference > 0 ? '+ ' : '− ') + money(Math.abs(difference))}</strong></div>
      <div class="summary-item"><span>Dinheiro contado</span><strong>${money(result.counted_cents)}</strong></div><div class="summary-item"><span>Dinheiro esperado</span><strong>${money(result.expected_cents)}</strong></div>`;
  }
  return `<div class="summary-grid">${head}${rows.map(([label, value]) => `<div class="summary-item"><span>${label}</span><strong>${value}</strong></div>`).join('')}</div>`;
}
// Blind count: the expected cash only appears after the counted amount is recorded.
async function closeCash() {
  if (state.cart.length) throw Error('Finalize, cancele ou coloque a venda atual em espera (F6) antes de fechar o caixa');
  const summary = await api('/api/cash/summary');
  const modal = openModal({title: 'Fechar caixa', eyebrow: 'Conferência do turno · F12', className: 'wide', text: 'Conte o dinheiro da gaveta e informe o valor. O esperado e a diferença aparecem depois do fechamento.'});
  const report = el('div', '', cashSummaryMarkup(summary, null));
  const fields = el('div', 'form-stack', `<label class="field"><span>Dinheiro contado na gaveta (R$)</span><input data-field="counted" class="money-input" inputmode="decimal" autocomplete="off" placeholder="Informe o valor contado"></label>
    <label class="field"><span>PIN do gerente ou superior</span><input data-field="pin" type="password" inputmode="numeric" autocomplete="off" maxlength="12" placeholder="Digite o PIN"></label>`);
  modal.body.append(fields, report);
  const counted = fields.querySelector('[data-field="counted"]'), pin = fields.querySelector('[data-field="pin"]');
  modal.action('Cancelar', '', () => modal.close(null));
  const accept = modal.action('Fechar caixa', 'primary', async () => {
    if (!counted.value.trim()) { counted.focus(); throw Error('Informe o dinheiro contado na gaveta'); }
    const cents = parseMoney(counted.value);
    if (!pin.value.trim()) { pin.focus(); throw Error('Informe o PIN do gerente ou superior'); }
    let result;
    try { result = await api('/api/cash/close', 'POST', {counted_cents: cents, manager_pin: pin.value.trim()}); } catch (error) { pin.value = ''; pin.focus(); throw error; }
    modal.heading.textContent = 'Caixa fechado';
    modal.lead.textContent = result.difference_cents === 0 ? 'O valor contado confere com o esperado.' : result.difference_cents > 0 ? 'Há sobra no valor contado.' : 'Há falta no valor contado.';
    fields.remove();
    report.innerHTML = cashSummaryMarkup(summary, result);
    modal.actions.innerHTML = '';
    modal.action('Concluir', 'primary', () => modal.close(result)).focus();
  });
  counted.focus();
  const result = await modal.promise;
  if (result === null && accept.isConnected) return;
  await poll();
  renderSale();
  focusHome();
}
async function receiveCreditDialog() {
  await loadCustomers();
  const debtors = state.customers.filter(customer => customer.open_credit_cents > 0);
  if (!debtors.length) { toast('Nenhum cliente com fiado em aberto neste caixa', true); return; }
  const modal = openModal({title: 'Receber pagamento de fiado', eyebrow: 'Fiado', text: 'O valor quita primeiro as compras mais antigas. Em dinheiro, entra no caixa e na conferência.'});
  modal.body.innerHTML = `<label class="field"><span>Cliente</span><select data-field="customer">${debtors.map(customer => `<option value="${esc(customer.id)}" ${customer.id === state.customer.id ? 'selected' : ''}>${esc(customer.name)} · em aberto ${money(customer.open_credit_cents)}</option>`).join('')}</select></label>
    <label class="field"><span>Valor recebido (R$)</span><input data-field="amount" class="money-input" inputmode="decimal" autocomplete="off"></label>
    <label class="field"><span>Forma</span><select data-field="method"><option value="cash">Dinheiro</option><option value="pix">Pix</option><option value="card_debit">Débito</option><option value="card_credit">Crédito</option></select></label>`;
  const field = name => modal.body.querySelector(`[data-field="${name}"]`);
  const customer = () => debtors.find(row => row.id === field('customer').value);
  const fill = () => { field('amount').value = moneyPlain(customer().open_credit_cents); };
  field('customer').addEventListener('change', fill);
  fill();
  const receiptId = uuid();
  modal.action('Cancelar', '', () => modal.close(null));
  modal.action('Confirmar recebimento', 'primary', async () => {
    const amount = parseMoney(field('amount').value);
    if (!amount) { field('amount').focus(); throw Error('Informe o valor recebido'); }
    modal.close(await api('/api/customers/receive', 'POST', {receipt_id: receiptId, customer_id: customer().id, amount_cents: amount, method: field('method').value}));
  });
  field('amount').focus();
  field('amount').select();
  const result = await modal.promise;
  if (!result) return;
  toast(`Fiado recebido: ${money(result.amount_cents)}${result.open_credit_cents != null ? ' · em aberto ' + money(result.open_credit_cents) : ''}`);
  await Promise.all([loadCustomers(), poll()]).catch(() => {});
  if (result.receipt) await receiptDialog({receipt: result.receipt}, result.print_job_id ? 'Recibo enviado à impressora.' : 'Recibo pronto para visualização.', {title: 'Recibo de fiado', eyebrow: 'Recebimento registrado'});
}
async function returnDialog(saleId) {
  const info = await api('/api/sales/' + encodeURIComponent(saleId) + '/returnable');
  const lines = info.items.filter(item => item.available_milli > 0);
  if (!lines.length) { toast('Todos os itens desta venda já foram devolvidos', true); return; }
  await loadCustomers().catch(() => {});
  const refunds = [['cash', 'Dinheiro (sai do caixa)'], ['store_credit', 'Crédito na loja (troca)']];
  if (info.on_account) refunds.push(['account', 'Abater do fiado']);
  const modal = openModal({title: 'Quais itens voltaram?', eyebrow: `Devolução da venda ${saleId.slice(0, 8)}`, text: 'Informe a quantidade devolvida de cada item. O estoque volta automaticamente.', className: 'wide'});
  modal.body.innerHTML = `<div class="return-lines">${lines.map((item, index) => `<label class="return-line"><span><strong>${esc(item.name)}</strong><small>Disponível para devolução: ${qtyText(item.available_milli, item.unit)} ${esc(item.unit)}</small></span><input data-return-index="${index}" inputmode="decimal" value="0" autocomplete="off" aria-label="Quantidade devolvida de ${esc(item.name)}"></label>`).join('')}</div>
    <label class="field"><span>Como devolver o valor</span><select data-field="refund">${refunds.map(([value, label]) => `<option value="${value}">${label}</option>`).join('')}</select></label>
    <label class="field hidden" data-wrap="customer"><span>Cliente que recebe o crédito</span><select data-field="customer"><option value="">Escolha o cliente</option>${state.customers.map(customer => `<option value="${esc(customer.id)}" ${customer.id === info.customer_id ? 'selected' : ''}>${esc(customer.name)}</option>`).join('')}</select></label>
    <label class="field"><span>Motivo</span><input data-field="reason" maxlength="240" autocomplete="off" placeholder="Ex.: produto com defeito, troca de tamanho"></label>
    ${role() === 'cashier' ? '<label class="field"><span>PIN do gerente</span><input data-field="pin" type="password" inputmode="numeric" autocomplete="off" maxlength="12"></label>' : ''}
    <p class="hint" data-total>Total a devolver: R$ 0,00</p>`;
  const field = name => modal.body.querySelector(`[data-field="${name}"]`);
  const quantities = () => lines.map((item, index) => {
    const milli = Math.round(Number(modal.body.querySelector(`[data-return-index="${index}"]`).value.replace(',', '.')) * 1000);
    return {item, milli: Number.isFinite(milli) ? milli : NaN};
  });
  const refresh = () => {
    let total = 0;
    for (const {item, milli} of quantities()) if (milli > 0) total += Math.round(item.line_total_cents * Math.min(milli, item.available_milli) / item.quantity_milli);
    modal.body.querySelector('[data-total]').textContent = 'Total a devolver: ' + money(total);
    modal.body.querySelector('[data-wrap="customer"]').classList.toggle('hidden', field('refund').value !== 'store_credit' || !!info.customer_id);
  };
  modal.body.addEventListener('input', refresh);
  modal.body.addEventListener('change', refresh);
  refresh();
  const returnId = uuid();
  modal.action('Cancelar', '', () => modal.close(null));
  modal.action('Registrar devolução', 'primary', async () => {
    const items = [];
    for (const {item, milli} of quantities()) {
      if (Number.isNaN(milli) || milli < 0 || milli > item.available_milli) throw Error(`Quantidade inválida para ${item.name}`);
      if (item.unit === 'UN' && milli % 1000) throw Error(`${item.name} aceita somente quantidade inteira`);
      if (milli) items.push({product_id: item.product_id, quantity_milli: milli});
    }
    if (!items.length) throw Error('Informe ao menos um item devolvido');
    const reason = field('reason').value.trim();
    if (reason.length < 3) { field('reason').focus(); throw Error('Informe o motivo da devolução'); }
    const refund = field('refund').value;
    const request = {return_id: returnId, refund_method: refund, reason, items, manager_pin: field('pin')?.value.trim() || undefined};
    if (refund === 'store_credit' && !info.customer_id) {
      request.customer_id = field('customer').value;
      if (!request.customer_id) throw Error('Escolha o cliente que recebe o crédito');
    }
    const result = await api('/api/sales/' + encodeURIComponent(saleId) + '/return', 'POST', request);
    modal.close({...result, refund});
  });
  modal.body.querySelector('[data-return-index]').focus();
  modal.body.querySelector('[data-return-index]').select();
  const result = await modal.promise;
  if (!result) return;
  toast(`Devolução registrada: ${money(result.total_cents)} · ${result.refund === 'cash' ? 'entregue em dinheiro' : result.refund === 'store_credit' ? 'em crédito na loja' : 'abatido do fiado'}`);
  await Promise.all([state.tab === 'sales' ? loadSales() : null, loadCustomers(), poll()]).catch(() => {});
  if (result.receipt) await receiptDialog({receipt: result.receipt}, result.print_job_id ? 'Comprovante de devolução enviado à impressora.' : 'Comprovante de devolução pronto para visualização.', {title: 'Comprovante de devolução', eyebrow: 'Devolução registrada'});
}
async function returnFlow() {
  const id = await salePicker({title: 'Devolução ou troca', text: 'Escolha a venda de origem. A mais recente vem primeiro.', eyebrow: 'Devolução', onlyCompleted: true});
  if (id) await returnDialog(id);
}

/* ---------- scale ---------- */
function captureWeight(milli) {
  const item = C.scaleTarget(state.cart, state.scale.awaitingId);
  if (!item) return;
  const total = item.quantity_milli + milli;
  const stockError = stockLimitError(item, total);
  if (stockError) { scanError(stockError); return; }
  item.quantity_milli = total;
  // Locked: later readings (same bag left on the plate, another object) cannot change this line.
  state.scale.awaitingId = null;
  state.scale.gate.reset();
  touch(item.id);
  cartChanged();
  beep('ok');
  scanStatus('ok', `${item.name} · peso capturado ${qtyText(milli, 'KG')} kg`);
}
function onScaleReading(milli) {
  if (modalStack.length || !C.scaleTarget(state.cart, state.scale.awaitingId)) return;
  clearTimeout(state.scale.settleTimer);
  const stable = state.scale.gate.push(milli);
  if (stable !== null) { captureWeight(stable); return; }
  scanStatus('', `Balança: ${qtyText(milli, 'KG')} kg · estabilizando…`);
  state.scale.settleTimer = setTimeout(() => { const settled = state.scale.gate.settle(); if (settled) captureWeight(settled); }, 700);
}
async function readScale(port, reader) {
  const decoder = new TextDecoder();
  try {
    while (state.scale.port === port && port.readable) {
      const result = await reader.read();
      if (result.done) break;
      if (!result.value) continue;
      state.scale.buffer += decoder.decode(result.value, {stream: true});
      const lines = state.scale.buffer.split(/[\r\n]+/);
      state.scale.buffer = lines.pop() || '';
      for (const line of lines) { const milli = C.scaleReading(line); if (milli) onScaleReading(milli); }
    }
  } catch (error) {
    if (state.scale.port === port) toast('A conexão com a balança foi encerrada', true);
  } finally {
    try { reader.releaseLock(); } catch { /* already released */ }
    if (state.scale.port === port) { state.scale.port = null; state.scale.reader = null; state.scale.buffer = ''; renderScaleChip(); }
  }
}
async function disconnectScale() {
  const {port, reader} = state.scale;
  state.scale.port = null;
  state.scale.reader = null;
  if (reader) { try { await reader.cancel(); } catch { /* ignore */ } try { reader.releaseLock(); } catch { /* ignore */ } }
  if (port) { try { await port.close(); } catch { /* ignore */ } }
  state.scale.buffer = '';
  renderScaleChip();
}
async function connectScale() {
  if (!navigator.serial) throw Error('Este navegador não oferece Web Serial. Use Chrome ou Edge no computador do caixa.');
  const port = await navigator.serial.requestPort();
  await port.open({baudRate: Number(state.scale.baudRate) || 9600, dataBits: 8, stopBits: 1, parity: 'none'});
  state.scale.port = port;
  state.scale.buffer = '';
  state.scale.gate.reset();
  const reader = port.readable.getReader();
  state.scale.reader = reader;
  readScale(port, reader);
  renderScaleChip();
}
async function scaleDialog() {
  const modal = openModal({title: 'Balança e etiquetas', eyebrow: 'Equipamentos', text: 'Com a balança conectada, escolha o produto por quilo e coloque-o no prato: o peso entra somente nesse item e fica travado.'});
  modal.body.innerHTML = `<p class="hint" data-status></p>
    <label class="field"><span>Etiqueta de balança (EAN iniciado em 2)</span><select data-field="mode"><option value="weight">Traz o peso em gramas</option><option value="price">Traz o preço total</option></select></label>
    <label class="field"><span>Velocidade da porta serial</span><select data-field="baud"><option value="9600">9600 bps</option><option value="4800">4800 bps</option><option value="19200">19200 bps</option></select></label>`;
  const mode = modal.body.querySelector('[data-field="mode"]'), baud = modal.body.querySelector('[data-field="baud"]'), status = modal.body.querySelector('[data-status]');
  mode.value = state.scale.barcodeMode;
  baud.value = state.scale.baudRate;
  mode.addEventListener('change', () => { state.scale.barcodeMode = mode.value; prefs.set('scale.barcodeMode', mode.value); });
  baud.addEventListener('change', () => { state.scale.baudRate = baud.value; prefs.set('scale.baudRate', baud.value); if (state.scale.port) status.textContent = 'Desconecte e conecte a balança para aplicar a nova velocidade.'; });
  modal.action('Fechar', '', () => modal.close());
  const toggle = modal.action('', 'primary', async () => { if (state.scale.port) await disconnectScale(); else await connectScale(); refresh(); });
  const refresh = () => {
    status.textContent = state.scale.port ? `Balança serial conectada a ${state.scale.baudRate} bps.` : 'Balança manual: o peso é digitado ou lido da etiqueta.';
    toggle.textContent = state.scale.port ? 'Desconectar balança' : 'Conectar balança';
  };
  refresh();
  toggle.focus();
  await modal.promise;
  focusHome();
}

/* ---------- actions, key ruler, help, palette ---------- */
function setTheme(theme) {
  document.documentElement.dataset.theme = theme;
  prefs.set('theme', theme);
}
async function toggleFocus() {
  const enabled = !document.body.classList.contains('focus-mode');
  document.body.classList.toggle('focus-mode', enabled);
  prefs.set('focus', enabled ? 'on' : 'off');
  try {
    if (enabled && !document.fullscreenElement) await document.documentElement.requestFullscreen();
    else if (!enabled && document.fullscreenElement) await document.exitFullscreen();
  } catch { /* fullscreen may be denied; the focus layout still applies */ }
  if (enabled && canSell(role())) setTab('checkout');
  renderKeybar();
  focusHome();
}
async function lockTerminal() {
  if (state.cart.length) throw Error('Finalize, cancele ou coloque a venda em espera (F6) antes de trocar de operador');
  await api('/api/logout', 'POST', {});
  clearLocalSale();
  state.done = null;
  state.cartRestored = false;
  try { sessionStorage.removeItem('pdv2.cart'); } catch { /* storage unavailable */ }
  toast('Caixa bloqueado. O próximo operador entra com o próprio PIN.');
  await loadSession();
}
async function logout() {
  if (state.session?.cash) throw Error('Feche o caixa (F12) antes de sair, ou use “Trocar operador”.');
  await lockTerminal();
}
function goBack() {
  const input = $('productSearch');
  if (state.tab !== 'checkout') { if (canSell(role())) setTab('checkout'); return; }
  if (state.pay.active) {
    if (state.pay.method) { cancelPayment(); state.pay.active = true; renderSale(); $('paymentMethods').focus(); return; }
    cancelPayment();
  } else if (state.scale.awaitingId) {
    const waiting = state.cart.find(item => item.id === state.scale.awaitingId);
    state.scale.awaitingId = null;
    if (waiting && waiting.quantity_milli <= 0) state.cart.splice(state.cart.indexOf(waiting), 1);
    state.lastItemId = state.selectedId = state.cart.at(-1)?.id || null;
  } else if (input.value) input.value = '';
  else if (state.pendingQty) { state.pendingQty = null; renderPendingQty(); }
  else if (state.done) state.done = null;
  scanStatus('', 'Leitor pronto');
  renderSale();
  focusScan();
}
const needsCart = () => (state.cart.length ? '' : 'Nenhum item na venda');
// label/words feed the palette; sale = belongs to the sale screen; why() returns the reason it is unavailable.
const ACTIONS = {
  help: {label: 'Ajuda e atalhos', words: 'teclas atalhos', run: openHelp},
  search: {label: 'Buscar produto no catálogo', words: 'catalogo produto nome grade categoria', sale: true, run: () => openSearch('')},
  quantity: {label: 'Alterar quantidade ou peso do item', words: 'qtd peso multiplicar', sale: true, why: needsCart, run: () => changeQuantity()},
  customer: {label: 'Cliente e CPF na nota', words: 'cpf cnpj cliente vendedor documento', sale: true, run: () => customerDialog('document')},
  discount: {label: 'Desconto na venda', words: 'abatimento percentual', sale: true, why: needsCart, pin: () => role() === 'cashier', run: discountDialog},
  hold: {label: 'Vendas em espera: guardar ou retomar', words: 'suspender espera retomar pausar', sale: true, run: holdDialog},
  cashMove: {label: 'Sangria ou suprimento', words: 'retirada entrada dinheiro cofre troco', sell: true, why: () => (state.session?.cash ? '' : 'Abra o caixa primeiro (F12)'), run: cashMoveDialog},
  removeItem: {label: 'Cancelar item selecionado', words: 'remover excluir tirar', sale: true, why: needsCart, pin: () => role() === 'cashier', run: () => removeItem()},
  reprint: {label: 'Reimprimir comprovante', words: 'recibo cupom segunda via imprimir', sell: true, run: reprint},
  pay: {label: 'Pagar a venda', words: 'receber finalizar fechar venda', sale: true, why: payBlockReason, run: startPayment},
  focus: {label: 'Modo foco (tela cheia)', words: 'fullscreen tela cheia abas', run: toggleFocus},
  closeCash: {label: 'Abrir ou fechar caixa', words: 'abertura fechamento conferencia turno', sell: true, pin: () => !!state.session?.cash, run: () => (state.session?.cash ? closeCash() : openCash())},
  back: {label: 'Voltar', words: 'esc sair cancelar', run: goBack},
  palette: {label: 'Paleta de comandos', hidden: true, run: openPalette},
  receiveCredit: {label: 'Receber fiado de cliente', words: 'fiado a prazo pagamento divida', sell: true, why: () => (state.session?.cash ? '' : 'Abra o caixa primeiro (F12)'), run: receiveCreditDialog},
  returnSale: {label: 'Devolução ou troca', words: 'devolver trocar estorno', sell: true, why: () => (state.session?.cash ? '' : 'Abra o caixa primeiro (F12)'), run: returnFlow},
  discardSale: {label: 'Cancelar a venda atual', words: 'descartar limpar nova venda', sale: true, why: needsCart, pin: () => role() === 'cashier', run: discardSale},
  scale: {label: 'Balança e etiquetas', words: 'peso serial conectar', sell: true, run: scaleDialog},
  recheckLicense: {label: 'Verificar licença e relógio de novo', words: 'licenca renovar hora data relogio', run: recheckLicense},
  refreshCatalog: {label: 'Atualizar catálogo e operadores', words: 'sincronizar produtos precos', run: async () => { await refreshOperators(); await poll(); await refreshCartProducts(); }},
  lock: {label: 'Trocar operador (bloquear caixa)', words: 'bloquear sair trocar', run: lockTerminal},
  logout: {label: 'Sair', words: 'logout encerrar', why: () => (state.session?.cash ? 'Feche o caixa antes de sair' : ''), run: logout},
  themeLight: {label: 'Tema claro', words: 'aparencia cor', run: () => setTheme('light')},
  themeDark: {label: 'Tema escuro', words: 'aparencia cor noite', run: () => setTheme('dark')},
  themeContrast: {label: 'Tema de alto contraste', words: 'aparencia acessibilidade', run: () => setTheme('contrast')},
  tabCheckout: {label: 'Ir para o Caixa', words: 'aba venda', sell: true, run: () => setTab('checkout')},
  tabSales: {label: 'Ir para Vendas', words: 'aba historico', run: () => setTab('sales')},
  tabStock: {label: 'Ir para Estoque', words: 'aba ajuste', why: () => (canAdjustStock(role()) ? '' : 'Seu perfil não acessa o estoque'), run: () => setTab('stock')},
  tabPrinters: {label: 'Ir para Impressoras', words: 'aba termica fila', why: () => (canManage(role()) ? '' : 'Disponível para gerentes'), run: () => setTab('printers')},
  tabSystem: {label: 'Ir para Sistema', words: 'aba diagnostico backup sincronizacao sobre', why: () => (canManage(role()) ? '' : 'Disponível para gerentes'), run: () => setTab('system')},
};
function actionBlocked(name) {
  const action = ACTIONS[name];
  if ((action.sale || action.sell) && !canSell(role())) return 'Seu perfil não realiza vendas';
  return action.why ? action.why() : '';
}
async function runAction(name) {
  const action = ACTIONS[name];
  if (!action) return;
  const reason = actionBlocked(name);
  if (reason) { toast(reason, true); beep('error'); return; }
  if (action.sale && state.tab !== 'checkout') setTab('checkout');
  try { await action.run(); } catch (error) { toast(error.message, true); beep('error'); }
}
const PAY_CONTEXT_ACTIONS = ['help', 'customer', 'discount', 'focus', 'back', 'palette'];
let keybarMarkup = '';
function renderKeybar() {
  if (!state.session?.operator) return;
  const keyButton = ({cap, label, short, action, pay, title = '', dim = false, accent = false, on = false, lock = false, count = 0}) =>
    `<button type="button" class="key${dim ? ' dim' : ''}${accent ? ' accent' : ''}${on ? ' on' : ''}" ${action ? `data-action="${action}"` : ''} ${pay != null ? `data-paykey="${pay}"` : ''} ${short ? 'data-short' : ''} title="${esc(title)}" aria-disabled="${dim}">
      ${count ? `<span class="key-count">${count}</span>` : ''}${lock ? ico('lock', 'key-lock') : ''}<kbd>${esc(cap)}</kbd><span class="kl"><span class="kl-full">${label}</span>${short ? `<span class="kl-short">${esc(short)}</span>` : ''}</span></button>`;
  let keys;
  if (state.pay.active && state.tab === 'checkout') {
    const {method, other} = state.pay;
    keys = state.payOptions.filter(option => option.key).map(option => keyButton({cap: option.key, label: esc(option.short || option.label), pay: option.key, title: option.label,
      on: option.method === method && (method !== 'other' || option.id === other?.id)}));
    keys.push(keyButton({cap: '0', label: 'Dividir', pay: '0', title: 'Dividir o pagamento em mais de uma forma', on: method === 'split'}));
    keys.push(keyButton({cap: 'F4', label: 'Cliente/<wbr>CPF', action: 'customer'}), keyButton({cap: 'F5', label: 'Desconto', action: 'discount', lock: role() === 'cashier'}));
    keys.push(keyButton({cap: 'Enter', label: 'Confirmar', action: 'confirm', accent: true, dim: !method, title: method ? 'Confirmar a venda' : 'Escolha a forma de pagamento'}));
    keys.push(keyButton({cap: 'Esc', label: 'Voltar', action: 'back'}));
  } else {
    keys = C.SHORTCUTS.map(shortcut => {
      const action = ACTIONS[shortcut.action], reason = actionBlocked(shortcut.action);
      let label = shortcut.label.replace('/', '/<wbr>');
      if (shortcut.action === 'closeCash' && !state.session.cash) label = 'Abrir caixa';
      return keyButton({cap: shortcut.cap || shortcut.key, label, short: shortcut.short, action: shortcut.action, title: reason || shortcut.hint || action.label, dim: !!reason,
        accent: shortcut.action === 'pay' && !reason, on: shortcut.action === 'focus' && document.body.classList.contains('focus-mode'),
        lock: !reason && !!action.pin?.(), count: shortcut.action === 'hold' ? state.suspendedCount : 0});
    });
  }
  const markup = keys.join('');
  if (markup !== keybarMarkup) { keybarMarkup = markup; $('keyBar').innerHTML = markup; }
}
function openHelp() {
  const modal = openModal({title: 'Atalhos do caixa', eyebrow: 'Ajuda · F1', text: 'Tudo o que está na régua de teclas também pode ser tocado. Ctrl+K abre a busca de comandos por nome.', className: 'wide', onKey: arrowNav('.help-item')});
  const item = (cap, label, action = '') => `<button type="button" class="help-item" ${action ? `data-action="${action}"` : 'tabindex="-1"'}><kbd>${esc(cap)}</kbd><span>${esc(label)}</span></button>`;
  const sections = [
    ['Venda', C.SHORTCUTS.map(shortcut => item(shortcut.cap || shortcut.key, shortcut.action === 'closeCash' ? 'Abrir ou fechar caixa' : shortcut.hint || shortcut.label, shortcut.action)).join('')],
    ['Leitura e lista', [item('Enter', 'Lança o código digitado ou lido'), item('3*cód', 'Lança 3 unidades do código'), item('↑ ↓', 'Seleciona o item da lista'), item('Delete', 'Cancela o item selecionado'), item('Ctrl+K', 'Busca de comandos por nome', 'palette')].join('')],
    ['Pagamento (depois do F10)', [...C.PAY_KEYS.map(option => item(option.key, option.label)), item('7 a 9', 'Formas próprias da empresa'), item('0', 'Dividir o pagamento'), item('Enter', 'Confirma a venda'), item('Esc', 'Volta um passo')].join('')],
    ['Outras ações', ['receiveCredit', 'returnSale', 'discardSale', 'scale', 'refreshCatalog', 'lock'].map(name => item('•', ACTIONS[name].label, name)).join('')],
  ];
  modal.body.innerHTML = sections.map(([title, items]) => `<div class="menu-label">${title}</div><div class="help-grid">${items}</div>`).join('');
  modal.action('Fechar <kbd>Esc</kbd>', 'primary', () => modal.close()).focus();
  modal.body.addEventListener('click', event => { const button = event.target.closest('[data-action]'); if (!button) return; modal.close(); runAction(button.dataset.action); });
  return modal.promise;
}
function openPalette() {
  const modal = openModal({title: 'O que você quer fazer?', eyebrow: 'Comandos · Ctrl+K', className: 'wide', onKey: (event, self) => {
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') { event.preventDefault(); setActive(active + (event.key === 'ArrowDown' ? 1 : -1)); return true; }
    if (event.key === 'Enter') { event.preventDefault(); const row = list.children[active]; if (row?.dataset.action) { self.close(); runAction(row.dataset.action); } return true; }
    return false;
  }});
  const field = el('label', 'search-field', `${ico('search')}<input placeholder="Digite: sangria, cpf, reimprimir, tema…" autocomplete="off" spellcheck="false" aria-label="Buscar comando">`);
  const input = field.querySelector('input'), list = el('div', 'menu-list');
  modal.body.append(field, list);
  let active = 0;
  const setActive = index => {
    const rows = [...list.children];
    if (!rows.length) return;
    active = (index + rows.length) % rows.length;
    rows.forEach((row, position) => row.classList.toggle('active', position === active));
    rows[active].scrollIntoView({block: 'nearest'});
  };
  const render = () => {
    const term = fold(input.value.trim());
    const rows = Object.entries(ACTIONS).filter(([, action]) => !action.hidden).map(([name, action]) => ({name, action, key: C.SHORTCUTS.find(shortcut => shortcut.action === name), reason: actionBlocked(name)}))
      .filter(row => !term || fold(row.action.label + ' ' + (row.action.words || '')).includes(term));
    list.innerHTML = rows.length ? rows.map(row => `<button type="button" class="menu-item" data-action="${row.name}" tabindex="-1"><span>${esc(row.action.label)}${row.reason ? ` <small>· ${esc(row.reason)}</small>` : ''}</span>${row.key ? `<kbd>${esc(row.key.cap || row.key.key)}</kbd>` : ''}</button>`).join('') : '<p class="pick-empty">Nenhum comando com esse nome.</p>';
    setActive(0);
  };
  input.addEventListener('input', render);
  list.addEventListener('click', event => { const row = event.target.closest('[data-action]'); if (!row) return; modal.close(); runAction(row.dataset.action); });
  render();
  input.focus();
  return modal.promise;
}
function openMenu() {
  const operator = state.session.operator, cashOpen = !!state.session.cash;
  const modal = openModal({title: operator.name, eyebrow: roleNames[operator.role] || operator.role, className: 'menu', backdropClass: 'menu-backdrop', onKey: arrowNav('.menu-item')});
  const item = (name, label = ACTIONS[name].label) => {
    const shortcut = C.SHORTCUTS.find(row => row.action === name), reason = actionBlocked(name);
    return `<button type="button" class="menu-item" data-action="${name}" ${reason ? 'disabled' : ''} title="${esc(reason)}"><span>${esc(label)}</span>${shortcut ? `<kbd>${esc(shortcut.cap || shortcut.key)}</kbd>` : ''}</button>`;
  };
  const sells = canSell(operator.role), theme = document.documentElement.dataset.theme || 'light';
  const toggle = (key, label, value) => `<button type="button" class="menu-item" data-pref="${key}"><span>${label}</span><small>${value}</small></button>`;
  modal.body.innerHTML = `<div class="menu-list">
    ${sells ? `<div class="menu-label">Caixa</div>${item('closeCash', cashOpen ? 'Fechar caixa' : 'Abrir caixa')}${item('cashMove')}${item('hold', 'Vendas em espera')}${item('receiveCredit')}${item('returnSale')}${state.cart.length ? item('discardSale') : ''}<div class="menu-sep"></div>` : ''}
    <div class="menu-label">Aparência</div>
    <div class="segmented" role="group" aria-label="Tema"><button type="button" data-theme-choice="light" aria-pressed="${theme === 'light'}">Claro</button><button type="button" data-theme-choice="dark" aria-pressed="${theme === 'dark'}">Escuro</button><button type="button" data-theme-choice="contrast" aria-pressed="${theme === 'contrast'}">Alto contraste</button></div>
    ${sells ? item('focus', document.body.classList.contains('focus-mode') ? 'Sair do modo foco' : 'Modo foco (tela cheia)') : ''}
    ${toggle('sound', 'Som de leitura', prefs.get('sound', 'on') === 'on' ? 'Ligado' : 'Desligado')}
    ${sells ? toggle('receiptPreview', 'Abrir comprovante após a venda', prefs.get('receiptPreview', 'off') === 'on' ? 'Sim' : 'Não') : ''}
    <div class="menu-sep"></div><div class="menu-label">Equipamentos e ajuda</div>
    ${sells ? item('scale') : ''}${item('refreshCatalog')}${item('help')}
    <div class="menu-sep"></div><div class="menu-label">Sessão</div>
    ${item('lock')}${cashOpen ? '' : item('logout')}</div>`;
  modal.body.addEventListener('click', event => {
    const themeButton = event.target.closest('[data-theme-choice]'), pref = event.target.closest('[data-pref]'), action = event.target.closest('[data-action]');
    if (themeButton) {
      setTheme(themeButton.dataset.themeChoice);
      modal.body.querySelectorAll('[data-theme-choice]').forEach(button => button.setAttribute('aria-pressed', button === themeButton));
    } else if (pref) {
      const key = pref.dataset.pref, fallback = key === 'sound' ? 'on' : 'off';
      const next = prefs.get(key, fallback) === 'on' ? 'off' : 'on';
      prefs.set(key, next);
      pref.querySelector('small').textContent = key === 'sound' ? (next === 'on' ? 'Ligado' : 'Desligado') : (next === 'on' ? 'Sim' : 'Não');
      if (key === 'sound') beep('ok');
    } else if (action) { modal.close(); runAction(action.dataset.action); }
  });
  modal.action('Fechar', '', () => modal.close());
  modal.body.querySelector('.menu-item:not([disabled])')?.focus();
  return modal.promise;
}

/* ---------- pages: Vendas, Estoque, Impressoras, Sistema ---------- */
const badge = ([label, kind] = ['—', '']) => `<span class="badge ${kind || ''}">${esc(label)}</span>`;
async function loadSales() {
  const data = await api('/api/sales'), sells = canSell(role());
  $('salesList').innerHTML = data.sales.length ? data.sales.map(sale => `<div class="list-row"><span><strong>${money(sale.total_cents)} ${badge(saleStatusNames[sale.status] || [sale.status, ''])}</strong><small>${dateTimeText(sale.created_at)} · ${esc(sale.customer_name || 'Consumidor não identificado')} · venda ${esc(sale.id.slice(0, 8))}${sale.void_reason ? ' · ' + esc(sale.void_reason) : ''}</small></span>
    <span class="list-actions"><button class="btn" data-detail="${esc(sale.id)}">Comprovante</button>${sells ? `<button class="btn" data-reprint="${esc(sale.id)}">Reimprimir</button>` : ''}${sells && sale.status === 'completed' ? `<button class="btn" data-return="${esc(sale.id)}">Devolver</button><button class="btn danger" data-void="${esc(sale.id)}">Cancelar</button>` : ''}</span></div>`).join('') : '<p class="list-empty">Nenhuma venda neste terminal.</p>';
}
async function voidSale(id) {
  const reason = await textDialog('Motivo do cancelamento', 'Descreva o motivo para registrar na auditoria da venda.', {eyebrow: 'Cancelamento de venda', fieldLabel: 'Motivo', placeholder: 'Ex.: produto lançado incorretamente', acceptLabel: 'Continuar', multiline: true, minLength: 3, maxLength: 240});
  if (!reason) return;
  let pin = '';
  if (role() === 'cashier') {
    pin = await pinDialog('Autorizar cancelamento', 'Cancelar uma venda exige o PIN de um gerente.');
    if (!pin) return;
  }
  if (!(await confirmDialog('Confirmar cancelamento', 'A venda será cancelada, o estoque volta e a operação fica registrada na auditoria.', 'Cancelar venda', {danger: true, cancelLabel: 'Manter venda'}))) return;
  await api('/api/sales/' + encodeURIComponent(id) + '/void', 'POST', {reason, manager_pin: pin});
  pin = '';
  toast('Venda cancelada. Devolva o pagamento ao cliente pela mesma forma.');
  await Promise.all([loadSales(), poll()]);
}
async function loadStock() {
  const data = await api('/api/products?q=' + encodeURIComponent($('stockSearch').value.trim()));
  state.stockProducts = data.products;
  const products = state.stockProducts, select = $('stockProduct'), previous = select.value, total = Number(data.total) || products.length;
  $('stockCount').textContent = total > products.length ? `Mostrando ${products.length} de ${total} produtos. Refine a busca para ver os demais.` : '';
  select.innerHTML = products.map(product => `<option value="${esc(product.id)}">${esc(product.name)} (${esc(product.unit)})</option>`).join('');
  select.value = C.keepSelection(previous, products.map(product => product.id));
  $('stockList').innerHTML = products.length ? products.map(product => `<div class="list-row"><span><strong>${esc(product.name)}</strong><small>${esc(product.sku || product.id)}${product.barcode ? ' · EAN ' + esc(product.barcode) : ''}</small></span><strong class="num">${product.stock_controlled === false ? 'Sem controle' : `${qtyText(product.stock_milli, product.unit)} ${esc(product.unit)}`}</strong></div>`).join('') : '<p class="list-empty">Nenhum produto encontrado.</p>';
}
async function loadPrinters() {
  const data = await api('/api/print/settings');
  applyPrinterData(data);
  const settings = (data.settings || []).filter(setting => setting.kind === 'receipt'), jobs = (data.jobs || []).filter(job => job.kind === 'receipt');
  $('printerSettings').innerHTML = settings.length ? settings.map(setting => `<div class="list-row"><span><strong>Recibo térmico</strong><small>${esc(driverNames[setting.driver] || setting.driver)} · ${esc(setting.format === 'escpos58' ? 'bobina 58 mm' : 'bobina 80 mm')}</small></span><span>${esc(setting.target)}</span></div>`).join('') : '<p class="list-empty">Nenhuma impressora de recibos configurada.</p>';
  $('printJobs').innerHTML = jobs.length ? jobs.map(job => `<div class="list-row"><span><strong>Recibo ${badge(jobStatusNames[job.status] || [job.status, ''])}</strong><small>${dateTimeText(job.created_at)} · ${esc(driverNames[job.driver] || job.driver)}${job.error ? ' · ' + esc(job.error) : ''}</small></span><span class="list-actions">${['uncertain', 'printed'].includes(job.status) ? `<button class="btn" data-retry="${esc(job.id)}">Reenviar</button>` : ''}</span></div>`).join('') : '<p class="list-empty">Fila de recibos vazia.</p>';
}
async function loadSystem() {
  const result = await api('/api/sync'), sync = result.sync, license = result.license, indicator = syncIndicator(sync, Date.now());
  $('aboutVersion').textContent = `Versão instalada: ${result.version || '—'}`;
  renderLease(license);
  api('/api/update/status').then(renderUpdate).catch(() => { $('updateState').textContent = 'Estado: não foi possível consultar a atualização.'; });
  $('systemPending').textContent = sync.pending;
  $('systemReview').textContent = indicator.review;
  $('systemConflicts').textContent = sync.conflicts ?? 0;
  $('systemLastSync').textContent = sync.last_sync_at ? dateTimeText(sync.last_sync_at) : 'Ainda não ocorreu';
  $('systemLease').textContent = license.expires_at ? new Date(license.expires_at * 1000).toLocaleString('pt-BR') : license.reason;
  $('systemError').textContent = indicator.detail;
  $('identityInfo').textContent = JSON.stringify({connection: state.session.connection, device_id: state.session.device_id, paired: state.session.paired, license: license.state, heartbeat_at: sync.last_heartbeat_at}, null, 2);
  if (!canManage(role())) { $('auditList').textContent = 'Auditoria disponível para gerentes e administradores.'; $('conflictList').textContent = 'Conflitos disponíveis para gerentes e administradores.'; $('reviewList').textContent = 'Detalhes disponíveis para gerentes e administradores.'; return; }
  loadReview().then(data => { $('reviewList').innerHTML = reviewMarkup(data); }).catch(() => { $('reviewList').textContent = 'Não foi possível carregar os registros em revisão.'; });
  try {
    const [audit, conflicts] = await Promise.all([api('/api/audit'), api('/api/sync/conflicts')]);
    $('auditList').innerHTML = audit.events.map(event => `<div class="mini-row"><span>${esc(event.action)}${event.subject_id ? ' · ' + esc(String(event.subject_id).slice(0, 8)) : ''}</span><small>${dateTimeText(event.created_at)}</small></div>`).join('') || 'Sem eventos.';
    $('conflictList').innerHTML = (conflicts.conflicts || []).map(conflict => `<div class="mini-row"><span>${esc(conflict.reason)} · ${esc(conflict.aggregate_id)}</span><small>${esc(conflict.status)}</small></div>`).join('') || 'Sem conflitos locais.';
  } catch { $('auditList').textContent = 'Não foi possível carregar a auditoria.'; $('conflictList').textContent = 'Não foi possível carregar os conflitos.'; }
}
function renderLease(license = {}) {
  const until = license.expires_at ? new Date(license.expires_at * 1000).toLocaleString('pt-BR') : '';
  const attention = !!license.warning || license.state !== 'active';
  $('leaseCard').classList.toggle('attention', attention);
  $('leaseInfo').textContent = !attention ? `Vale até ${until} (${license.days_left} dia${license.days_left === 1 ? '' : 's'} sem precisar de internet).`
    : `${license.reason || 'Licença indisponível'}${until ? ` · ${license.state === 'expired' ? 'venceu em' : 'vale até'} ${until}` : ''}`;
}
const updateStateNames = {idle: 'nenhuma atualização em andamento', downloading: 'baixando a nova versão…', staged: 'nova versão baixada e conferida, pronta para instalar',
  applying: 'instalando a nova versão; o caixa reinicia sozinho', failed: 'a última tentativa falhou; a versão anterior continua em uso'};
function renderUpdate(update = {}) {
  const version = update.staged_version || update.available?.version, cashOpen = !!state.session?.cash, manager = canManage(role());
  $('currentVersion').textContent = `Versão instalada: ${update.current_version || '—'}`;
  $('updateAvailable').textContent = version ? `Versão disponível: ${version}${update.available?.notes ? ' · ' + update.available.notes : ''}` : 'Versão disponível: nenhuma. Este caixa está atualizado.';
  $('updateState').textContent = `Estado: ${updateStateNames[update.state] || update.state || '—'}${update.error ? ' · ' + update.error : ''}`;
  const reason = !update.supported ? update.blocked_reason : !version ? '' : cashOpen ? 'Feche o caixa para atualizar.' : !manager ? 'Somente gerentes podem atualizar.'
    : update.can_apply ? '' : (update.blocked_reason || 'A nova versão ainda está sendo preparada.');
  const button = $('updateApplyBtn');
  button.classList.toggle('hidden', !update.supported);
  button.disabled = !(update.can_apply && !cashOpen && manager);
  $('updateBlocked').textContent = reason || '';
  const link = $('releaseLink'), manual = !update.supported && update.available?.url;
  link.classList.toggle('hidden', !manual);
  if (manual) link.href = update.available.url; else link.removeAttribute('href');
}
const eventTypeNames = {'sale.completed': 'Venda', 'sale.voided': 'Cancelamento de venda', 'sale.returned': 'Devolução', 'cash.opened': 'Abertura de caixa', 'cash.closed': 'Fechamento de caixa', 'cash.movement': 'Sangria ou suprimento', 'stock.adjusted': 'Ajuste de estoque', 'credit.received': 'Recebimento de fiado'};
const skippedKindNames = {product: 'Produto', barcode: 'Código de barras', operator: 'Operador', customer: 'Cliente', promotion: 'Promoção', payment_method: 'Forma de pagamento'};
async function loadReview() {
  const [quarantine, skipped] = await Promise.all([api('/api/sync/quarantine'), api('/api/catalog/skipped')]);
  return {events: quarantine.events || [], skipped: skipped.items || []};
}
function reviewMarkup({events, skipped}) {
  const eventRows = events.map(event => `<div class="mini-row"><span><strong>${esc(eventTypeNames[event.event_type] || event.event_type)}</strong> · ${esc(String(event.aggregate_id).slice(0, 8))}<br><small>${esc(event.message || event.reason)}</small></span><small>${dateTimeText(event.created_at)}</small></div>`).join('');
  const skippedRows = skipped.map(item => `<div class="mini-row"><span><strong>${esc(skippedKindNames[item.kind] || item.kind)}</strong>${item.id ? ' · ' + esc(String(item.id).slice(0, 8)) : ''}<br><small>${esc(item.error)}</small></span></div>`).join('');
  return `<h3 class="review-title">Registros em revisão no servidor (${events.length})</h3>${eventRows || '<p class="list-empty">Nenhum registro aguardando revisão.</p>'}
    <h3 class="review-title">Itens do catálogo ignorados por este caixa (${skipped.length})</h3>${skippedRows || '<p class="list-empty">Nenhum item ignorado na última atualização do catálogo.</p>'}`;
}
// Detail of the "N em revisão" chip of the status bar.
async function reviewDialog() {
  if (!canManage(role())) { toast($('reviewChip').title.replace(' Toque para ver os detalhes.', '') + ' Chame o gerente para ver os detalhes.'); return; }
  const data = await loadReview();
  const modal = openModal({title: 'Registros em revisão', eyebrow: 'Sincronização', className: 'wide', text: 'O servidor recebeu estes registros, mas não os aplicou. Nada foi perdido: o gestor confere cada um no painel da empresa. As vendas seguintes continuam sendo enviadas.'});
  modal.body.append(el('div', 'mini-list review-list', reviewMarkup(data)));
  modal.action('Fechar <kbd>Esc</kbd>', 'primary', () => modal.close()).focus();
  await modal.promise;
  focusHome();
}
/* ---------- second-disk copy (Sistema) ---------- */
function renderMirror(status) {
  const view = C.mirrorCard(status), manager = canManage(role());
  $('mirrorCard').classList.toggle('attention', view.attention);
  $('mirrorInfo').textContent = view.text;
  $('mirrorDetail').textContent = !status?.supported ? '' : [status.last_snapshot_at ? 'Última cópia completa: ' + dateTimeText(status.last_snapshot_at) : '', status.pending_events ? `${status.pending_events} registro(s) ainda só neste computador` : '', status.last_error || ''].filter(Boolean).join(' · ');
  $('mirrorChooseBtn').disabled = !view.canChoose;
  $('mirrorSnapshotBtn').disabled = !view.canCopy;
  $('mirrorChooseBtn').classList.toggle('hidden', !manager);
  $('mirrorSnapshotBtn').classList.toggle('hidden', !manager);
}
async function loadMirror() {
  try { renderMirror(await api('/api/mirror/status')); } catch { $('mirrorInfo').textContent = 'Não foi possível consultar a cópia de segurança.'; }
}
async function chooseMirrorDisk() {
  const data = await api('/api/mirror/drives');
  const modal = openModal({title: 'Onde guardar a cópia de segurança?', eyebrow: 'Cópia em outro disco', text: 'Escolha um pendrive ou um segundo disco. Não use o mesmo disco deste caixa: se ele quebrar, a cópia vai junto.'});
  const save = async directory => { renderMirror(await api('/api/mirror/config', 'POST', {directory})); toast(directory ? 'Cópia de segurança ligada. A primeira cópia já foi feita.' : 'Cópia de segurança desligada.'); modal.close(true); };
  const list = el('div', 'mini-list');
  for (const drive of data.drives || []) {
    const row = el('div', 'mini-row', `<span><strong>${esc(drive.label || drive.directory)}</strong>${drive.same_volume ? '<br><small>Mesmo disco do caixa: escolha outro</small>' : ''}</span>`);
    const button = el('button', 'btn', 'Usar este');
    button.type = 'button';
    button.disabled = !!drive.same_volume;
    button.addEventListener('click', () => modal.run(() => save(drive.directory)));
    row.append(button);
    list.append(row);
  }
  if (!list.children.length) list.textContent = 'Nenhum pendrive ou segundo disco encontrado. Ligue um e abra esta janela de novo, ou informe a pasta abaixo.';
  const field = el('label', 'field'), input = document.createElement('input');
  input.type = 'text';
  input.autocomplete = 'off';
  input.placeholder = 'Ex.: E:\\ ou /Volumes/PENDRIVE';
  field.append(Object.assign(el('span'), {textContent: 'Ou informe a pasta'}), input);
  modal.body.append(list, field);
  modal.action('Cancelar', '', () => modal.close(null));
  modal.action('Desligar a cópia', 'danger', () => save(null));
  modal.action('Usar a pasta informada', 'primary', async () => { const folder = input.value.trim(); if (!folder) { input.focus(); throw Error('Informe a pasta ou escolha um disco da lista'); } await save(folder); });
  await modal.promise;
}
async function loadBackups() {
  if (!canManage(role())) { $('backupInfo').textContent = 'Backup local disponível para gerentes e administradores.'; return; }
  try {
    const data = await api('/api/backups');
    $('backupInfo').textContent = data.copies.length ? `${data.copies.length} cópia(s) em ${data.directory}. Mais recente: ${data.copies[0].name}` : `Nenhuma cópia criada. Destino: ${data.directory}`;
  } catch { $('backupInfo').textContent = 'Não foi possível carregar os backups locais.'; }
}
/* ---------- "Recuperar minha loja" (new computer) ---------- */
function renderRestore(restore) {
  const flow = state.restore;
  const step = flow.finished ? 'done' : restore?.active ? C.restoreStep(restore) : 'start';
  showView('restoreView');
  for (const [id, name] of [['restoreStart', 'start'], ['restoreProgress', 'progress'], ['restoreBackup', 'backup'], ['restoreDone', 'done']]) $(id).classList.toggle('hidden', name !== step);
  if (step === 'start') {
    const fixed = !!state.session?.default_cloud_url;
    $('restoreCloudField').classList.toggle('hidden', fixed);
    $('restoreCloudUrl').required = !fixed;
  } else if (step === 'progress') {
    renderRestoreProgress(restore, false);
    runRestore();
  } else if (step === 'done') $('restoreSummary').textContent = C.restoreSummary(flow.finished);
}
function renderRestoreProgress(restore, failed) {
  const view = C.restoreProgress(restore, failed);
  $('restoreBarFill').style.width = view.percent + '%';
  $('restoreBar').setAttribute('aria-valuenow', String(view.percent));
  $('restoreLine').textContent = view.line;
  $('restoreNotice').textContent = view.notice;
  $('restoreNotice').classList.toggle('hidden', !view.notice);
  $('restoreRetryBtn').classList.toggle('hidden', !view.retry);
}
// One loop asks the terminal for the next pages until the download ends, pauses or needs an answer.
async function runRestore() {
  const flow = state.restore;
  if (flow.running) return;
  flow.running = true;
  clearTimeout(flow.timer);
  let wait = 0;
  try {
    const {restore} = await api('/api/restore/step', 'POST', {});
    if (!restore.active) { finishRestore(restore); return; }
    if (restore.state === 'awaiting_backup') { renderRestore(restore); return; }
    renderRestoreProgress(restore, false);
    if (restore.paused) return;
    wait = restore.error ? 5000 : 150;
  } catch (error) {
    // 409: the restore ended elsewhere (the background worker finished it). Anything else: try again soon.
    if (error.status === 409) { flow.running = false; await loadSession(); return; }
    renderRestoreProgress(state.session?.restore, true);
    wait = 5000;
  } finally { flow.running = false; }
  if (wait) flow.timer = setTimeout(() => { if (!$('restoreProgress').classList.contains('hidden') && !$('restoreView').classList.contains('hidden')) runRestore(); }, wait);
}
function finishRestore(restore) {
  state.restore.finished = restore;
  state.restore.open = false;
  renderRestore(restore);
  $('restoreEnterBtn').focus();
}
async function loadRestoreBackups(directory = '') {
  $('restoreBackupPanel').classList.remove('hidden');
  $('restoreBackupInfo').textContent = 'Procurando cópias de segurança…';
  $('restoreBackupList').innerHTML = '';
  const data = await api('/api/restore/backups' + (directory ? '?directory=' + encodeURIComponent(directory) : ''));
  if (!data.supported) { $('restoreBackupInfo').textContent = 'Esta instalação não lê cópias de segurança. Toque em “Não tenho, continuar”.'; return; }
  const found = data.candidates || [];
  $('restoreBackupInfo').textContent = found.length ? 'Escolha a cópia do computador antigo:' : 'Nenhuma cópia de segurança encontrada. Ligue o pendrive ou o disco e informe a pasta abaixo.';
  $('restoreBackupList').innerHTML = found.map((copy, index) => `<div class="mini-row"><span><strong>${esc(copy.terminal_name || 'Caixa')}${copy.shop_name ? ' · ' + esc(copy.shop_name) : ''}</strong>${copy.matches ? '' : ' · de outro caixa'}${copy.current_device ? ' · feita neste computador' : ''}<br><small>${esc(copy.directory)}${copy.updated_at ? ' · atualizada em ' + dateTimeText(copy.updated_at) : ''}</small></span><button type="button" class="btn${copy.matches ? ' primary' : ''}" data-restore-copy="${index}"${copy.matches ? '' : ' disabled'}>Usar esta cópia</button></div>`).join('');
  state.restore.candidates = found;
}
async function importRestoreBackup(copy) {
  const result = await api('/api/restore/import-backup', 'POST', {directory: copy.directory, replica_id: copy.replica_id});
  toast(C.restoreImportText(result));
  if (result.restore.state === 'awaiting_backup') return;  // nothing new in that copy: the person chooses again or continues
  renderRestore(result.restore);
}

// A paired terminal only changes company with a manager: the PIN goes in the request and is not kept.
async function maintenancePin(title, text) {
  const session = state.session;
  if (!session?.paired || canManage(role()) || !(session.operators || []).some(operator => canManage(operator.role))) return '';
  const pin = await pinDialog(title, text);
  if (!pin) throw Error('Operação cancelada: é necessário o PIN de um gerente.');
  return pin;
}
async function createNewProfile() {
  if (!(await confirmDialog('Criar perfil para outra empresa?', 'O banco atual será preservado em um backup e o próximo pareamento começará em um diretório separado.', 'Criar perfil'))) return;
  const pin = await maintenancePin('Autorizar novo perfil', 'Este caixa já está vinculado a uma empresa. Informe o PIN de um gerente para criar outro perfil.');
  const result = await api('/api/new-profile', 'POST', {confirm: true, manager_pin: pin});
  state.session = null;
  clearLocalSale();
  state.lastOperatorsRefreshAt = 0;
  toast(`Novo perfil criado: ${result.profile}. Informe a licença e o código da outra empresa.`);
  await loadSession();
}
function formatMoneyInput(input) {
  try { if (input.value.trim()) input.value = moneyPlain(parseMoney(input.value)); } catch { /* the error is shown when the value is used */ }
}
function updatePrinterDriverHint() {
  const driver = $('printerDriver').value;
  $('discoverPrintersBtn').classList.toggle('hidden', driver !== 'cups');
  $('printerTarget').placeholder = {cups: 'Fila CUPS, ex.: EPSON_TM_T20', tcp: 'IP da impressora:9100', windows_raw: 'Nome exato da fila no Windows', file: 'Pasta para arquivos de diagnóstico'}[driver];
  $('printerDiscoveryStatus').textContent = {cups: 'Use uma fila do macOS/Linux. No Docker, a ponte local de impressão precisa estar em execução.', tcp: 'Informe o endereço da impressora de rede e a porta, por exemplo 192.168.1.50:9100.', windows_raw: 'Use o nome da fila instalada no Windows. O terminal precisa rodar nesse computador.', file: 'Modo arquivo gera um .bin para diagnóstico e testes.'}[driver];
}

/* ---------- wiring ---------- */
// One handler at a time per control: a double tap must not repeat a stock adjustment or a pairing.
function on(id, eventName, handler) {
  const node = $(id);
  let busy = false;
  node.addEventListener(eventName, async event => {
    if (eventName === 'submit') event.preventDefault();
    if (busy) return;
    busy = true;
    try { await handler(event); } catch (error) { toast(error.message, true); } finally { busy = false; }
  });
}
on('pairForm', 'submit', async () => {
  await api('/api/pair', 'POST', {cloud_url: $('cloudUrl').value, license_key: $('licenseKey').value.trim(), code: $('pairCode').value.trim()});
  $('licenseKey').value = '';
  $('pairCode').value = '';
  toast('Terminal vinculado à empresa. Entre com o PIN do operador.');
  await loadSession();
});
on('restoreOpenBtn', 'click', async () => { state.restore.open = true; renderRestore(state.session?.restore); $('restoreLicenseKey').focus(); });
on('restoreBackBtn', 'click', async () => { state.restore.open = false; showView('pairView'); });
on('restoreForm', 'submit', async () => {
  const result = await api('/api/restore/start', 'POST', {cloud_url: state.session?.default_cloud_url || $('restoreCloudUrl').value, license_key: $('restoreLicenseKey').value.trim(), code: $('restoreCode').value.trim()});
  $('restoreLicenseKey').value = '';
  $('restoreCode').value = '';
  state.restore.open = false;
  if (!result.recovery) toast('Este caixa ainda não tinha vendas guardadas. Entre com o PIN do operador.');
  await loadSession();
});
on('restoreRetryBtn', 'click', async () => { const {restore} = await api('/api/restore/retry', 'POST', {}); renderRestoreProgress(restore, false); await runRestore(); });
on('restoreHasBackupBtn', 'click', () => loadRestoreBackups());
on('restoreFolderForm', 'submit', async () => { const folder = $('restoreFolder').value.trim(); if (!folder) throw Error('Informe a pasta onde está a cópia de segurança'); await loadRestoreBackups(folder); });
on('restoreSkipBtn', 'click', async () => { const {restore} = await api('/api/restore/skip-backup', 'POST', {}); finishRestore(restore); });
on('restoreEnterBtn', 'click', async () => { state.restore.finished = null; await loadSession(); });
$('restoreBackupList').addEventListener('click', event => {
  const copy = state.restore.candidates?.[Number(event.target.closest('[data-restore-copy]')?.dataset.restoreCopy)];
  if (copy) importRestoreBackup(copy).catch(error => toast(error.message, true));
});
on('loginForm', 'submit', async () => {
  if (state.loginLockedUntil > Date.now()) throw Error(`Aguarde ${Math.ceil((state.loginLockedUntil - Date.now()) / 1000)} segundos para tentar novamente`);
  try {
    const result = await api('/api/login', 'POST', {operator_id: $('operatorSelect').value, pin: $('operatorPin').value});
    state.csrf = result.csrf;
    state.loginLockedUntil = 0;
    prefs.set('operator', $('operatorSelect').value);
    $('operatorPin').value = '';
    await loadSession();
  } catch (error) {
    $('operatorPin').value = '';
    $('operatorPin').focus();
    beep('error');
    if (error.status === 429) { state.loginLockedUntil = Date.now() + 300000; throw Error('Muitas tentativas; aguarde cinco minutos antes de tentar novamente'); }
    throw error;
  }
});
$('pinPad').addEventListener('click', event => {
  const key = event.target.closest('[data-pin]')?.dataset.pin, input = $('operatorPin');
  if (!key) return;
  if (key === 'clear') input.value = ''; else if (key === 'back') input.value = input.value.slice(0, -1); else if (input.value.length < 12) input.value += key;
  input.focus();
});
on('refreshBootstrap', 'click', async () => { state.lastOperatorsRefreshAt = Date.now(); await refreshOperators(); await loadSession(); });
on('changeConnection', 'click', async () => {
  if (!(await confirmDialog('Corrigir pareamento de teste?', 'Use esta opção apenas quando não existem vendas, caixa ou movimentações. Para outra empresa, crie um perfil separado.', 'Corrigir pareamento', {danger: true}))) return;
  const pin = await maintenancePin('Autorizar correção do pareamento', 'Este caixa já está vinculado a uma empresa. Informe o PIN de um gerente para remover o pareamento.');
  await api('/api/unpair', 'POST', {confirm: true, manager_pin: pin});
  state.session = null;
  clearLocalSale();
  toast('Pareamento removido. Informe a licença e o novo código.');
  await loadSession();
});
on('newProfileBtn', 'click', createNewProfile);
on('newProfileLoginBtn', 'click', createNewProfile);
document.querySelectorAll('.tab').forEach(button => button.addEventListener('click', () => setTab(button.dataset.tab)));
$('operatorMenuBtn').addEventListener('click', openMenu);
$('netChip').addEventListener('click', () => toast(state.indicator?.detail || ''));
$('queueChip').addEventListener('click', () => toast($('queueChip').title));
$('reviewChip').addEventListener('click', () => reviewDialog().catch(error => toast(error.message, true)));
$('printerChip').addEventListener('click', () => { if (canManage(role())) setTab('printers'); else toast($('printerChip').title); });
$('scaleChip').addEventListener('click', () => runAction('scale'));
$('openSearchBtn').addEventListener('click', () => runAction('search'));
$('productSearch').addEventListener('input', () => {
  // Text that reaches the reading field without key events (paste, input method) is not a reading while paying.
  if (state.pay.active) { $('productSearch').value = ''; focusPay(); return; }
  if (state.done) { state.done = null; renderSale(); }
  if ($('scanStatus').dataset.kind === 'error') scanStatus('', 'Leitor pronto');
});
$('cartItems').addEventListener('click', event => {
  const quantity = event.target.closest('[data-qty]'), remove = event.target.closest('[data-remove]'), row = event.target.closest('.item-row');
  if (!row) return;
  state.selectedId = row.dataset.id;
  renderCart();
  if (quantity) changeQuantity(quantity.dataset.qty).catch(error => toast(error.message, true));
  else if (remove) removeItem(remove.dataset.remove).catch(error => toast(error.message, true));
  else focusHome();
});
$('paymentMethods').addEventListener('click', event => {
  const button = event.target.closest('[data-pay]');
  if (button) selectPayMethod(button.dataset.pay === 'split' ? 'split' : state.payOptions[Number(button.dataset.pay)]);
});
$('quickValues').addEventListener('click', event => {
  const quick = event.target.closest('[data-quick]')?.dataset.quick;
  if (!quick) return;
  $('payCash').value = moneyPlain(quick === 'exact' ? cartTotal() : Number(quick));
  payError('');
  renderPay();
  $('payCash').focus();
});
for (const id of ['payCash', 'paySplitCash', 'paySplitPix', 'paySplitDebit', 'paySplitCredit']) {
  $(id).addEventListener('input', () => { payError(''); renderPay(); });
  $(id).addEventListener('blur', () => formatMoneyInput($(id)));
}
$('sellBtn').addEventListener('click', () => { if (state.pay.active && state.pay.method) confirmSale(); else runAction('pay'); });
// Works for the ruler and for any button that names an action (blocked panel, done panel).
document.addEventListener('click', event => {
  if (event.target.closest('[data-done-dismiss]')) { dismissDone(); return; }
  const key = event.target.closest('#keyBar .key');
  if (key?.dataset.paykey != null) { const option = key.dataset.paykey === '0' ? 'split' : state.payOptions.find(row => row.key === key.dataset.paykey); if (option) selectPayMethod(option); return; }
  const target = event.target.closest('#keyBar [data-action], #blockedPanel [data-action], #donePanel [data-action]');
  if (!target || modalStack.length) return;
  if (target.dataset.action === 'confirm') { confirmSale(); return; }
  if (state.pay.active && key && !PAY_CONTEXT_ACTIONS.includes(target.dataset.action)) { toast('Tecle Esc para voltar à venda antes de usar esta função', true); return; }
  runAction(target.dataset.action);
});
// Keep the focus where it is (scan field, cash field) when the ruler is tapped.
$('keyBar').addEventListener('mousedown', event => event.preventDefault());
on('reloadSales', 'click', loadSales);
$('salesList').addEventListener('click', event => {
  const button = event.target.closest('button');
  if (!button) return;
  const run = async () => {
    if (button.dataset.detail) await receiptDialog((await api('/api/sales/' + encodeURIComponent(button.dataset.detail))).sale, 'Comprovante pronto para visualização.');
    else if (button.dataset.reprint) {
      try { await api('/api/sales/' + encodeURIComponent(button.dataset.reprint) + '/reprint', 'POST', {}); toast('Comprovante colocado na fila da impressora'); api('/api/print/flush', 'POST', {}).then(loadPrinterStatus).catch(() => {}); }
      catch (error) { if (error.status !== 409) throw error; await receiptDialog((await api('/api/sales/' + encodeURIComponent(button.dataset.reprint))).sale, error.message); }
    } else if (button.dataset.return) await returnDialog(button.dataset.return);
    else if (button.dataset.void) await voidSale(button.dataset.void);
  };
  run().catch(error => toast(error.message, true));
});
let stockSearchTimer;
$('stockSearch').addEventListener('input', () => { clearTimeout(stockSearchTimer); stockSearchTimer = setTimeout(() => loadStock().catch(error => toast(error.message, true)), 180); });
on('adjustStockBtn', 'click', async () => {
  const delta = parseQty($('stockDelta').value, true);
  await api('/api/stock/adjust', 'POST', {product_id: $('stockProduct').value, delta_milli: delta, reason: $('stockReason').value});
  $('stockDelta').value = '';
  $('stockReason').value = '';
  toast('Estoque ajustado');
  await loadStock();
  await refreshCartProducts();
});
on('printerDriver', 'change', updatePrinterDriverHint);
on('discoverPrintersBtn', 'click', async () => {
  $('printerDiscoveryStatus').textContent = 'Procurando filas CUPS neste computador…';
  const result = await api('/api/print/discover');
  if (!result.queues?.length) { $('printerDiscoveryStatus').textContent = 'Nenhuma fila CUPS foi encontrada. Adicione a impressora no sistema operacional e tente de novo.'; throw Error('Nenhuma fila CUPS encontrada'); }
  $('printerTarget').value = result.queues[0];
  $('printerDiscoveryStatus').textContent = `Fila encontrada: ${result.queues.join(', ')}. A primeira foi preenchida no destino.`;
});
on('printerForm', 'submit', async () => {
  await api('/api/print/settings', 'POST', {kind: 'receipt', driver: $('printerDriver').value, format: $('printerFormat').value, target: $('printerTarget').value});
  toast('Impressora de recibos salva');
  await loadPrinters();
});
on('flushPrintBtn', 'click', async () => {
  const result = await api('/api/print/flush', 'POST', {});
  toast(result.status === 'uncertain' ? 'Resultado de impressão incerto; verifique o equipamento' : 'Fila processada', result.status === 'uncertain');
  await loadPrinters();
});
$('printJobs').addEventListener('click', event => {
  const button = event.target.closest('[data-retry]');
  if (!button) return;
  (async () => {
    if (!(await confirmDialog('Reenviar impressão?', 'A impressora pode já ter concluído este trabalho. O reenvio pode produzir um comprovante duplicado.', 'Reenviar'))) return;
    await api('/api/print/jobs/' + encodeURIComponent(button.dataset.retry) + '/retry', 'POST', {confirm_duplicate: true});
    toast('Reenvio colocado na fila');
    await loadPrinters();
  })().catch(error => toast(error.message, true));
});
on('syncNowBtn', 'click', async () => { const result = await api('/api/sync/now', 'POST', {}); toast(`${result.sent} registro(s) enviados`); await poll(); await loadSystem(); });
on('leaseRenewBtn', 'click', recheckLicense);
on('updateApplyBtn', 'click', async () => {
  if (!(await confirmDialog('Atualizar o PDV2 agora?', 'O caixa fica fora do ar por cerca de um minuto e volta sozinho. Antes é feito um backup do banco; se a nova versão não subir, a anterior é restaurada.', 'Atualizar agora'))) return;
  await api('/api/update/apply', 'POST', {});
  toast('Atualização iniciada. O caixa reinicia em instantes.');
  api('/api/update/status').then(renderUpdate).catch(() => {});
});
$('damagedRetryBtn').addEventListener('click', () => window.location.reload());
on('mirrorChooseBtn', 'click', chooseMirrorDisk);
on('mirrorSnapshotBtn', 'click', async () => { await api('/api/mirror/snapshot', 'POST', {}); toast('Cópia de segurança feita agora.'); await loadMirror(); });
on('backupBtn', 'click', async () => { const copy = await api('/api/backups', 'POST', {}); toast(`Backup criado: ${copy.name}`); await loadBackups(); });
on('bootstrapBtn', 'click', async () => { await api('/api/bootstrap', 'POST', {}); toast('Catálogo e licença atualizados'); await poll(); await loadSystem(); });

function handlePayKey(event, editable) {
  const key = event.key, scan = $('productSearch');
  let target = event.target;
  // The reading field is out of play while paying: a key that reaches it belongs to the payment.
  if (target === scan) {
    scan.value = '';
    focusPay();
    target = document.activeElement;
    editable = target !== scan && !!target.matches?.('input');
    if (target === scan) scan.blur();
  }
  if (key === 'Enter') {
    if (target.matches?.('button') && !scannerBurst()) return;
    event.preventDefault();
    if (scannerBurst()) {
      const pickedByReading = state.pay.method && performance.now() - (state.pay.pickedAt || 0) < 1500;
      if (pickedByReading) { cancelPayment(); state.pay.active = true; renderSale(); $('paymentMethods').focus(); }
      else if (target.matches?.('input')) target.value = '';
      scan.value = '';
      payError('Leitura de código ignorada durante o pagamento. Tecle Esc para voltar à venda.');
      renderPay();
      return;
    }
    confirmSale();
    return;
  }
  if (editable) return;
  if (/^[0-9]$/.test(key)) { event.preventDefault(); pickPayDigit(key); return; }
  if (key.length === 1) event.preventDefault();
}
// Single keyboard entry point: dialogs first, then the shortcut map, then the sale screen.
document.addEventListener('keydown', event => {
  noteKey(event);
  const key = event.key, functionKey = /^F([1-9]|1[0-2])$/.test(key);
  const logged = !!state.session?.operator && !$('appView').classList.contains('hidden');
  if (logged && functionKey) event.preventDefault();
  if (modalStack.length) { modalStack.at(-1).handleKey(event); return; }
  if (!logged) return;
  if ((event.ctrlKey || event.metaKey) && key.toLowerCase() === 'k') { event.preventDefault(); runAction('palette'); return; }
  const shortcut = !event.ctrlKey && !event.metaKey && !event.altKey ? C.SHORTCUTS.find(row => row.key === key) : null;
  if (shortcut) {
    event.preventDefault();
    if (event.repeat) return;
    if (state.pay.active && state.tab === 'checkout' && !PAY_CONTEXT_ACTIONS.includes(shortcut.action)) { toast('Tecle Esc para voltar à venda antes de usar esta função', true); return; }
    runAction(shortcut.action);
    return;
  }
  if (state.tab !== 'checkout' || !canSell(role()) || event.ctrlKey || event.metaKey || event.altKey) return;
  const target = event.target, scan = $('productSearch');
  const editable = !!target.matches?.('input,textarea,select,[contenteditable="true"]');
  if (state.pay.active) { handlePayKey(event, editable); return; }
  if (key === 'Enter') {
    if (target === scan) { event.preventDefault(); processScan(); }
    else if (!editable && !target.matches?.('button,a,summary')) { event.preventDefault(); if (scan.value.trim()) processScan(); else if (state.done) dismissDone(); }
    return;
  }
  if (key === 'Tab' && target === scan && scan.value.trim()) { event.preventDefault(); processScan(); return; }
  if (key === 'ArrowDown' || key === 'ArrowUp') {
    if (target.matches?.('select,textarea')) return;
    event.preventDefault();
    moveSelection(key === 'ArrowDown' ? 1 : -1);
    return;
  }
  if (key === 'Delete') {
    if ((target === scan && scan.value) || (editable && target !== scan)) return;
    event.preventDefault();
    runAction('removeItem');
    return;
  }
  // Typing anywhere on the sale screen goes to the reading field instead of being lost.
  if (key.length === 1 && !editable && !(key === ' ' && target.matches?.('button,a,summary'))) scan.focus();
}, true);
document.addEventListener('fullscreenchange', renderKeybar);

if (prefs.get('focus', 'off') === 'on') document.body.classList.add('focus-mode');
updatePrinterDriverHint();
tickClock();
setInterval(tickClock, 15000);
loadSession().catch(error => toast(error.message, true));
setInterval(() => poll().catch(() => {}), 15000);
