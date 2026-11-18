# Cadastro fiscal e motor de impostos (NFC-e 65 / NF-e 55)

Esta frente entrega o **cadastro fiscal** (empresa, perfis tributários, produto) e um
**motor de cálculo de impostos por item**, determinístico e testado. **Não** emite nota:
a emissão (Focus NFe ou SEFAZ direta) é outra frente e deve consumir o contrato da seção 5.

Código:

| Onde | O quê |
| --- | --- |
| `shared/fiscal/validators.py` | CNPJ (numérico e alfanumérico), CPF, IE das 27 UFs, CEP, IBGE, NCM, CEST, CFOP, cBenef, CNAE, GTIN |
| `shared/fiscal/data.py` | Tabelas públicas pequenas (UF, CRT, origem, CST/CSOSN, PIS/COFINS, IPI, CFOPs da NFC-e, segmentos CEST, UFs com cBenef) |
| `shared/fiscal/schema.py` | Campos fiscais de perfil/produto, limpeza, leitura do `tax_fields` legado e resolução em camadas |
| `shared/fiscal/presets.py` | Modelos prontos de perfil |
| `shared/fiscal/readiness.py` | "O que falta" para produto e empresa, em português |
| `shared/fiscal/engine.py` | Motor: `compute_document()` / `compute_item()` |
| `shared/fiscal/ibpt.py` | Leitor da tabela IBPT (Lei 12.741) |
| `cloud/core/fiscal.py` | Ponte banco ↔ motor (`FiscalContext`, `simulate`, dados do emitente) |
| `cloud/core/views_fiscal.py` | API de perfis, referência, prontidão, simulação e importação CSV |
| `cloud/core/management/commands/load_ibpt.py` | Carga da tabela IBPT por UF |

Tudo em `shared/fiscal` é Python puro (só biblioteca padrão): nuvem, terminal e testes usam as mesmas regras.

## 1. Empresa (Tenant)

| Campo | Uso / tag NF-e | Validação |
| --- | --- | --- |
| `crt` (1–4) | `emit/CRT`. 1 Simples, 2 Simples com excesso de sublimite, 3 Regime Normal, 4 MEI | inteiro 1–4; `tax_regime` (texto antigo) acompanha automaticamente |
| `document` | `emit/CNPJ` | DV real; aceita CNPJ alfanumérico (IN RFB 2.229/2024) e CPF |
| `state_registration` | `emit/IE` | algoritmo da UF do endereço (validada só quando muda; a prontidão sempre confere) |
| `municipal_registration` | `emit/IM` | texto |
| `cnae` | `emit/CNAE` | 7 dígitos |
| `special_tax_regime` | `ISSQNtot/cRegTrib` (1–6), opcional | lista fechada |
| `city_ibge_code` | `enderEmit/cMun` | 7 dígitos, prefixo = UF, DV módulo 10 (com as 9 exceções oficiais) |
| `address_*` | `enderEmit` | UF da lista oficial, CEP 8 dígitos |
| `simples_credit_rate` | `pCredSN` (CSOSN 101/201) | 0–100, até 4 casas |
| `approx_tax_fallback_rate` | % usado na Lei 12.741 quando o NCM não está no IBPT | 0–100 |

Responsável técnico (`infRespTec`): padrão da plataforma por variáveis de ambiente
`PDV_FISCAL_TECH_CNPJ`, `PDV_FISCAL_TECH_CONTACT` (padrão "CrownTech"), `PDV_FISCAL_TECH_EMAIL`
(padrão `PDV_SUPPORT_EMAIL`) e `PDV_FISCAL_TECH_PHONE`. **O CNPJ da CrownTech precisa ser configurado
antes da produção** (não foi inventado). CSC/ID do CSC e certificado ficam para a frente de emissão.

API: `GET/PATCH /api/v1/ops/company` (owner/admin). A resposta traz `fiscal_status`
(`ready`, `missing`, `warnings`, `issues`) e `tech_responsible`.

## 2. Perfis tributários (grupos de tributação)

`TaxProfile`: `name`, `description`, `preset`, `fields` (dict validado), `is_default`, `active`.
Cada empresa tem exatamente um perfil padrão (criado sob demanda pelo regime). Produto sem
perfil segue o padrão.

### Campos (`shared/fiscal/schema.py::FIELDS`)

