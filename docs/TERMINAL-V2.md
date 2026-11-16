# Terminal PDV2 local (Caixa Rápido) — prévia funcional

Atualizado em 16/11/2026.

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

### Outro terminal ou outra empresa no mesmo computador

Um banco local não pode ser reaproveitado por outra empresa depois que existem vendas, caixa, fila de sincronização ou backup. Na tela de pareamento/login, use **Criar novo perfil para outra empresa**. O terminal cria um SQLite novo em `data/profiles/perfil-.../terminal.sqlite3`, grava um backup verificado do banco anterior e seleciona o novo perfil também nos próximos reinícios. O banco antigo permanece preservado; antes de trocar, feche o caixa e sincronize eventos pendentes. Para instalações separadas, também é possível iniciar outro processo com `PDV_TERMINAL_DATA` e `PDV_TERMINAL_DB` apontando para outro diretório.

## Operação

- PIN local e papéis vindos do cloud; sessão HttpOnly/SameSite Strict, token CSRF em mutações, validação de Host e Origin.
- Busca por nome, SKU e código de barras; preço, categoria, estoque local e carrinho em UN/KG/L com milésimos. `UN` exige quantidade inteira. Cada linha arredonda `preço_cents × quantidade_milli ÷ 1000` com `ROUND_HALF_UP`.
- Produtos de produção própria podem ser marcados no admin da empresa com **Controlar estoque** desativado. Eles aparecem no caixa mesmo com saldo zero, aceitam venda por KG ou unidade conforme o cadastro e não reduzem o saldo nem geram conflitos de estoque. Produtos normais continuam bloqueados quando não há saldo.
- Venda por peso: produtos `KG` pedem o peso em quilogramas com até três casas decimais e calculam o valor proporcional ao preço por kg. O cadastro aceita um `scale_code`/PLU opcional para etiquetas de balança.
- Balanças: o terminal pode receber peso automaticamente pelo Web Serial em Chrome/Edge (USB/COM, 9600/4800/19200 bps, protocolo textual com peso em kg ou g). Também interpreta etiquetas EAN-13 de balanças com prefixo `2`, PLU de cinco dígitos e payload configurável como peso em gramas ou preço total. O botão **Conectar balança** fica no caixa; sem equipamento conectado, o peso pode ser digitado.
- Desconto por venda: operador de caixa até 10%, gerente até 50%, proprietário/admin até 100%; acima do limite do operador requer PIN de gerente. Os limites são constantes desta prévia.
- Pagamento dividido entre dinheiro, Pix e cartão, com troco apenas sobre dinheiro. Pix/cartão são **declarações manuais**, sem TEF, captura, confirmação ou estorno automático.
- Abertura/fechamento de caixa, sangria e suprimento, suspensão/retomada de carrinho, histórico local, reimpressão e cancelamento comercial com motivo e autorização de gerente. Ajustes de estoque exigem papel autorizado. Todos geram auditoria local relevante.
- Venda, baixa de estoque, snapshot financeiro e evento de outbox são atômicos no SQLite. Reenvio da mesma requisição retorna a venda original; dados diferentes com a mesma chave são rejeitados.
- O worker envia lotes pendentes para `/api/v1/terminal/sync` com backoff. Só marca eventos locais após ACK integral e ordenado; perda de resposta faz reenvio idempotente. A nuvem confirma todos os eventos recebidos: os que ela não conseguiu aplicar voltam em `quarantined`, são guardados no SQLite (`sync_quarantine`) e saem da fila, para que um evento ruim nunca trave os seguintes; as divergências voltam em `conflicts` e ficam visíveis para o gerente. Contrato em [SYNC-VENDAS.md](SYNC-VENDAS.md). `sale.completed` não gera ajuste de estoque separado. Histórico, exportação e fechamento de caixa continuam disponíveis depois que a lease expira; novas vendas ficam bloqueadas. A política atual do cloud permite **até 72 horas** de uso offline em uma licença normal, podendo reduzir o prazo para assinatura atrasada; não há prazo adicional após `expires_at`. Relógio que retrocede mais de cinco minutos bloqueia novas vendas. O valor de 72 horas deve permanecer alinhado entre backend, terminal, planos e documentação.
- Após cada carga bem-sucedida, o terminal consulta a versão estável publicada. O manifesto de release é verificado com a **chave Ed25519 fixada no pareamento** e aparece na tela Sistema. O terminal não baixa nem executa o pacote automaticamente; a instalação e checagem do SHA-256 são etapas manuais do suporte.
- O serviço faz uma cópia SQLite consistente após o pareamento e, depois, no máximo uma vez a cada 24 horas; um gerente pode acionar **Criar backup agora**. Até sete cópias rotativas ficam em `data/backups`, com permissões locais restritas. A API mostra nomes e estado, sem servir o arquivo bruto pela web. O arquivo contém token, hashes de PIN e vendas: guarde cópia externa protegida. A restauração ainda é manual, com o serviço parado, e deve ser testada no piloto.

