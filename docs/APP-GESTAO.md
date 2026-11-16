# PDV2 Gestão — API do app e roteiro de publicação

O **PDV2 Gestão** é o aplicativo gratuito (iPhone e Android) para o empresário **acompanhar** a empresa: vendas, caixas, estoque, fiado e terminais. É só de consulta: não vende, não cobra e não mostra preço, plano nem assinatura. Quem decide quem usa o app é o **administrador do SaaS** (superusuário da CrownTech), na tela **Plataforma > Acessos ao app** do painel web.

Plano aprovado: `docs/auditoria-2026-11/app-gestao.md`. Código: `cloud/core/app_auth.py` (tokens, contexto, erros), `cloud/core/app_access.py` (liberação, revogação, exclusão de conta), `cloud/core/monitoring.py` (números), `cloud/core/views_app.py` (rotas do app), `cloud/core/views_app_admin.py` (rotas do painel e pública). Testes: `cloud/core/tests_app.py`.

---

## 1. Quem entra

| Pessoa | Precisa de | Verificação em duas etapas |
|---|---|---|
| Superusuário da CrownTech | nada (acesso implícito) | sempre (TOTP), mesmo com `PDV_PLATFORM_MFA_REQUIRED=0` |
| Equipe CrownTech (`is_staff` + grupo `platform_*`) | `AppAccess` escopo `platform` ativo | sempre (TOTP) |
| Dono, gerente ou outro membro de empresa cliente | `AppAccess` escopo `tenant` ativo **e** vínculo ativo na empresa **e** empresa ativa | não |

- O **papel** na empresa não decide: um dono sem liberação é recusado; um gerente liberado entra.
- A checagem roda no login, no refresh **e em cada requisição**: revogar vale na hora.
- A ativação do TOTP continua só no painel web; o app só pede o código.
- Nada no app depende de plano ou assinatura (leitura nunca é bloqueada por cobrança).
- Liberação tem lojas (vazio = todas as lojas ativas), permissões (`sales`, `cash`, `stock`, `credit`, `terminals`) e validade opcional (`expires_on`, vale até o fim do dia, fuso de São Paulo).
- Revogar a última liberação de alguém desconecta todos os aparelhos dessa pessoa.

## 2. Convenções

- **Base:** produção `https://pdv2.crowntech.com.br`; desenvolvimento `http://127.0.0.1:8000` (Android: `adb reverse tcp:8000 tcp:8000`).
- **Prefixo:** `/api/v1/app/...`, sem barra final.
- **Autenticação:** `Authorization: Bearer pa_...` em tudo, exceto `auth/login`, `auth/mfa`, `auth/refresh` e `auth/logout`. **Sem cookie e sem CSRF**; sessão do painel não vale nas rotas do app e o token do app não vale nas rotas do painel.
- **Empresa:** cabeçalho `X-Tenant-ID: <uuid>` nas rotas de dados. Ausente → 400 `tenant_required`.
- **Loja:** `?shop_id=<uuid>` opcional (ausente = todas as lojas liberadas).
- **Período:** `?period=today|yesterday|7d|month` (padrão `today`), fuso `America/Sao_Paulo`, sempre pela **data em que a venda foi feita** (`created_at`). `7d` = hoje e os 6 dias anteriores; `month` = do dia 1 até hoje.
- **Dinheiro:** centavos inteiros. **Quantidade:** milésimos (`quantity_milli`) + `unit` (`UN|KG|L`).
- **Data-hora:** `"2026-11-16T14:03:12-03:00"` (sem frações). **Data:** `"2026-11-16"`.
- **Paginação:** `?limit=1..100` (padrão 50) e `?cursor=` (opaco). Resposta `{"results": [...], "next_cursor": "50" | null}`.
- **Compatibilidade:** o servidor só acrescenta campos dentro da v1; os apps ignoram chaves desconhecidas.
- Respostas saem com `Cache-Control: private, no-store`.

### 2.1 Erros

Corpo: `{"error": "<frase para leigo>", "code": "<codigo>"}` (às vezes com extras).

