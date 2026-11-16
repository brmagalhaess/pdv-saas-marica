## O que já existe (lido no código)

- `integrations/billing.py`: as URLs de sandbox e produção (l.9), `verify_webhook_token` com tamanho entre 32 e 255 e `compare_digest` (l.11-13), header `access_token` (l.20), `create_customer` que envia só nome, cpfCnpj, e-mail e externalReference (l.27-32), `create_subscription` que aceita PIX/BOLETO/UNDEFINED (l.37-46), `get_payment`/`get_subscription`/`list_subscription_payments` e o mapa `payment_state` (l.57-62).
- `integrations/base.py`: `ProviderError.outcome_unknown` para timeout, 5xx e POST (l.18-24). Não há tratamento de 429.
- `operations/services.py`: fila idempotente (`enqueue` l.22-28), `subscribe` que procura o cliente pela externalReference antes de criar (l.52-61), `reconcile_payment` que sempre consulta o estado canônico por GET (l.70-107) e grava `checkout_url=invoiceUrl` (l.88), e as recuperações (l.109-146). Uma mutação interrompida vai para `needs_review` (l.216-217).
- `operations/views.py`: só o superusuário configura o Asaas, e a conta é global (l.110-111). Também há `subscribe` (l.142-166) e o webhook em `/webhooks/asaas` (l.168-187). O webhook valida `asaas-access-token` contra `PDV_ASAAS_WEBHOOK_TOKEN`, deduplica pelo `id` do evento em `BillingEvent` e enfileira `billing.reconcile`.
- `cloud/core/views_platform.py`: a lista de faturas expõe só `checkout_url` (l.472-481), e o VIP confere a assinatura no Asaas (l.371-384).
- O fiado vive em `FinanceEntry` (receivable com `sale`, `cloud/core/models.py` l.615-633). A baixa é manual em `views_customer_credit.receive` (l.51-79). `Customer` tem email e phone (l.174-183).
- O SMTP já está configurado (`settings.py` l.123-129), e `infra/production.env.example` tem `PDV_ASAAS_WEBHOOK_TOKEN` e `PDV_EMAIL_*` (l.14-20).

## O que diz a documentação atual do Asaas

- **Cliente** (`POST /v3/customers`): `name` e `cpfCnpj` são obrigatórios. Também aceita `email`, `mobilePhone`, `additionalEmails`, `notificationDisabled`, `externalReference` e endereço. A API **permite duplicados**, então é preciso buscar antes de criar.
- **Cobrança** (`POST /v3/payments`): `customer`, `billingType` (BOLETO/PIX/UNDEFINED/CREDIT_CARD), `value`, `dueDate`, `description` (até 500), `externalReference`, `fine`, `interest`, `discount` e `daysAfterDueDateToRegistrationCancellation` (só boleto e imutável). A resposta traz `bankSlipUrl`, `invoiceUrl` e `nossoNumero`.
- **Linha digitável**: `GET /payments/{id}/identificationField` devolve `identificationField`, `barCode` e `nossoNumero`. Deve ser buscada de novo após `PAYMENT_UPDATED`.
- **Pix**: `GET /payments/{id}/pixQrCode` devolve `encodedImage`, `payload` e `expirationDate`. Sem chave Pix cadastrada, o QR vale só até 23:59 do dia.
- **Assinatura**: as cobranças são geradas **40 dias antes** do `nextDueDate`. Criar a assinatura não confirma pagamento. A primeira cobrança dispara `PAYMENT_CREATED` com o campo `subscription`.
- **Notificações**: por padrão o Asaas envia e-mail e SMS em PAYMENT_CREATED, PAYMENT_DUEDATE_WARNING (0 e 10 dias), SEND_LINHA_DIGITAVEL, PAYMENT_RECEIVED, PAYMENT_OVERDUE (0 e 7) e PAYMENT_UPDATED. **WhatsApp vem desligado.**
  - Para mudar por cliente: `GET /customers/{id}/notifications` e depois `PUT /notifications/batch {customer, notifications:[{id, enabled, emailEnabledForCustomer, smsEnabledForCustomer, whatsappEnabledForCustomer, scheduleOffset}]}`.
  - O envio é cobrado.
  - A página de padrões indica que "Cobrança criada" pode não ser enviada para cobranças de assinatura. Isso precisa ser confirmado no sandbox.
- **Webhooks**: entrega "at least once", com o `id` do evento como chave. É preciso responder 200 rápido. A fila pausa após 15 falhas. `POST /v3/webhooks` aceita `name`, `url`, `email`, `enabled`, `interrupted`, `sendType` (SEQUENTIALLY ou NON_SEQUENTIALLY), `events` e `authToken` (32 a 255). O limite é de 10 webhooks por conta.
- **Limites**: 25.000 requisições a cada 12 h por conta, 50 GETs concorrentes, cabeçalhos `RateLimit-*` e erro 429 sem `Retry-After`. Não encontrei header de idempotência para POST, então a idempotência continua sendo "consultar antes de reenviar".

