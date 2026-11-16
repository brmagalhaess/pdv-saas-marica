# Vários caixas na mesma loja (fase 3)

Este documento descreve a fase 3 de [ARQUITETURA-OFFLINE-LOCAL.md](ARQUITETURA-OFFLINE-LOCAL.md): uma loja com mais de um caixa. A primeira parte é o guia do lojista; a segunda é a parte técnica da **réplica pela rede** e da **promoção** (`terminal/replica_net.py`, `terminal/promotion.py`, e o que `terminal/mirror.py` ganhou). A escuta de rede, o vínculo, a ponte e a tela do caixa adicional estão na [seção 6](#6-parte-técnica-rede-e-tela-do-caixa-adicional) (`terminal/lan.py`, `terminal/station.py`); o modelo local (tabela `stations`, rotas do principal) em [TERMINAL-V2.md](TERMINAL-V2.md); instalação e firewall em [INSTALADOR-WINDOWS.md](INSTALADOR-WINDOWS.md); a réplica em segundo disco, que usa os mesmos arquivos, em [RECUPERACAO-E-REPLICA.md](RECUPERACAO-E-REPLICA.md).

Dois nomes, sempre os mesmos:

| Na tela | No código | O que é |
|---|---|---|
| **Caixa principal** | servidor (`server`) | O computador que guarda os dados da loja e fala com a nuvem. Só existe um por loja. |
| **Caixa adicional** | estação (`station`) | Um computador que vende pela rede da loja, usando os dados do principal. Guarda uma cópia de segurança dele. |

## 1. Para o lojista

### 1.1 Adicionar um caixa

1. Confira no painel, em **Terminais**, quantos caixas adicionais o seu plano inclui.
2. Ligue o computador novo na **mesma rede** do caixa principal (de preferência por cabo) e instale o PDV2 nele.
3. Abra o PDV2 no computador novo e escolha **Este é um caixa adicional (a loja já tem um caixa principal)**.
4. No caixa principal, entre como gerente e abra **Sistema → Caixas da loja → Adicionar caixa**. Aparece um código de 6 números, que vale por 5 minutos.
5. Digite o código no computador novo, dê um nome a ele (por exemplo "Caixa 2") e toque em **Conectar este caixa**.

Pronto: o caixa adicional mostra a mesma tela de venda. Estoque, fiado e preços são os mesmos nos dois, na hora. Cada caixa abre e fecha o **seu** caixa de dinheiro e usa a **sua** impressora.

Se o computador novo não achar o principal sozinho, toque em **Não é este computador? Digitar o endereço** e digite o endereço que aparece na tela do principal (por exemplo `192.168.0.10`). Se a rede tiver mais de um caixa principal (duas lojas no mesmo prédio, dois terminais da mesma loja), a tela lista os que encontrou para você tocar no certo; ela nunca escolhe sozinha. Se mesmo assim não achar, confira se os dois estão na mesma rede e se o firewall do Windows do principal tem a regra **PDV2 - Servidor da loja** (o instalador ou o próprio PDV2 a criam; vale para rede Particular, Domínio e Pública, sempre só para a rede local). O cartão **Caixas da loja** do principal avisa quando o firewall não liberou ou quando a rede entre caixas não abriu.

Cada caixa responde pela própria gaveta: uma venda só pode ser **cancelada no caixa que a fez**. Em outro caixa, use **Devolver** (o dinheiro sai da gaveta de quem devolve).

Poucos minutos depois de conectado, o caixa adicional passa a guardar uma **cópia de segurança do caixa principal**. No principal, em **Sistema → Caixas da loja**, cada caixa mostra "Cópia de segurança em dia" ou "atrasada". Você não precisa fazer nada: a cópia se atualiza sozinha a cada venda.

### 1.2 O caixa principal ficou fora do ar

O caixa adicional mostra **O caixa principal está fora do ar** e **para de vender**. Isso é de propósito: é o que garante que estoque e fiado nunca fiquem diferentes entre os caixas.

- O que já estava lançado na tela **não se perde**: quando o principal voltar, o caixa adicional volta sozinho e a venda continua de onde parou.
- Quase sempre é coisa simples: o computador principal desligou, reiniciou por causa de uma atualização do Windows, ou o cabo de rede soltou. A tela do caixa adicional diz o que encontrou em **Ver o que aconteceu**: "O computador principal está desligado ou fora da rede" (nenhuma resposta), "O computador principal está ligado, mas o PDV2 não está aberto nele" (a conexão foi recusada), "Este computador está sem rede" ou "O relógio deste computador está atrasado em relação ao do caixa principal".
- Se o principal estiver ligado mostrando a tela de restauração do banco, o adicional mostra **O caixa principal está com o banco de dados danificado** e não oferece assumir: chame o suporte.
- Enquanto isso, **venda pelo caixa principal**, se ele estiver funcionando.

Não toque em "Assumir como caixa principal" se o computador principal só está desligado ou sem rede. Ligue-o de novo.

Outras telas do caixa adicional:

- **Este caixa não está liberado para vender**: o plano da loja encolheu (o caixa mais novo fica de fora) ou outro terminal virou o caixa principal da loja no painel. A rede está funcionando e o caixa volta sozinho quando o plano for ampliado; não há o que assumir.
- **O caixa principal mudou de computador**: outro caixa assumiu como principal. Peça o código ao gerente no novo principal. Mesmo que o computador antigo volte a ser ligado, este caixa **não volta a vender por ele**.
- **Este computador não vai mais ser caixa adicional desta loja** (link no pé das telas acima e da tela de fora do ar): desfaz o vínculo. Depois disso aparece "Este computador é o caixa principal? Voltar", que leva à tela de ativação, onde fica **Recuperar minha loja**.

### 1.3 Assumir como caixa principal

Use **só** quando o computador principal não vai voltar agora: quebrou, queimou ou foi roubado.

1. No caixa adicional, na tela "O caixa principal está fora do ar" (na tela de venda, toque antes em **Ver o que aconteceu**), toque em **Assumir como caixa principal**. O botão fica desabilitado até completar um minuto sem resposta do principal, e não libera com **este** computador sem rede (sem rede não dá para saber se o principal parou).
2. Leia o aviso. A tela mostra de quando é a cópia que este caixa guarda ("Cópia deste caixa: de 14:32").
3. Digite o **PIN de um gerente** e confirme.
4. Espere os três passos: "Abrindo a cópia…", "Aplicando as últimas vendas…", "Preparando este computador…".
5. A tela volta para o login. Entre com o seu PIN: este computador agora é o caixa principal e **já vende**, com ou sem internet.

O que você precisa saber:

- **Depois de assumir, o computador antigo NÃO pode voltar a vender.** Se ele for consertado, não o religue na loja como caixa: fale com o suporte. Se for religado na mesma rede, ele encontra o novo principal, mostra "Outro computador assumiu como caixa principal" e para de vender; os caixas adicionais que já viram o novo principal também não voltam para ele.
- Vêm junto todas as vendas, o estoque, o fiado e os caixas que o principal tinha. O que pode faltar são as vendas dos **últimos segundos** antes de o principal parar, se ainda não tinham chegado a este caixa.
- O **caixa de dinheiro que estava aberto no computador antigo** aparece em **Sistema → Caixas da loja** como "Caixa principal anterior". Conte o dinheiro daquela gaveta e feche por ali.
- Errou o PIN cinco vezes? O assistente espera cinco minutos antes de aceitar outra tentativa.
- "Este caixa ainda não tem uma cópia completa do caixa principal": o caixa foi adicionado há muito pouco tempo e a primeira cópia não terminou. Neste caso o caminho é o da [troca de computador](RECUPERACAO-E-REPLICA.md): toque em **Este computador não vai mais ser caixa adicional desta loja**, depois em **Este computador é o caixa principal? Voltar** e, na tela de ativação, em **Recuperar minha loja**.
- "Atualize este caixa antes de assumir": o caixa principal usava uma versão do PDV2 com formato de banco mais novo (o primeiro ou o segundo número da versão é maior). Instale a versão nova neste computador e tente de novo. Versão de correção (só o terceiro número diferente, ex.: 2.1.0 e 2.1.7) não impede de assumir. Para não descobrir isso na hora ruim, o cartão **Caixas da loja** do principal avisa "Versão antiga (x.y.z): instale a versão nova neste caixa" enquanto o principal está no ar.
- "O relógio deste computador está atrasado em relação ao do caixa principal": acerte a data e a hora do Windows e tente de novo. A promoção recusa antes de trocar qualquer coisa, porque o banco promovido não venderia com o relógio atrasado mais de 5 minutos.

### 1.4 Regularizar com a nuvem

Depois de assumir, o novo caixa principal mostra um cartão fixo: **Este computador assumiu como caixa principal.** As vendas continuam normais, mas ainda não sobem para a nuvem. Quando tiver internet:

1. No painel, abra **Terminais → Trocar de computador**, no terminal desta loja. Aparecem a chave da licença e um código.
2. No caixa, digite a chave e o código no cartão e toque em **Regularizar agora**.

As vendas feitas desde então sobem sozinhas. Nada é lançado em dobro: a nuvem reconhece cada venda pelo identificador dela.

Não precisa ter pressa: o caixa vende com a licença que já tinha, pelo prazo sem internet do seu plano. Mas regularize assim que puder, porque até lá o painel não vê as vendas novas.

### 1.5 Religar os outros caixas

Os outros caixas adicionais passam a mostrar **O caixa principal agora é outro computador**. Em cada um:

1. No novo caixa principal: **Sistema → Caixas da loja → Adicionar caixa**.
2. Digite o código de 6 números no caixa adicional.

O caixa continua com o mesmo nome e, se estava com o caixa de dinheiro aberto, **continua aberto** com tudo o que já tinha.

### 1.6 Perguntas comuns

- **Posso usar tablet ou celular como caixa adicional?** Não nesta versão.
- **O caixa adicional funciona sem o principal?** Não. Ele depende do principal para vender; em troca, os dados nunca divergem.
- **E sem internet?** Funciona normalmente. Só o caixa principal fala com a nuvem, quando houver internet.
- **A cópia nos outros caixas substitui o pendrive?** Elas se somam. Loja com um caixa só usa o pendrive ou HD externo; loja com vários caixas tem a cópia em cada um deles e pode ter o pendrive também.

## 2. Parte técnica: réplica pela rede

Mesmos formatos da réplica em segundo disco (cópia `.pdv2snap`, diário de uma linha cifrada por evento) e mesma chave (`mirror_key`). Tudo trafega dentro do TLS da rede da loja, com o certificado do principal fixado no vínculo; além disso, cópia e diário já vão cifrados por Fernet.

### 2.1 Rotas `/lan/v1/replica` (no caixa principal)

A escuta TLS (`terminal/lan.py`) confere o token do caixa adicional e entrega o pedido a:

```python
replica_net.handle_request(store, station, method, path, query, headers, body=None)
    -> (status, cabeçalhos, corpo)      # corpo: bytes ou iterador de bytes; nunca levanta
```

`station` é `{"id","name"}`; `query` é um dicionário (valor simples ou lista); `headers` em qualquer caixa de letra. Erros têm sempre o corpo `{"error","code"}`.

| Rota | Resposta |
|---|---|
| `GET status` | 200 `{"v":1,"state","terminal_id","device_id","tenant_id","shop_id","tenant_name","shop_name","terminal_name","key_id","app_version","generation","outbox_seq","snapshot":{"name","created_at","outbox_seq","size_bytes","sha256"}\|null,"server_time_ms"}`. `state`: `ok` (há cópia pronta), `preparing` (a cópia está sendo feita), `no_key`, `restoring`. O `sha256` é o do arquivo em claro. `generation` é `meta.server_generation` (1 se nunca houve promoção; `finish_promotion` soma um). |
| `GET key` | 200 `{"key_id","key"}` com `Cache-Control: no-store`; 409 `no_key`. É o único lugar por onde a chave sai do principal. |
| `GET snapshot/<nome>` | 200, ou 206 com `Range: bytes=N-` (única forma aceita), `application/octet-stream`, `Content-Length`, `Accept-Ranges: bytes`. 404 `snapshot_gone` (nome desconhecido ou cópia já substituída); 416 `range_invalid`. |
| `GET journal?after=<seq>&after_id=<event_id>&wait=<0..25>&limit=<1..500>` | 200, corpo com as linhas `<seq>\t<event_id>\t<token Fernet>\n` da fila com `seq > after`, em ordem. Cabeçalhos `X-PDV-Journal-Last` (maior `seq` do principal), `X-PDV-Journal-Count` e `X-PDV-Journal-Key` (o `key_id` usado). Sem nada novo, espera até `wait` segundos e responde vazio. `after` maior que o maior `seq` do principal: 409 `journal_reset` com `outbox_seq`. `after_id` (opcional) é o `event_id` que quem pede tem na posição `after`: se o principal tem outro evento nesse `seq`, ou nenhum, também responde 409 `journal_reset` (quem pede seguiu outra linha do tempo). Parâmetro fora da faixa: 400. |
| `POST ack` | `{"journal_seq": int, "snapshot_outbox_seq": int\|null}` → 200 `{"ok": true}`. Os dois números também são aceitos na query (`?journal_seq=..&snapshot_outbox_seq=..`), para quando a escuta não repassa o corpo. |

Com o principal sem `mirror_key`, `status` responde `no_key` e as outras rotas 409 `no_key` ("Conecte o caixa principal à internet uma vez para liberar a cópia de segurança."). Com recuperação em andamento (`restore_state` diferente de `done`), `restoring`. **Nesses dois estados nenhuma cópia é gerada.**

### 2.2 A cópia de saída do principal

- Fica em `<pasta do banco>/replica-out/` (permissão 700): no máximo **2** arquivos `snapshot-<ms>.pdv2snap` e um `index.json` (nomes, tamanhos, `sha256`, `key_id`; nenhum segredo).
- É feita em thread própria (`pdv-replica-out`), **uma por vez** no processo, com os mesmos passos da fase 2: `backup` do SQLite para um temporário ao lado do banco (`.replica-*.tmp`) → `integrity_check` → remove `token` e `mirror_key` da cópia → cifra em blocos → relê e confere.
- Quando: um caixa adicional pede `status` e **não há cópia**; ou a cópia tem **15 min e a fila andou**; ou tem **24 h**; ou houve **fechamento de caixa** (o gancho só marca, a cópia sai no próximo `status`). Depois de uma falha, nova tentativa em 1 min.
- Vários caixas pedindo ao mesmo tempo recebem a mesma cópia: há uma só geração por vez.
- Cópia de outra chave, de outro banco (`device_id`) ou mais adiantada que a fila atual é descartada e refeita.
- Os ganchos `mirror.on_event` e `mirror.on_cash_closed` chamam os ouvintes registrados com `mirror.add_listener`; o do `replica_net` só acorda quem está em espera longa. **Nenhum faz E/S, e erro de ouvinte não chega à venda.**

`replica_net.status(store)` (tela do principal): `{"supported": true, "state": "off"|"ok"|"late", "message", "stations": {"<station_id>": {"journal_seq", "behind", "at"}}}`. O progresso é guardado em memória, por caixa, a cada pedido e a cada `ack`. `ok` = pelo menos um caixa adicional está com a cópia em dia; `late` = há caixas conhecidos e nenhum em dia (sem contato há mais de 60 s, ou com evento há mais de 60 s fora do diário dele); `off` = nenhum caixa puxou a cópia desde que o serviço subiu. Caixa sem contato há 24 h sai da lista. `replica_net.stop_all()` solta as esperas longas e esquece esse estado.

### 2.3 O puxador no caixa adicional

```python
replica_net.start_puller(data_dir, client_factory, *, interval=2.0) -> Puller   # thread "pdv-replica-pull"
Puller.stop(timeout=5.0); Puller.wake(); Puller.cycle(wait=0.0) -> bool          # uma passada síncrona
replica_net.puller_status(data_dir) -> {"state","message","journal_seq","server_seq",
                                        "last_snapshot_at","last_contact_at","last_error"}
```

`client_factory()` devolve um objeto com `request(method, path, *, body=None, headers=None, timeout=None)`; a resposta tem `.status`, `.headers` (chaves minúsculas), `.read(amount=-1)` e `.close()`. Qualquer exceção do cliente é tratada como falha de rede. O módulo não importa nada da camada de rede.

Arquivos, com o **mesmo desenho do segundo disco** (por isso `mirror.candidates`, `mirror.read_pending` e `mirror.restore_database` abrem a pasta sem mudança):

```
<dados>/replica/
    state.json                 600: identidade do principal, chave, contadores, último erro
    promotion.json             estado do assistente de promoção (seção 3)
    PDV2-copia-de-seguranca/<terminal_id>/<device_id>/
        manifest.json
        snapshot-<ms>.pdv2snap      as 2 mais recentes
        snapshot-<ms>.pdv2snap.part download em andamento
        journal-<primeiro_seq>.pdv2log
    anterior-<ms>/             gerações guardadas (ver abaixo); nunca são apagadas
```

A chave só existe em `state.json`. Não vai para log, `puller_status`, `promotion.json`, manifesto nem mensagem de erro.

Cada passada:

1. `status`. `no_key` ou `restoring`: anota e espera.
2. `terminal_id`/`device_id` diferentes dos guardados → a geração atual é guardada e começa outra.
   O mesmo quando `generation` é diferente da guardada em `state.json`: outro caixa assumiu como principal. O banco promovido mantém `terminal_id`, `device_id` e chave do principal antigo até a regularização, então só a geração (e a conferência do item 6) mostra que a linha do tempo é outra.
3. `key_id` diferente → busca a chave de novo; se já havia outra, a geração da chave antiga é guardada.
4. A fila do principal está atrás do diário daqui (ele voltou no tempo) → guarda a geração e começa outra. O mesmo acontece se `journal` responder 409 `journal_reset`: o pedido leva `after_id` com o `event_id` da última linha gravada aqui, e o principal recusa quando o evento dele nessa posição é outro (principal trocado por promoção ou restaurado de backup e que já vendeu de novo).
5. A cópia do principal foi mais longe que a daqui → download em `.part`. Conexão caiu: o que chegou fica, e a próxima passada **retoma por `Range`**. Completo: decifra inteiro e confere o `sha256`, a chave e a identidade; só então `os.replace`, manifesto e segmento novo do diário. Cópia que não confere é apagada e baixada de novo.
6. Diário: cada linha é validada com a chave, tem de ser do mesmo `terminal_id` e ter `seq` maior que o anterior. Só linha conferida é gravada, com `fsync`. A primeira linha recusada interrompe a leva e nada depois dela entra. Resposta cortada no meio: ficam as linhas completas; a seguinte recomeça do último `seq` gravado, sem repetir e sem buraco.
7. `ack`, e uma espera longa de até 25 s por novos eventos.

O diário **nunca pula**: depois da primeira cópia ele continua do último `seq` gravado, mesmo que chegue uma cópia mais adiantada. Um segmento antigo só é apagado quando a cópia mais velha guardada já o cobre inteiro.

Recuo depois de falha: 2 s, 4 s, 8 s, 16 s, 30 s (teto). Com o principal de volta, a primeira passada boa zera o recuo.

Estados de `puller_status`: `empty` ("Este caixa ainda não tem a cópia do caixa principal"), `ok` ("Cópia de segurança do caixa principal em dia"), `late` ("Cópia de segurança atrasada": sem contato há mais de 60 s ou diário atrás há mais de 60 s), `no_key`, `error` (não conseguiu gravar no disco deste computador, ou recebeu dado que não confere com a chave).

**Gerações guardadas.** `anterior-<ms>/PDV2-copia-de-seguranca/…` guarda o que este caixa tinha quando o principal mudou de identidade, de chave, de geração (promoção de outro caixa) ou voltou no tempo. Pode conter vendas que o principal atual não tem. Os leitores automáticos não enxergam essa pasta; ela serve ao suporte, que a abre com a chave da empresa.

### 2.4 O que `terminal/mirror.py` ganhou

A interface da fase 2 não mudou. Novidades, usadas pela réplica pela rede e pela promoção:

```python
key_from_meta(meta) -> {"key_id","key"} | None
make_snapshot(store, directory, now=None) -> {"name","created_at","outbox_seq","size_bytes","sha256"}
verify_snapshot(path, key) -> {"header","size","sha256","chunks"} | None
journal_lines(store, after, limit=500) -> [bytes]         # linhas prontas, cifradas com a chave atual
parse_journal_line(line, key) -> registro | None
max_seq(store) -> int
add_listener(callback); remove_listener(callback)         # callback(kind, store); kind: "event" | "cash_closed"
restore_database(directory, key, identity, target_path, replica_id=None)
    -> {"snapshot": {"name","created_at","outbox_seq"}, "events": [{"seq","event_id","created_at","event"}],
        "journal_last_seq": int, "app_version": str, "warnings": [str]}
```

`restore_database` decifra em `target_path` a cópia do banco mais recente que estiver inteira (se a mais nova estiver danificada, cai para a anterior com o aviso "A cópia mais recente do banco estava danificada; foi usada a anterior."), confere `PRAGMA integrity_check`, garante que o banco não tem `token` nem `mirror_key`, e devolve **só** os eventos do diário com `seq` maior que o da cópia. Sem nenhuma cópia legível levanta `MirrorError` 409 e não deixa arquivo. Buraco na numeração do diário vira o aviso "Faltam registros no diário desta cópia; as vendas mais recentes podem não ter voltado."

## 3. Parte técnica: promoção

```python
promotion.status(data_dir) -> {"available","reason","state","step","error","shop_name","terminal_name",
                               "last_snapshot_at","journal_seq","last_contact_at","locked_until"}
promotion.verify_manager(data_dir, pin) -> {"operator_id","name","role"}
promotion.promote(data_dir, pin, *, station_id, station_name, store_factory=None) -> dict
promotion.pending(data_dir) -> bool
```

- **`verify_manager`**: decifra a cópia em uma pasta temporária dentro de `replica/` (apagada em seguida) e confere o PIN contra os operadores **ativos** com papel `owner`, `admin` ou `manager`. O PIN de um operador de caixa não serve. Cinco erros seguidos → 429 por cinco minutos (guardado em `promotion.json`; nem o PIN certo passa nesse intervalo). Erros: 403 "PIN de gerente inválido"; 429 "Muitas tentativas; aguarde cinco minutos"; 409 "Este caixa ainda não tem uma cópia completa do caixa principal. Não é possível assumir."; 409 "Atualize este caixa antes de assumir: o caixa principal usava uma versão mais nova." Este último só quando o número de compatibilidade do banco, `(maior, menor)` da versão do PDV2, da cópia é maior que o deste computador; a correção (terceiro número) não conta, porque o principal se atualiza sozinho de madrugada e o caixa adicional não. **Regra para quem publica versão: mudança de formato do banco sobe o segundo número.** `promotion.status` devolve `available`/`reason` o tempo todo, inclusive com o principal no ar, para a tela poder avisar "atualize este caixa" antes de precisar.
- **`promote`** (uma por vez: trava de arquivo `replica/promotion.lock`, que o sistema solta se o processo cair). Estado em `promotion.json`: `state` (`idle`, `running`, `done`, `failed`), `step`, `error`, horários e o resumo do resultado.
  1. `verifying`: `verify_manager`.
  2. `restoring`: `mirror.restore_database` em `<dados>/terminal.sqlite3.promoting`; o banco tem de ser o do principal esperado (`terminal_id` e `device_id`).
  3. `replaying`: `Store(.promoting)` (as migrações rodam) e `apply_journal_events(eventos)`, que aplica o efeito local de cada evento **sem criar evento novo**.
  4. `finalizing`: `finish_promotion(station_id, station_name, chave, operador)`; o banco é fechado em um arquivo só e conferido; se já existia `<dados>/terminal.sqlite3`, ele é renomeado para `terminal-antes-da-promocao-<ms>.sqlite3` (**nunca apagado**); `active-profile.json` é removido; `os.replace` põe o banco promovido no lugar.
  5. `done`.

  Devolve `{"promoted": true, "applied", "journal_last_seq", "snapshot_at", "previous_server_cash_open", "warnings"}`. **Falha em qualquer passo apaga o `.promoting`, grava `failed` com a mensagem e deixa este computador como caixa adicional**, com a cópia intacta; dá para tentar de novo. Repetir depois de `done` é recusado com 409 "Este computador já assumiu como caixa principal."
- **`pending`**: verdadeiro quando o estado é `done`, `terminal.sqlite3` existe e `<dados>/mode.json` **não** foi gravado depois do fim da promoção (`changed_at`, em segundos, maior que o segundo de `finished_at`). Cobre só a queda de energia entre a troca do banco e a troca de modo: na partida em modo de caixa adicional, quem vê `pending` sobe como caixa principal. Assim que `station.py` grava o modo, a promoção vira história: `status` volta a `idle` e o computador pode, mais tarde, voltar a ser caixa adicional (por exemplo depois de uma troca de computador) e até assumir de novo. Para assumir de novo ele precisa ter falado com o principal atual depois da promoção anterior (`last_contact_at` maior que `promoted_at` de `promotion.json`); antes disso responde "ainda não tem uma cópia completa", para a cópia velha nunca passar por cima do banco promovido. Com `PDV_MODE` no ambiente e sem `mode.json` (desenvolvimento), `pending` continua verdadeiro.
- `promotion.py` **não** troca o modo nem reinicia o serviço; isso é de `terminal/station.py`, que também confirma antes que o principal continua sem responder.

Depois da promoção o banco está em `promotion_state = awaiting_cloud`: vende com a licença que veio na cópia e não fala com a nuvem até a regularização (`POST /api/promotion/regularize`, seção 1.4), que usa o código de recuperação da troca de computador. A nuvem troca o `device_id`, o token do computador antigo vira só-envio e a fila sobe, deduplicada por `event_id`.

## 4. Limites conhecidos

- **Com o principal fora do ar o caixa adicional não vende.** É decisão de projeto; a alternativa (venda independente por caixa) deixaria estoque e fiado aproximados.
- **Janela de perda na promoção: segundos.** A venda é confirmada no principal antes de chegar à cópia do caixa adicional. O que o principal vendeu e ainda não tinha chegado a **este** caixa não entra no banco promovido. Se outro caixa adicional recebeu essas vendas, elas ficam na pasta `anterior-<ms>` dele, para o suporte.
- **Caixa adicional recém-ligado não pode assumir** antes de a primeira cópia terminar.
- **Caixa adicional com versão antiga não pode assumir** quando o principal já usava um formato de banco mais novo (primeiro ou segundo número da versão maior). O caixa adicional não se atualiza sozinho nesta fase; depois de uma versão que muda o segundo número é preciso instalar a versão nova em cada caixa adicional (o instalador é o mesmo do principal, ver INSTALADOR-WINDOWS.md §8). O cartão "Caixas da loja" do principal compara a versão de cada caixa (`app_version`, de `X-PDV-Station-Version`) com a dele (`version` de `/api/stations`) e avisa com antecedência.
- **O computador antigo não pode voltar.** Se ele ressuscitar na mesma rede, encontra o novo principal pela busca, confere a prova de geração (seção 6.4), grava `superseded_at` e para de vender (`lease_status` = `superseded`); as vendas que ficaram só nele precisam do suporte. Religado **fora** da rede da loja e sem internet, ele não tem como saber e continua vendendo com a licença que tinha até a regularização do novo principal chegar a ele pela nuvem (aí vira `transferred` e sobe a fila como só-envio).
- **Estoque e fiado podem contar em dobro por instantes** entre a primeira carga da nuvem depois da regularização e o envio completo da fila; corrigem-se sozinhos.
- **A chave da cópia fica no disco do caixa adicional** (`replica/state.json`, permissão 600, pasta 700). Quem tem acesso de administrador a esse computador consegue abrir a cópia do banco da loja. O navegador do caixa não alcança a chave nem a cópia: a ponte recusa `/lan/*` e o principal só responde às rotas nativas com o cabeçalho dos agentes (seção 6.1).
- **A conferência do PIN na promoção decifra a cópia inteira** a cada tentativa; em banco grande leva alguns segundos.
- **A cópia de saída ocupa até duas vezes o tamanho do banco** no disco do principal, e a de cada caixa adicional outras duas, mais as gerações guardadas em `anterior-<ms>`, que não são apagadas sozinhas.
- **O progresso por caixa no principal é só em memória**: depois de reiniciar o serviço, a tela mostra a cópia como desligada até cada caixa adicional pedir de novo (segundos).
- **Não há rotação da chave da cópia de segurança.** Se a chave mudar, o caixa adicional guarda a geração antiga e começa outra.
- **O vínculo confia no primeiro contato** (código de 6 números dentro de TLS ainda não fixado): alguém já dentro da rede da loja, atacando no minuto do vínculo, poderia se passar pelo principal.
- **Tablet ou celular como caixa adicional**: fora desta versão.
- Nada disto foi validado em Windows físico nem em rede real: os testes ligam o puxador ao principal por um cliente em processo.

## 5. Testes

- `python -m unittest terminal.tests.test_replica_net`: primeira cópia conferida, diário em segundos, arquivos ilegíveis sem a chave, leitores da fase 2 na pasta do caixa adicional, cabo puxado no meio do download (retomada por `Range`) e do diário, linha adulterada ou de outra chave, chave trocada, principal reiniciado, principal que voltou no tempo ou mudou de `device_id`, principal trocado por promoção (mesma identidade, fila divergente; geração diferente), `no_key`, `restoring`, cadência da cópia, três caixas ao mesmo tempo, ganchos sem E/S.
- `python -m unittest terminal.tests.test_promotion`: PIN de gerente e de operador de caixa, bloqueio de cinco minutos, sem cópia, versão mais nova e versão de correção, pendência só até o modo ser gravado e nova promoção do mesmo computador, ordem dos passos e `promotion.json`, falha em cada passo, promoção repetida, uma por vez, queda no meio, cópia danificada.
- `python -m unittest terminal.tests.test_mirror`: inclui `restore_database` e as funções novas.
- `terminal/tests/test_e2e_promocao.py`: ponta a ponta com o `Store` de verdade e a nuvem Django (principal vende, caixa adicional replica, principal morre, promoção sem e com internet, regularização com a fila subindo sem duplicar).
- `terminal/tests/test_e2e_estacoes.py`: o app inteiro do principal, a escuta TLS de verdade e duas pontes reais; principal e dois caixas adicionais vendendo ao mesmo tempo em threads, cabo puxado no meio da venda.
- `terminal/tests/test_fase3_integracao.py`: o que só apareceu na integração (rotas nativas fora do alcance do navegador, principal que sobe com banco danificado, principal substituído, caixa fora do plano, desvincular, promoção sem rede, cliente fixado que nunca vira TCP simples).

## 6. Parte técnica: rede e tela do caixa adicional

### 6.1 Rotas `/lan/v1` (escuta TLS do caixa principal, TCP 8011)

O caixa adicional fala com o principal por TLS com o certificado fixado no vínculo (`<dados>/lan/cert.pem`). Cabeçalhos que ele envia: `X-PDV-Station-Token` (o token do vínculo), `X-PDV-Station-Api` (versão do protocolo, hoje 1), `X-PDV-Station-Version` (versão do PDV2 deste caixa) e `X-PDV-Station-Host` (nome do computador). Toda recusa da camada de rede vem como `{"error","code"}` com o cabeçalho `x-pdv-lan-error: <code>`.

| Rota | Quem | Resposta |
|---|---|---|
| `GET /lan/v1/hello[?challenge=<32 hex>]` | qualquer um na rede | identidade do principal (`shop_id`, `terminal_id`, nomes, `host`, `port`, `generation`, `fingerprint`, `enroll_open`). Com `challenge`, também `proof` = HMAC-SHA256 da chave da cópia sobre `pdv2-generation\|challenge\|terminal_id\|generation\|fingerprint` (seção 6.4). |
| `POST /lan/v1/enroll` `{"code","name","station_id"?}` | quem tem o código de 6 números | `{"api","station_id","station_name","token","fingerprint","server"}`; 401 `invalid_code`; 409 `station_limit`; 429 `too_many_attempts` (10 por endereço a cada 10 min). |
| `GET /lan/v1/ping` | agente do caixa | `{"ok","api","version","station","server_time","generation"}` |
| `/lan/v1/replica/*` | agente do caixa | seção 2.1 |
| `POST /lan/v1/print/next` · `POST /lan/v1/print/<id>/done` `{"error"?}` · `POST /lan/v1/print/recover` | agente de impressão do caixa | próximo recibo da fila **deste** caixa (`payload_b64`), conclusão, e volta dos que ficaram em `sending` |
| qualquer outra rota | navegador do caixa, pela ponte | o app Flask do principal, com `environ["pdv2.station"] = {"id","name","version"}`; rotas de `SERVER_ONLY_ENDPOINTS` dão 403 `server_only` |

**Rotas nativas só para os agentes.** `/lan/*` (qualquer caixa de letra, barras repetidas) exige também `X-PDV-Station-Native: 1`, que só o monitor, o agente de impressão e o puxador da cópia enviam. A ponte local recusa `/lan/*` com 404 antes de encaminhar e descarta todo cabeçalho `X-PDV-Station*` vindo do navegador, então uma página aberta no computador do caixa não lê a chave da cópia, não baixa o banco e não tira recibo da fila.

Códigos de recusa: `station_unauthorized` (401), `station_revoked` (403), `station_limit` (403: fora do plano, ou o principal deixou de ser o principal da loja no painel), `station_outdated`/`server_outdated` (426), `server_damaged` (503: o principal subiu com o banco danificado; a escuta continua aberta para os caixas que já tinha), `server_superseded` (409: outro computador assumiu), `server_busy` (503), `too_many_attempts` (429), `not_found` (404). Na ponte: `server_unreachable` e `server_timeout` (503/504), `server_replaced` (409: este caixa já viu um principal de geração maior).

### 6.2 Tela e rotas locais do caixa adicional (`127.0.0.1:8010`)

`/health` responde `{"status":"ok","service":"terminal-local","mode":"station","server":"unlinked|up|down"}` (`down` cobre também os estados em que o principal recusa este caixa). A tela própria fica em `/station/`; o resto é encaminhado ao principal. As rotas de escrita de `/station/api/*` exigem `X-PDV-Station-Local: 1` e Origin local.

| Rota | Resposta |
|---|---|
| `GET /station/api/status` | `{"linked","state","station","server","reason","reason_text","relink","diagnosis","replica","promotion","can_promote","promote_block","version"}` |
| `POST /station/api/discover` | caixas principais achados pela busca UDP 8012 (`known`, `same_shop`) |
| `POST /station/api/probe` `{"address","port"?}` | o `hello` do endereço digitado |
| `POST /station/api/link` `{"address","port","fingerprint","code","name"?}` | vincula; `fingerprint_mismatch` se outro computador respondeu |
| `POST /station/api/unlink` `{"confirm":true}` | `{"linked":false}`; o vínculo vai para `station-anterior.json` |
| `POST /station/api/mode/server` `{"confirm":true}` | só sem vínculo: volta a ser caixa principal (reinicia o serviço) |
| `GET /station/api/promotion` · `POST …/promotion/verify` · `POST …/promotion/start` | assistente da seção 3; `start` recusa com 409 `server_alive`, `no_network`, ou com o principal danificado |

Estados (`state`): `unlinked`, `up`, `down` (sem resposta), `blocked` (recusado por `station_limit`; sem oferta de assumir; volta sozinho), `relink` (o principal mudou de computador), `revoked`, `incompatible` (`station_outdated`/`server_outdated`) e `damaged`. Numa navegação, toda recusa que é estado deste caixa redireciona para `/station/`; recusas passageiras (`server_busy`, `too_many_attempts`) mostram uma página curta que tenta de novo em 3 s, nunca o JSON cru.

Arquivos do caixa adicional em `<dados>`: `station.json` (600: endereço, impressão digital e geração do principal, token, e `superseded` quando viu um principal mais novo), `station-anterior.json` (vínculo desfeito), `station-print-pending.json` (recibo em impressão quando o serviço parou) e `replica\` (seção 2.3).

### 6.3 Instalação em Docker

`compose.terminal.yaml` define `PDV_LAN_DISABLED=1`: a rede do contêiner não é a da loja, então a escuta não abre e a tela não oferece o código. Caixas adicionais exigem o terminal nativo (instalador Windows ou `python -m terminal`).

### 6.4 Principal substituído

Cada promoção soma 1 a `meta.server_generation`. A busca anuncia a geração, mas o anúncio sozinho não basta (qualquer um na rede poderia mentir): quem acha um anúncio de geração maior com o mesmo `terminal_id` pede `hello?challenge=…` e confere a `proof` com a chave da cópia, que só o principal e os caixas que guardam a cópia dele têm.

- **No principal antigo** (`LanServer.check_superseded`, na partida e a cada reavaliação): prova conferida → `Store.mark_superseded` grava `superseded_at`, `lease_status` passa a `superseded` (não vende nem abre caixa), a busca deixa de anunciá-lo e a escuta recusa caixas com `server_superseded`. Só uma nova configuração (pareamento, recuperação ou promoção) limpa a marca.
- **No caixa adicional** (`Station.rediscover`): prova conferida → `station.json` guarda `superseded` e o caixa fica em `relink` mesmo que o principal antigo volte a responder; a ponte responde `server_replaced`. O vínculo com o novo principal limpa a marca. Sem prova, nada é gravado e o principal original, se voltar, cancela a pista.
