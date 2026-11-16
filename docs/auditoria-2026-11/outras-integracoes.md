## Integrações do PDV2: o que existe e a ordem recomendada

**Ponto de partida, conferido no repositório**
- O caixa só registra Pix e cartão **declarados pelo operador**, sem TEF, adquirente, Pix dinâmico ou confirmação bancária (`docs/STATUS-V2.md:26`; métodos aceitos em `cloud/core/views_terminal.py:384`).
- O Asaas já tem conector com `billingType` PIX, hoje só para assinaturas (`integrations/billing.py:37-45`). O webhook já é autenticado e deduplicado (`STATUS-V2.md:17`).
- O SMTP está ligado por variáveis de ambiente (`cloud/pdvcloud/settings.py:123-129`), mas o provedor de produção está pendente (`STATUS-V2.md:28`).
- Balança: há leitura serial genérica por Web Serial e etiqueta EAN-13 com prefixo 2 (`terminal/static/terminal.js:19-23`). O protocolo do fabricante ficou para depois do piloto (`docs/HARDWARE.md:11`).
- XML de NF-e de compra já é importado (`cloud/core/nfe.py:127`). Delivery, conciliação e TEF estão listados como ausentes (`STATUS-V2.md:37`).

**Ordem recomendada**

**Onda 1: antes ou durante o primeiro piloto (2 a 4 semanas)**
1. **E-mail transacional (P).** Configurar Resend (3 mil/mês grátis) ou SES (cerca de US$ 0,10 a 0,16 por mil) com SPF, DKIM e DMARC. Destrava convite, recuperação de senha e envio ao contador. É só configuração.
2. **Pix dinâmico no caixa (M).** É o maior ganho operacional, porque elimina o "Pix declarado".
   - Fluxo: o terminal pede a cobrança à nuvem, a nuvem chama `POST /v3/payments` (PIX, `externalReference`) e depois `GET /pixQrCode`. O terminal mostra o QR e consulta o status. O webhook `PAYMENT_RECEIVED` confirma e o terminal fecha a venda.
   - Offline: cair para Pix estático (BR Code gerado localmente) com conferência manual.
   - Taxa: as páginas do Asaas se contradizem (grátis × cobrada), então confirmar no contrato.
   - Segundo PSP: Efí (`/v2/cob`, mTLS) para lojistas que querem a própria conta.
3. **CNPJ/CEP via BrasilAPI (P).** Autopreenche empresa, fornecedor e cliente. Custo zero, com cache.
4. **Cosmos/GS1 no cadastro de produto (P).** Acelera a migração de catálogo das mercearias.

**Onda 2: após o piloto validar (1 a 2 meses)**
5. **WhatsApp Cloud API (M).** Comprovante digital e lembrete de fiado com link Pix (combina com o item 2).
   - Templates utility custam cerca de US$ 0,007 por entrega. Desde 01/10/2026, utility dentro da janela de 24 h também é cobrado.
   - Onboarding por embedded signup, um WABA por lojista. Exigir opt-in registrado (LGPD).
6. **Pacote do contador (M).** Primeiro um ZIP mensal de XMLs autorizados e cancelados com resumo por pagamento e CFOP, enviado por e-mail. Depois, o TXT "Domínio Sistemas com Separador" ou integração via Onvio/Box-e.
7. **Balança com protocolo (M).** Perfis ENQ/STX-5 dígitos-ETX (Toledo P03, Prix PRT5 em 4800-8-N-1, Filizola), testados no equipamento do piloto.
8. **Maquininha integrada, começando pelo Mercado Pago Point (M/G).**
   - É nuvem a nuvem: cria-se a ordem no device, o operador toca "Cobrar" e o resultado chega por webhook. Não exige app nativo.
   - Usar a nova **Orders API**; a payment-intents será descontinuada.