## Plano, parte A: boleto da assinatura do SaaS por e-mail, com conciliação

1. **Cliente**: estender `create_customer` (billing.py l.27-32) com `mobilePhone`, `additionalEmails` e `notificationDisabled=False`, e incluir `phone` opcional no `subscribe` (views.py l.145-155).
2. **Assinatura com boleto**: no painel, usar `billing_type='BOLETO'` por padrão. Mandar `fine` e `interest` só se o produto quiser sobrepor a configuração global da conta. Lembrar que `nextDueDate=hoje` (views.py l.155) gera um boleto que vence no mesmo dia. Usar hoje + N dias.
3. **Dados do boleto**: criar no `BillingInvoice` os campos `bank_slip_url`, `identification_field`, `pix_payload`, `billing_type` e `last_event` (migração). No `reconcile_payment` (l.88), gravar `bankSlipUrl` e, para BOLETO ou UNDEFINED, buscar `identificationField`. Adicionar `get_identification_field` e `get_pix_qr` em billing.py.
4. **E-mail**: duas opções, que podem ser combinadas.
   - (a) Confiar no Asaas: após criar o cliente, ajustar via `PUT /notifications/batch` para ligar e-mail em PAYMENT_DUEDATE_WARNING e SEND_LINHA_DIGITAVEL.
   - (b) E-mail próprio: novo job `billing.notify` enfileirado em `PAYMENT_CREATED` com a chave `notify:{payment_id}`. Ele envia por `send_mail` o link `bankSlipUrl`, a linha digitável e o Pix copia-e-cola. É idempotente porque a chave é única.
   - Recomendo a (b) para assinatura, por causa da dúvida sobre PAYMENT_CREATED em cobranças de assinatura.
5. **Webhook**: o webhook já aceita qualquer evento PAYMENT_*. Falta:
   - preencher `BillingEvent.tenant` e `invoice`;
   - tratar `PAYMENT_UPDATED` com nova busca da linha;
   - incluir `RECEIVED_IN_CASH_UNDONE` e `PAYMENT_PARTIALLY_REFUNDED` no `payment_state`;
   - responder 200 também para eventos de assinatura (hoje eles já são ignorados, l.177-178).
6. **Configuração do webhook**: usar `sendType=SEQUENTIALLY`, a URL `https://<api>/api/.../webhooks/asaas`, o token em `PDV_ASAAS_WEBHOOK_TOKEN` (≥32) e e-mail de alerta de fila pausada.
7. **Painel**: a empresa precisa de uma tela "Minhas faturas" (hoje só a plataforma vê, `views_platform.invoices` l.473), com o link do boleto e a linha digitável.
8. **Rate limit**: em `base.py`, tratar 429 como reprocessável com backoff e ler `RateLimit-Reset`.

## Plano, parte B: lojista cobra fiado por boleto ou Pix

1. **Conta Asaas por empresa**: liberar `provider='asaas'` com `tenant` para OWNER (views.py l.110-114 hoje exige superusuário global). O modelo `IntegrationAccount` já suporta isso (models.py l.14-15). Chamar `account('asaas', tenant)`.
2. **Novo modelo `CustomerCharge`**: tenant, customer, `entries` (M2M de FinanceEntry), asaas_customer_id, external_id (unique), billing_type, amount_cents, due_date, status, bank_slip_url, identification_field, pix_payload e idempotency_key.
3. **Ligação do cliente**: adicionar `asaas_customer_id` por (tenant, customer). Buscar pela externalReference `pdv2c:{tenant}:{customer}` antes de criar. O CPF/CNPJ é obrigatório no Asaas, mas hoje `Customer.document` é opcional, então é preciso validar.
4. **Endpoint**: `POST /customers/{id}/credit/charge {entry_ids, billing_type, due_date, idempotency_key}` cria um job `credit.charge`. Payload: `customer`, `billingType`, `value` (soma das parcelas), `dueDate`, `description` e `externalReference=charge.pk`. Uma falha ambígua vai para `needs_review` e se recupera com `GET /payments?externalReference=`.
5. **Webhook por empresa**: criar a rota `/webhooks/asaas/t/{tenant_id}` com o token guardado criptografado na `IntegrationAccount` (novo campo `webhook_token_encrypted`). O `reconcile_payment` atual exige `BillingBinding` (l.74-75) e falharia aqui. Por isso, criar `credit.reconcile`: ao receber RECEIVED ou CONFIRMED, dar baixa nas `FinanceEntry` com a mesma lógica de `receive` (views_customer_credit.py l.62-73), sem duplicar.
6. **Envio ao cliente final**: usar as notificações do próprio Asaas da conta do lojista (o custo fica com ele). Opcionalmente, configurar WhatsApp via `/notifications/batch`. A mesma tela permite reenviar o e-mail pelo SMTP do PDV2.
7. **Terminal**: mostrar o QR Pix (`encodedImage`) na tela de recebimento de fiado. Só funciona online, e a baixa vem pelo webhook, nunca pelo terminal.

