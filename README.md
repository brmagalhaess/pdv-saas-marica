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

### Impressão automática no Docker Desktop (macOS)

A TM-T20 conectada por USB fica na fila CUPS do macOS, enquanto o terminal roda no contêiner. O terminal usa a ponte local autenticada `infra/print_bridge.py` para enviar o ESC/POS diretamente à fila, sem abrir “Salvar como PDF”. Depois de instalar a fila como `EPSON_TM_T20`, instale a ponte uma vez:

```sh
sh scripts/install-print-bridge-macos.sh
```

Ela mantém a ponte local disponível na porta `8020`, protegida pelo token criado em `.runtime/local.env`. Se a ponte não estiver instalada, o comprovante continua disponível na tela e pela fila de reimpressão.

O login é único em `/login/`. Contas que também possuem papel na plataforma veem **Administração SaaS** no menu do avatar do painel da empresa; não é necessário manter uma senha ou login diferente para trocar entre as áreas.
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
- **Modo offline:** o painel web em `8000` depende da nuvem; o caixa deve ser aberto em `http://127.0.0.1:8010/`. Depois do pareamento e de uma carga inicial de catálogo, login, produtos, caixa, vendas e recibo usam o SQLite local. A fila é enviada à nuvem quando a conexão volta. O painel mostra um atalho para o terminal local quando a nuvem não responde.
- Comprovante não fiscal ESC/POS 58/80 mm. No painel da empresa, Produtos → Imprimir etiquetas oferece prévia e quantidade em páginas de 50×30 mm para o driver de uma impressora de etiquetas com sensor de intervalo. O terminal imprime somente recibos de venda; a fila de etiquetas fica no painel da empresa. O antigo envio de etiquetas ESC/POS à fila de recibos foi desabilitado: a TM-T20 usa bobinas de recibo de 58/80 mm e não alinha etiquetas pré-cortadas de 50×30 mm. Hardware físico exige validação.
- Conectores Focus NFe e Asaas com credenciais criptografadas, fila e confirmação pelo provedor. Não há credenciais reais configuradas.
- Web responsiva e PWA instalável no Android/iPhone. A PWA é gestão web; o caixa offline exige o serviço local. O app nativo iOS de monitoramento do proprietário está em [ios/PDV2OwnerMonitor](ios/PDV2OwnerMonitor), com projeto Xcode compilável e painel somente leitura.

## Pareamento

1. Entre na empresa, abra **Assinatura** e copie a chave de licença da empresa. A chave identifica a assinatura e o plano define quantos terminais podem ser criados.
2. Em **Equipe**, cadastre cada operador e defina um PIN numérico separado da senha web. Em **Terminais**, crie o caixa e copie o código de pareamento de uso único, válido por 10 minutos.
3. Abra `http://127.0.0.1:8010/` no computador do caixa. Informe a chave de licença, o código temporário e o endereço do servidor: `http://cloud:8080` quando terminal e cloud estão no mesmo Compose local, ou o HTTPS real da nuvem na instalação da loja.
4. O vínculo empresa/loja/caixa fica gravado nesse computador. Depois, escolha o operador e entre com seu PIN; abra o caixa antes de vender. Novos operadores com PIN são sincronizados quando a nuvem estiver acessível.

A chave empresarial não autoriza um dispositivo sozinha: o servidor também confere o código temporário e emite uma licença assinada vinculada ao dispositivo. A chave pública fica fixada no pareamento. Revogações alcançam um terminal offline quando ele reconectar ou a licença vencer. O prazo da licença não é prazo de contingência fiscal.

Se um caixa foi conectado à empresa errada e ainda não contém operações nem backups, a opção **Corrigir conexão deste caixa** limpa os dados baixados e permite novo pareamento. Depois de registrar operações ou backups, preserve o banco local e use uma instalação ou diretório de dados separado para outra empresa.

Em uma loja, cada computador de caixa executa seu próprio terminal e pode usar a mesma porta local `8010`. A licença, o código de pareamento, o `device_id` e o banco SQLite identificam o caixa; a porta não participa dessa identificação. A porta `8011` existe somente para testar duas instâncias no mesmo Docker Desktop, onde dois processos não conseguem escutar a mesma porta do mesmo computador. Para esse teste, use o perfil `multi-terminal`; em computadores diferentes, não é necessário mudar a porta.

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

- [Relatório de auditoria completa e plano de evolução](RELATORIO-AUDITORIA-COMPLETA.md)
- [Checklist de teste e promoção](docs/RELEASE-CHECKLIST.md)
- [Estado e critérios de lançamento](docs/STATUS-V2.md)
- [Dokploy](docs/DEPLOY-DOKPLOY.md)
- [API](docs/API-V2.md)
- [Cobrança, fiscal e versões](docs/OPERACOES-V2.md)
- [Pesquisa de mercado e equipamentos](docs/PESQUISA-MERCADO-V2.md)
- [Meta comercial de R$ 1 milhão/ano](docs/PLANO-COMERCIAL.md)
- [Prompt mestre](docs/PROMPT-MESTRE.md)

`app.py`, `local_pdv/` e `compose.preview.yaml` são o protótipo anterior preservado, não a implantação V2.
