# API v1 do novo serviço cloud (contrato de integração)

Base: `/api/v1/`. JSON UTF-8; dinheiro em centavos inteiros; estoque/quantidade em milésimos (`1000 = 1 UN/KG/L`). Produtos `UN` exigem quantidade múltipla de 1000 em vendas e movimentos; `KG` e `L` admitem frações. IDs UUID em texto com hífens. Tempos de eventos do terminal em epoch **milissegundos**; datas do painel em ISO 8601. Respostas de erro: `{ "error": "mensagem" }`.

## Autenticação

Painel: sessão Django (cookie HttpOnly), `X-CSRFToken` nas mutações; `GET /api/v1/csrf` devolve `{csrfToken}` e define o cookie. Selecione a empresa com `X-Tenant-ID` em toda rota de tenant. O servidor confere `Membership` em cada pedido; indicar outro tenant não concede acesso. Plataforma usa conta `is_staff`/`is_superuser` e rotas `/platform/`.

Os limites de tentativas de registro, login e pareamento são compartilhados no banco. Por padrão usam o endereço TCP do cliente. Atrás de proxy, configure **ambos** `PDV_TRUST_PROXY=1` e `PDV_TRUSTED_PROXY_CIDRS` com as faixas das conexões que podem entregar `X-Forwarded-For`; a API usa a última entrada IP válida apenas quando o peer imediato pertence a essas faixas. Com a lista vazia, cabeçalhos encaminhados são ignorados.

- `POST auth/register` `{company,name,email,password}` → `{user,tenant,membership}` e sessão iniciada.
- `POST auth/login` `{email,password}` → `{user,memberships}`.
- `POST auth/logout` → `{ok:true}`.
- `GET auth/me` → `{user,memberships,version}`. `version` é a versão do serviço em execução (`PDV_VERSION`), para o painel mostrar e o suporte conferir. `user`: `{id,name,email,is_staff,is_superuser,platform_roles}`; `memberships`: `{id,tenant_id,tenant_name,role}`.
- `GET plans` público → `{results:[{id,slug,name,price_month_cents,price_year_cents,terminal_limit,shop_limit,features}]}`.

Listagens devolvem `{results:[...],next_cursor:null|"..."}`. `shops`, `categories`, `products`, `customers` e `suppliers` vêm em ordem de nome (desempate pelo ID), e é nessa ordem que os seletores do painel paginam. Recursos em `GET/POST` coleção e `GET/PATCH/DELETE` detalhe, salvo indicação. Mutações exigem permissões por papel. Campos desconhecidos ou tipos inválidos são rejeitados.

## Dados da empresa