| Campo | Tag NF-e | Formato |
| --- | --- | --- |
| `origin` | `orig` | 0–8 |
| `ncm`, `ex_tipi`, `cest` | `NCM`, `EXTIPI`, `CEST` | 8 / 1–3 / 7 dígitos (CEST segmento 01–28) |
| `cfop` | `CFOP` | 4 dígitos; na NFC-e só a lista `NFCE_CFOPS` |
| `icms_cst` / `icms_csosn` | `CST` / `CSOSN` | listas fechadas; nunca os dois na mesma camada |
| `icms_bc_mode`, `icms_rate`, `icms_bc_reduction`, `fcp_rate`, `icms_deferral_rate` | `modBC`, `pICMS`, `pRedBC`, `pFCP`, `pDif` | % texto decimal, até 4 casas |
| `st_bc_mode`, `st_mva`, `st_rate`, `st_bc_reduction`, `st_fcp_rate` | `modBCST`, `pMVAST`, `pICMSST`, `pRedBCST`, `pFCPST` | ST calculada na própria venda |
| `st_retained_bc_unit`, `st_retained_rate`, `st_retained_substitute_unit`, `st_retained_fcp_rate` | `vBCSTRet` (por unidade), `pST`, `vICMSSubstituto` (por unidade), `pFCPSTRet` | ST já recolhida (CST 60 / CSOSN 500) |
| `cbenef` | `cBenef` | UF + 6 ou 8 caracteres |
| `pis_cst`, `pis_rate`, `cofins_cst`, `cofins_rate`, `pis_cofins_exclude_icms` | grupos PIS/COFINS | CST de saída 01–09, 49, 99 |
| `ipi_cst`, `ipi_rate`, `ipi_enquadramento` | `IPI` (só NF-e) | CST 50–55, 99; `cEnq` padrão 999 |
| `tax_unit`, `tax_unit_factor`, `tax_gtin` | `uTrib`, `qTrib = qCom × fator`, `cEANTrib` | `tax_gtin` só no produto |
| `ibs_cbs_cst`, `ibs_cbs_class`, `ibs_state_rate`, `ibs_city_rate`, `cbs_rate`, `ibs_cbs_base_excludes_taxes` | grupo `IBSCBS` (Reforma, 2026+) | opcionais; sem eles nada muda |

### Resolução por produto

`padrões < perfil < tax_fields legado < fiscal_overrides do produto < coluna Product.ncm`.
Escolher CST numa camada remove o CSOSN das camadas de baixo (e vice-versa).

### Modelos prontos (`presets.py`)

| slug | Regime | Códigos | Quando usar |
| --- | --- | --- | --- |
| `simples_tributado` | CRT 1/4 | CSOSN 102, CFOP 5102, PIS/COFINS 49 | revenda comum do Simples/MEI, sem ST |
| `simples_st` | CRT 1/4 | CSOSN 500, CFOP 5405 | ST já paga pelo fornecedor (exige CEST) |
| `simples_isento` | CRT 1/4 | CSOSN 400, CFOP 5102 | isentos/não tributados |
| `normal_tributado` | CRT 2/3 | CST 00, 5102, PIS 0,65 / COFINS 3 (01) | Lucro Presumido; informe a alíquota interna |
| `normal_tributado_real` | CRT 2/3 | CST 00, PIS 1,65 / COFINS 7,6 | Lucro Real |
| `normal_reducao` | CRT 2/3 | CST 20 | redução de base (informe % e cBenef) |
| `normal_st` | CRT 2/3 | CST 60, CFOP 5405 | ST já recolhida |
| `normal_isento` | CRT 2/3 | CST 40 | isento |

`isento` é um apelido que escolhe a variante do regime da empresa. Alíquotas que dependem da UF
ou do produto (ICMS interno, MVA, ST retida) ficam vazias de propósito: a prontidão diz o que falta.

### API

| Método | Rota | Papéis |
| --- | --- | --- |
| GET | `/api/v1/tax-profiles` (`include_inactive=1`) | owner, admin, manager, inventory, finance, viewer |
| POST | `/api/v1/tax-profiles` `{name?, preset?, fields?, description?, is_default?}` | owner, admin, manager, finance |
| GET/PATCH/DELETE | `/api/v1/tax-profiles/<id>` (DELETE = desativar; 409 se padrão ou em uso) | idem |
| GET | `/api/v1/tax-profiles/presets` | leitura |
| GET | `/api/v1/fiscal/reference` (tabelas para telas) | qualquer membro |
| GET | `/api/v1/fiscal/status` (empresa + contagem de produtos prontos) | leitura |
| POST | `/api/v1/fiscal/simulate` | leitura |
| POST | `/api/v1/products/import` `{csv, dry_run}` | owner, admin, manager, inventory |

