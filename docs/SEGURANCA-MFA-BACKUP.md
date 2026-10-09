# MFA da administração e backup externo

## MFA da administração SaaS

Em produção (`PDV_PLATFORM_MFA_REQUIRED=1`), contas superusuárias e contas
com os papéis `platform_support`, `platform_billing` ou `platform_ops` não
recebem uma sessão de plataforma somente com e-mail e senha.

1. No primeiro acesso, o login exibe a chave TOTP e a URI `otpauth://`.
2. O administrador cadastra a chave no autenticador e confirma o código de
   seis dígitos.
3. O sistema entrega dez códigos `PDV2-XXXX-XXXX`; eles são mostrados uma vez,
   armazenados apenas como hashes e invalidados depois do uso.
4. Cada novo acesso exige o código TOTP ou um código de recuperação. Cinco
   tentativas inválidas bloqueiam a conta por quinze minutos, além do limite
   compartilhado por IP e usuário.

O segredo TOTP fica cifrado com `PDV_ENCRYPTION_KEY`, a mesma chave Fernet
usada para credenciais de integrações. Para ambiente local, o padrão de
desenvolvimento mantém a exigência desligada; isso evita alterar os testes e
o fluxo de clientes. A composição de produção liga a exigência por padrão.

## Backup Restic externo

O serviço `backup` gera um `pg_dump` PostgreSQL em formato custom a cada hora,
valida o dump com `pg_restore --list`, cifra e envia para o repositório Restic
configurado em `RESTIC_REPOSITORY`. O banco principal nunca é usado como alvo
de restauração.

Configure no arquivo de ambiente do Dokploy:

```dotenv
RESTIC_REPOSITORY=s3:https://endpoint-do-provedor/bucket/pdv2
RESTIC_PASSWORD=uma-senha-longa-e-aleatoria
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
AWS_DEFAULT_REGION=us-east-1
BACKUP_PRUNE=1
```

Depois de subir a composição de produção, valide o destino externo e a
restauração em um banco temporário isolado:

```bash
python3 scripts/verify_backup.py \
  --compose compose.production.yaml \
  --env-file .env.production \
  --require-external
```

O comando cria um snapshot novo, encontra o dump mais recente, restaura em um
banco com nome aleatório, confere tenants, produtos, vendas e migrações, gera
`.runtime/backup-restore-report.json` e remove o banco temporário mesmo quando a
verificação falha. O relatório não contém a senha Restic.

O teste local já foi validado no projeto com o repositório Restic do volume de
desenvolvimento: a restauração isolada passou com 2 tenants, 9 produtos, 25
vendas e 38 migrações. Essa evidência não substitui a execução final apontando
para o bucket externo da VPS antes de aceitar clientes pagantes.