- `shops`: `{id,name,active,address}`.
- `categories`: `{id,name,active,commission_bps}`.
- `products`: `{id,sku,barcode,ncm,scale_code,name,category_id,variant,unit,group_id,variant_values,price_cents,cost_cents,stock_minimum_milli,stock_controlled,image_url,tax_fields,active}`; `image_url` aceita um caminho local (`/assets/...`) ou uma URL `http(s)`, mas o painel só exibe imagens externas servidas por **HTTPS** (ver Cabeçalhos de segurança); o cadastro recebe preço de venda, nunca custo de compra. `cost_cents` é o custo médio ponderado calculado pelo servidor a partir das entradas. `scale_code` é o PLU numérico opcional (até 6 dígitos) usado em etiquetas EAN-13 de balanças. `unit` em `UN|KG|L`. O servidor gera SKU e código de barras EAN-13 interno ao criar; ambos são somente leitura e únicos por empresa. O prefixo `2` identifica código de circulação interna, não um GTIN GS1. Filtro `?q=`.
- `customers`: `{id,name,document,email,phone,active,credit_limit_cents}`.
- `suppliers`: `{id,name,document,email,phone,active}`.
- `stock?shop_id=<uuid>`: `{results:[{product_id,sku,name,unit,quantity_milli,stock_minimum_milli,below_minimum}]}`.
- `stock/movements`: GET `?shop_id=`; POST `{shop_id,product_id,delta_milli,reason,idempotency_key}` → movimento e saldo. Ajustes manuais só por gerente/estoque/administrador.
- `stock/receipts`: GET; POST `{shop_id,supplier_id?,reference?,idempotency_key,items:[{product_id,quantity_milli,unit_cost_cents}]}` → entrada e movimentos atômicos. O custo da entrada é obrigatório; cada item preserva o custo pago e o custo médio após o lançamento.
- `finance`: GET/POST `{kind:'payable'|'receivable',description,amount_cents,due_date,customer_id?,supplier_id?}`; `POST finance/<id>/settle` `{paid_at?,amount_cents?}`. A primeira versão liquida o título integralmente. Exige recurso `finance` no plano.
- `users`: membros da empresa `{id,user_id,name,email,role,active,has_pin}`. POST `{name,email,password,role,pin?}`; PATCH `{name?,role?,active?}`; `POST users/<id>/pin` `{pin}`. O último proprietário ativo não pode ser desativado, removido ou rebaixado.
- `terminals`: GET; POST `{shop_id,name}` → terminal mais `pairing_code` utilizável uma vez por 10 minutos. PATCH `{name?,revoked?,reissue_pairing?,end_send_only?,hub?}`. Criação e reativação obedecem ao limite do plano. Cada terminal traz `hub`, `max_stations`, `stations` e `stations_used` (ver "Caixas adicionais"). A listagem também informa `heartbeat_pending_events` e `heartbeat_error` para suporte.
- `subscription`: GET → `{status,plan,trial_ends_at,current_period_end,license:{key,source,status,can_activate,terminal_limit,terminal_used,terminal_available,max_stations}}`. `license.max_stations` é a quantidade de caixas adicionais incluída por loja no plano. A chave identifica a assinatura da empresa e é visível ao proprietário/administrador; para outros papéis, `key` é `null`. `source` distingue `trial`, `subscription` (pagamento confirmado), `vip` (concessão gratuita da plataforma) e `manual` (alteração administrativa). `license.status` é o estado efetivo, enquanto o `status` externo é o estado gravado da assinatura. Uma assinatura `active` com período vencido produz licença efetiva `past_due` até o fim de 72 horas de tolerância. Após esse prazo, a API não renova licença nem aceita novas alterações de negócio. Eventos locais já enfileirados continuam aceitos de terminal não revogado.
- `sales`: GET `?shop_id=&limit=&cursor=` → resumo paginado (cada venda traz `station_id` e `station_name` do caixa que vendeu; vazios para o caixa principal de terminais antigos); GET `<id>` → cabeçalho, itens, pagamentos, estado fiscal e `returns` (`{id,total_cents,refund_method,reason,created_at,items:[{product_id,name,unit,quantity_milli,amount_cents}]}`; `unit` é a unidade em que o item foi vendido); `POST sales/<id>/return` `{items:[{product_id,quantity_milli}],refund_method:'store_credit'|'account'|'external',reason,customer_id?}` registra devolução pelo painel; `POST sales/<id>/cancel` ou `/refund` `{reason}` estorna o estoque uma vez (papel autorizado). Documento fiscal já autorizado exige cancelamento fiscal separado.
- `dashboard`: GET `?shop_id=&from=&to=` → `{sales_count,revenue_cents,average_ticket_cents,low_stock_count,open_payables_cents,open_receivables_cents,recent_sales}`. Exige `cloud_dashboard`; filtros de período exigem `reports`. Sem recurso `finance`, saldos financeiros retornam zero.
- `owner/monitoring`: GET → snapshot privado do protótipo antigo do app do proprietário (mantido para compatibilidade; o app novo usa `/api/v1/app/*`, ver `docs/APP-GESTAO.md`). Exige papel `owner` e `cloud_dashboard`; retorna terminais (sem os revogados; `terminals_total` conta os não revogados, inclusive não pareados), vendas e faturamento do dia **pela data em que a venda foi feita** (`created_at`, não a de recebimento na nuvem), totais por terminal, recebimentos por forma (`cash` já sem o troco; `other` sem fiado nem crédito da loja) e contas abertas quando o plano possui `finance`. As regras ficam em `core/monitoring.py`, as mesmas do app. A resposta usa `Cache-Control: private, no-store` e nunca permite alterações.
- `audit`: GET → ações recentes com usuário, recurso e data.

