# Sincronização de vendas e reconciliação de estoque

Atualizado em 16/11/2026. Descreve o que `cloud/core/views_terminal.py`, `sale_logic.py`, `credit_logic.py` e `cash_logic.py` fazem hoje. O contrato antigo (lote atômico: um evento recusado derrubava o lote inteiro) deixou de valer.

## A regra: o que aconteceu no balcão é fato

Quando o evento chega à nuvem, a venda já foi entregue, o dinheiro já entrou ou saiu da gaveta e o estoque local já foi baixado. A nuvem não pode desfazer isso, então:

1. **Todo evento recebido é confirmado.** A resposta devolve em `accepted_event_ids` o ID de todos os eventos do lote, na ordem em que vieram. O caixa remove esses eventos da fila e segue.
2. **Divergência vira conflito, não recusa.** Se o cadastro mudou depois da venda offline (limite de desconto, unidade, controle de estoque, produto ou forma de pagamento desativados, promoção alterada) ou se o painel mexeu na mesma venda, o evento é aplicado como aconteceu e a nuvem abre um conflito para o administrador conferir.
3. **O que não pode ser aplicado vai para a quarentena.** Um evento com erro de integridade (produto que não existe na empresa, totais que não fecham, ID repetido com outro conteúdo) é guardado com o conteúdo original, vira um conflito `rejected_event` e também é confirmado. Um evento ruim nunca trava os seguintes.

## Pedido e resposta

`POST /api/v1/terminal/sync` com `Authorization: Bearer <token do terminal>` e `{ "events": [...] }`, de 1 a 50 eventos. O terminal ainda limita o corpo a 512 KiB e, se receber HTTP 413, reenvia com metade do lote.

Cada evento: `{event_id, type, aggregate_id, identity, data}`.

- `event_id`: 32 caracteres hexadecimais, estável no reenvio.
- `type`: `sale.completed`, `sale.voided`, `sale.returned`, `credit.received`, `stock.adjusted`, `cash.opened`, `cash.closed`, `cash.withdrawn` ou `cash.supplied`.
- `aggregate_id`: ID local do agregado (venda, devolução, recebimento, sessão ou movimento de caixa).
- `identity`: `{tenant_id, shop_id, terminal_id}`, igual ao que o token representa.

Resposta `200`:

```json
{
  "accepted_event_ids": ["<todos os event_id recebidos, na ordem>"],
  "conflicts": [{"id": "...", "event_id": "...", "event_type": "sale.completed", "aggregate_id": "...",
                 "reason": "discount_over_limit", "status": "open", "detail": {"message": "...", "...": "..."},
                 "terminal_id": "...", "shop_id": "...", "created_at": "..."}],
  "quarantined": [{"event_id": "...", "reason": "rejected_event", "message": "Totais da venda não conferem"}]
}
```

- `accepted_event_ids` contém **todos** os eventos recebidos: aplicados, aplicados com conflito e em quarentena. O terminal só dá baixa na fila se a lista for idêntica à que enviou; qualquer outra coisa é tratada como confirmação incompleta e o lote é reenviado.
- `conflicts` traz um registro por evento e motivo. O `detail` vem sem o `payload` (o terminal já tem o que enviou).
- `quarantined` lista só os eventos que não puderam ser aplicados. Eles também aparecem em `conflicts` com `reason = rejected_event`.

### Como o lote é processado

- A linha do terminal é travada no banco durante o lote, então dois lotes do mesmo caixa nunca se misturam.
- Cada evento roda no seu próprio *savepoint*. Se a aplicação falha, só aquele evento é desfeito e ele vai para a quarentena; os anteriores e os seguintes continuam valendo.
- A ordem do lote é respeitada: uma devolução pode chegar no mesmo lote da venda a que se refere.
- A quarentena também roda em *savepoint*. Se o banco recusa o conteúdo original (caractere nulo, `NaN`), o evento é guardado como texto escapado.
- Erro inesperado no código é registrado em log com *traceback* e o evento vai para a quarentena com a mensagem "Erro interno ao processar o evento".

### O que ainda recusa o pedido inteiro

