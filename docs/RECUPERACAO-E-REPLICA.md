# Recuperação e réplica em segundo disco (fase 2)

Este documento descreve o que a fase 2 de [ARQUITETURA-OFFLINE-LOCAL.md](ARQUITETURA-OFFLINE-LOCAL.md) entrega: trocar de computador sem perder dados e manter uma cópia de segurança contínua do caixa em outro disco. A fase foi dividida em três frentes; os detalhes de rota ficam em [API-V2.md](API-V2.md) e os de tela em [TERMINAL-V2.md](TERMINAL-V2.md).

| Frente | Onde | O que faz |
|---|---|---|
| A | nuvem e painel | "Trocar de computador", token só-envio, `terminal/restore`, evento `terminal.config`, chave da cópia de segurança |
| B | terminal | tela "Recuperar minha loja", `apply_restore`, importação da fila, rotas locais |
| C | `terminal/mirror.py` | réplica cifrada em segundo disco e a leitura dela na recuperação |

## 1. Para o lojista

**Cópia de segurança em outro disco**

1. Espete um pendrive ou HD externo no computador do caixa.
2. No caixa, entre como gerente, abra **Sistema → Cópia de segurança em outro disco → Escolher disco**.
3. Pronto. A primeira cópia é tentada na hora; a tela só diz "A primeira cópia já foi feita" quando ela foi mesmo. Depois disso cada venda vai para o pendrive em segundos e o caixa mostra "Cópia de segurança em dia".

Se o pendrive for retirado, o caixa **continua vendendo** e a barra de estado da tela de venda mostra "Cópia de segurança parada" (ou "Cópia atrasada"), com o motivo e quantos registros estão só neste computador. Ao recolocar, a cópia se põe em dia sozinha.

**Troquei de computador**

1. No painel: **Terminais → Trocar de computador**. Aparecem a chave da licença e um código de recuperação (vale 30 minutos).
2. No computador novo: instale o PDV2, abra o ícone, escolha **Recuperar minha loja** e digite a chave e o código.
3. Espere a barra "Trazendo seus dados".
4. Se o computador antigo quebrou com vendas ainda não enviadas, escolha **Tenho uma cópia de segurança** e espete o pendrive. Senão, **Não tenho, continuar**. Se parte da cópia não puder ser lida, o aviso fica na tela e dá para ler outra cópia (outro pendrive) antes de continuar.

Se o código foi usado no caixa errado, **Desistir da recuperação** (com o PIN do gerente) devolve o caixa ao computador antigo.

Voltam: catálogo, clientes e saldos, vendas dos últimos 90 dias, fiado em aberto, devoluções, recebimentos, o caixa aberto e a configuração de impressão. Não voltam: carrinhos suspensos e fila de impressão.

## 2. Dois segredos diferentes

| Nome | O que é | Onde aparece |
|---|---|---|
| **Chave da licença** | `PDV2-XXXXXX-…`. Identifica a empresa. | Painel; é digitada em "Recuperar minha loja". |
| **Chave da cópia de segurança** (`mirror_key`) | Segredo por empresa que cifra a réplica. | O terminal recebe da nuvem no pareamento e a cada renovação de licença. O dono pode vê-la **uma única vez** no painel; ninguém precisa dela para trocar de computador (serve só para o suporte abrir a cópia do pendrive em um caso extremo). |

O terminal nunca pede para digitar a chave da cópia de segurança: ela chega da nuvem. Um caixa que nunca falou com a nuvem depois desta versão fica em "Conecte este caixa à internet uma vez para liberar a cópia de segurança" e não grava nada no disco até recebê-la. Token, `mirror_key` e código de recuperação nunca vão para log, auditoria, exportação, manifesto ou mensagem de erro.

## 3. A réplica (`terminal/mirror.py`)

### 3.1 O que é gravado

```
<pasta escolhida>/PDV2-copia-de-seguranca/<terminal_id>/<device_id>/
    manifest.json                         em claro: quem é, quais arquivos valem
    snapshot-<created_ms>.pdv2snap        cópia do banco, cifrada
    journal-<primeiro_seq 12 dígitos>.pdv2log   diário de eventos, cifrado
```

