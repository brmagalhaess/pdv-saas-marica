# PDV2 Local: servidor da loja, estações e nuvem

Base: proposta **mvp** (dois de três juízes; é a única que cumpre ao pé da letra "somente um é o servidor" e "sempre depende do servidor local para vender"), com a durabilidade e a recuperação da **robustez** e o onboarding da **operador**. Conferi o código neste branch; nada foi executado em Windows.

## 1. Resumo para o dono

- Você baixa um instalador, clica em Avançar e aparece o ícone PDV2 na área de trabalho. Abriu, já está no caixa.
- A venda acontece sempre no computador da loja. Sem internet, nada muda na tela.
- Quando a internet volta, tudo sobe sozinho para a nuvem, que serve de backup e consulta.
- Loja com vários caixas: um computador é o principal e guarda os dados; os outros são janelas dele pela rede da loja. Estoque e fiado ficam iguais em todos na hora.
- Cada caixa adicional guarda uma cópia de segurança contínua do principal.
- Se o principal quebrar, um caixa adicional vira o principal com essa cópia, por um assistente na tela.
- Trocou de computador: instala, escolhe "Recuperar minha loja", digita o código do painel e os dados voltam.
- Cada loja da empresa tem o seu principal; as lojas se enxergam pela nuvem.

## 2. Decisões de arquitetura

| Decisão | Escolha | Por quê | Descartada |
|---|---|---|---|
| Dados da loja | Um banco SQLite, só no servidor da loja | Um escritor: estoque, fiado e caixa exatos, sem fusão | Banco por caixa com relay (fiado em dobro, devolução antes da venda, dois servidores) |
| Estação | Mesmo executável em modo ponte: `127.0.0.1:8010` local repassado ao servidor | O navegador nunca sai do loopback: Host/Origin/CSRF de `terminal/app.py:78-90` e a balança Web Serial (`terminal/static/terminal.js:24`) ficam intactos | Navegador apontando para o IP do servidor |
| Durabilidade | Réplica contínua nas estações e em segundo disco: cópia do banco a cada 15 min e a cada fechamento, mais diário cifrado de cada evento | Corrige o defeito da mvp (a promoção dependia de backup diário, `terminal/sync.py:216-217`) | Só backup diário |
| Servidor fora do ar | Estação para de vender; promoção assistida | Pedido literal do dono; evita divergência | Venda independente por estação (fica como fase opcional, decisão 1) |
| Nuvem | O servidor é o único `Terminal`; estação é conceito local | A identidade do evento (`cloud/core/views_terminal.py:371-374`) não muda; a nuvem não limita caixas abertos por terminal (`cloud/core/models.py:444-467`) | Relay com identidade de terceiros |
| Ordem da fila | Coluna `seq` crescente | Hoje é `created_at,rowid` em três leituras (`terminal/store.py:346`, `550`, `698`); o relógio pode inverter venda e cancelamento | Manter relógio |
| Impressão | Agente na sessão do usuário (o mesmo `PDV2.exe` do atalho), no servidor e na estação | Serviço do Windows não enxerga impressora por usuário; `win32print` (`terminal/printers.py:383-396`) funciona na sessão | Imprimir de dentro do serviço |
| Ativação | Código do painel (chave + código de uso único), sem URL nem IP | Não expõe a senha do dono em máquina compartilhada | Login por e-mail e senha no caixa |
| Empacotamento | PyInstaller em pasta + Inno Setup + serviço pywin32 | Mantém a linguagem; dispensa Python instalado (`terminal/install-windows.ps1:8-9` exige) | Electron, WebView2, PostgreSQL local, SQLite em pasta de rede |

## 3. Topologia e papéis

```
                    NUVEM (Django + Postgres)
        catálogo, preços, licença, histórico, backup, restore
                        ^  HTTPS, só o servidor fala
                        |
  LOJA A                |                         LOJA B (igual)
  +---------------------+-----------------+
  | SERVIDOR DA LOJA (= Terminal na nuvem)|
  | serviço PDV2: Flask+Waitress 127.0.0.1:8010
  | escuta de rede TLS :8011 (/lan/v1 + ponte)
  | SQLite + outbox + worker de sync      |
  | agente (sessão): atalho, impressão    |
  +---------+-------------------+---------+
            | rede local, TLS   |
  +---------+--------+  +-------+----------+
  | ESTAÇÃO 2        |  | ESTAÇÃO 3        |
  | ponte 127.0.0.1:8010 -> servidor:8011  |
  | Edge --app, balança, impressora local  |
  | réplica cifrada do servidor (sem venda)|
  +------------------+  +------------------+
```