Só o que impede saber de quem é o evento, ou uma falha de infraestrutura. Nesses casos nada é confirmado e o caixa reenvia depois:

| Situação | Resposta |
|---|---|
| Token ausente, inválido ou de terminal revogado | 401 |
| Corpo sem `events`, ou fora de 1 a 50 eventos | 400 |
| Evento sem `event_id` em texto de 1 a 64 caracteres | 400 |
| `identity` diferente do terminal do token | 403 |
| Banco de dados indisponível durante o lote | erro 5xx; o lote inteiro é reenviado |

### Reenvio

O terminal reenvia quando perde a resposta. A nuvem reconhece o reenvio antes de validar qualquer regra, para não julgar duas vezes um fato já aceito:

- mesmo `event_id` e mesmo conteúdo: confirmado de novo, e os conflitos já abertos para aquele evento voltam na resposta (inclusive a quarentena);
- mesma venda (`aggregate_id`) com outro `event_id` e conteúdo idêntico: confirmada sem repetir baixa de estoque, fiado ou comissão;
- mesmo `event_id`, ou mesma venda, devolução, recebimento ou ajuste, com **conteúdo diferente**: quarentena (`rejected_event`, HTTP 409 registrado no detalhe).

## Motivos de conflito (`reason`)

Todos, exceto `rejected_event`, significam "o evento foi aceito e aplicado; confira".

| `reason` | Evento | Significado |
|---|---|---|
| `negative_stock` | `sale.completed` | A venda deixou o saldo central negativo. A venda é mantida (`stock_status = negative`); o detalhe lista produto, quantidade vendida e saldo central. |
| `discount_over_limit` | `sale.completed` | O desconto manual passa do limite atual da administração (`max_discount_bps`), calculado sobre o preço já com promoção. |
| `unit_changed` | `sale.completed`, `stock.adjusted` | A unidade do produto (UN/KG/L) mudou depois da venda, ou chegou um ajuste fracionado para produto que hoje é vendido por unidade. |
| `stock_control_changed` | `sale.completed`, `stock.adjusted` | O "controlar estoque" do produto mudou. Na venda, vale o cadastro central para a baixa. No ajuste, se o produto não controla mais estoque, **o ajuste não é aplicado** (`applied: false`). |
| `product_inactive` | `sale.completed`, `stock.adjusted` | Venda ou ajuste de produto desativado no cadastro. |
| `promotion_changed` | `sale.completed` | A promoção usada no caixa foi desativada ou alterada depois da venda (tolerância de 5 minutos para diferença de relógio). |
| `payment_method_inactive` | `sale.completed` | A venda foi recebida em forma de pagamento desativada depois. |
| `void_already_reversed` | `sale.voided` | A venda cancelada no caixa já estava cancelada ou estornada no painel, com outro motivo ou operação. Conferir se o cliente foi reembolsado duas vezes. |
| `void_after_return` | `sale.voided` | A venda foi cancelada no caixa depois de uma devolução. O estoque e a comissão já devolvidos não voltam de novo. |
| `fiscal_cancel_required` | `sale.voided` | A venda cancelada tem NFC-e autorizada ou em processamento: a nota precisa ser cancelada na SEFAZ. |
| `return_exceeds_sold` | `sale.returned` | A devolução no caixa passa do que ainda podia ser devolvido (em geral já havia devolução pelo painel). |
| `return_on_reversed_sale` | `sale.returned` | Devolução de uma venda já cancelada ou estornada no painel. Só o reembolso é registrado; estoque, fiado e comissão não são mexidos de novo. |
| `customer_inactive` | `credit.received` | Fiado recebido de cliente desativado. O dinheiro já entrou, então o recebimento é lançado. |
| `cash_expected_negative` | `cash.closed` | Caixa fechado com valor esperado negativo (venda em dinheiro cancelada depois de uma sangria). O fechamento é aceito como contado. |
| `cash_closed_twice` | `cash.closed` | O mesmo caixa foi fechado duas vezes com valores diferentes (computador antigo e novo depois de "Trocar de computador"). O detalhe traz `first` e `second` (`closed_at`, `counted_cents`, `expected_cents`); vale o fechamento de `created_at` mais recente, qualquer que seja a ordem de chegada. |
| `rejected_event` | qualquer | **Quarentena.** O evento não pôde ser aplicado. O detalhe guarda `type`, `error`, `status` e o `payload` original. |

