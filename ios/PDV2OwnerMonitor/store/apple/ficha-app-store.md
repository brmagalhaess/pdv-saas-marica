# PDV2 Gestão — ficha da App Store (pt-BR)

Textos iguais aos da Google Play (contrato §14). Rascunho para o dono aprovar.

## Dados gerais

| Campo | Valor |
|---|---|
| Nome | PDV2 Gestão |
| Subtítulo (≤30) | Acompanhe suas lojas PDV2 |
| Bundle ID | br.com.crowntech.pdv2gestao |
| Categoria principal | Negócios |
| Preço | Grátis — sem compras no app, sem anúncios |
| Disponibilidade | Somente Brasil |
| Idioma principal | Português (Brasil) |
| Classificação etária | 4+ |
| Copyright | © 2026 CrownTech |

## Descrição

O PDV2 Gestão é o complemento gratuito do PDV2 para quem administra a empresa. Acompanhe pelo celular, em tempo real, o faturamento, as vendas, o ticket médio e os cancelamentos de cada loja; veja quanto entrou em dinheiro, Pix e cartão; confira os caixas abertos, sangrias e quebras; saiba quais produtos estão zerados ou abaixo do mínimo; acompanhe o fiado em aberto e vencido; e veja se os terminais estão online. Filtre por empresa, loja e período (hoje, ontem, 7 dias ou mês). O app é só de consulta e protegido por Face ID ou biometria. O acesso é liberado pelo administrador do PDV2: entre com o mesmo e-mail e senha do painel web.

## Palavras-chave (≤100)

pdv,vendas,caixa,estoque,loja,gestão,faturamento,fiado,comércio,varejo

## URLs

| Campo | URL |
|---|---|
| Suporte | https://pdv2.crowntech.com.br/suporte/ |
| Política de privacidade | https://pdv2.crowntech.com.br/privacidade/ |
| Exclusão de conta | https://pdv2.crowntech.com.br/excluir-conta/ |
| Marketing | https://pdv2.crowntech.com.br/ |

## Notas para a revisão

Aplicativo gratuito e somente leitura para empresas clientes do PDV2. As empresas se cadastram no site; o administrador do PDV2 libera o uso do app, por isso não há cadastro dentro do app. Para revisar sem conta, toque em **"Ver demonstração"** na tela de entrada: o app abre inteiro com dados fictícios da empresa "Mercadinho Exemplo" (sem senha, sem conexão com o servidor). Uma faixa no alto indica "Demonstração · dados fictícios" e o botão "Sair da demonstração" volta à tela de entrada. Não há compras nem conteúdo pago.

(Em inglês, para o campo "Notes": This is a free, read-only companion app for business customers of PDV2; access is granted by the PDV2 administrator, so there is no in-app sign-up. To review without an account, tap "Ver demonstração" (View demo) on the sign-in screen: the whole app opens with sample data for a fictional store, "Mercadinho Exemplo", with no password and no server connection. A banner at the top reads "Demonstração · dados fictícios" (Demo · sample data), and "Sair da demonstração" returns to the sign-in screen. No purchases or paid content.)

## Privacidade (App Privacy)

- Rastreamento: **não**.
- Dados coletados, todos **vinculados à pessoa**, uso **Funcionalidade do app**, sem rastreamento:
  - Informações de contato: **Nome**, **E-mail**.
  - Identificadores: **ID do usuário**, **ID do dispositivo**.
- Igual ao `PrivacyInfo.xcprivacy` (que declara também UserDefaults com motivo `CA92.1`).
- Exclusão de conta: no app (Conta › Excluir minha conta) e em https://pdv2.crowntech.com.br/excluir-conta/.

## Exportação / criptografia

`ITSAppUsesNonExemptEncryption = false` (só HTTPS do sistema).

## Artes

- Ícone 1024×1024 opaco: `PDV2OwnerMonitor/Resources/Assets.xcassets/AppIcon.appiconset/AppIcon.png`.
- Capturas 6,9" (1320×2868, PNG sem transparência) em `store/apple/capturas/`, geradas no modo demonstração
  (iPhone 17 Pro Max, barra de status 9:41 e bateria cheia): `01-resumo`, `02-vendas`, `03-caixa`,
  `04-estoque`, `05-fiado`. O app é só iPhone (`TARGETED_DEVICE_FAMILY = 1`): sem capturas de iPad.
  Em Debug, `-PDV2Demo` com `-PDV2Tab`/`-PDV2More`/`-PDV2Period` abre direto em cada tela.

## Pendências do dono

- Team ID da conta Apple Developer (Organização) no Xcode.
- Backend em produção durante a revisão (o modo "Ver demonstração" não depende dele).
- Aprovar política de privacidade e e-mail de suporte publicados no site.
