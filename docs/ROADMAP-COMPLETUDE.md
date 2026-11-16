# PDV2 — Roadmap de completude funcional

18/10/2026, com a tabela de andamento atualizada em 16/11/2026. Complementa a [auditoria de novembro/2026](AUDITORIA-2026-11.md), o [RELATORIO-AUDITORIA-COMPLETA.md](../RELATORIO-AUDITORIA-COMPLETA.md) e o [STATUS-V2.md](STATUS-V2.md).

A auditoria anterior cobre bem os bloqueios de **produção**: NFC-e, TEF, Pix dinâmico, deploy, hardware e MFA. Este documento cobre o que falta para o PDV2 ser **completo como produto de gestão** para o lojista. Ele parte de três coisas: uma comparação com um PDV de referência já pronto (PHP/MySQL, de curso), uma leitura do código atual e a experiência de mercado com PDVs de varejo.

Os itens P0 fiscais e de pagamento continuam no plano de 90 dias da auditoria e **não se repetem aqui**.

## Andamento

| Item | Estado |
|---|---|
| 1.1 Operador e vendedor na venda | ✅ entregue (inclui correção do sync que recusava `stock_controlled`) |
| 1.3 Caixa na nuvem + 2.1 conferência de caixa | ✅ entregue (turnos reconstruídos do histórico pela migração 0024) |
| 2.2 Comissão de vendedor | ✅ entregue (planos com Financeiro) |
| Onda 3 — relatórios com CSV | ✅ entregue: vendas (dia/hora/forma/operador/vendedor), curva ABC e lucro, fluxo de caixa, estoque baixo com sugestão, perdas. Falta: DRE, gráficos no painel, PDF |
| 2.3 Venda a prazo (fiado) com limite | ✅ entregue (limite controlado offline; extrato e recebimento em todos os planos) |
| 2.4 Devolução parcial, troca e crédito na loja | ✅ entregue no terminal (dinheiro, crédito na loja ou abatimento do fiado) |
| 1.2 Saldo de estoque materializado | ✅ entregue (`StockBalance` + `manage.py reconcile_stock`) |
| 1.4 Formas de pagamento configuráveis | ✅ entregue (ativar/desativar, taxa, prazo e formas próprias; relatório líquido de taxas) |
| 4.1 Grade de variações | ✅ entregue (`ProductGroup`; combinações viram produtos; escolha de variação no caixa) |
| 4.4 Compras pelo XML da NF-e | ✅ entregue (custo real com desconto/frete/IPI/ST, memória de códigos por fornecedor, fator de embalagem, duplicatas → contas a pagar). Falta: pedido de compra e recebimento parcial, manifestação/DF-e automático |
| 4.3 Inventário guiado | ✅ entregue (contagem pelo celular com leitor/câmera, modo cego, ajuste pelo saldo do momento da contagem) |
| 4.5 Transferência entre lojas | ✅ entregue (em trânsito, recebimento com divergência, cancelamento) |
| Fiado no caixa, comprovantes e devolução pelo painel | ✅ entregue (recebimento entra na gaveta e quita as parcelas mais antigas; comprovantes ESC/POS de devolução e recebimento; devolução no painel com reembolso fora do PDV; devolução offline excedente vira conflito) |
| Promoções (Onda 6) | ✅ entregue (preço por período, leve X pague Y, atacado; motor único no terminal, offline; limite de desconto manual sobre o preço promocional; relatório) |
| Auditoria nov/2026 · Onda 0 "Estancar" | ✅ entregue em 16/11/2026: fila do caixa que não trava (aceitação por evento e quarentena), backup que acusa falha, caixa no Windows, `/control/` atrás da MFA, limite de fiado fora do alcance do caixa, logs de produção (fecha o achado A12) e testes de regressão do sync |
| Auditoria nov/2026 · Onda 1 "Cara nova do caixa" | ✅ entregue em 16/11/2026: Caixa Rápido com régua de teclas F, total e troco grandes, sangria e suprimento na tela, temas, marca CrownTech e cupom no leiaute novo |
| Offline local · fases 0 e 1 | ✅ entregue em 16/11/2026: licença offline longa (30 dias, por plano) com renovação própria e avisos no caixa, fila em ordem estável, chave de versões separada, atualização automática do terminal com pacote assinado, troca atômica e volta à versão anterior, instalador Windows com serviço e atalho. **Ainda não testado em Windows físico** (roteiro em [INSTALADOR-WINDOWS.md](INSTALADOR-WINDOWS.md)). Faltam as fases 2 a 4: restauração na troca de computador, estações na rede da loja e acabamento. |
| Próximos, na ordem da auditoria | 1. **Produção no Dokploy** (`pdv.crowntech.com.br`, e-mail, troca e recuperação de senha, alertas) · 2. **Cobrança** (Asaas completo, Stripe com cartão, régua de cobrança) · 3. **Painel e admin** (navegação em 6 áreas, dashboard com gráficos, Console CrownTech, perfis de permissão e planos) · 4. **App** de gestão (iOS e Android) · 5. **Fiscal** (NFC-e pela Focus NFe de ponta a ponta, contingência) · 6. **Offline local, fases 2 a 4** (restauração na troca de computador e estações na rede da loja, conforme [ARQUITETURA-OFFLINE-LOCAL.md](ARQUITETURA-OFFLINE-LOCAL.md)) |
| Próximos de produto (este roadmap) | fidelidade/cashback, importador de planilha para migração, lote e validade (4.7), DRE e gráficos no painel (Onda 3) |