- **Servidor da loja:** única fonte da verdade local e única máquina com token da nuvem.
- **Estação:** não decide nada; guarda `station.json` (token, impressão digital do certificado) e a réplica cifrada.
- **Loja de um caixa só:** é apenas o servidor; a réplica vai para segundo disco ou pendrive.

## 4. Fluxos

**Venda no servidor.** Como hoje: transação única com idempotência por `request_id` (`terminal/store.py:1124`), evento na `outbox`. Na mesma rodada o evento vai para o diário cifrado.

**Venda na estação.** Navegador → ponte local → servidor por TLS → mesma transação. A ponte repassa `Host` e `Origin` sem alterar, então a checagem de `app.py:78-90` passa sem mudança; a escuta `:8011` valida o token, descarta qualquer cabeçalho de estação vindo do cliente e injeta o `station_id`. Se a rede cair no meio, a tela repete com o mesmo `request_id` e não duplica. O recibo entra em `print_jobs` com `station_id` e o agente da estação imprime.

**Sincronização local.** Não existe fila entre estação e servidor. O que existe é a réplica: a estação puxa `GET /lan/v1/replica/journal?after=<seq>` em espera longa e a cópia do banco quando há nova. A tela do servidor mostra "cópia de segurança em dia" ou "atrasada".

**Sincronização com a nuvem.** `sync_once` (`terminal/sync.py:134`) envia lotes de 50 em ordem de `seq`. A baixa (`acked_at`) só vem com confirmação da nuvem. Depende do protocolo de quarentena: neste branch o lote ainda é atômico (`views_terminal.py:477-514`) e um evento repetido com conteúdo diferente devolve 409 (`:483-484`), o que trava a fila.

**Servidor fora do ar.** A estação mostra "Caixa principal fora do ar" e tenta de novo a cada 2 s. O serviço reinicia sozinho em falha e sobe no boot sem login, então reinício de Windows Update volta em cerca de um minuto. Se não voltar, "Tornar este o caixa principal" (seção 6). O carrinho em andamento passa a ser rascunho gravado no navegador da estação e reaparece.

**Internet fora do ar.** Nada muda na venda. Indicador "N vendas aguardando envio"; loja de um caixa sem segundo disco vê também "N vendas só neste computador".

## 5. Instalador, serviço e atalho

- **Pacote:** `PDV2-Instalar.exe` (PyInstaller em pasta, com `tzdata`: `terminal/printers.py:92` usa `ZoneInfo`, que no Windows não tem base própria). Gerado em CI Windows, assinado.
- **Instalação:** programa em `Program Files\PDV2\app-<versão>`, dados em `%ProgramData%\PDV2` (hoje `%LOCALAPPDATA%`, `terminal/app.py:19-23`, que é por usuário e o serviço não acha). Migra o banco existente.
- **Serviço `PDV2Service`:** início automático, reinício em falha, `quick_check` do banco na partida.
- **Atalho PDV2** (área de trabalho e menu Iniciar): `PDV2.exe` confere `/health`, sobe o serviço se preciso, abre o Edge em `--app=http://127.0.0.1:8010` com perfil dedicado e fica como agente de impressão. Instância única, sem console.
- **Primeira abertura:** "Primeiro caixa desta loja", "Caixa adicional" ou "Recuperar minha loja". O endereço da nuvem vem fixo no pacote.
- **Caixa adicional:** a estação procura o servidor e mostra "Caixa principal do Mercado X encontrado"; no servidor o gerente toca em "Adicionar caixa" e dita 6 dígitos (`POST /lan/v1/enroll`). Não exige internet.
- **Descoberta:** último endereço; nome do computador gravado no vínculo; broadcast UDP 8012 respondendo só ao `shop_id` certo; por fim IP digitado. Sem mDNS (dependência a mais, falha em roteador de operadora).
- **Firewall:** regra criada pelo instalador elevado, TCP 8011 e UDP 8012, sub-rede local, todos os perfis; a escuta só abre no servidor.
- **Atualização:** `updater.exe` separado; pasta por versão e troca de ponteiro; só com todos os caixas fechados; backup antes, `/health` depois, volta automática. Chave de release separada da de licença (hoje `check_release` valida com a mesma `public_key`, `terminal/sync.py:114-125`). Cada máquina baixa da nuvem; sem cascata pelo servidor. A ponte fala `/lan/v1` versionado; estação incompatível avisa "atualize este caixa".

## 6. Troca de computador e restauração

**O que o usuário faz**
1. No painel: Terminais → "Trocar de computador" → aparece o código de recuperação.
2. No computador novo: instala, abre o ícone, "Recuperar minha loja", digita o código.
3. Espera a barra "Trazendo seus dados" e volta a vender.