| HTTP | code | O app faz |
|---|---|---|
| 400 | `invalid_request` | mostra `error` |
| 400 | `tenant_required` | abre o seletor de empresa |
| 400 | `invalid_period` | volta para Hoje |
| 401 | `invalid_credentials` | mostra no login |
| 401 | `unauthenticated` | apaga tokens e volta ao login |
| 401 | `token_expired` | renova (refresh) e repete uma vez |
| 401 | `refresh_invalid` | apaga tokens e volta ao login |
| 401 | `device_revoked` | apaga tokens; "Este aparelho foi desconectado da sua conta. Entre de novo." |
| 401 | `mfa_invalid` / `mfa_expired` | tela do código / volta ao login |
| 403 | `app_access_denied` | tela "Acesso ao app não liberado" |
| 403 | `mfa_setup_required` | "Ative a verificação em duas etapas no painel web..." |
| 403 | `tenant_forbidden` / `shop_forbidden` | recarrega `/me` / volta para "Todas as lojas" |
| 403 | `permission_denied` | estado vazio "Sem permissão" |
| 403 | `platform_forbidden` | mostra `error` |
| 404 | `not_found` | mostra `error` |
| 405 | `method_not_allowed` | mostra `error` |
| 409 | `access_exists` (+`access_id`), `member_required`, `superuser_implicit`, `conflict` | mostra `error` |
| 429 | `rate_limited` (+`retry_after_seconds`), `mfa_locked` | mostra `error` |
| 5xx | `unavailable` | "O servidor não respondeu como esperado. Tente de novo em instantes." |

### 2.2 Limites de tentativa

| Rota | Chave | Máximo / janela |
|---|---|---|
| `auth/login` | IP + e-mail (zera no sucesso) | 5 / 15 min |
| `auth/login` | IP | 30 / 15 min |
| `auth/mfa` | IP + usuário, e 5 erros travam a conta por 15 min | 5 / 15 min |
| `auth/refresh` | IP | 120 / 15 min |
| `account/deletion-request` | usuário | 3 / 24 h |
| `public/account-deletion` | IP | 5 / 1 h |

## 3. Tokens

- `access` = `pa_…` (15 min), `refresh` = `pr_…` (60 dias; 30 dias se o aparelho fez TOTP), desafio de MFA `pm_…` (10 min). O servidor guarda só o SHA-256.
- Cada refresh devolve um **par novo** e reinicia o prazo. O refresh anterior, usado de novo em até 30 s (resposta perdida), ainda recebe um par novo; depois de 30 s é tratado como roubo: o aparelho é revogado (`device_revoked`) e o evento `app.refresh_reuse_detected` vai para a auditoria.
- Até 10 aparelhos ativos por pessoa; no 11º login o aparelho usado há mais tempo é desconectado.
- O app guarda o refresh no Keychain/Keystore e o access só em memória; renova uma requisição por vez.

## 4. Rotas

Os exemplos JSON abaixo são as **fixtures compartilhadas**: o teste `cloud/core/tests_app.py` confere que as chaves batem e os apps iOS/Android decodificam exatamente estes exemplos.

### 4.1 Objetos comuns

```json
// UserRef
{"id": 12, "name": "Ana Souza", "email": "ana@mercadobompreco.com.br"}
// ShopRef
{"id": "a1b2c3d4-0001-4000-8000-000000000001", "name": "Loja Centro"}
// TokenPair
{"access_token": "pa_Q2x...", "access_expires_at": "2026-11-16T14:18:12-03:00", "access_expires_in": 900,
 "refresh_token": "pr_Zm9...", "refresh_expires_at": "2027-01-15T14:03:12-03:00"}
// Device (o próprio)
{"id": "d7e1c0a2-5b3f-4c11-9d2e-8a6f4b2c1e90", "platform": "ios", "name": "iPhone", "model": "iPhone16,2",
 "os_version": "iOS 26.1", "app_version": "1.0.0 (1)", "created_at": "2026-11-16T14:03:12-03:00",
 "last_seen_at": "2026-11-16T14:03:12-03:00", "current": true}
// TenantAccess
{"id": "3f6e1c2a-8b4d-4e2f-9c1a-5d7b0e3a2f10", "name": "Mercado Bom Preço", "all_shops": false,
 "shops": [{"id": "a1b2c3d4-0001-4000-8000-000000000001", "name": "Loja Centro"}],
 "permissions": ["sales", "cash", "stock", "credit", "terminals"], "access_expires_on": null}
// Me
{"user": {"id": 12, "name": "Ana Souza", "email": "ana@mercadobompreco.com.br"},
 "device": { "...": "Device" },
 "platform": null,
 "tenants": [ { "...": "TenantAccess" } ],
 "links": {"privacy_url": "https://pdv2.crowntech.com.br/privacidade/",
           "support_url": "https://pdv2.crowntech.com.br/suporte/",
           "account_deletion_url": "https://pdv2.crowntech.com.br/excluir-conta/"},
 "server_time": "2026-11-16T14:03:12-03:00"}
// Me.platform (só com TOTP feito neste login)
{"enabled": true, "superuser": true, "can_manage_access": true,
 "roles": ["platform_billing", "platform_ops", "platform_support"]}
```

