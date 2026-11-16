# PDV2 Gestão para iOS

Aplicativo SwiftUI **gratuito e só de consulta** para quem administra empresas que
usam o PDV2. Não vende nada, não mostra preço, plano nem assinatura. O acesso é
liberado pelo administrador do PDV2 (modelo `AppAccess`); a pessoa entra com o
mesmo e-mail e senha do painel web.

- Nome na tela: **PDV2 Gestão** · bundle `br.com.crowntech.pdv2gestao`
- iPhone, iOS 17 ou mais novo, só retrato, sem dependências externas
- Projeto: `PDV2OwnerMonitor.xcodeproj` (a pasta manteve o nome antigo); alvo e
  scheme `PDV2Gestao`; testes `PDV2GestaoTests`
- Contrato da API: `docs/auditoria-2026-11/app-gestao.md` e o contrato de
  integração (rotas `/api/v1/app/*`, token Bearer, sem cookie nem CSRF)

## O que o app faz

| Perfil | Abas |
|---|---|
| Pessoa de empresa cliente | Resumo · Vendas · Caixa · Estoque · Mais |
| Equipe CrownTech (plataforma) | Plataforma · Resumo · Vendas · Caixa · Mais (Estoque fica em Mais) |

- **Entrada:** login (S1), código do autenticador ou de recuperação para a equipe
  (S2), tela "Acesso ao app não liberado" sem deixar sessão guardada (S3), oferta
  de Face ID depois do 1º login (S4). Nas aberturas seguintes, Face ID (ou código
  do aparelho) e renovação automática da sessão.
- **Barra de contexto:** empresa, loja e período (Hoje, Ontem, 7 dias, Mês),
  guardados no aparelho.
- **Resumo:** faturamento, vendas, ticket médio, cancelamentos, recebido por forma
  de pagamento, fiado, gráfico por hora/dia, caixas, estoque, fiado e terminais.
  Atualiza sozinho a cada 60 s com "Hoje" e app aberto; puxar para atualizar em
  todas as telas; rodapé "Atualizado há X"; faixa "Não foi possível atualizar.
  Mostrando dados de HH:MM." quando a rede cai.
- **Vendas** (lista + detalhe), **Caixa** (abertos/fechados + detalhe),
  **Estoque**, **Fiado**, **Terminais**, **Conflitos** (só leitura).
- **Conta:** aparelhos conectados com "Desconectar", Face ID, política de
  privacidade, suporte, "Excluir minha conta" e "Sair".
- **Plataforma:** empresas com saúde e "Abrir empresa"; para o superusuário,
  acessos ao app (listar, liberar, revogar com motivo) e aparelhos (desconectar).

## Sessão e segurança

- Access token só em memória; refresh token + `device_id` + URL do servidor no
  Keychain (`kSecClassGenericPassword`, service `br.com.crowntech.pdv2gestao`,
  account `session`, `AfterFirstUnlockThisDeviceOnly`). Se a URL mudar, a sessão
  é descartada.
- Renovação "single-flight" (`SessionManager`, um `actor`): uma renovação por vez;
  `token_expired` renova e repete a chamada uma vez; renova antes se faltar menos
  de 60 s.
- `URLSessionConfiguration.ephemeral`, sem cookies, tempo limite de 20 s.
- Erros tratados pelo `code` do servidor (`APIErrorCatalog`): sessão encerrada,
  aparelho desconectado, acesso não liberado, empresa/loja sem acesso, sem
  permissão etc. Corpo de erro 5xx nunca é mostrado.
- A trava por Face ID é da interface (abertura a frio e volta do segundo plano
  após 5 min); a chave do Keychain não depende da biometria.

## Configurações de build

| | Debug | Release |
|---|---|---|
| xcconfig | `Config/Debug.xcconfig` | `Config/Release.xcconfig` |
| Servidor | `http://127.0.0.1:8000` (campo "Servidor (desenvolvimento)" no login) | `https://pdv2.crowntech.com.br` fixo; `http` é recusado |
| Info.plist | `Resources/Info-Debug.plist` (com `NSAllowsLocalNetworking`) | `Resources/Info.plist` |
| Símbolos | dwarf | dwarf-with-dsym, `-O`, wholemodule |

Assinatura: `CODE_SIGN_STYLE = Automatic` e `DEVELOPMENT_TEAM` vazio — o dono
preenche o Team ID no Xcode antes de arquivar.