Papéis: owner, admin, manager, cashier, inventory, finance, viewer. Owner/admin administram pessoas e terminais; manager opera catálogo/estoque/vendas; inventory opera catálogo/estoque; finance opera títulos; cashier consulta catálogo e vendas; viewer só lê. Operações de alta sensibilidade (plano, papel owner, cancelamento, PIN) recebem checagem adicional. O admin de plataforma não ganha acesso automático aos dados de uma empresa por `X-Tenant-ID`.

## Plataforma

O superadministrador pode usar `GET/POST platform/users` e `GET/PATCH platform/users/<id>` para gerir equipe. POST recebe `{name,email,password,roles,active?}`; PATCH aceita campos parciais. `roles` é uma lista não vazia de `platform_support`, `platform_billing`, `platform_ops`. A resposta omite a senha. Um funcionário de plataforma não pode alterar superusuários por essa API.

- `platform_support`: lê empresas, assinaturas, terminais e auditoria; não consulta catálogos/clientes de tenants.
- `platform_billing`: lê/edita planos e assinaturas, lê faturas.
- `platform_ops`: lê/revoga terminais, lê auditoria; reativação respeita o limite contratado.
- Superadministrador: acesso total da plataforma, incluindo equipe e cadastro das empresas.

Aplicativo PDV2 Gestão (somente superusuário, sessão com MFA da plataforma, CSRF normal; mesmos objetos da API do app, ver `docs/APP-GESTAO.md` §6.18 e §7): `GET/POST platform/app-access` (`?status=active|all&tenant_id=&q=`), `PATCH platform/app-access/<id>` (`{shop_ids?,permissions?,expires_on?,reason?}`), `POST platform/app-access/<id>/revoke` (`{reason}`), `GET platform/app-access/options?tenant_id=`, `GET platform/app-devices?status=&q=`, `POST platform/app-devices/<id>/revoke` (`{reason}`), `GET platform/account-deletions?status=pending|all` e `POST platform/account-deletions/<id>/resolve` (`{status:'done'|'rejected',note?}`; `done` desativa o login e revoga acessos e aparelhos). Pública: `POST public/account-deletion` `{email,note?}` → sempre 202 com a mesma mensagem (não revela se o e-mail existe). Erros dessas rotas trazem também `code` estável.

`GET platform/tenants`, `GET/POST platform/plans`, `PATCH platform/plans/<id>`, `GET platform/subscriptions`, `PATCH platform/subscriptions/<id>` (`{status?,plan_id?,trial_ends_at?,current_period_end?}`), `POST platform/subscriptions/<id>/vip` (`{plan_id,current_period_end}`; somente superusuário), `GET platform/terminals`, `PATCH platform/terminals/<id>` (`{revoked,reason}`), `GET platform/invoices`, `GET platform/audit`. VIP concede acesso exatamente até o fim escolhido, sem registrar pagamento, respeita limites do plano e é auditado. Uma contratação pendente bloqueia a concessão. Para converter uma assinatura paga em VIP, pause primeiro a recorrência no Asaas (`INACTIVE`); a API consulta o provedor e desativa o vínculo local somente após verificar o estado e o pagador. A API registra alterações em auditoria. A política de acesso confere grupo **e** `is_staff`; `is_staff` sozinho não concede visão global.

## Terminal

