## NFC-e de verdade no PDV2: o que já existe e o que falta

### 1. O que a documentação atual da Focus NFe diz
- **Emissão:** `POST /v2/nfce?ref=` em homologacao.focusnfe.com.br ou api.focusnfe.com.br. A autenticação é Basic, com o token como usuário e senha vazia. A emissão é **síncrona**: a resposta já volta `autorizado` ou `erro_autorizacao`.
- **Campos obrigatórios do corpo:** `cnpj_emitente`, `data_emissao` (no máximo 5 min de diferença do horário atual), `presenca_comprador`, `modalidade_frete`, `local_destino`, `items[]` e `formas_pagamento[]` (`forma_pagamento`, `valor_pagamento`).
- **Campos obrigatórios de cada item:** `numero_item`, `codigo_ncm`, `codigo_produto`, `descricao`, `cfop`, quantidades, valores e unidades comerciais e tributáveis, `valor_bruto`, `icms_origem` e `icms_situacao_tributaria`.
- **Resposta:** `chave_nfe`, `numero`, `serie`, `caminho_xml_nota_fiscal`, `caminho_danfe`, `qrcode_url`, `url_consulta_nf`, `contingencia_offline` e `contingencia_offline_efetivada`.
- **Consulta, cancelamento e inutilização:**
  - Consulta: `GET /v2/nfce/{ref}?completa=1`.
  - Cancelamento: `DELETE /v2/nfce/{ref}` com `justificativa` de 15 a 255 caracteres. A doc fala em até 30 min depois da emissão; o prazo varia por UF e é preciso confirmar.
  - Inutilização: `POST /v2/nfce/inutilizacao` com `cnpj`, `serie`, `numero_inicial`, `numero_final` e `justificativa`.
- **Contingência:** `forma_emissao=offline` exige `numero`, `serie` e `codigo_unico`. A Focus transmite depois e cancela ou inutiliza a tentativa original. Ela recomenda deixar a numeração por conta dela.
- **Gatilhos (webhooks):** `POST /v2/hooks` com `event` (`nfce`, `nfce_contingencia` ou `nfce_consulta_automatica`), `url`, `authorization` e `authorization_header`.
- **Cadastro da empresa:** `POST /v2/empresas` com `arquivo_certificado_base64`, `senha_certificado`, `csc_nfce_producao`/`id_token_nfce_producao` (e os de homologação), `habilita_nfce`, `habilita_nfe`, `regime_tributario` e `serie_nfce_*`. A resposta devolve `token_producao` e `token_homologacao`.
- **Reforma Tributária (NT 2025.002):** no item entram `ibs_cbs_situacao_tributaria`, `ibs_cbs_classificacao_tributaria` (cClassTrib), `ibs_cbs_base_calculo`, `cbs_aliquota`/`cbs_valor` e `ibs_uf_*`/`ibs_mun_*`. Nos totais entram `cbs_valor_total`, `ibs_valor_total` e outros. Em 2026 as alíquotas de teste são CBS 0,9% e IBS 0,1%, sem recolhimento. A validação começou em 01/04/2026, e para o Simples o envio é facultativo em 2026. As fontes divergem sobre a versão da NT e a data de obrigatoriedade para o regime normal (há menção a 03/08/2026); conferir no portal nacional.

### 2. Como os fornecedores se comparam
- **Focus NFe:** plano Retail a R$ 59,90/mês por CNPJ, com 9.000 NFC-e e R$ 0,06 por nota extra. A API é madura, tem contingência gerenciada e gatilhos. É o fornecedor já integrado no código.
- **Nuvem Fiscal:** API moderna, com SDKs. Não confirmei preço em fonte oficial; um comparativo de concorrente diz que só há plano gratuito para teste.
- **PlugNotas (TecnoSpeed):** diz não cobrar por documento e calcula IBS/CBS automaticamente. O preço é sob consulta e o modelo é voltado a software house.
- **NFE.io:** Plano Base de R$ 220/mês com até 2.000 notas. Sai caro para mercado com muitos cupons.
- **eNotas:** o eNotas Gateway virou Nota Gateway e segue ativo. Preço não confirmado.

