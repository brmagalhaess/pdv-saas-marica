# NFC-e (modelo 65, leiaute 4.00)

Atualizado em 18/11/2026. Emissão real de NFC-e integrada às vendas, em dois modos configuráveis por empresa:

- **SEFAZ direta**: a nuvem monta, assina (certificado A1 da empresa) e transmite direto ao autorizador da UF.
- **Focus NFe**: a nuvem monta o JSON do provedor a partir do motor tributário e usa a API v2 da Focus NFe.

O cálculo de impostos é o do motor (`docs/FISCAL.md`, `shared.fiscal.compute_document`); um documento com
`ready = false` é recusado antes de gastar número.

## 1. Código

| Onde | O quê |
| --- | --- |
| `shared/nfce/xml.py` | Árvore XML própria serializada já em forma canônica (C14N 1.0), parser que recusa DTD/ENTITY |
| `shared/nfce/keys.py` | Chave de acesso de 44 posições, DV módulo 11 (aceita CNPJ alfanumérico), cNF aleatório |
| `shared/nfce/builder.py` | XML da NFC-e: ide, emit, dest, det/prod/imposto, total, transp, pag, infAdic, infRespTec |
| `shared/nfce/qrcode.py` | QR Code versão 2 (CSC) e versão 3 (NT 2025.001) |
| `shared/nfce/signature.py` | XMLDSig enveloped RSA-SHA1/C14N com `cryptography`; verificação |
| `shared/nfce/messages.py` / `responses.py` | Pedidos (consulta, status, evento 110111, inutilização) e leitura dos retornos; nfeProc, procEventoNFe, procInutNFe |
| `shared/nfce/webservices.py` | URLs por UF/ambiente, QR, urlChave, fuso por UF, prazo de cancelamento |
| `shared/nfce/payments.py` | tPag por forma de pagamento e regra de impressão |
| `shared/nfce/testing.py` | Só para testes: certificado autoassinado e SEFAZ simulada |
| `cloud/core/nfce/backends.py` | **Contrato único** e os dois backends (`SefazBackend`, `FocusBackend`) |
| `cloud/core/nfce/transport.py` | SOAP 1.2 com TLS de cliente (A1) |
| `cloud/core/nfce/certificate.py` | Leitura do .pfx: titular, CNPJ (OID 2.16.76.1.3.3), validade |
| `cloud/core/nfce/service.py` | Numeração, emissão, contingência, cancelamento, inutilização, reservas do caixa |
| `cloud/core/views_nfce.py` | Rotas do caixa |
| `operations/views_fiscal.py` | Rotas do painel |
| `operations/services.py` | Jobs `fiscal.*` |
| `terminal/nfce.py`, `terminal/sync.py`, `terminal/store.py` | NFC-e no caixa, contingência off-line, envio posterior |
| `terminal/static/terminal.js`, `terminal-core.js` | Diálogo "Imprimir a nota fiscal?" |
| `web/assets/app.js`, `core.js` | Painel: Minha empresa > Nota fiscal, formas de pagamento, vendas |

Sem dependência nova: XML, C14N e assinatura usam só a biblioteca padrão e `cryptography` (já fixada).

## 2. Configuração passo a passo (lojista)

Tudo em **Minha empresa** (só proprietário ou administrador).

1. Complete os **Dados da empresa** (CNPJ, IE, endereço, código IBGE, CRT). O aviso verde "Dados do emitente completos" precisa aparecer.
2. Complete o cadastro fiscal dos produtos (aba *Fiscal*; selo "Pronto para nota").
3. Em **Nota fiscal (NFC-e)**:
   - **Ambiente: Homologação** (sempre primeiro). Notas saem com chave e QR, mas sem valor fiscal.
   - **Modo de emissão**: *SEFAZ direta* (envie o certificado) ou *Focus NFe* (cole o token).
   - **Série** e **próximo número**: altere só ao migrar de outro emissor. O sistema recusa um número já usado ("Nunca reutilize").
   - **Versão do QR Code**: 3 (padrão). Use 2 apenas se a sua UF ainda exigir.
   - **CSC e ID do CSC** de homologação (e depois de produção), gerados no portal da SEFAZ da UF. Obrigatórios para a contingência no caixa e para o QR versão 2.
   - **Contingência no caixa** ligada (recomendado; exige CSC).
   - **E-mail do contador** (usado pelo pacote mensal).