`all_shops=true` ⇒ `shops` traz todas as lojas ativas. `Me.tenants` lista só liberações de empresa (vazia para quem é só da plataforma). Os links vêm de `PDV_PUBLIC_URL`.

### 4.2 Entrada e sessão

| Rota | Corpo | Resposta |
|---|---|---|
| `POST auth/login` | `{"email","password","device":{"platform":"ios|android","name","model?","os_version?","app_version"}}` | `{"status":"ok","tokens":TokenPair,"me":Me}` ou `{"status":"mfa_required","mfa_token":"pm_…","mfa_expires_at":"…","user":{"name","email"}}` |
| `POST auth/mfa` | `{"mfa_token","code"}` ou `{"mfa_token","recovery_code"}` | igual ao login `ok` (`me.platform` preenchido) |
| `POST auth/refresh` | `{"refresh_token"}` | `{"tokens":TokenPair}` |
| `POST auth/logout` | Bearer (aceita vencido) e/ou `{"refresh_token"}` | sempre `{"ok":true}` |
| `GET me` | — | `Me` |
| `GET tenants?q=&limit=&cursor=` | — | `{"results":[TenantAccess],"next_cursor"}` (plataforma: todas as empresas) |
| `GET devices` | — | `{"results":[Device],"next_cursor":null}` |
| `DELETE devices/<id>` | — | `{"ok":true}` (só aparelhos da própria pessoa) |
| `POST account/deletion-request` | `{"note?"}` | 202 `{"status":"received","message":"Recebemos seu pedido. …"}`; desconecta todos os aparelhos |

### 4.3 Dados da empresa (com `X-Tenant-ID`)

**`GET dashboard?period=&shop_id=`** — cada seção sem permissão vem `null`.

```json
{"generated_at": "2026-11-16T14:03:12-03:00",
 "tenant": {"id": "3f6e1c2a-8b4d-4e2f-9c1a-5d7b0e3a2f10", "name": "Mercado Bom Preço"},
 "shop": null,
 "shops": [{"id": "a1b2c3d4-0001-4000-8000-000000000001", "name": "Loja Centro"},
           {"id": "a1b2c3d4-0002-4000-8000-000000000002", "name": "Loja Bairro"}],
 "period": {"key": "today", "label": "Hoje", "date_from": "2026-11-16", "date_to": "2026-11-16",
            "from": "2026-11-16T00:00:00-03:00", "to": "2026-11-17T00:00:00-03:00"},
 "permissions": ["sales", "cash", "stock", "credit", "terminals"],
 "sales": {"count": 42, "revenue_cents": 352000, "average_ticket_cents": 8380,
           "cancelled_count": 2, "cancelled_cents": 5400, "returns_cents": 1200, "net_revenue_cents": 350800},
 "payments": {"cash_cents": 98000, "pix_cents": 150000, "card_credit_cents": 60000, "card_debit_cents": 30000,
              "other_cents": 4000, "received_total_cents": 342000,
              "credit_account_cents": 8000, "store_credit_cents": 2000, "credit_received_cents": 5000},
 "series": {"kind": "hour", "points": [{"key": "00", "label": "00h", "count": 0, "revenue_cents": 0},
                                       {"key": "14", "label": "14h", "count": 6, "revenue_cents": 51000}]},
 "cash": {"open_count": 2, "closed_count": 3, "shortage_count": 1, "shortage_cents": -1500},
 "stock": {"zeroed_count": 3, "below_minimum_count": 7},
 "credit": {"open_cents": 120000, "overdue_cents": 30000, "overdue_customers": 4},
 "terminals": {"total": 3, "online": 2, "offline": 1, "unpaired": 0, "online_window_seconds": 300, "conflicts_open": 1}}
```

