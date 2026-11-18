/* Pure helpers of the cashier screen. No DOM access here, so the same file is
   loaded by the browser (window.PDVCore) and by the node-run regression tests. */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.PDVCore = api;
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  // Single source for the key ruler, the keyboard handler, the help panel and the command palette.
  const SHORTCUTS = [
    {key: 'F1', action: 'help', label: 'Ajuda'},
    {key: 'F2', action: 'search', label: 'Buscar'},
    {key: 'F3', action: 'quantity', label: 'Quantidade', short: 'Qtde'},
    {key: 'F4', action: 'customer', label: 'Cliente/CPF'},
    {key: 'F5', action: 'discount', label: 'Desconto'},
    {key: 'F6', action: 'hold', label: 'Em espera'},
    {key: 'F7', action: 'cashMove', label: 'Sangria/Suprimento', short: 'Sangria'},
    {key: 'F8', action: 'removeItem', label: 'Cancelar item'},
    {key: 'F9', action: 'reprint', label: 'Reimprimir'},
    {key: 'F10', action: 'pay', label: 'Pagar'},
    {key: 'F11', action: 'focus', label: 'Foco', hint: 'Modo foco (tela cheia)'},
    {key: 'F12', action: 'closeCash', label: 'Caixa', hint: 'Menu do caixa: pausar, fechar turno, relatórios'},
    {key: 'Escape', cap: 'Esc', action: 'back', label: 'Voltar'},
    // Dedicated key of PC keyboards; F12 also has it. Not on the key ruler (ruler: false).
    {key: 'Pause', cap: 'Pause', action: 'pauseCash', label: 'Pausar caixa', ruler: false},
  ];
  // key: digit while paying; alt: Alt+letter (mnemonic), which also works from the amount field.
  const PAY_KEYS = [
    {key: '1', alt: 'D', method: 'cash', label: 'Dinheiro', icon: 'cash'},
    {key: '2', alt: 'P', method: 'pix', label: 'Pix', icon: 'pix'},
    {key: '3', alt: 'B', method: 'card_debit', label: 'Débito', icon: 'card'},
    {key: '4', alt: 'C', method: 'card_credit', label: 'Crédito', icon: 'card'},
    {key: '5', alt: 'F', method: 'credit_account', label: 'A prazo (fiado)', short: 'A prazo', icon: 'book'},
    {key: '6', alt: 'L', method: 'store_credit', label: 'Crédito na loja', short: 'Crédito loja', icon: 'gift'},
  ];
  const SPLIT_OPTION = {key: '0', alt: '0', method: 'split', label: 'Dividir', icon: 'split', hint: 'Dividir o pagamento em mais de uma forma'};
  // Mirrors DISCOUNT_BPS in terminal/store.py; the backend stays authoritative.
  const DISCOUNT_BPS = {cashier: 1000, manager: 5000, admin: 10000, owner: 10000};
  const STALE_CONTACT_MS = 150000;

  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
  const money = cents => new Intl.NumberFormat('pt-BR', {style: 'currency', currency: 'BRL'}).format((Number(cents) || 0) / 100);
  const moneyPlain = cents => ((Number(cents) || 0) / 100).toLocaleString('pt-BR', {minimumFractionDigits: 2, maximumFractionDigits: 2});
  const qtyText = (milli, unit = '') => (Number(milli) / 1000).toLocaleString('pt-BR', {minimumFractionDigits: String(unit).toUpperCase() === 'KG' ? 3 : 0, maximumFractionDigits: 3});

  function documentText(value) {
    const digits = String(value ?? '').replace(/\D/g, '');
    if (digits.length === 14) return `${digits.slice(0, 2)}.${digits.slice(2, 5)}.${digits.slice(5, 8)}/${digits.slice(8, 12)}-${digits.slice(12)}`;
    if (digits.length === 11) return `${digits.slice(0, 3)}.${digits.slice(3, 6)}.${digits.slice(6, 9)}-${digits.slice(9)}`;
    return String(value ?? '');
  }
  function parseMoney(raw) {
    let text = String(raw ?? '').trim().replace(/R\$|\s/g, '');
    if (!text) return 0;
    if (text.includes(',')) text = text.replace(/\./g, '').replace(',', '.');
    else if (!/^\d+\.\d{1,2}$/.test(text)) text = text.replace(/\./g, '');
    if (!/^\d+(\.\d{1,2})?$/.test(text)) throw Error('Valor monetário inválido');
    return Math.round(Number(text) * 100);
  }
  function parsePercent(raw) {
    let text = String(raw ?? '').trim().replace('%', '').replace(/\s/g, '');
    if (!text) return 0;
    if (text.includes(',')) text = text.replace(/\./g, '').replace(',', '.');
    if (!/^\d+(\.\d{1,2})?$/.test(text)) throw Error('Percentual inválido');
    const value = Number(text);
    if (value < 0 || value > 100) throw Error('Percentual deve ficar entre 0 e 100');
    return value;
  }
  function parseQty(raw, signed = false) {
    const text = String(raw ?? '').trim().replace(',', '.');
    if (!(signed ? /^[+-]?\d+(\.\d{1,3})?$/ : /^\d+(\.\d{1,3})?$/).test(text)) throw Error('Quantidade inválida: use até três casas decimais');
    return Math.round(Number(text) * 1000);
  }

  // "3*7891234567890" -> 3 units of that code; "3*" alone arms the multiplier for the next reading.
  function parseScanInput(raw) {
    const text = String(raw ?? '').trim();
    const match = text.match(/^(\d{1,4}(?:[.,]\d{1,3})?)\s*\*\s*(.*)$/);
    if (!match) return {quantityMilli: null, code: text};
    const quantityMilli = parseQty(match[1]);
    if (quantityMilli <= 0) throw Error('Informe uma quantidade maior que zero antes do *');
    return {quantityMilli, code: match[2].trim()};
  }

  function ean13Valid(code) {
    const digits = String(code || '');
    if (!/^\d{13}$/.test(digits)) return false;
    let sum = 0;
    for (let i = 0; i < 12; i++) sum += Number(digits[i]) * (i % 2 ? 3 : 1);
    return (10 - sum % 10) % 10 === Number(digits[12]);
  }
  // In-store scale label: 2 + product scale code (5) + weight in grams or price in cents (5) + check digit.
  function scaleLabel(code) {
    const digits = String(code || '').trim();
    if (!/^2\d{12}$/.test(digits) || !ean13Valid(digits)) return null;
    return {scaleCode: digits.slice(1, 6), payload: Number(digits.slice(6, 11))};
  }
  function scaleLabelQuantity(label, product, mode) {
    if (!label || !product || product.unit !== 'KG') return null;
    let milli = label.payload;
    if (mode === 'price') {
      const price = Number(product.price_cents);
      if (!label.payload || !price) return null;
      milli = Math.round(label.payload * 1000 / price);
    }
    return Number.isSafeInteger(milli) && milli > 0 ? milli : null;
  }
  function scaleReading(raw) {
    const match = String(raw || '').replace(',', '.').match(/(-?\d+(?:\.\d{1,3})?)\s*(kg|g|gramas?)?/i);
    if (!match) return null;
    let value = Number(match[1]);
    if (!Number.isFinite(value) || value <= 0) return null;
    if (/^g|gram/i.test(match[2] || '')) value /= 1000;
    const milli = Math.round(value * 1000);
    return milli > 0 && milli <= 1000000 ? milli : null;
  }
  // A weight is only taken after it repeats (continuous scales) or after the scale goes quiet (print-key scales).
  function createWeightGate(required = 2) {
    let last = null, count = 0;
    return {
      push(milli) {
        if (milli === last) count += 1;
        else { last = milli; count = 1; }
        return count >= required ? milli : null;
      },
      settle() { return count >= 1 ? last : null; },
      reset() { last = null; count = 0; },
    };
  }
  // The scale never writes to a line by itself: only a KG item explicitly waiting for weight is a target.
  function scaleTarget(cart, awaitingId) {
    if (!awaitingId) return null;
    return (cart || []).find(item => item.id === awaitingId && item.unit === 'KG') || null;
  }

  function discountCents(discount, base) {
    if (!discount || !(base > 0) || !(Number(discount.value) > 0)) return 0;
    const cents = discount.mode === 'value' ? Math.round(discount.value) : Math.round(base * discount.value / 100);
    return Math.max(0, Math.min(cents, base - 1));
  }
  function discountNeedsPin(role, cents, base) {
    return cents > 0 && cents * 10000 > base * (DISCOUNT_BPS[role] || 0);
  }

  // last_sync_error is only rewritten when there is something to send, so it goes stale after an
  // outage. Presence comes from the heartbeat; the sync error only matters while events are queued.
  function syncIndicator(sync, now) {
    sync = sync || {};
    const pending = Number(sync.pending) || 0;
    const review = Number(sync.quarantined_count) || 0;
    const heartbeatError = String(sync.last_heartbeat_error || '');
    const syncError = pending > 0 ? String(sync.last_sync_error || '') : '';
    const lastContact = Math.max(Number(sync.last_sync_at) || 0, Number(sync.last_heartbeat_at) || 0);
    const heartbeatSeen = Number(sync.last_heartbeat_at) > 0;
    let state, label, detail;
    if (heartbeatError || (syncError && !heartbeatSeen)) {
      state = 'offline'; label = 'Offline';
      detail = 'Sem conexão com o servidor da empresa. As vendas continuam sendo gravadas neste caixa. ' + (heartbeatError || syncError);
    } else if (!lastContact) {
      state = 'local'; label = 'Modo local';
      detail = 'Este caixa ainda não registrou contato com o servidor da empresa.';
    } else if (now - lastContact > STALE_CONTACT_MS) {
      state = 'offline'; label = 'Offline';
      detail = `Sem contato com o servidor há ${Math.max(1, Math.round((now - lastContact) / 60000))} min. As vendas continuam sendo gravadas neste caixa.`;
    } else {
      state = 'online'; label = 'Online';
      detail = syncError ? 'Conectado, mas o envio da fila falhou: ' + syncError : 'Conectado ao servidor da empresa.';
    }
    return {state, label, detail, pending, review, syncProblem: state === 'online' && !!syncError,
            queueLabel: `Fila ${pending}`, reviewLabel: review ? `${review} em revisão` : ''};
  }
  function statusTone({loggedIn, sells, canSell, cashOpen, indicator, pendingForMs}) {
    if (canSell === false) return 'danger';
    if (loggedIn && sells && !cashOpen) return 'danger';
    if (indicator && (indicator.state === 'offline' || indicator.syncProblem || indicator.review > 0)) return 'warn';
    if (indicator && indicator.pending > 0 && pendingForMs >= 30000) return 'warn';
    return 'ok';
  }

  // Rebuilding a <select> must not move the choice the person already made.
  function keepSelection(previous, ids, fallback = '') {
    if (previous && ids.includes(previous)) return previous;
    if (fallback && ids.includes(fallback)) return fallback;
    return ids[0] ?? '';
  }

  // Built-in forms keep fixed digits (1 is always cash); the company's own forms follow from 7.
  function paymentOptions(config) {
    const rows = Array.isArray(config) ? config : [];
    const active = kind => rows.some(row => row.kind === kind && row.active);
    const options = PAY_KEYS.filter(option => active(option.method)).map(option => ({...option}));
    let next = 7, own = 1;
    for (const row of rows.filter(row => row.kind === 'other' && row.active)) {
      // 7 to 9 for the first three; Alt+1 to Alt+9 for every one of them, in the same order.
      options.push({key: next <= 9 ? String(next) : '', alt: own <= 9 ? String(own) : '', method: 'other', id: row.id,
                    label: row.name, custom: true, icon: 'wallet'});
      next += 1;
      own += 1;
    }
    return options;
  }
  // "tecla 1 ou Alt+D": what the button title, its aria-label and the help panel say.
  function payShortcutText(option) {
    return [option.key ? `tecla ${option.key}` : '', option.alt ? `Alt+${option.alt}` : ''].filter(Boolean).join(' ou ');
  }
  // Alt+letter or Alt+digit by physical key (event.code), so it works with any layout and on macOS,
  // where Alt changes the character. Returns the option, 'split' or null.
  function payAltOption(code, options) {
    const match = /^(?:Key([A-Z])|Digit([0-9])|Numpad([0-9]))$/.exec(String(code || ''));
    if (!match) return null;
    const wanted = match[1] || match[2] || match[3];
    if (wanted === SPLIT_OPTION.alt) return 'split';
    return (options || []).find(option => option.alt === wanted) || null;
  }
  // Whose cashier is open on this computer, for the screen: closed, mine, paused (mine) or other.
  function shiftView(session) {
    const cash = session && session.cash, me = session && session.operator;
    if (!cash) return {state: 'closed', mine: false};
    const mine = !cash.operator_id || !!(me && cash.operator_id === me.id);
    const owner = cash.operator_name || 'outro operador';
    const paused = cash.paused || null;
    if (!mine) return {state: 'other', mine, owner, since: cash.opened_at, paused: !!paused, pausedSince: paused ? paused.since : null};
    if (paused) return {state: 'paused', mine, owner, since: paused.since, reason: paused.reason || '', pausedBy: paused.operator_name || ''};
    return {state: 'mine', mine, owner, since: cash.opened_at};
  }

  function receiptDimensions(sale) {
    const model = (sale && sale.receipt) || {};
    const width = Number(model.paper_width_mm) === 58 ? 58 : 80;
    return {width, printable: width === 58 ? 48 : 72, columns: width === 58 ? 32 : 48,
            feed: Math.max(20, Math.min(40, Number(model.bottom_feed_mm) || 20))};
  }
  function receiptBlocksMarkup(model, dimensions) {
    return (model.blocks || []).map(block => {
      if (block.kind === 'rule') return '<div class="receipt-rule" aria-hidden="true"></div>';
      if (block.kind === 'qr') {
        const uri = String(block.data_uri || '');
        if (!/^data:image\/(?:svg\+xml|png);base64,[a-zA-Z0-9+/=]+$/.test(uri)) return '';
        const qrWidth = Number(block.width_mm) > 0 ? Math.min(100, Number(block.width_mm) / dimensions.printable * 100) : 45;
        return `<div class="receipt-qr" style="--receipt-qr-width:${qrWidth}%"><img src="${esc(uri)}" alt="QR Code para consulta do comprovante">${block.label ? `<small>${esc(block.label)}</small>` : ''}</div>`;
      }
      if (block.kind !== 'text' || !Array.isArray(block.lines)) return '';
      const classes = ['receipt-text',
        block.align === 'center' ? 'receipt-text-center' : block.align === 'right' ? 'receipt-text-right' : 'receipt-text-left',
        block.bold ? 'receipt-text-bold' : '', block.tall ? 'receipt-text-tall' : '', block.wide ? 'receipt-text-wide' : '',
        block.small ? 'receipt-text-small' : ''].filter(Boolean).join(' ');
      return `<div class="${classes}">${block.lines.map(line => `<div class="receipt-text-line"><span>${esc(line) || '&nbsp;'}</span></div>`).join('')}</div>`;
    }).join('');
  }

  /* ---------- "Recuperar minha loja" and the second-disk copy ---------- */
  const count = value => (Number(value) || 0).toLocaleString('pt-BR');
  const plural = (value, one, many) => `${count(value)} ${Number(value) === 1 ? one : many}`;
  // Which of the four steps the recovery screen shows for the restore status of the terminal.
  function restoreStep(restore) {
    if (!restore || !restore.active) return restore && restore.finished_at ? 'done' : 'start';
    return restore.state === 'awaiting_backup' ? 'backup' : 'progress';
  }
  // Bar, line under it and notice of step 2. `failed` is true when the last request did not even reach this terminal.
  function restoreProgress(restore, failed = false) {
    const info = restore || {};
    const percent = Math.max(0, Math.min(100, Math.round(Number(info.percent) || 0)));
    const importing = info.state === 'importing', second = Number(info.pass) === 2;
    let line = Number(info.total) > 0
      ? `${count(info.applied)} de ${count(info.total)} registros · não desligue o computador`
      : 'Começando… não desligue o computador';
    if (importing) line = 'Enviando as vendas da cópia de segurança · não desligue o computador';
    else if (second) line = `Conferindo de novo com a cópia de segurança · ${line}`;
    let notice = '';
    if (info.paused) notice = info.error || 'A recuperação parou. Toque em “Tentar de novo”.';
    else if (failed) notice = 'Sem resposta deste caixa. Continuamos sozinhos em instantes.';
    else if (info.error) notice = restoreErrorText(info.error);
    return {percent, line, notice, retry: !!info.paused};
  }
  // Why the recovery is not moving, in the shopkeeper's words. Only a missing connection is "Sem internet":
  // a refusal of the cloud never goes away by waiting.
  function restoreErrorText(error) {
    const text = String(error || '');
    if (/transferido/i.test(text)) return 'Este caixa foi transferido para outro computador. Toque em “Desistir da recuperação”.';
    if (/Sem conexão/i.test(text)) return 'Sem internet. Continuamos sozinhos quando a conexão voltar.';
    const reason = text.replace(/^Servidor recusou pedido \(\d+\):\s*/, '').trim() || 'erro';
    return `O servidor da empresa recusou este computador (${reason}). Confira no painel se o terminal não foi revogado ou chame o suporte.`;
  }
  // "1.180 vendas · R$ 9.876,54" of step 4, from the totals checked against the cloud.
  function restoreSummary(restore) {
    const counts = (restore && restore.counts) || {};
    const parts = [`${plural(counts.sales, 'venda', 'vendas')} · ${money(counts.sales_cents)}`];
    if (counts.cash_sessions) parts.push(plural(counts.cash_sessions, 'caixa', 'caixas'));
    if (counts.returns) parts.push(plural(counts.returns, 'devolução', 'devoluções'));
    if (counts.credit_receipts) parts.push(plural(counts.credit_receipts, 'recebimento de fiado', 'recebimentos de fiado'));
    return parts.join(' · ');
  }
  // What came back after reading a backup copy, in one sentence.
  function restoreImportText(result) {
    const imported = Number(result && result.imported) || 0;
    if (imported) return `${plural(imported, 'registro', 'registros')} da cópia de segurança ${imported === 1 ? 'será enviado' : 'serão enviados'} ao servidor.`;
    if (Number(result && result.foreign)) return 'Esta cópia é de outra loja. Nada foi trazido.';
    return 'A cópia não tinha nada além do que o servidor já guardava.';
  }
  // What must stay on the screen after reading a copy that was not whole (a toast would be gone in seconds).
  function restoreImportNotice(result) {
    const warnings = Array.isArray(result && result.warnings) ? result.warnings.filter(Boolean).map(String) : [];
    if (!warnings.length) return '';
    return `Atenção: parte desta cópia de segurança não pôde ser lida. ${warnings.join(' ')} `
      + 'Se houver outra cópia (outro pendrive ou disco), leia-a também antes de continuar. '
      + 'Depois confira as últimas vendas com o computador antigo ou chame o suporte.';
  }
  // Sale panel of the computer that was replaced ("Trocar de computador").
  function transferredBlock(license, pending, now) {
    const info = license || {}, queue = Number(pending) || 0;
    const until = info.send_only_until ? new Date(info.send_only_until).getTime() : NaN;
    const open = !info.send_only_closed && Number.isFinite(until) && until > now;
    const head = 'A loja passou a usar outro computador, e este não vende mais. ';
    if (!queue) return {title: 'Este caixa foi transferido', recheck: false,
      text: head + 'Tudo o que foi vendido aqui já está guardado no servidor da empresa.',
      steps: ['O caixa que estava aberto continua no computador novo: o fechamento é feito lá, não aqui.',
        'Este computador pode ser desligado.']};
    if (open) return {title: 'Este caixa foi transferido', recheck: false,
      text: head + `Ainda faltam ${queue} registro(s) para enviar. Deixe este computador ligado e com internet; `
        + `ele consegue enviar até ${new Date(until).toLocaleDateString('pt-BR')}.`,
      steps: ['O caixa que estava aberto continua no computador novo: o fechamento é feito lá, não aqui.',
        'Quando o indicador mostrar que não há nada aguardando envio, este computador pode ser desligado.']};
    return {title: 'Este caixa foi transferido', recheck: false,
      text: head + `O prazo para este computador enviar terminou e ${queue} registro(s) ficaram só aqui.`,
      steps: ['Não apague nem formate este computador.',
        'No computador novo, use a cópia de segurança (pendrive ou outro disco) em “Recuperar minha loja”, ou chame o suporte.']};
  }
  // Chip of the status bar while the second-disk copy is not protecting the sales (null: nothing to show).
  function mirrorNotice(mirror) {
    const info = mirror || {};
    if (!info.supported || !info.enabled || !['error', 'late', 'no_key'].includes(info.state)) return null;
    const pending = Number(info.pending_events) || 0;
    const label = info.state === 'late' ? 'Cópia atrasada' : info.state === 'no_key' ? 'Cópia aguardando internet' : 'Cópia de segurança parada';
    const detail = [info.message || 'A cópia de segurança em outro disco não está em dia.', info.last_error || '',
      pending ? `${pending} registro(s) só neste computador.` : '', 'As vendas continuam normalmente. Veja em Sistema.'].filter(Boolean).join(' ');
    return {label, detail, tone: info.state === 'error' ? 'danger' : 'warn'};
  }
  // Card "Cópia de segurança em outro disco" of the Sistema tab.
  function mirrorCard(status) {
    const info = status || {};
    if (!info.supported) return {text: info.message || 'Cópia em segundo disco indisponível nesta instalação', tone: 'muted', canChoose: false, canCopy: false, attention: false};
    if (!info.enabled) return {text: 'Desligada. Escolha um pendrive ou outro disco para guardar a cópia.', tone: 'warn', canChoose: true, canCopy: false, attention: true};
    const ok = info.state === 'ok';
    return {text: `${info.message || (ok ? 'Cópia de segurança em dia' : 'Cópia de segurança atrasada')} · ${info.directory || ''}`.replace(/ · $/, ''),
            tone: ok ? 'ok' : info.state === 'late' ? 'warn' : 'danger', canChoose: true, canCopy: info.state !== 'no_key', attention: !ok};
  }

  /* ---------- phase 3: additional cashiers of the shop ---------- */
  const SERVER_DOWN_CODES = ['server_unreachable', 'server_timeout'];
  const STATION_GONE_CODES = ['station_revoked', 'station_unauthorized'];
  // What this computer is to the shop, and what the screen hides on an additional cashier.
  function stationNotice(session) {
    const info = session || {}, station = info.station || {}, connection = info.connection || {}, limit = info.stations_limit || {};
    const isStation = station.is_server === false;
    const shop = connection.shop_name || connection.tenant_name || 'Loja';
    // A shop with a single computer keeps seeing the name it gave the terminal in the panel.
    const several = isStation || Number(limit.limit) > 0 || Number(limit.used) > 0;
    const name = several ? (station.name || (isStation ? 'Caixa adicional' : 'Caixa principal')) : (connection.terminal_name || 'Caixa');
    // Everything here either always fails on an additional cashier (the route is only of the main
    // computer) or talks about the main computer: the copy on another disk, its update, its bootstrap.
    return {isStation, name, place: `${shop} · ${name}`,
            hidden: isStation ? ['backupCard', 'mirrorChooseBtn', 'mirrorSnapshotBtn', 'updateApplyBtn', 'stationsCard', 'exportLink', 'changeConnection', 'newProfileLoginBtn',
              'refreshBootstrap', 'bootstrapBtn', 'mirrorCard', 'updateCard', 'loginMaintenance'] : [],
            version: isStation && typeof station.version === 'string' ? station.version : '',
            operatorHint: isStation ? 'Nenhum operador com PIN está disponível. Cadastre o PIN em Equipe, no painel da empresa; os operadores chegam a este caixa pelo caixa principal.' : '',
            printerHint: isStation ? 'Impressora deste caixa' : ''};
  }
  const clockText = value => { const date = new Date(Number(value)); return `${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}`; };
  // Card "Caixas da loja" of the Sistema tab, from GET /api/stations.
  function stationsCard(data) {
    const info = data || {}, limit = info.limit || {}, lan = info.lan || {}, replica = info.replica || {};
    const total = Number(limit.limit) || 0, used = Number(limit.used) || 0;
    // hub false: the plan may include cashiers, but another terminal is the main one of this shop.
    const notHub = limit.hub === false;
    const NOT_HUB = 'Este computador não é o caixa principal desta loja. No painel, em Terminais, escolha “Tornar principal desta loja” ou adicione os caixas no computador principal.';
    const summary = total > 0 ? `Seu plano inclui ${total} caixa(s) adicional(is); ${used} em uso.`
      : notHub ? NOT_HUB : 'Seu plano não inclui caixas adicionais. Fale com o suporte para ampliar o plano.';
    // Only (major, minor) counts: a correction never changes what the cashiers exchange.
    const compat = value => { const parts = String(value || '').split('.').map(Number); return parts.length >= 2 && parts.slice(0, 2).every(Number.isInteger) ? parts[0] * 100000 + parts[1] : null; };
    const mine = compat(info.version);
    const outdated = station => !station.is_server && !station.revoked_at && station.linked && mine !== null && compat(station.app_version) !== null && compat(station.app_version) < mine;
    const behind = replica.stations || {};
    const rows = (info.stations || []).map(station => {
      const removed = !!station.revoked_at, copy = behind[station.id];
      const seen = station.is_server ? 'Este computador' : removed ? 'Removido' : !station.linked ? 'Precisa ser conectado de novo'
        : station.online ? 'Conectado agora' : station.last_seen_at ? `Visto por último às ${clockText(station.last_seen_at)}` : 'Ainda não conectou';
      const status = outdated(station) ? `${seen} · Versão antiga (${station.app_version}): instale a versão nova neste caixa` : seen;
      return {id: station.id, name: station.name || 'Caixa', isServer: !!station.is_server, removed, status,
              cash: station.cash_open ? 'Caixa aberto' : 'Caixa fechado',
              copy: station.is_server || removed || !copy ? '' : Number(copy.behind) > 0 ? 'Cópia de segurança atrasada' : 'Cópia de segurança em dia',
              canRename: !removed, canRevoke: !station.is_server && !removed, canCloseCash: !station.is_server && !!station.cash_open};
    }).filter(row => !row.removed || row.canCloseCash);
    // A removed cashier gave its place away: only one that lost its link can come back on a full plan.
    const relinkable = (info.stations || []).some(station => !station.is_server && !station.linked && !station.revoked_at);
    const blocked = !info.supported || lan.supported === false ? 'Rede entre caixas indisponível nesta instalação.'
      : total <= 0 ? (notHub ? NOT_HUB : 'O plano desta loja não inclui caixas adicionais.')
      : used >= total && !relinkable ? 'Todos os caixas adicionais do plano já estão em uso. Remova um caixa ou amplie o plano.' : '';
    // The firewall rule covers every network profile, so "Public" is not a problem to report.
    // A caixa adicional that does not keep the copy cannot prove it, so this computer only warns.
    const warning = lan.newer_reported && !lan.superseded ? 'Um caixa adicional avisou que outro computador assumiu como caixa principal desta loja. Pare de vender aqui e chame o suporte antes de continuar.'
      : lan.error ? `A rede entre os caixas não está funcionando: ${lan.error}`
      : lan.firewall === 'missing' ? 'O firewall do Windows deste computador não liberou os outros caixas. Chame o suporte (regra “PDV2 - Servidor da loja”).'
      : (info.stations || []).some(outdated) ? 'Há caixa adicional com versão antiga: ele não poderá assumir se este computador quebrar. Instale a versão nova do PDV2 nele.' : '';
    return {summary, rows, canAdd: !blocked, addBlocked: blocked, warning};
  }
  // "483920" is read out loud as "483 920".
  function enrollCodeText(code) {
    const digits = String(code ?? '').replace(/\D/g, '');
    return digits.length === 6 ? `${digits.slice(0, 3)} ${digits.slice(3)}` : digits;
  }
  // Fixed card shown while the promoted computer has not been regularized with the cloud.
  function promotionCard(promotion) {
    const info = promotion || {};
    if (info.state !== 'awaiting_cloud') return null;
    const pending = Number(info.pending_events) || 0;
    return {text: 'As vendas continuam normais. Para voltar a enviar para a nuvem, abra o painel em Terminais → Trocar de computador e digite aqui a chave da licença e o código.'
      + (pending ? ` ${pending} registro(s) aguardam o envio.` : ''),
            warning: info.previous_server_cash_open ? 'O caixa do computador anterior ficou aberto. Conte o dinheiro e feche em Sistema → Caixas da loja.' : '',
            pending};
  }

  // The id of a sale already sent to the main cashier survives a reload only while the draft that
  // came back is exactly the one that was sent: the repetition then hits the server's idempotency.
  function draftSaleRequest(saved, cart) {
    const id = saved && typeof saved.saleRequest === 'string' ? saved.saleRequest : '';
    const items = saved && Array.isArray(saved.items) ? saved.items : [], now = Array.isArray(cart) ? cart : [];
    if (!id || id.length > 100 || !items.length || items.length !== now.length) return null;
    return items.every((item, index) => item && now[index] && String(item.id) === String(now[index].id)
      && Number(item.quantity_milli) === Number(now[index].quantity_milli)) ? id : null;
  }

  // ---------- NFC-e after the sale (docs/NFCE.md) ----------
  const NFCE_PRINT = ['always', 'ask', 'never'];
  const WHATSAPP_LINK = 'https:' + '//wa.me/';
  // Same rule as shared/nfce/payments.py: split payment prints if any method says "always", asks if any says "ask".
  function nfcePrintDecision(payments, rules) {
    const defaults = {cash: 'ask', pix: 'ask', card: 'always', card_credit: 'always', card_debit: 'always',
      credit_account: 'always', store_credit: 'ask', other: 'ask'};
    const found = (payments || []).map(payment => {
      let rule = payment.method === 'other' && payment.payment_method_id ? (rules || {})[payment.payment_method_id] : null;
      rule = rule || (rules || {})[payment.method] || defaults[payment.method] || 'ask';
      return NFCE_PRINT.includes(rule) ? rule : 'ask';
    });
    if (found.includes('always')) return 'always';
    if (found.includes('ask') || !found.length) return 'ask';
    return 'never';
  }
  // What the screen does once the NFC-e answer arrived: print now, ask, or only tell.
  function nfcePlan(sale) {
    const info = sale?.nfce || {};
    if (!info.enabled && !info.status) return {issue: false, print: 'always'};
    const fiscal = sale.fiscal || {}, status = fiscal.status || info.status || 'pending';
    return {issue: true, status, print: NFCE_PRINT.includes(info.print) ? info.print : 'ask',
            fiscal: ['authorized', 'contingency'].includes(status)};
  }
  function nfceStatusText(sale) {
    const fiscal = sale?.fiscal || {}, status = fiscal.status || sale?.nfce?.status || 'pending';
    const number = fiscal.number ? ` nº ${fiscal.number}` : '';
    const homologation = fiscal.environment === 'homologation' ? ' (homologação, sem valor fiscal)' : '';
    if (status === 'authorized') return `NFC-e${number} autorizada${homologation}.`;
    if (status === 'contingency') return `NFC-e${number} emitida em contingência${homologation}: será enviada à SEFAZ quando a internet voltar (prazo de 24 h).`;
    if (status === 'rejected') return `NFC-e rejeitada${fiscal.error || sale?.nfce?.error ? ': ' + (fiscal.error || sale.nfce.error) : ''}. Corrija o cadastro no painel e reenvie.`;
    if (status === 'cancelled') return `NFC-e${number} cancelada.`;
    return 'NFC-e pendente: será emitida assim que o caixa falar com a nuvem. F9 imprime quando sair.';
  }
  // "Imprimir a nota fiscal?": Enter or S = sim, Esc or N = não.
  function printPromptKey(key) {
    if (key === 'Enter' || key === 's' || key === 'S') return 'yes';
    if (key === 'Escape' || key === 'n' || key === 'N') return 'no';
    return null;
  }
  // Public consultation link only (no WhatsApp integration): wa.me / mailto built from what the customer said.
  function nfceShareLinks(fiscal, contact) {
    const link = fiscal?.qr_url || (fiscal?.url_consulta && fiscal?.access_key ? fiscal.url_consulta : '');
    if (!link) return null;
    const text = `Sua NFC-e${fiscal.number ? ' nº ' + fiscal.number : ''}: ${link}${fiscal.access_key && !fiscal.qr_url ? ' Chave de acesso: ' + fiscal.access_key : ''}`;
    const value = String(contact || '').trim(), result = {text};
    if (/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value)) {
      result.email = `mailto:${encodeURIComponent(value)}?subject=${encodeURIComponent('Sua NFC-e')}&body=${encodeURIComponent(text)}`;
    } else {
      let phone = value.replace(/\D/g, '');
      if (phone.length === 10 || phone.length === 11) phone = '55' + phone;
      // A link the operator chooses to open (not an asset the page loads; the screen stays offline-safe).
      if (/^55\d{10,11}$/.test(phone)) result.whatsapp = WHATSAPP_LINK + phone + "?text=" + encodeURIComponent(text);
    }
    return result.email || result.whatsapp ? result : null;
  }
  // Header chip: NFC-e still waiting for the cloud/SEFAZ (contingency has 24 h).
  function nfceChip(summary) {
    const info = summary || {}, waiting = Number(info.waiting) || 0, overdue = Number(info.overdue) || 0, rejected = Number(info.rejected) || 0;
    if (!waiting && !rejected) return {hidden: true};
    if (overdue) return {hidden: false, state: 'danger', text: `NFC-e: ${overdue} atrasada(s)`,
      title: `${overdue} NFC-e em contingência ou pendente há mais de 20 horas. Ligue a internet do caixa: a SEFAZ exige o envio em até 24 horas.`};
    if (rejected && !waiting) return {hidden: false, state: 'warn', text: `NFC-e: ${rejected} rejeitada(s)`,
      title: 'Corrija o cadastro no painel (Vendas) e reenvie a nota.'};
    return {hidden: false, state: 'warn', text: `NFC-e: ${waiting} aguardando envio`,
      title: 'Notas emitidas em contingência ou pendentes. Serão enviadas automaticamente quando houver internet.'};
  }

  return {nfcePrintDecision, nfcePlan, nfceStatusText, printPromptKey, nfceShareLinks, nfceChip,
          restoreStep, restoreProgress, restoreErrorText, restoreSummary, restoreImportText, restoreImportNotice,
          transferredBlock, mirrorNotice, mirrorCard,
          stationNotice, stationsCard, enrollCodeText, promotionCard, draftSaleRequest, SERVER_DOWN_CODES, STATION_GONE_CODES,
          SHORTCUTS, PAY_KEYS, SPLIT_OPTION, payShortcutText, payAltOption, shiftView, DISCOUNT_BPS, STALE_CONTACT_MS, esc, money, moneyPlain, qtyText, documentText,
          parseMoney, parsePercent, parseQty, parseScanInput, ean13Valid, scaleLabel, scaleLabelQuantity,
          scaleReading, createWeightGate, scaleTarget, discountCents, discountNeedsPin, syncIndicator,
          statusTone, keepSelection, paymentOptions, receiptDimensions, receiptBlocksMarkup};
});