4. **Certificado digital A1** (modo SEFAZ direta): envie o `.pfx` e a senha. O painel mostra titular, CNPJ e validade; recusa certificado de outro CNPJ ou vencido e avisa 30 dias antes de vencer.
5. **Testar comunicação**: consulta o status do serviço na SEFAZ (cStat 107) ou testa o token na Focus NFe.
6. Em **Formas de pagamento**, escolha a impressão da nota por forma.
7. Faça vendas de teste no caixa: autorização, impressão, "Perguntar", cancelamento (F9 para reimprimir), contingência (tire o cabo de rede).
8. Para produção: escolha *Produção* e digite **EMITIR EM PRODUCAO** no campo de confirmação. A plataforma também precisa estar com `PDV_ALLOW_LIVE_FISCAL=1`; sem ela, produção é recusada (homologação é sempre liberada).

**Quando a NFC-e liga no caixa** (`service.readiness`): ambiente homologação/produção **e** produção liberada pela plataforma **e** configuração salva com a credencial do modo (certificado A1 do CNPJ da empresa, ou token da Focus ativo) **e** dados fiscais da empresa completos. Só abrir a tela (GET da configuração) não cria registro nem liga nada. A emissão (`create_document`, caixa e "Emitir" do painel) confere tudo isso **antes** de gastar um número.

Segredos (CSC, token, certificado e senha) são cifrados com `PDV_ENCRYPTION_KEY` (Fernet) e nunca voltam ao navegador nem aparecem em logs ou na auditoria (a auditoria registra só *quais* segredos mudaram).

## 3. Montagem do XML

- `ide`: cUF, cNF (8 dígitos aleatórios, nunca igual ao nNF nem sequência), natOp `VENDA`, mod 65, série, nNF, `dhEmi` no fuso da UF (AC −05, AM/MT/MS/RO/RR −04, demais −03), tpNF 1, idDest 1, cMunFG, tpImp 4, tpEmis (1 ou 9), cDV, tpAmb, finNFe 1, indFinal 1, indPres 1 (4 aceito; indIntermed=0 quando indPres ≠ 1), procEmi 0, verProc; em contingência `dhCont` e `xJust`.
- `emit` com endereço completo, IE e CRT; `dest` opcional com CPF/CNPJ informado na venda (F4), `indIEDest` 9.
- `det`: `prod` e `imposto` vindos do motor (ICMS00/10/20/30/40/51/60/70/90, ICMSSN101/102/201/202/500/900, PIS/COFINS Aliq/NT/Outr, `vTotTrib`). Para venda do caixa o desconto do item é `promo_discount_cents + discount_cents`.
- `total/ICMSTot` com a soma do motor; `transp/modFrete` 9.
- `pag/detPag` com tPag: 01 dinheiro, 03 crédito, 04 débito, 05 crédito loja / a prazo, 17 Pix, 99 outros (com `xPag`); cartões levam `card/tpIntegra=2`; troco em `vTroco`.
- `infAdic/infCpl` com a Lei 12.741 (federal/estadual/municipal quando a fonte é IBPT); `infRespTec` quando a plataforma configura CNPJ, contato, e-mail e telefone (`PDV_FISCAL_TECH_*`).
- Homologação: o primeiro `xProd` (e `dest/xNome`) é o texto obrigatório "NOTA FISCAL EMITIDA EM AMBIENTE DE HOMOLOGACAO - SEM VALOR FISCAL".
- **Validação**: os XSD oficiais (PL_009) **não** foram incluídos (não baixados nesta entrega e o pacote é grande). A estrutura é coberta por testes (ordem das tags dos grupos principais, valores, C14N comparada com `xml.etree.ElementTree.canonicalize`, assinatura verificada). Validar contra o XSD e na SEFAZ de homologação faz parte da lista do item 10.
- **Fora desta entrega**: grupo IBS/CBS (Reforma) no XML (os valores do motor são só informativos), ICMS desonerado, combustíveis.

