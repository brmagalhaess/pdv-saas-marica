# Plano único: finalizar o PDV2 Gestão e publicar na App Store e na Google Play

**Antes de tudo, sobre "totalmente grátis":** o app sai gratuito para quem baixa, sem compra interna, sem preço e sem link de assinatura, e o plano usa só ferramentas gratuitas. Publicar com custo zero não é possível: a Apple cobra US$ 99/ano e o Google cerca de US$ 25 uma vez (valores dos relatórios, não confirmados em reais). Esse pagamento é do dono.

Nada foi compilado nem testado; o plano vem dos quatro relatórios e de uma conferência pontual de linhas no repositório.

## 1. Decisão de tecnologia

**Manter o iOS em SwiftUI e escrever o Android nativo em Kotlin + Jetpack Compose.**
- O iOS já existe (~900 linhas) e a regra de negócio fica no servidor (`cloud/core/views_owner.py:45-124`), então duplicar a interface custa pouco.
- O Mac já tem tudo (Xcode, Android SDK 36, JDK 17); Flutter exigiria instalar ferramenta e reescrever o iOS.
- Empacotar a PWA está descartado: leva "Solicitar assinatura" (`web/assets/app.js:2582-2587`) para dentro do app e cai na diretriz 4.2 da Apple.

No Android, usar Ktor, kotlinx.serialization e kotlinx-datetime, para deixar aberta a migração a Kotlin Multiplatform se o app passar de 12–15 telas.

## 2. Modelo de acesso "o admin do SaaS libera usuários"

Hoje o papel `owner` está fixo no servidor (`cloud/core/views_owner.py:48`) e no app (`AppSession.swift:33`), e o admin do SaaS nem entra (`cloud/core/views_auth.py:178-186`). Fica assim:

- **`AppAccess`** (novo modelo): `user`, `scope` (`platform` ou `tenant`), `tenant`, `shops` (vazio = todas), `permissions` (vendas, caixa, estoque, fiado, terminais), `granted_by`, `granted_at`, `reason`, `expires_at`, `revoked_at`, `revoked_by`, `revoke_reason`. Único por usuário + empresa enquanto ativo.
- **`AppDevice`** (novo modelo): `user`, `platform`, `name`, `app_version`, `access_hash`, `refresh_hash`, `mfa_verified_at`, `last_seen_at`, `last_ip`, `revoked_at`. Campos de push ficam nulos até a v1.1.
- **Quem entra:**
  - Superusuário da plataforma: acesso implícito, sempre com TOTP.
  - Demais contas da equipe CrownTech: precisam de `AppAccess` com escopo `platform`.
  - Dono ou gerente de loja cliente: precisa de `AppAccess` com escopo `tenant`; o papel no `Membership` (`cloud/core/models.py:95`) deixa de decidir.
- **Sem liberação ativa:** o login responde 403 "Acesso ao app não liberado" e não cria dispositivo.
- **Revogar** derruba os dispositivos do usuário na hora. Concessão e revogação passam por `audit()` (`cloud/core/api_common.py:212`).
- **Recurso de plano:** os endpoints do app não exigem `cloud_dashboard` (`views_owner.py:49`). A liberação do admin é a única trava, e a mensagem "Recurso não incluído no plano" nunca aparece no app.
- **Autenticação por token de dispositivo**, no padrão SHA-256 do terminal (`cloud/core/views_terminal.py:104-115`): access de ~15 min e refresh rotativo de 30–60 dias. Sessão + CSRF não serve: o cookie dura 8 h e some ao fechar (`cloud/pdvcloud/settings.py:101-102`), e o POST em HTTPS sem `Referer` deve dar 403.

**Divergências entre relatórios, com a escolha:**
- **CSRF:** um relatório aceita "enviar Referer" como alternativa. Escolho o token, que também resolve persistência, biometria e revogação por aparelho.
- **Prefixo da API:** `/api/v1/app/*` contra `/api/v1/mobile/*`. Escolho `/api/v1/app/*`, em `cloud/core/views_app.py`.
- **Token só de leitura contra "gestão completa":** a v1.0 é leitura, com uma única exceção de escrita (liberar/revogar acesso e dispositivo, só para escopo `platform` com MFA). Cancelar venda e dar baixa ficam para a v1.1.
- **Linhas da assinatura no Xcode:** as corretas são `project.pbxproj:163-164, 175-176, 187-188, 204-205` (conferidas; um relatório citava 171-172 e 203-204).

## 3. Escopo da v1.0

**Comum aos dois perfis**
- Login com e-mail e senha; desbloqueio por Face ID/biometria nas aberturas seguintes.
- Tela "Conta": nome, dispositivos conectados (com revogar), política de privacidade, suporte, solicitar exclusão da conta, sair.
- Indicador de "atualizado há X" no lugar do selo fixo "AO VIVO" (`OwnerDashboardView.swift:96`).

**Usuário liberado (dono/gerente da loja cliente)**: somente leitura, só as lojas e permissões liberadas
- Seletor de empresa, loja e período (hoje, ontem, 7 dias, mês).
- **Resumo:** faturamento, vendas, ticket médio, cancelamentos, recebido por forma de pagamento, caixas abertos, alertas de estoque e fiado.
- **Vendas:** lista do período e detalhe da venda.
- **Caixa:** sessões abertas e fechadas, sangrias e quebras.
- **Estoque:** itens abaixo do mínimo e zerados.
- **Mais:** fiado em aberto e vencido, terminais (online/offline, versão), conflitos de sincronização.

**Admin do SaaS (equipe CrownTech)**: tudo acima para qualquer empresa, mais a aba "Plataforma"
- Login com código TOTP. A configuração inicial do MFA continua no painel web; o app só verifica.
- **Empresas:** lista com situação e saúde (terminais offline, conflitos, último uso); tocar abre a visão da empresa em leitura, com registro na auditoria.
- **Acessos ao app:** listar, liberar (usuário, empresa, lojas, permissões, validade, motivo) e revogar.
- **Dispositivos:** listar e revogar.
- A mesma tela de liberação entra no painel web `/platform`.

**Fora da v1.0:** push e alertas, metas, ações de escrita nas lojas, cache offline, iPad, qualquer tela de plano, preço ou cobrança.

## 4. Backend, em ordem

1. **Corrigir os números do monitoramento** e extrair para um módulo comum: filtrar por `created_at` em vez de `received_at` (`views_owner.py:61-62`), abater troco e tirar fiado/crédito de loja de "recebimentos" (`views_owner.py:28-41`), alinhar contagem de terminais (`:57` contra `:95-108`).
2. **Modelos e migração** de `AppAccess` e `AppDevice`.
3. **Autenticação do app:** `POST /api/v1/app/auth/login|mfa|refresh|logout` e `GET /api/v1/app/me`, sem CSRF e com limite de taxa. `require_user` (`api_common.py:120`) passa a aceitar o token; `platform_user` (`api_common.py:178-180`) aceita `mfa_verified_at` do dispositivo; token do app recusa `write=True` e métodos diferentes de GET, salvo a exceção da seção 2.
4. **Liberação de acesso:** `GET/POST /api/v1/platform/app-access`, `PATCH/DELETE .../<id>`, `GET /api/v1/platform/app-devices`, `POST .../<id>/revoke`, mais a tela no painel web.
5. **Leitura para o app:**
   - `GET /api/v1/app/overview?date=&shop_id=` agregado por loja, respeitando `AppAccess.shops`.
   - Filtro de data em vendas (`views_sales.py:24-36`).
   - Fiado com nome do cliente e vencidos (`views_customer_credit.py:15-22`).
   - Estoque "abaixo do mínimo/zerado" (`views_stock.py:29-49`).
   - Sessões de caixa sem as ~6 consultas por linha (`cash_logic.py:67-91`).
   - Resumo de plataforma (empresas e saúde).
6. **Conta e páginas públicas:** `GET/DELETE /api/v1/app/devices`, pedido de exclusão de conta, e páginas `/privacidade`, `/suporte` e `/excluir-conta` (hoje `web/` não tem nenhuma).
7. **Empresa de demonstração:** comando que cria a empresa, o usuário liberado e os terminais, mais rotina diária em `operations/worker.py` gerando vendas, para o revisor nunca ver painel vazio.
8. **Contrato e testes:** `mobile/contract/openapi.yaml` com fixtures JSON usadas pelo Django, iOS e Android; testes de troco, fiado, venda atrasada, token expirado, reuso de refresh e acesso revogado (hoje há um teste só, `cloud/core/tests.py:265-292`).
9. **Produção em HTTPS** com domínio definitivo (`infra/production.env.example:1` ainda é placeholder).

## 5. App iOS, em ordem

1. **Projeto:**
   - Bundle id e nome definitivos ("PDV2 Gestão"), com `Info.plist:10` usando `$(PRODUCT_BUNDLE_IDENTIFIER)`.
   - Só iPhone na v1.0 (`project.pbxproj:196, 213`).
   - Região pt-BR (`project.pbxproj:113`).
   - `MARKETING_VERSION`, dSYM de Release, scheme compartilhado e alvo de testes.
   - `ITSAppUsesNonExemptEncryption = false`.
2. **Servidor:** URL HTTPS fixa por configuração de build; campo "Configuração do servidor" (`RootView.swift:86-104`) e `NSAllowsLocalNetworking` (`Info.plist:23-27`) só em Debug; recusar `http` em Release (`APIClient.swift:32`).
3. **Núcleo:** cliente com token Bearer e renovação automática, refresh no Keychain, desbloqueio por Face ID, mensagens de erro corretas para 401/403/rede (`APIClient.swift:14, 93-95`).
4. **Entrada:** login, tela de TOTP, tela "acesso não liberado" com logout limpo (hoje a sessão fica órfã, `AppSession.swift:33-35`).
5. **Navegação:** abas, seletor de empresa/loja/período.
6. **Telas de leitura** da seção 3, depois a aba Plataforma, depois Conta.
7. **Correções visuais:** modo escuro (`DesignSystem.swift:8-9`), tamanho de texto do sistema, VoiceOver, dados velhos sinalizados (`AppSession.swift:54-58`), "puxe para atualizar" na tela de erro.
8. **Loja:** ícone opaco sem cantos arredondados; `PrivacyInfo.xcprivacy` com UserDefaults (CA92.1) e e-mail/identificador de dispositivo; ligar assinatura (linhas da seção 2) com a equipe da conta.
9. **Testes** de decodificação com as fixtures e teste de UI para gerar capturas; TestFlight interno.

## 6. App Android, em ordem

1. Criar `android/PDV2Gestao`: Compose, `compileSdk`/`targetSdk` 36, Gradle wrapper, mesmo identificador do iOS.
2. Núcleo: Ktor com token e renovação, refresh no Keystore (DataStore cifrado), BiometricPrompt, URL HTTPS fixa por `buildConfig`, sem tráfego em texto claro.
3. Login, TOTP e "acesso não liberado".
4. Navegação e seletor de empresa/loja/período.
5. Telas de leitura, aba Plataforma, Conta. Gráfico em Canvas do Compose ou Vico no lugar do Swift Charts (`OwnerDashboardView.swift:167`).
6. Tema claro/escuro a partir de `mobile/design/tokens.json`, TalkBack e tamanho de fonte.
7. Testes com as mesmas fixtures; AAB de release assinado; ícone 512×512 e imagem de destaque 1024×500.

## 7. Checklist de publicação

