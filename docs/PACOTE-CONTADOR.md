# Pacote mensal do contador (XMLs das NFC-e + resumo + SPED Fiscal)

Todo mês a loja entrega ao contador os XMLs das NFC-e do mês (autorizadas, canceladas e inutilizadas) e,
no Regime Normal (CRT 3 — Lucro Presumido ou Real), o arquivo do SPED Fiscal (EFD ICMS/IPI). O PDV2
monta esse pacote a partir dos XMLs autorizados que já ficam arquivados na nuvem.

- **Painel:** Fiscal > **Pacote do contador** (papéis owner, admin e finance; editar os dados do contador:
  owner e admin).
- **Automático:** no dia 1º o pacote do mês anterior é gerado e o contador recebe um e-mail com link.
- **Manual:** escolher o mês, gerar, baixar o ZIP/SPED ou reenviar o link.

## Quando usar

- Fechamento do mês: conferir as pendências do resumo (contingência, rejeitadas, lacunas) **antes** de mandar.
- O contador pediu de novo: "Enviar ao contador" gera um link novo (cada envio tem o seu).
- Corrigiu uma pendência (transmitiu a contingência, inutilizou a faixa): gere o pacote outra vez.

## Conteúdo do ZIP

```
pacote-contador-<CNPJ>-<AAAA>-<MM>/
  autorizadas/<chave>-nfce.xml            nfeProc (NFe + protNFe) de cada NFC-e autorizada
  canceladas/<chave>-nfce.xml             XML da nota cancelada
  canceladas/<chave>-cancelamento.xml     procEventoNFe do cancelamento (110111)
  inutilizadas/<CNPJ>-serie<S>-<ini>-<fim>-inutilizacao.xml   procInutNFe homologado (cStat 102)
  sped/SPED-EFD-ICMS-IPI-<CNPJ>-<AAAAMM>.txt                   só CRT 3 (ou se pedido no Simples)
  resumo.csv     ';' e vírgula decimal (abre no Excel), UTF-8 com BOM
  resumo.html    o mesmo resumo, pronto para imprimir
  LEIA-ME.txt    o que tem no pacote, como foi calculado, pendências e avisos do SPED
```

O resumo traz: quantidade e valor por situação; por dia; por forma de pagamento (`tPag`, com troco e
líquido); por CFOP × CST/CSOSN × alíquota (valor da operação, base e valor do ICMS, ST, redução de base);
totais de produtos, descontos, frete, seguro, outras, vNF, BC/ICMS, FCP, BC/ST, ST, IPI, PIS, COFINS e
tributos aproximados; inutilizações; e a **conferência**:

- em cada nota, `ICMSTot` = soma dos itens (vProd, vDesc, vBC, vICMS, vST, vPIS, vCOFINS...) e vNF recalculado;
- soma analítica (CFOP × CST × alíquota) = soma dos vNF do mês;
- pagamentos − troco = soma dos vNF.

**Pendências** listadas no resumo, no LEIA-ME e na tela:
notas em contingência não transmitidas; notas aguardando retorno; rejeitadas sem correção (nem a venda
saiu depois, nem o número foi reaproveitado); **lacunas de numeração** por série (faixas sem nota e sem
inutilização, inclusive desde o último número do mês anterior quando o modelo tiver `number/series`);
notas sem XML arquivado; notas com total diferente da soma dos itens.

A data que vale é a do `dhEmi` **no fuso do próprio XML** (o do emitente): 23:30 de 31/10 em São Paulo é
outubro, mesmo já sendo 01/11 em UTC. Nota sem XML usa `issued_at`/`created_at` no fuso `TIME_ZONE`.
Inutilização entra no mês do recibo (`dhRecbto`).

## Envio e links

- `Tenant.accountant_email` (**e-mail do contador**, campo criado em `0046_pacote_contador`; a tela Nota fiscal grava no mesmo campo;
  se a frente de emissão criar um campo com o mesmo propósito, unificar no merge mantendo este nome).
- Demais dados do contador e opções: `AccountantSettings` (nome, CPF, CRC, CNPJ do escritório, endereço,
  telefone, município IBGE, envio automático, perfil do SPED, SPED no Simples, E116).
- E-mail **sem anexo**, com link `/api/v1/accountant/download/<token>`: token aleatório de 256 bits,
  guardado só como SHA-256, **válido 30 dias**, um por envio, contagem de downloads e auditoria.
- O ZIP fica guardado 35 dias (ou até o último link vencer) e depois é apagado pelo worker; os XMLs
  continuam no banco: basta gerar de novo.
- Cada envio (`AccountantDelivery`) registra destinatário, situação (`sent`/`failed`), erro e validade
  do link; tudo aparece na tela. Falha de SMTP deixa o envio como "Falha no envio" e a tarefa em
  revisão; "Enviar ao contador" tenta de novo.
- Agendamento: o worker, de hora em hora, chama `core.accountant.jobs.housekeeping()`; do dia 1º ao dia 5
  (para tolerar worker parado) enfileira `accountant.monthly` por empresa ativa com e-mail do contador,
  envio automático ligado e alguma NFC-e. A chave `accountant.monthly:<empresa>:<AAAA-MM>` garante um só por mês.