Regras (em `core/monitoring.py`, as mesmas do `GET /api/v1/owner/monitoring`):
- **Vendas:** concluídas com `created_at` no período. Ticket médio = faturamento ÷ vendas. Cancelamentos = `cancelled` + `refunded` do período. Devoluções = `SaleReturn` do período; líquido = faturamento − devoluções.
- **Recebido:** dinheiro **menos troco**; Pix; crédito; débito; "outras" (cartão genérico antigo e formas da empresa). Fiado (`credit_account`) e crédito da loja (`store_credit`) **não** são dinheiro recebido. Sempre vale: `revenue_cents = received_total_cents + credit_account_cents + store_credit_cents`. `credit_received_cents` = fiado pago no caixa no período (fora do total).
- **Gráfico:** Hoje/Ontem = 24 pontos por hora; 7 dias/Mês = um ponto por dia (`"10/11"`), com zeros.
- **Caixas:** abertos agora; fechados no período; quebras = fechados no período com diferença negativa (soma negativa).
- **Estoque:** pares loja × produto ativo e controlado. Zerado = saldo ≤ 0, ou sem saldo e com mínimo; abaixo do mínimo = 0 < saldo < mínimo.
- **Fiado:** títulos de venda a prazo em aberto (não depende do período); vencido = vencimento antes de hoje. Com acesso restrito a lojas, títulos sem loja ficam de fora.
- **Terminais:** sem revogados; `total` = pareados; online = contato nos últimos 5 min; conflitos em aberto.

| Rota | Permissão | Resposta |
|---|---|---|
| `GET sales?period=&shop_id=&status=all|completed|cancelled` | sales | lista paginada (`id, created_at, shop, terminal_name, station_name, operator_name, customer_name, total_cents, status, items_count, payment_methods`) |
| `GET sales/<id>` | sales | detalhe com `items`, `payments` (`label`), `returns` (`refund_label`), `cancelled_at`, `cancellation_reason` |
| `GET cash-sessions?status=open|closed&period=&shop_id=` | cash | lista paginada (`sales_count, sales_total_cents, cash_net_cents, supplied_cents, withdrawn_cents, expected/counted/difference_cents, incomplete`) |
| `GET cash-sessions/<id>` | cash | + `closed_by_name, supervisor_name, credit_received_cents, by_method, movements` |
| `GET stock/alerts?status=all|zeroed|below_minimum&shop_id=` | stock | `{"counts":{"zeroed","below_minimum"},"results":[…],"next_cursor"}` |
| `GET credit?status=open|overdue&shop_id=` | credit | `{"totals":{…},"results":[por cliente],"next_cursor"}` |
| `GET terminals?shop_id=` | terminals | `{"online_window_seconds":300,"results":[…],"next_cursor":null}` |
| `GET conflicts?status=open|all&shop_id=` | terminals | lista com `reason_text` e `status_text` vindos do servidor |

Venda ou caixa de loja não liberada responde 404 (não revela que existe). Exemplos de cada linha no Apêndice A.

Rótulos fixos usados pelos apps: pagamento `cash` Dinheiro · `pix` Pix · `card_credit` Cartão de crédito · `card_debit` Cartão de débito · `credit_account` Fiado · `store_credit` Crédito da loja · `card` Cartão · `other` nome da forma ou "Outra forma". Venda `completed` Concluída · `cancelled` Cancelada · `refunded` Estornada. Terminal `online` Online · `offline` Sem contato · `unpaired` Não pareado. Movimento `withdrawn` Sangria · `supplied` Suprimento.

### 4.4 Plataforma no app (TOTP feito neste login)

| Rota | Quem | Resposta |
|---|---|---|
| `GET platform/tenants?q=&health=all|attention` | qualquer papel de plataforma | empresas com situação (`subscription_status_text`), terminais sem contato, conflitos, última venda e saúde (`ok`/`attention`/`inactive`); sem preço nem link de cobrança |
| `GET/POST platform/access` | superusuário | lista / libera (`AppAccessObj`, 201) |
| `PATCH platform/access/<id>` | superusuário | altera `shop_ids, permissions, expires_on, reason` |
| `POST platform/access/<id>/revoke` `{"reason"}` | superusuário | revoga |
| `GET platform/access/options?tenant_id=` | superusuário | lojas e membros (com `has_active_access`); sem `tenant_id`, a equipe CrownTech |
| `GET platform/devices?status=&q=` | superusuário | aparelhos (`PlatformDevice`) |
| `POST platform/devices/<id>/revoke` `{"reason"}` | superusuário | desconecta |