**Por baixo**
- O código reaproveita `reissue_pairing` (`views_terminal.py:194-199`): mesmo `Terminal.id`. O `pair` grava `device_id` e token novos (`views_terminal.py:244-251`). Novidade: o token antigo vira só-envio pelo prazo da licença offline do plano mais 1 dia, nunca menos de 7, para a fila do computador velho ainda subir.
- `GET /api/v1/terminal/restore?cursor=`, paginado e retomável. `restore_state` fica em `meta` e a venda só libera em `done`.
- **Volta:** catálogo, operadores, clientes e saldos (bootstrap); vendas de 90 dias mais as com fiado em aberto, a partir de `SyncEvent.payload` (`cloud/core/models.py:636-645`); devoluções, recebimentos, o caixa aberto mais recente com movimentos; configuração de impressão e estações (evento novo `terminal.config`).
- **Não volta:** carrinhos suspensos e fila de impressão.
- **O que não tinha subido:** "Tenho uma cópia de segurança" lê a réplica do segundo disco ou de uma estação e reenvia a fila; a nuvem deduplica por `event_id`.

**Promover estação (servidor queimou).** Assistente na estação, com PIN de gerente: abre a réplica local, aplica o diário posterior à cópia com a mesma função da restauração (`apply_restore`, que não gera eventos novos) e assume. Com internet, pede o código de recuperação; sem internet, vende com a licença que veio na réplica e regulariza na primeira conexão. As outras estações seguem o novo servidor ao serem reapontadas pelo mesmo código de 6 dígitos.

## 7. Multiloja

- Um servidor por loja, cada um `Terminal` da sua `shop`; `Shop.hub_terminal` único entra já na fase 3.
- Lojas só se falam pela nuvem; a descoberta filtra por `shop_id`, então lojas vizinhas não se misturam.
- Estoque é por loja. Fiado e preços são da empresa: duas lojas offline podem estourar o mesmo limite; vira conflito `credit_limit_exceeded` no painel, nunca venda rejeitada.
- Limites do plano já existem (`Plan.terminal_limit`, `shop_limit`, `cloud/core/models.py:25-26`); recuperação não consome vaga.

## 8. Licença offline

- Hoje: 72 h, ou 24 h em atraso (`views_terminal.py:86`), teto de 7 dias no verificador (`shared/licensing.py:12`).
- Novo: `Plan.offline_days` (padrão 30), sempre limitado ao período pago mais tolerância (`views_terminal.py:87`). `MAX_LEASE_SECONDS` sobe para 45 dias.
- **Sem ordem frágil de implantação:** a nuvem só emite licença longa para terminal cuja `version` (`cloud/core/models.py:203`) já aceita; os antigos seguem com 72 h.
- Aviso a 7 dias do fim. Vencida: não corta venda no meio do turno; impede abrir novo caixa.
- Rota própria de renovação, para assinatura vencida não congelar o catálogo.
- Relógio: reancorar `last_seen` na hora da nuvem a cada contato; tela que ensina a acertar a hora (hoje só bloqueia, `store.py:450-452`).

## 9. Segurança

- Tela, PIN e rotas de pareamento nunca saem de `127.0.0.1` (`terminal/__main__.py:11`).
- Porta 8011: TLS com certificado próprio fixado no vínculo, token por estação; sem token, 401.
- Proibido para estação: `/api/pair`, `/api/unpair`, `/api/new-profile`, `/api/bootstrap`, exportação e backup.
- Bloqueio de PIN por operador e por estação. Hoje a autorização de gerente usa um contador único `manager_override` (`store.py:879`): um erro repetido trava a loja toda.
- Token da nuvem cifrado com DPAPI e amarrado ao `MachineGuid`: banco copiado cai em "Recuperar".
- Réplica e backup cifrados com chave da empresa, exibida uma vez no painel. Na fase 2 isso vale para a réplica em segundo disco (`terminal/mirror.py`, Fernet); o backup diário em `data/backups` continua em claro, protegido só pelas permissões do sistema.

## 10. Plano de entrega

**Fase 0. Caixa único blindado**
- Objetivo: corrigir a base.
- Mudanças: `terminal/store.py` (`seq` na outbox e nas três leituras; coluna `resolution`, que `resolve_sync_conflict` grava em `:429` e a tabela não tem; `lease_status` sem gravar `last_seen` a cada leitura, `:467-468`; contador de PIN por operador; `quick_check`); `terminal/requirements.txt` (`tzdata`); `terminal/app.py:19-23`.
- Testar: testes de ordenação com relógio atrasado, resolução de conflito, bloqueio de PIN.
- Lojista ganha: resolver conflito deixa de dar erro.

