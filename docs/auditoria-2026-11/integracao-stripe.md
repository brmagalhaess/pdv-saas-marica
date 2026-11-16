## Stripe como segundo provedor de assinatura (cartão, BRL)

### 1. O que a documentação atual do Stripe diz
- **Checkout com `mode=subscription`**: a sessão recebe `line_items[0][price]`, `success_url` (que pode levar `{CHECKOUT_SESSION_ID}`), `customer` ou `client_reference_id` e `subscription_data[metadata]`. O Stripe recomenda `subscription_data[billing_mode][type]=flexible`, que exige a API 2025-06-30.basil ou mais nova. Os eventos mínimos são `checkout.session.completed`, `invoice.paid` e `invoice.payment_failed`. Também vale ouvir `customer.subscription.updated/deleted`, `invoice.payment_action_required`, `charge.refunded` e `charge.dispute.created`.
- **Status da assinatura no Stripe**: `incomplete` dura 23h e depois vira `incomplete_expired`. Os outros são `active`, `past_due`, `unpaid`, `canceled` (terminal) e `paused`. A orientação é: ao receber `invoice.paid`, buscar a assinatura de novo e confirmar que está `active` antes de estender o acesso. A partir da API 2025-03-31.basil, a fatura aponta para a assinatura em `parent.subscription_details.subscription`, e não mais em `invoice.subscription`.
- **Verificação do webhook**: usa o header `Stripe-Signature` (`t=…,v1=…`), o corpo **bruto** e o segredo `whsec_`. O código deve aceitar entradas extras no header e mais de um `v1` durante a rotação do segredo. O Stripe pode reenviar o mesmo evento e por até 3 dias em produção, então é preciso deduplicar pelo `event.id` e responder 2xx rápido.
- **Idempotency-Key**: vale em todo POST, tem até 255 caracteres e pode ser descartada depois de 24h. Se a mesma chave voltar com parâmetros diferentes, o Stripe dá erro. Se a chave for repetida, o Stripe devolve a resposta salva, até mesmo um erro 500.
- **Cartão no Brasil**: a página de suporte do Stripe para o Brasil indica Visa e Mastercard de crédito. A tabela está desconfigurada, mas Elo, Hipercard e Amex parecem não aceitos, assim como débito nacional. **Isso precisa ser confirmado com o Stripe antes de lançar.** **Não há parcelamento brasileiro**: a página de parcelamento só lista México, Japão e Mastercard Installments.
- **Impostos**: na América Latina, o Stripe Tax só calcula imposto doméstico para empresa sediada no México. Para SaaS no Brasil, não ligue `automatic_tax`. O ISS/NFS-e continua fora do Stripe.
- **SCA/3DS**: o Checkout cuida da autenticação na primeira cobrança. Nas renovações, o banco pode pedir autenticação e o Stripe manda `invoice.payment_action_required`. Cartões de teste: `4000 0025 0000 3155` (3DS), `4000 0000 0000 9995` (recusa) e `4000 0007 6000 0002` (Visa BR).
- **Customer Portal**: cria-se a sessão com `customer` e `return_url`. O downgrade agendado para o fim do período só funciona entre preços do **mesmo produto** e cria um *subscription schedule*.

### 2. Como o código está hoje (lido)
- O provedor fica preso no Asaas em vários pontos:
  - `operations/services.py:30-34`: `account()` devolve `FocusNFe` para **qualquer** provedor que não seja `asaas`. Passar `'stripe'` geraria o cliente errado sem dar erro.
  - `operations/services.py:49,71,115,131` também chamam `account('asaas')` direto.
  - `operations/views.py:110` e `:151`, e `cloud/core/views_platform.py:376`, que verifica o Asaas antes de conceder o VIP.
