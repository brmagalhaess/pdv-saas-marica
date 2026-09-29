# Sincronização de vendas e reconciliação de estoque

## Contrato atual

O caixa envia lotes de 1 a 50 eventos para `POST /api/sync/events` com o token do terminal em `Authorization: Bearer`. A credencial fixa empresa e loja; a identidade de cada evento deve coincidir. O servidor grava o lote em uma transação SQLite e só devolve `accepted_event_ids` após o commit. Se qualquer evento for inválido ou conflitante, rejeita o lote inteiro, sem confirmar nem aplicar os anteriores.

Em `sale.completed`, `aggregate_id` é o ID imutável da venda. O servidor valida produto cadastrado na mesma empresa, itens, preços registrados no momento da venda, soma dos itens, pagamentos e troco. Os preços atuais do catálogo não substituem o retrato da venda offline. A venda fica em `sales`, com itens em `sale_items` e pagamentos em `sale_payments`.

Cada item gera uma baixa em `sale_stock_movements`, cuja chave única é `(empresa, venda, produto)`. Reenviar o mesmo `event_id` ou a mesma venda com outro `event_id` e conteúdo idêntico confirma o evento sem repetir a baixa. O mesmo ID com conteúdo diferente resulta em conflito. O `request_id` também é único por terminal. Eventos `stock.adjusted` entram uma vez em `terminal_stock_movements`.

## Regra para não baixar duas vezes

O caixa local baixa o **próprio banco local** quando conclui a venda. A nuvem baixa **seu ledger central** ao receber `sale.completed`. São saldos separados. Não envie a baixa da venda também para `POST /api/stock/movements` nem como `stock.adjusted`: isso causaria uma segunda baixa central. Use `stock.adjusted` somente para ajustes de inventário feitos no caixa; use `/api/stock/movements` para entradas e ajustes feitos no painel.

O saldo central mostrado por `GET /api/stock?shop_id=...` soma entradas/ajustes manuais, ajustes do terminal e baixas de vendas. `GET /api/stock/movements?shop_id=...` mostra as três fontes com `source` (`manual`, `terminal_adjustment`, `sale`). Uma unidade equivale a `1000` em `quantity_milli`/`delta_milli`; o caixa atual vende quantidades inteiras, inclusive para produtos marcados KG ou L. Venda fracionada ainda exige evolução do caixa e do contrato.

O estoque inicial passado a `local_pdv.upsert_product` fica apenas no caixa e **não emite evento**. Na implantação, use um único fluxo de carga: cadastre o mesmo produto por ID no SaaS, carregue o saldo inicial no caixa por `upsert_product` e lance **uma única entrada correspondente** no SaaS por `/api/stock/movements`. Não chame também `local_pdv.adjust_stock` para esse saldo inicial: ele emite `stock.adjusted`, que produziria uma segunda entrada central. Para recebimentos posteriores feitos no caixa, use `adjust_stock` e deixe a sincronização fazer a entrada central; não repita esses recebimentos manualmente no SaaS.

Sem saldo inicial central, a venda é recebida, mas pode gerar saldo negativo. O servidor preserva a venda concluída e expõe `stock_status=negative` na consulta de vendas, além do saldo negativo em `/api/stock`, para conferência; não altera ou descarta a venda para forçar o saldo a zero. Ajustes posteriores devem registrar motivo e responsável. Essa visibilidade é uma indicação técnica de reconciliação, ainda sem fila de tarefas ou alerta operacional.

Produto inexistente na empresa é erro: o lote não recebe ACK. Cadastre/corrija o produto com o mesmo ID e reenvie os eventos pendentes. Um produto arquivado continua reconhecido para receber vendas offline feitas antes do arquivamento. Isso não equivale a autorização fiscal; NFC-e, contingência e transmissão precisam de fluxo próprio.

## Leitura de vendas

- `GET /api/sales?limit=50&shop_id=...&cursor=...` devolve `sales` e `next_cursor`. O limite máximo é 100. Use o cursor retornado até que seja `null`.
- `GET /api/sales/{id}` devolve cabeçalho, itens e pagamentos, sempre limitado à empresa autenticada.
- `stock_status` mostra a condição do saldo central no momento da recepção. Um ajuste posterior não reescreve esse histórico.

## Limites conhecidos

Eventos `sale.completed` antigos que já tenham sido confirmados antes da criação das tabelas de vendas ficam apenas em `sync_events`; um reenvio os materializa, mas os que não forem reenviados requerem migração específica antes de produção. O servidor ainda não sincroniza catálogo e saldo central para o caixa, não resolve conflitos entre vários terminais, não emite NFC-e e não oferece fila de reconciliação para o operador. Essas etapas impedem uso comercial sem supervisão.
