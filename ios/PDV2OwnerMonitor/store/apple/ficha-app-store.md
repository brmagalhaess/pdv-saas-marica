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

## Notas para a revisão (rascunho — credenciais o dono preenche)

Aplicativo gratuito e somente leitura para empresas clientes do PDV2. As empresas se cadastram no site; o administrador libera o uso do app. Conta de demonstração: e-mail [PREENCHER], senha [PREENCHER], sem verificação em duas etapas. Não há compras nem conteúdo pago.

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
- Capturas exigidas: 1320×2868 (6,9") e 1206×2622 (6,3"), mostrando o **Resumo com dados** (não o login).
  Em Debug, abrir o app com `-PDV2Demo` mostra dados fictícios para as capturas.

## Pendências do dono

- Team ID da conta Apple Developer (Organização) no Xcode.
- Conta de demonstração real (empresa demo sem MFA) e backend em produção durante a revisão.
- Aprovar política de privacidade e e-mail de suporte publicados no site.