- `POST terminal/pair` sem sessão, `{license_key,code,device_id,version}` → `{token,terminal_id,tenant_id,shop_id,lease,public_key}`. O servidor confirma que a chave empresarial pertence à empresa do código de pareamento. O código é de uso único; a chave sozinha não dá acesso ao terminal. O token só aparece nessa resposta.
- `GET terminal/bootstrap?limit=500&cursor=0` com `Authorization: Bearer <token>` → `{tenant,shop,terminal,products,next_cursor,operators,lease,public_key}`. Cada produto inclui `quantity_milli` para a loja e `stock_controlled`; quando `stock_controlled=false`, o item é de produção própria, pode ser vendido sem saldo e não gera baixa de estoque. Operadores contêm `{id,name,role,pin_hash}`; PIN separado da senha web. A lease é assinada por `shared/licensing.py`, vincula tenant, loja, terminal e dispositivo, e inclui hash da lista de operadores.
- `POST terminal/heartbeat` com Bearer e `{version,pending_events,last_error,client_time}` → `{ok,server_time,terminal_id,last_seen_at}`. O endpoint é idempotente e mantém a presença do terminal mesmo sem vendas.
- `POST terminal/sync` com Bearer e `{events:[...]}` (1 a 50) → `{accepted_event_ids:[...],conflicts:[...],quarantined:[...],send_only,send_only_until?}`. `send_only` vem sempre (`false` para o token do computador atual); `send_only_until` só quando é `true`. Contrato completo, exemplos e todos os campos em [SYNC-VENDAS.md](SYNC-VENDAS.md).

### Recuperação e troca de computador (fase 2)

Visão geral em [RECUPERACAO-E-REPLICA.md](RECUPERACAO-E-REPLICA.md). A nuvem anuncia `capabilities: ["restore","send-only","terminal-config","mirror-key","stations"]` em `pair`, `lease` e `bootstrap`; o terminal só usa o que foi anunciado.

