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
3. Pronto. A primeira cópia é feita na hora. Depois disso cada venda vai para o pendrive em segundos e o caixa mostra "Cópia de segurança em dia".

Se o pendrive for retirado, o caixa **continua vendendo** e mostra "Cópia de segurança atrasada" ou o aviso de disco não encontrado. Ao recolocar, a cópia se põe em dia sozinha.

**Troquei de computador**

1. No painel: **Terminais → Trocar de computador**. Aparecem a chave da licença e um código de recuperação (vale 30 minutos).
2. No computador novo: instale o PDV2, abra o ícone, escolha **Recuperar minha loja** e digite a chave e o código.
3. Espere a barra "Trazendo seus dados".
4. Se o computador antigo quebrou com vendas ainda não enviadas, escolha **Tenho uma cópia de segurança** e espete o pendrive. Senão, **Não tenho, continuar**.

Voltam: catálogo, clientes e saldos, vendas dos últimos 90 dias, fiado em aberto, devoluções, recebimentos, o caixa aberto e a configuração de impressão. Não voltam: carrinhos suspensos e fila de impressão.

## 2. Dois segredos diferentes

| Nome | O que é | Onde aparece |
|---|---|---|
| **Chave da licença** | `PDV2-XXXXXX-…`. Identifica a empresa. | Painel; é digitada em "Recuperar minha loja". |
| **Chave da cópia de segurança** (`mirror_key`) | Segredo por empresa que cifra a réplica. | O terminal recebe da nuvem no pareamento e a cada renovação de licença. O dono pode vê-la **uma única vez** no painel. |

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
- **`manifest.json`:** `{"format":"pdv2-mirror","v":1,"tenant_id","shop_id","terminal_id","device_id","tenant_name","shop_name","terminal_name","key_id","snapshots":[{"name","created_at","outbox_seq","size_bytes","sha256"}],"journal_seq","journal_segments":[{"name","first_seq"}],"updated_at","app_version"}`. Trocado de forma atômica.
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

### 3.3 Quando o disco falha

A réplica nunca derruba uma venda. Os ganchos `on_event` e `on_cash_closed` não fazem E/S nem levantam erro; a gravação acontece em uma thread própria (`pdv-mirror`).

| Situação | O que acontece |
|---|---|
| Pendrive retirado | `state="error"`, "O disco da cópia de segurança não foi encontrado." A pasta não é recriada em outro lugar. Ao voltar, o diário se completa sozinho. |
| Disco cheio | `state="error"`, "O disco da cópia de segurança está cheio." O diário continua tentando; a cópia do banco é tentada de novo depois de 1 min. |
| Pendrive trocado por um vazio | Começa uma geração nova com cópia completa na hora. |
| Chave da empresa diferente da gravada no disco | Geração nova; os arquivos da chave antiga são apagados depois que a cópia nova é conferida. |
| Banco do caixa restaurado de um backup mais antigo | A geração que estava no disco é movida para `anterior-<ms>/` (pode conter vendas que o banco restaurado não tem) e começa outra. |
| Queda de energia no meio de uma linha do diário | O escritor isola a sobra com uma quebra de linha e continua; o leitor ignora a linha ilegível e avisa. |

`status(store)` lê só o estado local (`mirror.json`, ao lado do banco, permissão 600) e nunca toca o disco de destino, então a tela não trava com pendrive lento ou ausente. Estados: `off`, `ok` ("Cópia de segurança em dia"), `late` ("Cópia de segurança atrasada": evento há mais de 1 min fora do diário ou nenhuma cópia do banco há mais de 26 h), `error`, `no_key`.

`mirror.json`: `{"v":1,"directory","enabled","last_snapshot_at","last_journal_at","journal_seq","last_error","last_error_at"}`.

### 3.4 Escolha do disco

`configure(store, directory, allow_same_volume=False)`:

- a pasta precisa existir, com caminho completo;
- grava e apaga um arquivo de teste;
- recusa pasta no mesmo disco do banco (mesma letra no Windows, mesmo `st_dev` nos demais) com "Escolha outro disco ou um pendrive: esta pasta fica no mesmo disco do caixa." Só os ambientes `development` e `test` liberam;
- faz a primeira cópia na hora;
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

