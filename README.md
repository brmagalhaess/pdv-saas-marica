# PDV2 — SaaS, gestão e terminal local

V2.0.0 para validação em mercados, mercearias e varejo de produtos. Django/PostgreSQL na nuvem e Flask/SQLite no terminal, com aplicações executáveis, containers e testes integrados. **A instalação local é demonstração; a liberação comercial depende de [STATUS-V2.md](docs/STATUS-V2.md).** PDV2 é nome provisório.

## Executar no Docker Desktop

```sh
sh scripts/start-local.sh
```

- Landing: http://127.0.0.1:8000/
- Administração SaaS: http://127.0.0.1:8000/platform/
- Gestão da empresa: http://127.0.0.1:8000/app/
- Terminal: http://127.0.0.1:8010/
- Credenciais únicas desta instalação: `.runtime/access.txt`.

O script gera segredos, migra o PostgreSQL e cria uma demonstração no Avançado. Cadastros públicos recebem avaliação do Essencial. A política de senha aceita 8 a 128 caracteres com maiúscula, minúscula, número e símbolo; a senha fornecida na conversa não foi gravada no código.

Se a prévia antiga ocupar a porta 8000, pare apenas `pdv-saas-phase1-pdv-preview-1`. O volume antigo é preservado; a V2 usa outro banco e não migra dados V1 automaticamente.

## Serviços

- **cloud:** Django/Gunicorn/WhiteNoise, API de empresa/plataforma, sessões, CSRF, permissões e licenças assinadas.
- **db:** PostgreSQL persistente.
- **worker:** fila no banco para cobrança, conciliação, fiscal e arquivo XML.
- **gateway:** Caddy local. Em Dokploy o HTTPS aponta ao cloud.
- **terminal:** SQLite WAL, PIN, catálogo, caixa, vendas, sincronização e spool.
- **backup:** pg_dump + Restic criptografado; localmente em outro volume, produção em destino externo.
- **migrate:** tarefa de schema anterior ao início da aplicação.

## Recursos implementados

- Plataforma: empresas, planos/limites, assinaturas, cobranças, terminais, equipe de suporte/cobrança/operação, auditoria, integrações e versões assinadas.
- Empresa: lojas, produtos/variações, categorias, clientes, fornecedores, estoque/entradas, contas a pagar/receber, equipe/PIN, vendas e auditoria.
- Papéis: proprietário, administrador, gerente, caixa, estoque, financeiro e consulta; permissões separadas da equipe SaaS.
- Terminal: código/nome, quantidade fracionária, desconto autorizado, pagamento dividido informado pelo operador, troco, abrir/fechar caixa, sangria/suprimento, suspensão, histórico, cancelamento e reimpressão.
- Offline com licença vinculada ao dispositivo, proteção contra retrocesso de relógio e fila persistente. Reenvio confirmado não duplica venda ou baixa de estoque.
- Comprovante não fiscal ESC/POS 58/80 mm; etiquetas ZPL/EPL via TCP, RAW Windows ou arquivo de diagnóstico. Hardware físico exige validação.
- Conectores Focus NFe e Asaas com credenciais criptografadas, fila e confirmação pelo provedor. Não há credenciais reais configuradas.
- Web responsiva e PWA instalável no Android/iPhone. A PWA é gestão web; o caixa offline exige o serviço local. Não há app nativo publicado nas lojas.

## Pareamento

1. Entre na empresa, configure o PIN na equipe e crie o terminal.
2. No terminal, informe o código e `http://cloud:8080` se ambos estiverem no Compose local. O código expira em 10 minutos.
3. Escolha operador/PIN e abra o caixa antes de vender.
4. No terminal nativo em loja, use o endereço HTTPS real da nuvem.

A chave pública fica fixada no pareamento. Revogações alcançam um terminal offline quando ele reconectar ou a licença vencer. O prazo da licença não é prazo de contingência fiscal.

## Testes

```sh
PDV_DEBUG=1 python cloud/manage.py test core operations -v 2
python -m unittest discover -s shared/tests -v
python -m unittest discover -s integrations/tests -v
python -m unittest discover -s terminal/tests -v
```

Na demonstração local já iniciada:

```sh
python scripts/smoke_local.py --offline
python scripts/verify_backup.py
```

O primeiro interrompe apenas o cloud deste projeto, vende offline, religa, sincroniza e estorna a venda de teste. O segundo restaura o backup em um banco temporário separado e o remove após verificar. Relatórios em `.runtime/`.

## Instalação e atualização

Use `compose.production.yaml` para a nuvem Dokploy e `compose.terminal.yaml` para um terminal Docker na loja. Windows/Python e periféricos: [TERMINAL-V2.md](docs/TERMINAL-V2.md).

`python scripts/package_terminal.py` gera ZIP sem segredos ou vendas. Hospede-o em HTTPS e cadastre versão, tamanho e SHA-256 na plataforma. O servidor assina o manifesto; o terminal avisa sobre versões verificadas. A atualização é manual. `scripts/verify_update.py` verifica o pacote baixado antes da instalação.

## Documentação

- [Estado e critérios de lançamento](docs/STATUS-V2.md)
- [Dokploy](docs/DEPLOY-DOKPLOY.md)
- [API](docs/API-V2.md)
- [Cobrança, fiscal e versões](docs/OPERACOES-V2.md)
- [Pesquisa de mercado e equipamentos](docs/PESQUISA-MERCADO-V2.md)
- [Meta comercial de R$ 1 milhão/ano](docs/PLANO-COMERCIAL.md)
- [Prompt mestre](docs/PROMPT-MESTRE.md)

`app.py`, `local_pdv/` e `compose.preview.yaml` são o protótipo anterior preservado, não a implantação V2.