**Preparação comum**
- 👤 Pedir o D-U-N-S da CrownTech (pode levar dias; fazer primeiro).
- 👤 Definir domínio de produção, e-mail e site no domínio da empresa.
- 👤 Aprovar o texto da política de privacidade e da página de exclusão de conta.
- 🤖 Publicar `/privacidade`, `/suporte`, `/excluir-conta` e o backend em HTTPS.
- 🤖 Criar a empresa demo e a conta de revisão (usuário de loja, sem MFA).

**Apple**
- 👤 Inscrever-se no Apple Developer Program como Organização e pagar a anuidade.
- 👤 Aceitar os contratos (app gratuito dispensa banco e impostos).
- 👤 Criar a chave da API do App Store Connect e informar o Team ID.
- 🤖 Registrar o bundle id, ligar a assinatura e gerar o archive.
- 🤖 Criar o registro do app: gratuito, categoria Negócios, só Brasil, pt-BR.
- 🤖 Gerar capturas de iPhone (1320×2868 e 1206×2622), descrição e palavras-chave.
- 👤 Responder classificação etária e rótulos de privacidade (e-mail e identificador de dispositivo, vinculados ao usuário, sem rastreamento).
- 🤖 Subir ao TestFlight; redigir as notas de revisão com a conta demo.
- 👤 Clicar em "Enviar para revisão".

**Google**
- 👤 Criar a conta Play Console como Organização, pagar a taxa e concluir as verificações de identidade.
- 👤 Criar o app no Console, aceitar o Play App Signing e guardar a chave de upload.
- 🤖 Gerar o AAB, a ficha (ícone, destaque, 4 capturas 1080×1920) e os textos.
- 👤 Preencher, com rascunho meu: acesso ao app (credenciais demo), anúncios (não), classificação de conteúdo, público-alvo (adultos), segurança dos dados (com link de exclusão).
- 🤖 Teste interno, depois fechado.
- 👤 Promover para produção.

## 8. Riscos de rejeição e como evitar

| Risco | Como evitar |
|---|---|
| Revisor não entra (Apple 2.1) | URL de produção fixa, conta demo de loja sem MFA, backend no ar durante a revisão |
| Painel vazio | Rotina diária de vendas na empresa demo |
| App para público restrito (Apple 3.2) | Ficha como "complemento gratuito para clientes do PDV2", notas explicando como se obtém acesso; plano B: distribuição não listada |
| Funcionalidade mínima (4.2) | Cinco abas nativas, filtros, detalhe de venda e tela de conta |
| Compra fora do app (3.1.3) | Nenhum preço, plano, `checkout_url` ou cadastro no app; mensagens de plano trocadas por texto neutro |
| Privacidade (5.1.1) | Política publicada e ligada na tela Conta; exclusão de conta no app e em página web (o Google exige porque há cadastro web, `views_auth.py:121`) |
| Bloqueio no upload | Ícone sem alfa, manifesto de privacidade completo, chave de criptografia declarada, assinatura ligada |
| Prazo do Google | Conta de Organização; conta pessoal obriga a 12 testadores por 14 dias |

## 9. Perguntas em aberto para o dono

1. **Identificador e domínio:** qual domínio a CrownTech controla? Ele define o bundle id (sugestão `br.com.crowntech.pdv2gestao`), que não muda depois do primeiro envio, e a URL de produção.
2. **Quem libera acesso:** só superusuário ou também a equipe de suporte da plataforma? Assumi só superusuário (`views_platform.py:77`).
3. **Validade da liberação:** vale até ser revogada ou expira por padrão? Assumi sem expiração, com data opcional.
4. **Empresa inadimplente ou cancelada:** o usuário liberado continua vendo os dados? Hoje a leitura nunca é bloqueada (`api_common.py:169-172`); assumi que continua.
5. **Taxas das lojas:** as contas de desenvolvedor são pagas. Segue com Apple e Google agora, ou começa pelo Google (taxa única) enquanto o D-U-N-S e a conta Apple saem?

---

# Relatórios de apoio

## ios-codigo

## App iOS PDV2 Monitor: estado atual e o que impede a publicação

**Veredito:** o app é um protótipo de uma tela, só leitura, que funciona no simulador contra `http://127.0.0.1:8000`. Hoje não dá para enviar à App Store, e o modelo de acesso é o oposto do decidido (só `owner` entra; o admin do SaaS não consegue). Nada foi compilado nem testado ao vivo: tudo abaixo vem da leitura do código.

Arquivos Swift em `/Users/brunomagalhaes/Documents/Codex/2026-09-28/se-x20/outputs/pdv-saas-phase1/ios/PDV2OwnerMonitor/PDV2OwnerMonitor/`; backend em `.../pdv-saas-phase1/cloud/`.

### O que faz hoje
- **Login** (RootView.swift:18-155): e-mail, senha e um campo "Configuração do servidor" com a URL da API.
- **Painel único** (OwnerDashboardView.swift:55-74): faturamento, vendas e ticket médio do dia; terminais online; recebimentos por dinheiro/Pix/crédito/débito; a pagar/receber; gráfico e lista por terminal; status dos terminais.
- **Atualização**: a cada 30 s, por botão e por puxar (AppSession.swift:61-69).
- **Dados**: só `GET /api/v1/owner/monitoring`, sempre "hoje", sem filtro de loja ou período (cloud/core/views_owner.py:44-124).
- **Tamanho**: 7 arquivos Swift, ~900 linhas, sem dependências externas, sem testes.

### Bloqueadores de envio
1. **Assinatura desligada**: `CODE_SIGNING_ALLOWED = NO` em todas as configurações, sem `DEVELOPMENT_TEAM` (project.pbxproj:163-164, 171-172, 187-188, 203-204). Não gera archive.
2. **Ícone com transparência**: PNG RGBA 1024 com cantos já arredondados e alfa 0 nos cantos. A App Store recusa ícone com canal alfa.
3. **Servidor padrão é localhost**: `http://127.0.0.1:8000` (AppSession.swift:22). Não há URL de produção embutida; o revisor da Apple não consegue entrar.
4. **Login deve falhar em HTTPS**: o app não envia `Origin` nem `Referer` (APIClient.swift:81-88). O Django 5.2 rejeita POST seguro sem Referer (`csrf.py:441`), e produção roda atrás de proxy com `PDV_TRUST_PROXY=1` (compose.production.yaml:11). Só passa no simulador porque lá é HTTP.
5. **Admin do SaaS não entra**: com MFA de plataforma ligada (padrão em produção, settings.py:15), o login devolve `mfa_required` com `memberships: []` (views_auth.py:178-186). O app não tem tela de MFA e mostra "exclusivo do proprietário" (AppSession.swift:33-35).
6. **Regra de acesso contrária à decisão**: `role == "owner"` está fixo no app (AppSession.swift:33) e no servidor (views_owner.py:48). Não existe modelo, endpoint nem tela para o admin liberar usuários.
7. **Privacidade**: `PrivacyInfo.xcprivacy` declara zero APIs e zero dados, mas o app usa `UserDefaults` (AppSession.swift:22,40) e envia e-mail. Não há política de privacidade publicada em `web/`, exigida pelas duas lojas.
8. **Android não existe**: não há pasta `android/` no repositório.

### Segurança
- **Sessão**: cookie do Django em `HTTPCookieStorage.shared` (APIClient.swift:36). Sem Keychain, sem biometria, sem pinning. A senha não é gravada.
- **Sem restauração**: o cookie é de sessão (settings.py:101-102) e o app não chama `/auth/me` ao abrir; o usuário faz login a cada abertura.
- **ATS**: só `NSAllowsLocalNetworking` (Info.plist:23-27), mas o cliente aceita `http` para qualquer host (APIClient.swift:32). Como essa chave libera endereços IP, a senha pode sair em texto claro para um IP digitado.
- **URL configurável pelo usuário** em versão de loja: credenciais vão para qualquer servidor informado (RootView.swift:86-104).
- **Sessão órfã**: login de não-proprietário autentica no servidor, o app recusa e não faz logout nem limpa cookies (AppSession.swift:33-35).

### Bugs
- **Sessão expirada fica invisível**: erro no refresh é engolido quando já há dados (AppSession.swift:55-57). O painel mantém números velhos com o selo "AO VIVO" (OwnerDashboardView.swift:96).
- **401 sempre vira "E-mail ou senha inválidos"**, inclusive em sessão expirada (APIClient.swift:14).
- **"Recebimentos" inflado**: soma `other` (OwnerDashboardView.swift:124), onde o servidor joga fiado e crédito de loja (views_owner.py:38-40), e não desconta troco. `other` não aparece no detalhamento (134-139).
- **Modo escuro**: `PDVTheme.ink` e `muted` são cores fixas escuras (DesignSystem.swift:8-9), usadas em títulos sobre fundo do sistema (OwnerDashboardView.swift:79-80; RootView.swift:74,77).
- **"Puxe para tentar novamente" não funciona** na tela de erro, que não é rolável (OwnerDashboardView.swift:16-17, 41).
- **Idioma**: o projeto é `en` (project.pbxproj:113-118) e o Info.plist não declara região. Datas relativas e textos do sistema saem em inglês; a loja listaria o app como inglês.
- **Só a primeira empresa**: usa o primeiro vínculo `owner`, sem seletor (AppSession.swift:33).
- **Menores**: três requisições simultâneas ao entrar (AppSession.swift:41; OwnerDashboardView.swift:46-49); "Estável" com zero terminais (129); terminais homônimos somados no gráfico (168-169; Models.swift:71); README promete versão do terminal, que não é exibida.

### Acessibilidade
- Fontes de tamanho fixo não acompanham o tamanho de texto do sistema (RootView.swift:55,59; OwnerDashboardView.swift:80,97,102).
- Ícones decorativos não são ocultados do VoiceOver; cartões não são agrupados; barras de pagamento sem rótulo (OwnerDashboardView.swift:148).
- Só dois botões têm rótulo explícito (36, 38).

### Projeto e metadados
- **Bundle id** `br.com.pdv2.OwnerMonitor`, fixo também no Info.plist (linha 10); não remete à CrownTech e é permanente após o primeiro envio.
- **Versão** 1.0.0 (1) fixa no Info.plist, sem `MARKETING_VERSION`.
- **Alvo** iOS 17.0, iPhone e iPad (exige capturas de iPad).
- **Build**: `.pbxproj` escrito à mão, sem otimização/dSYM de Release, sem scheme compartilhado, sem alvo de testes. Falta `ITSAppUsesNonExemptEncryption`.

### "Totalmente grátis"
- O app não tem compra interna, o que atende à decisão.
- O endpoint exige o recurso de plano `cloud_dashboard` (views_owner.py:49); a mensagem "Recurso não incluído no plano" pode aparecer dentro do app.
- Publicar tem custo para a CrownTech, não para o usuário: Apple Developer Program (US$ 99/ano) e Google Play (US$ 25, uma vez).
- A landing já anuncia "App Store — Aplicativo para iPhone" (web/landing.html:145) sem o app estar publicado.