```json
// AppAccessObj
{"id": "6e5d4c3b-2a19-4f8e-9d7c-6b5a49382716", "scope": "tenant",
 "user": {"id": 12, "name": "Ana Souza", "email": "ana@mercadobompreco.com.br"},
 "tenant": {"id": "3f6e1c2a-8b4d-4e2f-9c1a-5d7b0e3a2f10", "name": "Mercado Bom Preço"},
 "all_shops": false, "shops": [{"id": "a1b2c3d4-0001-4000-8000-000000000001", "name": "Loja Centro"}],
 "permissions": ["sales", "cash", "stock", "credit", "terminals"],
 "reason": "Dona da empresa pediu acesso ao app", "granted_by": {"id": 1, "name": "Bruno", "email": "admin@crowntech.com.br"},
 "granted_at": "2026-11-16T14:00:00-03:00", "expires_on": null,
 "status": "active", "status_text": "Ativo",
 "revoked_at": null, "revoked_by": null, "revoke_reason": "", "devices_active": 1}
// Corpo da liberação
{"scope": "tenant", "user_id": 12, "tenant_id": "3f6e1c2a-8b4d-4e2f-9c1a-5d7b0e3a2f10",
 "shop_ids": [], "permissions": ["sales", "cash", "stock", "credit", "terminals"],
 "expires_on": null, "reason": "Dona da empresa pediu acesso ao app"}
// Equipe CrownTech
{"scope": "platform", "user_id": 7, "expires_on": null, "reason": "Atendimento"}
```

Regras: motivo de 3 a 240 caracteres; pelo menos uma permissão; só lojas ativas da empresa; validade de hoje em diante. Pessoa fora da equipe da empresa → 409 `member_required`; já liberada → 409 `access_exists` (+`access_id`); superusuário → 409 `superuser_implicit`; equipe CrownTech precisa ser `is_staff` com papel de plataforma.

### 4.5 Painel web e páginas públicas

- Painel (sessão + CSRF, só superusuário): `GET/POST /api/v1/platform/app-access`, `PATCH …/<id>`, `POST …/<id>/revoke`, `GET …/options`, `GET /api/v1/platform/app-devices`, `POST …/<id>/revoke`, `GET /api/v1/platform/account-deletions?status=pending|all`, `POST …/<id>/resolve` `{"status":"done"|"rejected","note"}`.
- Tela: **Plataforma > Acessos ao app** (`/platform/#app_access`) com Acessos, Aparelhos conectados e Pedidos de exclusão de conta.
- Pública: `POST /api/v1/public/account-deletion` `{"email","note?"}` → sempre 202 com a mesma mensagem; não desconecta ninguém (só o próprio titular, pelo app, faz isso).
- Páginas: `/privacidade/`, `/suporte/`, `/excluir-conta/` (também no sitemap).

### 4.6 Exclusão de conta

1. A pessoa pede no app (**Mais > Conta > Excluir minha conta**) ou em `/excluir-conta/`.
2. Pelo app: todos os aparelhos saem na hora. Pela página: só o pedido fica registrado.
3. O superusuário confere (de preferência respondendo ao e-mail da conta) e marca **Atendido** no painel em até 15 dias: o login é desativado e todos os acessos e aparelhos são revogados. Superusuário não pode ser desativado por aí.
4. Dados pessoais apagados; dados fiscais e de vendas da empresa ficam pelo prazo legal.

### 4.7 Auditoria

`app.login`, `app.login_denied`, `app.mfa_verified`, `app.logout`, `app.refresh_reuse_detected`, `app.device_revoked`, `app.access_granted`, `app.access_updated`, `app.access_revoked`, `app.account_deletion_requested`, `app.account_deletion_resolved`, `app.platform_tenant_viewed` (no máximo 1 vez a cada 30 min por pessoa e empresa). Nenhum detalhe guarda token, senha, código ou hash.

---

## 5. Roteiro de publicação nas lojas

> O app é **grátis para quem baixa**, mas publicar tem custo para a CrownTech: Apple Developer Program **US$ 99 por ano** e Google Play Console **US$ 25 uma vez** (valores em dólar; confira o câmbio e os impostos no cartão). Esse pagamento é do dono.

### 5.1 Antes de tudo (dono)