**Recomendação:** manter a Focus.

### 3. O que existe no código hoje
- **Conector (`integrations/fiscal.py`):** tem `submit`, `get` e `cancel` e baixa o XML validado (linhas 27–75). A produção só é liberada com `PDV_ALLOW_LIVE_FISCAL=1` (linha 24).
- **Fila (`operations/services.py`):** jobs `fiscal.submit`, `poll`, `archive` e `cancel` (linhas 163–207). Mutações interrompidas vão para `needs_review` em vez de serem repetidas (linhas 216–217).
- **Solicitação de emissão (`operations/views.py`):** `fiscal_submit` exige que **o chamador envie o payload pronto** e confere total, emitente e itens (linhas 190–221). Um corpo com `tipo_emissao` diferente de 1 é recusado (linha 195).
- **Falta no modelo:**
  - `Tenant` não tem certificado nem CSC; tem só `nfce_series` e `nfce_next_number` (`cloud/core/models.py:50-51`).
  - `FiscalDocument` não guarda chave, número, série, `qrcode_url` nem DANFE (`models.py:713-726`).
  - `Product.tax_fields` é um JSON livre (`models.py:160`) validado só pelo tamanho (`views_resources.py:117-120`).
- **Cupom sem dados:** `terminal/printers.py:130-244` já desenha o DANFE com chave, protocolo e QR quando recebe `sale["fiscal"]`, mas nenhum código no terminal ou no `views_terminal.py` preenche isso. O `_issuer_profile` omite de propósito os campos de autorização (linhas 62–79).
- **Status órfão:** `reverse_sale` testa `cancel_pending` (`sale_logic.py:199`), mas nada grava esse status.

### 4. Plano passo a passo
1. **Cadastro fiscal da empresa:**
   - Criar o modelo `FiscalProfile` com CRT, IE, CSC e ID token cifrados, série por loja ou caixa, certificado A1 (.pfx cifrado com `PDV_ENCRYPTION_KEY`) e validade.
   - Sincronizar com `POST/PUT /v2/empresas` usando um token de revenda global e guardar o `token_producao` por tenant no `IntegrationAccount`.
   - Avisar no painel 30 dias antes de o certificado vencer.
2. **Tributação do produto:**
   - Trocar o JSON livre por campos validados: NCM com 8 dígitos e tabela vigente, CEST quando houver ST, CFOP (5102, ou 5405 para ST), CSOSN (102/500) ou CST, origem, unidade tributável, GTIN e IBS/CBS (CST e cClassTrib).
   - Criar "perfis tributários" por categoria e bloquear a emissão de produto sem perfil.
   - Preencher `valor_total_tributos` (Lei 12.741/IBPT).
3. **Montagem do payload:**
   - `build_nfce_payload(sale)` no servidor, com rateio de descontos e promoções por item até o centavo e meios de pagamento mapeados (01, 03, 04, 05, 17 = Pix, 99).
   - Para cartão, enviar `tipo_integracao` e a bandeira.
   - O CPF no cupom vem de `customer_document_snapshot`.
4. **Emissão a cada venda:**
   - Ao sincronizar a venda com `tenant.fiscal_environment != non_fiscal`, enfileirar `fiscal.submit` automaticamente.
   - Persistir `chave_nfe`, `numero`, `serie`, `qrcode_url`, `url_consulta_nf`, `caminho_danfe` e o protocolo.
   - Tratar o 422 `already_processed` como consulta (GET).
   - Ligar o gatilho `nfce`, que é mais leve que fazer poll a cada 15 s.