**Fase 1. Instalador, atalho e licença longa**
- Mudanças: novos `terminal/service_win.py`, `terminal/launcher.py` (atalho e agente de impressão), `terminal/updater.py`, `installer/pdv2.spec`, `installer/pdv2.iss`, CI Windows; `shared/licensing.py`, `views_terminal.py:_lease`, migração `Plan.offline_days`; chave de release própria.
- Testar: Windows 10 e 11 limpos, usuário sem privilégio, reinício, impressora USB e de rede, antivírus comum, atualização com volta.
- Lojista ganha: instala sozinho, abre pelo ícone, 30 dias sem internet.

**Fase 2. Recuperação e réplica em segundo disco**
- Mudanças: nuvem `terminal/restore`, token só-envio, evento `terminal.config`; `store.py` `apply_restore`, `restore_state`, importação de fila; novo `terminal/mirror.py`; tela "Recuperar minha loja".
- Testar: restaurar com queda de rede no meio; comparar totais com a nuvem; reenvio duplicado.
- Lojista ganha: troca de computador sem perder dados.
- Contrato detalhado, formato dos arquivos da réplica e limites: [RECUPERACAO-E-REPLICA.md](RECUPERACAO-E-REPLICA.md). Onde este documento cita linhas de código antigas, valem as daquele.
- Ajustes em relação ao desenho acima: o código de recuperação vale 30 min e tem a forma `XXXX-XXXX-XXXX`; o token antigo vira só-envio quando o computador novo pareia (não no clique do painel); a fila lida da réplica não é aplicada direto no banco local, sobe para a nuvem e volta por uma segunda passada do restore; a cópia do banco na réplica não leva o token do terminal; o prazo do só-envio acompanha a licença offline do plano (+1 dia, mínimo 7) e cada computador substituído mantém o próprio token só-envio; "Desistir da recuperação" devolve o caixa ao computador anterior.

**Fase 3. Estações** (depende da quarentena em produção)
- Mudanças: `store.py` (tabela `stations`; `station_id` em `cash_sessions`, `sales`, `sale_returns`, `credit_receipts`, `print_jobs`, `print_settings`; índice `cash_one_open` por estação, hoje global em `:134`; `_open_cash` por estação, `:937-941`); novos `terminal/lan.py`, `terminal/station.py`, `terminal/discovery.py`; réplica pela rede; promoção; nuvem: `station` em `cash.opened`, `Shop.hub_terminal`.
- Testar: é a refatoração mais cara; suíte com dois e três caixas simultâneos, cabo puxado no meio da venda, servidor reiniciado, promoção com e sem internet.
- Lojista ganha: vários caixas com estoque único.

**Fase 4. Acabamento**
- Conflito de fiado entre lojas, cancelamentos do painel descendo ao caixa e, se o dono decidir, venda de contingência na estação.

## 11. Riscos conhecidos e limites

- **Servidor e rede local são ponto único da loja.** Cabo, switch ou Wi-Fi ruim param a estação. Recomendar cabo e nobreak no servidor.
- **Janela de perda:** disco do servidor morrendo perde no máximo o que ainda não chegou à réplica (segundos, com estação ligada). Loja de um caixa sem segundo disco e sem internet perde desde a última sincronização.
- **Promoção sem internet** deixa por alguns dias dois aparelhos com a mesma identidade se o antigo ressuscitar; a nuvem aceita só o token novo após a regularização, e a fila do antigo sobe como só-envio.
- **SmartScreen e antivírus** contra PyInstaller nas primeiras semanas do certificado.
- **Rede marcada como Pública, IP que muda:** diagnóstico na tela da estação.
- **Cadastro de produto e alteração de preço continuam só na nuvem.** Este desenho cobre vender offline, não cadastrar.
- **NFC-e em contingência** tem prazo fiscal próprio; fora deste escopo.
- **Tablet ou celular como estação:** não, exigiria HTTPS de verdade na rede.

## 12. Decisões que dependem do dono

1. **Com o principal fora do ar, o caixa adicional para?** Recomendo que pare, com promoção assistida. Parar: dados sempre exatos, loja depende de uma máquina. Vender sozinho: loja não para, mas custa uma fase a mais e estoque e fiado ficam aproximados até reconciliar.
2. **Prazo sem internet.** Recomendo 30 dias, configurável por plano. Mais prazo é mais crédito para inadimplente; menos prazo arrisca loja de internet ruim.
3. **Caixa adicional é cobrado?** Recomendo incluir uma quantidade por plano e controlar pela licença. Cobrar por caixa aumenta receita e atrito; liberar tudo simplifica e abre mão dela.
4. **Quanto histórico volta ao computador novo.** Recomendo 90 dias mais fiado em aberto. Mais dias alongam a restauração; menos dias limitam devolução e reimpressão no caixa (o painel mantém tudo).
5. **Certificado de assinatura e CI Windows.** Recomendo comprar antes da fase 1. Sem eles o Windows mostra alerta de "aplicativo desconhecido" na instalação e não há como gerar nem testar o instalador de forma repetível.