## Compilar e testar

```bash
cd ios/PDV2OwnerMonitor
xcodebuild -project PDV2OwnerMonitor.xcodeproj -scheme PDV2Gestao \
  -sdk iphonesimulator -destination 'platform=iOS Simulator,name=iPhone 17 Pro' \
  -derivedDataPath /tmp/pdv2-dd CODE_SIGNING_ALLOWED=NO build test
xcodebuild -project PDV2OwnerMonitor.xcodeproj -scheme PDV2Gestao -configuration Release \
  -sdk iphonesimulator -destination 'platform=iOS Simulator,name=iPhone 17 Pro' \
  -derivedDataPath /tmp/pdv2-dd CODE_SIGNING_ALLOWED=NO build
rm -rf /tmp/pdv2-dd
```

Sem assinatura o simulador não dá acesso ao Keychain: o teste
`testKeychainStoreRoundTrip` é pulado nesse caso (passa com `CODE_SIGN_IDENTITY=-`).

Testes (`PDV2GestaoTests`): decodificação de todos os exemplos JSON do contrato
(`PDV2GestaoTests/Fixtures`), corpos enviados (login, MFA, concessão de acesso),
formatação de dinheiro/quantidade/datas, `code` → ação, recusa de `http` fora do
Debug, renovação de token (repetição única, single-flight, renovação antecipada,
aparelho revogado, sem rede) e fluxo de entrada do `AppState`.

## Modo demonstração ("Ver demonstração")

- Botão na tela de entrada, também em Release: abre o app inteiro com os dados
  fictícios do **Mercadinho Exemplo** (2 lojas: Centro e Jardim) e da Padaria
  Exemplo (para testar a troca de empresa). Serve para a revisão das lojas, sem senha.
- Dados gerados no aparelho (`Core/DemoData.swift`, `DemoGestaoAPI`) a partir da
  data de hoje e de uma semente fixa: Resumo, vendas com detalhe, caixas abertos e
  fechados com detalhe, estoque, fiado, terminais, conflitos e conta.
- Sem nenhuma chamada de rede, sem sessão, sem token, sem Keychain e sem gravar
  preferências (empresa/loja/período/Face ID). Faixa fixa no alto "Demonstração ·
  dados fictícios" com "Sair da demonstração", que volta à entrada sem deixar estado;
  "Sair" em Conta faz o mesmo.
- Testes: `PDV2GestaoTests/DemoModeTests` (sem rede, nada gravado, saída limpa) e
  `PDV2GestaoUITests/DemoModeUITests` (botão, faixa em todas as abas, saída).

## Dados fictícios (Previews e capturas)

- Todas as telas têm `#Preview` com os exemplos do contrato (`PreviewGestaoAPI`).
- Só em Debug: `-PDV2Demo` abre direto na demonstração; `-PDV2Tab
  resumo|vendas|caixa|estoque|mais`, `-PDV2More fiado|terminais|conflitos|conta` e
  `-PDV2Period hoje|ontem|7d|mes` escolhem a tela. `-PDV2DemoPlatform` usa as
  fixtures do contrato com o perfil da equipe.
- Capturas da App Store (iPhone 6,9", 1320×2868, sem transparência) em
  `store/apple/capturas/`, geradas com `xcrun simctl status_bar … override
  --time 9:41 --batteryLevel 100` e `xcrun simctl io … screenshot`. O app é só
  iPhone (`TARGETED_DEVICE_FAMILY = 1`): não há capturas de iPad.

## Scripts

- `scripts/generate_project.py` — regenera o `project.pbxproj` e o scheme
  compartilhado a partir das pastas (rode depois de criar/remover arquivos).
- `scripts/make_fixtures.py` — grava as fixtures do contrato e
  `Core/PreviewFixtures.swift`:
  `python3 scripts/make_fixtures.py PDV2GestaoTests/Fixtures PDV2OwnerMonitor/Core/PreviewFixtures.swift`
- `scripts/make_icon.swift` — gera o ícone 1024×1024 opaco:
  `swift scripts/make_icon.swift PDV2OwnerMonitor/Resources/Assets.xcassets/AppIcon.appiconset/AppIcon.png`

## Loja

Textos da App Store em `store/apple/`. Antes de enviar: Team ID, backend em produção e páginas `/privacidade/`,
`/suporte/` e `/excluir-conta/` publicadas.