1. **D-U-N-S da CrownTech** (gratuito, pode levar dias): exigido pela Apple e pelo Google para conta de **Organização**. Peça primeiro.
2. Domínio e e-mail: `https://pdv2.crowntech.com.br` no ar com HTTPS; confirme o e-mail de suporte (`PDV_SUPPORT_EMAIL`, padrão `suporte@crowntech.com.br`).
3. Aprovar o texto de `/privacidade/` e `/excluir-conta/` (rascunhos em `web/privacidade.html` e `web/excluir-conta.html`).
4. Conta de demonstração para a revisão: uma empresa de teste com vendas, e um usuário **de empresa** (sem verificação em duas etapas) com acesso liberado em **Acessos ao app**. Anote e-mail e senha para as notas de revisão.

### 5.2 Apple (App Store)

1. Inscrever a CrownTech no **Apple Developer Program** como Organização (US$ 99/ano) e aceitar os contratos (app grátis dispensa contrato bancário).
2. Informar o **Team ID** no Xcode (`DEVELOPMENT_TEAM`) e criar o identificador `br.com.crowntech.pdv2gestao`.
3. No App Store Connect, criar o app: nome **PDV2 Gestão**, idioma pt-BR, categoria **Negócios**, preço **Grátis**, disponível só no **Brasil**.
4. Ficha: subtítulo "Acompanhe suas lojas PDV2"; descrição e palavras-chave em `ios/PDV2OwnerMonitor/store/apple/`; URLs de suporte, privacidade e marketing.
5. **Capturas**: iPhone 6,9" (1320×2868) e 6,3" (1206×2622), mostrando o Resumo com dados (não a tela de login).
6. **Privacidade** (rótulos): Contato (nome, e-mail) e Identificadores (ID do usuário, ID do dispositivo), vinculados à pessoa, para funcionalidade do app, **sem rastreamento**. Classificação 4+.
7. Assinar e enviar o build (Xcode > Archive) para o **TestFlight**; testar internamente.
8. Notas de revisão com a conta demo (ver §5.4) e clicar em **Enviar para revisão**.

### 5.3 Google (Play)

1. Criar a conta **Play Console** como Organização (US$ 25, uma vez) e concluir as verificações de identidade. Conta pessoal exigiria 12 testadores por 14 dias antes de publicar.
2. Criar o app **PDV2 Gestão** (grátis, app), pacote `br.com.crowntech.pdv2gestao`; aceitar o **Play App Signing** e criar a chave de **upload** (guardar fora do repositório; `android/PDV2Gestao/keystore.properties`).
3. Ficha: descrição curta e longa em `android/PDV2Gestao/store/google/`; ícone 512×512; imagem de destaque 1024×500; ao menos 4 capturas 1080×1920.
4. Formulários: **Acesso ao app** (credenciais da conta demo), **Anúncios: não**, classificação de conteúdo (Livre), **público-alvo: adultos (18+)**, **Segurança dos dados**: informações pessoais (nome, e-mail) e IDs do dispositivo, coletados para funcionalidade e gestão de conta, criptografados em trânsito, sem compartilhamento, exclusão por solicitação com o link `https://pdv2.crowntech.com.br/excluir-conta/`.
5. Gerar o AAB assinado (`./gradlew bundleRelease`), enviar para **teste interno**, depois teste fechado, e promover para **produção**.

### 5.4 Textos comuns

- **Descrição curta (Play, até 80):** "Acompanhe vendas, caixas e estoque das lojas que usam o PDV2."
- **Descrição longa:** "O PDV2 Gestão é o complemento gratuito do PDV2 para quem administra a empresa. Acompanhe pelo celular, em tempo real, o faturamento, as vendas, o ticket médio e os cancelamentos de cada loja; veja quanto entrou em dinheiro, Pix e cartão; confira os caixas abertos, sangrias e quebras; saiba quais produtos estão zerados ou abaixo do mínimo; acompanhe o fiado em aberto e vencido; e veja se os terminais estão online. Filtre por empresa, loja e período (hoje, ontem, 7 dias ou mês). O app é só de consulta e protegido por Face ID ou biometria. O acesso é liberado pelo administrador do PDV2: entre com o mesmo e-mail e senha do painel web."
- **Palavras-chave (Apple, até 100):** `pdv,vendas,caixa,estoque,loja,gestão,faturamento,fiado,comércio,varejo`
- **URLs:** suporte `https://pdv2.crowntech.com.br/suporte/` · privacidade `https://pdv2.crowntech.com.br/privacidade/` · exclusão `https://pdv2.crowntech.com.br/excluir-conta/` · marketing `https://pdv2.crowntech.com.br/`. Copyright "© 2026 CrownTech".
- **Notas de revisão (rascunho):** "Aplicativo gratuito e somente leitura para empresas clientes do PDV2. As empresas se cadastram no site; o administrador libera o uso do app. Conta de demonstração: e-mail [PREENCHER], senha [PREENCHER], sem verificação em duas etapas. Não há compras nem conteúdo pago."