**Onda 3: por demanda comprovada**
9. **SmartPOS Android (G).** Stone SDK, PagBank PlugPag, Cielo LIO. Exige um app Android do PDV2 rodando na maquininha, mais programa de parceiros e testes físicos.
10. **TEF SiTef/PayGo (G).** CliSiTef via ctypes no terminal Windows, com homologação obrigatória. Só para mercados com vários adquirentes.
11. **Nuvemshop, depois Mercado Livre (G).** Sincronização de estoque e preço por webhooks com assinatura. Antes, resolver a reserva de estoque frente à venda offline (`STATUS-V2.md:45`). Alternativa: um hub (Bling/Olist).
12. **SSO Google (P).** Conveniência; não pode contornar o MFA dos admins SaaS.
13. **Conciliação bancária.** Primeiro importar OFX/CNAB (custo zero). Pluggy/Belvo custam milhares de reais por mês (relato de dev, sem tabela pública), o que não se justifica agora.
14. **iFood/delivery (G).** Polling de eventos com homologação; pouco aderente a mercearia e moda neste momento.

**Por que essa ordem**
- Itens P de custo zero entram primeiro porque destravam a produção (`STATUS-V2.md:28`).
- Pix dinâmico vem logo depois: reaproveita o conector e o webhook do Asaas e é o pagamento mais usado no varejo pequeno.
- Integrações que exigem hardware, homologação de terceiros ou app nativo (SmartPOS, TEF, iFood) ficam para depois que um piloto real mostrar demanda, como `STATUS-V2.md:37` já orienta ("não anunciar cobertura integral").

**Ressalvas da pesquisa**
- Preços de Pluggy, Belvo, Cosmos, GS1 e SiTef não são públicos; os valores citados são relatos ou dados antigos.
- O valor do WhatsApp no Brasil vem de fontes secundárias, que divergem entre si.
- Nenhuma integração foi testada; os endpoints vêm das documentações e de buscas, e precisam ser conferidos na versão vigente antes de implementar.