## Variáveis de ambiente

- `PDV_ASAAS_WEBHOOK_TOKEN` (32 a 255 caracteres, diferente da API key).
- `PDV_EMAIL_HOST`, `PDV_EMAIL_PORT`, `PDV_EMAIL_USER`, `PDV_EMAIL_PASSWORD` e `PDV_DEFAULT_FROM_EMAIL` (já existem).
- `PDV_ENCRYPTION_KEY`, para os tokens.
- Novas, sugeridas: `PDV_PUBLIC_API_URL` (para montar a URL do webhook) e `PDV_BILLING_DUE_DAYS`.
- A API key e o ambiente ficam no banco (`IntegrationAccount`), não no env.

## Armadilhas

- Clientes duplicados no Asaas: buscar sempre antes de criar.
- `CONFIRMED` de Pix em conta PF pode virar `REFUNDED` em até 72 h.
- A linha digitável muda após `PAYMENT_UPDATED`.
- A fila do webhook pausa após 15 falhas: é preciso responder 200 mesmo em evento ignorado.
- Sandbox e produção usam tokens e IPs diferentes.
- GET com corpo retorna 403.
- Não fazer polling: a cota é de 25 mil requisições por 12 h.
- `value` é decimal em reais. Converter centavos com `Decimal`, como já é feito (l.45).

## Itens
- [high/P] **Campos de boleto/Pix no BillingInvoice**: Migração com bank_slip_url, identification_field, pix_payload e billing_type; preencher em reconcile_payment (services.py l.88) usando GET /payments/{id}/identificationField e /pixQrCode.
- [high/M] **E-mail do boleto da assinatura**: Job billing.notify disparado em PAYMENT_CREATED, idempotente pela chave notify:{payment_id}, enviado pelo SMTP já configurado (settings.py l.123-129). Opcionalmente ajustar as notificações do Asaas via PUT /notifications/batch.
- [high/P] **Vencimento e cliente completos no subscribe**: Trocar next_due_date=hoje (views.py l.155) por hoje+N dias e enviar mobilePhone e additionalEmails em create_customer (billing.py l.27-32).
- [medium/P] **Webhook mais completo**: Preencher BillingEvent.tenant e invoice, tratar PAYMENT_UPDATED, ampliar payment_state e configurar o webhook com SEQUENTIALLY e e-mail de alerta de fila pausada.
- [medium/P] **Tratar 429 e RateLimit no ProviderClient**: Em base.py, tratar 429 como erro reprocessável com backoff baseado em RateLimit-Reset, sem retry imediato.
- [medium/M] **Faturas visíveis para a empresa**: Endpoint e tela no painel com o boleto, a linha digitável e o Pix das faturas do SaaS (hoje só a plataforma vê, em views_platform.py l.473).
- [high/M] **Conta Asaas por lojista**: Liberar IntegrationAccount asaas por tenant para OWNER (views.py l.110-111) e guardar o token de webhook da empresa criptografado.
- [high/G] **Cobrança de fiado por boleto/Pix**: Modelo CustomerCharge, job credit.charge com externalReference e recuperação por GET, webhook /webhooks/asaas/t/{tenant} e credit.reconcile para dar baixa nas FinanceEntry.
- [low/M] **QR Pix no recebimento de fiado do terminal**: Mostrar encodedImage e payload no caixa quando houver conexão, com a baixa confirmada só pelo webhook.

Fontes:
https://docs.asaas.com/docs/webhook-para-cobrancas
https://docs.asaas.com/docs/receba-eventos-do-asaas-no-seu-endpoint-de-webhook
https://docs.asaas.com/reference/criar-nova-cobranca
https://docs.asaas.com/reference/obter-linha-digitavel-do-boleto
https://docs.asaas.com/reference/obter-qr-code-para-pagamentos-via-pix
https://docs.asaas.com/docs/notificacoes
https://docs.asaas.com/docs/notificacoes-padroes
https://docs.asaas.com/reference/atualizar-notificacoes-existentes-em-lote
https://docs.asaas.com/reference/criar-novo-webhook
https://docs.asaas.com/reference/criar-novo-cliente
https://docs.asaas.com/reference/criar-nova-assinatura
https://docs.asaas.com/reference/rate-e-quota-limit
https://docs.asaas.com/docs/faq-assinaturas
https://docs.asaas.com/docs/eventos-para-assinaturas
integrations/billing.py
integrations/base.py
operations/services.py
operations/models.py
operations/views.py
cloud/core/views_platform.py
cloud/core/views_customer_credit.py
cloud/core/models.py
cloud/pdvcloud/settings.py
infra/production.env.example