- `POST terminals/<id>/transfer` (owner, admin), corpo `{}` → dados do terminal mais `{recovery_code:"XXXX-XXXX-XXXX",recovery_expires_at,license_key,pending_events,open_cash_session,send_only_days,history_days:90}`. `send_only_days` é um número: `max(7, dias offline do plano + 1)`, 31 no plano padrão. Mesmo `Terminal.id`; o código vale 30 minutos e não muda o token em uso. 409 `terminal_not_paired` se o terminal nunca foi conectado; 409 `Terminal revogado` (sem `code`) se foi revogado. `PATCH terminals/<id>` aceita `{"end_send_only": true}` e fecha o envio de todos os computadores substituídos.
- `POST terminal/pair` aceita o código de recuperação em maiúsculas ou minúsculas e sem hífen. Com outro `device_id`, o token anterior vira **só-envio pelo prazo da licença offline do plano mais 1 dia, nunca menos de 7**; a data exata vem em `send_only_until` e o dono pode encerrar antes com `PATCH terminals/<id> {"end_send_only": true}`. Cada computador substituído guarda o próprio token só-envio (tabela `TerminalFormerToken`): uma segunda troca não tira o envio do primeiro. A resposta ganha `recovery` (`true` quando houve troca de computador ou quando a nuvem já tem histórico deste terminal), `history_days`, `capabilities` e `mirror_key: {"key_id","key"} | null`. `lease` também devolve `mirror_key`.
- **Token só-envio:** `terminal/sync` responde 200 com `send_only: true, send_only_until`; `lease`, `bootstrap`, `heartbeat` e `restore` respondem 403 `{"error":"Este caixa foi transferido para outro computador.","code":"terminal_transferred","send_only_until","transferred_at"}`. Vencido o prazo (ou encerrado no painel), o `sync` também responde 403, com `send_only_until: null`. Um `terminal.config` enviado pelo token só-envio é guardado como evento, mas não substitui a configuração do computador atual.
- `POST terminal/release` com o Bearer do computador **atual**, corpo `{}` → `{released,terminal_id}`. É o "Desistir da recuperação": enquanto o computador substituído por último ainda pode enviar, a nuvem devolve a ele o terminal (token e `device_id` voltam a ser os dele, `transferred_at` e `previous_*` são limpos, auditoria `terminal.transfer_undone`) e o token de quem desistiu deixa de valer. Fora desse caso responde `released:false` e nada muda. Token só-envio recebe 403 `terminal_transferred`.
- `GET terminal/restore?cursor=&limit=` com Bearer (1 a 500, padrão 200; página também fecha em 1 MB) → `{records,next_cursor,done,progress:{position,total},cutoff,history_days,server_time,totals}`. `records` traz, nesta ordem: `config` `{kind,data}`; `cash_session` `{kind,id,operator_id,station_id,station_name,opening_cents,opened_at,closed_at,counted_cents,expected_cents,incomplete,movements[]}`; `sale` `{kind,id,event_id|null,station_id,status:"completed"|"voided",voided_at,void_reason,data,returns[]}` (`station_id` fica fora de `data`); `credit_receipt` `{kind,id,customer_id,cash_session_id,method,amount_cents,operator_id,created_at}`. Em `sale`, `data` é o do `sale.completed` original; **sem o evento original (ou se ele não confere com o hash guardado), `data` é remontado das tabelas e `event_id` vem `null`** (o `sku` e o `ncm` dos itens vêm do cadastro atual do produto). Datas em milissegundos. Entram vendas dos últimos 90 dias, as com fiado em aberto e as do caixa aberto. **Caixa aberto só o mais novo de cada caixa da loja (`station_id`)**: um terminal antigo, sem etiqueta, continua recebendo no máximo um; caixas sem etiqueta contam como o caixa principal (`server`); os demais abertos são contados em `totals.cash_sessions.stale_open_omitted`, e o `cash_session_id` de uma venda pode apontar para um caixa que não veio (fora da janela). O cursor é opaco e retomável; cursor de outro terminal ou vencido → 400 `invalid_cursor`. `totals` só vem na última página e soma o que foi enviado: `{sales:{count,total_cents,voided_count},returns:{count,total_cents},credit_receipts:{count,total_cents},cash_sessions:{count,open_count,stale_open_omitted},cash_movements:{count,total_cents},config:{count}}`. Terminal sem histórico responde uma página vazia com `done:true` e totais zerados.
- Evento `terminal.config` em `terminal/sync`: `aggregate_id` = id do terminal; `data = {version:1,print_settings:[{kind,driver,target,format}],mirror:{enabled},app_version,stations?,updated_at}`. A nuvem guarda a versão de maior `updated_at`, devolve no restore e mostra no painel se a cópia em segundo disco está ligada.
- `GET backup-key` (owner, admin) → `{exists,key_id,revealed_at,can_reveal}`. O GET **não cria** a chave: devolve `exists:false` e `key_id:""` até o primeiro `pair`/`lease` ou `reveal`, com `can_reveal:true` para o owner. `POST backup-key/reveal` (só owner), corpo `{"confirm": true}` → `{key_id,key,revealed_at}` **uma única vez** (cria a chave se preciso); depois 409 `backup_key_already_revealed`; 503 `backup_key_unavailable` quando a chave do servidor (`PDV_ENCRYPTION_KEY`) falta. É a chave que cifra a réplica em segundo disco; a nuvem a guarda cifrada e nunca a registra em log ou auditoria.
- `cash.closed` de uma sessão que já estava fechada com outros valores (o mesmo caixa fechado no computador antigo e no novo) abre o conflito `cash_closed_twice` com os dois fechamentos; vale o de `created_at` mais recente, qualquer que seja a ordem de chegada.
- Respostas de erro com código estável trazem `{"error","code",…}`; as demais continuam só com `error`.

### Sincronização: o que aconteceu no balcão é fato

O contrato antigo (lote atômico) não vale mais. Hoje:

- **Aceitação por evento.** Cada evento roda no próprio *savepoint*, dentro de uma transação que trava o terminal. Um evento que falha não desfaz os outros.
- **`accepted_event_ids` contém todos os eventos recebidos**, na ordem do lote: aplicados, aplicados com conflito e em quarentena. O terminal só dá baixa na fila quando a lista é igual à que enviou.
- **`conflicts`**: um registro por evento e motivo, `{id,event_id,event_type,aggregate_id,reason,status,detail,resolution,terminal_id,terminal_name,shop_id,shop_name,created_at,resolved_at,resolved_by}`. O evento foi aplicado; o administrador confere.
- **`quarantined`**: `[{event_id,reason:"rejected_event",message}]`. O evento não pôde ser aplicado (integridade), foi guardado com o conteúdo original e também aparece em `conflicts`. Não é reprocessado sozinho.
- **Só recusam o pedido inteiro**: token inválido ou terminal revogado (401), lote fora de 1 a 50 eventos (400), evento sem `event_id` em texto de 1 a 64 caracteres (400), `identity` diferente de `{tenant_id,shop_id,terminal_id}` do token (403) e banco indisponível (5xx).
- **Reenvio**: mesmo `event_id` com o mesmo conteúdo é confirmado de novo, devolvendo os conflitos já abertos, sem julgar as regras outra vez. ID repetido com conteúdo diferente vai para a quarentena.

Eventos e campos de `data` (centavos, milésimos, datas em epoch ms):

| Evento | `aggregate_id` | `data` |
|---|---|---|
| `sale.completed` | venda | `{request_id,cash_session_id,created_at,gross_cents,discount_cents,total_cents,change_cents,promo_discount_cents?,customer_id?,customer_name?,customer_document?,operator_id?,seller_id?,items:[{product_id,name,unit,quantity_milli,unit_price_cents,gross_cents,discount_cents,line_total_cents,promo_discount_cents?,promotion_id?,promotion_name?,ncm?,sku?,stock_controlled?}],payments:[{method,amount_cents,payment_method_id?}]}` |
| `sale.voided` | venda | `{sale_id,reason,created_at}` |
| `sale.returned` | devolução | `{sale_id,items:[{product_id,quantity_milli,amount_cents}],total_cents,refund_method,reason,operator_id,created_at,customer_id?,supervisor_id?,cash_session_id?}`; `refund_method` em `cash`, `store_credit` ou `account` |
| `credit.received` | recebimento | `{customer_id,amount_cents,method,operator_id,created_at,cash_session_id?}`; `method` em `cash`, `pix`, `card_credit` ou `card_debit` |
| `stock.adjusted` | ajuste | `{product_id,delta_milli,reason,created_at}` |
| `cash.opened` | sessão de caixa | `{opening_cents,operator_id,created_at}` |
| `cash.closed` | sessão de caixa | `{counted_cents,expected_cents,difference_cents,created_at,operator_id?,supervisor_id?}` |
| `cash.withdrawn`, `cash.supplied` | movimento | `{cash_session_id,amount_cents,reason,operator_id,created_at}` |

Campos novos em relação ao contrato antigo:

- `operator_id` e `seller_id` na venda: operador do caixa e vendedor (membros da empresa); alimentam relatórios e comissão.
- `promo_discount_cents` na venda e no item, com `promotion_id`/`promotion_name` no item. A linha fecha em `gross_cents − promo_discount_cents − discount_cents`; cada linha usa arredondamento HALF_UP de `unit_price_cents × quantity_milli ÷ 1000`.
- `payment_method_id` no pagamento: obrigatório com `method:'other'` (forma própria da empresa), proibido nas demais. Formas: `cash`, `pix`, `card`, `card_credit`, `card_debit`, `other`, `credit_account` (fiado) e `store_credit` (crédito na loja); as duas últimas exigem `customer_id`.
- `sale.returned`: devolução parcial ou total feita no caixa, com reembolso em dinheiro, crédito na loja ou abatimento do fiado.
- `credit.received`: fiado recebido no caixa; quita as parcelas mais antigas e o excedente vira crédito na loja.

Motivos de conflito (`reason`):