- `reconcile_payment` (`services.py:70-107`) já segue o modelo certo: consulta o estado canônico no provedor, confere pagador e valor e só libera o acesso com `state=='paid'`. Mas ela filtra `provider='asaas'` (linha 85) e calcula o período com `_end_period` a partir do vencimento (linha 89).
- `BillingBinding` (`operations/models.py:35-43`) não tem coluna `provider`, e `external_id` é único no banco todo. Os IDs do Asaas também começam com `sub_`.
- `Plan` (`cloud/core/models.py:19-28`) guarda só preço em centavos, sem nenhum ID de preço do provedor. `BillingEvent.external_id` é único entre todos os provedores (linha 707).
- `asaas_webhook` (`operations/views.py:168-188`) autentica por token, grava um `BillingEvent` mínimo e enfileira `billing.reconcile`. É bom modelo a seguir.
- `run_one` (`services.py:213-236`) manda para `needs_review` toda mutação que foi interrompida no meio.
- No painel, `billingForm` (`web/assets/app.js:3235-3272`) só oferece PIX e Boleto. A tela de Serviços (`app.js:3147-3166`) só configura o Asaas.

### 3. Plano passo a passo
1. **Abstração de provedor**: criar `integrations/stripe_billing.py` com uma classe `Stripe(ProviderClient)`.
   - Cabeçalhos: `Authorization: Bearer`, `Stripe-Version` fixa e `Idempotency-Key` em todo POST. O corpo vai como formulário (`data=`), porque o `base.py` envia JSON por padrão.
   - Métodos: `create_checkout_session`, `get_subscription`, `get_invoice`, `update_subscription`, `create_portal_session` e um `invoice_state()` com o mesmo vocabulário do Asaas (`paid`, `overdue`, `refunded`, `disputed`, `cancelled`).
   - Em `services.account()`, trocar o `if` por um dicionário `{'asaas':…, 'stripe':…, 'focusnfe':…}` que dá erro para provedor desconhecido.
2. **Migrações**:
   - Novo modelo `PlanProviderPrice(plan, provider, cycle, currency='brl', external_price_id, lookup_key, amount_cents, active)`, com unicidade de `(provider, plan, cycle)` entre os ativos.
   - `provider` em `BillingBinding`, com unicidade em `(provider, external_id)`.
   - `provider_customer_id` por tenant.
3. **Mapeamento Plan↔Price**: um Product por plano e um Price mensal e outro anual em BRL.
   - Preço no Stripe é imutável. Ao mudar o preço de um plano, criar um Price novo e arquivar o antigo, usando `lookup_key` (`pdv2_<slug>_monthly`) com `transfer_lookup_key`.
   - Antes de cada checkout, validar `amount_cents == price_*_cents`, como já é feito em `services.py:46-47`.
   - No admin, mostrar o ID do preço em Planos e avisar quando estiver divergente.
4. **Endpoints novos**:
   - `POST /api/v1/ops/billing/checkout`: só o owner pode chamar. Antes de criar a sessão, repete as checagens de capacidade e de duplicidade de `views.py:157-161`. Usa a chave do `ProviderJob` como `Idempotency-Key`, `client_reference_id=tenant_id`, `metadata.tenant_id/plan_id/job_id`, `tax_id_collection[enabled]=true` para CPF/CNPJ, `locale=pt-BR` e devolve a `url`.
   - `POST /api/v1/ops/billing/portal`: devolve a URL do portal.
   - `POST /api/v1/ops/billing/change-plan`: troca de plano.
   - `POST /api/v1/ops/webhooks/stripe`: com `@csrf_exempt`. Verifica a assinatura sobre o `request.body` **antes** de chamar `payload()`.
   - `integration()` (`views.py:108`) passa a aceitar `stripe` com ambientes `test` e `live`, e confere se o prefixo da chave (`sk_`/`rk_`) bate com o ambiente.