## 4. Assinatura, transporte e web services (SEFAZ direta)

- XMLDSig: C14N 1.0 sem comentários, transforms enveloped + C14N, SHA-1/RSA-SHA1, `Reference URI="#NFe…"`, X509Certificate. Também para `infEvento` e `infInut`.
- SOAP 1.2, `nfeDadosMsg`, autorização **síncrona** (`indSinc=1`). Resposta 103/105 vira "pendente" e é consultada (`NFeConsultaProtocolo4`). 204/539 (duplicidade) consulta e recupera o protocolo.
- TLS de cliente com o A1: a chave fica só em memória (o PEM vai para diretório temporário 0700 e é apagado após a carga). O certificado da SEFAZ é sempre verificado; como muitas usam cadeia ICP-Brasil, configure `PDV_SEFAZ_CA_BUNDLE` com certifi + raízes ICP-Brasil. `PDV_NFCE_WS_OVERRIDES` corrige uma URL sem nova versão.
- URLs **conferidas em 18/11/2026** contra as tabelas públicas do projeto NFePHP (`autorizadores.json` modelo 65, `wsnfe_4.00_mod65.xml`, `uri_consulta_nfce.json`): autorizadores próprios **AM, GO, MG, MS, MT, PR, RS, SP**; demais UFs (inclusive **BA, CE, PE, RJ, ES, DF, SC**) usam a **SVRS**. QR e urlChave das 27 UFs na mesma fonte. Não houve chamada real a nenhum web service.

## 5. QR Code: versão adotada

- **Versão 3 (NT 2025.001)** é o padrão: emissão normal `chave|3|tpAmb`, sem CSC; contingência `chave|3|tpAmb|dia|vNF|tpIdDest|idDest|assinatura` com assinatura RSA-SHA1 do certificado em Base64. A NT dispensa o CSC e proíbe a v2 para emissor pessoa física (rejeição 444); para CNPJ a v2 continua aceita até onde foi conferido.
- **Versão 2 (NT 2015.002)** continua disponível (`qr_version=2`) e é usada **sempre** na contingência feita no caixa (hash SHA-1 com CSC; a v3 off-line exigiria a chave privada no caixa).
- Conferir em homologação de cada UF: aceitação da v3 online e da v2 off-line na mesma empresa.

## 6. Fluxo com o caixa (offline-first)

1. A venda é gravada no caixa. Com a NFC-e ligada (bootstrap `nfce.enabled`), o comprovante **não** é impresso na hora.
2. A tela chama `POST /api/sales/<id>/nfce` (caixa) → o caixa envia a fila de eventos e chama `POST /api/v1/terminal/nfce {sale_id}` (nuvem, idempotente pela venda), esperando até ~8 s.
3. Respostas:
   - **Autorizada** → DANFE NFC-e com chave, protocolo e QR.
   - **SEFAZ fora (nada enviado)** → a nuvem emite em **contingência off-line** com o mesmo número (tpEmis 9, QR off-line), assina e transmite depois (`fiscal.transmit`).
   - **Resposta perdida (tempo esgotado depois de enviar)** → a nuvem consulta; sem confirmação emite a contingência com **outro número** e marca a primeira tentativa como substituída. O job `fiscal.reconcile_replaced` consulta a primeira chave: se foi autorizada, **cancela**; se não consta (217), **inutiliza** o número.
   - **Rejeitada** → motivo traduzido; nada é impresso como nota; o lojista corrige o cadastro e usa **Reenviar** no painel (mesmo número).
   - **Pendente** (Focus processando) → o caixa e o worker perguntam de novo.
