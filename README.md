# PDV2 — protótipo SaaS e caixa local

Projeto em desenvolvimento para mercados e mercearias, começando em Maricá. **PDV2 é nome provisório.** O código contém uma landing com planos, painel de gestão, catálogo, estoque, backup local e um núcleo de caixa offline com sincronização experimental. **Não está apto para atender clientes reais:** faltam NFC-e, impressão física, pagamentos integrados, segurança de produção, cobrança, mobile e homologação.

## Prévia no Docker Desktop

Na pasta do projeto:

```sh
docker compose -f compose.preview.yaml up -d --build
```

Abra `http://127.0.0.1:8000`. A prévia publica a porta apenas no computador local e guarda o banco em um volume Docker. Se a porta 8000 estiver ocupada, defina `PDV_PREVIEW_PORT=8001` antes do comando e abra `http://127.0.0.1:8001`.

Para parar sem apagar o volume:

```sh
docker compose -f compose.preview.yaml down
```

Também é possível executar sem Docker: `python3 app.py` (Python 3.9+). O banco padrão será `pdv.sqlite3` na pasta; altere com a variável `PDV_DB`. **Não abra `index.html` por `file://` para cadastrar contas**: o formulário precisa da API em `http://127.0.0.1:8000`.

## O que funciona

- Cadastro de empresas e loja principal; login, papéis e sessões.
- Catálogo de produtos, busca, preços em centavos e estoque por loja com movimentações auditáveis.
- Planos propostos na landing: R$ 149, R$ 229 e R$ 349/mês. A cobrança ainda não existe; contas novas ficam em teste no Essencial.
- Registro de um terminal e ingestão idempotente de eventos locais; vendas recebidas aparecem no painel. O saldo central **ainda não é conciliado** com essas vendas.
- [Caixa local experimental](local_pdv/README.md): abertura/fechamento, venda, pagamentos informados pelo operador, baixa local e outbox. Não emite NFC-e nem imprime em hardware.
- [Backup local](backup.py) consistente, com verificação de integridade e hash. Falta cópia externa criptografada.

## Testes

```sh
python3 -m unittest discover -s tests -v
python3 -m unittest discover -s local_pdv/tests -v
```

Os testes incluem isolamento entre empresas, permissões, regra de senha, estoque, deduplicação de eventos, venda atômica e recuperação do caixa após reinício e queda abrupta. O exemplo de senha dado pelo proprietário é aceito pela regra de 8 a 128 caracteres com maiúscula, minúscula, número e símbolo; ele **não foi gravado no código**.

## API principal

- `GET /health`, `GET /api/plans`
- `POST /api/register`, `POST /api/login`, `POST /api/logout`, `GET /api/me`
- `GET/POST /api/shops`, `GET/POST /api/users`, `GET /api/audit`
- `GET/POST /api/products`, `GET/PATCH/DELETE /api/products/{id}`
- `GET /api/stock?shop_id=...`, `GET/POST /api/stock/movements`
- `GET/POST /api/terminals` — proprietário registra um terminal e recebe um token uma vez
- `POST /api/sync/events` — autenticação Bearer do terminal, lote idempotente
- `GET /api/sales` — vendas recebidas do caixa local

Os endpoints foram criados para prototipagem local. A página pública e o servidor HTTP padrão **não devem ser expostos à internet** nesta versão.

## Documentos

- [Especificação e arquitetura inicial](docs/FASE-0.md)
- [Estado real e critérios para piloto](docs/STATUS.md)
- [Backlog](docs/BACKLOG.md)
- [Pesquisa de periféricos](docs/HARDWARE.md)
- [Preparação futura para Dokploy](docs/DEPLOY-DOKPLOY.md)

O logo provisório está em `logo-pdv2.svg` e é um SVG editável. Consulte marca e domínio antes de adotá-lo comercialmente.