5. **Webhook → fila**: gravar `BillingEvent(provider='stripe', external_id='stripe:'+evt.id)` e enfileirar `billing.stripe_sync` com a chave `stripe:<evt.id>`. O worker **busca de novo** a assinatura e a fatura, porque a ordem dos eventos não é garantida, confere `metadata.tenant_id`, customer e valor, e então:
   - `invoice.paid` com assinatura `active` → `Subscription.status='active'` e `current_period_end` tirado do período do item (no basil ele fica em `items`), com `license_source='subscription'`;
   - `past_due` → `past_due`;
   - `unpaid` → `suspended`;
   - `canceled` ou `incomplete_expired` → `cancelled`.
   - `checkout.session.completed` só cria o `BillingBinding` e não libera nada.
6. **Upgrade e downgrade**: só pelo endpoint próprio, porque o portal não roda o `capacity_fits` (`services.py:18`).
   - Upgrade: `items[0][id]` mais o novo `price`, com `proration_behavior=always_invoice` e `payment_behavior=pending_if_incomplete`, para o plano só mudar depois de pago.
   - Downgrade: via subscription schedule para o fim do período.
   - Trocar de mensal para anual muda a data de cobrança.
   - No portal, deixar **desligado** "Switch plan". Ligar só atualizar cartão, histórico de faturas e cancelar no fim do período.
7. **Dunning (cobrança de inadimplentes)**: Smart Retries e e-mails do Stripe em pt-BR. Depois de esgotar as tentativas, marcar como `unpaid` (vira `suspended`). Em `invoice.payment_action_required` ou `payment_failed`, guardar o `hosted_invoice_url` em `BillingInvoice.checkout_url`.
8. **Painel**:
   - Em Assinatura, adicionar a opção "Cartão de crédito" que leva ao Checkout.
   - Faixa de aviso para `past_due` com o botão "Atualizar cartão", que abre o portal com `flow_data[type]=payment_method_update`.
   - Mostrar o próximo vencimento e o provedor.
   - No admin, card do Stripe em Serviços e coluna de provedor em Cobranças (o campo já sai em `views_platform.py:479`).
9. **VIP e a regra de uma assinatura ativa**: generalizar `views_platform.py:369-382` para checar o provedor do binding (no Stripe, `canceled` ou `cancel_at_period_end`). Manter no máximo um binding ativo por tenant somando os dois provedores.
10. **Fiscal**: emitir a NFS-e de serviço pelo fluxo próprio quando chegar `invoice.paid`. O Stripe não emite.

### 4. Variáveis de ambiente
- `PDV_STRIPE_WEBHOOK_SECRET` (`whsec_`), uma por ambiente.
- `PDV_STRIPE_API_VERSION`, versão fixa, basil ou mais nova.
- `PDV_PUBLIC_BASE_URL`, para montar `success_url`, `cancel_url` e `return_url`.
- `PDV_STRIPE_PORTAL_CONFIGURATION_ID`.
- A chave secreta fica criptografada em `IntegrationAccount`, como o Asaas. De preferência uma **restricted key**.

### 5. Armadilhas
- O corpo do webhook precisa ser o bruto. Re-serializar o JSON quebra a assinatura.
- Eventos podem chegar duplicados ou fora de ordem. Sempre consultar o Stripe de novo antes de aplicar.
- A chave de idempotência é descartada depois de 24h. Isso permite relaxar o `needs_review` (`services.py:216`) para o Stripe, mas só dentro dessa janela.
- Mudanças do basil: `invoice.subscription` e `current_period_end` mudaram de lugar.
- Escutar `invoice.created` sem responder 2xx atrasa a finalização das faturas em até 72h.
- Não há parcelamento, e Elo e débito brasileiro provavelmente ficam de fora. Por isso, manter o Asaas para Pix/Boleto e para o plano anual.
- Assinatura no Stripe não gera NFS-e.
- Os segredos de teste e de produção são diferentes.
- Existe o risco de o mesmo tenant ficar com assinatura ativa nos dois provedores ao mesmo tempo.