A pasta por `device_id` impede que o computador antigo e o novo, usando o mesmo pendrive, misturem as numerações da fila.

- **Cifra:** Fernet (`cryptography`, que já era dependência) com a chave da cópia de segurança.
- **`manifest.json`:** `{"format":"pdv2-mirror","v":1,"tenant_id","shop_id","terminal_id","device_id","tenant_name","shop_name","terminal_name","key_id","snapshots":[{"name","created_at","outbox_seq","size_bytes","sha256"}],"journal_seq","journal_segments":[{"name","first_seq"}],"updated_at","app_version"}`. Trocado de forma atômica. É regravado **só quando uma cópia do banco termina**, nunca a cada venda: em pendrive FAT/exFAT a troca do arquivo não é atômica, e o diário já diz sozinho até onde a réplica chegou. Se o manifesto se perder (queda de energia ou pendrive puxado no meio da troca), a leitura tenta `manifest.json.tmp` e, na falta, refaz o índice pelos cabeçalhos das cópias e pelo caminho `<terminal_id>/<device_id>`; a recuperação segue com o aviso "O índice da cópia de segurança estava danificado; os arquivos foram lidos diretamente."
- **Cópia do banco** (texto ASCII, por linhas):
  - linha 1: cabeçalho em claro `{"format":"pdv2-mirror-snapshot","v":1,"key_id","terminal_id","device_id","created_at","outbox_seq","chunk_bytes":4194304}`;
  - uma linha por bloco de 4 MiB do SQLite, cada uma um token Fernet;
  - última linha: `END <token Fernet de {"chunks","size","sha256"}>`, com o SHA-256 do arquivo em claro.
- **Diário:** uma linha por evento, `<seq>\t<event_id>\t<token Fernet>\n`. O conteúdo cifrado é o JSON canônico `{"seq","event_id","created_at","event":{…}}`, com o evento exatamente como está na fila.

A cópia do banco **não leva** o token do terminal nem a própria `mirror_key`: as duas chaves são apagadas da cópia antes de cifrar. Quem restaura sempre pareia de novo.

### 3.2 Quando é gravado

| O quê | Quando |
|---|---|
| Diário | A cada evento novo na fila (a thread é acordada pelo gancho `on_event`) e, de todo modo, a cada 5 s. |
| Cópia do banco | Na hora em que o disco é escolhido; a cada 15 min se houve evento desde a última; a cada 24 h mesmo sem movimento; em todo fechamento de caixa; em "Copiar agora". |
| Retenção | 3 cópias. Segmentos do diário já cobertos pela cópia mais antiga são apagados. |

O diário é sempre alimentado **pela tabela `outbox`, em ordem de `seq`**, com uma conexão SQLite própria e só de leitura. Evento desfeito por rollback nunca entra; queda entre a venda e a gravação não perde nada, porque a passada seguinte recomeça de onde o disco parou.

Passos de cada cópia: `backup` do SQLite para um arquivo temporário ao lado do banco → `PRAGMA integrity_check` → cifra em blocos em `*.tmp` no destino → `fsync` → `os.replace` → **relê do disco, decifra e confere o SHA-256** → atualiza o manifesto → apaga o temporário. Cada cópia concluída com `outbox_seq = S` abre o segmento `journal-<S+1>`.

**Relógio do computador corrigido para trás.** Se a hora estava adiantada quando uma cópia foi feita e depois foi acertada, a passada seguinte faz **uma** cópia de reancoragem com a hora certa e rebaixa, no manifesto, o carimbo das cópias retidas; a cadência normal volta dali em diante. Prazos guardados em memória (próxima cópia, nova tentativa depois de falha) são descartados quando ficam mais distantes do que o intervalo que os criou. Na leitura, a cópia "mais recente" é a que foi mais longe na fila (`outbox_seq`); o carimbo de hora só desempata.

### 3.3 Quando o disco falha

A réplica nunca derruba uma venda. Os ganchos `on_event` e `on_cash_closed` não fazem E/S nem levantam erro; a gravação acontece em uma thread própria (`pdv-mirror`).

