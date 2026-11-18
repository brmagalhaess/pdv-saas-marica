'use strict';
/* Tela Balança: escolher o produto por quilo, ler o peso, imprimir a etiqueta de pesado.
   Carregado ANTES de terminal.js: aqui só se definem funções; os nomes de terminal.js
   (state, api, $, toast, openModal…) são usados quando a tela já está de pé. */
(function () {
  const S = window.PDVScaleCore;
  const scale = {
    loaded: false, config: null, products: [], favorites: true, selected: null, searchTimer: null, searchSeq: 0,
    reading: null, stable: false, stability: S.createStability(2), tareMilli: 0, lastPrinted: null, cleared: true,
    staleTimer: null, printing: false,
  };
  const node = id => document.getElementById(id);
  const permissions = () => scale.config?.permissions || {manage: false, manual_weight: false};
  const settingsNow = () => scale.config?.settings || {barcode_mode: 'weight', validity_days: 3, manual_weight: 'managers', show_store_name: true};

  /* ---------- configuração ---------- */
  async function fetchConfig() {
    scale.config = await api('/api/scale/settings');
    const mode = scale.config.settings.barcode_mode;
    // O caixa lê a etiqueta no mesmo formato em que a balança imprime.
    state.scale.barcodeMode = mode;
    prefs.set('scale.barcodeMode', mode);
    state.labelPrinter = scale.config.printer;
    return scale.config;
  }
  async function onSession() {
    if (!canWeigh(role())) { state.labelPrinter = null; return; }
    try { await fetchConfig(); } catch { /* sem a configuração o caixa segue com o modo salvo */ }
  }
  async function saveBarcodeMode(mode) {
    scale.config = {...(await api('/api/scale/settings', 'POST', {barcode_mode: mode})), permissions: scale.config?.permissions || permissions()};
    state.scale.barcodeMode = mode;
    prefs.set('scale.barcodeMode', mode);
    toast(mode === 'price' ? 'Etiquetas de balança com o preço total' : 'Etiquetas de balança com o peso em gramas');
    renderAll();
  }

  /* ---------- produtos ---------- */
  async function searchProducts(query) {
    const ticket = ++scale.searchSeq;
    const data = await api('/api/scale/products?q=' + encodeURIComponent(query));
    if (ticket !== scale.searchSeq) return null;
    scale.products = data.products || [];
    scale.favorites = !!data.favorites;
    node('scaleListTitle').textContent = data.favorites ? (data.total ? 'Mais usados — toque para escolher' : '')
      : data.total > scale.products.length ? `Mostrando ${scale.products.length} de ${data.total}. Refine a busca.` : `${data.total} produto(s) por quilo`;
    renderProducts();
    return scale.products;
  }
  function renderProducts() {
    const list = node('scaleProducts');
    if (!scale.products.length) {
      list.innerHTML = `<p class="list-empty">${scale.favorites ? 'Nenhum produto por quilo (KG) no catálogo. Cadastre no painel, em Produtos, com a unidade KG e o código de balança (PLU).' : 'Nenhum produto por quilo com esse nome ou código.'}</p>`;
      return;
    }
    list.innerHTML = scale.products.map(product => `<button type="button" class="scale-tile${scale.selected?.id === product.id ? ' on' : ''}${product.scale_code ? '' : ' no-plu'}" data-product="${esc(product.id)}" role="listitem">
      <strong>${esc(product.name)}</strong>
      <span>${product.scale_code ? 'PLU ' + esc(product.scale_code) : 'Sem PLU'}</span>
      <b>${money(product.price_cents)}/kg</b></button>`).join('');
  }
  function selectProduct(product) {
    scale.selected = product;
    scale.lastPrinted = null;
    node('scaleCopies').value = '1';
    renderProducts();
    renderAll();
    if (!product.scale_code) explainMissingPlu(product).catch(() => {});
  }
  async function explainMissingPlu(product) {
    // Nunca inventa PLU: o cadastro é do painel. O gerente vê qual número está livre.
    let text = `“${product.name}” não tem código de balança (PLU), então o caixa não conseguiria ler a etiqueta. Cadastre no painel, em Produtos → editar → Código de balança, e atualize o catálogo.`;
    if (permissions().manage) {
      try { const free = await api('/api/scale/next-plu'); text += ` Próximo PLU livre neste catálogo: ${free.plu}.`; } catch { /* sugestão é opcional */ }
    } else text += ' Peça ao gerente.';
    setMessage(text, 'warn');
  }

  /* ---------- peso ---------- */
  function onReading(reading) {
    clearTimeout(scale.staleTimer);
    if (!reading) { scale.reading = null; scale.stable = false; scale.stability.reset(); renderWeight(); return; }
    scale.reading = reading;
    scale.stable = scale.stability.push(reading);
    if (reading.status === 'zero' || (reading.status === 'ok' && reading.milli <= scale.tareMilli)) scale.cleared = true;
    // Sem novas leituras por 2 s: o peso mostrado deixa de valer.
    scale.staleTimer = setTimeout(() => { scale.reading = null; scale.stable = false; renderWeight(); }, 2000);
    if (state.tab === 'scale') renderWeight();
  }
  function manualMilli() {
    const raw = node('scaleManual').value.trim();
    if (!raw) return null;
    try { const milli = parseQty(raw); return milli > 0 ? milli : null; } catch { return null; }
  }
  // Peso que vai para a etiqueta: líquido da balança (estável) ou o digitado.
  function currentWeight() {
    if (state.scale.port) {
      const reading = scale.reading;
      if (!reading || reading.status !== 'ok') return {milli: null, manual: false};
      const net = reading.milli - scale.tareMilli;
      return {milli: net > 0 ? net : null, manual: false, stable: scale.stable};
    }
    return {milli: manualMilli(), manual: true, stable: true};
  }
  function weightStatus() {
    if (!state.scale.port) return ['Sem balança · peso digitado', ''];
    const reading = scale.reading;
    if (!reading) return ['Aguardando a balança…', 'warn'];
    return {unstable: ['Instável', 'warn'], negative: ['Peso negativo', 'bad'], overload: ['Sobrecarga', 'bad'], zero: ['Prato vazio', '']}[reading.status]
      || (scale.stable ? ['Estável', 'ok'] : ['Estabilizando…', 'warn']);
  }
  function renderWeight() {
    const connected = !!state.scale.port, [label, tone] = weightStatus();
    const status = node('scaleStatus');
    status.textContent = label;
    status.className = 'badge ' + tone;
    const gross = connected && scale.reading?.status === 'ok' ? scale.reading.milli : 0;
    const shown = connected ? Math.max(0, gross - scale.tareMilli) : (manualMilli() || 0);
    node('scaleWeight').innerHTML = `${esc(qtyText(shown, 'KG'))}<small> kg</small>`;
    node('scaleWeight').dataset.stable = String(connected ? scale.stable : true);
    node('scaleTareInfo').textContent = scale.tareMilli ? `Tara ${qtyText(scale.tareMilli, 'KG')} kg descontada` : (connected ? 'Para zerar, use a tecla da própria balança' : '');
    node('scaleConnectBtn').textContent = connected ? 'Desconectar balança' : 'Conectar balança';
    node('scaleTareBtn').textContent = scale.tareMilli ? 'Tirar tara' : 'Tarar';
    node('scaleTareBtn').disabled = !connected;
    const manualField = node('scaleManualField');
    manualField.classList.toggle('hidden', connected);
    node('scaleManualLabel').textContent = permissions().manual_weight ? 'Peso digitado (kg)' : 'Peso digitado (kg) · pede PIN do gerente';
    renderTotal();
  }
  function renderTotal() {
    const product = scale.selected, {milli, stable} = currentWeight();
    const printButton = node('scalePrintBtn');
    let fields = null, problem = '';
    if (product && milli) {
      try {
        fields = S.labelFields(product, milli, {barcodeMode: settingsNow().barcode_mode,
          validityDays: product.validity_days ?? settingsNow().validity_days,
          storeName: settingsNow().show_store_name ? scale.config?.store_name : ''});
      } catch (error) { problem = error.message; }
    }
    node('scaleTotal').textContent = product && milli ? money(S.lineTotal(product.price_cents, milli)) : 'R$ 0,00';
    node('scaleUnitPrice').textContent = product ? `${money(product.price_cents)}/kg` : '';
    printButton.disabled = !fields || !stable || scale.printing;
    printButton.title = !product ? 'Escolha um produto' : !milli ? 'Coloque o produto na balança' : problem || (!stable ? 'Aguarde o peso estabilizar' : 'Imprimir etiqueta (Enter)');
    node('scalePreview').innerHTML = fields ? S.labelHtml(fields, scale.config?.size || '50x30') : '';
    node('scalePreviewTitle').textContent = fields ? `Pré-visualização · ${(scale.config?.size || '50x30').replace('x', ' × ')} mm · ${scale.config?.printer ? 'etiquetadora ' + scale.config.printer.format.toUpperCase() : 'impressão pelo navegador'}` : 'Pré-visualização';
    if (problem && product?.scale_code) setMessage(problem, 'bad');
  }
  function renderProduct() {
    const product = scale.selected, box = node('scaleProduct');
    if (!product) { box.className = 'scale-product empty'; box.innerHTML = '<strong>Escolha um produto</strong><span>Busque pelo nome, PLU ou leia o código. Os mais usados aparecem ao lado.</span>'; return; }
    box.className = 'scale-product';
    const validity = product.validity_days ?? settingsNow().validity_days;
    box.innerHTML = `<strong>${esc(product.name)}</strong><span>${product.scale_code ? 'PLU ' + esc(product.scale_code) : '<b class="warn-text">Sem PLU</b>'} · ${money(product.price_cents)}/kg · validade ${validity ? validity + ' dia(s)' : 'não impressa'}${product.validity_days != null ? ' (deste produto)' : ''}</span>`;
  }
  function renderAll() {
    if (!node('scaleTab') || state.tab !== 'scale') return;
    renderProduct();
    renderWeight();
    node('scaleHistoryCard').classList.toggle('hidden', !permissions().manage);
  }
  function setMessage(text, tone = '') {
    const box = node('scaleMessage');
    box.textContent = text || '';
    box.dataset.tone = tone;
  }

  /* ---------- impressão ---------- */
  function copies() {
    const value = Number(node('scaleCopies').value);
    return Number.isInteger(value) && value >= 1 && value <= 50 ? value : null;
  }
  function printInBrowser(fields, size, count) {
    const frame = document.createElement('iframe');
    frame.className = 'scale-print-frame';
    frame.setAttribute('aria-hidden', 'true');
    document.body.append(frame);
    const doc = frame.contentDocument;
    doc.open();
    doc.write(S.printDocument(fields, size, count));
    doc.close();
    setTimeout(() => {
      try { frame.contentWindow.focus(); frame.contentWindow.print(); } finally { setTimeout(() => frame.remove(), 60000); }
    }, 150);
  }
  async function printLabel() {
    const product = scale.selected, {milli, manual, stable} = currentWeight(), count = copies();
    if (scale.printing) return;
    if (!product) throw Error('Escolha um produto antes de imprimir');
    if (!product.scale_code) { await explainMissingPlu(product); throw Error('Produto sem código de balança (PLU)'); }
    if (!milli) throw Error(state.scale.port ? 'Coloque o produto na balança' : 'Digite o peso em kg');
    if (!stable) throw Error('Aguarde o peso estabilizar');
    if (!count) throw Error('Cópias: de 1 a 50');
    const request = {product_id: product.id, weight_milli: milli, copies: count, manual, output: scale.config?.printer ? 'auto' : 'browser'};
    if (manual && !permissions().manual_weight) {
      const pin = await pinDialog('Autorizar peso digitado', `Sem balança conectada, o peso digitado (${qtyText(milli, 'KG')} kg) precisa do PIN de um gerente.`);
      if (!pin) return;
      request.manager_pin = pin;
    }
    scale.printing = true;
    renderTotal();
    try {
      const result = await api('/api/scale/labels', 'POST', request);
      scale.lastPrinted = milli;
      scale.cleared = false;
      beep('ok');
      if (result.output === 'printer') {
        setMessage(`Etiqueta enviada para a etiquetadora: ${result.label.product_name} · ${result.label.weight_text} · ${result.label.total_text}${count > 1 ? ` · ${count} cópias` : ''}.`, 'ok');
        api('/api/print/flush', 'POST', {}).catch(() => {});
      } else {
        setMessage(`Etiqueta pronta: ${result.label.weight_text} · ${result.label.total_text}. Escolha a impressora de etiquetas na janela de impressão.`, 'ok');
        printInBrowser(result.label, result.size, result.copies);
      }
      if (manual) node('scaleManual').value = '';
      if (permissions().manage) loadHistory().catch(() => {});
    } finally {
      scale.printing = false;
      renderWeight();
    }
  }
  // Enter só imprime de novo depois que o prato esvaziou ou o peso mudou (evita etiqueta dobrada).
  function enterMayPrint() {
    const {milli} = currentWeight();
    return scale.lastPrinted === null || scale.cleared || milli !== scale.lastPrinted || !state.scale.port;
  }
  async function run(task) {
    try { await task(); } catch (error) { setMessage(error.message, 'bad'); beep('error'); }
  }

  /* ---------- ajustes ---------- */
  async function settingsDialog() {
    const manager = permissions().manage, current = settingsNow(), product = scale.selected;
    const modal = openModal({title: 'Ajustes da balança e da etiqueta', eyebrow: 'Balança', className: 'wide',
      text: manager ? 'O formato do código vale para a loja toda: o caixa lê a etiqueta do mesmo jeito.' : 'Somente o gerente muda o formato da etiqueta.'});
    const option = (value, label, selected) => `<option value="${value}"${selected ? ' selected' : ''}>${label}</option>`;
    modal.body.innerHTML = `<div class="scale-settings">
      <label class="field"><span>Código de barras da etiqueta</span><select data-field="barcode_mode" ${manager ? '' : 'disabled'}>${option('weight', 'Peso em gramas (2 + PLU + peso)', current.barcode_mode === 'weight')}${option('price', 'Preço total em centavos (2 + PLU + valor)', current.barcode_mode === 'price')}</select></label>
      <label class="field"><span>Validade padrão da loja (dias, 0 = não imprimir)</span><input data-field="validity_days" type="number" min="0" max="365" value="${current.validity_days}" ${manager ? '' : 'disabled'}></label>
      ${product ? `<label class="field"><span>Validade de “${esc(product.name)}” (vazio = padrão da loja)</span><input data-field="product_validity" type="number" min="0" max="365" value="${product.validity_days ?? ''}" ${manager ? '' : 'disabled'}></label>` : ''}
      <label class="field"><span>Peso digitado sem balança</span><select data-field="manual_weight" ${manager ? '' : 'disabled'}>${option('managers', 'Só gerente (outros pedem o PIN)', current.manual_weight === 'managers')}${option('everyone', 'Qualquer operador da balança', current.manual_weight === 'everyone')}</select></label>
      <label class="field"><span>Nome da loja na etiqueta</span><select data-field="show_store_name" ${manager ? '' : 'disabled'}>${option('yes', 'Imprimir' + (scale.config?.store_name ? ` (${esc(scale.config.store_name)})` : ''), current.show_store_name)}${option('no', 'Não imprimir', !current.show_store_name)}</select></label>
      <label class="field"><span>Tamanho da etiqueta neste computador</span><select data-field="size" ${manager ? '' : 'disabled'}>${(scale.config?.sizes || ['40x30', '50x30', '60x40']).map(size => option(size, size.replace('x', ' × ') + ' mm', size === scale.config?.size)).join('')}</select></label>
      <label class="field"><span>Ao entrar neste computador</span><select data-field="home">${option('off', 'Abrir no Caixa', prefs.get('scale.home', 'off') !== 'on')}${option('on', 'Abrir na Balança (balcão)', prefs.get('scale.home', 'off') === 'on')}</select></label>
    </div><p class="hint">Conexão, protocolo (Toledo, Filizola/Urano) e velocidade da balança ficam em “Balança e etiquetas”.</p>`;
    const field = name => modal.body.querySelector(`[data-field="${name}"]`);
    modal.action('Cancelar', '', () => modal.close());
    modal.action('Balança e etiquetas…', '', async () => { modal.close(); await runAction('scale'); renderAll(); });
    modal.action('Salvar', 'primary', async () => {
      prefs.set('scale.home', field('home').value);
      if (manager) {
        const days = Number(field('validity_days').value);
        if (!Number.isInteger(days) || days < 0 || days > 365) throw Error('Validade: de 0 a 365 dias');
        scale.config = {...(await api('/api/scale/settings', 'POST', {barcode_mode: field('barcode_mode').value, validity_days: days,
          manual_weight: field('manual_weight').value, show_store_name: field('show_store_name').value === 'yes', size: field('size').value})),
          permissions: permissions()};
        state.scale.barcodeMode = scale.config.settings.barcode_mode;
        prefs.set('scale.barcodeMode', state.scale.barcodeMode);
        if (product && field('product_validity')) {
          const raw = field('product_validity').value.trim(), value = raw === '' ? null : Number(raw);
          if (value !== null && (!Number.isInteger(value) || value < 0 || value > 365)) throw Error('Validade do produto: de 0 a 365 dias');
          if (value !== (product.validity_days ?? null)) {
            await api('/api/scale/products/' + encodeURIComponent(product.id) + '/validity', 'POST', {validity_days: value});
            product.validity_days = value;
          }
        }
      }
      modal.close();
      toast('Ajustes da balança salvos');
      renderAll();
    });
    await modal.promise;
  }

  /* ---------- conferência (gerente) ---------- */
  async function loadHistory() {
    const data = await api('/api/scale/labels');
    node('scaleHistory').innerHTML = (data.labels || []).length ? data.labels.map(label => `<div class="list-row"><span><strong>${esc(label.product_name)} · ${esc(qtyText(label.weight_milli, 'KG'))} kg · ${money(label.total_cents)}</strong>
      <small>${dateTimeText(label.created_at)} · ${esc(label.operator_name || 'operador')} · ${esc(label.barcode)}${label.copies > 1 ? ` · ${label.copies} cópias` : ''}${label.manual ? ' · peso digitado' : ''} · ${label.output === 'printer' ? 'etiquetadora' : 'navegador'}</small></span></div>`).join('')
      : '<p class="list-empty">Nenhuma etiqueta impressa ainda.</p>';
  }

  /* ---------- impressora de etiquetas (aba Impressoras) ---------- */
  async function loadLabelPrinter() {
    const [config, data] = await Promise.all([fetchConfig(), api('/api/print/settings')]);
    const setting = (data.settings || []).find(row => row.kind === 'label');
    if (setting) {
      node('labelPrinterDriver').value = setting.driver;
      node('labelPrinterFormat').value = setting.format;
      node('labelPrinterTarget').value = setting.target;
    }
    node('labelPrinterSize').value = config.size;
    node('labelPrinterInfo').innerHTML = setting ? `<div class="list-row"><span><strong>Etiquetadora</strong><small>${esc(driverNames[setting.driver] || setting.driver)} · ${esc(setting.format.toUpperCase())} · ${esc(config.size.replace('x', ' × '))} mm · ${esc(setting.target)}</small></span></div>`
      : '<p class="list-empty">Nenhuma etiquetadora configurada: a Balança imprime pelo navegador.</p>';
    const jobs = (data.jobs || []).filter(job => job.kind === 'label').slice(0, 10);
    node('labelJobs').innerHTML = jobs.map(job => `<div class="list-row"><span><strong>Etiqueta ${badge(jobStatusNames[job.status] || [job.status, ''])}</strong><small>${dateTimeText(job.created_at)}${job.error ? ' · ' + esc(job.error) : ''}</small></span><span class="list-actions">${['uncertain', 'printed'].includes(job.status) ? `<button class="btn" type="button" data-label-retry="${esc(job.id)}">Reenviar</button>` : ''}</span></div>`).join('');
  }
  async function saveLabelPrinter() {
    await api('/api/print/settings', 'POST', {kind: 'label', driver: node('labelPrinterDriver').value, format: node('labelPrinterFormat').value, target: node('labelPrinterTarget').value});
    await api('/api/scale/settings', 'POST', {size: node('labelPrinterSize').value});
    toast('Etiquetadora salva');
    await loadLabelPrinter();
  }

  /* ---------- tela ---------- */
  async function load() {
    if (!scale.loaded) wire();
    scale.loaded = true;
    await fetchConfig();
    renderAll();
    await searchProducts(node('scaleSearch').value.trim());
    if (permissions().manage) loadHistory().catch(() => {});
    if (!modalStack.length) node('scaleSearch').focus({preventScroll: true});
  }
  function wire() {
    const search = node('scaleSearch');
    search.addEventListener('input', () => {
      clearTimeout(scale.searchTimer);
      scale.searchTimer = setTimeout(() => searchProducts(search.value.trim()).catch(error => setMessage(error.message, 'bad')), 180);
    });
    node('scaleProducts').addEventListener('click', event => {
      const tile = event.target.closest('[data-product]');
      if (!tile) return;
      const product = scale.products.find(row => row.id === tile.dataset.product);
      if (product) { selectProduct(product); setMessage(product.scale_code ? (state.scale.port ? 'Coloque o produto na balança.' : 'Digite o peso ou conecte a balança.') : node('scaleMessage').textContent, product.scale_code ? '' : 'warn'); }
      (state.scale.port ? node('scalePrintBtn') : node('scaleManual')).focus?.();
    });
    node('scaleManual').addEventListener('input', renderWeight);
    node('scalePrintBtn').addEventListener('click', () => run(printLabel));
    node('scaleConnectBtn').addEventListener('click', () => run(async () => {
      if (state.scale.port) await disconnectScale(); else await connectScale();
      scale.tareMilli = 0;
      scale.stability.reset();
      renderWeight();
    }));
    node('scaleTareBtn').addEventListener('click', () => run(async () => {
      if (scale.tareMilli) { scale.tareMilli = 0; renderWeight(); return; }
      if (!scale.reading || scale.reading.status !== 'ok' || !scale.stable) throw Error('Coloque o pote ou a bandeja vazia e aguarde estabilizar para tarar');
      scale.tareMilli = scale.reading.milli;
      renderWeight();
    }));
    node('scaleSettingsBtn').addEventListener('click', () => run(settingsDialog));
    node('scaleHistoryBtn').addEventListener('click', () => run(loadHistory));
    document.querySelector('#scaleTab .scale-copies').addEventListener('click', event => {
      const button = event.target.closest('[data-copies]');
      if (!button) return;
      const next = Math.min(50, Math.max(1, (copies() || 1) + Number(button.dataset.copies)));
      node('scaleCopies').value = String(next);
    });
  }
  // Teclado da tela Balança (fase de captura, antes do teclado do caixa): Enter imprime com peso estável.
  document.addEventListener('keydown', event => {
    if (typeof state === 'undefined' || state.tab !== 'scale' || modalStack.length || !state.session?.operator) return;
    if (event.ctrlKey || event.metaKey || event.altKey || /^F\d+$/.test(event.key) || event.key === 'Escape') return;
    const target = event.target, search = node('scaleSearch');
    const editable = !!target.matches?.('input,textarea,select');
    if (event.key === 'Enter') {
      if (target === search && search.value.trim()) {
        event.preventDefault();
        clearTimeout(scale.searchTimer);
        run(async () => {
          const found = await searchProducts(search.value.trim());
          if (found?.length === 1 || (found?.length && !scannerBurst())) { selectProduct(found[0]); search.select(); }
          else if (!found?.length) setMessage('Nenhum produto por quilo com esse nome ou código.', 'bad');
        });
        return;
      }
      if (target.matches?.('button') && target !== node('scalePrintBtn')) return;
      event.preventDefault();
      if (scannerBurst()) return;  // leitura de código nunca imprime etiqueta
      if (node('scalePrintBtn').disabled) { renderTotal(); setMessage(node('scalePrintBtn').title, 'warn'); beep('error'); return; }
      if (!enterMayPrint()) { setMessage('Etiqueta deste peso já impressa. Tire o produto da balança ou toque em Imprimir para outra cópia.', 'warn'); return; }
      run(printLabel);
      return;
    }
    if (editable) return;
    if (event.key === '+' || event.key === '-') {
      event.preventDefault();
      node('scaleCopies').value = String(Math.min(50, Math.max(1, (copies() || 1) + (event.key === '+' ? 1 : -1))));
      return;
    }
    if (event.key.length === 1 && event.key !== ' ') search.focus();
  }, true);
  // Etiqueta de gôndola a partir da aba Estoque e reenvio de etiqueta na aba Impressoras.
  document.addEventListener('click', event => {
    const shelf = event.target.closest?.('[data-shelf-label]'), retry = event.target.closest?.('[data-label-retry]');
    if (shelf) {
      (async () => {
        await api('/api/print/labels', 'POST', {product_id: shelf.dataset.shelfLabel, quantity: 1});
        api('/api/print/flush', 'POST', {}).catch(() => {});
        toast('Etiqueta de gôndola enviada para a etiquetadora');
      })().catch(error => toast(error.message, true));
    } else if (retry) {
      (async () => {
        if (!(await confirmDialog('Reenviar etiqueta?', 'A etiquetadora pode já ter impresso. O reenvio pode gerar etiqueta duplicada.', 'Reenviar'))) return;
        await api('/api/print/jobs/' + encodeURIComponent(retry.dataset.labelRetry) + '/retry', 'POST', {confirm_duplicate: true});
        toast('Reenvio colocado na fila');
        await loadLabelPrinter();
      })().catch(error => toast(error.message, true));
    }
  });
  document.addEventListener('submit', event => {
    if (event.target.id !== 'labelPrinterForm') return;
    event.preventDefault();
    saveLabelPrinter().catch(error => toast(error.message, true));
  });

  window.PDVScale = {load, onSession, onReading, saveBarcodeMode, loadLabelPrinter, printLabel};
})();