### O que falta para ser gestão completa
O backend já expõe quase tudo em `cloud/pdvcloud/urls.py:40-106`; o app não consome nada disso:
- **Navegação**: abas, seletor de empresa e loja, período (hoje/7 dias/mês).
- **Vendas**: lista, detalhe, cancelamento e devolução.
- **Caixa**: sessões, sangrias e quebras.
- **Estoque**: saldos, movimentações, inventário, transferências.
- **Financeiro**: contas a pagar/receber com baixa; fiado.
- **Cadastros**: produtos e preços, promoções, usuários e PIN.
- **Terminais**: parear, revogar, conflitos de sincronização.
- **Relatórios, comissões e auditoria.**
- **Admin do SaaS**: empresas, assinaturas, terminais, auditoria e a tela de liberar acesso ao app.
- **Sem base no backend**: alertas push e metas (docs/ROADMAP-COMPLETUDE.md:138-139), cache offline, biometria.

### Itens

- **[requisito-loja · blocker] Assinatura de código desativada e sem equipe**: project.pbxproj:163-164, 171-172, 187-188, 203-204 têm CODE_SIGNING_ALLOWED = NO e CODE_SIGNING_REQUIRED = NO nas quatro configurações. Não há DEVELOPMENT_TEAM, CODE_SIGN_STYLE nem entitlements. Não é possível gerar archive nem enviar ao TestFlight. Exige conta Apple Developer da CrownTech (organização, com D-U-N-S).
- **[requisito-loja · blocker] Ícone do app tem canal alfa e cantos transparentes**: Assets.xcassets/AppIcon.appiconset/AppIcon.png é RGBA 1024x1024 com alfa de 0 a 255; os pixels (0,0) e (1023,1023) são totalmente transparentes porque o desenho já vem com cantos arredondados. A App Store recusa ícone grande com transparência. Precisa de PNG quadrado, opaco, sem cantos arredondados. O ícone também difere da marca usada dentro do app (DesignSystem.swift:13-27).
- **[bug · blocker] URL padrão do servidor é localhost e não há URL de produção**: AppSession.swift:22 usa "http://127.0.0.1:8000" como padrão em qualquer build. RootView.swift:86-104 expõe "Configuração do servidor" ao usuário final, com o texto "Simulador: http://127.0.0.1:8000". O revisor da Apple e o usuário real não conseguem entrar sem digitar a URL. Precisa de URL HTTPS de produção fixa no build de Release; o campo só deve existir em Debug.
- **[bug · blocker] Login deve falhar em produção HTTPS por checagem de Referer do CSRF**: APIClient.swift:81-88 não envia Origin nem Referer. O Django 5.2.17 (requirements.txt:8), em requisição segura sem Origin, exige Referer (.venv/lib/python3.12/site-packages/django/middleware/csrf.py:436-441 e 297-300). Produção roda com PDV_TRUST_PROXY=1 (compose.production.yaml:11) e SECURE_PROXY_SSL_HEADER (cloud/pdvcloud/settings.py:119-120), então is_secure() é verdadeiro. O POST /api/v1/auth/login retornaria 403 com "CSRF inválido; atualize a página" (cloud/core/api_common.py:36-37). Só funciona no simulador por ser HTTP. Deduzido do código, não testado ao vivo. O teste existente (cloud/core/tests.py:1224-1231) não cobre HTTPS. Correção: enviar Referer/Origin do próprio host ou criar autenticação por token para o app.
- **[lacuna · blocker] Admin do SaaS não consegue entrar: sem fluxo de MFA**: Com PLATFORM_MFA_REQUIRED (padrão 1 fora de DEBUG, cloud/pdvcloud/settings.py:15), o login de conta de plataforma devolve mfa_required ou mfa_setup_required com memberships vazio (cloud/core/views_auth.py:178-186). O app decodifica como LoginResponse, não acha vínculo owner e mostra OwnerAccessError (AppSession.swift:33-35, 86-90). Não há tela de código TOTP nem chamada a /api/v1/auth/mfa/verify. A sessão fica presa em MFA pendente.
- **[lacuna · blocker] Modelo de acesso é o oposto do decidido**: O papel owner está fixo no cliente (AppSession.swift:33) e no servidor (cloud/core/views_owner.py:48, membership_context(roles={"owner"})). A decisão é: acesso do admin do SaaS, que libera usuários específicos. Não existe campo, modelo ou endpoint de liberação (busca por mobile_access/app_access em cloud/ não retorna nada) nem tela no painel /platform. Membership (cloud/core/models.py:95-110) não tem indicador de acesso ao app. Textos do app também dizem "proprietário" (RootView.swift:75, 151; OwnerDashboardView.swift:79).
- **[requisito-loja · high] Manifesto de privacidade incompleto**: PrivacyInfo.xcprivacy:7-10 declara NSPrivacyCollectedDataTypes e NSPrivacyAccessedAPITypes vazios. O app usa UserDefaults (AppSession.swift:22, 40), que exige declarar NSPrivacyAccessedAPICategoryUserDefaults com motivo CA92.1. O app também transmite e-mail no login, que deve constar como dado coletado e no questionário de privacidade do App Store Connect.
- **[requisito-loja · blocker] Não existe política de privacidade publicada**: web/ contém apenas landing.html, login.html, app.html, offline.html, manifest e sw.js. Busca por privacidade/termos/LGPD nos HTML não encontra página nem link. App Store e Google Play exigem URL pública de política de privacidade, além de URL de suporte. O próprio README do app (linhas 10-12) admite a pendência.
- **[lacuna · blocker] App Android não existe**: Não há diretório android/ no repositório. A landing marca Android como "Em desenvolvimento" (web/landing.html:146). Flutter não está instalado; com Android SDK e JDK 17 as opções são app nativo Kotlin/Compose ou empacotar a PWA existente (web/manifest.webmanifest, web/sw.js) como Trusted Web Activity.
- **[bug · high] Sessão expirada não é tratada: painel mostra dados velhos como "AO VIVO"**: AppSession.swift:54-58 só exibe erro quando snapshot é nil. Depois de 8 h (SESSION_COOKIE_AGE, cloud/pdvcloud/settings.py:101) o refresh recebe 401 e o erro é descartado. O painel segue com números antigos e o selo fixo "AO VIVO" (OwnerDashboardView.swift:96). Não há retorno ao login nem aviso de dados desatualizados.
- **[lacuna · high] Sem restauração de sessão: login a cada abertura**: SESSION_EXPIRE_AT_BROWSER_CLOSE = True (cloud/pdvcloud/settings.py:102) gera cookie de sessão não persistente, e AppSession.init (AppSession.swift:21-23) não chama /api/v1/auth/me. Cada abertura a frio exige e-mail e senha. Não há Keychain, token de longa duração nem desbloqueio por Face ID.
- **[bug · high] HTTP em texto claro aceito para qualquer IP**: APIClient.swift:32 aceita esquema http para qualquer host. Info.plist:23-27 habilita NSAllowsLocalNetworking, que libera conexões sem TLS para endereços IP e nomes locais. Com a URL digitável (RootView.swift:88), e-mail e senha podem trafegar em claro. Release deve exigir https e remover a exceção.
- **[bug · medium] Sessão autenticada órfã quando o login é recusado pelo app**: Se um gerente ou caixa entra, o servidor cria a sessão (cloud/core/views_auth.py:187) e o app lança OwnerAccessError (AppSession.swift:33-35) sem chamar logout nem clearCookies. O cookie autenticado permanece no HTTPCookieStorage.shared até o processo encerrar.
- **[bug · medium] Mensagens de erro erradas para 401 e 403**: APIClient.swift:14 converte todo 401 em "E-mail ou senha inválidos", inclusive sessão expirada no monitoramento. Qualquer falha de rede vira "Sem conexão" (APIClient.swift:93-95), inclusive bloqueio por ATS, erro de TLS e cancelamento. Mensagens do servidor pensadas para a web aparecem cruas, como "CSRF inválido; atualize a página".
- **[bug · high] "Recebimentos" inclui fiado e não desconta troco**: cloud/core/views_owner.py:32-40 joga credit_account (fiado), store_credit e card em other. OwnerDashboardView.swift:124 soma other no cartão "Recebimentos", mas a lista de formas (134-139) não mostra other, então o total não fecha com o detalhamento. O troco também não é abatido do dinheiro, ao contrário do relatório de fluxo de caixa (cloud/core/views_reports.py:210-216).
- **[bug · high] Textos ilegíveis no modo escuro**: PDVTheme.ink e PDVTheme.muted são cores fixas escuras (DesignSystem.swift:8-9). São usadas sobre fundo do sistema e material adaptável: "Visão geral" e "Olá, proprietário" (OwnerDashboardView.swift:79-80) e título/legendas do cartão de login (RootView.swift:74, 77, 97, 138). No modo escuro ficam escuro sobre escuro.
- **[bug · medium] App não está localizado em pt-BR no nível do bundle**: project.pbxproj:113-118 define developmentRegion = en e knownRegions en/Base. Info.plist não tem CFBundleDevelopmentRegion nem CFBundleLocalizations. Datas relativas (DesignSystem.swift:57) e textos do sistema saem em inglês em aparelhos em português; o VoiceOver usa voz inglesa; a App Store listaria o idioma como inglês.
- **[bug · low] "Puxe para tentar novamente" não funciona na tela de erro**: OwnerDashboardView.swift:16-17 mostra ContentUnavailableView, que não é rolável; o .refreshable da linha 41 só age dentro de ScrollView. O usuário precisa usar o botão da barra.
- **[lacuna · medium] Usuário com várias empresas só vê a primeira**: AppSession.swift:33 usa memberships.first(where: role == "owner"). Não há seletor de empresa, embora o servidor devolva todos os vínculos (cloud/core/views_auth.py:32-35). Para o admin do SaaS ou um dono de várias lojas isso inviabiliza o uso.
- **[bug · low] Problemas menores no painel**: Três requisições simultâneas ao entrar e isRefreshing sem controle de concorrência (AppSession.swift:41, 49-50; OwnerDashboardView.swift:46-49). "Status da rede" mostra "Estável" com zero terminais (OwnerDashboardView.swift:129). Terminais com mesmo nome em lojas diferentes são somados no gráfico e podem gerar id duplicado (OwnerDashboardView.swift:168-169; Models.swift:71). README (linha 35) promete versão do terminal, que é decodificada (Models.swift:41) mas não exibida. Saudação fixa "Olá, proprietário" em vez do nome do usuário. Vendas do dia filtram por received_at (cloud/core/views_owner.py:61-62), então venda offline sincronizada depois cai no dia errado.
- **[bug · medium] Acessibilidade: fontes fixas e VoiceOver incompleto**: Fontes com tamanho fixo não seguem o tamanho de texto do sistema (RootView.swift:55, 59; OwnerDashboardView.swift:80, 97, 102, 110). Ícones decorativos sem accessibilityHidden; cartões de métrica sem agrupamento (OwnerDashboardView.swift:251-266); barras de progresso sem rótulo (148). Só os botões de atualizar e sair têm accessibilityLabel (36, 38). A mensagem de erro do login não é anunciada (RootView.swift:106-111).
- **[decisao · high] Decidir o bundle id definitivo antes do primeiro envio**: Hoje é br.com.pdv2.OwnerMonitor, em project.pbxproj:192, 209 e fixo em Info.plist:9-10 (deveria ser $(PRODUCT_BUNDLE_IDENTIFIER)). Não remete à CrownTech e carrega "OwnerMonitor", que deixa de fazer sentido num app de gestão liberado pelo admin. O bundle id não pode ser trocado depois do primeiro upload. O nome de exibição "PDV2 Monitor" (Info.plist:5-6) também deve ser revisto.
- **[tarefa · medium] Versão fixa no Info.plist e configurações de build incompletas**: Info.plist:15-18 fixa 1.0.0 e build 1; não há MARKETING_VERSION nem CURRENT_PROJECT_VERSION. O project.pbxproj foi escrito à mão (ids A1000001...) e não define otimização de Release, DEBUG_INFORMATION_FORMAT (dSYM), SWIFT_ACTIVE_COMPILATION_CONDITIONS nem avisos. Não há scheme compartilhado (xcshareddata ausente; xcuserdata está no .gitignore:20), nem alvo de testes, nem CI. Falta ITSAppUsesNonExemptEncryption no Info.plist.
- **[requisito-loja · medium] Suporte a iPad declarado exige capturas e layout de iPad**: TARGETED_DEVICE_FAMILY = "1,2" (project.pbxproj:196, 213) e quatro orientações (Info.plist:28-34). A App Store exigirá capturas de iPad além das de iPhone. O painel não tem largura máxima nem layout próprio para tela grande (OwnerDashboardView.swift:56-73). Decidir entre manter iPad ou lançar só iPhone.
- **[requisito-loja · high] Material de revisão e ficha das lojas inexistentes**: Faltam: conta de demonstração para o revisor (app exige login), notas de revisão explicando que é app gratuito de acompanhamento de um serviço B2B, descrição, palavras-chave, capturas, classificação etária, URL de suporte e formulário de segurança de dados do Google Play. Como o acesso é restrito a usuários liberados, avaliar distribuição não listada na Apple.
- **[decisao · medium] Acesso depende de recurso de plano pago, apesar de o app ser gratuito**: cloud/core/views_owner.py:49 exige a feature cloud_dashboard do plano. A mensagem "Recurso não incluído no plano" (cloud/core/api_common.py:169-172) aparece dentro do app (APIClient.swift:15). Decidir se a liberação pelo admin substitui essa trava e remover qualquer menção a plano ou compra no app, para não esbarrar nas regras de compra dentro do app.
- **[decisao · medium] Custos de publicação, embora o app seja gratuito para o usuário**: Não há compra interna no código, o que atende ao "totalmente grátis". Publicar exige Apple Developer Program (US$ 99/ano) e conta de desenvolvedor Google Play (US$ 25, uma vez), ambas em nome da empresa. A landing já anuncia "App Store — Aplicativo para iPhone" (web/landing.html:145) antes da publicação.
- **[lacuna · high] Falta todo o conteúdo de gestão: o app tem uma tela só**: O app consome apenas GET /api/v1/owner/monitoring (APIClient.swift:54-57). O backend já expõe, em cloud/pdvcloud/urls.py:40-106: vendas com cancelamento e devolução (59-64), sessões de caixa (85, 91), estoque e entradas (41-43), inventário e transferências (71-78), financeiro com baixa (44-46), fiado (66-68), promoções (69-70), usuários e PIN (47-49), terminais (50-51), conflitos de sincronização (57-58), relatórios (86-87), comissões (88-90), auditoria (93) e administração de plataforma (94-106). Faltam também navegação por abas, filtro de período e loja, e a tela de liberar acesso.
- **[lacuna · medium] Recursos de gestão sem base no backend**: Alertas push e metas estão apenas no roadmap (docs/ROADMAP-COMPLETUDE.md:138-139); não há registro de dispositivo nem integração APNs/FCM (busca por apns/fcm/device_token em cloud/ não retorna nada). Também não existem cache offline dos últimos dados, bloqueio por biometria, tela de privacidade no alternador de apps nem widgets.
- **[tarefa · medium] Zero testes automatizados no app**: Não há alvo de testes no project.pbxproj (único alvo nas linhas 82-98). Decodificação é estrita, sem valores padrão (Models.swift:115-136): qualquer campo novo obrigatório ou nulo inesperado derruba a tela inteira com "resposta inválida" (APIClient.swift:102-103). No backend há um único teste do endpoint (cloud/core/tests.py:265-292), sem cobrir login do app nem CSRF em HTTPS.