| Situação | O que acontece |
|---|---|
| Pendrive retirado | `state="error"`, mensagem geral na tela (`message`: "Não foi possível gravar a cópia de segurança. Confira se o disco ou pendrive está conectado.") e o motivo em `last_error` ("O disco da cópia de segurança não foi encontrado."). A pasta não é recriada em outro lugar. Ao voltar, o diário se completa sozinho. |
| Disco cheio | `state="error"`, a mesma mensagem geral e `last_error` = "O disco da cópia de segurança está cheio." O diário continua tentando; a cópia do banco é tentada de novo depois de 1 min. |
| Recuperação em andamento neste banco (`restore_state` diferente de `done`) | Nada é gravado: o banco ainda não tem nada próprio, e uma réplica nova e vazia apareceria na frente da cópia antiga na tela de recuperação. A gravação começa quando a recuperação termina. |
| Banco apagado e recuperado na mesma pasta (outro `device_id`) | Os contadores de `mirror.json` eram do banco anterior e são zerados; começa a réplica do banco novo, em outra subpasta. |
| Pendrive trocado por um vazio | Começa uma geração nova com cópia completa na hora. |
| Chave da empresa diferente da gravada no disco | Geração nova; os arquivos da chave antiga são apagados depois que a cópia nova é conferida. |
| Banco do caixa restaurado de um backup mais antigo | A geração que estava no disco é movida para `anterior-<ms>/` (pode conter vendas que o banco restaurado não tem) e começa outra. |
| Queda de energia no meio de uma linha do diário | O escritor isola a sobra com uma quebra de linha e continua; o leitor ignora a linha ilegível e avisa. |

`status(store)` lê só o estado local (`mirror.json`, ao lado do banco, permissão 600) e nunca toca o disco de destino, então a tela não trava com pendrive lento ou ausente. Estados: `off`, `ok` ("Cópia de segurança em dia"), `late` ("Cópia de segurança atrasada": evento há mais de 1 min fora do diário ou nenhuma cópia do banco há mais de 26 h), `error`, `no_key`. `/api/session` leva esse estado em `mirror`, e a barra da tela de venda mostra um aviso enquanto ele for `error`, `late` ou `no_key`.

`mirror.json`: `{"v":1,"directory","enabled","last_snapshot_at","last_journal_at","journal_seq","last_error","last_error_at","device_id"}` (`device_id` é o do banco que gravou por último; não é segredo).

### 3.4 Escolha do disco

`configure(store, directory, allow_same_volume=False)`:

- a pasta precisa existir, com caminho completo;
- grava e apaga um arquivo de teste;
- recusa pasta no mesmo disco do banco (mesma letra no Windows, mesmo `st_dev` nos demais) com "Escolha outro disco ou um pendrive: esta pasta fica no mesmo disco do caixa." Só os ambientes `development` e `test` liberam;
- tenta a primeira cópia na hora; se não der (sem chave, disco cheio), `configure` **não levanta**: devolve o status com `state` `no_key` ou `error` (e `last_error`). A tela só confirma "A primeira cópia já foi feita" com `state="ok"`, e a auditoria local registra "ligada, primeira cópia falhou (motivo)";
- `directory=None` desliga. Os arquivos já gravados ficam no disco.

`drives(store)` lista as unidades: no Windows, letras removíveis, fixas e de rede (`E: · pendrive · 12 GB livres`); no macOS, `/Volumes/*`; no Linux, `/media`, `/run/media` e `/mnt`.

### 3.5 Leitura na recuperação

`read_pending(directory, key, identity, replica_id=None)` devolve a fila que o computador antigo não chegou a enviar:

- as linhas **não confirmadas** da `outbox` dentro da cópia do banco mais recente que estiver inteira;
- **mais** todo evento do diário com `seq` maior que o dessa cópia;
- sem repetir `event_id`, em ordem de `seq`.

Regras:

- sem `replica_id`, escolhe a réplica mais recente do mesmo `terminal_id` gravada por **outro** computador (`device_id` diferente do atual). `replica_id` é o `device_id` de quem gravou;
- a pasta informada pode ser a raiz do pendrive, `PDV2-copia-de-seguranca`, a pasta do terminal ou a da réplica;
- cópia do banco danificada ou cortada cai para a anterior, com aviso; sem nenhuma cópia legível, usa só o diário (o que estava apenas na cópia não volta);
- linha do diário com prefixo que não bate com o conteúdo cifrado é ignorada, com aviso;
- os avisos vêm em `warnings` e a rota `/api/restore/import-backup` os repassa: a tela os mantém visíveis e deixa ler outra cópia;
- um evento confirmado pela nuvem depois da cópia ainda é devolvido: a nuvem deduplica por `event_id` e pelo hash do objeto.

Erros, sempre `MirrorError` (subclasse de `TerminalError`):

| HTTP | Mensagem |
|---|---|
| 404 | Nenhuma cópia de segurança deste caixa foi encontrada nesta pasta. |
| 409 | Esta cópia de segurança foi feita com outra chave da empresa. |
| 409 | Esta cópia é de outra loja. |
| 409 | Esta cópia é de outro caixa. (só quando `replica_id` aponta para a réplica de outro terminal) |
| 409 | Conecte este caixa à internet para liberar a cópia de segurança. (chave ausente ou inválida) |

A fila lida **não é aplicada direto no banco local**. `store.import_outbox_from_mirror` só a insere na `outbox` com o `event_id` e o objeto intactos; ela sobe para a nuvem e volta por uma segunda passada do restore.

### 3.6 Interface

```python
class MirrorError(TerminalError): ...
def start(store) -> MirrorWorker            # thread daemon "pdv-mirror"; uma por banco
MirrorWorker.stop(timeout=5.0)
def on_event(store) -> None                 # depois do COMMIT de quem inseriu na outbox
def on_cash_closed(store) -> None           # depois do COMMIT do fechamento de caixa
def tick(store, now=None, force_snapshot=False) -> dict   # uma passada síncrona (testes); now em ms
def status(store) -> dict
def is_enabled(store) -> bool
def drives(store) -> list[dict]
def configure(store, directory, allow_same_volume=False) -> dict
def snapshot_now(store) -> dict             # {"name","created_at","size_bytes","outbox_seq"}
def candidates(identity, directory=None) -> list[dict]
def read_pending(directory, key, identity, replica_id=None) -> dict
```

`candidates()` devolve uma lista de `{directory, replica_id, terminal_id, terminal_name, shop_name, updated_at, journal_seq, last_snapshot_at, matches, current_device}`. `directory` é a pasta **varrida** (a mesma que deve voltar em `read_pending`), não a da réplica; `replica_id` é o `device_id` de quem gravou. Ordem: as deste caixa primeiro, a gravada por este mesmo banco (`current_device`) por último entre elas, depois as mais recentes. A tela não deixa escolher a `current_device`: ela só tem o que já está na fila daqui.

`read_pending()` devolve `{terminal_id, replica_id, snapshot: {name, created_at, outbox_seq} | None, journal_last_seq, events: [{seq, event_id, created_at, event}], warnings: [texto]}`.

O módulo só importa a biblioteca padrão, `cryptography` e `TerminalError`. `store.py` chama os ganchos por import tardio e engole qualquer erro; `sync.py` importa `mirror` protegido, como faz com o `updater`.

## 4. Troca de computador

### 4.1 Nuvem