---

## 1. Achados novos no código (não citados na auditoria)

| # | Achado | Evidência | Impacto | Correção |
|---|---|---|---|---|
| A1 | **A venda perde o operador ao chegar na nuvem** | `terminal/store.py` grava `sales.operator_id`, mas `Sale` (`cloud/core/models.py`) e `sale_logic.py` não têm o campo | Sem relatório por operador, sem comissão, sem auditoria de quem vendeu ou cancelou | `Sale.operator` (FK Membership) + `Sale.seller` (opcional) e propagação no evento de sincronização |
| A2 | **O saldo de estoque soma todo o histórico a cada consulta** | `views_stock.py` usa `Sum("delta_milli")` sobre `StockMovement` | Fica lento com volume: 100 mil vendas × itens viram milhões de linhas | Tabela `StockBalance(shop, product, qty_milli, version)` atualizada na mesma transação do movimento, com comando de reconciliação |
| A3 | **O caixa não existe como entidade na nuvem** | `cash_sessions` e `cash_movements` só existem no SQLite; na nuvem chegam como `SyncEvent` cru | O dono não confere caixa, quebra, sangrias nem histórico pelo painel | Modelos `CashSession` e `CashMovement` materializados a partir dos eventos |
| A4 | **O fechamento de caixa conta só o total** | `cash_sessions.counted_cents` é um único valor | Não detecta "faltou no dinheiro e sobrou no cartão", e não há registro de quem conferiu | Contagem por forma de pagamento, opção de fechamento cego e campo de conferente |
| A5 | **Formas de pagamento fixas no código** | Dinheiro, Pix, Crédito e Débito estão escritos direto em `terminal.js` e `index.html` | Não há vale, fiado, voucher, crédito parcelado, taxa nem acréscimo | Cadastro `PaymentMethod` com tipo, taxa %, prazo de recebimento, acréscimo e "gera conta a receber" |
| A6 | **"Variação" é texto livre** | `Product.variant = CharField` | Uma loja de roupa precisa de um produto por cor/tamanho, sem grade nem estoque por combinação | Modelo de grade (ver Onda 4) |
| A7 | **O financeiro é uma lista solta** | `FinanceEntry` não tem categoria, forma de pagamento, baixa parcial, parcelas nem vínculo com venda/entrada | Não sai DRE, fluxo de caixa nem conciliação | Ver Onda 5 |
| A8 | **O cadastro de cliente é mínimo** | `Customer` tem só nome, documento, e-mail e telefone | Não dá suporte a fiado, fidelidade, aniversário nem LGPD | Endereço, nascimento, limite de crédito, saldo devedor, consentimento e observações |
| A9 | **O produto não tem histórico de preço nem códigos adicionais** | Um único `barcode` e um único `price_cents` | Fardo × unidade (EAN da caixa) não funciona; mudança de preço não deixa rastro | `ProductBarcode` (N códigos com fator de multiplicação) e `PriceChange` (auditoria e preço agendado) |
| A10 | **Código V1 legado na raiz** | `app.py` (917 linhas) e `index.html` versionados junto com a V2 | Confunde manutenção, deploy e auditoria | Mover para `legacy/` ou remover, já que a V2 não migra a V1 |
| A11 | **Painel web monolítico** | `web/assets/app.js` com 2.866 linhas | Cada nova tela aumenta o risco de regressão | Dividir em ES modules por página (`pages/sales.js`…), sem precisar de build |
| A12 | **Sem logs estruturados nem rastreamento de erros** | `settings.py` não define `LOGGING` nem Sentry/GlitchTip | Um erro em produção passa despercebido | `LOGGING` em JSON + GlitchTip auto-hospedado + `request_id` nos logs |