## api-app

## Avaliação da API para o app móvel de GESTÃO (gratuito, acesso liberado pelo admin do SaaS)

Análise somente leitura. Caminhos relativos a `/Users/brunomagalhaes/Documents/Codex/2026-09-28/se-x20/outputs/pdv-saas-phase1`.

### 1. Sessão + CSRF não serve para app nativo
- **Login quebra em produção (HTTPS).** O `CsrfViewMiddleware` (`cloud/pdvcloud/settings.py:67`) exige `Origin` ou `Referer` em POST seguro (Django 5.2). O app envia só `X-CSRFToken` (`ios/.../APIClient.swift:45-52`), então o login deve receber 403 "CSRF inválido" (`cloud/core/api_common.py:36`). Só funciona em `http://127.0.0.1:8000`. Conclusão tirada da leitura do código; não executei contra HTTPS.
- **Sessão curta e não persistente.** `SESSION_COOKIE_AGE = 8h` e `SESSION_EXPIRE_AT_BROWSER_CLOSE = True` (`settings.py:101-102`): o cookie some ao fechar o app e a senha é redigitada a cada 8 h. Biometria não tem o que destravar.
- **Sem revogação por aparelho.** A sessão do Django não lista dispositivos; não há registro de aparelho nem token de push.
- **Admin do SaaS não consegue entrar.** Com MFA obrigatória, o login devolve `mfa_required` e `memberships: []` (`cloud/core/views_auth.py:178-186`). O app procura um vínculo `owner` e mostra "exclusivo do proprietário" (`AppSession.swift:33`). `platform_user` depende de `session["platform_mfa_verified"]` (`api_common.py:180`).

**Necessário:** token opaco por dispositivo, guardado como SHA-256, no mesmo padrão do terminal (`cloud/core/views_terminal.py:104-115`).
- Access token de cerca de 15 min e refresh rotativo de 30–60 dias com detecção de reuso.
- Refresh no Keychain/Keystore, destravado por Face ID ou BiometricPrompt.
- MFA TOTP no fluxo do app para contas de plataforma (reaproveita `PlatformMFA`), com `mfa_verified_at` gravado no dispositivo.
- Token do app restrito a GET: `membership_context(write=True)` deve recusar.
- Endpoints `/api/v1/app/auth/*` com `csrf_exempt` e `take_rate`.

### 2. Regra "o admin do SaaS libera o acesso"
Hoje a única trava é `roles={"owner"}` (`cloud/core/views_owner.py:48`), e `Membership` não tem escopo por loja (`cloud/core/models.py:95-110`).

**Modelos propostos**
- `AppAccess`: `user`, `scope` (`platform` ou `tenant`), `tenant` (nulo para plataforma), `shops` (M2M; vazio = todas), `permissions` (vendas, caixa, estoque, fiado, comissões, relatórios), `granted_by`, `granted_at`, `reason`, `expires_at`, `revoked_at`, `revoked_by`, `revoke_reason`. Único por (user, tenant) enquanto ativo.
- `AppDevice`: `user`, `platform` (ios/android), `name`, `app_version`, `refresh_hash`, `access_hash`, `push_token`, `push_provider` (apns/fcm), `last_seen_at`, `last_ip`, `mfa_verified_at`, `revoked_at`.
- `AlertRule` (por empresa: tipo, limite, ativo) e `AppNotification` (fila com `dedupe_key`).

**Endpoints**
- Plataforma: `GET/POST /api/v1/platform/app-access`, `PATCH/DELETE /api/v1/platform/app-access/<id>` (revogar com motivo, derrubando os dispositivos), `GET /api/v1/platform/app-devices`, `POST /api/v1/platform/app-devices/<id>/revoke`. Tudo auditado com `audit()` (`api_common.py:212`).
- App: `POST /api/v1/app/auth/login|mfa|refresh|logout`, `GET /api/v1/app/me`, `GET/DELETE /api/v1/app/devices`, `PUT /api/v1/app/devices/current/push`.
- Sem `AppAccess` ativo, o login responde 403 "Acesso ao app não liberado".

### 3. O que já existe e o que falta
| Necessidade | Existe | Limitação |
|---|---|---|
| Vendas do dia | `/api/v1/owner/monitoring`; `/api/v1/dashboard` (`views_sales.py:103`); `/api/v1/reports/sales_by_day`, `sales_by_hour`, `sales_by_payment` | O dashboard não restringe papel (`views_sales.py:105`) e conta estoque baixo em laço Python (`:127-133`). `/api/v1/sales` não filtra por data (`:24-36`). |
| Caixas e quebra | `/api/v1/cash-sessions?status=open` ou `shortage` (`views_cash.py:41-63`) | `session_summary` roda por linha, cerca de 6 consultas cada (`cash_logic.py:67-91`). Sem limite de valor. |
| Estoque baixo | `/api/v1/stock` (`views_stock.py:29-49`); `/api/v1/reports/low_stock` | Exige `shop_id`, sem filtro "abaixo do mínimo" ou "zerado". O relatório exige o recurso `reports`. |
| Fiado em aberto | `/api/v1/customers/credit-summary` (`views_customer_credit.py:15-22`) | Sem nome do cliente, sem vencidos, sem paginação. |
| Comissões | `/api/v1/commissions/summary` (`views_commissions.py:48`) | Exige o recurso `finance`. |
| Relatórios | `/api/v1/reports/<kind>` (`views_reports.py:280-311`) | Serve como está. |
| Conflitos offline | `/api/v1/sync/conflicts?status=open` (`views_terminal.py:518`) | Serve como está. |
| Terminais online | `owner/monitoring`; `/api/v1/terminals` (só owner/admin) | — |
| Plataforma | `/api/v1/ops/platform/metrics` com MRR, inadimplentes e trials (`operations/views.py:303-319`); `/api/v1/platform/subscriptions?status=past_due`; `tenants`; `invoices` | Depende de sessão com MFA. `invoices` não filtra por status. |

**Falta**
- Resumo agregado multi-loja em uma chamada (`GET /api/v1/app/overview`): por loja, faturamento, cancelamentos, caixas abertos e quebras, estoque baixo e zerado, fiado vencido, conflitos, terminais.
- Push inexistente: não há APNs/FCM, registro de token nem avaliador de alertas. Ganchos naturais:
  - quebra de caixa em `cash_logic.py` no evento `cash.closed`;
  - cancelamento alto em `sale_logic.py:190` (`reverse_sale`);
  - estoque zerado em `stock_balance.py:16`;
  - terminal offline exige tarefa periódica, que pode ir em `operations/worker.py`.
- Visão de plataforma para o app: inadimplentes com valor e dias, trials vencendo, empresas novas, saúde por empresa.
- App Android: não existe no repositório (`docs/BACKLOG.md:36`).

### 4. Bugs e riscos em `views_owner.py`
1. **Dia errado para vendas offline.** Filtra por `received_at` (`:61-62`); painel e relatórios usam `created_at` (`views_sales.py:115`, `views_reports.py:46-47`). Venda de ontem sincronizada hoje entra em "hoje".
2. **Dinheiro inflado pelo troco.** `_payment_totals` (`:28-41`) não subtrai `change_cents`; o relatório subtrai (`views_reports.py:97`).
3. **Fiado e crédito de loja caem em "other"** (`:38-39`) e aparecem como recebimento.
4. **Assinatura vencida continua lendo.** `require_feature` (`api_common.py:169-172`) não olha status nem prazo.
5. **Contagem inconsistente.** `terminals_total` exclui revogados (`:57`), mas a lista inclui todos (`:95-108`), inclusive não pareados, que aparecem como offline; o payload cresce com revogados.
6. **Sem limite de taxa** com polling a cada 30 s e 6 consultas por chamada.
7. **Fuso fixo em America/Sao_Paulo** (`:20-25`); `Tenant` não tem fuso.
8. **Sem cancelamentos ou devoluções do dia** e sem `shop_id`.
9. **Teste insuficiente** (`cloud/core/tests.py:265-292`): não cobre troco, fiado nem venda atrasada.