4. **Caixa sem internet**: com a contingência ligada, o caixa usa a faixa de números reservada (`POST /api/v1/terminal/nfce/reserve {used}`, ver 5a) e o CSC recebido nela, monta a NFC-e tpEmis 9 com `shared/nfce` e QR v2 off-line, imprime o DANFE de contingência (duas vias, "EMITIDA EM CONTINGÊNCIA / Pendente de autorização") e guarda o XML **sem assinatura**. Quando volta a falar com a nuvem, o worker envia (`POST /api/v1/terminal/nfce/contingency`); a nuvem confere emitente, modelo, tpEmis, chave × número/série, ambiente, faixa reservada do caixa e total da venda, **assina exatamente o XML impresso** (mesmo DigestValue do QR) e transmite. Se a venda já tinha uma tentativa na nuvem, ela é substituída e conciliada como acima.
5. Sem contingência possível (modo Focus, sem CSC, sem faixa ou faixa vencida), a venda fica **NFC-e pendente** e o worker pede a nota assim que houver conexão.
5a. **Faixas de contingência** (`reserve_numbers`): blocos de 10 números por caixa, válidos por 20 dias. O caixa manda em `used` os números que já usou off-line e ainda não enviou; a nuvem só abre um bloco novo quando sobram menos de 3 livres (contando esses) ou quando os blocos abertos têm menos de 3 dias de validade, e só então grava a auditoria `fiscal.numbers_reserved`. A resposta traz os números livres de todos os blocos ainda válidos, `ranges` e `expires_at` (o do bloco mais antigo). O caixa pede de novo no máximo a cada 2 min, renova quando faltam menos de 3 dias e **não usa** a faixa no último dia antes de `expires_at` (margem para a nota chegar à nuvem). Um bloco perto de vencer não é mais entregue, mas continua aceitando o envio de números já usados até a varredura liberá-lo.
5b. **Faixa liberada**: a nuvem recusa nota de contingência com número de bloco liberado ou vencido (`409 reservation_released`): esses números já foram (ou serão) inutilizados. O caixa então marca a venda como **pendente** e pede uma NFC-e normal (número novo). O DANFE de contingência já impresso perde valor; o cupom autorizado sai pela regra de impressão.
5c. **Ordem na nuvem** (`register_terminal_contingency`): o XML do caixa é assinado **antes** de gravar qualquer coisa; a nota, o XML assinado e o job `fiscal.transmit` são gravados na **mesma transação**. Um reenvio da mesma chave (resposta perdida) refaz a assinatura e o job se faltarem. XML sem `infNFeSupl/qrCode` ou `urlChave` é recusado (422).
5d. **Venda com a NFC-e desligada na nuvem**: o caixa grava o estado final `disabled` e **nunca** pede nota retroativa, nem quando a NFC-e é religada. Janela de emissão: só vendas feitas com a NFC-e ligada (o caixa cria o registro `pending` na hora da venda só quando o bootstrap diz `enabled`).
6. **24 horas**: o caixa mostra o chip "NFC-e: N aguardando envio" e, passadas 20 h, "atrasada(s)" em vermelho; o painel mostra o mesmo alerta em Minha empresa e Integrações.
7. **Numeração sem buracos**: números rejeitados são reutilizados no reenvio; tentativas substituídas e números reservados e não usados (reserva liberada após 20 dias, troca de série/ambiente) são **inutilizados automaticamente**; o painel tem "Inutilizar números" para lacunas antigas. A varredura (`sweep`, no worker) trata cada bloco isolado: `released_at` só é gravado na mesma transação que registra e enfileira as inutilizações; uma falha deixa o bloco aberto para a próxima rodada. Se a empresa não emite mais no ambiente do bloco (desligou a NFC-e ou trocou de ambiente), o bloco é fechado **sem** inutilizar (não há como transmitir): os números ficam como lacuna no resumo do contador e devem ser inutilizados pelo painel quando a emissão voltar.
8. **Transporte (SEFAZ direta)**: só conta como "não chegou" (mesmo número em contingência) a falha antes de enviar: DNS, conexão recusada, tempo de conexão, handshake TLS. Tempo de leitura, conexão derrubada depois de aberta (`RemoteDisconnected`/`ProtocolError`), resposta truncada (`ChunkedEncodingError`) e qualquer outro erro são **resultado desconhecido**: com o cliente esperando, a contingência sai com **número novo** e a primeira tentativa é conciliada (cancelada se autorizada, inutilizada se não consta).

### Decisão sobre a contingência e riscos

