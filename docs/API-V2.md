# API v1 do novo serviço cloud (contrato de integração)

Base: `/api/v1/`. JSON UTF-8; dinheiro em centavos inteiros; estoque/quantidade em milésimos (`1000 = 1 UN/KG/L`). Produtos `UN` exigem quantidade múltipla de 1000 em vendas e movimentos; `KG` e `L` admitem frações. IDs UUID em texto com hífens. Tempos de eventos do terminal em epoch **milissegundos**; datas do painel em ISO 8601. Respostas de erro: `{ "error": "mensagem" }`.

## Autenticação

Painel: sessão Django (cookie HttpOnly), `X-CSRFToken` nas mutações; `GET /api/v1/csrf` devolve `{csrfToken}` e define o cookie. Selecione a empresa com `X-Tenant-ID` em toda rota de tenant. O servidor confere `Membership` em cada pedido; indicar outro tenant não concede acesso. Plataforma usa conta `is_staff`/`is_superuser` e rotas `/platform/`.

Os limites de tentativas de registro, login e pareamento são compartilhados no banco. Por padrão usam o endereço TCP do cliente. Atrás de proxy, configure **ambos** `PDV_TRUST_PROXY=1` e `PDV_TRUSTED_PROXY_CIDRS` com as faixas das conexões que podem entregar `X-Forwarded-For`; a API usa a última entrada IP válida apenas quando o peer imediato pertence a essas faixas. Com a lista vazia, cabeçalhos encaminhados são ignorados.

- `POST auth/register` `{company,name,email,password}` → `{user,tenant,membership}` e sessão iniciada.
- `POST auth/login` `{email,password}` → `{user,memberships}`.
- `POST auth/logout` → `{ok:true}`.
- `GET auth/me` → `{user,memberships}`. `user`: `{id,name,email,is_staff,is_superuser,platform_roles}`; `memberships`: `{id,tenant_id,tenant_name,role}`.
- `GET plans` público → `{results:[{id,slug,name,price_month_cents,price_year_cents,terminal_limit,shop_limit,features}]}`.

Listagens devolvem `{results:[...],next_cursor:null|"..."}`. Recursos em `GET/POST` coleção e `GET/PATCH/DELETE` detalhe, salvo indicação. Mutações exigem permissões por papel. Campos desconhecidos ou tipos inválidos são rejeitados.

## Dados da empresa

- `shops`: `{id,name,active,address}`.
- `categories`: `{id,name,active}`.
- `products`: `{id,sku,barcode,name,category_id,variant,unit,price_cents,cost_cents,stock_minimum_milli,tax_fields,active}`; `unit` em `UN|KG|L`. Filtro `?q=`.
- `customers`: `{id,name,document,email,phone,active}`.
- `suppliers`: `{id,name,document,email,phone,active}`.
- `stock?shop_id=<uuid>`: `{results:[{product_id,sku,name,unit,quantity_milli,stock_minimum_milli,below_minimum}]}`.
- `stock/movements`: GET `?shop_id=`; POST `{shop_id,product_id,delta_milli,reason,idempotency_key}` → movimento e saldo. Ajustes manuais só por gerente/estoque/administrador.
- `stock/receipts`: GET; POST `{shop_id,supplier_id?,reference?,idempotency_key,items:[{product_id,quantity_milli,unit_cost_cents?}]}` → entrada e movimentos atômicos.
- `finance`: GET/POST `{kind:'payable'|'receivable',description,amount_cents,due_date,customer_id?,supplier_id?}`; `POST finance/<id>/settle` `{paid_at?,amount_cents?}`. A primeira versão liquida o título integralmente. Exige recurso `finance` no plano.
- `users`: membros da empresa `{id,user_id,name,email,role,active,has_pin}`. POST `{name,email,password,role,pin?}`; PATCH `{name?,role?,active?}`; `POST users/<id>/pin` `{pin}`. O último proprietário ativo não pode ser desativado, removido ou rebaixado.
- `terminals`: GET; POST `{shop_id,name}` → terminal mais `pairing_code` utilizável uma vez por 10 minutos. PATCH `{name?,revoked?,reissue_pairing?}`. Criação e reativação obedecem ao limite do plano.
- `subscription`: GET → `{status,plan,trial_ends_at,current_period_end}`. Esse `status` é o estado gravado; uma assinatura `active` com período vencido produz licença efetiva `past_due` até o fim de 72 horas de tolerância. Após esse prazo, a API não renova licença nem aceita novas alterações de negócio. Eventos locais já enfileirados continuam aceitos de terminal não revogado.
- `sales`: GET `?shop_id=&limit=&cursor=` → resumo paginado; GET `<id>` → cabeçalho, itens, pagamentos e estado fiscal `not_issued`; `POST sales/<id>/cancel` ou `/refund` `{reason}` estorna o estoque uma vez (papel autorizado). Documento fiscal já autorizado exige cancelamento fiscal separado.
- `dashboard`: GET `?shop_id=&from=&to=` → `{sales_count,revenue_cents,average_ticket_cents,low_stock_count,open_payables_cents,open_receivables_cents,recent_sales}`. Exige `cloud_dashboard`; filtros de período exigem `reports`. Sem recurso `finance`, saldos financeiros retornam zero.
- `audit`: GET → ações recentes com usuário, recurso e data.