## Caixa Rápido: a tela de venda

A frente de caixa foi refeita em novembro/2026 para ser operada só pelo teclado e pelo leitor. O código está em `terminal/static/terminal.js` (tela) e `terminal/static/terminal-core.js` (regras puras, onde fica o mapa de teclas `SHORTCUTS`).

### Régua de teclas

A régua fica sempre visível no rodapé e cada tecla também pode ser tocada ou clicada. Tecla sem uso no momento aparece apagada, com o motivo na dica.

| Tecla | Ação |
|---|---|
| F1 | Ajuda e lista de atalhos |
| F2 | Buscar produto no catálogo |
| F3 | Alterar quantidade ou peso do item |
| F4 | Cliente e CPF na nota |
| F5 | Desconto na venda |
| F6 | Vendas em espera: guardar ou retomar (mostra quantas há) |
| F7 | Sangria ou suprimento (exige caixa aberto) |
| F8 | Cancelar o item selecionado |
| F9 | Reimprimir comprovante |
| F10 | Pagar a venda |
| F11 | Modo foco (tela cheia) |
| F12 | Abrir caixa, ou fechar o caixa aberto |
| Esc | Voltar um passo |
| Ctrl+K | Paleta de comandos: busca qualquer ação pelo nome |

Leitura e lista de itens:

| Tecla | Ação |
|---|---|
| Enter | Lança o código digitado ou lido |
| `3*código` | Lança 3 unidades do código |
| ↑ ↓ | Seleciona o item da lista |
| Delete | Cancela o item selecionado |
| qualquer letra ou número | Vai direto para o campo de leitura |

As teclas com cadeado (desconto, cancelar item, cancelar a venda, fechar caixa) podem pedir PIN de gerente. As ações sem tecla própria ficam na paleta (Ctrl+K), na ajuda (F1) e no menu do operador: receber fiado, devolução ou troca, cancelar a venda atual, balança e etiquetas, atualizar catálogo, trocar operador e sair.

### Modos

- **Venda** (padrão): o leitor está sempre pronto; a régua mostra F1 a F12.
- **Pagamento** (depois do F10): a régua troca para as formas de pagamento. `1` Dinheiro, `2` Pix, `3` Débito, `4` Crédito, `5` A prazo (fiado), `6` Crédito na loja, `7` a `9` formas próprias da empresa, `0` dividir o pagamento, Enter confirma, Esc volta um passo. Só aparecem as formas ativas no painel. Nesse modo continuam valendo F1, F4, F5, F11, Esc e Ctrl+K; as outras teclas F pedem para voltar à venda antes. Uma leitura do leitor de código durante o pagamento é ignorada, para não confirmar a venda por engano.
- **Foco** (F11): tela cheia, sem as abas, só a venda. A escolha fica salva no navegador daquele caixa.
- **Conexão**: o indicador mostra se o caixa está online ou offline e se há registros na fila ou para revisão. Sem internet a venda continua igual.

### Temas e preferências

No menu do operador (ou pela paleta): tema **Claro**, **Escuro** ou **Alto contraste**. O tema fica salvo no navegador do caixa e é aplicado antes de a tela aparecer (`terminal/static/theme.js`). No mesmo menu: som de leitura ligado ou desligado e abrir o comprovante depois de cada venda.