### O que vai para a quarentena

- formato inválido (campo faltando ou desconhecido, `event_id` fora do padrão, tipo desconhecido, data inválida);
- produto, cliente, operador, vendedor, promoção ou forma de pagamento própria que não existe na empresa;
- totais que não fecham: linha (`preço × quantidade ÷ 1000`, arredondado para cima a partir de meio), soma dos itens, pagamentos menos troco, troco maior que o dinheiro;
- produto `UN` com quantidade fracionada; fiado ou crédito na loja sem cliente cadastrado;
- `request_id` já usado por outra venda do mesmo terminal;
- cancelamento, devolução ou recebimento que aponta para venda ou cliente inexistente;
- conteúdo diferente para um ID já recebido.

Para corrigir: cadastre ou ajuste o que faltava e lance a operação pelo painel. O evento em quarentena **não é reprocessado sozinho**; ele fica guardado para consulta.

## Onde o administrador vê

- Painel: `GET /api/v1/sync/conflicts?status=open|acknowledged|resolved|ignored` (proprietário, administrador ou gerente). `PATCH /api/v1/sync/conflicts/<id>` com `{status, resolution?}` marca como `acknowledged`, `resolved` ou `ignored`. A resolução é auditada e não reescreve a venda.
- Caixa: os conflitos e a quarentena devolvidos na resposta ficam no SQLite local (`sync_conflicts`, `sync_quarantine`) e aparecem para o gerente em `GET /api/sync/conflicts`.

## Conteúdo dos eventos

Dinheiro em centavos, quantidade em milésimos, datas em epoch **milissegundos**. Campos desconhecidos mandam o evento para a quarentena.

### `sale.completed`

`aggregate_id` é o ID imutável da venda.

```text
data: {request_id, cash_session_id, created_at, gross_cents, discount_cents, total_cents, change_cents,
       promo_discount_cents?, customer_id?, customer_name?, customer_document?, operator_id?, seller_id?,
       items: [{product_id, name, unit, quantity_milli, unit_price_cents, gross_cents, discount_cents,
                line_total_cents, promo_discount_cents?, promotion_id?, promotion_name?,
                ncm?, sku?, stock_controlled?}],
       payments: [{method, amount_cents, payment_method_id?}]}
```

- `operator_id` e `seller_id`: quem operou o caixa e quem vendeu (membros da empresa). A venda guarda também o nome de cada um no momento do recebimento. A comissão é calculada para o vendedor.
- `promo_discount_cents`: desconto de promoção, por item e somado na venda. Exige `promotion_id` no item, e vice-versa. O total da linha é `gross_cents − promo_discount_cents − discount_cents`. O limite de desconto da administração vale só para `discount_cents` (o desconto manual).
- `payment_method_id`: obrigatório quando `method = other` (forma de pagamento própria da empresa) e proibido nas demais. Formas aceitas: `cash`, `pix`, `card`, `card_credit`, `card_debit`, `other`, `credit_account` (fiado) e `store_credit` (crédito na loja); as duas últimas exigem `customer_id`.
- `stock_controlled` no item é o retrato do catálogo do caixa; a baixa segue o cadastro central e a diferença vira `stock_control_changed`.
- Os preços da venda valem como foram praticados; o catálogo atual não substitui o retrato da venda offline.

Efeitos: grava venda, itens e pagamentos; uma baixa de estoque por produto controlado (`source = sale`, chave `sale:<venda>:<produto>`); conta a receber por pagamento em fiado, com vencimento em `credit_term_days`; uso de crédito na loja; comissão.

### `sale.voided`

`data: {sale_id, reason, created_at}`, com `aggregate_id = sale_id`. Cancela a venda, devolve o estoque que ainda não tinha voltado por devolução (`source = void`), desfaz fiado e crédito e estorna a comissão. Nunca é recusado por causa do que o painel fez com a venda: vira `void_already_reversed`, `void_after_return` ou `fiscal_cancel_required`.