O módulo só importa a biblioteca padrão, `cryptography` e `TerminalError`. `store.py` chama os ganchos por import tardio e engole qualquer erro; `sync.py` importa `mirror` protegido, como faz com o `updater`.

## 4. Troca de computador

### 4.1 Nuvem

- `POST /api/v1/terminals/<id>/transfer` (owner, admin) gera o **código de recuperação** `XXXX-XXXX-XXXX` (30 min) para o mesmo `Terminal.id`. O token atual continua valendo até o computador novo parear.
- No `pair` com outro `device_id`, o token antigo vira **só-envio por 7 dias**: `terminal/sync` continua aceitando a fila do computador velho; `lease`, `bootstrap`, `heartbeat` e `restore` respondem 403 `terminal_transferred`.
- `GET /api/v1/terminal/restore?cursor=&limit=` devolve, em páginas retomáveis, a configuração, os caixas, as vendas (90 dias, mais as com fiado em aberto e as do caixa aberto) e os recebimentos de fiado. A última página traz `totals`.
- O evento `terminal.config` guarda impressoras e o estado da réplica, para voltar no restore e aparecer no painel ("Cópia em 2º disco: Ligada / Desligada").
- `pair` e `lease` entregam `mirror_key`; `pair`, `lease` e `bootstrap` anunciam `capabilities`. O terminal só usa o que a nuvem anunciou, por isso **a nuvem é implantada primeiro**.

### 4.2 Terminal

`restore_state` em `meta`: `pending` → `restoring` → `awaiting_backup` → (`importing` → `restoring`, passada 2) → `done`. Enquanto não chega em `done`, vender e abrir ou fechar caixa ficam bloqueados com "Este caixa ainda está trazendo os dados da loja. Aguarde terminar para vender."

`apply_restore` grava vendas, caixas, devoluções e recebimentos sem gerar evento, sem imprimir e sem mexer em estoque ou saldo de cliente (esses vêm do bootstrap). Ao final compara as contagens com os `totals` da nuvem; se não baterem, recomeça, e na terceira tentativa pausa e pede "Tentar de novo".

No computador antigo, o primeiro 403 `terminal_transferred` marca o caixa como transferido: ele para de vender, deixa fechar o caixa e segue enviando o que faltava.

## 5. Limites conhecidos

- **A réplica não substitui a nuvem.** Ela cobre a janela entre a venda e o envio. Loja de um caixa, sem internet e sem segundo disco, ainda perde o que não sincronizou se o disco morrer.
- **A janela de perda com réplica é de segundos**, mas não é zero: a venda é confirmada no banco local antes de chegar ao pendrive.
- **O serviço do Windows roda como sistema.** Unidade de rede mapeada por usuário não aparece para ele; use pendrive, HD externo ou caminho UNC acessível à conta do computador.
- **Pendrive em FAT/exFAT** não guarda permissão por arquivo. O conteúdo é cifrado; o `manifest.json` mostra nome da empresa, da loja e do caixa.
- **Não há rotação da chave da cópia de segurança** nesta fase, nem tela para digitá-la no terminal.
- **O que o computador antigo enviar depois de o novo terminar a restauração** fica só no painel.
- **Caixas "abertos" antigos na nuvem**, além do mais recente, não são restaurados.
- **Restaurar o banco inteiro a partir da cópia do segundo disco** (sem passar pela nuvem) não existe nesta fase; a cópia serve para recuperar a fila e para a promoção de estação da fase 3.
- Nada disto foi validado em Windows físico; ver a lista em [INSTALADOR-WINDOWS.md](INSTALADOR-WINDOWS.md).

## 6. Testes

- Réplica: `python -m unittest terminal.tests.test_mirror` (cópia e conferência, arquivos ilegíveis sem a chave, chave errada, disco ausente, cheio e trocado, linha truncada, cópia corrompida, rotação, cadência, importação sem duplicar, ganchos sem E/S).
- Recuperação no terminal: `terminal/tests/test_recuperacao_fase2.py` e `terminal/tests/test_contract_restore.py` (ponta a ponta com a nuvem).
- Nuvem: `cloud/core/tests_fase2_recuperacao.py`.