| `reason` | Significado |
|---|---|
| `negative_stock` | venda deixou o saldo central negativo (`stock_status=negative`) |
| `discount_over_limit` | desconto manual acima do limite atual da administração |
| `unit_changed` | unidade do produto mudou depois da venda ou do ajuste |
| `stock_control_changed` | controle de estoque do produto mudou; um ajuste para produto sem controle não é aplicado |
| `product_inactive` | venda ou ajuste de produto desativado |
| `promotion_changed` | promoção desativada ou alterada depois da venda |
| `payment_method_inactive` | forma de pagamento desativada depois da venda |
| `void_already_reversed` | venda cancelada no caixa já estava cancelada ou estornada no painel |
| `void_after_return` | venda cancelada no caixa depois de uma devolução |
| `fiscal_cancel_required` | venda cancelada com NFC-e autorizada ou em processamento |
| `return_exceeds_sold` | devolução no caixa acima do que ainda podia ser devolvido |
| `return_on_reversed_sale` | devolução de venda já cancelada ou estornada no painel |
| `customer_inactive` | fiado recebido de cliente desativado |
| `cash_expected_negative` | caixa fechado com valor esperado negativo |
| `cash_closed_twice` | mesmo caixa fechado duas vezes com valores diferentes (computador antigo e novo depois de uma troca) |
| `rejected_event` | quarentena: o evento não pôde ser aplicado |

O caixa baixa seu banco local ao concluir a venda; a nuvem registra **uma** baixa por venda e produto ao receber `sale.completed`. Não envie essa baixa também como ajuste de estoque. Para a carga inicial, escolha **um único fluxo**: lance no SaaS por entrada ou ajuste manual e deixe o saldo chegar ao caixa pelo bootstrap. O servidor aceita eventos já enfileirados quando a assinatura vence, desde que o dispositivo não tenha sido revogado. Emissão de NFC-e, pagamentos e operação em escala precisam de homologação antes de venda em produção.

Conflitos de sincronização: `GET sync/conflicts?status=open|acknowledged|resolved|ignored` (papel owner/admin/manager) lista conflitos por tenant; `PATCH sync/conflicts/<id>` aceita `status=acknowledged|resolved|ignored` e `resolution`. O terminal guarda a mesma lista em SQLite e expõe `GET /api/sync/conflicts` para gerentes locais.


## Caixas adicionais (fase 3)

Visão geral em [ARQUITETURA-OFFLINE-LOCAL.md](ARQUITETURA-OFFLINE-LOCAL.md). O computador servidor da loja ("caixa principal") é o único `Terminal` da nuvem; os caixas adicionais (estações) são um conceito local dele. A nuvem só limita a quantidade e etiqueta caixas e vendas. Tudo aqui é opcional para o terminal: eventos sem `station`, `terminal.config` sem `stations` e restore de um caixa aberto só continuam valendo.

