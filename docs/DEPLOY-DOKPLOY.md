# Nuvem PDV2 no Dokploy

O projeto está preparado para Docker Compose. Nenhuma VPS foi acessada nesta entrega. Use `compose.production.yaml`, não `compose.local.yaml` nem `compose.preview.yaml`.

## Preparar a instalação

1. Aponte o DNS de `pdv.crowntech.com.br` para a VPS e configure esse domínio com HTTPS no Dokploy.
2. Crie projeto Compose a partir do repositório privado e selecione `compose.production.yaml`.
3. Gere valores exclusivos para as variáveis de `infra/production.env.example` e mantenha `PDV_DOMAIN=pdv.crowntech.com.br` (o valor alimenta `PDV_ALLOWED_HOSTS` e `PDV_CSRF_TRUSTED_ORIGINS`). O segredo Django precisa de ao menos 50 caracteres aleatórios. Use senha de banco URL-safe; a URL de conexão exige codificação se usar caracteres especiais reservados.
4. Gere um par Ed25519 exclusivo para licenças com `shared.licensing.generate_keypair()`. Armazene as chaves fora do repositório. Configure `PDV_LICENSE_PRIVATE_KEY_PATH` e `PDV_LICENSE_PUBLIC_KEY_PATH` com caminhos absolutos na VPS. O arquivo montado deve ser legível pelo UID 10001 no container; proteja o diretório pai no host. Docker Compose com secret baseado em arquivo preserva permissões do arquivo no host.
   Gere também um **segundo par Ed25519, só para versões** (atualização automática do terminal) e configure `PDV_RELEASE_PRIVATE_KEY_PATH` e `PDV_RELEASE_PUBLIC_KEY_PATH`. O compose de produção não sobe sem eles. Esse par é separado do de licenças de propósito: quem assina uma licença não consegue assinar um programa. A chave pública de versões fica fixada em cada terminal no pareamento; trocar o par depois exige reparear ou atualizar manualmente os terminais, então guarde a chave privada com o mesmo cuidado da de licenças.
5. Gere `PDV_ENCRYPTION_KEY` com `Fernet.generate_key()`; ela protege tokens dos provedores no banco. Guarde cópias seguras da chave e dos demais segredos separadas do banco.
6. Configure Restic em destino externo, senha do repositório e credenciais de armazenamento. `PDV_BACKUP_OFFSITE=1` declara a configuração esperada; confira a restauração de verdade antes de atender clientes.
7. Associe `pdv.crowntech.com.br` ao serviço `cloud`, porta interna **8080**, usando o painel de domínios do Dokploy e faça deploy. Não publique PostgreSQL ou worker. O proxy precisa encaminhar HTTPS corretamente; `PDV_TRUST_PROXY=1` só deve existir atrás do proxy confiável.

`migrate` termina antes de iniciar cloud. `worker` só inicia depois do healthcheck. A tarefa de backup roda a cada hora. O banco usa volume persistente. Não use `down -v` em uma instalação com dados.

Depois do deploy, confira `https://pdv.crowntech.com.br/health/` (deve responder `{"status": "ok"}`) e o login em `https://pdv.crowntech.com.br/login/`.

### Variáveis opcionais

| Variável | Padrão em produção | Para que serve |
| --- | --- | --- |
| `PDV_ENABLE_DJANGO_ADMIN` | `0` | Publica o Django admin em `/control/`. Veja a seção própria abaixo. |
| `PDV_LOG_LEVEL` | `INFO` | Nível dos logs de cloud e worker (`DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`). |
| `BACKUP_PRUNE` | `0` | Com `1`, cada backup aplica a retenção (7 diários, 4 semanais, 12 mensais) e libera espaço no repositório. Recomendado. |
| `PDV_PLATFORM_MFA_REQUIRED` | `1` | Exige MFA da equipe SaaS. Não desligue em produção. |

O compose de produção só entrega aos containers as variáveis listadas no bloco `x-env` de `compose.production.yaml`; uma variável definida apenas no painel do Dokploy e ausente desse bloco não tem efeito.

## Criar o primeiro administrador

No terminal do serviço cloud:

```sh
python manage.py createsuperuser
```

Use e-mail também como username, pois o login web usa o endereço de e-mail. O bootstrap de demonstração está bloqueado por padrão em produção. Cadastre a equipe SaaS no painel; reserve superusuário para administração restrita.

No primeiro acesso em `https://pdv.crowntech.com.br/login/` o superusuário cadastra a MFA (TOTP) e recebe os códigos de recuperação. Guarde-os fora da VPS.

## Django admin (`/control/`)

O Django admin fica **desligado em produção**: sem `PDV_ENABLE_DJANGO_ADMIN=1` as rotas `/control/` nem existem (resposta 404). O trabalho do dia a dia (empresas, planos, assinaturas, terminais, equipe) é feito em `/platform/`, que valida as regras de negócio e registra auditoria.

Ligue o admin só quando precisar inspecionar dados brutos, e desligue em seguida:

1. Defina `PDV_ENABLE_DJANGO_ADMIN=1` nas variáveis de ambiente do projeto no Dokploy e refaça o deploy. O `compose.production.yaml` repassa a variável aos serviços `cloud`, `worker` e `migrate`; sem ela o padrão é `0`.
2. Entre por `https://pdv.crowntech.com.br/login/` com um superusuário e conclua a MFA. `/control/login/` não autentica ninguém: apenas redireciona para `/login/`, onde valem o limite de tentativas e o TOTP.
3. Abra `https://pdv.crowntech.com.br/control/`. O acesso exige superusuário **e** sessão com MFA verificada; equipe SaaS sem superusuário e usuários de empresas são recusados.

O que o admin permite:

- **Editar:** apenas Plano e Empresa, com as mesmas validações da API (lista de recursos do plano, limites que não podem ficar abaixo do uso atual, teto de desconto de 0 a 10000) e registro em auditoria. Não há exclusão, e o código (slug) do plano não muda depois de criado.
- **Somente leitura:** assinaturas, faturas, financeiro/fiado, vínculos de usuários, terminais, vendas, documentos fiscais, estoque e cadastros das empresas. Alterações nesses dados passam pelas rotas `/api/v1/platform/*` ou pelo painel da própria empresa.
- **Não aparecem:** usuários e grupos do Django, MFA e credenciais de integrações.

Se possível, restrinja `/control/` por IP ou VPN no proxy do Dokploy enquanto estiver ligado.

## Cobrança, fiscal e e-mail

Configure SMTP nas variáveis indicadas. As credenciais Asaas são globais da plataforma e devem ser inseridas no painel Serviços. Focus NFe é configurado em cada empresa, inicialmente em homologação. Nunca copie tokens para Git.

Cadastre webhook Asaas HTTPS em `https://pdv.crowntech.com.br/api/v1/ops/webhooks/asaas`, com o mesmo token aleatório (32–255 caracteres) de `PDV_ASAAS_WEBHOOK_TOKEN`. Teste recebimento, reenvio e reconciliação no sandbox. Uma assinatura criada só libera acesso após cobrança confirmada pelo estado consultado no provedor.

Emissão Focus em ambiente production só é habilitada com `PDV_ALLOW_LIVE_FISCAL=1`, depois da homologação. Essa variável não conclui a configuração fiscal da empresa nem cria um motor tributário.

## Backup e alertas de falha

O serviço `backup` executa, a cada hora: `pg_dump`, validação do dump (`pg_restore --list`), envio ao repositório Restic, retenção (quando `BACKUP_PRUNE=1`) e listagem dos snapshots. O horário de sucesso só é gravado quando **todas** as etapas terminam sem erro. Se qualquer uma falhar, o painel Plataforma > Backups passa a mostrar estado de erro com a etapa que falhou (por exemplo, "Falha ao gerar o dump do PostgreSQL (pg_dump)"), e a mensagem também sai no log do serviço `backup`.

- Estado `healthy`: última cópia dentro de duas vezes o intervalo. `stale`: nenhuma cópia nova nesse prazo. `error`: a última execução falhou.
- `pdv2-backup once` (usado por `scripts/verify_backup.py`) termina com código diferente de zero quando o backup falha.
- Valide a restauração no destino externo antes de atender clientes e depois de trocar credenciais ou bucket:

```sh
python3 scripts/verify_backup.py --compose compose.production.yaml --env-file .env.production --require-external
```

## Logs e diagnóstico

Cloud e worker escrevem os logs na saída padrão (coletada pelo Docker/Dokploy) no formato `data nível logger [id-da-requisição] mensagem`. Erros internos (HTTP 500) e falhas de tarefas do worker saem com o traceback completo.

- Toda resposta HTTP traz o cabeçalho `X-Request-ID`; o mesmo identificador aparece nas linhas de log daquela requisição. Peça esse valor ao reportar um erro.
- Tarefa de cobrança/fiscal em `needs_review`: procure `Job <id>` no log do serviço `worker` para ver a causa real; o painel mostra apenas a mensagem resumida.
- Respostas 4xx comuns não geram linha própria; elas já constam no log de acesso do gunicorn. Falhas de CSRF e de host não permitido são registradas como aviso.

## Dependências

`requirements.txt` (imagens cloud e terminal) e `terminal/requirements.txt` (instalador Windows) são fixados por versão. O CI roda `scripts/audit_dependencies.py` (pip-audit): o build falha quando existe vulnerabilidade conhecida com versão corrigida publicada e apenas avisa quando ainda não há correção. Ao subir uma versão, reconstrua e reimplante as imagens e atualize os terminais instalados.

## Atualização e recuperação

- Versione o deploy em Git. Leia migrações antes de atualizar e gere snapshot/backup primeiro.
- Faça upgrade em homologação, execute testes e verifique compatibilidade dos terminais.
- Mantenha a imagem anterior e o backup. Reverter a imagem não reverte automaticamente o schema ou os dados.
- Faça restore em banco novo para testar; troque conexão somente após conferir os registros. Nunca restaure por cima do banco ativo como teste.
- Monitore saúde do cloud/worker, fila `needs_review`, idade do último backup, espaço em disco e erros de sincronização.
- Faça guarda externa da chave de criptografia de tokens, chave de licença, chave de versões, segredo de sessão e senha Restic. Sem esses segredos, restaurar apenas o SQL é insuficiente.

## Terminal na loja

O terminal fica na loja, separado da nuvem. No Windows, use o instalador descrito em [INSTALADOR-WINDOWS.md](INSTALADOR-WINDOWS.md) (serviço, atalho na área de trabalho e atualização automática); em máquina com Docker, `compose.terminal.yaml`. Pareie a `https://pdv.crowntech.com.br`. Para impressora USB Windows, prefira instalação nativa com driver RAW; container Linux não recebe automaticamente o spool do Windows.

Cada terminal possui banco próprio. O cenário de servidor local compartilhado por várias estações, com replicação local entre caixas, ainda não foi implementado. Os terminais independentes continuam vendendo offline dentro da licença (30 dias por padrão, campo `offline_days` do plano) e reconciliam na nuvem quando a conexão retorna. O desenho do servidor da loja com estações está em [ARQUITETURA-OFFLINE-LOCAL.md](ARQUITETURA-OFFLINE-LOCAL.md).

## Antes de liberar

Confira [STATUS-V2.md](STATUS-V2.md). A configuração de deploy existe, mas ambiente fiscal, pagamentos, hardware, carga, monitoramento e processos de suporte precisam de validação no ambiente final.

Referências: [Dokploy Compose](https://docs.dokploy.com/docs/core/docker-compose), [checklist Django](https://docs.djangoproject.com/en/5.2/howto/deployment/checklist/), [Restic restore](https://restic.readthedocs.io/en/stable/050_restore.html).

Para identificar corretamente clientes nos limites de tentativas, defina `PDV_TRUSTED_PROXY_CIDRS` como o CIDR real da rede interna do proxy. Cabeçalhos X-Forwarded-For de conexões fora dessa rede são ignorados. Não configure `0.0.0.0/0`. A verificação `check --deploy` pode avisar sobre HSTS preload ainda desativado; só solicite inclusão na lista de navegadores após definir domínio e política de subdomínios.