### `sale.returned`

```text
data: {sale_id, items: [{product_id, quantity_milli, amount_cents}], total_cents, refund_method, reason,
       operator_id, created_at, customer_id?, supervisor_id?, cash_session_id?}
```

`aggregate_id` é o ID da devolução. `refund_method` no caixa: `cash`, `store_credit` (exige cliente) ou `account` (abate o fiado da venda; se o fiado já foi pago, a diferença vira crédito na loja). `amount_cents` de cada item é a parte proporcional da linha vendida. Efeitos: entrada de estoque (`source = return`), crédito ou abatimento, e estorno proporcional da comissão.

Na consulta da venda (`GET /api/v1/sales/<id>`), cada devolução aparece em `returns`, e cada item devolvido traz `unit`: a unidade em que o produto foi vendido naquela venda.

### `credit.received`

`data: {customer_id, amount_cents, method, operator_id, created_at, cash_session_id?}`, com `method` em `cash`, `pix`, `card_credit` ou `card_debit`. `aggregate_id` é o ID do recebimento. Quita as parcelas de fiado mais antigas do cliente; uma parcela paga em parte é dividida; o que passar do saldo em aberto vira crédito na loja.

### `stock.adjusted`

`data: {product_id, delta_milli, reason, created_at}`. Entra uma vez no estoque central (`source = terminal_adjustment`).

### `cash.opened`, `cash.closed`, `cash.withdrawn`, `cash.supplied`

Alimentam a sessão de caixa na nuvem: abertura (`opening_cents`, `operator_id`), fechamento (`counted_cents`, `expected_cents`, `difference_cents`, `operator_id?`, `supervisor_id?`), sangria e suprimento (`cash_session_id`, `amount_cents`, `reason`, `operator_id`). `expected_cents` pode ser negativo; `difference_cents` tem de ser `counted_cents − expected_cents`.

## Regra para não baixar duas vezes

O caixa baixa o **próprio banco local** quando conclui a venda. A nuvem baixa **o estoque central** ao receber `sale.completed`. São saldos separados. Não envie a baixa da venda também como `stock.adjusted` nem como movimento manual: seria uma segunda baixa central. Use `stock.adjusted` só para ajustes de inventário feitos no caixa; entradas e ajustes do painel vão por `POST /api/v1/stock/movements` e `POST /api/v1/stock/receipts`.

Para a carga inicial, use um único caminho: lance o saldo no painel e deixe o caixa recebê-lo pelo bootstrap.

O saldo central de `GET /api/v1/stock?shop_id=...` soma todas as fontes; `GET /api/v1/stock/movements?shop_id=...` mostra cada movimento com `source` (`manual`, `receipt`, `sale`, `void`, `terminal_adjustment`, `return`, `inventory`, `transfer`).

Dois caixas desconectados podem vender o mesmo saldo. A nuvem recebe as duas vendas, marca `stock_status = negative` e abre `negative_stock`; não altera nem descarta a venda para forçar o saldo a zero.

## Heartbeat

O terminal envia `POST /api/v1/terminal/heartbeat` mesmo sem vendas, com versão, tamanho da fila e último erro. Isso atualiza `last_seen_at` e alimenta o monitoramento do proprietário.

## Leitura de vendas

- `GET /api/v1/sales?limit=50&shop_id=...&cursor=...` devolve `results` e `next_cursor` (limite máximo 100).
- `GET /api/v1/sales/<id>` devolve cabeçalho, itens, pagamentos e devoluções, sempre limitado à empresa autenticada.
- `stock_status` mostra a condição do saldo central no momento da recepção. Um ajuste posterior não reescreve esse histórico.

## Limites conhecidos

- A quarentena não tem reprocessamento automático: depois de corrigir o cadastro, a operação precisa ser relançada pelo painel.
- O catálogo é baixado por bootstrap periódico, sem cursor incremental.
- NFC-e, contingência fiscal, TEF e Pix dinâmico ficam fora deste fluxo e exigem homologação própria.

Os testes de regressão deste contrato estão em `cloud/core/tests_sync_resiliente.py` e `terminal/tests/test_contract_cloud.py`.
