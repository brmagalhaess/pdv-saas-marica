/* PDV2 · caixa adicional: vínculo, diagnóstico do caixa principal fora do ar e assistente para assumir.
   Fala só com /station/api/* deste computador. */
(function () {
  'use strict';

  var $ = function (id) { return document.getElementById(id); };
  var views = ['viewLoading', 'viewLink', 'viewDown', 'viewIncompatible', 'viewPromote', 'viewWorking'];
  var state = { status: null, target: null, wizard: false, working: false, searching: false, lastSearch: 0, manual: false };

  function show(view) {
    views.forEach(function (id) { $(id).classList.toggle('hidden', id !== view); });
  }

  function setText(id, text) {
    var node = $(id);
    node.textContent = text || '';
    if (node.classList.contains('error') || node.classList.contains('lead')) node.classList.toggle('hidden', !text);
  }

  function api(path, body) {
    var options = { cache: 'no-store' };
    if (body !== undefined) {
      options.method = 'POST';
      options.headers = { 'Content-Type': 'application/json', 'X-PDV-Station-Local': '1' };
      options.body = JSON.stringify(body);
    }
    return fetch('/station/api/' + path, options).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (data) {
        if (!response.ok) {
          var error = new Error(data.error || 'Não foi possível concluir. Tente de novo.');
          error.code = data.code;
          error.status = response.status;
          throw error;
        }
        return data;
      });
    });
  }

  function clock(ms) {
    return new Date(ms).toLocaleTimeString('pt-BR', { hour: '2-digit', minute: '2-digit' });
  }

  /* ---------- reinício: espera o modo certo em /health ---------- */
  function waitForMode(mode, destination, hint) {
    state.working = true;
    show('viewWorking');
    setText('workingHint', hint || 'Só um instante.');
    var tries = 0;
    var timer = setInterval(function () {
      tries += 1;
      fetch('/health', { cache: 'no-store' }).then(function (response) { return response.json(); }).then(function (data) {
        if (data && data.mode === mode) {
          clearInterval(timer);
          location.replace(destination);
        }
      }).catch(function () { /* reiniciando */ });
      if (tries === 30) setText('workingHint', 'Está demorando. Feche e abra o PDV2 de novo.');
    }, 1000);
  }

  /* ---------- vínculo ---------- */
  function chooseTarget(server) {
    state.target = server;
    $('linkForm').classList.toggle('hidden', !server);
    if (server) {
      var shop = server.shop_name || 'da loja';
      setText('searchStatus', 'Caixa principal do ' + shop + ' encontrado (' + (server.host || server.address) + ')');
      if (!$('linkName').value) $('linkName').value = (state.status && state.status.station && state.status.station.name) || '';
      $('linkCode').focus();
    }
  }

  function search() {
    if (state.searching || state.manual) return;
    state.searching = true;
    state.lastSearch = Date.now();
    api('discover', {}).then(function (data) {
      var servers = data.servers || [];
      var best = servers.filter(function (item) { return item.known; })[0]
        || servers.filter(function (item) { return item.same_shop; })[0] || servers[0];
      if (best) {
        if (!state.target || state.target.fingerprint !== best.fingerprint) chooseTarget(best);
        $('manualForm').classList.add('hidden');
      } else if (!state.target) {
        setText('searchStatus', 'Não encontrei o caixa principal sozinho.');
        $('manualForm').classList.remove('hidden');
      }
    }).catch(function () {
      if (!state.target) {
        setText('searchStatus', 'Não encontrei o caixa principal sozinho.');
        $('manualForm').classList.remove('hidden');
      }
    }).then(function () { state.searching = false; });
  }

  function renderLink(status) {
    show('viewLink');
    var lead = '';
    if (status.state === 'relink') {
      var host = (status.relink && status.relink.host) || 'outro computador';
      $('linkTitle').textContent = 'O caixa principal mudou de computador';
      lead = 'O caixa principal agora é outro computador (' + host + '). Peça ao gerente o código de 6 números em Sistema → Adicionar caixa e digite aqui.';
      if (status.relink && (!state.target || state.target.fingerprint !== status.relink.fingerprint)) {
        chooseTarget(status.relink);
        setText('searchStatus', 'Novo caixa principal encontrado (' + host + ')');
      }
    } else if (status.state === 'revoked') {
      $('linkTitle').textContent = 'Este caixa foi removido';
      lead = 'Este caixa foi removido do caixa principal. Para voltar a usar, peça um novo código ao gerente.';
    } else {
      $('linkTitle').textContent = 'Este é um caixa adicional';
    }
    setText('linkLead', lead);
    $('backToServerBtn').classList.toggle('hidden', status.state !== 'unlinked');
    if (status.state !== 'relink' && !state.target && Date.now() - state.lastSearch > 5000) search();
  }

  $('linkForm').addEventListener('submit', function (event) {
    event.preventDefault();
    if (!state.target) return;
    var code = $('linkCode').value.replace(/\D/g, '');
    if (code.length !== 6) { setText('linkError', 'Digite os 6 números do código.'); return; }
    setText('linkError', '');
    $('linkBtn').disabled = true;
    $('linkBtn').textContent = 'Conectando…';
    api('link', { address: state.target.address, port: state.target.port, fingerprint: state.target.fingerprint,
      code: code, name: $('linkName').value.trim() }).then(function () {
      location.replace('/');
    }).catch(function (error) {
      setText('linkError', error.message);
      if (error.code === 'fingerprint_mismatch') { state.target = null; state.manual = false; chooseTarget(null); search(); }
      $('linkCode').select();
    }).then(function () {
      $('linkBtn').disabled = false;
      $('linkBtn').textContent = 'Conectar este caixa';
    });
  });

  $('manualForm').addEventListener('submit', function (event) {
    event.preventDefault();
    var address = $('manualAddress').value.trim();
    setText('linkError', '');
    if (!address) { state.manual = false; state.lastSearch = 0; setText('searchStatus', 'Procurando o caixa principal na rede da loja…'); search(); return; }
    state.manual = true;
    $('manualBtn').disabled = true;
    api('probe', { address: address }).then(function (server) {
      chooseTarget(server);
    }).catch(function (error) {
      setText('linkError', error.message);
    }).then(function () { $('manualBtn').disabled = false; state.manual = false; state.lastSearch = Date.now(); });
  });

  $('linkCode').addEventListener('input', function () {
    var digits = this.value.replace(/\D/g, '').slice(0, 6);
    this.value = digits.length > 3 ? digits.slice(0, 3) + ' ' + digits.slice(3) : digits;
  });

  $('backToServerBtn').addEventListener('click', function () {
    if (!confirm('Este computador vai voltar a ser um caixa principal (para ativar uma loja nele). Continuar?')) return;
    api('mode/server', { confirm: true }).then(function (data) {
      waitForMode('server', '/', data.restart ? 'Só um instante.' : 'Feche e abra o PDV2 de novo.');
    }).catch(function (error) { setText('linkError', error.message); });
  });

  /* ---------- caixa principal fora do ar ---------- */
  function renderDown(status) {
    show('viewDown');
    var server = status.server || {};
    var line = 'Caixa principal: ' + (server.shop_name || 'loja') + ' (' + (server.host || server.address || '') + ').';
    if (server.last_ok_at) line += ' Última resposta às ' + clock(server.last_ok_at) + '.';
    $('downServer').textContent = line;
    var list = $('diagnosis');
    list.textContent = '';
    (status.diagnosis || []).forEach(function (item) {
      if (item.ok && item.id !== 'ip_changed') return;
      var row = document.createElement('li');
      var title = document.createElement('strong');
      title.textContent = item.title;
      row.appendChild(title);
      if (item.help) {
        var help = document.createElement('span');
        help.textContent = item.help;
        row.appendChild(help);
      }
      if (!item.ok) row.className = 'problem';
      list.appendChild(row);
    });
    $('promoteBtn').disabled = !status.can_promote;
    $('promoteHint').textContent = status.can_promote
      ? 'Use só se o computador principal não vai voltar agora.'
      : (status.promotion ? 'O botão de assumir fica disponível depois de 1 minuto com o caixa principal fora do ar.' : '');
    $('promoteBtn').classList.toggle('hidden', !status.promotion);
  }

  $('retryBtn').addEventListener('click', function () {
    var button = this;
    button.disabled = true;
    setText('downNote', 'Procurando o caixa principal na rede da loja…');
    api('discover', {}).then(function (data) {
      var servers = data.servers || [];
      var same = servers.filter(function (item) { return item.known || item.same_shop; })[0];
      setText('downNote', same ? 'Encontrei o caixa principal. Reconectando…' : 'Ainda não encontrei o caixa principal.');
    }).catch(function () {
      setText('downNote', 'Ainda não encontrei o caixa principal.');
    }).then(function () { button.disabled = false; refresh(); });
  });

  /* ---------- assistente: assumir como caixa principal ---------- */
  var STEPS = { verifying: 'Abrindo a cópia…', restoring: 'Abrindo a cópia…', replaying: 'Aplicando as últimas vendas…',
    finalizing: 'Preparando este computador…', done: 'Preparando este computador…' };
  var pin = '';

  function wizardStep(step) {
    ['promoteWarn', 'promotePinForm', 'promoteConfirm', 'promoteProgress'].forEach(function (id) {
      $(id).classList.toggle('hidden', id !== step);
    });
  }

  function closeWizard() {
    state.wizard = false;
    pin = '';
    $('promotePin').value = '';
    setText('promoteError', '');
    refresh();
  }

  $('promoteBtn').addEventListener('click', function () {
    state.wizard = true;
    show('viewPromote');
    wizardStep('promoteWarn');
    setText('promoteError', '');
    setText('promoteUnavailable', '');
    $('promoteCopy').textContent = '';
    $('promoteNextBtn').disabled = true;
    api('promotion').then(function (data) {
      if (data.available === false) {
        setText('promoteUnavailable', data.reason || 'Este caixa ainda não tem uma cópia completa do caixa principal. Não é possível assumir.');
        return;
      }
      var moment = data.last_contact_at || data.last_snapshot_at;
      if (moment) {
        var minutes = Math.max(0, Math.round((Date.now() - moment) / 60000));
        $('promoteCopy').textContent = 'Cópia deste caixa: de ' + clock(moment) + ' (há ' + minutes + ' min).';
      }
      $('promoteNextBtn').disabled = false;
    }).catch(function (error) { setText('promoteUnavailable', error.message); });
  });

  ['promoteCancelBtn', 'promotePinCancelBtn', 'promoteConfirmCancelBtn'].forEach(function (id) {
    $(id).addEventListener('click', closeWizard);
  });

  $('promoteNextBtn').addEventListener('click', function () {
    wizardStep('promotePinForm');
    $('promotePin').focus();
  });

  $('promotePinForm').addEventListener('submit', function (event) {
    event.preventDefault();
    setText('promoteError', '');
    var typed = $('promotePin').value;
    api('promotion/verify', { manager_pin: typed }).then(function (data) {
      pin = typed;
      $('promoteConfirmTitle').textContent = data.manager ? data.manager + ', assumir agora?' : 'Assumir agora?';
      wizardStep('promoteConfirm');
    }).catch(function (error) {
      setText('promoteError', error.message);
      $('promotePin').select();
    });
  });

  function followPromotion() {
    api('promotion').then(function (data) {
      if (data.finished || data.state === 'done') {
        waitForMode('server', '/', 'Este computador agora é o caixa principal. Entre de novo com o seu PIN.');
        return;
      }
      if (data.state === 'failed') {
        wizardStep('promoteWarn');
        setText('promoteError', data.error || 'Não foi possível assumir como caixa principal. Chame o suporte.');
        return;
      }
      setText('promoteStep', STEPS[data.step] || STEPS.verifying);
      setTimeout(followPromotion, 1000);
    }).catch(function () { setTimeout(followPromotion, 1000); });
  }

  $('promoteStartBtn').addEventListener('click', function () {
    setText('promoteError', '');
    this.disabled = true;
    var button = this;
    api('promotion/start', { manager_pin: pin, confirm: true }).then(function () {
      pin = '';
      $('promotePin').value = '';
      wizardStep('promoteProgress');
      followPromotion();
    }).catch(function (error) {
      if (error.code === 'server_alive') { closeWizard(); return; }
      setText('promoteError', error.message);
    }).then(function () { button.disabled = false; });
  });

  /* ---------- laço principal ---------- */
  function render(status) {
    state.status = status;
    if (state.working) return;
    if (status.state === 'up') { location.replace('/'); return; }
    if (state.wizard && status.state === 'down') return;
    state.wizard = false;
    if (status.state === 'down') { renderDown(status); return; }
    if (status.state === 'incompatible') {
      show('viewIncompatible');
      $('incompatibleTitle').textContent = status.reason === 'server_outdated'
        ? 'Atualize o caixa principal: instale a versão nova do PDV2 nele.'
        : 'Atualize este caixa: instale a versão nova do PDV2.';
      return;
    }
    renderLink(status);
  }

  function refresh() {
    api('status').then(render).catch(function () { /* o serviço local está reiniciando */ });
  }

  refresh();
  setInterval(refresh, 2000);
})();
