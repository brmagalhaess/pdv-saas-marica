/* Balança e etiqueta de pesados: funções puras (sem DOM). Carregado pelo navegador
   (window.PDVScaleCore) e pelos testes em node. O código de barras é o MESMO que o caixa lê
   em terminal-core.js (scaleLabel/scaleLabelQuantity) e que terminal/labels.py gera:
   2 + PLU (5) + peso em gramas ou preço em centavos (5) + 0 + dígito verificador. */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.PDVScaleCore = api;
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  const STX = '\x02', ETX = '\x03', ENQ = 0x05;
  // request: bytes enviados para pedir o peso (modo "sob pedido"); null = a balança transmite sozinha.
  const PROTOCOLS = {
    generic: {label: 'Genérico (texto com o peso)', request: null},
    toledo: {label: 'Toledo (Prix e compatíveis)', request: [ENQ]},
    filizola: {label: 'Filizola / Urano', request: [ENQ]},
  };
  const LABEL_SIZES = {'40x30': [40, 30], '50x30': [50, 30], '60x40': [60, 40]};
  const MAX_PAYLOAD = 99999;

  /* ---------- leitura da balança ---------- */
  function genericLine(line) {
    const match = String(line || '').replace(',', '.').match(/(-?\d+(?:\.\d{1,3})?)\s*(kg|g|gramas?)?/i);
    if (!match) return null;
    let value = Number(match[1]);
    if (!Number.isFinite(value)) return null;
    if (/^g|gram/i.test(match[2] || '')) value /= 1000;
    const milli = Math.round(value * 1000);
    if (milli < 0) return {milli: 0, stable: null, status: 'negative'};
    if (milli === 0) return {milli: 0, stable: null, status: 'zero'};
    return milli <= 1000000 ? {milli, stable: null, status: 'ok'} : {milli: 0, stable: null, status: 'overload'};
  }
  // Quadro STX ... ETX das balanças Toledo/Filizola: 5 ou 6 dígitos em gramas (3 casas implícitas),
  // ou o peso com vírgula/ponto; IIIII = instável, NNNNN = negativo, SSSSS = sobrecarga.
  function frameReading(content) {
    const text = String(content || '').trim();
    if (!text) return null;
    if (/^I+$/i.test(text)) return {milli: 0, stable: false, status: 'unstable'};
    if (/^N+$/i.test(text)) return {milli: 0, stable: false, status: 'negative'};
    if (/^S+$/i.test(text)) return {milli: 0, stable: false, status: 'overload'};
    let milli = null;
    if (/^\d{5,6}$/.test(text)) milli = Number(text);
    else if (/^\d{1,3}[.,]\d{3}$/.test(text)) milli = Math.round(Number(text.replace(',', '.')) * 1000);
    else {
      const reading = genericLine(text);
      return reading ? {...reading, stable: reading.status === 'ok' ? true : reading.stable} : null;
    }
    if (milli === 0) return {milli: 0, stable: true, status: 'zero'};
    return {milli, stable: true, status: 'ok'};
  }
  // Recebe pedaços de texto da porta serial e devolve as leituras completas.
  function createScaleParser(protocol = 'generic') {
    let buffer = '';
    const framed = protocol === 'toledo' || protocol === 'filizola';
    return {
      push(chunk) {
        buffer = (buffer + String(chunk || '')).slice(-512);
        const readings = [];
        if (framed) {
          // Quadros STX...ETX primeiro; o que vier em linhas (Urano, modo texto) também vale.
          for (;;) {
            const start = buffer.indexOf(STX);
            if (start < 0) break;
            const end = buffer.indexOf(ETX, start + 1);
            if (end < 0) { buffer = buffer.slice(start); return readings; }
            const reading = frameReading(buffer.slice(start + 1, end));
            if (reading) readings.push(reading);
            buffer = buffer.slice(end + 1);
          }
        }
        const lines = buffer.split(/[\r\n]+/);
        buffer = lines.pop() || '';
        for (const line of lines) {
          const reading = framed ? frameReading(line) : genericLine(line);
          if (reading) readings.push(reading);
        }
        return readings;
      },
      reset() { buffer = ''; },
    };
  }
  function requestBytes(protocol, continuous) {
    const spec = PROTOCOLS[protocol] || PROTOCOLS.generic;
    return continuous || !spec.request ? null : Uint8Array.from(spec.request);
  }
  // Peso estável: o protocolo disse que é (Toledo/Filizola) ou o mesmo valor repetiu (genérico).
  function createStability(required = 2) {
    let last = null, count = 0;
    return {
      push(reading) {
        if (!reading || reading.status !== 'ok') { last = null; count = 0; return false; }
        if (reading.milli === last) count += 1; else { last = reading.milli; count = 1; }
        return reading.stable === true || count >= required;
      },
      reset() { last = null; count = 0; },
    };
  }

  /* ---------- EAN-13 de pesados ---------- */
  function ean13CheckDigit(digits12) {
    const text = String(digits12);
    if (!/^\d{12}$/.test(text)) throw Error('O código precisa de 12 dígitos');
    let sum = 0;
    for (let i = 0; i < 12; i++) sum += Number(text[i]) * (i % 2 ? 3 : 1);
    return String((10 - sum % 10) % 10);
  }
  function plu5(scaleCode) {
    const text = String(scaleCode ?? '').trim();
    if (!text) throw Error('Este produto não tem código de balança (PLU). Cadastre no painel, em Produtos.');
    if (!/^\d+$/.test(text) || Number(text) <= 0 || Number(text) > MAX_PAYLOAD) throw Error('O código de balança precisa ser um número de 1 a 99999. Corrija no painel, em Produtos.');
    return String(Number(text)).padStart(5, '0');
  }
  // Mesmo arredondamento da venda: meio centavo para cima (valores inteiros, sem ponto flutuante).
  const lineTotal = (priceCents, weightMilli) => Math.floor((Number(priceCents) * Number(weightMilli) + 500) / 1000);
  function weighedBarcode(scaleCode, priceCents, weightMilli, mode = 'weight') {
    if (!Number.isSafeInteger(weightMilli) || weightMilli <= 0) throw Error('Peso inválido: coloque o produto na balança');
    if (weightMilli > MAX_PAYLOAD) throw Error('Peso acima de 99,999 kg não cabe no código da etiqueta');
    if (!Number.isSafeInteger(priceCents) || priceCents <= 0) throw Error('Produto sem preço por quilo');
    const total = lineTotal(priceCents, weightMilli);
    let payload = weightMilli;
    if (mode === 'price') {
      if (total <= 0) throw Error('Valor zero: o peso é pequeno demais para este preço');
      if (total > MAX_PAYLOAD) throw Error('Valor acima de R$ 999,99 não cabe na etiqueta no modo preço');
      payload = total;
    }
    const body = '2' + plu5(scaleCode) + String(payload).padStart(5, '0') + '0';
    return {barcode: body + ean13CheckDigit(body), payload, total_cents: total};
  }
  const EAN_A = ['0001101', '0011001', '0010011', '0111101', '0100011', '0110001', '0101111', '0111011', '0110111', '0001011'];
  const invert = bits => bits.replace(/[01]/g, bit => (bit === '0' ? '1' : '0'));
  const EAN_C = EAN_A.map(invert), EAN_B = EAN_C.map(bits => bits.split('').reverse().join(''));
  const EAN_PARITY = ['AAAAAA', 'AABABB', 'AABBAB', 'AABBBA', 'ABAABB', 'ABBAAB', 'ABBBAA', 'ABABAB', 'ABABBA', 'ABBABA'];
  function ean13Modules(code) {
    const text = String(code || '');
    if (!/^\d{13}$/.test(text) || ean13CheckDigit(text.slice(0, 12)) !== text[12]) throw Error('EAN-13 inválido');
    const parity = EAN_PARITY[Number(text[0])];
    let left = '';
    for (let i = 1; i <= 6; i++) left += (parity[i - 1] === 'A' ? EAN_A : EAN_B)[Number(text[i])];
    let right = '';
    for (let i = 7; i <= 12; i++) right += EAN_C[Number(text[i])];
    return '101' + left + '01010' + right + '101';
  }
  // SVG em milímetros: módulo de 0,33 mm (tamanho nominal do EAN-13 a 100%) reduzido para caber.
  function ean13Svg(code, {module = 0.3, height = 12} = {}) {
    const bits = ean13Modules(code), quiet = 9, total = (95 + quiet * 2) * module, text = 2.6;
    const guard = new Set([0, 1, 2, 45, 46, 47, 48, 49, 92, 93, 94]);
    let bars = '';
    for (let i = 0; i < bits.length; i++) {
      if (bits[i] !== '1') continue;
      const h = guard.has(i) ? height : height - text * 0.8;
      bars += `<rect x="${((quiet + i) * module).toFixed(3)}" y="0" width="${module.toFixed(3)}" height="${h.toFixed(2)}"/>`;
    }
    const digit = (value, x) => `<text x="${x.toFixed(2)}" y="${(height + 0.4).toFixed(2)}" text-anchor="middle">${value}</text>`;
    let digits = digit(code[0], (quiet - 4) * module);
    for (let i = 1; i <= 6; i++) digits += digit(code[i], (quiet + 3 + (i - 0.5) * 7) * module);
    for (let i = 7; i <= 12; i++) digits += digit(code[i], (quiet + 50 + (i - 6.5) * 7) * module);
    return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${total.toFixed(2)} ${(height + 0.6).toFixed(2)}" width="${total.toFixed(2)}mm" height="${(height + 0.6).toFixed(2)}mm" role="img" aria-label="Código de barras ${code}"><g fill="#000">${bars}</g><g font-family="monospace" font-size="${text}" fill="#000">${digits}</g></svg>`;
  }

  /* ---------- textos e etiqueta HTML (pré-visualização e impressão pelo navegador) ---------- */
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
  const amount = cents => {
    const value = Math.abs(Math.trunc(Number(cents) || 0));
    const reais = String(Math.floor(value / 100)).replace(/\B(?=(\d{3})+(?!\d))/g, '.');
    return (Number(cents) < 0 ? '-' : '') + reais + ',' + String(value % 100).padStart(2, '0');
  };
  const moneyText = cents => 'R$ ' + amount(cents);
  const weightText = milli => `${Math.floor(milli / 1000)},${String(milli % 1000).padStart(3, '0')} kg`;
  const pad = value => String(value).padStart(2, '0');
  const dateText = date => `${pad(date.getDate())}/${pad(date.getMonth() + 1)}/${date.getFullYear()}`;
  const dateTimeText = date => `${dateText(date)} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
  // Mesmos campos que terminal/labels.py label_fields (o servidor é quem vale na impressão).
  function labelFields(product, weightMilli, {barcodeMode = 'weight', validityDays = 0, storeName = '', now = new Date()} = {}) {
    const code = weighedBarcode(product.scale_code, Number(product.price_cents), weightMilli, barcodeMode);
    let validUntil = '';
    if (validityDays) { const until = new Date(now.getTime()); until.setDate(until.getDate() + Number(validityDays)); validUntil = dateText(until); }
    return {store_name: String(storeName || '').trim().slice(0, 60), product_name: String(product.name || '').trim().slice(0, 80),
            plu: plu5(product.scale_code), weight_milli: weightMilli, price_cents: Number(product.price_cents), total_cents: code.total_cents,
            barcode: code.barcode, barcode_mode: barcodeMode, weight_text: weightText(weightMilli),
            unit_price_text: moneyText(product.price_cents) + '/kg', total_text: moneyText(code.total_cents),
            packed_at: dateTimeText(now), valid_until: validUntil, validity_days: Number(validityDays) || 0};
  }
  function labelHtml(fields, size = '50x30') {
    const [width, height] = LABEL_SIZES[size] || LABEL_SIZES['50x30'];
    const tall = height >= 40, barHeight = tall ? 13 : 8.5, module = width <= 40 ? 0.28 : 0.32;
    return `<div class="wlabel" style="width:${width}mm;height:${height}mm">
      ${fields.store_name ? `<div class="wl-store">${esc(fields.store_name)}</div>` : ''}
      <div class="wl-name">${esc(fields.product_name)}</div>
      <div class="wl-line"><span>${esc(fields.weight_text)}</span><span>${esc(fields.unit_price_text)}</span></div>
      <div class="wl-total"><small>Total</small> ${esc(fields.total_text)}</div>
      <div class="wl-dates"><span>Emb ${esc(fields.packed_at)}</span>${fields.valid_until ? `<span>Val ${esc(fields.valid_until)}</span>` : ''}</div>
      <div class="wl-bar">${ean13Svg(fields.barcode, {module, height: barHeight})}</div>
    </div>`;
  }
  const LABEL_CSS = `.wlabel{box-sizing:border-box;display:flex;flex-direction:column;gap:.3mm;padding:1.2mm 1.5mm;overflow:hidden;background:#fff;color:#000;font:600 2.6mm/1.15 Arial,Helvetica,sans-serif}
    .wl-store{font-size:2.2mm;font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
    .wl-name{font-size:3.1mm;font-weight:800;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
    .wl-line,.wl-dates{display:flex;justify-content:space-between;gap:1mm;white-space:nowrap}
    .wl-dates{font-size:2.1mm;font-weight:500}
    .wl-total{font-size:4.4mm;font-weight:900}.wl-total small{font-size:2.4mm;font-weight:700}
    .wl-bar{margin-top:auto;display:flex;justify-content:center}.wl-bar svg{display:block;max-width:100%;height:auto}`;
  // Documento completo para imprimir pelo navegador: uma página por etiqueta, no tamanho dela.
  function printDocument(fields, size = '50x30', copies = 1) {
    const [width, height] = LABEL_SIZES[size] || LABEL_SIZES['50x30'];
    const pages = Array.from({length: Math.max(1, Math.min(50, Number(copies) || 1))}, () => labelHtml(fields, size)).join('');
    return `<!doctype html><html lang="pt-BR"><head><meta charset="utf-8"><title>Etiqueta ${esc(fields.barcode)}</title><style>
      @page{size:${width}mm ${height}mm;margin:0}html,body{margin:0;padding:0;background:#fff}
      .wlabel{page-break-after:always;break-after:page}${LABEL_CSS}</style></head><body>${pages}</body></html>`;
  }

  return {PROTOCOLS, LABEL_SIZES, MAX_PAYLOAD, genericLine, frameReading, createScaleParser, requestBytes, createStability,
          ean13CheckDigit, plu5, lineTotal, weighedBarcode, ean13Modules, ean13Svg, moneyText, weightText,
          labelFields, labelHtml, printDocument, LABEL_CSS};
});