- `POST /api/v1/terminals/<id>/transfer` (owner, admin) gera o **código de recuperação** `XXXX-XXXX-XXXX` (30 min) para o mesmo `Terminal.id`. O token atual continua valendo até o computador novo parear.
- No `pair` com outro `device_id`, o token antigo vira **só-envio pelo prazo da licença offline do plano mais 1 dia, nunca menos de 7** (31 dias no plano padrão): `terminal/sync` continua aceitando a fila do computador velho; `lease`, `bootstrap`, `heartbeat` e `restore` respondem 403 `terminal_transferred`. A data exata vem em `send_only_until` e o dono pode encerrar antes com `PATCH terminals/<id> {"end_send_only": true}`. O prazo acompanha a licença porque o antigo, sem internet, continua vendendo com a licença offline até se reconectar.
- Cada computador substituído tem a própria linha em `TerminalFormerToken` (token, `device_id`, prazo). Uma segunda troca, ou uma recuperação da qual se desistiu, não tira o envio do computador que ainda guarda vendas. `Terminal.previous_*` e `transferred_at` espelham a troca mais recente para o painel.
- O `pair` responde `recovery: true` sempre que houve troca de computador, mesmo sem nenhum evento na nuvem: um caixa que nunca sincronizou ainda tem a cópia de segurança para ler.
- `POST /api/v1/terminal/release` (token do computador atual) desfaz a última troca enquanto o computador anterior ainda pode enviar: ele volta a ser o caixa e a auditoria ganha `terminal.transfer_undone`.
- O mesmo caixa pode acabar fechado nos dois computadores. O segundo `cash.closed` com valores diferentes abre o conflito `cash_closed_twice` (com os dois fechamentos) e vale o mais recente. `terminal.config` vindo do token só-envio não substitui a configuração do computador novo.
- `GET /api/v1/terminal/restore?cursor=&limit=` devolve, em páginas retomáveis, a configuração, os caixas, as vendas (90 dias, mais as com fiado em aberto e as do caixa aberto) e os recebimentos de fiado. A última página traz `totals`.
- O evento `terminal.config` guarda impressoras e o estado da réplica, para voltar no restore e aparecer no painel ("Cópia em 2º disco: Ligada / Desligada").
- `pair` e `lease` entregam `mirror_key`; `pair`, `lease` e `bootstrap` anunciam `capabilities`. O terminal só usa o que a nuvem anunciou, por isso **a nuvem é implantada primeiro**.

### 4.2 Terminal

`restore_state` em `meta`: `pending` → `restoring` → `awaiting_backup` → (`importing` → `restoring`, passada 2) → `done`. Enquanto não chega em `done`, vender e abrir ou fechar caixa ficam bloqueados com "Este caixa ainda está trazendo os dados da loja. Aguarde terminar para vender."

`apply_restore` grava vendas, caixas, devoluções e recebimentos sem gerar evento, sem imprimir e sem mexer em estoque ou saldo de cliente (esses vêm do bootstrap). Ao final compara as contagens com os `totals` da nuvem; se não baterem, recomeça, e na terceira tentativa pausa e pede "Tentar de novo".

No computador antigo, o primeiro 403 `terminal_transferred` marca o caixa como transferido: ele para de vender e segue enviando o que faltava até a data informada pela nuvem, que a tela mostra. O caixa que estava aberto continua no computador novo e é fechado lá. Quando o prazo vence ou é encerrado no painel (403 com `send_only_until: null`), o terminal grava `send_only_closed`, para de reenviar e a tela passa a dizer "O prazo para este computador enviar terminou e N registro(s) ficaram só aqui": o caminho é a cópia de segurança no computador novo ou o suporte. A cada 5 minutos o computador transferido pergunta à nuvem (um `heartbeat`) se a troca foi desfeita; se for aceito, volta a ser o caixa.

O pareamento vale mesmo com o **relógio do computador novo errado**: a nuvem já consumou a troca, então a licença é julgada na própria data de emissão (assinatura e identidade continuam conferidas) e o caixa mostra "Acerte a data e a hora deste computador" em vez de ficar sem parear.

**Rotas locais da recuperação** (`terminal/app.py`; todas respondem `{"restore": {state, active, pass, applied, total, percent, counts, error, paused, started_at, finished_at, import}}` além do que está indicado):

