# Terminal PDV2 local — prévia funcional

`terminal/` é uma aplicação Flask/Waitress independente do navegador do painel SaaS. O navegador acessa **o serviço local** em `http://127.0.0.1:8010`; SQLite é o armazenamento do caixa e `WAL`+`synchronous=FULL` protegem transações confirmadas. Cada computador de caixa pode usar essa mesma porta: a licença, o código de pareamento, o `device_id` e o banco local identificam o equipamento. Somente duas instâncias no mesmo computador precisam de portas diferentes. O código anterior em `local_pdv/` permanece como protótipo legado e não é usado por este serviço.

## Iniciar

Na pasta do projeto, com Python 3.12 e as dependências de `terminal/requirements.txt`:

```sh
./.venv/bin/python -m terminal
```

Abra `http://127.0.0.1:8010`. Por padrão o serviço escuta somente em `127.0.0.1`. No contêiner, configure `PDV_TERMINAL_HOST=0.0.0.0`, `PDV_TERMINAL_PORT=8010`, `PDV_TERMINAL_DATA=/data`, `PDV_TERMINAL_DB=/data/terminal.sqlite3` e publique a porta somente no loopback do host. O serviço aceita Host `localhost`/`127.0.0.1` por padrão.

No Windows, `terminal/install-windows.ps1` copia `terminal/` e `shared/` para `%LOCALAPPDATA%\PDV2`, cria um ambiente Python e um lançador `PDV2-Terminal.cmd`. Antes de substituir uma instalação existente, faz um backup SQLite verificado em `data\backups`; uma falha cancela a atualização. A opção `-Autostart` cria um atalho na inicialização do usuário. É um **instalador de desenvolvimento**, requer Python 3.12 e internet para instalar dependências, e ainda não foi validado em Windows físico. Em Linux/macOS, `sh terminal/run-local.sh` usa a `.venv` do projeto quando disponível.

## Pareamento e primeira carga

No painel SaaS, crie a empresa, loja, operadores com PIN, produtos, saldo inicial e um terminal. Copie a chave empresarial em **Assinatura** e o código de pareamento de uso único em **Terminais**. Informe endereço do cloud, chave empresarial e código no assistente local. A chave identifica a assinatura; o código temporário autoriza o dispositivo. O terminal gera e guarda seu `device_id` antes do pareamento, recebe token exclusivo, chave pública Ed25519 e lease assinada; a chave pública fica fixada no dispositivo. A primeira carga baixa catálogo, saldos em milésimos e operadores. A lista de operadores só é gravada se seu digest corresponder ao `operators_hash` assinado na lease. Atualizações seguintes de catálogo **não sobrescrevem o estoque local** ou vendas ainda na fila. Produto retirado do catálogo deixa de aparecer para novas vendas, mas permanece no histórico; produtos sem código de barras são aceitos.

Para testes em Docker, `PDV_ENV=development` libera apenas `http://cloud:8080` e `http://host.docker.internal:8000` além de localhost. Em produção, servidor remoto exige HTTPS. O token do terminal nunca é enviado ao navegador. O banco local e `session.key` precisam de proteção do sistema operacional e backup seguro.

## Operação

- PIN local e papéis vindos do cloud; sessão HttpOnly/SameSite Strict, token CSRF em mutações, validação de Host e Origin.
- Busca por nome, SKU e código de barras; preço, categoria, estoque local e carrinho em UN/KG/L com milésimos. `UN` exige quantidade inteira. Cada linha arredonda `preço_cents × quantidade_milli ÷ 1000` com `ROUND_HALF_UP`.
- Desconto por venda: operador de caixa até 10%, gerente até 50%, proprietário/admin até 100%; acima do limite do operador requer PIN de gerente. Os limites são constantes desta prévia.
- Pagamento dividido entre dinheiro, Pix e cartão, com troco apenas sobre dinheiro. Pix/cartão são **declarações manuais**, sem TEF, captura, confirmação ou estorno automático.
- Abertura/fechamento de caixa, sangria e suprimento, suspensão/retomada de carrinho, histórico local, reimpressão e cancelamento comercial com motivo e autorização de gerente. Ajustes de estoque exigem papel autorizado. Todos geram auditoria local relevante.
- Venda, baixa de estoque, snapshot financeiro e evento de outbox são atômicos no SQLite. Reenvio da mesma requisição retorna a venda original; dados diferentes com a mesma chave são rejeitados.
- O worker envia lotes pendentes para `/api/v1/terminal/sync` com backoff. Só marca eventos locais após ACK integral e ordenado; perda de resposta faz reenvio idempotente. `sale.completed` não gera ajuste de estoque separado. Histórico, exportação e fechamento de caixa continuam disponíveis depois que a lease expira; novas vendas ficam bloqueadas. O lease emitido pelo cloud já contém **até sete dias** de uso offline desde `issued_at`; não há prazo adicional após `expires_at`. Relógio que retrocede mais de cinco minutos bloqueia novas vendas.
- Após cada carga bem-sucedida, o terminal consulta a versão estável publicada. O manifesto de release é verificado com a **chave Ed25519 fixada no pareamento** e aparece na tela Sistema. O terminal não baixa nem executa o pacote automaticamente; a instalação e checagem do SHA-256 são etapas manuais do suporte.
- O serviço faz uma cópia SQLite consistente após o pareamento e, depois, no máximo uma vez a cada 24 horas; um gerente pode acionar **Criar backup agora**. Até sete cópias rotativas ficam em `data/backups`, com permissões locais restritas. A API mostra nomes e estado, sem servir o arquivo bruto pela web. O arquivo contém token, hashes de PIN e vendas: guarde cópia externa protegida. A restauração ainda é manual, com o serviço parado, e deve ser testada no piloto.

## Impressão

Configurações locais suportam recibo **não fiscal** ESC/POS de 58 ou 80 mm e etiqueta ZPL/EPL. Cada trabalho é persistido no SQLite antes do envio. Transportes: TCP para `host:porta`, spool RAW Windows via [`pywin32` 312](https://pypi.org/project/pywin32/312/) incluído apenas na instalação Windows, ou arquivo binário em uma pasta para teste. O worker marca um trabalho como `sending` antes de transmitir; se falhar ou o processo reiniciar nesse ponto, fica `uncertain`. O operador precisa confirmar explicitamente o risco de duplicidade para reenviar. Reimpressão da venda cria outro trabalho e fica auditada. Compatibilidade física por modelo ainda não foi verificada.

## Testes

```sh
./.venv/bin/python -m unittest discover -s terminal/tests -v
```

Os testes cobrem autenticação, CSRF/Origin/Host, venda fracionária e dinheiro, reinício, idempotência, limite e rateio de desconto, cancelamento, proteção da lista de PINs, lease vencida/relógio, backup consistente, bytes ESC/POS/ZPL/EPL, manifesto de versão assinado e fila de impressão com resultado incerto. Um teste adicional integra as rotas reais do Django com o terminal Flask/SQLite em processo: pareia, carrega catálogo assinado, vende, simula perda de ACK, reenvia e cancela com estorno central único.

## Limites comerciais e fiscais

Esta prévia **não emite NFC-e nem DANFE**, não assina XML, não implementa contingência fiscal ou homologação na SEFAZ-RJ. Uma venda local não é documento fiscal. Não houve teste físico de balança, gaveta, leitor, impressora ou energia em loja. O backup local não substitui backup externo; não há recuperação remota, atualização assinada do binário ou instalador comercial homologado. O SaaS ainda precisa de conciliação operacional de estoque e assinatura/cobrança automática antes de venda como produto completo.