### 5.5 Riscos de recusa e como evitar

| Risco | Como evitar |
|---|---|
| Revisor não consegue entrar (Apple 2.1) | servidor de produção no ar, conta demo de empresa sem TOTP, liberação ativa e sem validade curta |
| Painel vazio | empresa demo com vendas recentes |
| Compra fora do app (Apple 3.1.3) | nenhum preço, plano, assinatura ou link de cobrança no app; a API do app não expõe nada disso |
| Privacidade (Apple 5.1.1, Play) | política publicada, exclusão de conta no app **e** na web |
| App para público restrito (Apple 3.2) | ficha como "complemento gratuito para clientes do PDV2"; notas explicando que o acesso é liberado pelo administrador |

### 5.6 Checklist do servidor antes de enviar

- [ ] `PDV_PUBLIC_URL=https://pdv2.crowntech.com.br` e `PDV_SUPPORT_EMAIL` definidos no ambiente.
- [ ] Migração `0041_app_gestao` aplicada.
- [ ] `/privacidade/`, `/suporte/` e `/excluir-conta/` abrindo em HTTPS.
- [ ] Conta demo liberada em **Plataforma > Acessos ao app** e testada no app de produção.
- [ ] Superusuário com TOTP ativo (o app sempre pede o código para a plataforma).

---

## Apêndice A. Exemplos das listas e detalhes (fixtures)