### API

| Método | Rota | Papéis |
| --- | --- | --- |
| GET/PATCH | `/api/v1/accountant/settings` | GET owner/admin/finance; PATCH owner/admin |
| GET/POST | `/api/v1/accountant/packages` `{year, month, include_sped?, force_sped?, send?}` | owner/admin/finance |
| GET | `/api/v1/accountant/packages/<id>` (detalhe com pendências) | idem |
| GET | `/api/v1/accountant/packages/<id>/download` (ZIP) e `/sped` (TXT) | idem |
| POST | `/api/v1/accountant/packages/<id>/send` | idem |
| GET | `/api/v1/accountant/download/<token>` (link do e-mail, sem login) | público com token |

Pacote de outra empresa responde 404; `X-Tenant-ID` de empresa alheia responde 403. Mês futuro: 400.
SPED pedido no Simples sem confirmar: 409 `sped_simples_confirm`.

## SPED Fiscal (EFD ICMS/IPI)

**Versão:** leiaute com código **020** no registro 0000 (Ato COTEPE/ICMS 79/2025, Nota Técnica 2025.001),
vigente para períodos de 01/01/2026 a 31/12/2026; regras conforme o **Guia Prático da EFD ICMS/IPI 3.2.x**
(3.1.9 foi publicado com o leiaute 020; 3.2.2 em 25/03/2026; 3.2.3 em 06/05/2026). Períodos 2020–2025 usam
014–019. Para 2027+ o gerador usa 020 e **avisa** que o leiaute não foi confirmado (já há notícia de um
leiaute 021): atualize `shared/fiscal/efd.py::LAYOUTS` quando sair o Ato COTEPE.

Arquivo: texto ISO-8859-1 (Latin-1), uma linha por registro `|REG|...|`, CRLF, datas `DDMMAAAA`,
valores com vírgula decimal e sem separador de milhar; `|` e quebras de linha são retirados dos textos.

Registros gerados:

| Bloco | Registros |
| --- | --- |
| 0 | 0000 (COD_FIN 0, perfil A/B/C configurável, IND_ATIV 1 = outros), 0001, 0005, 0100 (contador), 0190 (unidades), 0200 (itens vendidos, TIPO_ITEM 00), 0990 |
| B | B001 (sem dados — só DF escritura ISS) e B990 |
| C | C001, **C100 por NFC-e + C190**, C990 |
| D, G, H, K | só abertura (IND_MOV 1) e encerramento |
| E | E001, E100, E110 (apuração a partir dos C190), E116 quando há ICMS a recolher, E990 |
| 1 | 1001, 1010 (todos "N"), 1990 |
| 9 | 9001, 9900 (um por registro, incluindo 9900/9990/9999), 9990, 9999 |

Regras do Guia aplicadas à NFC-e (modelo 65):

- **Só C100 + C190, sem C170**. No C100 da NFC-e ficam vazios COD_PART, VL_BC_ICMS_ST, VL_ICMS_ST,
  VL_IPI, VL_PIS, VL_COFINS, VL_PIS_ST e VL_COFINS_ST. Nunca há 0150 (NFC-e não informa participante).
- C100 autorizada: IND_OPER 1, IND_EMIT 0, COD_SIT 00, SER, NUM_DOC, CHV_NFE, DT_DOC = DT_E_S (data do dhEmi
  ou dhSaiEnt), VL_DOC = vNF, IND_PGTO (0 à vista; 1 se tPag 05/15; 2 se só "sem pagamento"), VL_DESC,
  VL_ABAT_NT = vICMSDeson, VL_MERC = vProd, IND_FRT = modFrete, frete/seguro/outras, VL_BC_ICMS e VL_ICMS
  = soma dos C190.
- **C190** por CST_ICMS × CFOP × ALIQ_ICMS: CST = origem + CST (3 dígitos) ou o CSOSN no Simples;
  `VL_OPR = vProd − vDesc + frete + seguro + outras + ST + FCP-ST + IPI`; VL_RED_BC = base cheia − base
  reduzida (CST 20/70). **Soma dos VL_OPR = VL_DOC** de cada nota.
- **Cancelada:** C100 com COD_SIT **02** e só REG, IND_OPER, IND_EMIT, COD_MOD, COD_SIT, SER, NUM_DOC e
  CHV_NFE; sem C190.
- **Inutilizada (COD_SIT 05):** escriturada (um C100 por número, sem chave) **só para períodos até 12/2022**.
  O Guia dispensou a informação de denegadas/inutilizadas desde 12/2021 e os códigos 04 e 05 saíram da
  tabela 4.1.2 em 01/2023 — informar 05 em 2026 seria rejeitado pelo PVA. As inutilizações seguem no ZIP.
- Contingência não transmitida, pendentes, rejeitadas e denegadas **não entram** no SPED (aparecem como
  pendência no resumo).