- **O certificado A1 nunca vai para o caixa.** O caixa só recebe CSC + faixa de números. O DANFE de contingência é impresso no caixa com o QR v2 (CSC + DigestValue, que é um hash e não exige a chave privada) e a assinatura é feita na nuvem, sobre o mesmo XML.
- **Risco 1 (legal)**: a legislação descreve a NFC-e de contingência como assinada no momento da emissão; aqui a assinatura é aplicada na transmissão, sobre o conteúdo idêntico (o DigestValue do QR prova que nada mudou). Validar com o contador/SEFAZ da UF; a alternativa (assinar no caixa) exigiria entregar a chave privada ao computador da loja.
- **Risco 2**: o CSC fica no SQLite do caixa (arquivo 0600, tabela própria, apagada ao desparear). Quem tiver o arquivo pode gerar QR codes válidos da empresa; o CSC pode ser revogado no portal da SEFAZ. O CSC **não vai** para cópias do banco: o backup local (`Store.backup_now`) e o segundo disco/réplica (`mirror._local_copy`) removem o campo `csc` (`nfce.strip_secrets`); números e lista de usados permanecem, e a cópia restaurada pede o CSC à nuvem antes de emitir em contingência.
- **Risco 3**: se a UF deixar de aceitar QR v2 para CNPJ, a contingência no caixa deixa de funcionar (a da nuvem continua, com v3).
- **Modo Focus NFe**: sem internet no caixa não há contingência local (a Focus não aceita XML assinado por terceiros); com a SEFAZ fora, a própria Focus faz a contingência (`contingencia_offline`). Lojas com internet instável devem usar o modo SEFAZ direta.

## 7. Impressão por forma de pagamento

Em **Formas de pagamento**, cada forma (dinheiro, Pix, débito, crédito, a prazo, crédito na loja, formas próprias) tem "Impressão da nota fiscal": **Sempre imprimir**, **Perguntar** ou **Não imprimir (só enviar)**. Padrão: dinheiro, Pix, crédito na loja e formas próprias = Perguntar; cartões e a prazo = Sempre. A nota é emitida de qualquer forma; só a impressão muda.

- Pagamento dividido: imprime se qualquer forma for "Sempre"; pergunta se alguma for "Perguntar"; senão não imprime.
- Diálogo no caixa: "Imprimir a nota fiscal?" com botões grandes; **Enter/S = Sim**, **Esc/N = Não**. Campo opcional para abrir o link de consulta pública por WhatsApp (`wa.me`) ou e-mail (`mailto:`) — nada é enviado automaticamente.
- **F9** reimprime o DANFE quando a venda tem nota.
- As regras vão ao caixa no bootstrap (`payment_methods[].print_rule` e `nfce.print_rules`).

## 8. Cancelamento e consulta

- **Painel** (Vendas > detalhes, ou Integrações): "Cancelar NFC-e" com justificativa de 15 a 255 caracteres, até o prazo (padrão **30 minutos** da autorização; tabela por UF em `webservices.CANCEL_MINUTES`). Fora do prazo: recusa com orientação de cancelamento extemporâneo.
- **Caixa**: cancelar a venda com NFC-e exige PIN de gerente, motivo ≥ 15 caracteres e o prazo; o evento `sale.voided` faz a nuvem pedir o cancelamento (`fiscal.cancel`). Nota ainda em contingência/pendente é cancelada logo após a autorização.
- Situação por venda no painel: **Autorizada / Em contingência / Rejeitada (motivo) / Cancelada**; download do XML autorizado, do XML do cancelamento e das inutilizações.

## 9. Rotas

Painel (`/api/v1/ops/…`, sessão + `X-Tenant-ID`):

| Rota | Quem |
| --- | --- |
| `GET/PATCH fiscal/config` | owner, admin |
| `POST/DELETE fiscal/certificate` `{pfx_base64, password}` | owner, admin |
| `POST fiscal/test` | owner, admin |
| `POST fiscal/submit {sale_id}` (sem payload pronto; o antigo `fiscal_payload` é ignorado) | owner, admin, manager |
| `GET fiscal/documents[?sale_id=&status=]` | owner, admin, manager, finance, viewer |
| `GET fiscal/documents/<id>/xml`, `fiscal/events/<id>/xml`, `fiscal/inutilizations/<id>/xml` | owner, admin, manager, finance |
| `POST fiscal/documents/<id>/cancel {reason}` / `…/retry` | owner, admin, manager |
| `POST fiscal/inutilize {series?, start, end, reason}` | owner, admin |