## Itens
- [high/M] **Abstração de provedor e correção de account()**: services.py:30-34 devolve FocusNFe para qualquer provedor que não seja asaas. Trocar por um dicionário de provedores e criar integrations/stripe_billing.py (corpo em formulário, Stripe-Version, Idempotency-Key).
- [high/M] **Migrações: PlanProviderPrice e provider no BillingBinding**: Plan (core/models.py:19) não guarda o ID do preço. BillingBinding.external_id é único no banco todo (operations/models.py:39), sem provedor.
- [high/M] **Webhook /ops/webhooks/stripe com assinatura sobre o corpo bruto**: Verifica Stripe-Signature antes do payload(), grava BillingEvent 'stripe:evt' e enfileira billing.stripe_sync, seguindo o asaas_webhook (views.py:168-188).
- [high/G] **Worker billing.stripe_sync com estado canônico**: Busca de novo a assinatura e a fatura; ativa só com invoice paga e assinatura active; usa o período do item (basil); mapeia past_due, unpaid e canceled.
- [high/M] **Endpoint de Checkout (cartão) e opção no billingForm**: POST /ops/billing/checkout com checagens de capacidade e duplicidade (views.py:157-161), client_reference_id, tax_id_collection e locale pt-BR; nova opção em app.js:3250.
- [medium/P] **Portal do cliente restrito e banner de dunning**: Portal só com cartão, faturas e cancelamento no fim do período; botão 'Atualizar cartão' via flow_data payment_method_update quando past_due.
- [medium/G] **Upgrade/downgrade pelo endpoint próprio**: Upgrade com always_invoice e pending_if_incomplete; downgrade por schedule no fim do período; sempre rodar capacity_fits antes.
- [medium/M] **VIP e admin cientes do provedor**: Generalizar a checagem do Asaas em views_platform.py:369-382; card do Stripe em Serviços (app.js:3147); garantir uma única assinatura ativa somando os dois provedores.
- [medium/M] **Fiscal e impostos fora do Stripe**: Não ligar automatic_tax (Stripe Tax não calcula imposto doméstico no BR); emitir a NFS-e quando chegar invoice.paid.

Fontes:
https://docs.stripe.com/payments/checkout/build-subscriptions
https://docs.stripe.com/billing/subscriptions/webhooks
https://docs.stripe.com/webhooks/signature
https://docs.stripe.com/api/idempotent_requests
https://docs.stripe.com/customer-management/configure-portal
https://docs.stripe.com/billing/subscriptions/upgrade-downgrade
https://docs.stripe.com/payments/installments
https://docs.stripe.com/tax/supported-countries/latin-america-and-caribbean
https://support.stripe.com/questions/accepted-payment-methods-in-brazil
https://docs.stripe.com/payments/cards/testing
/Users/brunomagalhaes/Documents/Codex/2026-09-28/se-x20/outputs/pdv-saas-phase1/integrations/billing.py
/Users/brunomagalhaes/Documents/Codex/2026-09-28/se-x20/outputs/pdv-saas-phase1/integrations/base.py
/Users/brunomagalhaes/Documents/Codex/2026-09-28/se-x20/outputs/pdv-saas-phase1/operations/services.py
/Users/brunomagalhaes/Documents/Codex/2026-09-28/se-x20/outputs/pdv-saas-phase1/operations/models.py
/Users/brunomagalhaes/Documents/Codex/2026-09-28/se-x20/outputs/pdv-saas-phase1/operations/views.py
/Users/brunomagalhaes/Documents/Codex/2026-09-28/se-x20/outputs/pdv-saas-phase1/cloud/core/models.py
/Users/brunomagalhaes/Documents/Codex/2026-09-28/se-x20/outputs/pdv-saas-phase1/cloud/core/views_platform.py
/Users/brunomagalhaes/Documents/Codex/2026-09-28/se-x20/outputs/pdv-saas-phase1/web/assets/app.js