- **E110:** VL_TOT_DEBITOS = soma do VL_ICMS dos C190 com CFOP 5/6/7; créditos, ajustes e saldo credor
  anterior começam em zero (o contador completa); VL_SLD_APURADO/VL_ICMS_RECOLHER/
  VL_SLD_CREDOR_TRANSPORTAR pela fórmula do Guia. **E116** (COD_OR 000, COD_REC e dia de vencimento
  configuráveis, MES_REF) quando há ICMS a recolher; sem configuração, o arquivo sai com aviso.

Conferências reproduzidas do PVA (`efd.validate`, também rodadas na geração e mostradas na tela):
pipes nas pontas; número de campos por registro; ordem dos blocos; encerramentos de bloco (x990);
9900 = contagem real de cada registro; 9999 = total de linhas; formatos de data e valor; DT_DOC dentro do
período; chave com DV válido e coerente com CNPJ/modelo/série/número; C100 × C190 (VL_DOC, VL_BC_ICMS,
VL_ICMS); C190 sem combinação repetida; NFC-e sem COD_PART/ST/IPI/PIS/COFINS no C100; COD_SIT 02 sem filhos;
E110 = soma dos C190 de saída e fórmula do saldo; soma dos E116 = VL_ICMS_RECOLHER + DEB_ESP.

**Simples Nacional (CRT 1, 2 e 4):** em regra dispensado da EFD ICMS/IPI; a tela diz isso e só gera se o
usuário confirmar ("gerar mesmo assim") ou se a empresa marcar "SPED no Simples" (exigência da UF), com aviso.

### Limites (o que o contador precisa completar/validar)

- O arquivo tem **só as saídas por NFC-e**. Faltam entradas (NF-e de compra, C100 de entrada/C170), NF-e
  modelo 55 de saída, CT-e/energia/comunicação (bloco D/C500), créditos e ajustes da apuração (E111/E113/E115),
  ST e DIFAL por UF (E200/E300), inventário (**bloco H é obrigatório no arquivo de fevereiro** — avisamos),
  produção (bloco K) e registros do bloco 1 exigidos pela UF.
- 0200 lista os itens vendidos no mês (COD_ITEM = cProd do XML, TIPO_ITEM 00 revenda, COD_GEN = 2 primeiros
  dígitos do NCM). IND_ATIV fixo em 1 (não industrial).
- Perfil B/C apenas muda o 0000 (NFC-e é sempre C100/C190).
- 1010 com 13 indicadores "N" (leiaute vigente); confira se a UF exige algum registro do bloco 1.
- **O contador deve importar e validar o arquivo no PVA da Receita Federal antes da entrega**; o PDV2 não
  transmite a EFD.

## Contrato com a frente de emissão (adaptador)

Toda leitura passa por `cloud/core/accountant/sources.py::iter_month(tenant, year, month)`. Ele lê primeiro o
XML (`operations.FiscalPayload.xml_content` — nfeProc) e usa campos do modelo só quando existem
(`getattr`): `access_key`, `number`, `series`, `model`, `issued_at`, `emission_type`, `cancelled_at`, `status`
(`pending`, `authorized`, `contingency`, `rejected`, `cancelled`). Eventos de cancelamento vêm de um modelo
`FiscalEvent` (FK `document`, `xml_content` = procEventoNFe) e inutilizações de um modelo com o procInutNFe em
`xml_content` (nomes aceitos em `EVENT_MODEL_NAMES` e `INUTILIZATION_MODEL_NAMES`). Enquanto esses modelos não
existirem, o pacote funciona com o que houver: a nota com status `cancelled` sem evento sai em canceladas/ com
aviso, e não há inutilizações. Só modelo 65 entra.

**Migração:** `core/0046_pacote_contador`, depois de `core/0045_nfce`.

## Código

| Onde | O quê |
| --- | --- |
| `shared/fiscal/nfce_xml.py` | Leitura de nfeProc/NFe/procEventoNFe/procInutNFe (sem DOCTYPE/ENTITY) |
| `shared/fiscal/month_report.py` | Resumo, conferência, lacunas, resumo.csv e resumo.html |
| `shared/fiscal/efd.py` | Gerador e validador do SPED EFD ICMS/IPI |
| `cloud/core/accountant/sources.py` | Adaptador único de leitura (contrato acima) |
| `cloud/core/accountant/package.py` | ZIP, LEIA-ME, e-mail, tokens, expiração |
| `cloud/core/accountant/jobs.py` | Tarefas `accountant.package`, `accountant.send`, `accountant.monthly` e o agendamento |
| `cloud/core/accountant/views.py` | API |
| `cloud/core/accountant/models.py` | `AccountantSettings`, `AccountantPackage`, `AccountantDelivery` |
| `web/assets/app.js` (`accountantPage`) e `core.js` | Tela Fiscal > Pacote do contador |
| Testes | `cloud/core/tests_accountant.py`, `shared/tests/test_accountant_efd.py`, `shared/tests/nfce_samples.py` (XMLs de exemplo), `web/tests/painel-core.test.mjs` |