Erro de campo fiscal: HTTP 400 `{"error": "...", "code": "fiscal_field_invalid", "field": "icms_cst"}`.

Produto (`/api/v1/products`): novos campos `tax_profile_id` e `fiscal_overrides`; a resposta inclui
`fiscal = {ready, missing[], warnings[], issues[], profile_id, profile_name}`. Clientes antigos
continuam funcionando (campos novos são opcionais e `tax_fields` segue aceito).

## 3. Prontidão ("Pronto para nota" / "Falta: …")

`readiness.product_issues(fields, crt, uf, model)` devolve `{code, field, severity, message}`.
`error` impede a nota; `warning` é conselho para o contador. Exemplos: `Falta NCM…`,
`Falta CEST: este produto está na substituição tributária…`, `Empresa do Simples Nacional usa CSOSN…`,
`Falta a alíquota de ICMS para o CST 00`, aviso de cBenef nas UFs que exigem, CFOP de ST com código
sem ST, ST calculada em NFC-e, IPI informado em NFC-e, CEST sugerido pelo segmento do NCM.
`readiness.company_issues()` faz o mesmo para o emitente.

## 4. Terminal

O bootstrap (`GET /api/v1/terminal/bootstrap`) passa a enviar por produto
`fiscal = {v: 1, profile_id, fields: {...resolvidos}, ready, missing[≤5]}` e, em `issuer`, `crt`,
`cnae`, `city_ibge_code`, `special_tax_regime`, `simples_credit_rate`, `fiscal_ready`,
`tech_responsible`. Terminais antigos ignoram as chaves novas.

No SQLite do terminal: colunas `products.fiscal` (JSON) e `products.fiscal_ready` (1, 0 ou NULL =
desconhecido, nuvem antiga), criadas por `ALTER TABLE` na abertura. Ao concluir uma venda com
item `fiscal_ready=0`, a resposta de `POST /api/sales` traz `fiscal_warnings` e a tela mostra um aviso
discreto. **A venda nunca é bloqueada** e o evento `sale.completed` não muda.

## 5. Motor: contrato para a frente de emissão

```python
from shared.fiscal import compute_document
doc = compute_document(
    issuer={"crt": 1, "uf": "SP", "simples_credit_rate": "1.25"},
    items=[{"product_id": "...", "code": "SKU", "description": "Nome", "barcode": "789...",
            "unit": "UN", "quantity_milli": 2000, "unit_price_cents": 899,
            "discount_cents": 0, "freight_cents": 0, "other_cents": 0,
            "fiscal": {...campos resolvidos...}}],
    model=65,                  # 65 NFC-e, 55 NF-e
    discount_cents=None,       # desconto da nota, rateado se os itens não têm desconto próprio
    approx=callable_or_None,   # (ncm, ex) -> linha IBPT
    approx_fallback="31.45",   # % quando não há linha IBPT
)
```

Na nuvem use `core.fiscal.simulate(tenant, [(product, qty_milli, price_cents, discount_cents)], model,
discount_cents)`, que resolve perfis e IBPT. Para venda do terminal, passe em `discount_cents` de cada
item `promo_discount_cents + discount_cents` da linha (o terminal já rateou).

Saída (`schema = "pdv2.fiscal.v1"`, dinheiro sempre em **centavos inteiros**, alíquotas como texto decimal):

