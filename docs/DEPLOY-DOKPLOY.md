# Nuvem PDV2 no Dokploy

O projeto está preparado para Docker Compose. Nenhuma VPS foi acessada nesta entrega. Use `compose.production.yaml`, não `compose.local.yaml` nem `compose.preview.yaml`.

## Preparar a instalação

1. Configure domínio próprio e HTTPS no Dokploy.
2. Crie projeto Compose a partir do repositório privado e selecione `compose.production.yaml`.
3. Gere valores exclusivos para as variáveis de `infra/production.env.example`. O segredo Django precisa de ao menos 50 caracteres aleatórios. Use senha de banco URL-safe; a URL de conexão exige codificação se usar caracteres especiais reservados.
4. Gere um par Ed25519 exclusivo para licenças com `shared.licensing.generate_keypair()`. Armazene as chaves fora do repositório. Configure `PDV_LICENSE_PRIVATE_KEY_PATH` e `PDV_LICENSE_PUBLIC_KEY_PATH` com caminhos absolutos na VPS. O arquivo montado deve ser legível pelo UID 10001 no container; proteja o diretório pai no host. Docker Compose com secret baseado em arquivo preserva permissões do arquivo no host.
5. Gere `PDV_ENCRYPTION_KEY` com `Fernet.generate_key()`; ela protege tokens dos provedores no banco. Guarde cópias seguras da chave e dos demais segredos separadas do banco.
6. Configure Restic em destino externo, senha do repositório e credenciais de armazenamento. `PDV_BACKUP_OFFSITE=1` declara a configuração esperada; confira a restauração de verdade antes de atender clientes.
7. Associe o domínio ao serviço `cloud`, porta interna **8080**, usando o painel de domínios do Dokploy e faça deploy. Não publique PostgreSQL ou worker. O proxy precisa encaminhar HTTPS corretamente; `PDV_TRUST_PROXY=1` só deve existir atrás do proxy confiável.

`migrate` termina antes de iniciar cloud. `worker` só inicia depois do healthcheck. A tarefa de backup roda a cada hora. O banco usa volume persistente. Não use `down -v` em uma instalação com dados.

## Criar o primeiro administrador

No terminal do serviço cloud:

```sh
python manage.py createsuperuser
```

Use e-mail também como username, pois o login web usa o endereço de e-mail. O bootstrap de demonstração está bloqueado por padrão em produção. Cadastre a equipe SaaS no painel; reserve superusuário para administração restrita.

## Cobrança, fiscal e e-mail

Configure SMTP nas variáveis indicadas. As credenciais Asaas são globais da plataforma e devem ser inseridas no painel Serviços. Focus NFe é configurado em cada empresa, inicialmente em homologação. Nunca copie tokens para Git.

Cadastre webhook Asaas HTTPS em `/api/v1/ops/webhooks/asaas`, com o mesmo token aleatório (32–255 caracteres) de `PDV_ASAAS_WEBHOOK_TOKEN`. Teste recebimento, reenvio e reconciliação no sandbox. Uma assinatura criada só libera acesso após cobrança confirmada pelo estado consultado no provedor.

Emissão Focus em ambiente production só é habilitada com `PDV_ALLOW_LIVE_FISCAL=1`, depois da homologação. Essa variável não conclui a configuração fiscal da empresa nem cria um motor tributário.

## Atualização e recuperação

- Versione o deploy em Git. Leia migrações antes de atualizar e gere snapshot/backup primeiro.
- Faça upgrade em homologação, execute testes e verifique compatibilidade dos terminais.
- Mantenha a imagem anterior e o backup. Reverter a imagem não reverte automaticamente o schema ou os dados.
- Faça restore em banco novo para testar; troque conexão somente após conferir os registros. Nunca restaure por cima do banco ativo como teste.
- Monitore saúde do cloud/worker, fila `needs_review`, idade do último backup, espaço em disco e erros de sincronização.
- Faça guarda externa da chave de criptografia de tokens, chave de licença, segredo de sessão e senha Restic. Sem esses segredos, restaurar apenas o SQL é insuficiente.

## Terminal na loja

O terminal fica na loja, separado da nuvem. Use `compose.terminal.yaml` em máquina com Docker, ou instalador Python/Windows em `terminal/`. Pareie ao domínio HTTPS. Para impressora USB Windows, prefira instalação nativa com driver RAW; container Linux não recebe automaticamente o spool do Windows.

Cada terminal possui banco próprio. O cenário de servidor local compartilhado por várias estações, com replicação local entre caixas, ainda não foi implementado. Os terminais independentes continuam vendendo offline dentro da licença e reconciliam na nuvem quando a conexão retorna.

## Antes de liberar

Confira [STATUS-V2.md](STATUS-V2.md). A configuração de deploy existe, mas ambiente fiscal, pagamentos, hardware, carga, monitoramento e processos de suporte precisam de validação no ambiente final.

Referências: [Dokploy Compose](https://docs.dokploy.com/docs/core/docker-compose), [checklist Django](https://docs.djangoproject.com/en/5.2/howto/deployment/checklist/), [Restic restore](https://restic.readthedocs.io/en/stable/050_restore.html).

Para identificar corretamente clientes nos limites de tentativas, defina `PDV_TRUSTED_PROXY_CIDRS` como o CIDR real da rede interna do proxy. Cabeçalhos X-Forwarded-For de conexões fora dessa rede são ignorados. Não configure `0.0.0.0/0`. A verificação `check --deploy` pode avisar sobre HSTS preload ainda desativado; só solicite inclusão na lista de navegadores após definir domínio e política de subdomínios.