5. **Devolver o resultado ao terminal:** criar o evento de sincronização `sale.fiscal_updated`, preencher `sale["fiscal"]` (com `qr_url` vindo de `qrcode_url`) e reimprimir.
6. **Cancelamento:**
   - Mostrar no painel o prazo da UF e gravar `cancel_pending`.
   - Fora do prazo, orientar a emitir NF-e de devolução (finalidade 4).
   - Inutilizar automaticamente os números rejeitados ou pulados.
7. **Contingência:**
   - O terminal é offline, mas a Focus só ajuda se a **internet da loja** estiver de pé. Como `data_emissao` aceita só 5 min, uma venda que sincroniza horas depois **não pode** sair como emissão normal.
   - Decisão de arquitetura: (a) o terminal assina localmente em tpEmis 9 com o A1 e o CSC (por exemplo PyNFe/ACBr), imprime o DANFE de contingência com QR e transmite em até 24h; ou (b) imprime um comprovante não fiscal e emite com a data da sincronização, o que é irregular.
   - A opção (a) é a correta, mas é grande.
8. **DANFE no cupom:** o layout já existe. Falta a mensagem de homologação ("EMITIDA EM AMBIENTE DE HOMOLOGAÇÃO – SEM VALOR FISCAL"), o texto da consulta pela chave em `url_consulta_nf` e a via de contingência.
9. **NF-e modelo 55:**
   - Adicionar `submit_nfe` e `cancel_nfe` (`/v2/nfe`) ao conector.
   - Devolução: `finalidade_emissao=4`, `notas_referenciadas[].chave_nfe`, CFOP 1202/1411 (entrada) ou 5202 e destinatário completo.
   - Transferência entre lojas: CFOP 5152/6152, ligada ao fluxo de transferência existente.
   - Criar `FiscalDocument.kind='nfe'` com série própria.
10. **Homologação:** usar a série 900+ em homologação, testar rejeições comuns (NCM inválido, CSC, data, totais) e gerar o relatório de XML mensal para o contador.

### 5. Variáveis de ambiente
| Variável | Situação |
|---|---|
| `PDV_ALLOW_LIVE_FISCAL` | Existe |
| `PDV_ENCRYPTION_KEY` / `PDV_ENCRYPTION_KEY_FILE` | Existem |
| `FOCUS_RESELLER_TOKEN` | Proposta |
| `FOCUS_WEBHOOK_SECRET` | Proposta |
| `FISCAL_DEFAULT_ENVIRONMENT` | Proposta |
| `FISCAL_CANCEL_WINDOW_MIN` (por UF) | Proposta |

### 6. Armadilhas
- **Numeração em dois lugares:** `nfce_next_number` local e a numeração automática da Focus conflitam; escolher uma só (a Focus).
- **Arredondamento:** a soma dos itens com o rateio de desconto precisa bater exatamente com o total.
- **Status sem mapeamento:** `normalized_status` não conhece `erro_cancelamento` e cai em `pending`.
- **Caminho do XML:** `xml_url` é `URLField`, mas a Focus devolve caminho relativo.
- **NFC-e para CNPJ:** a regra que proibia NFC-e para destinatário CNPJ teve a revogação publicada em 04/2026; confirmar o ajuste SINIEF vigente.
- **Certificado e CSC:** o certificado vence todo ano, e o CSC tem um por ambiente.
- **IBS/CBS:** errar o cClassTrib causa rejeição no regime normal.
- **Dado sensível:** nunca registrar no log a senha do .pfx.