```json
// GET sales
{"results": [{"id": "5c0e9a1b-2d3f-4e5a-8b7c-1d2e3f4a5b6c", "created_at": "2026-11-16T13:58:40-03:00",
              "shop": {"id": "a1b2c3d4-0001-4000-8000-000000000001", "name": "Loja Centro"},
              "terminal_name": "Caixa 1", "station_name": "", "operator_name": "Carla", "customer_name": "",
              "total_cents": 4590, "status": "completed", "items_count": 2, "payment_methods": ["pix"]}],
 "next_cursor": null}
// GET sales/<id>
{"id": "5c0e9a1b-2d3f-4e5a-8b7c-1d2e3f4a5b6c", "created_at": "2026-11-16T13:58:40-03:00",
 "received_at": "2026-11-16T13:58:43-03:00", "status": "completed",
 "shop": {"id": "a1b2c3d4-0001-4000-8000-000000000001", "name": "Loja Centro"},
 "terminal_name": "Caixa 1", "station_name": "", "operator_name": "Carla", "seller_name": "", "customer_name": "",
 "gross_cents": 4800, "promo_discount_cents": 110, "discount_cents": 100, "total_cents": 4590, "change_cents": 0,
 "items": [{"line": 1, "name": "Arroz 5 kg", "unit": "UN", "quantity_milli": 1000, "unit_price_cents": 2890,
            "gross_cents": 2890, "promo_discount_cents": 0, "discount_cents": 100, "line_total_cents": 2790}],
 "payments": [{"method": "pix", "label": "Pix", "amount_cents": 4590}],
 "returns": [], "cancelled_at": null, "cancellation_reason": ""}
// returns[]: {"id","created_at","total_cents","refund_method","refund_label","reason"}
// GET cash-sessions
{"results": [{"id": "c4a1e2b3-7d8f-4a9b-8c0d-1e2f3a4b5c6d",
              "shop": {"id": "a1b2c3d4-0001-4000-8000-000000000001", "name": "Loja Centro"},
              "terminal_name": "Caixa 1", "station_name": "", "operator_name": "Carla", "status": "open",
              "opened_at": "2026-11-16T08:01:00-03:00", "closed_at": null, "opening_cents": 20000,
              "sales_count": 18, "sales_total_cents": 154000, "cash_net_cents": 43000,
              "supplied_cents": 0, "withdrawn_cents": 10000,
              "expected_cents": null, "counted_cents": null, "difference_cents": null, "incomplete": false}],
 "next_cursor": null}
// GET cash-sessions/<id>: os campos acima e mais
{"closed_by_name": "", "supervisor_name": "", "credit_received_cents": 0,
 "by_method": [{"method": "cash", "label": "Dinheiro", "amount_cents": 43000}],
 "movements": [{"id": "e1f2a3b4-1111-4c2d-8e3f-0a1b2c3d4e5f", "kind": "withdrawn", "label": "Sangria",
                "amount_cents": 10000, "reason": "Depósito no banco", "operator_name": "Carla",
                "created_at": "2026-11-16T12:30:00-03:00"}]}
// GET stock/alerts
{"counts": {"zeroed": 3, "below_minimum": 7},
 "results": [{"shop": {"id": "a1b2c3d4-0001-4000-8000-000000000001", "name": "Loja Centro"},
              "product_id": "0f1e2d3c-4b5a-4968-8776-655443322110", "name": "Leite integral 1 L",
              "sku": "7891000100103", "unit": "UN", "quantity_milli": 0, "minimum_milli": 12000, "status": "zeroed"}],
 "next_cursor": null}
// GET credit
{"totals": {"open_cents": 120000, "overdue_cents": 30000, "customers": 12, "overdue_customers": 4},
 "results": [{"customer_id": "9a8b7c6d-5e4f-4a3b-9c2d-1e0f9a8b7c6d", "name": "João da Silva", "phone": "21999990000",
              "open_cents": 15000, "overdue_cents": 9000, "oldest_due_date": "2026-10-20", "entries_count": 3}],
 "next_cursor": null}
// GET terminals
{"online_window_seconds": 300,
 "results": [{"id": "7b6a5c4d-3e2f-4a1b-9c8d-7e6f5a4b3c2d", "name": "Caixa 1",
              "shop": {"id": "a1b2c3d4-0001-4000-8000-000000000001", "name": "Loja Centro"},
              "status": "online", "version": "2.1.0", "last_seen_at": "2026-11-16T14:02:50-03:00",
              "pending_events": 0, "error": ""}],
 "next_cursor": null}
// GET conflicts
{"results": [{"id": "2c3d4e5f-6a7b-4c8d-9e0f-1a2b3c4d5e6f", "created_at": "2026-11-16T10:15:00-03:00",
              "shop": {"id": "a1b2c3d4-0001-4000-8000-000000000001", "name": "Loja Centro"},
              "terminal_name": "Caixa 2", "reason": "negative_stock", "reason_text": "Estoque negativo",
              "status": "open", "status_text": "Em aberto"}],
 "next_cursor": null}
// GET platform/tenants
{"results": [{"id": "3f6e1c2a-8b4d-4e2f-9c1a-5d7b0e3a2f10", "name": "Mercado Bom Preço", "active": true,
              "subscription_status": "active", "subscription_status_text": "Ativa",
              "shops_count": 2, "terminals_total": 3, "terminals_offline": 1, "conflicts_open": 1,
              "last_sale_at": "2026-11-16T13:58:40-03:00", "last_seen_at": "2026-11-16T14:02:50-03:00",
              "health": "attention", "health_text": "Precisa de atenção"}],
 "next_cursor": "50"}
// GET platform/access/options?tenant_id=
{"tenant": {"id": "3f6e1c2a-8b4d-4e2f-9c1a-5d7b0e3a2f10", "name": "Mercado Bom Preço"},
 "shops": [{"id": "a1b2c3d4-0001-4000-8000-000000000001", "name": "Loja Centro"}],
 "members": [{"user_id": 12, "name": "Ana Souza", "email": "ana@mercadobompreco.com.br",
              "role": "owner", "role_text": "Proprietário", "has_active_access": false}]}
// PlatformDevice
{"id": "d7e1c0a2-5b3f-4c11-9d2e-8a6f4b2c1e90", "platform": "android", "name": "Samsung SM-S918B", "model": "SM-S918B",
 "os_version": "Android 15", "app_version": "1.0.0 (1)", "created_at": "2026-11-16T14:03:12-03:00",
 "last_seen_at": "2026-11-16T14:03:12-03:00", "last_ip": "177.10.20.30",
 "user": {"id": 12, "name": "Ana Souza", "email": "ana@mercadobompreco.com.br"},
 "status": "active", "revoked_at": null, "revoke_reason": ""}
```