| Rota | Corpo | Observação |
|---|---|---|
| `POST /api/restore/start` | `{cloud_url?, license_key, code}` | Pareia com o código de recuperação. Responde também `paired`, `recovery`, `bootstrap` e, se só a primeira carga do catálogo falhou, `warning`. |
| `GET /api/restore/status` | | |
| `POST /api/restore/step` | `{}` | Avança até 3 páginas. Falha de conexão volta em `restore.error` com HTTP 200; 409 quando a recuperação já terminou. |
| `POST /api/restore/retry` | `{}` | "Tentar de novo": limpa pausa, erro, tentativas e cursor. |
| `POST /api/restore/abandon` | `{confirm: true, manager_pin}` | "Desistir da recuperação". Responde `paired:false` e `released`. |
| `GET /api/restore/backups?directory=` | | `{supported, candidates}` (itens de `candidates()`, §3.6). |
| `POST /api/restore/import-backup` | `{directory, replica_id}` | `{imported, already_present, foreign, invalid, first_seq, last_seq, warnings}`. |
| `POST /api/restore/skip-backup` | `{}` | "Não tenho, continuar". |

As rotas de cópia (`backups` e `import-backup`) valem em `awaiting_backup`, em `importing` e na segunda passada: dá para ler outra cópia enquanto a primeira sobe, e a fila nunca recebe o mesmo `event_id` duas vezes.

**Desistir da recuperação.** Disponível enquanto a recuperação não terminou; exige o PIN de um gerente e é recusada se a fila tiver registros da cópia de segurança esperando para subir (estado `importing`). O terminal pede à nuvem `POST terminal/release` (melhor esforço: sem internet segue assim mesmo), apaga o que tinha voltado, despareia e **gera um `device_id` novo**, para poder recuperar outro caixa da mesma empresa sem esbarrar em "Dispositivo já vinculado". Se a nuvem desfez a troca, o computador antigo volta a vender sozinho; se não, ele continua em só-envio.

## 5. Limites conhecidos

- **A réplica não substitui a nuvem.** Ela cobre a janela entre a venda e o envio. Loja de um caixa, sem internet e sem segundo disco, ainda perde o que não sincronizou se o disco morrer.
- **A janela de perda com réplica é de segundos**, mas não é zero: a venda é confirmada no banco local antes de chegar ao pendrive.
- **O serviço do Windows roda como sistema.** Unidade de rede mapeada por usuário não aparece para ele; use pendrive, HD externo ou caminho UNC acessível à conta do computador.
- **Pendrive em FAT/exFAT** não guarda permissão por arquivo. O conteúdo é cifrado; o `manifest.json` mostra nome da empresa, da loja e do caixa.
- **Não há rotação da chave da cópia de segurança** nesta fase, nem tela para digitá-la no terminal.
- **O que o computador antigo enviar depois de o novo terminar a restauração** fica só no painel.
- **Depois que a recuperação termina (`done`), não dá mais para ler uma cópia de segurança** por esta tela; vendas que tenham ficado só em um pendrive não lido precisam do suporte.
- **Fechamento do mesmo caixa nos dois computadores** não é impedido: vira o conflito `cash_closed_twice` para o gestor conferir.
- **Caixas "abertos" antigos na nuvem**, além do mais recente, não são restaurados.
- **Restaurar o banco inteiro a partir da cópia do segundo disco** (sem passar pela nuvem) não existe nesta fase; a cópia serve para recuperar a fila e para a promoção de estação da fase 3.
- Nada disto foi validado em Windows físico; ver a lista em [INSTALADOR-WINDOWS.md](INSTALADOR-WINDOWS.md).

## 6. Testes

- Réplica: `python -m unittest terminal.tests.test_mirror` (cópia e conferência, arquivos ilegíveis sem a chave, chave errada, disco ausente, cheio e trocado, linha truncada, cópia corrompida, rotação, cadência, importação sem duplicar, ganchos sem E/S).
- Recuperação no terminal: `terminal/tests/test_recuperacao_fase2.py` e `terminal/tests/test_contract_restore.py` (ponta a ponta com a nuvem).
- Ponta a ponta sem dublê (nuvem Django, terminal e `mirror.py` de verdade): `terminal/tests/test_e2e_replica_recuperacao.py` cobre réplica → perda do banco → importação da fila sem duplicar, cópia danificada mais segundo disco, banco apagado no mesmo computador, desistência com devolução do caixa e segunda troca.
- Nuvem: `cloud/core/tests_fase2_recuperacao.py`.