Caixa (`/api/v1/terminal/…`, token do terminal): `POST nfce`, `GET nfce/<sale_id>`, `POST nfce/reserve`, `POST nfce/contingency`, `POST nfce/<sale_id>/cancel {reason, manager_id}`.
O bootstrap ganha `nfce = {enabled, environment, mode, contingency, wait_ms, cancel_minutes, print_rules}` (sem segredos).

## 10. Para o pacote do contador

Contrato estável para a frente do pacote mensal (ZIP de XMLs, e-mail, SPED EFD):

- `core.FiscalDocument` (uma tentativa por linha; a vigente da venda tem `replaced_by = NULL`):
  `tenant, shop, sale, terminal, provider ("sefaz" | "focusnfe"), model (65), environment ("homologation" | "production"),
  series, number, access_key (44), emission_type (1 normal, 9 contingência), issued_at, authorized_at, cancelled_at,
  status ∈ {pending, authorized, contingency, rejected, cancelled}, status_code, authorization (protocolo), error_message, replaced_by`.
- `operations.FiscalPayload.xml_content`: **nfeProc completo** (NFe assinada + protNFe) da nota autorizada, com `xml_sha256`. Também em notas canceladas (o nfeProc original continua guardado).
- `core.FiscalEvent`: `document, kind ("cancel"), event_type ("110111"), sequence, status ∈ {pending, registered, rejected}, protocol, reason, registered_at, xml_content` = **procEventoNFe** (evento assinado + retEvento), `xml_sha256`.
- `core.FiscalInutilization`: `environment, model, series, start, end, reason, status ∈ {pending, done, rejected}, protocol, done_at, xml_content` = **procInutNFe**, `xml_sha256`.
- `core.FiscalConfig.accountant_email`.
- Para o mês: notas com `environment="production"` e `status in ("authorized","cancelled")`, eventos `registered` e inutilizações `done`. Notas `contingency` ainda não autorizadas não têm nfeProc.

## 10a. Notas da versão anterior (Focus NFe, até core 0041) e migração

Em produção antes desta versão só havia emissão pela Focus NFe, com status próprios e sem número/chave nas colunas. A migração `core.0045_nfce_status` (separada de `0045_nfce`: no PostgreSQL um UPDATE na mesma transação dos índices criados dá "pending trigger events"):

- status antigos para novos: `queued`/`processing` para `pending`, `denied` para `rejected` (mensagem prefixada com "Uso denegado"), `cancel_pending` para `authorized` (o cancelamento continua pelo job `fiscal.cancel` antigo, veja abaixo); o mesmo em `Sale.fiscal_status`;
- empresas com `IntegrationAccount` focusnfe ativa e sem `FiscalConfig` ganham `FiscalConfig(mode="focusnfe")`;
- notas antigas (`environment=""`) recebem `environment` da conta Focus, `access_key` de `provider_response.chave_nfe` (sem o prefixo "NFe"), `number`/`series` de `numero`/`serie` (ou da chave) quando não colidem com outra nota, `issued_at`/`authorized_at` de `data_emissao`/`data_autorizacao`.

Comportamento com notas antigas (`service.is_legacy`: Focus sem `cnf`/`issued_at` no payload):

- consulta, envio e cancelamento usam o **provedor e o ambiente do documento** (`document_backend`/`document_environment`), não o modo e o ambiente atuais da empresa; funcionam mesmo com a empresa em "sem nota fiscal";
- `fiscal.submit`/`fiscal.poll` pendentes: consulta a Focus pela referência; se ela não conhece (404), reenvia o **payload antigo** guardado, como a versão anterior fazia;
- `fiscal.archive`: usa `access_key` ou a `chave_nfe` da resposta da Focus;
- `fiscal.cancel` antigo (`{document_id, reason}`, sem `event_id`): cria o `FiscalEvent` e cancela; sem justificativa válida vai para revisão com mensagem clara;
- "Reenviar" de nota antiga rejeitada é recusado (`fiscal_legacy_document`): a numeração era da Focus.