Papéis: owner, admin, manager, cashier, inventory, finance, viewer. Owner/admin administram pessoas e terminais; manager opera catálogo/estoque/vendas; inventory opera catálogo/estoque; finance opera títulos; cashier consulta catálogo e vendas; viewer só lê. Operações de alta sensibilidade (plano, papel owner, cancelamento, PIN) recebem checagem adicional. O admin de plataforma não ganha acesso automático aos dados de uma empresa por `X-Tenant-ID`.

## Plataforma

O superadministrador pode usar `GET/POST platform/users` e `GET/PATCH platform/users/<id>` para gerir equipe. POST recebe `{name,email,password,roles,active?}`; PATCH aceita campos parciais. `roles` é uma lista não vazia de `platform_support`, `platform_billing`, `platform_ops`. A resposta omite a senha. Um funcionário de plataforma não pode alterar superusuários por essa API.

- `platform_support`: lê empresas, assinaturas, terminais e auditoria; não consulta catálogos/clientes de tenants.
- `platform_billing`: lê/edita planos e assinaturas, lê faturas.
- `platform_ops`: lê/revoga terminais, lê auditoria; reativação respeita o limite contratado.
- Superadministrador: acesso total da plataforma, incluindo equipe e cadastro das empresas.

`GET platform/tenants`, `GET/POST platform/plans`, `PATCH platform/plans/<id>`, `GET platform/subscriptions`, `PATCH platform/subscriptions/<id>` (`{status?,plan_id?,trial_ends_at?,current_period_end?}`), `GET platform/terminals`, `PATCH platform/terminals/<id>` (`{revoked,reason}`), `GET platform/invoices`, `GET platform/audit`. A API registra alterações em auditoria. A política de acesso confere grupo **e** `is_staff`; `is_staff` sozinho não concede visão global.

## Terminal

- `POST terminal/pair` sem sessão, `{code,device_id,version}` → `{token,terminal_id,tenant_id,shop_id,lease,public_key}`. Código de pareamento de uso único; resposta de erro não revela validade específica. O token só aparece nessa resposta.
- `GET terminal/bootstrap?limit=500&cursor=0` com `Authorization: Bearer <token>` → `{tenant,shop,terminal,products,next_cursor,operators,lease,public_key}`. Cada produto inclui `quantity_milli` para a loja. Operadores contêm `{id,name,role,pin_hash}`; PIN separado da senha web. A lease é assinada por `shared/licensing.py`, vincula tenant, loja, terminal e dispositivo, e inclui hash da lista de operadores.
- `POST terminal/sync` com Bearer e `{events:[...]}` → `{accepted_event_ids:[...]}` apenas após commit de todo o lote. `event_id` é estável no reenvio, `aggregate_id` é o ID local do agregado, `identity` deve coincidir com token `{tenant_id,shop_id,terminal_id}`.
- Eventos `sale.completed` incluem `data:{request_id,cash_session_id,created_at,gross_cents,discount_cents,total_cents,change_cents,customer_id?,items:[{product_id,name,unit,quantity_milli,unit_price_cents,gross_cents,discount_cents,line_total_cents}],payments:[{method,amount_cents}]}`. Cada linha usa arredondamento HALF_UP de `unit_price_cents*quantity_milli/1000`.
- `sale.voided` usa `aggregate_id=<sale_id>` e `data:{sale_id,reason,created_at}`; reverte estoque uma vez. `cash.opened`, `cash.closed`, `cash.withdrawn`, `cash.supplied` têm esquema validado e são guardados como eventos nesta etapa.

O caixa baixa seu banco local ao concluir venda; a nuvem registra **uma** baixa por venda/produto ao receber `sale.completed`. Não envie essa baixa também como ajuste de estoque. Para a carga inicial, escolha **um único fluxo**: lance no SaaS por entrada/ajuste manual e sincronize o saldo no bootstrap; não lance o mesmo estoque novamente como `stock.adjusted` no caixa. O servidor aceita eventos já enfileirados quando a assinatura vence, desde que o dispositivo não tenha sido revogado. Rejeita produto de outro tenant, valor inconsistente e ID repetido com conteúdo distinto. Pode registrar saldo central negativo para venda offline já concluída (`stock_status=negative`), visível para reconciliação. A API é um protótipo funcional; emissão NFC-e, pagamentos e operação em escala precisam de homologação antes de venda em produção.