## Itens
- [high/M] **Pix dinâmico no caixa com confirmação automática (reusar o conector Asaas; Efí como 2º PSP)**: O que já existe: o conector Asaas cria assinatura com billingType PIX (integrations/billing.py:37-45) e já há webhook autenticado e deduplicado (docs/STATUS-V2.md:17). Mas o caixa só registra o Pix declarado pelo operador (STATUS-V2.md:26, cloud/core/views_terminal.py:384). Como integrar: (1) o terminal chama a nuvem; (2) a nuvem cria a cobrança avulsa no Asaas (POST /v3/payments billingType=PIX, externalReference=id da venda) e busca o QR em GET /v3/payments/{id}/pixQrCode; (3) o terminal mostra o QR e o copia-e-cola e consulta a nuvem a cada 2 s (ou usa SSE); (4) o webhook PAYMENT_RECEIVED marca o pagamento como confirmado e o terminal fecha a venda. Sem internet, cair para Pix estático (BR Code EMV gerado localmente com chave da loja e valor), com conferência manual. Alternativa direta via API Pix do BCB: Efí (POST /v2/cob, PUT /v2/webhook/{chave}), que exige certificado mTLS .p12 e credenciais por loja (subconta ou credencial do lojista). Custo: as páginas do Asaas se contradizem (ora 'QR dinâmico gratuito', ora taxa por Pix recebido). Confirmar no contrato; em geral fica abaixo de R$ 2 por Pix. Dificuldade média: modelagem de 'pagamento pendente' no terminal offline, expiração, estorno e conciliação.
- [high/G] **Maquininha integrada (Mercado Pago Point Orders API primeiro; Stone/PagBank via SmartPOS Android depois)**: Hoje não existe: o caixa registra crédito/débito declarado, sem TEF nem adquirente (STATUS-V2.md:26,37). Caminhos por complexidade: (a) Mercado Pago Point, nuvem a nuvem. O backend cria a ordem no device (a API legada é /point/integration-api/devices/{id}/payment-intents com amount em centavos; o MP orienta migrar para a nova Orders API), o operador toca 'Cobrar' na Point Smart e o resultado chega por webhook. Sem SDK local e sem homologação pesada, mas exige internet. (b) Stone e PagBank SmartPOS: SDKs Android (stone-payments/sdk-android; PagBank PlugPagServiceWrapper, terminais A920/P2 com ativação por código), que exigem um app Android rodando NA maquininha. Isso pede um 'terminal Android' do PDV2 (PWA não basta) e cadastro no programa de parceiros. (c) Cielo LIO: mesmo modelo de app embarcado. Custo: sem mensalidade de integração para o MP; as taxas MDR são do lojista. Dificuldade: (a) média, (b)/(c) alta, com app nativo, lojas de aplicativos e teste físico.
- [high/P] **E-mail transacional (Resend ou Amazon SES sobre o SMTP já configurado)**: O Django já usa o backend SMTP por variáveis PDV_EMAIL_* (cloud/pdvcloud/settings.py:123-129), e o SMTP de produção ainda está pendente (STATUS-V2.md:28). Integrar = escolher o provedor, configurar SPF/DKIM/DMARC no domínio e preencher as variáveis. Resend: grátis até 3.000 e-mails/mês (limite de 100/dia); Pro a US$ 20 para 50 mil. SES: cerca de US$ 0,10 a 0,16 por mil (planos mudaram em 2026). Usos: convite de equipe, recuperação de senha, comprovante/2ª via, alertas de assinatura e envio do XML ao contador. Dificuldade baixa: só DNS e templates.
- [medium/P] **Consulta de CNPJ/CEP (BrasilAPI com fallback ViaCEP/ReceitaWS)**: Os formulários pedem CPF/CNPJ digitado à mão, sem autopreenchimento (web/assets/app.js:517, 2630-2673). Integrar no backend Django: GET brasilapi.com.br/api/cnpj/v1/{cnpj} e /api/cep/v2/{cep}, com cache no PostgreSQL e timeout curto. Fallback para viacep.com.br/ws/{cep}/json. Não há limite numérico publicado, mas os termos proíbem varreduras, então chamar o serviço só por ação do usuário. Custo zero. Ganho: onboarding da empresa, cadastro de fornecedor/cliente e dados do emitente para a NFC-e.
- [high/M] **WhatsApp Business Cloud API (comprovante e cobrança de fiado)**: Hoje existe o fiado recebido no caixa e o comprovante impresso (commit 23864eb), sem envio digital. Integrar via Meta Cloud API direta (Graph API /{phone-number-id}/messages) com templates de categoria utility aprovados ('comprovante_venda', 'lembrete_fiado' com link Pix), webhook de status (entregue/lido) e opt-in registrado do cliente (LGPD). Precisa de número dedicado e verificação do negócio no Meta Business por empresa cliente. Para um SaaS, o modelo é embedded signup, um WABA por lojista. Custo: cobrança por template entregue. Utility no Brasil fica em torno de US$ 0,007 (R$ 0,04), marketing em torno de US$ 0,06. A partir de 01/10/2026 utility dentro da janela também é cobrado. Repassar ao lojista. Dificuldade média, mais pelo onboarding Meta do que pelo código.
- [medium/M] **Exportação contábil (XMLs + resumo e leiaute Domínio)**: Já existe a importação de XML de NF-e de compra (cloud/core/nfe.py:127) e o arquivamento de XML autorizado do Focus (STATUS-V2.md:11). Falta a saída para o contador: (1) botão mensal 'Pacote do contador' com um ZIP de XMLs de NFC-e/NF-e autorizados e cancelados, relatório de vendas por forma de pagamento e CFOP/NCM, enviado por e-mail; (2) depois, o TXT 'Domínio Sistemas com Separador' (registros de notas de saída, reduções e lançamentos) ou integração via Onvio/Box-e. A Domínio importa NFC-e pelo Portal do Cliente quando as notas estão disponíveis. SPED Fiscal/EFD é responsabilidade do contador; não gerar no início. Custo zero (Onvio API via parceria). Dificuldade baixa no ZIP, média no leiaute Domínio.
- [medium/M] **Balanças Toledo/Filizola: protocolo serial específico**: Já existe leitura serial genérica por Web Serial (regex de peso em linhas, baudRate configurável) e decodificação de etiqueta EAN-13 com prefixo 2, peso ou preço (terminal/static/terminal.js:2, 19-23). HARDWARE.md:11 deixa o protocolo para depois do piloto. Falta o modo 'pergunta e resposta': enviar ENQ (0x05) e ler STX + 5 dígitos + ETX (padrão Filizola/Toledo P03). Na Toledo Prix 3 Light, relatos indicam PRT5 em 4800-8-N-1, e o padrão do fabricante pode ser 2400 bps. Implementar perfis (Toledo P03/PRT5, Filizola, Elgin DP), tratar peso instável/negativo e testar com o equipamento real. Exportação de itens para a balança etiquetadora (MGV/Toledo) fica como fase 2. Custo: hardware de bancada. Dificuldade média.
- [medium/P] **Cadastro por código de barras (Cosmos Bluesoft / GS1 Verified)**: Ao bipar um EAN desconhecido no cadastro do painel, consultar a API Cosmos (token no header X-Cosmos-Token, GET /gtins/{gtin}), que retorna descrição, NCM, marca e imagem. A alternativa oficial é a GS1 Brasil Verified (GET api.gs1br.org/provider/v2/verified?gtin=, com client_id e access_token, mediante contrato). Chamar só no painel/nuvem com cache global por GTIN, sem fluxo no terminal offline. Custo: Cosmos tem plano gratuito limitado e planos pagos por volume, preços sob consulta; GS1 por contrato. Ganho alto para mercearias com migração de catálogo. Dificuldade baixa.
- [low/P] **SSO Google (OAuth/OIDC) no painel**: O painel usa sessão + CSRF, e há MFA TOTP para a plataforma (cloud/core/views_auth.py:6-87, STATUS-V2.md:29). Adicionar 'Entrar com Google' via OIDC (biblioteca django-allauth ou authlib), vinculando por e-mail verificado a um usuário existente, sem criar empresa automaticamente. Não vale para o PIN do operador no terminal. Custo zero. Dificuldade baixa a média: cuidar de vinculação de conta e de não contornar o MFA de admins SaaS.
- [medium/G] **TEF tradicional (SiTef/CliSiTef, PayGo)**: Necessário para mercados médios com PinPad em vários adquirentes no mesmo caixa. Integração: a DLL/SO CliSiTef (ou PayGo Client) rodando no terminal Windows, chamada pelo Flask via ctypes, com o fluxo iterativo de menus do CliSiTef, comprovantes, confirmação/desfazimento e pendências após queda. Exige homologação obrigatória (SiTef/Software Express, PayGo ou SkyTef); relatos antigos citam participação de 30-50% na mensalidade ou um custo fixo em torno de R$ 300, então pedir proposta atual. Dificuldade alta. Só depois de validar demanda nos pilotos; para micro e pequenos lojistas, a maquininha integrada (POS/Point) resolve.
- [medium/G] **E-commerce e marketplaces (Nuvemshop, Mercado Livre, Shopee, Tray)**: Valor principal para moda: sincronizar estoque e preço e baixar pedidos. Nuvemshop: app OAuth, REST em api.nuvemshop.com.br/v1/{store_id}, webhooks com assinatura e payload mínimo (buscar o recurso depois), API de locations para multiestoque. Mercado Livre: OAuth com refresh, notificações por tópico (orders_v2, items), PUT /items/{id} para estoque. Shopee e Tray seguem modelo parecido (assinatura HMAC na Shopee). No PDV2 exige mapear SKU/variação, reservar estoque e tratar o conflito com a venda offline do terminal (STATUS-V2.md:45). Custo: APIs gratuitas; homologação de app na Nuvemshop/ML. Dificuldade alta por conector, por ser bidirecional e por isso começar só por Nuvemshop. Alternativa barata: integrar com um hub (Bling, Olist, Plugg.to).
- [low/G] **iFood / delivery (Merchant API ou Open Delivery)**: O STATUS lista delivery como ausente (STATUS-V2.md:37), e o foco é mercados/mercearias e moda. Integração iFood: credenciais de software (client_id/secret) e autorização do merchant, polling de eventos a cada ~30 s com acknowledgment, confirmar/despachar pedido e sincronizar catálogo. Exige homologação no portal developer.ifood.com.br. Para mercados, iFood Mercado e Rappi têm catálogos grandes, com sincronização de estoque. Open Delivery (ABRASEL) padroniza outros apps (Keeta etc.). Custo: sem taxa de API, mas comissão do marketplace para o lojista. Dificuldade alta e pouco aderente ao público-alvo atual; deixar depois dos pilotos.
- [low/M] **Open Finance / conciliação bancária (Pluggy, Belvo; OFX como passo zero)**: STATUS-V2.md:37 lista conciliação bancária como pendente. Passo zero, com custo zero: importar extrato OFX/CNAB no Financeiro e casar com vendas Pix/cartão e recebíveis. Depois: Pluggy (widget Connect, item por banco, GET /transactions, webhooks de atualização; também é iniciadora Pix) ou Belvo. Custo: só sob consulta. Um relato de dev cita cerca de R$ 2,5 mil/mês na Pluggy e R$ 6 mil/mês na Belvo como mínimo, o que é alto para o tamanho atual. Dificuldade média (OFX) a alta (consentimento Open Finance, renovação e LGPD). Postergar a API; fazer OFX quando o Financeiro pedir.