Não encontrei vazamento entre empresas: todas as consultas filtram por `tenant`.

### Gratuidade
Sem compra no app, não entra StoreKit nem Play Billing. O app não deve exibir `checkout_url` (`views_platform.py:481`) nem oferecer contratação de plano. Como não há cadastro no app, as lojas vão pedir conta de demonstração para a revisão.

### Itens

- **[bug · blocker] Login do app iOS deve falhar em produção HTTPS por checagem de Referer/Origin do CSRF**: CsrfViewMiddleware (cloud/pdvcloud/settings.py:67) exige Origin ou Referer em POST seguro (Django 5.2.17). O app envia só X-CSRFToken (ios/PDV2OwnerMonitor/PDV2OwnerMonitor/APIClient.swift:45-52), então POST /api/v1/auth/login deve retornar 403 via csrf_failure (cloud/core/api_common.py:36). Só funciona em http://127.0.0.1:8000. Conclusão por leitura de código, não testada contra HTTPS. Correção definitiva: autenticação por token nos endpoints /api/v1/app/auth/* com csrf_exempt.
- **[lacuna · blocker] Admin do SaaS não consegue usar o app (MFA e ausência de vínculo owner)**: Com PLATFORM_MFA_REQUIRED, o login devolve mfa_required e memberships vazio (cloud/core/views_auth.py:178-186). O app procura um vínculo owner e mostra 'exclusivo do proprietário' (AppSession.swift:33). platform_user depende de session['platform_mfa_verified'] (cloud/core/api_common.py:180). O público principal definido pelo dono do produto fica sem acesso. Precisa de fluxo MFA TOTP no app e de mfa_verified_at gravado no dispositivo/token.
- **[lacuna · blocker] Não existe a regra 'admin do SaaS libera acesso ao app'**: A única trava é roles={'owner'} em cloud/core/views_owner.py:48; qualquer proprietário entra e gerente nunca entra. Membership não tem escopo por loja (cloud/core/models.py:95-110). Criar AppAccess (user, scope platform/tenant, tenant, shops M2M, permissions, granted_by, granted_at, reason, expires_at, revoked_at, revoked_by, revoke_reason; único por user+tenant enquanto ativo). Endpoints GET/POST /api/v1/platform/app-access e PATCH/DELETE /api/v1/platform/app-access/<id>, auditados com audit() (api_common.py:212). Login do app responde 403 sem AppAccess ativo.
- **[tarefa · blocker] Implementar token por dispositivo com refresh e revogação**: Sessão de 8 h não persistente (cloud/pdvcloud/settings.py:101-102) obriga redigitar a senha e não permite revogar um aparelho. Criar AppDevice (user, platform, name, app_version, refresh_hash, access_hash, push_token, push_provider, last_seen_at, last_ip, mfa_verified_at, revoked_at) no padrão SHA-256 do terminal (cloud/core/views_terminal.py:104-115). Access token ~15 min, refresh rotativo 30-60 dias com detecção de reuso, refresh no Keychain/Keystore destravado por biometria. Endpoints POST /api/v1/app/auth/login|mfa|refresh|logout, GET /api/v1/app/me, GET/DELETE /api/v1/app/devices, POST /api/v1/platform/app-devices/<id>/revoke. Tornar require_user (api_common.py:120-123) ciente do token e recusar write=True e métodos diferentes de GET para token do app.
- **[bug · high] Monitoramento usa received_at: vendas offline caem no dia errado**: cloud/core/views_owner.py:61-62 filtra por received_at (chegada no servidor). Painel e relatórios usam created_at (cloud/core/views_sales.py:115, cloud/core/views_reports.py:46-47). Venda feita ontem offline e sincronizada hoje entra em 'hoje' no app e os números divergem do painel. O único índice de Sale é (tenant, -received_at, -id) (cloud/core/models.py:281); trocar para created_at pede índice novo.
- **[bug · high] Total em dinheiro inflado pelo troco e fiado contado como recebimento**: _payment_totals (cloud/core/views_owner.py:28-41) soma SalePayment.amount_cents sem subtrair Sale.change_cents; o relatório subtrai (cloud/core/views_reports.py:97) e o caixa também (cloud/core/cash_logic.py:74-78). credit_account e store_credit caem em 'other' (views_owner.py:38-39) e aparecem como recebimento, embora cash_flow os exclua (views_reports.py:212).
- **[lacuna · high] Push inexistente: sem APNs/FCM, registro de token ou avaliador de alertas**: Nenhum código de push no backend; só a menção em docs/ROADMAP-COMPLETUDE.md:139. Faltam AlertRule (por empresa: tipo, limite, ativo), AppNotification (fila com dedupe_key) e PUT /api/v1/app/devices/current/push. Ganchos: quebra de caixa no evento cash.closed em cloud/core/cash_logic.py; cancelamento alto em cloud/core/sale_logic.py:190 (reverse_sale); estoque zerado em cloud/core/stock_balance.py:16; terminal offline exige tarefa periódica, possível em operations/worker.py. Requer chave APNs (.p8) e projeto Firebase.
- **[lacuna · high] Falta endpoint de resumo agregado multi-loja**: owner/monitoring não aceita shop_id nem data e não traz cancelamentos, caixas, estoque, fiado ou conflitos. O app teria de chamar 6 ou mais endpoints por loja. Criar GET /api/v1/app/overview?date=&shop_id= com, por loja: faturamento, quantidade, ticket, cancelamentos e devoluções, caixas abertos e quebras, estoque baixo e zerado, fiado em aberto e vencido, comissões pendentes, conflitos abertos, terminais online e offline. Deve respeitar AppAccess.shops.
- **[lacuna · medium] Visão de plataforma no app depende de sessão e está incompleta**: Já existem /api/v1/ops/platform/metrics com MRR, past_due e trials (operations/views.py:303-319), /api/v1/platform/subscriptions?status=past_due (cloud/core/views_platform.py:287-293), tenants (:174) e invoices (:473, sem filtro de status). Todos exigem sessão com MFA (cloud/core/api_common.py:180). Faltam: inadimplentes com valor e dias de atraso, trials vencendo em N dias, empresas novas, saúde por empresa (último uso, terminais offline, conflitos).
- **[lacuna · medium] Endpoints de leitura existentes têm lacunas para uso móvel**: /api/v1/sales não filtra por data (cloud/core/views_sales.py:24-36). /api/v1/customers/credit-summary não traz nome do cliente nem vencidos e não pagina (cloud/core/views_customer_credit.py:15-22). /api/v1/stock exige shop_id e não filtra abaixo do mínimo ou zerado (cloud/core/views_stock.py:29-49). /api/v1/cash-sessions roda session_summary por linha, cerca de 6 consultas cada (cloud/core/views_cash.py:16-24, cloud/core/cash_logic.py:67-91). /api/v1/reports/* exige o recurso 'reports' e comissões exigem 'finance', ausentes do plano básico (cloud/core/migrations/0002_seed_plans.py:8).
- **[bug · medium] /api/v1/dashboard sem restrição de papel e com cálculo pesado**: cloud/core/views_sales.py:105 chama membership_context sem roles: operador de caixa lê faturamento e contas a pagar e receber. O estoque baixo é contado em laço Python sobre todos os produtos vezes lojas (:127-133), caro para polling móvel.
- **[bug · medium] Assinatura vencida ou cancelada continua lendo o monitoramento**: require_feature (cloud/core/api_common.py:169-172) verifica só plan.features; não olha status, trial_ends_at nem current_period_end. Empresa com assinatura cancelada segue recebendo dados em cloud/core/views_owner.py:49. Definir a política de leitura e aplicá-la também ao token do app.
- **[bug · low] Contagem de terminais inconsistente e payload crescente**: terminals_total exclui revogados (cloud/core/views_owner.py:57), mas a lista 'terminals' inclui todos (:95-108). Terminais ainda não pareados (sem token_hash) entram no total e aparecem como offline. Revogados não contam no limite do plano e se acumulam no payload. device_id é exposto sem necessidade.
- **[bug · low] Monitoramento sem limite de taxa e com fuso fixo**: Polling a cada 30 s (AppSession.swift, startMonitoring) contra 6 consultas por chamada, sem take_rate nem ETag. _day_window (cloud/core/views_owner.py:20-25) usa America/Sao_Paulo para todas as empresas; Tenant não tem campo de fuso, então lojas em Manaus ou no Acre têm o corte do dia errado.
- **[tarefa · low] Teste do monitoramento não cobre troco, fiado nem venda atrasada**: cloud/core/tests.py:265-292 testa só um pagamento Pix com change_cents=0 e created_at igual a agora. Acrescentar casos: dinheiro com troco, credit_account, venda com created_at de ontem recebida hoje, terminal revogado e terminal não pareado.
- **[decisao · high] Decidir quem pode liberar acesso ao app e com qual escopo**: Definir: (a) só superusuário ou também o grupo platform_support concede AppAccess (hoje a gestão de usuários de plataforma exige superuser, cloud/core/views_platform.py:77); (b) gerente vê todas as lojas ou só as de AppAccess.shops; (c) o acesso expira (expires_at) ou vale até ser revogado; (d) o proprietário da empresa pode pedir acesso pelo painel; (e) o recurso de plano cloud_dashboard continua exigido (cloud/core/views_owner.py:49).
- **[decisao · medium] Decidir política de leitura para empresa inadimplente**: Hoje a leitura nunca é bloqueada; só escrita passa por subscription_window (cloud/core/api_common.py:138-139, 143-166). Decidir se o app segue mostrando dados durante past_due, na carência de 72 h e após cancelamento, de forma coerente com o 'bloqueio gradual' previsto em docs/ROADMAP-COMPLETUDE.md.
- **[requisito-loja · high] App gratuito: sem compra no app nem link de contratação**: Como é totalmente gratuito, não usar StoreKit nem Play Billing. O app não deve exibir checkout_url (cloud/core/views_platform.py:481), preços de plano (/api/v1/plans, cloud/core/views_auth.py:336-345) nem botão de assinar ou cadastrar (/api/v1/auth/register), para não esbarrar nas regras de pagamento da Apple e do Google.
- **[requisito-loja · high] Conta de demonstração e URL fixa de produção para a revisão das lojas**: O acesso é concedido pelo admin, sem cadastro no app; Apple e Google exigem credenciais de demonstração funcionais (uma empresa demo com AppAccess). Hoje o usuário digita a URL da API e o padrão é http://127.0.0.1:8000 (AppSession.swift:22; RootView.swift:88), e o Info.plist só libera rede local. Em produção a URL HTTPS deve ser fixa. O projeto está com CODE_SIGNING_ALLOWED = NO e sem equipe de assinatura (project.pbxproj:163-209).
- **[requisito-loja · medium] Declarações de privacidade e LGPD para o app**: PrivacyInfo.xcprivacy declara zero dados coletados, mas o app passará a tratar e-mail, identificador de dispositivo, token de push e dados financeiros da empresa. Atualizar o manifesto de privacidade, os rótulos da App Store e o formulário de segurança de dados do Google Play, e publicar política de privacidade. Oferecer encerramento de acesso: revogar dispositivo pelo próprio app (DELETE /api/v1/app/devices/<id>).
- **[lacuna · high] App Android não existe no repositório**: Só há ios/PDV2OwnerMonitor; docs/BACKLOG.md:36 lista 'Aplicativos Android e iOS' como pendente. A publicação no Google Play depende de um app novo (Flutter não está instalado; há Android SDK e JDK 17). A API por token com AppDevice.platform e push_provider apns/fcm atende às duas plataformas.
- **[bug · medium] App mostra dados velhos em silêncio quando a sessão expira**: AppSession.refresh só exibe erro quando snapshot é nil (AppSession.swift, bloco catch por volta das linhas 53-57). Depois de 8 h (cloud/pdvcloud/settings.py:101) o polling recebe 401 e a tela continua com o último retrato sem aviso. Tratar 401 com refresh do token ou retorno ao login e mostrar a idade dos dados.

## android-estrategia

## Recomendação única: (a) manter o SwiftUI nativo e escrever o Android nativo em Kotlin + Jetpack Compose

Nada foi alterado no repositório (somente leitura). O Android deve usar só bibliotecas compatíveis com Kotlin Multiplatform, para deixar o plano B barato.

### O que o código mostra

- **O app iOS é pequeno e funcional.** São 919 linhas em 7 arquivos Swift, com 2 telas (login em `ios/PDV2OwnerMonitor/PDV2OwnerMonitor/RootView.swift:18` e painel em `OwnerDashboardView.swift:4`) e 1 endpoint (`APIClient.swift:54-57`).
- **A regra de negócio está no servidor.** O snapshot inteiro sai de `cloud/core/views_owner.py:45-124`; o cliente só decodifica e desenha.
- **Faltam push, biometria e sessão persistente no iOS.** Não há `LocalAuthentication`, `UserNotifications`, Keychain nem arquivo `.entitlements`. A sessão vive em memória (`AppSession.swift:18`) e o cookie dura 8 h (`cloud/pdvcloud/settings.py:101`).
- **A PWA é o painel web inteiro, não um app de gestão enxuto.** `web/assets/app.js` tem 3.750 linhas e inclui "Solicitar assinatura" (`app.js:2582-2587`) e as telas de plataforma e cobranças (`app.js:59-69`).
- **A PWA não tem push.** `web/sw.js:18-40` só faz cache; não há `pushManager` nem `showNotification` em `web/` ou `cloud/`.
- **A máquina já serve para a opção (a).** Xcode 27.2, Android Studio, plataformas `android-35/36`, build-tools 36.1.0, JDK 17 e CocoaPods estão instalados. Faltam Flutter, Gradle global, fastlane e bubblewrap.

### Comparação

| Opção | Reuso do iOS | Risco 4.2 | Push e biometria | Ferramentas novas | Veredito |
|---|---|---|---|---|---|
| (a) SwiftUI + Compose | 100% | nenhum | APIs de primeira classe | nenhuma | **Recomendada** |
| (b) KMP + Compose Multiplatform | 0% (reescreve a UI) | nenhum | código nativo por plataforma do mesmo jeito | Gradle + Kotlin/Native no iOS | **Plano B** |
| (c) Flutter | 0% | nenhum | plugins | SDK + Dart | Descartada |
| (d) React Native/Expo | 0% | nenhum | módulos Expo | toolchain JS/EAS | Descartada |
| (e) Capacitor/TWA da PWA | 0% | alto na Apple | sem push hoje; TWA usa Web Push e é só Android | bubblewrap/Capacitor | Descartada |

### Por que (a)

1. **O iOS é o caminho mais curto até a App Store.** Qualquer outra opção reescreve o que já existe antes de submeter.
2. **O custo de duplicar é baixo.** Com 5 a 8 telas de leitura, estimo 1.500 a 2.500 linhas de Compose; SwiftUI e Compose são declarativos e mapeiam quase 1:1.
3. **Push e biometria exigem configuração nativa em qualquer framework.** Chave APNs, `google-services.json`, entitlements e permissões não somem com Flutter, Expo ou KMP.
4. **Risco zero na diretriz 4.2.** O texto atual da Apple pede que o app vá "beyond a repackaged website"; a opção (e) é exatamente isso.
5. **A opção (e) também quebra a decisão "totalmente grátis".** Ela levaria "Solicitar assinatura" para dentro do app, o que conflita com "sem compra no app" e com as regras de pagamento das lojas.
6. **A máquina cobre o requisito de SDK das duas lojas.** A Apple exige Xcode 26+ desde 28/04/2026 e a Google exige API 36 desde 31/08/2026.

### Plano B: Kotlin Multiplatform + Compose Multiplatform

- **Estado:** Compose Multiplatform para iOS é estável desde a 1.8.0 (maio de 2025); a linha estável que encontrei é a 1.10.x.
- **Gatilhos para migrar:** o app passar de cerca de 12 a 15 telas, ou a paridade entre os dois apps começar a atrasar entregas.
- **Como manter a porta aberta:** no Android, usar Ktor Client, kotlinx.serialization, kotlinx-datetime e Navigation/Lifecycle do androidx, sem Retrofit nem APIs Android na camada de dados e UI.
- **Risco não verificado:** compatibilidade do Kotlin/Native com o Xcode 27.2; validar antes de migrar.

### Estrutura de pastas proposta

```
ios/PDV2OwnerMonitor/            # existente; renomear o produto para "PDV2 Gestão"
  PDV2OwnerMonitor/
    Core/      APIClient, AppSession, Keychain, Biometria, Push
    Features/  Login, MFA, Painel, Lojas, Terminais, Alertas, Conta
    Resources/ Assets, Info.plist, PrivacyInfo.xcprivacy, *.entitlements
  PDV2OwnerMonitorTests/
android/PDV2Gestao/
  settings.gradle.kts, gradle/libs.versions.toml, gradlew
  app/src/main/kotlin/br/com/<id-definitivo>/
    core/{api,session,biometria,push}
    feature/{login,mfa,painel,lojas,terminais,alertas,conta}
    ui/theme
  app/src/test, app/src/androidTest
mobile/
  contract/openapi.yaml          # contrato único de /api/v1/mobile/*
  contract/fixtures/*.json       # usados nos testes do Django, do iOS e do Android
  design/tokens.json             # cores do PDVTheme (DesignSystem.swift:3-11)
  store/apple/, store/google/    # textos pt-BR, capturas, notas de revisão
cloud/core/views_mobile.py       # liberação de acesso, dispositivos de push, sessão longa
```

### Prazo

O que dita o prazo é o processo das lojas, não o código.

- **Google Play:** conta pessoal criada após 13/11/2023 exige teste fechado com 12 testadores por 14 dias contínuos (página oficial da Google). Blogs dizem que conta de organização é isenta, mas exige D-U-N-S; a página oficial não confirma a isenção.
- **Apple:** a revisão exige conta demo com o backend no ar (diretriz 2.1).
- **Custo do publicador:** "totalmente grátis" vale para o usuário; as contas de desenvolvedor são pagas (de memória: Apple US$ 99/ano, Google US$ 25 uma vez; confirmar).

### O que não foi verificado

- Não rodei build de nenhum dos projetos.
- A maior parte das fontes sobre Play, 4.2 e comparação de frameworks são blogs de terceiros. Conferi no original só as diretrizes da Apple e a página de teste fechado da Google.

### Itens

- **[decisao · high] Estratégia: manter SwiftUI e escrever Android em Kotlin + Jetpack Compose**: O app iOS já existe (919 linhas, 2 telas, 1 endpoint) e a regra de negócio está no servidor (cloud/core/views_owner.py:45-124). A máquina já tem Xcode 27.2, Android Studio, plataformas android-35/36, build-tools 36.1.0 e JDK 17. Risco zero de rejeição 4.2 e push/biometria por API nativa. Custo estimado: 1.500 a 2.500 linhas de Compose para 5 a 8 telas.
- **[decisao · medium] Plano B: Kotlin Multiplatform + Compose Multiplatform**: Compose Multiplatform para iOS é estável desde a 1.8.0 (maio de 2025). Migrar se o app passar de 12 a 15 telas ou se a paridade entre os apps atrasar entregas. Para baratear a migração, o Android deve usar Ktor Client, kotlinx.serialization, kotlinx-datetime e Navigation/Lifecycle do androidx, sem Retrofit. Risco não verificado: compatibilidade do Kotlin/Native com o Xcode 27.2.
- **[decisao · high] Empacotar a PWA (Capacitor/TWA) é inviável para este produto**: A PWA é o painel web inteiro (web/assets/app.js, 3.750 linhas), com 'Solicitar assinatura' em app.js:2582-2587 e telas de plataforma e cobranças em app.js:59-69. Isso conflita com 'sem compra no app' e com a diretriz 4.2 da Apple. Não há push: web/sw.js:18-40 só faz cache e não existe pushManager em web/ nem em cloud/. TWA só atende Android.
- **[decisao · low] Flutter e React Native/Expo descartados**: Ambos jogam fora o SwiftUI existente e exigem toolchain nova (Flutter e Dart não estão instalados). Push e biometria continuam exigindo configuração nativa (chave APNs, google-services.json, entitlements). Para 5 a 8 telas de leitura, o ganho de UI única não compensa.
- **[bug · blocker] Admin do SaaS não consegue entrar no app iOS hoje**: Para conta de plataforma, o login devolve mfa_required ou mfa_setup_required com memberships vazio (cloud/core/views_auth.py:178-186). O app procura um membership com papel owner (AppSession.swift:33) e mostra 'exclusivo do proprietário'. Não existe tela de MFA. O servidor também trava em owner (views_owner.py:48), o que contraria a decisão de o admin liberar acesso a usuários específicos, como gerentes.
- **[bug · high] URL da API padrão é localhost e é editável pelo usuário**: AppSession.swift:22 usa http://127.0.0.1:8000 como padrão, e RootView.swift:86-104 expõe 'Configuração do servidor' na tela de login. O build de loja precisa de URL HTTPS fixa por configuração; do contrário o revisor da Apple cai em localhost (diretriz 2.1). Info.plist:23-27 (NSAllowsLocalNetworking) deve ficar só no Debug.
- **[lacuna · high] Sem push, biometria e sessão persistente no iOS**: Não há LocalAuthentication, UserNotifications, Keychain nem arquivo .entitlements em ios/PDV2OwnerMonitor. A sessão vive em memória (AppSession.swift:18) e o cookie dura 8 h (cloud/pdvcloud/settings.py:101), então reabrir o app exige senha de novo. O backend precisa de registro de dispositivo de push e de sessão longa revogável, compartilhados pelos dois apps.
- **[requisito-loja · medium] Privacy manifest incompleto**: PrivacyInfo.xcprivacy:7-10 declara NSPrivacyAccessedAPITypes e NSPrivacyCollectedDataTypes vazios, mas o app usa UserDefaults (AppSession.swift:22 e :40), que é API de motivo obrigatório (categoria UserDefaults, motivo CA92.1). O app também coleta e-mail no login, o que deve aparecer no manifesto e nos rótulos de privacidade.
- **[requisito-loja · high] Apple: conta demo, sem compra e SDK mínimo**: A diretriz 2.1 exige conta demo com o backend no ar para a revisão. Não pode haver compra nem chamada para compra externa (3.1.3); o texto 'Regularize a assinatura' do painel web não deve ir para o app. Exclusão de conta (5.1.1(v)) só é exigida se o app criar contas; manter o cadastro fora do app. Desde 28/04/2026 o upload exige Xcode 26+; o Xcode 27.2 atende.
- **[requisito-loja · high] Google Play: API 36, teste fechado e formulários**: Apps novos devem mirar a API 36 desde 31/08/2026; a plataforma android-36 e o build-tools 36.1.0 já estão instalados. Conta pessoal criada após 13/11/2023 exige teste fechado com 12 testadores por 14 dias contínuos (página oficial). Blogs dizem que conta de organização é isenta, mas exige D-U-N-S; a página oficial não confirma. Também é preciso AAB com Play App Signing e o formulário de segurança de dados.
- **[decisao · high] Definir identificador e nome antes do primeiro upload**: Info.plist:10 fixa br.com.pdv2.OwnerMonitor como literal (igual em project.pbxproj:192), e o nome exibido é 'PDV2 Monitor' (Info.plist:6), enquanto a PWA usa 'PDV2 Gestão' (web/manifest.webmanifest:3). O identificador não muda depois de publicado. Sugestão: mesmo identificador nas duas lojas, sob o domínio da CrownTech, e Info.plist usando $(PRODUCT_BUNDLE_IDENTIFIER).
- **[lacuna · medium] Assinatura de código desligada no projeto iOS**: project.pbxproj:163-164 e :187-188 têm CODE_SIGNING_ALLOWED = NO e CODE_SIGNING_REQUIRED = NO, e não há DEVELOPMENT_TEAM. Para TestFlight e App Store é preciso ligar a assinatura, definir o time e adicionar a capability de Push Notifications.
- **[lacuna · low] Gráfico usa Swift Charts, exclusivo da Apple**: OwnerDashboardView.swift:1 e :167 usam Swift Charts para vendas por terminal. No Android, usar barras em Canvas do Compose ou a biblioteca Vico, que também serviria no plano B.
- **[tarefa · high] Criar o projeto Android e o contrato compartilhado**: Criar android/PDV2Gestao (Compose, alvo API 36, Gradle wrapper, pastas core/{api,session,biometria,push} e feature/*). Criar mobile/contract com openapi.yaml e fixtures JSON usados nos testes do Django, do iOS e do Android. Criar mobile/design/tokens.json a partir de DesignSystem.swift:3-11 e mobile/store/{apple,google} com os textos das lojas.
- **[tarefa · high] Criar a API mobile no backend**: Criar cloud/core/views_mobile.py com: liberação de acesso por usuário feita pelo admin do SaaS (hoje a trava é roles={'owner'} em views_owner.py:48), registro e remoção de token de push (APNs/FCM), sessão longa revogável para destravar por biometria e fluxo de MFA utilizável pelo app (views_auth.py:178-186).
- **[decisao · low] 'Totalmente grátis' vale para o usuário, não para o publicador**: Publicar exige contas de desenvolvedor pagas. Valores de memória, a confirmar: Apple Developer Program US$ 99/ano e Google Play US$ 25 uma vez. Como a CrownTech é empresa, conta de organização nas duas lojas exige D-U-N-S; iniciar esse pedido cedo, pois ele pode ditar o prazo.

## lojas-requisitos

## Resumo

O app pode ser publicado como gratuito nas duas lojas, mas hoje não passa em nenhuma: o iOS falha antes da revisão e o Android não existe. Publicar também não sai de graça: a Apple cobra US$ 99/ano e o Google cerca de US$ 25 uma vez (taxa do Google vista só em fontes de terceiros).

Li o repositório e as páginas oficiais (diretrizes da Apple atualizadas em 16/10/2026). Não compilei nem rodei nada.

## Estado da máquina e do código

- **Apple exige Xcode 26+ e SDK iOS 26** desde 28/04/2026. O Mac tem Xcode 27.2 e o alvo é iOS 17.0 (`project.pbxproj:165`), então está em conformidade.
- **Google exige target API 36** para apps novos desde 31/08/2026. O SDK local tem `android-36` e JDK 17.
- **Não há projeto Android** no repositório, e Flutter e fastlane não estão instalados.
- **Não há domínio de produção**: `infra/production.env.example:1` ainda é um placeholder.

## Checklist Apple (em ordem)

**O dono faz pessoalmente**
1. Pedir o D-U-N-S da CrownTech (gratuito, pode levar dias).
2. Inscrever-se no Apple Developer Program como **Organização**: Apple Account com 2FA, e-mail e site no domínio da empresa, autoridade legal. Como individual, o vendedor exibido seria o nome da pessoa física.
3. Aceitar os contratos. App gratuito dispensa contrato de apps pagos, banco e impostos.
4. Criar a chave da API do App Store Connect para automação.
5. Aprovar a política de privacidade e as URLs de suporte e de exclusão de conta.
6. Responder o questionário de classificação etária e as declarações de privacidade.

**Código e automação**
1. Colocar o backend em HTTPS público e criar uma empresa de demonstração com vendas do dia.
2. Corrigir os bloqueios de upload (lista abaixo).
3. Criar uma tela "Conta" com política de privacidade, pedido de exclusão e sair.
4. Registrar bundle ID, assinatura e o registro do app: gratuito, categoria Negócios, só Brasil.
5. Gerar capturas: iPhone grande (1320×2868 ou 1290×2796) e médio (1206×2622), mais iPad 13" (2064×2752) se o app continuar universal. De 1 a 10 por tamanho, sem alfa. A leitura sobre qual tamanho de iPhone é obrigatório ficou ambígua, por isso recomendo os dois.
6. TestFlight interno, depois enviar para revisão com conta demo e notas.
7. fastlane cobre assinatura, capturas, metadados e envio. `snapshot` exige um alvo de teste de UI, que não existe.

## Checklist Google (em ordem)

**O dono faz pessoalmente**
1. Criar a conta Play Console como **Organização** (exige D-U-N-S). Conta pessoal criada após 13/11/2023 exige teste fechado com 12 testadores por 14 dias seguidos, mais cerca de uma semana de análise.
2. Concluir as verificações de identidade. Desde 30/09/2026 o Brasil exige desenvolvedor verificado para instalar apps em aparelhos certificados.
3. Criar o app no Console e fazer o primeiro envio manual.
4. Guardar a chave de upload em local seguro.

**Código e automação**
1. Escrever o app Android nativo (Kotlin/Compose) com target e compile SDK 36.
2. Gerar AAB e aderir ao Play App Signing.
3. Preencher: política de privacidade, credenciais de acesso para o revisor, anúncios (não), classificação de conteúdo, público-alvo (adultos) e segurança dos dados (e-mail coletado, criptografado em trânsito, link de exclusão).
4. Ficha da loja: ícone 512×512, imagem de destaque 1024×500, mínimo 2 capturas (recomendado 4 em 1080×1920).
5. Seguir teste interno, fechado e produção. fastlane `supply` e `screengrab` funcionam a partir do segundo envio.

## Bloqueios de upload no iOS

| Problema | Onde |
|---|---|
| Assinatura desligada e sem equipe | `project.pbxproj:163-164, 187-188, 204-205` |
| Ícone com canal alfa | `AppIcon.png` (`sips` indica `hasAlpha: yes`) |
| Manifesto de privacidade vazio, mas o app usa `UserDefaults` | `PrivacyInfo.xcprivacy:9-10`, `AppSession.swift:22,40` |
| Falta `ITSAppUsesNonExemptEncryption` | `Info.plist` |

## Rejeições mais prováveis para este app

1. **Revisor não consegue entrar (2.1).** A URL padrão é `http://127.0.0.1:8000` (`AppSession.swift:22`) e a tela de login expõe "Configuração do servidor" (`RootView.swift:86-104`). Fixar a URL de produção e esconder o campo em Release.
2. **Painel vazio na revisão.** O endpoint só mostra o dia atual (`views_owner.py:20-25`) e exige o recurso `cloud_dashboard` (`views_owner.py:49`). A empresa demo precisa de vendas geradas diariamente.
3. **App para público restrito (3.2).** É o risco de política mais sério dado o modelo "o admin libera o acesso". Apresentar como complemento para qualquer cliente PDV2 e explicar nas notas. O plano B é pedir distribuição não listada.
4. **Funcionalidade mínima (4.2).** O app tem uma tela só de leitura. Acrescentar filtro de período, detalhe por terminal e tela de conta.
5. **Privacidade (5.1.1).** Não há link de política no app nem página no backend. Não há exclusão de conta, e o serviço permite cadastro pela web (`views_auth.py:121`), o que aciona a regra do Google.
6. **Compra fora do app (3.1.3).** Hoje não há preço nem link de assinatura no app; manter assim. A mensagem "Recurso não incluído no plano" (`api_common.py:172`) aparece crua no app (`APIClient.swift:15`).
7. **iPad.** O app é universal (`project.pbxproj:196`), então o revisor testa no iPad.

## Modelo de acesso diverge da decisão do produto

O servidor só aceita o papel `owner` (`views_owner.py:48`) e o app filtra o mesmo (`AppSession.swift:33`). Contas da plataforma caem no fluxo de MFA com `memberships: []` (`views_auth.py:178-186`), que o app não trata. Não existe uma permissão "acesso ao app" por usuário em `views_platform.py`.

## Não confirmado

- Valor da anuidade da Apple em reais.
- Declarações extras do Play (recursos financeiros, saúde, ID de publicidade) não apareceram na página consultada; conferir no Console.
- Verificação de aparelho para contas pessoais do Google.

### Itens

- **[bug · blocker] URL padrão do app aponta para 127.0.0.1 e o login expõe campo de servidor**: ios/PDV2OwnerMonitor/PDV2OwnerMonitor/AppSession.swift:22 usa "http://127.0.0.1:8000" como padrão. RootView.swift:86-104 mostra "Configuração do servidor" com o texto de desenvolvedor "Simulador: http://127.0.0.1:8000" (linha 95). O revisor veria "Sem conexão com o servidor" (APIClient.swift:18), o que leva a rejeição por 2.1. Correção: fixar a URL HTTPS de produção por configuração de build e esconder o campo em Release.
- **[bug · blocker] Assinatura de código desligada e sem equipe no projeto Xcode**: project.pbxproj:163-164, 175-176, 187-188 e 204-205 têm CODE_SIGNING_ALLOWED = NO e CODE_SIGNING_REQUIRED = NO. Não há DEVELOPMENT_TEAM nem CODE_SIGN_STYLE. Nesse estado não é possível gerar um arquivo assinado para o TestFlight ou a App Store. Depende da conta Apple Developer (Team ID) e pode ser automatizado com fastlane e a chave da API do App Store Connect.
- **[bug · high] Ícone do app tem canal alfa**: Assets.xcassets/AppIcon.appiconset/AppIcon.png é 1024x1024 e o sips indica hasAlpha: yes. A validação de upload da Apple recusa ícone com transparência ou canal alfa (erro ITMS-90717, citado de memória). Reexportar como PNG sem alfa. O Google Play, ao contrário, pede ícone 512x512 PNG de 32 bits.
- **[bug · high] Manifesto de privacidade vazio enquanto o app usa UserDefaults**: PrivacyInfo.xcprivacy:9-10 declara NSPrivacyAccessedAPITypes vazio, mas AppSession.swift:22 e :40 usam UserDefaults.standard, que é API de motivo obrigatório desde 01/05/2024. Declarar a categoria NSPrivacyAccessedAPICategoryUserDefaults com o motivo CA92.1 (código confirmado apenas em fontes de terceiros). NSPrivacyCollectedDataTypes (linhas 7-8) também está vazio, embora o e-mail de login seja enviado ao servidor; declarar e-mail para funcionalidade do app, vinculado ao usuário, sem rastreamento, e repetir o mesmo nas declarações de privacidade do App Store Connect.
- **[lacuna · medium] Falta ITSAppUsesNonExemptEncryption no Info.plist**: ios/PDV2OwnerMonitor/PDV2OwnerMonitor/Info.plist (linhas 1-36) não tem a chave. Sem ela, cada build fica em "Missing Compliance" até alguém responder manualmente, o que trava o TestFlight e a automação. O app usa apenas HTTPS do sistema (URLSession), então o valor correto é false. A explicação da chave veio de conhecimento geral, porque a página da Apple não carregou o conteúdo.
- **[lacuna · blocker] Não existe app Android no repositório**: Só há ios/PDV2OwnerMonitor. Flutter e fastlane não estão instalados. O SDK local tem as plataformas android-35 e android-36 e o JDK é 17.0.20.1. Para a Google Play é preciso escrever um app nativo (Kotlin/Compose) com targetSdk e compileSdk 36, exigido para apps novos desde 31/08/2026. Ao mirar o Android 16, conferir as mudanças de comportamento (tela de ponta a ponta, telas grandes); isso vem de conhecimento geral, não foi verificado nesta pesquisa.
- **[lacuna · blocker] Sem backend público em HTTPS nem domínio definido**: infra/production.env.example:1 traz PDV_DOMAIN=pdv.seudominio.com.br, um placeholder. As duas lojas revisam contra um servidor real, que precisa ficar no ar durante toda a revisão. O Info.plist:23-27 só libera rede local, então produção tem de ser HTTPS. settings.py:105-107 já força cookies seguros e redirecionamento SSL fora do modo DEBUG.
- **[requisito-loja · blocker] Conta de demonstração com dados para os revisores**: A Apple (2.1) exige conta demo funcional e o Google exige credenciais na seção de acesso ao app (até 5 conjuntos de instruções). O painel só mostra o dia atual (cloud/core/views_owner.py:20-25 e 61-62) e exige papel owner (linha 48) e o recurso cloud_dashboard (linha 49). É preciso uma empresa demo com assinatura que inclua cloud_dashboard, terminais com heartbeat recente e vendas geradas todo dia por rotina agendada. A conta não pode ser de plataforma, porque essas caem em MFA (views_auth.py:178-186).
- **[requisito-loja · high] Risco de rejeição 3.2 por app de público restrito**: Os relatos de rejeição citam a frase de que o app foi desenhado para um negócio ou organização específica, com indicação de Apple Business Manager ou distribuição não listada. O modelo "o admin do SaaS libera o acesso" se encaixa nesse padrão. Mitigação: descrever o app como complemento para qualquer empresa cliente do PDV2 e explicar nas notas de revisão como se obtém uma conta (o serviço tem cadastro aberto em views_auth.py:121). Plano B: pedir distribuição não listada, que exige o app já submetido à revisão e uma nota nas Review Notes. Há relatos de demora nessa fila.
- **[requisito-loja · medium] Risco de rejeição 4.2 por funcionalidade mínima**: O app tem uma única tela de leitura (OwnerDashboardView.swift, 289 linhas) e o login. A diretriz 4.2 pede recursos, conteúdo e interface que vão além de um site reempacotado. O fato de ser SwiftUI nativo ajuda. Reforçar com filtro de período, detalhe por terminal ou loja, tela de conta e, se possível, alerta de terminal offline. O Google tem política equivalente de funcionalidade mínima.
- **[requisito-loja · blocker] Política de privacidade ausente no app e no backend**: A Apple (5.1.1(i)) exige o link no App Store Connect e dentro do app, em local de fácil acesso. O Google exige URL pública em página web comum (não PDF), na ficha e dentro do app. Não há nenhum Link ou URL nas telas do app, e a busca por "privacidade" em cloud/ e web/ não encontrou página. É preciso publicar a política (com base na LGPD, incluindo retenção e exclusão), uma URL de suporte e ligar ambas a uma tela de conta.
- **[requisito-loja · high] Exclusão de conta: fluxo no app e link web**: A Apple (5.1.1(v)) exige exclusão dentro do app quando ele permite criar conta. Este app não cria conta, mas o serviço permite cadastro pela web (cloud/core/views_auth.py:121, rota em urls.py:29). A regra do Google vale também quando a criação acontece fora do app e exige link web de exclusão, declarado no formulário de segurança dos dados. Não existe endpoint nem tela de exclusão, e também não há recuperação de senha. Implementar "Solicitar exclusão da conta" (endpoint mais página web) com link direto no app. A Apple não aceita só desativar a conta nem obrigar o usuário a ligar ou mandar e-mail.
- **[requisito-loja · medium] Manter o app sem preço, plano ou link de assinatura (3.1.3)**: Por ser complemento gratuito de um SaaS pago, o app não pode ter compra interna nem chamada para comprar fora (3.1.1 e 3.1.3). Hoje as telas não têm preço nem link. Atenção ao texto do servidor "Recurso não incluído no plano" (cloud/core/api_common.py:172), que o app mostra sem tratamento (APIClient.swift:15); trocar por mensagem neutra. No Play, um app publicado como gratuito não pode virar pago depois, o que é coerente com a decisão (regra citada de memória).
- **[requisito-loja · medium] Capturas de tela e iPad**: O app é universal (TARGETED_DEVICE_FAMILY = "1,2" em project.pbxproj:196 e 213), então a Apple exige capturas de iPad 13" (2064x2752 ou 2048x2732) e o revisor testa no iPad. Para iPhone, gerar o tamanho grande (1320x2868, 1290x2796 ou 1260x2736) e o médio (1206x2622 ou 1179x2556); a leitura sobre qual deles é obrigatório ficou ambígua. São de 1 a 10 por tamanho, em JPG ou PNG sem alfa. Google: ícone 512x512, imagem de destaque 1024x500, mínimo 2 capturas e máximo 8, entre 320 e 3840 px, proporção de até 2:1; recomendado 4 ou mais em 1080x1920. As capturas devem mostrar o painel com dados, não só o login.
- **[requisito-loja · medium] Formulários de conteúdo do Play Console**: Preencher: política de privacidade, acesso ao app (credenciais), anúncios (não tem), classificação de conteúdo, público-alvo (adultos) e segurança dos dados (e-mail coletado para funcionalidade e gestão de conta, criptografado em trânsito, com link de exclusão). Declarações de recursos financeiros, saúde e ID de publicidade não apareceram na página consultada; conferir no Console. Entregar em AAB com Play App Signing (ambos citados de memória como obrigatórios para apps novos) e guardar a chave de upload em local seguro.
- **[decisao · high] Conta Apple Developer: organização ou individual**: Organização exige D-U-N-S, entidade legal (nome fantasia não serve), e-mail e site no domínio da empresa e alguém com autoridade legal; o vendedor exibido é a razão social. Individual é mais rápido, mas o vendedor exibido é o nome da pessoa física. Custa US$ 99 por ano nos dois casos. Recomendo organização em nome da CrownTech. O valor em reais aparece na inscrição e não foi confirmado.
- **[decisao · high] Conta Play Console: organização ou pessoal**: Organização exige D-U-N-S com dados iguais aos do cadastro na Dun & Bradstreet. Conta pessoal criada após 13/11/2023 precisa de teste fechado com 12 testadores inscritos por 14 dias seguidos, depois um pedido de acesso à produção com questionário e cerca de uma semana de análise. A página oficial só cita contas pessoais nessa regra. Recomendo organização, com o mesmo D-U-N-S da Apple. A taxa única de cerca de US$ 25 foi vista só em fontes de terceiros.
- **[decisao · medium] Publicar não é gratuito para o desenvolvedor**: O pedido "totalmente grátis" vale para o usuário final: app gratuito, sem compra interna, sem contrato de apps pagos. As taxas das contas de desenvolvedor são inevitáveis: Apple US$ 99 por ano e Google cerca de US$ 25 uma vez. O D-U-N-S é gratuito segundo fonte de terceiros. O pagamento é feito pelo dono; eu não realizo.
- **[decisao · high] Modelo de acesso diverge do código atual**: A decisão diz que o acesso é do admin do SaaS, que libera usuários específicos. O servidor só aceita o papel owner (cloud/core/views_owner.py:48) e o app filtra o mesmo (AppSession.swift:33, com a mensagem das linhas 86-89). Contas de plataforma recebem mfa_required com memberships vazio (views_auth.py:178-186), fluxo que o app não trata. Não há permissão "acesso ao app" por usuário em views_platform.py. É preciso definir se haverá uma permissão por usuário concedida pelo admin, se gerentes entram e se o admin de plataforma usa o app (o que exigiria MFA no app). Isso muda o texto da ficha e o risco de 3.2.
- **[decisao · medium] Identificador do app e países de distribuição**: O bundle ID atual é br.com.pdv2.OwnerMonitor (Info.plist:10 e project.pbxproj:192). Ele fica permanente depois do primeiro envio, e o mesmo vale para o applicationId no Android. Convém usar um domínio que a CrownTech controle de fato. Recomendo distribuir só no Brasil: evita a declaração de comerciante exigida na União Europeia e simplifica as fichas, que ficam só em português.
- **[decisao · low] Manter iPad ou publicar só para iPhone**: Manter o app universal (project.pbxproj:196) obriga a ter capturas de iPad 13" e um layout aprovado no iPad pelo revisor. O Info.plist:28-34 já declara as quatro orientações. Publicar só para iPhone reduz o trabalho, mas a família de dispositivos não pode ser reduzida depois da publicação sem atrito (afirmação de memória, não verificada).
- **[tarefa · high] Providências pessoais do dono, em ordem**: 1) Pedir o D-U-N-S da CrownTech. 2) Ter site e e-mail no domínio da empresa. 3) Inscrever-se no Apple Developer Program com Apple Account com 2FA e pagar. 4) Criar a conta Play Console, pagar e concluir as verificações de identidade. 5) Aceitar os contratos nas duas lojas. 6) Criar a chave da API do App Store Connect e, depois, a conta de serviço do Google para automação. 7) Aprovar o texto da política de privacidade. 8) Responder a classificação etária da Apple, a classificação de conteúdo do Google e as declarações de privacidade, que são feitas em nome da empresa. 9) Guardar a chave de upload do Android.
- **[tarefa · medium] Automação com fastlane**: fastlane não está instalado. No iOS ele cobre assinatura (match ou assinatura automática com a chave da API), build, capturas (snapshot), metadados (deliver) e TestFlight (pilot). O snapshot exige um alvo de teste de UI, e o projeto só tem o alvo do app. No Android, supply envia AAB e ficha e screengrab gera capturas; o app precisa ser criado no Console e o primeiro envio costuma ser manual. Ficam manuais os questionários de privacidade, classificação e segurança dos dados e o pedido de distribuição não listada.
- **[requisito-loja · medium] Verificação de desenvolvedor Android em vigor no Brasil**: Desde 30/09/2026, apps só instalam e atualizam em aparelhos Android certificados no Brasil se estiverem registrados por um desenvolvedor verificado, qualquer que seja a origem do download. Quem publica pelo Play Console faz a verificação no próprio Console. Isso também vale se um APK for distribuído fora da loja. O status atual da aplicação da regra não foi confirmado nas fontes.