## 10b. Deploy e rollback

**Janela do deploy.** Toda coluna nova `NOT NULL` em tabela existente tem default **no banco** (`db_default`): `core_tenant.crt/cnae/special_tax_regime/city_ibge_code/accountant_email`, `core_product.fiscal_overrides`, `core_cashsession.paused_ms/closing_totals`, `core_paymentmethod.print_rule`, `core_fiscaldocument.model/environment/access_key/emission_type/status_code/qr_url/url_key`. Assim o código anterior (que não conhece essas colunas) continua fazendo INSERT durante a migração e depois de um rollback. As demais colunas novas aceitam nulo; tabelas novas não são tocadas pelo código antigo.

**Rollback só do código** (voltar a imagem anterior sem desfazer migrações) é o caminho recomendado: o código antigo ignora colunas e tabelas novas. Atenção:

- notas emitidas no modo SEFAZ direta ficam invisíveis para o código antigo (ele só conhece a Focus); jobs `fiscal.transmit`, `fiscal.inutilize` e `fiscal.reconcile_replaced` ficam parados (o worker antigo não tem handler) até a versão nova voltar;
- status novos (`contingency`, `rejected`) aparecem como texto no painel antigo.

**Desfazer as migrações** (voltar `core` para 0041 e `operations` para 0003) apaga colunas e tabelas novas (NFC-e direta, eventos, inutilizações, reservas, pacote do contador). As migrações de dados `0043_fiscal_backfill` e `0045_nfce_status` têm reverso **noop**: o mapeamento de status **não volta**. `denied` virou `rejected` (sobra o prefixo "Uso denegado" na mensagem), `queued`/`processing` viraram `pending`, `cancel_pending` virou `authorized` (o pedido de cancelamento sobrevive só no job `fiscal.cancel`). `crt`, perfis e `fiscal_overrides` somem; `tax_regime` e `tax_fields` antigos nunca foram alterados. Faça backup do PostgreSQL antes do deploy.

## 11. Limites e o que precisa de teste real em homologação

Tudo foi testado com transportes simulados (sem rede). Antes de produção, validar em **homologação da SEFAZ** da UF do cliente:

1. Comunicação TLS com o A1 real e a cadeia ICP-Brasil (`PDV_SEFAZ_CA_BUNDLE`); status do serviço 107.
2. Autorização síncrona (cStat 100) de vendas reais: Simples (CSOSN 102/500) e Regime Normal (CST 00/20/60), com e sem CPF, desconto, troco, cartão, Pix e forma própria (tPag 99).
3. Validação do XML gerado contra os XSD PL_009 vigentes (incluir as NTs recentes de regras de validação).
4. QR Code v3 online e v2 off-line na leitura do app/portal da UF; urlChave impressa.
5. Contingência: SEFAZ fora (simular) → tpEmis 9 da nuvem; caixa sem internet → XML do caixa assinado na nuvem e autorizado depois (aceitação do dhCont/xJust e do QR v2 em nota tpEmis 9).
6. Cancelamento 110111 dentro do prazo e o prazo real da UF; duplicidade de evento (573).
7. Inutilização de faixa e de número único; rejeição 256/563.
8. Rejeições comuns (778 NCM, 591 CRT, 213/598 certificado) e a mensagem traduzida.
9. Modo Focus NFe: campos `numero`/`serie` enviados, `contingencia_offline`, endpoint de inutilização e downloads de XML (nota, cancelamento, inutilização).
10. infRespTec obrigatório na UF (configurar `PDV_FISCAL_TECH_*` com CNPJ real da CrownTech).
11. Impressora térmica real: DANFE autorizado e de contingência (duas vias), QR legível.

Limites conhecidos: sem DANFE A4/PDF; sem envio automático por e-mail/WhatsApp (só link); grupo IBS/CBS não emitido; prazo de cancelamento por UF só com o padrão de 30 min; numeração por empresa (série única), faixas de contingência por caixa.
