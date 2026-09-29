# Fase 0 — especificação inicial

## Decisões confirmadas

- Segmento dos primeiros pilotos: **mercados e mercearias em Maricá, RJ**.
- “bpk” significa **backup**. Backup é requisito do produto e da operação SaaS.
- Cliente típico: uma loja e um terminal. Servidor na loja é opcional.
- Primeira versão de caixa: Windows. Web e mobile servem à gestão.

## Perguntas que ainda dependem do proprietário

1. Quais três modelos de impressora térmica e dois de etiqueta serão comprados para testes físicos? Precisamos também dos modelos de balança, se os pilotos venderem por peso.
2. Qual é a configuração e capacidade da VPS, o sistema operacional, o domínio e o provedor de armazenamento externo para backups?
3. Qual provedor de cobrança recorrente será contratado? Quais formas de pagamento e política de inadimplência serão oferecidas?
4. Quem será o contador ou especialista fiscal responsável pela revisão e homologação de NFC-e no RJ?
5. Quais três lojas aceitam ser pilotos e quais dados precisam ser migrados?
6. Qual horário de suporte será prometido e quem atenderá incidentes fora desse horário?

Essas respostas não impedem a base SaaS. Impedem declarar o produto pronto para piloto fiscal ou produção.

## Recorte da primeira versão vendável

Mercado/mercearia de uma loja e um caixa. Cadastro/importação de produtos, EAN, unidade e venda por peso quando validada a balança; preço, estoque, venda, caixa, pagamentos, NFC-e RJ, impressão térmica, etiquetas, backup, painel de vendas e suporte. TEF e integração de balança ficam condicionados ao equipamento dos pilotos. Aplicativo móvel inicial: consulta e alertas do dono.

## Arquitetura recomendada

```text
Painel web / app móvel -> API SaaS -> PostgreSQL gerenciado ou isolado
                               -> fila de sincronização/eventos
                               -> armazenamento externo criptografado de backups/XML

PDV Windows -> SQLite local + outbox -> API SaaS
            -> impressora / etiqueta / periféricos
            -> módulo fiscal RJ
```

Começar com um backend modular e um banco central; separar serviços só quando volume ou responsabilidade exigirem. O código executável atual usa Python stdlib e SQLite para validar fluxos sem dependências. Antes de produção, migrar o banco central para PostgreSQL, adicionar migrações versionadas, servidor HTTP de produção e infraestrutura de segurança/observabilidade. SQLite permanece candidato para o banco local do caixa.

## Modelo de dados inicial

Implementado: `plans`, `tenants`, `shops`, `users`, `subscriptions`, `sessions`, `audit_events`.

Próximas entidades: `terminals`, `products`, `product_barcodes`, `prices`, `tax_profiles`, `inventory_movements`, `cash_sessions`, `sales`, `sale_items`, `payments`, `fiscal_documents`, `sync_events`, `print_jobs`, `backup_jobs`, `support_cases`. Todas as entidades de negócio devem identificar empresa e, quando aplicável, loja e terminal. Vendas e eventos terão IDs globais e chaves de idempotência.

## Fluxos críticos

**Venda conectada:** operador lê produtos; PDV calcula total com dados locais; confirma pagamento; grava venda e evento em transação local; inicia emissão fiscal; imprime documento com estado correto; sincroniza; painel recebe atualização.

**Sem internet:** PDV usa catálogo local; grava venda e evento; aplica procedimento fiscal de contingência permitido no RJ; imprime o documento de contingência; ao reconectar, transmite pendências, trata rejeições e reconcilia o servidor. Não mostra “autorizado” antes da resposta fiscal.

**Falha no meio:** após reinício, PDV lê outbox e estado fiscal, retoma envios com mesma chave de idempotência e permite reimpressão sem criar outra venda. Erros que não podem ser resolvidos automaticamente entram em fila de suporte/contador.

**Backup:** banco central com cópia consistente e armazenamento externo criptografado; arquivos fiscais incluídos; retenção configurável; restauração em ambiente separado testada periodicamente. Banco local do terminal com backup antes de atualização e mecanismo de recuperação. Backup atual é somente uma ferramenta local inicial.

## Critérios de aceite da primeira loja piloto

- Um operador completa venda, pagamento, emissão fiscal e impressão no equipamento escolhido.
- Venda persiste após queda de energia; reenvio não duplica venda ou documento.
- Contingência e recuperação fiscal são validadas por especialista.
- Dados de duas empresas permanecem isolados em testes.
- Backup externo é restaurado em ambiente separado.
- Suporte consegue identificar versão, últimas sincronizações e falhas.
- Implantação, treinamento e retorno ao sistema anterior têm roteiro escrito.

## Cronograma estimado, sujeito a equipe e homologação

- Semanas 1–2: descoberta de campo, arquitetura, protótipo e base SaaS.
- Semanas 3–8: caixa local, catálogo, venda, estoque, impressão e sincronização.
- Semanas 9–14: fiscal RJ, testes de hardware, falhas e backup.
- Semanas 15–18: três pilotos supervisionados e correções.
- Após pilotos: cobrança, atualização gradual, suporte e mobile; expansão só com métricas de retenção e incidentes.

Uma pessoa trabalhando sozinha provavelmente precisará de mais tempo. O prazo fiscal depende de documentação vigente, credenciamento e testes com clientes reais.

## Principais riscos e validação

- **Fiscal:** validar com SEFAZ e especialista antes de produção.
- **Hardware:** comprar e testar modelos específicos; registrar versões de driver/firmware.
- **Sincronização:** testar perda de energia e internet em cada estado da venda.
- **Suporte:** medir incidentes por loja e tempo de resolução nos pilotos.
- **VPS:** medir uso, testar backup fora da VPS e restaurar antes de aceitar clientes pagantes.