```text
{schema, model, issuer: {crt, uf}, ready,
 items: [{number, product_id, code, description, gtin ("SEM GTIN" para código interno 2xxx),
          ncm, ex_tipi, cest, cfop, cbenef, origin,
          unit, quantity ("2.0000"), quantity_milli, unit_price ("8.99"), unit_price_cents,
          tax_unit, tax_quantity, tax_unit_price (até 10 casas), tax_gtin,
          gross_cents (vProd), discount_cents (vDesc), freight_cents, other_cents,
          total_cents (contribuição do item ao vNF),
          icms: {group ("ICMS00", "ICMSSN102"...), cst, csosn, origin, bc_mode, bc_cents, rate,
                 bc_reduction, value_cents, fcp_rate, fcp_cents, operation_cents, deferral_rate,
                 deferred_cents, sn_credit_rate, sn_credit_cents, cbenef,
                 st: {bc_mode, mva, bc_reduction, bc_cents, rate, value_cents, fcp_bc_cents, fcp_rate, fcp_cents} | null,
                 st_retained: {bc_cents, rate, substitute_cents, value_cents, fcp_bc_cents, fcp_rate, fcp_cents} | null},
          pis / cofins: {cst, group ("Aliq" | "NT" | "Outr" | "Qtde"), bc_cents, rate, value_cents} | null,
          ipi: {cst, enquadramento, group, bc_cents, rate, value_cents} | null (só modelo 55),
          ibs_cbs: {cst, class, bc_cents, ibs_state_rate, ibs_state_cents, ibs_city_rate, ibs_city_cents,
                    ibs_cents, cbs_rate, cbs_cents} | null,
          approx_tax: {source ("ibpt" | "fallback" | "none"), version, federal_cents, state_cents,
                       municipal_cents, total_cents},
          issues: [...], ready}],
 totals: {products_cents (vProd), discount_cents (vDesc), freight_cents, other_cents,
          icms_bc_cents (vBC), icms_cents (vICMS), fcp_cents (vFCP), st_bc_cents (vBCST), st_cents (vST),
          fcp_st_cents (vFCPST), st_retained_cents, fcp_st_retained_cents (vFCPSTRet), ipi_cents (vIPI),
          pis_cents (vPIS), cofins_cents (vCOFINS), sn_credit_cents, approx_tax_cents (vTotTrib),
          total_cents (vNF), ibs_state_cents, ibs_city_cents, ibs_cents, cbs_cents, ibs_cbs_bc_cents}}
```

A frente de emissão **deve recusar** documento com `ready = false` e apresentar `issues`.

### Regras de cálculo

- `vProd = round(preço × qtd)` (mesma fórmula do terminal). Base do item = `vProd − vDesc + frete + outros`.
- ICMS próprio: `vBC = base (+ IPI na NF-e a consumidor)`; CST 20/70/90 reduzem a base por `pRedBC`;
  FCP sobre a mesma base. CST 51: `vICMS = vICMSOp − vICMSDif` (`pDif` padrão 100%).
- ST na venda (10, 30, 70, 90, 201–203, 900): `vBCST = base × (1 + MVA) × (1 − pRedBCST)` (modalidade 4)
  ou valor da operação (6); `vICMSST = vBCST × pICMSST − ICMS próprio` (para 30 e 201–203 o ICMS próprio é
  o hipotético com `icms_rate`); `vFCPST = vBCST × pFCPST − FCP próprio`. Pauta/preço tabelado: não suportado (erro).
- ST retida (60, 500): `vBCSTRet = base_unitária × qtd`, `vICMSSubstituto = unitário × qtd`,
  `vICMSSTRet = vBCSTRet × pST − vICMSSubstituto`; não soma no vNF.
- Simples 101/201: `vCredICMSSN = base × pCredSN`.
- PIS/COFINS: CST 01/02 `base × alíquota` (base sem ICMS se `pis_cofins_exclude_icms`); 04–09 sem valores;
  49/99 só calculam se houver alíquota; 03 (por unidade) não suportado.
- IBS/CBS: `base = valor do item − (vICMS + vFCP + vPIS + vCOFINS)` (desligável por perfil);
  valores informativos, não somam no vNF.
- Lei 12.741: `(vProd − vDesc) × (federal + estadual + municipal)`; federal "importados" para origens 1, 2, 6, 7.
- `vNF = vProd − vDesc + vST + vFCPST + vFrete + vOutro + vIPI`.

### Arredondamento

Cada valor monetário de item é calculado com `Decimal` a partir dos operandos exatos e arredondado
**uma vez** para centavos com `ROUND_HALF_UP` (a SEFAZ aceita diferença de R$ 0,01 em `vProd = qCom × vUnCom`
e em `vICMS = vBC × pICMS`, e HALF_UP é o arredondamento comercial já usado pelo terminal). Os totais são
a **soma exata** dos valores já arredondados dos itens; por isso `soma(itens) == totais` sempre (as regras
de total da SEFAZ comparam com a soma dos itens). Desconto da nota: rateio proporcional pelo maior resto,
desempate no primeiro item, nunca deixando item negativo — idêntico ao terminal.

