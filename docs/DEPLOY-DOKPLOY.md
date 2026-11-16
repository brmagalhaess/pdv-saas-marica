# Nuvem PDV2 no Dokploy

O projeto está preparado para Docker Compose. Nenhuma VPS foi acessada nesta entrega. Use `compose.production.yaml`, não `compose.local.yaml` nem `compose.preview.yaml`.

## Preparar a instalação

1. Aponte o DNS de `pdv2.crowntech.com.br` para a VPS e configure esse domínio com HTTPS no Dokploy.
2. Crie projeto Compose a partir do repositório privado e selecione `compose.production.yaml`.
3. Na aba **Environment** cole apenas as variáveis **não secretas** de `infra/production.env.example`. O mínimo é `PDV_DOMAIN=pdv2.crowntech.com.br` (alimenta `PDV_ALLOWED_HOSTS` e `PDV_CSRF_TRUSTED_ORIGINS`) e `PDV_TRUSTED_PROXY_CIDRS`; são as duas únicas variáveis obrigatórias. **Nenhum segredo é digitado no painel**: veja [Segredos do servidor](#segredos-do-servidor).
4. Backup: `RESTIC_REPOSITORY` tem como padrão provisório `/status/restic`, um repositório Restic local no volume `backup_status` (mesma VPS). Ele protege contra erro humano e banco corrompido, **não** contra perda da VPS. Quando houver destino externo (S3/B2/compatível), troque `RESTIC_REPOSITORY`, preencha `AWS_*` e só então defina `PDV_BACKUP_OFFSITE=1`, que é o que faz o painel declarar backup externo (padrão `0`).
5. Associe `pdv2.crowntech.com.br` ao serviço `cloud`, porta interna **8080**, usando o painel de domínios do Dokploy e faça deploy. Não publique PostgreSQL ou worker. O proxy precisa encaminhar HTTPS corretamente; `PDV_TRUST_PROXY=1` só deve existir atrás do proxy confiável.
6. Logo depois do primeiro deploy, **gere e guarde o kit de recuperação** (abaixo). Sem ele, perder a VPS significa perder a chave das credenciais, as chaves de licença e de versões e a senha que abre os backups.

Ordem de subida: `secrets-init` (gera o que falta e termina) → `db` → `migrate` → `cloud` → `worker`; `backup` sobe depois de `secrets-init` e do banco saudável. A tarefa de backup roda a cada hora. Banco e segredos usam volumes persistentes. **Nunca use `down -v`** (nem "apagar volumes" no Dokploy) numa instalação com dados: isso apaga o banco e o volume `pdv2_secrets`.

Depois do deploy, confira `https://pdv2.crowntech.com.br/health/` (deve responder `{"status": "ok"}`) e o login em `https://pdv2.crowntech.com.br/login/`.

### Variáveis opcionais

| Variável | Padrão em produção | Para que serve |
| --- | --- | --- |
| `PDV_ENABLE_DJANGO_ADMIN` | `0` | Publica o Django admin em `/control/`. Veja a seção própria abaixo. |
| `PDV_LOG_LEVEL` | `INFO` | Nível dos logs de cloud e worker (`DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`). |
| `RESTIC_REPOSITORY` | `/status/restic` | Repositório Restic. O padrão é local (mesma VPS); troque por um destino externo assim que existir. |
| `PDV_BACKUP_OFFSITE` | `0` | Com `1`, o painel declara backup externo. Só ligue quando `RESTIC_REPOSITORY` apontar para fora da VPS. |
| `BACKUP_PRUNE` | `0` | Com `1`, cada backup aplica a retenção (7 diários, 4 semanais, 12 mensais) e libera espaço no repositório. Recomendado. |
| `PDV_PLATFORM_MFA_REQUIRED` | `1` | Exige MFA da equipe SaaS. Não desligue em produção. |

O compose de produção só entrega aos containers as variáveis listadas no bloco `x-env` de `compose.production.yaml`; uma variável definida apenas no painel do Dokploy e ausente desse bloco não tem efeito.

## Segredos do servidor

### O que o servidor gera e onde fica

A cada deploy o serviço `secrets-init` (mesma imagem da nuvem, comando `python -m operations.secrets_init`) confere o volume `pdv2_secrets`, montado em `/secrets`, e **cria só o que ainda não existe**. Nada é sobrescrito: do segundo deploy em diante ele apenas confirma ("mantido") e termina. A escrita é atômica (arquivo temporário e troca de nome sem sobrescrever) e o log só mostra nomes de arquivos, nunca valores.

| Arquivo | O que é | Quem lê |
| --- | --- | --- |
| `django_secret_key` | Segredo de sessão/CSRF do Django (86 caracteres) | cloud, worker, migrate (`PDV_SECRET_KEY_FILE`) |
| `db_password` | Senha do PostgreSQL (URL-safe) | db (`POSTGRES_PASSWORD_FILE`), cloud/worker/migrate (`PDV_DATABASE_PASSWORD_FILE`), backup (`PGPASSWORD_FILE`) |
| `encryption_key` | Chave Fernet que cifra tokens de integrações, segredos de MFA e chaves de cópia das empresas | cloud, worker, migrate (`PDV_ENCRYPTION_KEY_FILE`) |
| `restic_password` | Senha que cifra o repositório de backup | backup (`RESTIC_PASSWORD_FILE`) |
| `license-private.pem` / `license-public.pem` | Par Ed25519 das licenças offline | cloud, worker, migrate |
| `release-private.pem` / `release-public.pem` | Par Ed25519 das versões do terminal (separado do de licenças) | cloud, worker, migrate |

Permissões: `secrets-init` roda **sem root**, como `10001:70` (o usuário `app` da imagem da nuvem e o grupo `postgres` da imagem `postgres:17-alpine`), sem nenhuma capability e com sistema de arquivos somente leitura. Tudo pertence a `10001:70`; o diretório é `0750`, os arquivos `0400`, e só `db_password` é `0440` para o container do banco ler pelo grupo. Nenhum outro UID lê nada. O serviço `backup` roda como root e lê `/secrets` montado somente leitura. Os demais serviços também montam o volume somente leitura.

Variáveis antigas: se o painel ainda tiver `PDV_SECRET_KEY`, `POSTGRES_PASSWORD`, `PDV_ENCRYPTION_KEY` ou `RESTIC_PASSWORD`, o `secrets-init` cria o arquivo que falta **com esse mesmo valor** (instalação existente continua com as mesmas chaves). Se a variável existir e for diferente do arquivo, o deploy para com uma mensagem clara, em vez de trocar de chave em silêncio. Depois que o deploy der certo, retire essas variáveis do painel.

### Kit de recuperação (faça agora)

No Dokploy, abra o terminal do serviço **cloud** e rode:

```sh
python -m operations.secrets_init --kit
```

O comando imprime na tela um script `sh` com todos os segredos. Copie o texto inteiro para um cofre **fora do servidor** (gerenciador de senhas, por exemplo) e não o guarde no repositório nem em e-mail. Ele só imprime num terminal interativo: redirecionado para arquivo ou log, recusa (há `--permitir-redirecionamento` para quem quer gravar num arquivo de propósito). Refaça o kit sempre que trocar uma chave.

O backup Restic também leva o diretório `/secrets` (snapshot próprio, tag `pdv2-secrets`), cifrado junto com o dump do banco. Mas **sem a `restic_password` não se restaura nada**, nem o banco nem os segredos: é por isso que o kit fora do servidor é indispensável.

### Restaurar em outra VPS

1. No Dokploy da VPS nova, crie o projeto Compose como no início, **sem fazer deploy ainda**. Anote o nome interno do projeto (o volume será `<nome-do-projeto>_pdv2_secrets`; depois do deploy confira com `docker volume ls | grep pdv2_secrets`).
2. No shell da VPS, crie o volume e coloque os arquivos de volta com o kit salvo em `kit.sh`:

   ```sh
   docker volume create <nome-do-projeto>_pdv2_secrets
   docker run --rm -i -v <nome-do-projeto>_pdv2_secrets:/secrets alpine:3.20 sh < kit.sh
   ```

   O script grava cada arquivo, ajusta dono `10001:70` e modos. Arquivo já existente e diferente é recusado; `SUBSTITUIR=1` (variável no `docker run -e`) troca de propósito.
3. Faça o deploy. O log de `secrets-init` deve mostrar "mantido" para os oito arquivos.
4. Restaure o banco a partir do Restic (mesmo `RESTIC_REPOSITORY` externo; a senha já está em `/secrets/restic_password`), no terminal do serviço `backup`:

   ```sh
   export PGPASSWORD="$(cat /secrets/db_password)"
   restic snapshots --tag pdv2-database
   restic restore latest --tag pdv2-database --target /tmp/r
   pg_restore --clean --if-exists --no-owner --dbname=pdv2 "$(find /tmp/r -name pdv2.dump)"
   ```

   Se o kit se perdeu mas a `restic_password` não, os outros segredos saem do snapshot `pdv2-secrets` (`restic restore latest --tag pdv2-secrets --target /tmp/s`).

Se o deploy já rodou antes de restaurar o kit, o servidor gerou segredos novos e o banco vazio foi criado com a senha nova. O caminho simples é apagar os volumes `pdv2_secrets` e `postgres_data` **dessa instalação nova e vazia**, recolocar o kit (passo 2) e refazer o deploy.

Instalação que já existia com chaves em `/srv/pdv2/secrets` (modelo antigo de `secrets:`): antes do primeiro deploy com este compose, copie os PEM para o volume e mantenha as quatro variáveis antigas no painel nesse deploy:

```sh
docker volume create <nome-do-projeto>_pdv2_secrets
docker run --rm -v <nome-do-projeto>_pdv2_secrets:/secrets -v /srv/pdv2/secrets:/old:ro alpine:3.20 sh -c \
  'cp /old/license-private.pem /old/license-public.pem /old/release-private.pem /old/release-public.pem /secrets/ \
   && chown -R 10001:70 /secrets && chmod 750 /secrets && chmod 400 /secrets/*.pem'
```

### Trocar uma chave e o impacto

Regra geral: com o arquivo apagado do volume, o próximo deploy gera um novo. Para editar o volume use o shell da VPS (`docker run --rm -it -v <nome-do-projeto>_pdv2_secrets:/secrets alpine:3.20 sh`), porque os serviços o montam somente leitura. Depois de qualquer troca, gere um kit novo.

- `django_secret_key`: apague e refaça o deploy. Todos saem da sessão e links de redefinição de senha pendentes deixam de valer. Sem outro efeito.
- `db_password`: o PostgreSQL não lê a senha de novo depois de criado. Primeiro mude no banco (`psql -U pdv2 -d pdv2 -c "ALTER ROLE pdv2 PASSWORD '<nova>'"` no terminal do `db`), depois grave o mesmo valor em `db_password` (modo `0440`) e refaça o deploy.
- `restic_password`: no terminal do `backup`, `restic key add` com a senha nova, troque o arquivo e, depois de conferir `restic snapshots`, `restic key remove` da antiga. Kits antigos deixam de abrir o repositório.
- `encryption_key`: **não troque** sem necessidade. Não há recifragem automática: tokens de integrações (Asaas, Focus) precisam ser cadastrados de novo, a equipe refaz a MFA e as chaves de cópia de segurança das empresas ficam indisponíveis até serem recriadas.
- `license-*.pem` (apague os dois): cada terminal também fixou a chave pública de licenças no pareamento e recusa renovar a licença com a chave nova ("Chave pública não corresponde à chave fixada"). Os terminais seguem vendendo só até o fim da licença offline atual e precisam ser reparados. Só troque se a chave privada vazou.
- `release-*.pem` (apague os dois): **quebra a atualização automática de todos os terminais pareados**, porque cada um fixou a chave pública de versões no pareamento e passa a recusar manifestos assinados pela chave nova. Exige reparear ou atualizar cada terminal manualmente. Só troque se a chave privada vazou.

## Criar o primeiro administrador

No terminal do serviço cloud:

```sh
python manage.py createsuperuser
```

Use e-mail também como username, pois o login web usa o endereço de e-mail. O bootstrap de demonstração está bloqueado por padrão em produção. Cadastre a equipe SaaS no painel; reserve superusuário para administração restrita.

No primeiro acesso em `https://pdv2.crowntech.com.br/login/` o superusuário cadastra a MFA (TOTP) e recebe os códigos de recuperação. Guarde-os fora da VPS.

## Django admin (`/control/`)

O Django admin fica **desligado em produção**: sem `PDV_ENABLE_DJANGO_ADMIN=1` as rotas `/control/` nem existem (resposta 404). O trabalho do dia a dia (empresas, planos, assinaturas, terminais, equipe) é feito em `/platform/`, que valida as regras de negócio e registra auditoria.

Ligue o admin só quando precisar inspecionar dados brutos, e desligue em seguida:

1. Defina `PDV_ENABLE_DJANGO_ADMIN=1` nas variáveis de ambiente do projeto no Dokploy e refaça o deploy. O `compose.production.yaml` repassa a variável aos serviços `cloud`, `worker` e `migrate`; sem ela o padrão é `0`.
2. Entre por `https://pdv2.crowntech.com.br/login/` com um superusuário e conclua a MFA. `/control/login/` não autentica ninguém: apenas redireciona para `/login/`, onde valem o limite de tentativas e o TOTP.
3. Abra `https://pdv2.crowntech.com.br/control/`. O acesso exige superusuário **e** sessão com MFA verificada; equipe SaaS sem superusuário e usuários de empresas são recusados.

O que o admin permite:

- **Editar:** apenas Plano e Empresa, com as mesmas validações da API (lista de recursos do plano, limites que não podem ficar abaixo do uso atual, teto de desconto de 0 a 10000) e registro em auditoria. Não há exclusão, e o código (slug) do plano não muda depois de criado.
- **Somente leitura:** assinaturas, faturas, financeiro/fiado, vínculos de usuários, terminais, vendas, documentos fiscais, estoque e cadastros das empresas. Alterações nesses dados passam pelas rotas `/api/v1/platform/*` ou pelo painel da própria empresa.
- **Não aparecem:** usuários e grupos do Django, MFA e credenciais de integrações.

Se possível, restrinja `/control/` por IP ou VPN no proxy do Dokploy enquanto estiver ligado.

## Cobrança, fiscal e e-mail

Configure SMTP nas variáveis indicadas. As credenciais Asaas são globais da plataforma e devem ser inseridas no painel Serviços. Focus NFe é configurado em cada empresa, inicialmente em homologação. Nunca copie tokens para Git.

Cadastre webhook Asaas HTTPS em `https://pdv2.crowntech.com.br/api/v1/ops/webhooks/asaas`, com o mesmo token aleatório (32–255 caracteres) de `PDV_ASAAS_WEBHOOK_TOKEN`. Teste recebimento, reenvio e reconciliação no sandbox. Uma assinatura criada só libera acesso após cobrança confirmada pelo estado consultado no provedor.

Emissão Focus em ambiente production só é habilitada com `PDV_ALLOW_LIVE_FISCAL=1`, depois da homologação. Essa variável não conclui a configuração fiscal da empresa nem cria um motor tributário.

## Backup e alertas de falha

O serviço `backup` executa, a cada hora: `pg_dump`, validação do dump (`pg_restore --list`), envio ao repositório Restic (tag `pdv2-database`), cópia do diretório `/secrets` num snapshot próprio (tag `pdv2-secrets`), retenção (quando `BACKUP_PRUNE=1`, para as duas tags) e listagem dos snapshots do banco. As senhas do banco e do Restic vêm de `/secrets` (`PGPASSWORD_FILE`, `RESTIC_PASSWORD_FILE`); se `PGPASSWORD` ou `RESTIC_PASSWORD` estiverem definidas no container, elas valem. O horário de sucesso só é gravado quando **todas** as etapas terminam sem erro. Se qualquer uma falhar, o painel Plataforma > Backups passa a mostrar estado de erro com a etapa que falhou (por exemplo, "Falha ao gerar o dump do PostgreSQL (pg_dump)"), e a mensagem também sai no log do serviço `backup`.

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
- Guarde o kit de recuperação (`python -m operations.secrets_init --kit`) fora da VPS: chave de criptografia de tokens, chaves de licença e de versões, segredo de sessão, senha do banco e senha Restic. Sem esses segredos, restaurar apenas o SQL é insuficiente.

## Terminal na loja

O terminal fica na loja, separado da nuvem. No Windows, use o instalador descrito em [INSTALADOR-WINDOWS.md](INSTALADOR-WINDOWS.md) (serviço, atalho na área de trabalho e atualização automática); em máquina com Docker, `compose.terminal.yaml`. Pareie a `https://pdv2.crowntech.com.br`. Para impressora USB Windows, prefira instalação nativa com driver RAW; container Linux não recebe automaticamente o spool do Windows.

Cada terminal possui banco próprio. O cenário de servidor local compartilhado por várias estações, com replicação local entre caixas, ainda não foi implementado. Os terminais independentes continuam vendendo offline dentro da licença (30 dias por padrão, campo `offline_days` do plano) e reconciliam na nuvem quando a conexão retorna. O desenho do servidor da loja com estações está em [ARQUITETURA-OFFLINE-LOCAL.md](ARQUITETURA-OFFLINE-LOCAL.md).

## Antes de liberar

Confira [STATUS-V2.md](STATUS-V2.md). A configuração de deploy existe, mas ambiente fiscal, pagamentos, hardware, carga, monitoramento e processos de suporte precisam de validação no ambiente final.

Referências: [Dokploy Compose](https://docs.dokploy.com/docs/core/docker-compose), [checklist Django](https://docs.djangoproject.com/en/5.2/howto/deployment/checklist/), [Restic restore](https://restic.readthedocs.io/en/stable/050_restore.html).

Para identificar corretamente clientes nos limites de tentativas, defina `PDV_TRUSTED_PROXY_CIDRS` como o CIDR real da rede interna do proxy. Cabeçalhos X-Forwarded-For de conexões fora dessa rede são ignorados. Não configure `0.0.0.0/0`. A verificação `check --deploy` pode avisar sobre HSTS preload ainda desativado; só solicite inclusão na lista de navegadores após definir domínio e política de subdomínios.