---

## 2. Princípios para as próximas ondas

1. **Toda regra de negócio nasce como evento do terminal.** Venda, troca, fiado e comissão precisam funcionar offline e sincronizar de forma idempotente, como a venda já faz.
2. **A nuvem materializa e o terminal consulta um snapshot.** Relatórios e saldos ficam na nuvem; o terminal recebe só o necessário (catálogo, operadores, formas de pagamento, limite de crédito).
3. **Nenhum valor financeiro é editado; ele é estornado.** Mantém o padrão atual de auditoria.
4. **Cada item fecha com teste de contrato terminal→cloud**, igual a `test_contract_cloud.py`.
5. **A funcionalidade fica atrás de um recurso do plano** (`hasFeature`), para gerar upgrade comercial.

---

## 3. Ondas de entrega

O tamanho é uma estimativa para um desenvolvedor: **P** até 3 dias, **M** até 1 semana, **G** até 2 semanas.

### Onda 1 — Fundação de dados (pré-requisito de tudo) · ~2 semanas

| Item | Tam. | O que entregar | Pronto quando |
|---|---|---|---|
| 1.1 Operador e vendedor na venda (A1) | P | `Sale.operator`, `Sale.seller`; o terminal envia `operator_id` e `seller_id` opcional | Lista de vendas filtra por operador; o contrato terminal→cloud cobre o caso |
| 1.2 Saldo materializado (A2) | M | `StockBalance` + `manage.py reconcile_stock` | Teste de 10 mil produtos × 100 mil movimentos com consulta abaixo de 100 ms |
| 1.3 Caixa na nuvem (A3) | M | `CashSession` e `CashMovement` gerados dos eventos `cash_open`, `cash_close`, `sangria` e `suprimento` | O dono vê no painel os caixas abertos e fechados de cada loja |
| 1.4 Formas de pagamento configuráveis (A5) | M | `PaymentMethod` por empresa, sincronizado para o terminal | O terminal mostra só as formas ativas; a venda guarda `method_id` e o nome no momento da venda |
| 1.5 Limpeza técnica (A10, A12) | P | Remove a V1 e configura `LOGGING` e GlitchTip | `check --deploy` sem avisos novos; erros aparecem no painel de erros |

### Onda 2 — Caixa e vendas como o lojista espera · ~3 semanas

| Item | Tam. | O que entregar |
|---|---|---|
| 2.1 **Conferência de caixa** | M | Tela "Caixas": abertura, vendas por forma, sangrias, suprimentos, esperado, contado, **quebra** e conferente. Fechamento **cego** opcional: o operador conta sem ver o esperado. Comprovante de fechamento ESC/POS. |
| 2.2 **Comissão de vendedor** | M | % por vendedor, por categoria ou por produto (a regra mais específica vence). `Commission` gerada da venda e estornada no cancelamento. Tela de comissões pendentes e pagas, com baixa em lote que gera conta a pagar. |
| 2.3 **Venda a prazo (fiado/crediário)** | G | Forma de pagamento do tipo "a prazo" gera `FinanceEntry` receivable com parcelas. **Limite de crédito** do cliente vai para o terminal; acima do limite, exige PIN de gerente. Extrato do cliente e recebimento no próprio caixa (entra como movimento do caixa). |
| 2.4 **Devolução e troca** | G | Devolução **parcial** por item, a partir da venda original (pelo número do comprovante ou do cliente). O valor volta como estorno no meio de pagamento ou como **crédito na loja** (`CustomerCredit`), usável como forma de pagamento. Troca = devolução + nova venda no mesmo atendimento. O estoque volta com motivo. |
| 2.5 Item avulso e consulta de preço | P | Venda de "item diverso" com valor digitado (com permissão) e tecla de consulta de preço sem adicionar ao carrinho. |
| 2.6 Desconto com regra | P | Teto de desconto por papel (ex.: caixa 5%, gerente 15%), motivo obrigatório e relatório de descontos concedidos. |

