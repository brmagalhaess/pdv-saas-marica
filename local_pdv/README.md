# Caixa local — incremento funcional

Este pacote Python usa SQLite para demonstrar **um terminal de caixa sem internet**. Produto, abertura de caixa, venda, pagamentos registrados pelo operador, baixa de estoque e evento pendente de sincronização são operações locais. A venda, suas linhas, pagamentos, baixa e evento são confirmados na **mesma transação**. Ao reiniciar, o estado já confirmado continua no arquivo.

## Experimente

Execute os comandos a partir da pasta `pdv-saas-phase1` com Python 3.9+:

```sh
python3 -m local_pdv --db ./caixa-demo.sqlite3 init --tenant empresa-demo --shop loja-1 --terminal caixa-1
python3 -m local_pdv --db ./caixa-demo.sqlite3 product --id arroz-1kg --name "Arroz 1 kg" --price 699 --stock 20 --barcode 7890000000001
python3 -m local_pdv --db ./caixa-demo.sqlite3 lookup --code 7890000000001
python3 -m local_pdv --db ./caixa-demo.sqlite3 open --opening 10000
python3 -m local_pdv --db ./caixa-demo.sqlite3 sell --request venda-0001 --item arroz-1kg:2 --payment cash:1500
python3 -m local_pdv --db ./caixa-demo.sqlite3 status
python3 -m local_pdv --db ./caixa-demo.sqlite3 outbox
python3 -m local_pdv --db ./caixa-demo.sqlite3 close --counted 11400
```

Valores monetários estão em **centavos**. No exemplo, 2 itens custam R$ 13,98, o cliente paga R$ 15,00 e recebe R$ 1,02 de troco. O dinheiro esperado no caixa é R$ 113,98 (R$ 100,00 de abertura + R$ 13,98 da venda); um fechamento de `11400` apresenta diferença de R$ 0,02. Repetir a venda com o mesmo `--request` e dados idênticos retorna a venda original sem nova baixa. A mesma chave com outros dados é recusada. **Os IDs deste exemplo são fictícios:** o fluxo demonstra apenas o caixa local e não sincroniza com o SaaS sem o provisionamento descrito abaixo.

Para rodar os testes:

```sh
python3 -m unittest discover -s local_pdv/tests -v
```

## Sincronização experimental

O SaaS recebe eventos de um terminal registrado e os deduplica por `event_id`. Para usar, crie a empresa e faça login no painel; registre o terminal pelo endpoint `POST /api/terminals` com o `shop_id` da conta. O token retornado aparece **somente nessa resposta**. O `tenant_id`, `shop_id` e `terminal_id` informados no comando `init` do caixa precisam corresponder exatamente aos IDs do servidor. Crie também o produto por `POST /api/products` e use o **ID retornado pelo SaaS** como `--id` ao carregar o produto no caixa. Para estoque central positivo, registre a entrada inicial por `POST /api/stock/movements` e carregue a mesma quantidade no SQLite local. Uma venda de produto inexistente na empresa do SaaS será recusada; o produto `arroz-1kg` do exemplo isolado não foi criado na nuvem.

Depois, no terminal de desenvolvimento:

```sh
export PDV_TERMINAL_TOKEN='token_recebido_no_registro'
python3 -m local_pdv.sync --db ./caixa-demo.sqlite3 --url http://127.0.0.1:8000
```

O cliente confirma eventos locais somente após receber a lista completa de IDs aceitos pelo servidor. HTTPS é exigido para destinos remotos; HTTP só é aceito em localhost. Erros de conexão ou respostas inválidas mantêm eventos pendentes. Ainda não há serviço automático, renovação/revogação de token pela interface, catálogo baixado do SaaS nem conciliação operacional de estoque central. **Não usar esse fluxo em produção.**

O teste integrado executa o protocolo HTTP entre o cliente e a API real usando `socket.socketpair()`, sem abrir uma porta TCP. Ele cria empresa, produto, estoque e terminal no SaaS, vende no SQLite local, simula perda da resposta após o servidor confirmar e reenvia depois do reinício:

```sh
python3 -m unittest discover -s local_pdv/tests -p 'test_sync_http.py' -v
```

Esse teste usa apenas a biblioteca padrão e não precisa de servidor externo. O teste de produção em uma VPS, com TLS, proxy e dispositivos reais, ainda é necessário.

## Contrato de eventos

`LocalPDV.pending_events()` devolve eventos persistidos com `event_id`, `type`, `aggregate_id`, `identity` (empresa, loja, terminal) e `data`. O SaaS valida a identidade do terminal, guarda **cada `event_id` uma única vez** e confirma após sua própria transação de banco. Só então o cliente chama `acknowledge_event(event_id)`. Sem confirmação, o evento permanece pendente e pode ser reenviado após reinício. Um sincronizador contínuo e reconciliação de dados ainda precisam ser implementados.

## Limites antes de um piloto real

- Não emite NFC-e, documento fiscal ou DANFE. A venda local é somente um registro operacional.
- `pix` e `card` registram a forma informada pelo operador; não capturam nem confirmam pagamento, não integram TEF ou adquirente.
- Não imprime em hardware. A saída JSON serve para inspeção e não é comprovante fiscal.
- Quantidades são unidades inteiras. Mercados precisam de produtos por peso, integração com balanças, códigos de barras de pesáveis, devoluções, descontos, suprimento e sangria antes de uso comercial.
- Não implementa usuários, permissões, criptografia do banco, backup automático nem licença. Proteja o arquivo SQLite e faça cópias consistentes; nunca edite ou apague eventos pendentes manualmente.
- O catálogo é carregado localmente para demonstração; sua atualização e conciliação com o SaaS ainda precisam de contrato e regras de conflito. `upsert_product` preserva o estoque já existente quando atualiza um produto.
- Cada banco é vinculado a uma empresa, loja e terminal. Não compartilhe o mesmo arquivo SQLite entre caixas por rede.
