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
    {key: 'F12', action: 'closeCash', label: 'Fechar caixa'},
    {key: 'Escape', cap: 'Esc', action: 'back', label: 'Voltar'},
  ];
  const PAY_KEYS = [
    {key: '1', method: 'cash', label: 'Dinheiro'},
    {key: '2', method: 'pix', label: 'Pix'},
    {key: '3', method: 'card_debit', label: 'Débito'},
    {key: '4', method: 'card_credit', label: 'Crédito'},
    {key: '5', method: 'credit_account', label: 'A prazo (fiado)', short: 'A prazo'},
    {key: '6', method: 'store_credit', label: 'Crédito na loja', short: 'Crédito loja'},
  ];
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
    let next = 7;
    for (const row of rows.filter(row => row.kind === 'other' && row.active)) {
      options.push({key: next <= 9 ? String(next) : '', method: 'other', id: row.id, label: row.name, custom: true});
      next += 1;
    }
    return options;
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
    else if (info.error) notice = /transferido/i.test(info.error) ? info.error
      : 'Sem internet. Continuamos sozinhos quando a conexão voltar.';
    return {percent, line, notice, retry: !!info.paused};
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
  // Card "Cópia de segurança em outro disco" of the Sistema tab.
  function mirrorCard(status) {
    const info = status || {};
    if (!info.supported) return {text: info.message || 'Cópia em segundo disco indisponível nesta instalação', tone: 'muted', canChoose: false, canCopy: false, attention: false};
    if (!info.enabled) return {text: 'Desligada. Escolha um pendrive ou outro disco para guardar a cópia.', tone: 'warn', canChoose: true, canCopy: false, attention: true};
    const ok = info.state === 'ok';
    return {text: `${info.message || (ok ? 'Cópia de segurança em dia' : 'Cópia de segurança atrasada')} · ${info.directory || ''}`.replace(/ · $/, ''),
            tone: ok ? 'ok' : info.state === 'late' ? 'warn' : 'danger', canChoose: true, canCopy: info.state !== 'no_key', attention: !ok};
  }

  return {restoreStep, restoreProgress, restoreSummary, restoreImportText, mirrorCard,
          SHORTCUTS, PAY_KEYS, DISCOUNT_BPS, STALE_CONTACT_MS, esc, money, moneyPlain, qtyText, documentText,
          parseMoney, parsePercent, parseQty, parseScanInput, ean13Valid, scaleLabel, scaleLabelQuantity,
          scaleReading, createWeightGate, scaleTarget, discountCents, discountNeedsPin, syncIndicator,
          statusTone, keepSelection, paymentOptions, receiptDimensions, receiptBlocksMarkup};
});