## 6. IBPT

`python cloud/manage.py load_ibpt TabelaIBPTaxSP26.2.A.csv --uf SP` substitui as linhas da UF (`IbptRate`).
Linhas vencidas (`vigenciafim` passada) são ignoradas. Sem linha: usa `approx_tax_fallback_rate` da empresa;
sem ele, `vTotTrib = 0` com `source = "none"`.

## 7. Migração

`0042_fiscal_profiles` (schema) e `0043_fiscal_backfill` (dados): CRT a partir de `tax_regime`;
perfil padrão por empresa (Simples → CSOSN 102; normal/excesso → CST 00 Presumido); todo produto aponta
para ele e mantém o NCM; chaves reconhecidas do `tax_fields` legado (cst, csosn, cfop, cest, origem,
aliquota_icms, cst_pis…) com valores válidos vão para `fiscal_overrides` — o `tax_fields` original fica intacto.

## 8. Painel

- **Produtos**: cadastro em abas *Básico* / *Fiscal* (perfil, NCM, CEST, CFOP, origem; demais campos
  em "Campos fiscais avançados", recolhidos); coluna **Nota fiscal** com o selo "Pronto para nota" ou "Falta …";
  botão **Importar CSV** com conferência antes de gravar.
- **Operação > Perfis tributários**: lista, modelos prontos com explicação, edição, padrão, desativar.
- **Minha empresa**: CRT, CNAE, IBGE, regime especial, pCredSN, % aproximado, pendências do emitente e responsável técnico.

Colunas do CSV (cabeçalho, `;` ou `,`, sem acento/maiúsculas importando): nome, sku, ean, preço, custo,
unidade, categoria, ativo, ncm, cest, cfop, origem, cst, csosn, aliquota_icms, reducao_bc, fcp, cbenef,
cst_pis, aliquota_pis, cst_cofins, aliquota_cofins, perfil_tributario, unidade_tributavel,
fator_tributavel, gtin_tributavel, bc_st_retido_unitario, pst, icms_substituto_unitario, cclasstrib,
cst_ibs_cbs. Casa por SKU e depois por EAN; linha com erro impede a gravação do arquivo inteiro.

## 9. Fontes dos dados embutidos

- Códigos de UF e regra do DV do código de município: IBGE.
- CRT, origem, CST/CSOSN, modBC/modBCST, grupos: MOC NF-e/NFC-e 7.0, leiaute 4.00 e Ajuste SINIEF 07/2005.
- CST PIS/COFINS/IPI: IN RFB 1.009/2010.
- CFOPs da NFC-e: MOC da NFC-e (regra de CFOP para venda a consumidor).
- Segmentos CEST: Convênio ICMS 142/2018 — a tabela de prefixos de NCM é **heurística** (só gera aviso).
- Algoritmos de IE: páginas de conferência do SINTEGRA, testados com os exemplos publicados por UF.
- CNPJ alfanumérico: IN RFB 2.229/2024 (exemplo oficial `12.ABC.345/01DE-35` nos testes).
- UFs com cBenef: adoção estadual da NT 2019.001 (lista curta, gera aviso, não erro).
- IBS/CBS: LC 214/2025 e NT 2025.002 (base com exclusões). Em 2026, CBS 0,9% e IBS 0,1% (fase de teste).

## 10. Limites conhecidos

- A emissão (XML, assinatura, CSC, certificado, numeração, contingência) está em [NFCE.md](NFCE.md).
- ST por pauta/preço tabelado, PIS/COFINS por unidade (CST 03), ICMS desonerado (`vICMSDeson`),
  ICMS monofásico de combustíveis, DIFAL/partilha (vendas interestaduais a não contribuinte) e ISSQN não são calculados.
- Existência do NCM: o validador aceita uma função `ncm_exists`; a tabela NCM oficial não está embutida.
- A tabela CEST × NCM é parcial (avisos); a obrigatoriedade definitiva do CEST vem do CST/CSOSN de ST.
- cBenef é exigido como aviso; a tabela de códigos de cada UF não está embutida.
- Valores de IBS/CBS seguem a regra geral; regimes específicos/diferenciados da LC 214 dependem de `cClassTrib` e do contador.