### Onda 3 — Relatórios e painel (o que vende o sistema) · ~2–3 semanas

Base: relatórios rodam no servidor sobre os dados da Onda 1, com filtro por período, loja, operador e categoria, e **exportação em PDF, CSV e XLSX**.

| Relatório | Para que serve |
|---|---|
| Vendas por período, hora, dia da semana, operador e forma de pagamento | Escala da equipe e conferência |
| **Lucro bruto** por período, produto e categoria (venda − custo médio no momento da venda) | Lucro real, não só a margem do cadastro |
| **Curva ABC** de produtos | Saber o que não pode faltar |
| **Ruptura e estoque baixo** + sugestão de compra (média de giro × dias de cobertura) | Repor antes de perder venda |
| Perdas e ajustes de estoque por motivo | Controle de furto, vencimento e quebra |
| **Fluxo de caixa** realizado × previsto | Saúde financeira |
| **DRE simplificada** (receita, CMV, despesas por categoria, resultado) | Visão do dono |
| Ticket médio, itens por venda, cancelamentos e descontos | Indicadores de operação |
| Caixas e quebras | Conferência (vem da Onda 2) |
| Comissões | Pagamento da equipe |

O painel inicial ganha gráficos (vendas 30 dias, por hora, top produtos, formas de pagamento), cards de alerta (estoque baixo, contas vencendo, quebra de caixa, conflitos offline) e comparação com o período anterior.

### Onda 4 — Estoque e compras · ~3–4 semanas

| Item | Tam. | O que entregar |
|---|---|---|
| 4.1 **Grade de variações** (A6) | G | `ProductGroup` (modelo pai) → `Product` como SKU filho, com atributos (Cor, Tamanho) em `VariantAttribute`. O gerador de grade cria todos os SKUs de uma vez, com código de barras próprio. O terminal mostra uma escolha rápida de variação. Cada SKU continua sendo um `Product`, então venda, estoque e sincronização **não mudam**. |
| 4.2 Saídas com motivo | P | Tipos padronizados: perda, avaria, vencimento, consumo interno, brinde, furto. Aprovação acima de um valor. |
| 4.3 **Inventário guiado** | M | Abrir inventário (total ou por categoria), contar pelo celular/PWA com leitor da câmera, contagem cega, relatório de divergência e ajuste aprovado em lote. |
| 4.4 **Compras completas** | G | Pedido de compra → recebimento (total ou parcial) → **contas a pagar parceladas geradas** → atualização do custo médio. Importação do **XML da NF-e de entrada**, com vínculo do código do fornecedor ao produto (memoriza o vínculo). |
| 4.5 Transferência entre lojas | M | Saída na origem, "em trânsito" e entrada confirmada no destino. |
| 4.6 Múltiplos códigos de barras e fardo (A9) | P | `ProductBarcode` com fator: o EAN do fardo de 12 dá baixa de 12 unidades. |
| 4.7 Lote e validade (opcional por produto) | G | Necessário para mercearia, farmácia e alimentos; alerta de vencimento. |

### Onda 5 — Financeiro de verdade · ~2–3 semanas

| Item | O que entregar |
|---|---|
| Plano de contas | `FinanceCategory` em árvore (Receitas › Vendas; Despesas › Aluguel…), com modelo padrão de varejo já criado |
| Contas bancárias e carteiras | Caixa da loja, banco, maquininha; saldo por conta |
| Baixa parcial, juros, multa e desconto | `FinancePayment` N:1 em `FinanceEntry` |
| Parcelamento e recorrência | Gera N parcelas; a recorrente gera a próxima ao baixar (mensal, semanal, anual) |
| Anexos | Boleto e comprovante no armazenamento de objetos, com hash como em `StockReceipt` |
| **Recebíveis de cartão** | A venda no crédito/débito gera o recebível líquido (taxa da forma de pagamento) com a data prevista; conciliação com o extrato da adquirente |
| Conciliação bancária | Importação OFX e casamento automático por valor e data |
| Vínculos automáticos | Venda a prazo, compra, comissão e sangria (destino) viram lançamentos sem digitação |