## Itens
- [high/M] **Perfil fiscal da empresa e cadastro na Focus**: Modelo com certificado A1 cifrado, senha, CSC e ID token por ambiente, CRT, IE e série por loja. Sincronizar com POST/PUT /v2/empresas e guardar o token_producao por tenant. Hoje o Tenant só tem nfce_series e nfce_next_number (cloud/core/models.py:50-51).
- [high/M] **Tributação estruturada do produto**: Trocar o tax_fields livre (models.py:160; views_resources.py:117-120) por NCM, CEST, CFOP, CSOSN/CST, origem, GTIN e IBS/CBS (CST e cClassTrib), com perfis por categoria e bloqueio de emissão sem perfil.
- [high/M] **Montar o payload NFC-e no servidor**: Criar build_nfce_payload(sale) com rateio de desconto e promoção por item, meios de pagamento (17 = Pix) e valor_total_tributos. Hoje fiscal_submit exige o payload pronto vindo do cliente (operations/views.py:190-221).
- [high/M] **Emissão automática a cada venda sincronizada**: Enfileirar fiscal.submit quando fiscal_environment não for non_fiscal. Persistir chave, número, série, qrcode_url, url_consulta_nf e DANFE no FiscalDocument (models.py:713-726). Tratar o 422 already_processed como consulta (GET).
- [high/P] **Devolver o resultado fiscal ao terminal e imprimir o DANFE**: printers.py:219-244 e terminal.js:103-108 já desenham chave e QR, mas nada preenche sale['fiscal']. Criar um evento de sincronização com os dados da autorização, a mensagem de homologação e a reimpressão.
- [high/G] **Contingência offline real (tpEmis 9) no terminal**: A Focus exige data_emissao com no máximo 5 min de diferença e só ajuda se a internet da loja estiver de pé. O caixa offline precisa assinar localmente com o A1 e o CSC, imprimir o DANFE de contingência e transmitir em até 24h. Hoje views.py:195 recusa a contingência.
- [medium/P] **Gatilhos (webhooks) da Focus**: POST /v2/hooks com os eventos nfce e nfce_contingencia, um cabeçalho authorization secreto (FOCUS_WEBHOOK_SECRET) e um endpoint que só enfileira fiscal.poll.
- [medium/P] **Cancelamento com prazo por UF e inutilização**: Gravar cancel_pending, que é testado em sale_logic.py:199 mas nunca gravado, e mapear erro_cancelamento em normalized_status. Inutilizar números rejeitados ou pulados via POST /v2/nfce/inutilizacao. Fora do prazo, encaminhar para NF-e de devolução.
- [medium/M] **NF-e modelo 55 para devolução e transferência**: Adicionar /v2/nfe ao conector, com finalidade_emissao=4 e notas_referenciadas para devolução e CFOP 5152/6152 para transferência entre lojas, com série própria e kind='nfe'.
- [medium/M] **Reforma Tributária IBS/CBS**: Campos ibs_cbs_situacao_tributaria, ibs_cbs_classificacao_tributaria, cbs_aliquota e ibs_uf_aliquota, mais os totais. Facultativo para o Simples em 2026, com validação ativa desde 04/2026. Testar em homologação.
- [medium/P] **Numeração única**: Escolher a numeração automática da Focus e aposentar nfce_next_number, para evitar duplicidade e lacunas.

Fontes:
https://doc.focusnfe.com.br/reference/emitir_nfce
https://doc.focusnfe.com.br/reference/cancelar_nfce
https://doc.focusnfe.com.br/reference/inutilizar_numeracao_nfce.md
https://doc.focusnfe.com.br/docs/nfce-mecanismo-de-contingencia-offline.md
https://doc.focusnfe.com.br/docs/webhooks.md
https://doc.focusnfe.com.br/reference/criar_empresa.md
https://focusnfe.com.br/guides/reforma-tributaria/
https://focusnfe.com.br/precos/
https://nfe.io/precos/emissao-nfce
https://tecnospeed.com.br/en/plugdfe/plugnotas/
https://notagateway.com.br/
https://www.notaas.com.br/blog/post/api-emissao-nota-fiscal-comparativo-plataformas-2026/
https://qive.com.br/blog/nfe-ibs-cbs-reforma-tributaria
https://blog.econeteditora.com.br/2026/04/14/
integrations/fiscal.py:24-80
operations/services.py:148-228
operations/views.py:190-257
cloud/core/models.py:49-51,160,713-726
cloud/core/views_terminal.py:62-79
cloud/core/sale_logic.py:199
terminal/printers.py:130-248
terminal/static/terminal.js:103-108