- **Capacidade `stations`** em `capabilities` de `pair`, `lease` e `bootstrap`. O terminal só envia `station` e `stations` depois de vê-la.
- **Plano:** `max_stations` (inteiro de 0 a 50, padrão 0) = caixas adicionais por loja, sem contar o principal. Aparece em `plans`, `subscription.plan`, `subscription.license.max_stations` e é aceito em `POST platform/plans` e `PATCH platform/plans/<id>`; fora da faixa → 400. A migração `0040` sugere `essential=1`, `management=2`, `advanced=4` só onde o valor ainda era 0.
- **Caixa principal da loja (hub):** `Shop.hub_terminal`. Enquanto ninguém escolhe, o hub é o terminal pareado e não revogado mais antigo da loja. `PATCH terminals/<id>` com `{"hub": true}` (owner, admin) define o hub da loja daquele terminal e grava a auditoria `terminal.hub_set` (`{shop_id,previous_terminal_id}`); `hub` diferente de `true` → 400; terminal revogado → 409; terminal ainda sem computador (nunca pareado) → 409 `terminal_not_paired`, e um ponteiro antigo para terminal não pareado é ignorado (vale o pareado mais antigo). Eventos `cash.*` com o id de um caixa de outro terminal ou de outra empresa vão para a quarentena ("Caixa já recebido de outro terminal"), sem tocar no caixa alheio. Revogar o hub limpa a escolha e o hub volta a ser o mais antigo. O primeiro `terminal.config` com `stations` não vazia de uma loja sem hub escolhido define esse terminal como hub.
- **Licença:** os claims assinados ganham `max_stations` (o do plano para o hub da loja, `0` para os demais terminais dela) e `hub` (booleano). Mudar o plano ou o hub muda o claim na renovação seguinte. Terminais antigos ignoram os claims extras.
- **Terminal no painel** (`terminals`, `terminals/<id>`, `terminal` em `pair`/`bootstrap`): `hub`, `max_stations` (o que a licença dele carrega), `stations` (`[{id,name,revoked}]`, como o terminal informou por último) e `stations_used` (as não revogadas).
- **Etiqueta nos eventos:** chave opcional de topo `"station": {"id","name"}` em qualquer tipo de evento de `terminal/sync`, fora de `data` (o `content_hash` da venda não muda). Válida = `id` texto de 1 a 64 caracteres em `[A-Za-z0-9_-]` e `name` texto de até 80. `cash.opened` grava `station_id`/`station_name` no caixa e `sale.completed` na venda. **Etiqueta malformada nunca recusa o evento**: os campos ficam vazios, sem quarentena e sem conflito. A etiqueta entra no `payload_hash` do evento como qualquer outra chave: o reenvio idêntico é reconhecido; o mesmo `event_id` com etiqueta diferente vai para a quarentena. Qualquer outra chave de topo desconhecida continua indo para a quarentena. O caixa principal usa `id: "server"`.
- **`terminal.config`:** chave opcional `stations`: lista de até 64 itens exatamente `{id,name,revoked}` (`id` e `name` como acima, `revoked` booleano); forma errada → quarentena. Vale a regra de sempre (maior `updated_at` vence; token só-envio não troca a configuração nem define hub).
- **Vários caixas abertos:** um terminal pode ter um caixa aberto por `station_id` ao mesmo tempo; fechar um não fecha os outros; `cash_closed_twice` continua igual.
- **`cash-sessions`:** GET `?shop_id=&terminal_id=&station_id=&status=&from=&to=`; cada caixa traz `station_id` e `station_name`. `station_id=server` também traz os caixas sem etiqueta (terminais antigos); `station_id` fora do formato → 400. `sales` e `sales/<id>` trazem os mesmos dois campos.
- **Restauração:** ver `terminal/restore` acima: um caixa aberto por estação, `station_id`/`station_name` em `cash_session` e `station_id` em `sale`. O cursor carrega até 64 caixas abertos; cursores emitidos antes desta versão continuam válidos.

## Páginas públicas e cabeçalhos de segurança

- `GET /robots.txt` (texto): libera a landing (`Allow: /`) e bloqueia `/app/`, `/platform/`, `/login/`, `/api/` e `/control/`; aponta para o sitemap.
- `GET /sitemap.xml`: contém a landing e as páginas públicas do app (`/privacidade/`, `/suporte/`, `/excluir-conta/`). O endereço público vem de `PDV_PUBLIC_URL` (variável de ambiente lida em `settings.py`); o padrão é `https://pdv2.crowntech.com.br`.
- `GET /privacidade/`, `/suporte/`, `/excluir-conta/`: páginas HTML estáticas de `web/` (política de privacidade LGPD, suporte e pedido de exclusão de conta exigidos pela App Store e pela Google Play). O e-mail mostrado vem de `PDV_SUPPORT_EMAIL` (padrão `suporte@crowntech.com.br`). O formulário de exclusão usa `web/assets/public.js` (sem script embutido, por causa da CSP).
- `Permissions-Policy: camera=(self), microphone=(), geolocation=()`. A câmera é liberada só para a própria origem, porque o inventário do painel lê códigos de barras pela câmera do celular. Microfone e localização continuam bloqueados.
- `Content-Security-Policy`: `default-src 'self'`, scripts só da própria origem, sem `object`, sem *frames* de terceiros. `img-src 'self' data: https:` permite que o catálogo mostre fotos de produto cadastradas por URL externa; imagens em `http://` não carregam. Nenhuma outra origem externa foi liberada (`connect-src`, `script-src`, `font-src` continuam em `'self'`).
- Rotas `/api/`, `/app/`, `/platform/`, `/login/` e `/control/` respondem com `Cache-Control: private, no-store`.
