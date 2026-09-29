# Preparação para Dokploy

O nome oficial do produto é **Dokploy**. Esta configuração é para **prévia interna**. O software ainda carece de autenticação reforçada, limitação de tentativas, banco de produção, monitoramento, backup externo e revisão fiscal. Não usar com dados de clientes reais.

## Pré-requisitos ainda não fornecidos

- IP e acesso administrativo à VPS.
- Conta/painel Dokploy instalado ou autorização para instalá-lo na VPS.
- Domínio escolhido, registrado em nome do proprietário e apontado por DNS para a VPS.
- Repositório Git privado acessível ao Dokploy.
- Destino externo e criptografado para backup.

## Artefatos já preparados

- `Dockerfile` empacota o protótipo sem dependências Python externas.
- `compose.preview.yaml` cria um serviço e volume de dados persistente, publicado somente em `127.0.0.1` para prévia no Docker Desktop. Antes de usar no Dokploy, retire a seção `ports` e configure o domínio na UI.
- `/health` permite verificar o processo.

## Procedimento quando houver VPS e domínio

1. Instalar/configurar Dokploy seguindo a [documentação oficial](https://docs.dokploy.com/docs/core/installation) e restringir o acesso administrativo.
2. Conectar o [repositório privado por provedor Git ou chave SSH](https://docs.dokploy.com/docs/core/providers).
3. Criar projeto e serviço Docker Compose a partir de `compose.preview.yaml`, no diretório raiz deste repositório.
4. Configurar o domínio de prévia na aba Domains e HTTPS no Dokploy, conforme a [documentação oficial de domínios](https://docs.dokploy.com/docs/core/docker-compose/domains). Verificar que DNS aponta para o IP correto.
5. Implantar e conferir `/health`, persistência do volume após redeploy e logs.
6. Configurar backup fora da VPS e ensaiar restauração. O volume do Compose sozinho **não é backup**.

O Dokploy oferece conexão de repositórios privados e domínio/HTTPS para aplicações Compose, conforme a [documentação dos provedores](https://docs.dokploy.com/docs/core/providers) e [domínios](https://docs.dokploy.com/docs/core/docker-compose/domains). O registro de um domínio `.com.br` requer consulta e contratação no [Registro.br](https://registro.br/ajuda/registro-de-novos-dominios/); não escolhemos nem compramos um domínio sem o nome final da marca e a conta titular.