### Caixa de demonstração

Para ver a tela sem parear nem tocar em dados reais:

```sh
./.venv/bin/python scripts/demo_terminal.py              # http://127.0.0.1:8099, online simulado
./.venv/bin/python scripts/demo_terminal.py --offline    # simula a queda da internet (fila pendente)
./.venv/bin/python scripts/demo_terminal.py --port 8098  # outra porta
```

O script cria um diretório temporário, já pareado com uma empresa fictícia, com catálogo de mercado, um operador de caixa e um gerente, uma promoção "leve 3 pague 2" e uma impressora de recibos que grava em arquivo. Ao subir, ele imprime os PINs de demonstração, um código de barras de teste e uma etiqueta de balança. Nunca lê nem grava o banco real do terminal, nunca fala com a nuvem, e apaga os dados ao encerrar (Ctrl+C).

## Impressão

Recibos **não fiscais** usam ESC/POS de 58 ou 80 mm. A impressão de etiquetas de 50×30 mm fica no painel da empresa, em Produtos → Imprimir etiquetas, com quantidade e prévia. O navegador cria uma etiqueta por página; selecione a mídia 50×30 mm, escala de 100%, sem cabeçalhos/rodapés, e desative o corte automático no driver. A impressora precisa aceitar o material e detectar o intervalo entre etiquetas. A calibração do sensor e o tamanho do intervalo dependem do modelo e do rolo.

A TM-T20 é uma impressora de recibos para bobinas de 58 ou 80 mm, conforme o [manual técnico da Epson, páginas 25–27](https://download4.epson.biz/sec_pubs/bs/pdf/TM-T20_trg_en_revD.pdf). Ela não oferece alinhamento por intervalo para as etiquetas pré-cortadas de 50×30 mm usadas no piloto. O perfil incorreto `escpos50x30` e a reutilização automática da fila de recibos foram desabilitados. Configurações antigas recebem um erro explicativo; trabalhos antigos ESC/POS de etiqueta ainda pendentes ficam bloqueados, preservando payload e histórico, e não podem ser reenviados. O corte dos recibos continua independente.

O terminal mantém os formatos ZPL/EPL para uma impressora de etiquetas dedicada, com layout e mídia a validar por modelo. Cada trabalho é persistido no SQLite antes do envio. Transportes: CUPS (`lp -d FILA -o raw`) no macOS/Linux; ponte local autenticada para o CUPS do host quando o terminal está no Docker; TCP para `host:porta`; spool RAW Windows via `pywin32`; ou arquivo binário de diagnóstico. O worker marca um trabalho como `sending` antes de transmitir; falhas ou interrupções nesse ponto ficam `uncertain`. O operador precisa confirmar explicitamente o risco de duplicidade para reenviar. Reimpressão da venda cria outro trabalho e fica auditada.

## Testes

```sh
./.venv/bin/python -m unittest discover -s terminal/tests -v
```

Os testes cobrem autenticação, CSRF/Origin/Host, venda fracionária e dinheiro, reinício, idempotência, limite e rateio de desconto, cancelamento, proteção da lista de PINs, lease vencida/relógio, backup consistente, bytes ESC/POS/ZPL/EPL, manifesto de versão assinado e fila de impressão com resultado incerto. Um teste adicional integra as rotas reais do Django com o terminal Flask/SQLite em processo: pareia, carrega catálogo assinado, vende, simula perda de ACK, reenvia e cancela com estorno central único.

## Limites comerciais e fiscais

Esta prévia **não emite NFC-e nem DANFE**, não assina XML, não implementa contingência fiscal ou homologação na SEFAZ-RJ. Uma venda local não é documento fiscal. Não houve teste físico de balança, gaveta, leitor, impressora ou energia em loja. O backup local não substitui backup externo; não há recuperação remota; o instalador Windows com atualização automática assinada existe ([INSTALADOR-WINDOWS.md](INSTALADOR-WINDOWS.md)), mas ainda não foi validado em Windows físico. O SaaS ainda precisa de conciliação operacional de estoque e assinatura/cobrança automática antes de venda como produto completo.
