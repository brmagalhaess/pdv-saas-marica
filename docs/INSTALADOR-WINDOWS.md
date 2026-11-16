# Instalador do PDV2 para Windows

> **Leia antes de entregar a um cliente.** Tudo aqui foi escrito e testado em macOS. A lógica de atualização, troca de versão, volta automática, supervisão do processo e migração de dados tem testes automáticos que rodam em macOS e Linux, e o empacotamento PyInstaller foi executado em macOS. **Nada foi executado em Windows:** o serviço `PDV2Service`, o roteiro do Inno Setup, o `build.ps1`, o atalho, as permissões de pasta e a regra de firewall só passam a valer depois da [lista de testes manuais](#7-lista-de-testes-manuais-em-windows-real).

## 1. O que o lojista vê

1. Baixa `PDV2-Instalar-<versão>.exe`, clica em Avançar até o fim.
2. Aparece o ícone **PDV2** na área de trabalho e no menu Iniciar.
3. Abre o ícone: o caixa aparece em uma janela própria, sem barra de endereço.
4. O caixa já está no ar quando o computador liga, mesmo antes de alguém entrar no Windows.
5. Quando sai uma versão nova, o PDV2 baixa sozinho e instala com o caixa fechado.

## 2. O que o instalador faz na máquina

| Onde | O quê |
|---|---|
| `C:\Program Files\PDV2\app-<versão>\` | O programa. Uma pasta por versão. |
| `C:\Program Files\PDV2\bin\` | Cópia estável do programa: é daqui que rodam o serviço e os atalhos. Só o instalador completo troca esta pasta. |
| `C:\Program Files\PDV2\current.txt` | A versão em uso (por exemplo `2.0.1`). |
| `C:\ProgramData\PDV2\` | Dados: `terminal.sqlite3`, `backups\`, `logs\`, `updates\`, `mirror.json` (configuração da cópia em outro disco). Só o serviço (SYSTEM) e administradores leem. **O desinstalador não apaga esta pasta.** |
| `%LOCALAPPDATA%\PDV2\browser-profile\` | Perfil do navegador usado só pelo PDV2 (um por usuário do Windows). |
| Serviço `PDV2Service` | Início automático, reinício em falha (5 s, 5 s, depois a cada 30 s). Roda como sistema. |
| Atalhos | Área de trabalho (opcional) e menu Iniciar, apontando para `bin\PDV2.exe`. |
| `<outro disco>\PDV2-copia-de-seguranca\` | Só depois que o gerente escolhe um disco em **Sistema → Cópia de segurança em outro disco**. Cópia cifrada do banco e diário das vendas. O instalador e o desinstalador não tocam nesta pasta. |
| `C:\ProgramData\PDV2\mode.json` | Papel deste computador: `server` (caixa principal, o padrão quando o arquivo não existe) ou `station` (caixa adicional). Escrito pela própria tela; ver a seção 9. |
| `C:\ProgramData\PDV2\lan\` | Só no caixa principal com caixas adicionais: `cert.pem` e `key.pem` da rede entre caixas. Apagar a pasta cria outra identidade e todos os caixas adicionais têm de ser ligados de novo. |
| `C:\ProgramData\PDV2\station.json` | Só no caixa adicional: endereço e impressão digital do caixa principal e o token deste caixa. |
| Firewall | A opção "Este computador será o servidor da loja" (desmarcada) cria a regra `PDV2 - Servidor da loja` para TCP 8011 e UDP 8012, só para a rede local. Sem a opção, o próprio serviço cria a mesma regra na primeira vez em que o caixa principal abre a rede entre caixas (plano com caixas adicionais). O caixa adicional não precisa de regra: ele só faz conexões de saída. |

Passo a passo do instalador (`installer/pdv2.iss`):

1. Para o serviço (`net stop`) e fecha o atalho. **Encerra o PDV2 antigo** (o `python.exe` de `%LOCALAPPDATA%\PDV2\.venv` que o instalador de desenvolvimento deixava subir no login) e confere que a porta 8010 ficou livre. Se a porta continuar em uso, a instalação **para aqui**, com a mensagem "feche o PDV2 antigo e execute o instalador de novo", antes de copiar ou migrar qualquer coisa. Só então remove as pastas `app-*` de versões anteriores.
2. Apaga `updates\` (um download pela metade não vale para a instalação nova).
3. Copia o programa para `app-<versão>\` e para `bin\`.
4. Restringe `C:\ProgramData\PDV2` ao sistema e aos administradores.
5. **Migra uma instalação antiga:** se ainda não existe banco em `C:\ProgramData\PDV2`, procura `terminal.sqlite3` em `%LOCALAPPDATA%\PDV2\data` e `%LOCALAPPDATA%\PDV2` de todos os usuários e copia o primeiro que achar (banco por cópia consistente do SQLite, `session.key`, perfis e backups). O original fica onde estava. Também remove o atalho de inicialização do instalador antigo. A cópia só acontece com o terminal antigo parado: `PDV2Service.exe --migrate-legacy` tenta encerrá-lo de novo e, se a porta 8010 ainda responder, não copia nada, devolve o código 3 e o instalador avisa e **não registra o serviço** (sem banco no destino, a próxima execução do instalador migra de novo). Isso evita que vendas feitas no terminal antigo depois da cópia fiquem para trás.
6. Grava `current.txt`, registra o serviço e o inicia.
7. Oferece "Abrir o PDV2 agora".

Instalação silenciosa: `PDV2-Instalar-<versão>.exe /VERYSILENT /NORESTART`.

### Os três programas

- **`PDV2.exe` (atalho).** Uma instância por usuário. Confere `http://127.0.0.1:8010/health`; se não responde, pede o início do serviço e espera até 40 s. Abre o caixa no Edge em modo aplicativo (`--app=`) com perfil dedicado; sem Edge usa o Chrome; sem os dois, o navegador padrão. Se nada funcionar, mostra uma mensagem em português com o que fazer. O atalho de `bin\` repassa para o `PDV2.exe` da versão em uso.
- **`PDV2Service.exe` (serviço).** Hospedeiro estável: lê `current.txt`, inicia `app-<versão>\PDV2Server.exe`, reinicia se ele cair ou parar de responder por 3 minutos, e confirma a saúde depois de uma troca de versão. Registro em `C:\ProgramData\PDV2\logs\service.log` (rotativo, 5 arquivos de 1 MB).
- **`PDV2Server.exe` (caixa local).** O mesmo terminal de sempre (Flask + Waitress em `127.0.0.1:8010`), com os dados em `C:\ProgramData\PDV2`. Registro em `logs\terminal.log`. Para de forma limpa quando o serviço pede.

`PDV_TERMINAL_DATA` continua tendo precedência sobre a pasta padrão de dados.

## 3. Como gerar

Requisitos: Windows 10 ou 11 de 64 bits, Python 3.12 no PATH, Inno Setup 6.3 ou mais novo.

```powershell
powershell -ExecutionPolicy Bypass -File installer\build.ps1
```

O roteiro instala as dependências, gera o ícone, roda o PyInstaller, faz um teste de fumaça do servidor empacotado (sobe, responde `/health`, para), gera o bundle e compila o instalador. Saída em `dist\windows`:

| Arquivo | Uso |
|---|---|
| `PDV2-Instalar-<versão>.exe` | Instalador completo, para máquinas novas. |
| `PDV2-Instalar-<versão>.metadata.json` | Metadados com `kind: installer`. |
| `PDV2-Terminal-<versão>-win64-bundle.zip` | Atualização automática: contém só `app-<versão>\`. |
| `PDV2-Terminal-<versão>-win64-bundle.metadata.json` | Metadados com `kind: bundle` (versão, SHA-256, tamanho). |

Opções: `-Version 2.0.1` (padrão: arquivo `VERSION`), `-SkipInstaller` (só o bundle), `-SkipSmoke`.

**Sem Windows:** o workflow `PDV2 Windows installer` (`.github/workflows/windows-installer.yml`) faz o mesmo em `windows-latest` e publica os dois artefatos. Ele roda os testes do instalador, do atualizador e do serviço como obrigatórios e o restante da suíte do terminal como informativo, porque essa suíte nunca tinha rodado em Windows.

Fora do Windows, `python scripts/package_terminal.py` continua gerando o ZIP de código-fonte e passa a gerar também `PDV2-Terminal-<versão>-bundle.zip`, um bundle de código-fonte no formato novo. Ele serve para instalações de desenvolvimento e para os testes; **um terminal instalado pelo instalador recusa esse bundle** (o marcador `bundle.json` diz `source` e a instalação exige `frozen-win64`).

## 4. Como assinar

Sem assinatura o Windows mostra "aplicativo desconhecido" (SmartScreen) e antivírus tendem a desconfiar de executáveis PyInstaller. Compre um certificado de assinatura de código antes de entregar a clientes.

- **Local:** defina `PDV_SIGN_PFX` (caminho do `.pfx`) e `PDV_SIGN_PASSWORD`, ou passe `-CertificatePath` e `-CertificatePassword` ao `build.ps1`. Precisa do `signtool.exe` (Windows SDK). São assinados os três executáveis e o instalador, com carimbo de tempo.
- **CI:** cadastre os segredos `WINDOWS_SIGN_PFX_BASE64` (o `.pfx` em Base64) e `WINDOWS_SIGN_PASSWORD`. Sem eles o workflow gera tudo sem assinar e deixa um aviso visível na execução.

O bundle de atualização é gerado depois da assinatura dos executáveis, então o SHA-256 publicado já é o do conteúdo assinado. Certificados em token físico ou em nuvem (exigência atual das autoridades certificadoras) pedem ajuste no passo de assinatura; isso não foi testado.

## 5. Como publicar uma versão

1. Suba o número em `VERSION` e gere (seção 3).
2. Hospede o `...-win64-bundle.zip` em um endereço **HTTPS** estável. O terminal recusa HTTP.
3. No painel de versões, cadastre a versão do componente `terminal`, canal `stable`, com a URL, o `sha256` e o `size_bytes` do `.metadata.json`, e `kind: bundle`. A nuvem assina o manifesto com a chave de versões.
4. Para uma versão que mexe em `bin\` (serviço ou atalho) ou que exige reinstalar, publique o instalador com `kind: installer`; o terminal avisa que precisa do instalador completo e não tenta aplicar sozinho. Use `min_version` em um bundle para exigir uma versão mínima já instalada.
5. Confira antes: `python scripts/verify_update.py pacote.zip manifesto.json chave-de-versoes.pem` valida assinatura, tamanho, SHA-256 e a estrutura do bundle.

Um manifesto sem `kind` é tratado como pacote manual: aparece para o operador, mas não é baixado nem aplicado.

## 6. Como a atualização automática funciona

Código em `terminal/updater.py`; estado em `C:\ProgramData\PDV2\updates\state.json`.

1. **Descoberta.** O terminal já consulta a nuvem e guarda o manifesto verificado (assinatura conferida com a chave fixada). O atualizador só olha para um manifesto de versão maior que a atual, com `kind: bundle`.
2. **Download.** Em segundo plano, para `updates\<versão>\package.zip`. Só HTTPS. Se a conexão cair, o pedaço baixado fica guardado e o download continua de onde parou. No fim confere **tamanho e SHA-256**; se não bater, apaga e tenta de novo mais tarde (5 min, dobrando até 6 h). A estrutura do zip também é conferida. Estado: `staged` (pronto para instalar).
3. **Aplicação.** Só acontece com **nenhum caixa aberto** e a fila de impressão ociosa. Pode ser pedida pela tela (`request_apply`) ou acontecer sozinha na janela de madrugada (2h às 5h; ajuste com `PDV_UPDATE_AUTO_APPLY=22-4`, desligue com `PDV_UPDATE_AUTO_APPLY=off`). Passos: confere o SHA-256 de novo, faz **backup verificado do banco**, extrai para `app-<versão>\` (recusando caminho absoluto, `..`, links simbólicos e qualquer arquivo fora de `app-<versão>\`), troca `current.txt` de forma atômica e pede o reinício ao serviço.
4. **Confirmação.** O serviço inicia a versão nova e espera `/health` por até 60 s. Respondeu: confirma, apaga o download e mantém só a versão atual e a anterior. Não respondeu (ou o processo morreu): **volta `current.txt` para a versão anterior**, inicia a anterior e marca `failed`. A versão que falhou não é baixada de novo; uma versão mais nova é aceita normalmente. Se a energia cair no meio da confirmação, ela se repete no próximo início.

Uma instalação rodando do código-fonte (sem `current.txt`) nunca baixa nem aplica: `status` devolve `can_apply: false` com o motivo "instalação de desenvolvimento".

### Como voltar atrás

- **Automático:** descrito acima; não exige ninguém.
- **Manual, para a versão anterior** (ela continua em `Program Files\PDV2\app-<anterior>\`): em um Prompt de Comando como administrador,
  ```
  net stop PDV2Service
  "C:\Program Files\PDV2\bin\PDV2Service.exe" --rollback
  net start PDV2Service
  ```
  Equivale a parar o serviço, escrever a versão anterior em `current.txt` e iniciar o serviço.
- **Banco de dados:** a volta de versão **não** restaura o banco. O backup feito antes da atualização fica em `C:\ProgramData\PDV2\backups\` e o nome dele está em `updates\state.json` (`backup`). Só restaure com orientação do suporte: vendas feitas depois do backup não estão nele.
- **Para qualquer versão:** instale o `PDV2-Instalar-<versão>.exe` desejado por cima. Os dados são mantidos.

## 7. Lista de testes manuais em Windows real

**Nenhum item abaixo foi executado nesta entrega.** Marque cada um em máquina real (ou máquina virtual limpa) antes de entregar a um cliente. Anote a versão do Windows e o resultado.

### 7.1 Instalação limpa

- [ ] Windows 10 22H2 limpo: instalar com conta de administrador. Ícone aparece, caixa abre em janela própria.
- [ ] Windows 11 limpo: o mesmo.
- [ ] Instalar a partir de uma **conta padrão** (sem privilégio): o Windows pede a senha de administrador; depois de instalar, o atalho aparece para a conta padrão e abre o caixa.
- [ ] Com o arquivo sem assinatura, registrar o que o SmartScreen mostra; com assinatura, confirmar o nome do editor.
- [ ] `services.msc`: `PDV2 - Caixa local` em execução, início Automático, aba Recuperação com reinício.
- [ ] `C:\Program Files\PDV2` contém `bin\`, `app-<versão>\` e `current.txt` com a versão certa.
- [ ] `C:\ProgramData\PDV2` contém `terminal.sqlite3` e `logs\`; uma conta padrão **não** consegue abrir a pasta.
- [ ] Parear o terminal, abrir caixa, vender, fechar caixa.
- [ ] Instalação silenciosa (`/VERYSILENT`) termina com o serviço no ar.

### 7.2 Usuário sem privilégio no dia a dia

- [ ] Com conta padrão: abrir pelo ícone, vender, imprimir.
- [ ] Parar o serviço como administrador e abrir o ícone com a conta padrão: o atalho consegue iniciar o serviço (permissão concedida por `sc sdset`) ou mostra a mensagem em português.
- [ ] Clicar duas vezes seguidas no ícone: abre uma janela só; com a janela aberta, um novo clique a traz para a frente.
- [ ] Dois usuários do Windows na mesma máquina (troca rápida de usuário): os dois abrem o mesmo caixa.

### 7.3 Reinício e quedas

- [ ] Reiniciar o Windows: sem entrar em nenhuma conta, `http://127.0.0.1:8010/health` responde (testar logo após entrar).
- [ ] Encerrar `PDV2Server.exe` pelo Gerenciador de Tarefas: volta sozinho em segundos.
- [ ] Encerrar `PDV2Service.exe`: o Windows reinicia o serviço e não sobra `PDV2Server.exe` órfão segurando a porta 8010.
- [ ] Desligar da tomada com caixa aberto: ao religar, o caixa aberto e as vendas estão lá.
- [ ] Reinício forçado por Windows Update com caixa aberto.
- [ ] Porta 8010 ocupada por outro programa: a mensagem do atalho aparece e `logs\service.log` explica.

### 7.4 Impressora e periféricos

- [ ] Impressora térmica USB instalada para todos os usuários: cupom imprime com o serviço rodando como sistema.
- [ ] Impressora de rede: imprime.
- [ ] **Impressora instalada só para um usuário** (mapeada por usuário): limitação conhecida, o serviço roda como sistema e pode não enxergá-la. Registrar o comportamento; a solução definitiva (agente de impressão na sessão do usuário) está planejada em `docs/ARQUITETURA-OFFLINE-LOCAL.md`.
- [ ] Gaveta, leitor de código de barras e balança (Web Serial no Edge em modo aplicativo).
- [ ] Hora do cupom correta (fuso `America/Sao_Paulo` vem do pacote `tzdata` embutido).

### 7.5 Antivírus

- [ ] Microsoft Defender com proteção em tempo real: instalar, abrir, vender, atualizar. Nenhum arquivo em quarentena.
- [ ] Um antivírus comum no varejo (por exemplo Avast ou Kaspersky): o mesmo.
- [ ] Conferir se o antivírus bloqueia a extração de `app-<versão>\` durante a atualização ou o início do serviço.

### 7.6 Atualização automática e volta

Use um servidor HTTPS de teste e duas versões geradas pelo `build.ps1` (por exemplo 2.0.0 e 2.0.1).

- [ ] Publicar 2.0.1 como `bundle`: o terminal baixa sozinho e mostra a versão pronta.
- [ ] Com **caixa aberto**, pedir a instalação: recusa com o motivo "há um caixa aberto".
- [ ] Com caixa fechado, instalar: o caixa reinicia em menos de um minuto, `current.txt` mostra 2.0.1, existe um backup novo em `backups\`, a pasta `app-2.0.0` continua lá.
- [ ] Derrubar a internet no meio do download e religar: continua de onde parou.
- [ ] Publicar um bundle com SHA-256 errado no manifesto: recusado, nada muda.
- [ ] Publicar um bundle de propósito quebrado (por exemplo, apagando `_internal\base_library.zip` antes de zipar): depois de até 60 s o PDV2 **volta sozinho para a versão anterior** e o caixa abre normalmente; `updates\state.json` mostra `failed`.
- [ ] Depois da volta, publicar uma versão corrigida com número maior: instala.
- [ ] Volta manual com `PDV2Service.exe --rollback`.
- [ ] Atualização na janela da madrugada com o computador ligado e o caixa fechado.
- [ ] Depois de duas atualizações, só restam a versão atual e a anterior em `Program Files\PDV2`.

### 7.7 Instalar por cima, migrar e desinstalar

- [ ] Instalar uma versão mais nova por cima com o instalador completo: dados preservados, serviço de volta, pastas de versões antigas removidas.
- [ ] Máquina com a instalação antiga (`terminal\install-windows.ps1`, dados em `%LOCALAPPDATA%\PDV2\data`), **com o PDV2 antigo aberto e uma venda ainda não sincronizada**: instalar. O instalador encerra o antigo sozinho; `http://127.0.0.1:8010/health` passa a ser respondido pelo serviço novo. As vendas e o pareamento aparecem no novo; o atalho antigo de inicialização sumiu; `C:\ProgramData\PDV2\migrated-from.txt` aponta a origem.
- [ ] Simular a falha em encerrar o antigo (por exemplo, outro programa ocupando a porta 8010): o instalador para com a mensagem em português, nada é copiado para `C:\ProgramData\PDV2` e o terminal antigo continua com todos os dados. Liberar a porta e instalar de novo: migra normalmente.
- [ ] O mesmo, com a instalação antiga feita por **outro usuário** do Windows que não o administrador que instala.
- [ ] Desinstalar pelo Painel de Controle: serviço removido, `Program Files\PDV2` apagado, atalhos removidos, regra de firewall removida (se existia) e **`C:\ProgramData\PDV2` intacto**. Reinstalar e confirmar que os dados voltam.
- [ ] Marcar "servidor da loja" na instalação: a regra `PDV2 - Servidor da loja` aparece no firewall, restrita à sub-rede local.

### 7.8 Cópia de segurança em outro disco e troca de computador

Formato e regras em [RECUPERACAO-E-REPLICA.md](RECUPERACAO-E-REPLICA.md). O caixa precisa ter falado com a nuvem ao menos uma vez nesta versão para receber a chave da cópia.

- [ ] Espetar um pendrive, entrar como gerente, **Sistema → Cópia de segurança em outro disco → Escolher disco**: o pendrive aparece na lista com o espaço livre; ao escolher, surge `E:\PDV2-copia-de-seguranca\<terminal>\<computador>\` com `manifest.json` e um `snapshot-*.pdv2snap`, e a tela mostra "Cópia de segurança em dia".
- [ ] Tentar escolher uma pasta no disco `C:`: é recusada com a mensagem de "mesmo disco do caixa".
- [ ] Fazer uma venda: em poucos segundos o arquivo `journal-*.pdv2log` cresce. Abrir os arquivos no Bloco de Notas: nada legível além do cabeçalho.
- [ ] Fechar o caixa: aparece um `snapshot-*` novo. Depois de 4 fechamentos existem só 3.
- [ ] **Puxar o pendrive no meio do expediente** e vender: a venda conclui normalmente; a tela passa a avisar que o disco não foi encontrado. Recolocar: volta para "em dia" sem intervenção.
- [ ] Puxar o pendrive **durante** "Copiar agora": nenhuma venda é afetada; ao recolocar, uma nova cópia completa é feita e não sobra arquivo `.tmp`.
- [ ] Encher o pendrive: aviso de disco cheio; liberar espaço resolve sozinho.
- [ ] Reiniciar o Windows com o pendrive espetado: o serviço volta a gravar sem login de usuário (o serviço roda como sistema).
- [ ] Pendrive que muda de letra (espetar em outra porta): anotar o comportamento. A configuração guarda o caminho escolhido; se a letra mudar, é preciso escolher o disco de novo.
- [ ] **Troca de computador:** desligar a internet do caixa antigo, fazer 3 vendas, desligar o computador. No painel, **Terminais → Trocar de computador**. Em outro Windows limpo: instalar, **Recuperar minha loja**, digitar chave e código, esperar a barra, escolher **Tenho uma cópia de segurança** com o pendrive do antigo. As 3 vendas aparecem no painel uma única vez e no histórico do caixa novo.
- [ ] Religar o computador antigo com internet: mostra "Este caixa foi transferido", não vende, e a fila dele zera.
- [ ] Repetir a recuperação puxando o cabo de rede no meio da barra: ao reconectar, continua de onde parou.

## 8. Limites conhecidos

- **Impressora por usuário:** ver 7.4.
- **Pasta `bin\` só muda com o instalador completo.** Correções no serviço ou no atalho estável não chegam por bundle; publique com `kind: installer`.
- **O bundle não é diferencial:** cada atualização baixa o programa inteiro (dezenas de MB).
- **A volta de versão não desfaz mudanças de esquema do banco.** As migrações do terminal são aditivas; se uma versão futura fizer uma mudança incompatível, publique-a como `installer` e planeje a volta com restauração de backup.
- **Um instalador mais antigo por cima de um mais novo** é aceito pelo Inno Setup; o banco pode ter sido migrado pela versão nova.
- **Cópia em outro disco e conta de sistema:** o serviço roda como SYSTEM e não enxerga unidade de rede mapeada por um usuário; use pendrive, HD externo ou caminho UNC liberado para a conta do computador. Pendrive em FAT/exFAT não tem permissão por arquivo: o conteúdo é cifrado, mas `manifest.json` mostra o nome da empresa, da loja e do caixa.
- **Token da nuvem cifrado com DPAPI** não faz parte desta fase.
- **Caixa adicional não se atualiza sozinho** nesta fase: a tela vem do caixa principal e o protocolo entre eles é versionado; quando for preciso, a tela pede ("Atualize este caixa") e instala-se por este mesmo instalador.
- **Vínculo confia no primeiro contato:** o código de 6 números viaja dentro de TLS ainda não fixado; alguém já dentro da rede da loja, atacando no minuto do vínculo, poderia se passar pelo caixa principal.
- **Reinício fora do serviço:** rodando à mão (`python -m terminal`), a troca de modo reexecuta o processo; se falhar, feche e abra o PDV2 de novo. Instalado, quem reinicia é o serviço.

## 9. Caixa principal e caixas adicionais (rede da loja)

O mesmo instalador serve para os dois papéis. O que muda é o arquivo `mode.json` na pasta de dados, escrito pela tela.

| Papel | O que roda | Portas |
|---|---|---|
| Caixa principal (`server`) | O terminal de sempre em `127.0.0.1:8010`, mais a rede entre caixas quando o plano inclui caixas adicionais | TCP 8011 (TLS) e UDP 8012 (busca), entrada, só rede local |
| Caixa adicional (`station`) | Uma ponte em `127.0.0.1:8010` que repassa tudo ao caixa principal; não tem banco de dados | Nenhuma de entrada |

- **Virar caixa adicional:** no computador novo, na tela de ativação, "Este é um caixa adicional". O serviço reinicia sozinho (arquivo `updates\restart.request`, o mesmo da atualização) e abre a tela `/station/`.
- **Ligar ao principal:** no caixa principal, **Sistema → Adicionar caixa** mostra 6 números por 5 minutos; no adicional, digitar os números. Se a busca automática não achar, digitar o endereço que aparece na tela do principal.
- **Voltar a ser principal:** na tela do caixa adicional sem vínculo, "Este computador é o caixa principal? Voltar".
- **Caixa principal fora do ar:** o adicional para de vender, mostra o diagnóstico e tenta de novo a cada 2 segundos. Depois de 1 minuto oferece "Assumir como caixa principal" (pede PIN de gerente).
- **Impressão:** o recibo da venda feita no caixa adicional sai na impressora configurada **naquele** computador.
- **Variáveis para suporte e testes:** `PDV_MODE` (`server`/`station`, vence o arquivo), `PDV_LAN_PORT` (padrão 8011), `PDV_DISCOVERY_PORT` (padrão 8012), `PDV_LAN_HOST` (padrão `0.0.0.0`).

### 9.1 Testes manuais em Windows real (dois computadores na mesma rede)

- [ ] Principal com plano sem caixas adicionais: `netstat -ano | findstr 8011` não mostra nada.
- [ ] Principal com plano que inclui caixas: 8011 em escuta; regra `PDV2 - Servidor da loja` presente (`netsh advfirewall firewall show rule name="PDV2 - Servidor da loja"`), mesmo sem ter marcado a opção no instalador.
- [ ] Adicional: escolher "Este é um caixa adicional"; a tela `/station/` aparece em até 10 s sem fechar o navegador; achar o principal sozinho; digitar o código; cair na tela de PIN.
- [ ] Vender no adicional: o estoque muda na tela do principal na hora; o recibo sai na impressora do adicional.
- [ ] Rede marcada como **Pública** em um dos dois: o adicional mostra a orientação de mudar para Particular.
- [ ] Puxar o cabo do principal no meio de uma venda: aviso "O caixa principal está fora do ar"; recolocar o cabo; repetir a venda; ela aparece uma única vez.
- [ ] Reiniciar o Windows do principal: o adicional volta sozinho, sem novo código.
- [ ] Trocar o IP do principal (renovar DHCP): o adicional acha o novo endereço e avisa que corrigiu.
- [ ] Remover o caixa em **Sistema → Caixas da loja**: o adicional mostra "Este caixa foi removido".
- [ ] Antivírus/SmartScreen não bloqueiam a escuta em 8011 nem a busca em 8012.
- [ ] **Velocidade da tela do adicional:** com as ferramentas do navegador abertas (F12 → Rede) no caixa adicional, bipar 10 itens. Cada chamada `/api/...` deve responder em poucas dezenas de milissegundos, parecido com o principal. Se ficar perto de 200 ms a mais por chamada, é o atraso de Nagle/ACK (o servidor da loja liga `TCP_NODELAY` e manda cabeçalhos e corpo de uma vez; em loopback esse atraso não aparece, por isso a medição é manual).
- [ ] **Quatro adicionais vendendo ao mesmo tempo** (ou o máximo do plano): nenhuma venda devolve "O caixa principal está ocupado". O limite de conexões é 128 ou 12 por caixa do plano, o que for maior.
- [ ] **Resposta perdida na impressão:** fazer uma venda no adicional e puxar o cabo dele logo em seguida; recolocar. O recibo aparece na fila de impressão do adicional como "incerto" e pode ser reimpresso, sem reiniciar o PDV2.
- [ ] **Banco danificado no principal** (suporte: renomear `terminal.sqlite3` com o serviço parado e corromper uma cópia): o adicional mostra "O caixa principal está com o banco de dados danificado. Chame o suporte.", sem diagnóstico de rede e sem o botão de assumir.
- [ ] **Dois terminais na mesma loja:** com o principal das estações desligado e outro terminal da mesma loja ligado, o adicional continua em "fora do ar" e oferece "Assumir como caixa principal" depois de 1 minuto (não pede código do outro terminal).