Fontes:
https://docs.asaas.com/docs/cobrancas-via-pix
https://docs.asaas.com/docs/webhook-para-cobrancas
https://www.asaas.com/precos-e-taxas
https://dev.efipay.com.br/en/docs/api-pix/webhooks/
https://dev.efipay.com.br/en/docs/api-pix/credenciais/
https://www.mercadopago.com.br/developers/en/docs/mp-point-legacy/integration-configuration/integrate-with-pdv/payment-processing
https://awesome.ecosyste.ms/projects/github.com%2Fstone-payments%2Fsdk-android
https://github.com/pagseguro/pagseguro-sdk-plugpagservicewrapper
https://docs.apis-fiserv.com/latam/docs/simplified-interface-with-the-application
https://www.projetoacbr.com.br/forum/topic/20071-duvida-custos-tef-paygo/
https://www.softwareexpress.com.br/pt/blog/sitef--entenda-o-que-e-tef-e-por-que-ele-e-estrategico-para-o-se
https://whapi.cloud/blog/pt/how-to-calculate-whatsapp-api-bill-after-october-1-2026-with-examples
https://www.unipile.com/br/precos-da-api-do-whatsapp-business/
https://resend.com/blog/new-free-tier
https://www.usecarly.com/blog/amazon-ses-pricing/
https://ajuda.tabletcloud.com.br/integracoes/integracao-com-ifood-o-que-preciso-saber
https://dev.nuvemshop.com.br/en/docs/developer-tools/erp-guide/overview
https://github.com/tiendanube/api-docs/blob/master/resources/webhook.md
https://suporte.dominioatendimento.com/central/faces/solucao.html?codigo=8931
https://ajuda.omie.com.br/pt-BR/articles/9009287-importando-os-lancamentos-contabeis-no-sistema-dominio-leiaute-com-separador
https://www.jettax.com.br/blog/integracao-jettax-e-dominio-como-importar-notas-atraves-da-api/
https://www.projetoacbr.com.br/forum/files/file/430-emulador-de-balan%C3%A7as-filizola-toledo/
https://www.projetoacbr.com.br/forum/topic/48957-balan%C3%A7a-toledo-prix-3ligth/
https://brapi.dev/blog/brasil-api-vs-brapi-apis-dados-brasil
https://gs1br.org/educacao-e-eventos/Documents/Manual%20do%20Usu%C3%A1rio%20-%20API%20Verified%20by%20GS1_v.03.pdf
https://github.com/matheuscas/bluesoft-cosmos-api
https://www.pluggy.ai/en/erp
https://www.tabnews.com.br/GuilhermeVieira/estou-desenvolvendo-um-app-de-financas-pessoais-e-nao-consigo-pagar-o-open-finance-pluggy-r2-5k-mes-belvo-r6k-mes-tecnospeed-r1-5k-de-entrada-r540
docs/STATUS-V2.md:17,26,28,37,45
docs/HARDWARE.md:11
integrations/billing.py:37-45
cloud/pdvcloud/settings.py:123-129
cloud/core/views_terminal.py:384
terminal/static/terminal.js:19-23
cloud/core/nfe.py:127
web/assets/app.js:2630-2673