### Onda 6 — Diferenciais comerciais · contínuo

| Item | Valor |
|---|---|
| **Promoções** | Preço por período, leve X pague Y, preço de atacado por quantidade, combo e desconto por categoria. Motor avaliado **no terminal** (funciona offline). |
| **Fidelidade e cashback** | Pontos ou % de volta como crédito na loja (reaproveita `CustomerCredit` da 2.4) |
| **WhatsApp** | Comprovante digital, cobrança de fiado vencido, aniversário e "seu pedido chegou" (via provedor oficial, com consentimento LGPD) |
| **Importador de migração** | Planilha (CSV/XLSX) de produtos, clientes, fornecedores e saldos, com prévia e validação. É o que destrava a troca de sistema do lojista. |
| Etiquetas de gôndola | Preço, unidade de medida (R$/kg, obrigatório) e QR; impressão em lote pelas mudanças de preço |
| Metas | Meta de vendas por loja e vendedor, com acompanhamento no painel e no app iOS |
| **Alertas push no app iOS** | Quebra de caixa, cancelamento acima de um valor, estoque zerado, terminal offline há X minutos |
| Catálogo online / pedido por link | Vitrine pública com pedido que cai no terminal como venda suspensa |
| Permissões finas | Permissão por ação (cancelar, dar desconto, abrir gaveta) além do papel |

### Onda SaaS — gestão da sua carteira de clientes

| Item | O que entregar |
|---|---|
| **Bloqueio gradual por inadimplência** | Dias configuráveis: aviso no painel → aviso no terminal → somente leitura → bloqueio de novas vendas (o terminal sempre permite fechar o caixa e sincronizar). Mensagem personalizada. |
| Contrato com aceite eletrônico | Termo versionado, aceite registrado com IP, data e versão; PDF para o cliente |
| Onboarding guiado | Checklist: empresa → produtos (importar) → equipe/PIN → terminal → primeira venda |
| Saúde do cliente | Último uso, vendas/dia, terminais offline, conflitos abertos; aponta risco de cancelamento |
| Chamados | Suporte com anexos e histórico por empresa, aberto pelo painel |
| Trial → pago | Cobrança automática ao fim do teste, dunning e troca de plano com prorrata |

---

## 4. Ordem recomendada e por quê

```text
Onda 1 (fundação) ─┬─> Onda 2 (caixa/vendas) ─┬─> Onda 3 (relatórios)
                   │                          └─> Onda 5 (financeiro)
                   └─> Onda 4 (estoque/compras) ─> Onda 6 / SaaS
```

- **A Onda 1 vem antes de tudo**: comissão, conferência de caixa e relatório por operador dependem de A1 e A3; os relatórios dependem de A2.
- **A Onda 2 é o que o lojista percebe no primeiro dia.** Fiado e conferência de caixa são as perguntas mais comuns na venda de PDV para mercearia.
- **A Onda 3 é o que fecha a venda do sistema**: o dono compra relatório, não cadastro.
- A grade (4.1) só é urgente se o público-alvo incluir moda e calçados. Para mercados e mercearias, 4.3, 4.4 e 4.6 vêm antes.

## 5. O que deixar de fora por enquanto

- Mesas, comandas, cozinha e delivery: é outro produto (food service). Melhor como módulo separado depois do piloto de varejo.
- Ordem de serviço, agenda e SNGPC: só com um segmento-alvo confirmado.
- Reescrever o painel em React/Vue: dividir em módulos (A11) resolve o problema sem custo de migração.

## 6. Primeiro passo sugerido

Implementar **1.1 (operador e vendedor na venda)**: é pequeno, destrava comissão e relatórios, e confirma o fluxo completo (terminal → evento → `sale_logic` → modelo → painel → teste de contrato) que todas as outras ondas vão repetir.
