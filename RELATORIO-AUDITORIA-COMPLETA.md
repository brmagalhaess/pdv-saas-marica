# PDV2 — Relatório de auditoria técnica e plano de evolução

**Data da auditoria:** 9 de outubro de 2026  
**Versão analisada:** V2, commit `cdf5b5a`  
**Escopo:** produto web da empresa, administrador SaaS, terminal local, sincronização, licenciamento, impressão, estoque, caixa, integrações, operação offline e preparação para produção.

## 1. Resumo executivo

O PDV2 já possui uma base técnica consistente para pilotos supervisionados:

- aplicação web multiempresa com PostgreSQL;
- painel do administrador SaaS e painel da empresa;
- terminal local com SQLite em modo WAL, caixa, vendas, descontos autorizados, suspensão, cancelamento, impressão e fila persistente;
- sincronização em lotes com idempotência e autenticação por licença/terminal;
- controle de planos, limites, terminais e operadores;
- integração inicial preparada para Asaas e Focus NFe;
- Docker Compose para ambiente local e serviços separados para nuvem, terminal, gateway, worker e backup;
- aplicativo iOS de monitoramento do proprietário e PWA para acesso móvel.

Ele ainda **não está pronto para ser vendido como PDV fiscal e financeiro completo** sem uma etapa de homologação. Os maiores riscos são fiscais, pagamentos eletrônicos, conflitos de estoque quando há mais de um terminal offline, operação de infraestrutura, validação física de hardware e observabilidade. O terminal atual é adequado como base de operação local, mas precisa de uma experiência de caixa mais direta, com atalhos, estados visíveis e menos campos secundários.

O trabalho recomendado é separar o produto em três níveis:

1. **Piloto não fiscal:** venda, caixa, estoque e recibo não fiscal, com poucos clientes e acompanhamento diário.
2. **Produção comercial:** licença, backup externo, monitoramento, fiscal homologado e pagamentos testados.
3. **Plataforma competitiva:** módulos por segmento, promoções, compras, devoluções, TEF/Pix, relatórios e integrações contábeis.

Não se deve anunciar NFC-e, TEF, contingência fiscal ou sincronização perfeita de estoque como recursos prontos enquanto os itens P0 deste relatório não forem concluídos.

## 2. Arquitetura encontrada

```text
                         ┌─────────────────────────┐
                         │ Cloud / SaaS             │
                         │ Django + PostgreSQL      │
                         │ empresas, planos,       │
                         │ usuários, licenças, API  │
                         └────────────┬────────────┘
                                      │ HTTPS / Bearer / sync em lotes
                         ┌────────────▼────────────┐
                         │ Terminal local           │
                         │ Flask + SQLite WAL       │
                         │ vendas, caixa, fila,     │
                         │ impressora, cache        │
                         └────────────┬────────────┘
                                      │ loop do worker
                         ┌────────────▼────────────┐
                         │ CUPS / ESC-POS / ZPL     │
                         │ impressora, leitor,      │
                         │ balança e gaveta         │
                         └─────────────────────────┘
```

### Componentes principais

| Área | Implementação atual | Avaliação |
|---|---|---|
| Nuvem | Django/Gunicorn, PostgreSQL, autenticação por sessão/CSRF | Boa base multiempresa |
| Terminal | Flask/Waitress em `8010`, SQLite WAL, interface no navegador | Boa base offline, ainda depende do serviço local em execução |
| Gateway | Caddy em `8000` nos Compose locais | Adequado para desenvolvimento e piloto |
| Sincronização | `terminal/sync.py`, lotes de até 50 eventos, ACK após commit | Idempotente, mas sem resolução de conflitos |
| Licença | claims assinados, plano, terminal, operador e validade | Precisa de política única e tela de diagnóstico |
| Impressão | CUPS/ESC-POS para recibo; ZPL/EPL para etiquetas dedicadas | Precisa de matriz de compatibilidade física |
| Fiscal | clientes Focus NFe preparados | Ainda não é emissão fiscal homologada ponta a ponta |
| Cobrança | cliente Asaas e eventos de faturamento | Fluxo real, webhooks e cobrança em atraso precisam ser fechados |
| Mobile | monitor iOS e PWA | Android nativo e publicação ainda pendentes |
| Legado | `local_pdv/` | Protótipo antigo; não deve ser usado como núcleo da V2 |

## 3. O que já existe e deve ser preservado

### Terminal e operação local

- catálogo local e consulta por SKU, código de barras e nome;
- leitor de código de barras no fluxo de venda;
- unidades UN, KG e L, incluindo entrada de peso manual e balança Web Serial quando suportada;
- venda com dinheiro, Pix declarado, crédito e débito;
- troco para dinheiro;
- abertura, movimentação e fechamento de caixa;
- suspensão e retomada de vendas;
- descontos com autorização de superior e limite configurado;
- cancelamento com motivo e trilha de auditoria;
- recibo formatado e fila local de impressão;
- exportação e backup local da base;
- fila de eventos pendentes para sincronização;
- perfil separado para evitar misturar o histórico de outra empresa no mesmo diretório.

### Web da empresa

- dashboard, vendas, produtos, categorias, estoque, clientes, fornecedores, financeiro, lojas, empresa, usuários, terminais, assinatura e integrações;
- geração e pareamento de terminal por licença;
- configuração de dados da empresa e informações fiscais do emitente;
- cadastro de custo nas entradas de estoque e cálculo de custo médio;
- importação de estoque por CSV e metadados de documento;
- impressão de etiquetas deslocada para o administrador da empresa, evitando prometer que a TM-T20 é uma impressora de etiquetas;
- gerenciamento de operadores e permissões básicas.

### Administrador SaaS

- tenants, planos, assinaturas, dispositivos, usuários da plataforma, serviços, releases, faturas e auditoria;
- criação de licença e controle de limites por plano;
- suporte a elevação do usuário principal da empresa para administrador SaaS, quando aplicável;
- visão operacional básica dos serviços e dispositivos.

## 4. Matriz de lacunas e prioridades

**P0 — bloqueia produção ou cria risco alto.**  
**P1 — necessário para venda competitiva.**  
**P2 — diferenciação e escala.**  
**P3 — melhoria posterior por segmento.**

### P0 — fechar antes de uma operação comercial ampla

| Lacuna | Evidência | Risco | Critério de conclusão |
|---|---|---|---|
| NFC-e real | O fluxo fiscal ainda recebe payload e não emite automaticamente toda venda | Venda apresentada como fiscal sem autorização válida | certificado/CSC, séries, numeração, XML, QR Code, autorização, cancelamento, inutilização e contingência testados em homologação e produção |
| Contingência fiscal offline | A venda offline é recibo não fiscal | Internet indisponível impede emissão fiscal | fila fiscal assinada, contingência prevista pela UF, reenvio idempotente e tratamento de rejeição |
| TEF e adquirência | Crédito/débito são pagamentos declarados manualmente | divergência financeira e risco operacional | pelo menos um integrador TEF homologado, retorno aprovado/negado, cancelamento, estorno e conciliação |
| Pix integrado | Pix atual é declarado manualmente | pagamento não confirmado automaticamente | QR dinâmico, webhook, expiração, confirmação, cancelamento e conciliação |
| Conflito de estoque | cada terminal pode vender offline sem reserva central | dois terminais podem vender o mesmo saldo | reserva por terminal, limite de risco configurável, fila de conflitos no admin e regra de resolução auditável |
| Política de licença | código e documentação divergem entre validade offline de 72 horas e 7 dias | terminal operar fora do contrato ou bloquear indevidamente | uma política publicada no backend, terminal, documentação e testes, com relógio do servidor e contador visível |
| Backup e restauração | backup atual é local ao terminal | falha de disco ou ransomware pode apagar a operação | backup externo criptografado, retenção, restauração ensaiada e relatório de sucesso |
| Segurança de plataforma | MFA, rotação e revisão independente ainda não fechados | comprometimento de contas administrativas | MFA para SaaS, rotação de chaves, bloqueio de sessão, rate limit testado e revisão de permissões |
| Observabilidade | não há operação completa de erros, métricas e alertas | falha fica invisível até o lojista reclamar | logs estruturados, métricas, alertas de sync/fiscal/impressão e painel de saúde |
| Hardware real | protocolos foram implementados, mas não validados em laboratório | impressão cortada, balança/leitor incompatível ou gaveta sem acionamento | matriz por modelo, teste de descoberta, calibração, largura, corte, gap e procedimento de suporte |
| Deploy de produção | domínio, TLS, SMTP, VPS, migração e restore final ainda dependem de configuração | indisponibilidade e recuperação lenta | ambiente reproduzível, HTTPS, segredos externos, migração automatizada, health checks e runbook |

### P1 — necessário para competir com os PDVs consolidados

| Área | O que falta |
|---|---|
| Compras | pedido de compra, entrada por fornecedor, importação de XML/DF-e, conferência de divergência e custo por lote |
| Estoque | lote, validade, número de série, inventário guiado, transferência entre lojas e ajuste com aprovação |
| Vendas | devolução, troca, venda parcialmente cancelada, crédito ao cliente e reembolso por meio de pagamento |
| Preços | tabela por loja/canal, promoção por período, combo, preço por quantidade e margem mínima |
| Clientes | limite de crédito, histórico de compras, consentimento LGPD e programa de fidelidade |
| Financeiro | contas a pagar/receber, conciliação bancária, taxas de cartão, sangria/suprimento e fechamento por meio |
| Fiscal | motor de regras por UF/regime/NCM/CEST/CFOP, regras de imposto e atualização controlada |
| Relatórios | DRE simplificada, margem por produto, curva ABC, ruptura, perdas, ticket médio e exportação contábil |
| Migração | importador assistido de produtos, estoque, clientes, fornecedores e saldos de caixa |
| Suporte | chamados, anexos, SLA, histórico por tenant e comunicação de incidentes |
| Cobrança SaaS | tentativa automática, dunning, prorrata, troca de plano, suspensão gradual e reconciliação de webhook |
| Mobile | publicação Android, publicação iOS, atualização, sessão segura e monitoramento de versão |
| Distribuição | instalador Windows validado, atualização assinada e inicialização automática do serviço local |

### P2/P3 — evolução e segmentos

- mesas, comandas, cozinha e delivery para alimentação;
- ordem de serviço, agenda e recorrência para serviços;
- farmácia, lotes, validade, controlados e SNGPC;
- ecommerce, marketplace e integração com delivery;
- etiquetas avançadas, balança integrada por modelo e impressão em lote;
- multiestoque avançado, previsão de compra e recomendação de reposição;
- permissões personalizadas por ação e aprovação em duas etapas;
- temas, acessibilidade, idioma e operação em telas pequenas.

## 5. Estratégia offline recomendada

### 5.1 Princípio de autoridade

O servidor em nuvem deve ser a autoridade fiscal, contratual e financeira. O terminal é uma **borda operacional durável**: pode continuar vendendo dentro da política de risco do plano, registra tudo localmente e envia os eventos assim que a conexão voltar.

Não é seguro prometer que dois terminais desconectados manterão estoque global perfeitamente igual em tempo real. O comportamento correto é detectar a divergência, preservá-la e solicitar decisão do administrador.

### 5.2 Fluxo local e sincronização

1. Ao parear, o terminal recebe catálogo assinado, operadores, permissões, preço, estoque inicial, versão do catálogo e uma licença com validade.
2. Cada venda, cancelamento, movimento de caixa e ajuste gera um evento com `event_id`, `request_id`, `tenant_id`, `terminal_id`, operador, horário local, hora do servidor conhecida, versão do catálogo e hash do conteúdo.
3. O SQLite grava venda, movimentação de estoque e evento de saída na mesma transação.
4. O worker envia até 50 eventos, aguarda confirmação transacional e só então marca os eventos como aceitos.
5. O servidor responde com aceitos, duplicados, rejeitados e conflitos. Nenhuma resposta parcial deve apagar eventos ainda não confirmados.
6. O terminal baixa alterações por cursor/versão, aplica catálogo de modo atômico e preserva os eventos locais pendentes.
7. O administrador visualiza conflitos e escolhe a ação permitida: aceitar venda, ajustar estoque, cancelar evento ou solicitar revisão.

### 5.3 Estruturas que devem ser acrescentadas

- `sync_cursors`: último cursor aplicado por tenant/terminal;
- `catalog_versions`: versão, hash e data de cada snapshot;
- `stock_reservations`: quantidade de risco reservada por terminal e validade;
- `sync_conflicts`: evento, saldo esperado, saldo recebido, motivo, status e responsável;
- `device_heartbeats`: último contato, versão, fila pendente, relógio e saúde;
- `fiscal_queue`: venda, tentativa, rejeição, XML, chave e contingência;
- `payment_transactions`: autorização, NSU/TID, meio, status, estorno e conciliação.

### 5.4 Política de venda sem internet

- mostrar permanentemente `ONLINE`, `OFFLINE`, `SINCRONIZANDO` ou `BLOQUEADO` no cabeçalho;
- mostrar tempo restante da licença e da última sincronização;
- permitir venda offline somente dentro do prazo assinado e do limite do plano;
- por padrão, impedir venda de produto sem saldo local; liberar saldo negativo apenas para perfil e plano configurados;
- usar reserva local por terminal para reduzir sobre venda;
- bloquear novas vendas quando a licença expirar, mantendo consulta, exportação e reimpressão;
- usar horário do servidor e detectar desvio do relógio do computador;
- reprocessar eventos rejeitados sem alterar o `event_id`;
- nunca apagar uma venda local por erro de rede.

### 5.5 Operação do serviço local

Hoje o worker sincroniza enquanto o processo do terminal está rodando. Para uma operação real, o instalador deve:

- instalar o serviço local como serviço do sistema;
- iniciar automaticamente com o computador;
- manter o worker mesmo quando a janela do navegador estiver fechada;
- abrir a interface somente quando solicitada;
- registrar saúde, fila, última sincronização e espaço em disco;
- permitir exportação de emergência em pendrive;
- fazer backup local rotativo e enviar cópia criptografada para a nuvem.

Para uma única loja com poucos terminais, um serviço local por computador é suficiente. Para lojas maiores, deve ser oferecido um **Store Edge** opcional: um computador/mini servidor local mantém o SQLite central da loja e os terminais se conectam a ele. Essa opção só deve ser introduzida depois de fechar autenticação local, backup e conflitos; criar um servidor local sem essas garantias aumenta a complexidade.

## 6. Redesign do terminal

O terminal já recebeu uma primeira melhoria visual. O próximo desenho deve seguir o padrão conhecido de caixa, mantendo a velocidade do teclado e facilitando o toque:

```text
┌ status online • empresa • caixa • operador • hora • sincronização ┐
├ busca/código de barras ───────────────┬ carrinho fixo              ┤
│ categorias e produtos                 │ item, qtd, preço, desconto │
│ atalhos e últimos itens               │ subtotal e total           │
│                                       │ ações rápidas               │
├ F2 produto  F4 pagamento  F6 suspender┴ pagamento destacado        ┤
└ caixa aberto • total • fila • impressora • leitor • balança       ┘
```

### Regras de experiência

- alvo de toque mínimo de 44 px;
- campo de código de barras sempre focável após uma venda;
- busca local primeiro, sem esperar a nuvem;
- atalhos: `F2` buscar, `F4` pagar, `F6` suspender, `F8` reimprimir, `Esc` limpar modal, `Enter` confirmar;
- quantidade e desconto editáveis diretamente na linha do carrinho;
- pagamento aberto em painel grande com dinheiro, Pix, crédito e débito;
- dinheiro pede valor recebido e calcula troco; Pix/cartão pedem apenas confirmação/retorno do meio;
- ações destrutivas exigem modal padronizado, motivo e permissão;
- impressora, leitor, balança e sincronização possuem estado visual e teste rápido;
- etiquetas ficam no administrador da empresa; o terminal imprime somente recibo de venda;
- lista de produtos usa paginação/virtualização para milhares de itens e não recarrega a tela inteira a cada leitura;
- imagens são carregadas sob demanda e não podem bloquear a venda;
- o terminal mantém uma fila de impressão com reimpressão segura e aviso de duplicidade.

### Desempenho

- criar índice local normalizado para nome, SKU e código;
- debouncing curto somente na busca textual; leitura de scanner deve ser imediata;
- evitar chamadas de rede no caminho crítico da venda;
- fazer atualização de catálogo em segundo plano e trocar o snapshot de forma atômica;
- medir tempo de leitura, inclusão no carrinho, fechamento e impressão;
- testar com 10 mil produtos, 100 mil vendas históricas e fila de 500 eventos.

## 7. Integrações necessárias

### Fiscal

1. manter Focus NFe como primeiro adaptador, atrás de uma interface própria;
2. cadastrar certificado/CSC de forma criptografada, com rotação e acesso mínimo;
3. validar CNPJ, IE, regime, série, numeração, NCM, CFOP, CST/CSOSN, CEST e tributos;
4. emitir NFC-e após pagamento quando houver internet;
5. guardar XML, protocolo, chave de 44 dígitos e QR Code;
6. tratar rejeição, cancelamento, inutilização e contingência conforme a UF;
7. imprimir cupom com dados do emitente, itens, totais, pagamento, tributos, chave e QR Code;
8. homologar com contador e SEFAZ antes de chamar o produto de fiscal.

### Pagamentos

- selecionar um integrador TEF compatível com o hardware alvo e homologar crédito, débito, Pix, cancelamento e estorno;
- adicionar Pix dinâmico via PSP com webhook e conciliação;
- registrar NSU/TID/ID da transação e relacionar ao `sale_id`;
- impedir fechamento como pago quando o provedor não confirmou;
- separar pagamento declarado de pagamento integrado no relatório;
- integrar taxas e prazo de recebimento ao financeiro.

### Hardware

- leitores USB em modo teclado: validar prefixo/sufixo e foco do campo;
- Epson TM-T20 M249A: validar CUPS, largura, corte e gaveta;
- impressoras de etiqueta 50×30 mm: usar driver dedicado e calibração de gap; não tratar TM-T20 como impressora de etiqueta;
- balanças: manter Web Serial para modelos suportados e criar adaptadores por protocolo/modelo;
- fornecer tela de diagnóstico que imprime teste, lê código, lê peso e aciona gaveta;
- publicar tabela de modelos testados, sistema operacional, driver e limitações.

### Cobrança e comunicação

- finalizar webhook Asaas com assinatura, idempotência e reconciliação;
- dunning para pagamentos atrasados, aviso antes da suspensão e período de recuperação;
- e-mail transacional para convite, licença, bloqueio, falha de sincronização e nota rejeitada;
- exportação contábil CSV/XML e integração futura com contador.

## 8. Melhorias do administrador SaaS

### Visão operacional

- mapa de tenants por plano, status, vencimento e saúde;
- terminais online/offline, última sincronização, fila pendente, versão e relógio divergente;
- fila de conflitos de estoque, fiscal, pagamento, impressão e backup;
- incidentes ativos e histórico de indisponibilidade;
- busca global por empresa, CNPJ, terminal, licença, usuário e fatura.

### Governança e segurança

- MFA obrigatório para administradores da plataforma;
- papéis separados: proprietário da plataforma, suporte, financeiro, operações e somente leitura;
- elevação de usuário da empresa para admin SaaS com aprovação, expiração opcional e auditoria;
- acesso de suporte por sessão temporária, motivo obrigatório e registro de ações;
- rotação de chaves de licença e assinatura com período de sobreposição;
- exportação e retenção de auditoria por política configurável.

### Assinaturas e planos

- matriz visual de recursos, terminais, usuários, lojas, armazenamento e prazo offline;
- uso atual versus limite por tenant;
- troca de plano com prorrata;
- fatura, webhook, tentativa, falha, estorno e reconciliação;
- suspensão gradual: aviso, somente leitura, bloqueio de novas vendas e reativação;
- licenças VIP com validade e motivo, sem alterar silenciosamente o plano comercial.

### Releases e suporte

- canais `stable`, `pilot` e `canary`;
- manifesto assinado com versão mínima, hash e rollback;
- rollout por tenant/percentual;
- tickets com SLA, anexos, comentários e vínculo com logs;
- feature flags por plano, segmento e tenant;
- página pública de status opcional.

## 9. Critérios para declarar “pronto para produção”

O produto só deve ser promovido de piloto quando todos os critérios abaixo forem comprovados:

- 30 dias de operação piloto sem perda de venda nem evento de sync;
- restauração de backup em ambiente limpo documentada;
- NFC-e homologada e emissão/contingência testadas na UF escolhida;
- TEF/Pix integrado ou contrato explícito de pagamento declarado no plano inicial;
- matriz física aprovada para impressora, leitor, balança e gaveta;
- teste de 10 mil produtos, 10 terminais e 100 mil eventos;
- licença expirada, relógio alterado, rede intermitente e servidor indisponível testados;
- relatório de conflitos de estoque e procedimento de correção;
- MFA e revisão de permissões concluídos;
- domínio, TLS, segredos externos, alertas e contatos de incidente configurados;
- documentação de instalação, operação, backup, restauração e suporte entregue;
- contrato comercial especifica claramente se o pacote é fiscal, não fiscal, com equipamento ou somente software.

## 10. Plano de execução de 90 dias

### Dias 1–15 — confiabilidade da base

- alinhar a política de licença (72 horas normal, 24 horas em atraso, ou outro valor comercial único);
- adicionar cursor, conflitos, heartbeat e diagnóstico de sincronização;
- instalar worker como serviço e criar backup externo criptografado;
- corrigir testes de falha de rede, reenvio, duplicidade e relógio;
- documentar matriz de hardware e procedimento de suporte.

### Dias 16–30 — operação de caixa

- finalizar layout por zonas e atalhos;
- virtualizar catálogo e medir latência de leitura;
- padronizar modais, estados de impressora e pagamento;
- criar devolução/troca, sangria/suprimento e relatório de fechamento;
- adicionar tela de conflitos no admin da empresa.

### Dias 31–50 — fiscal e pagamentos

- concluir cadastro fiscal, certificado/CSC e regras mínimas da UF piloto;
- homologar emissão, QR Code, cancelamento e contingência;
- escolher TEF/PSP, integrar transação, estorno e webhook;
- separar pagamento declarado de integrado nos relatórios.

### Dias 51–70 — administração SaaS

- MFA, papéis da plataforma, suporte temporário e auditoria;
- saúde de tenants, dispositivos, filas e incidentes;
- uso por plano, prorrata, dunning e reconciliação;
- releases assinados, canary e rollback.

### Dias 71–90 — piloto comercial

- executar piloto com mercados/mercearias de Maricá;
- treinar operadores e contador;
- medir vendas, tempo de checkout, falhas de impressão, sync e chamados;
- corrigir os três maiores problemas por impacto;
- publicar apenas recursos homologados e atualizar os planos comerciais.

## 11. Testes que devem entrar na esteira

### Unidade e integração

- idempotência de cada tipo de evento;
- venda offline com reconexão parcial;
- conflito de estoque entre dois terminais;
- licença normal, atrasada, expirada, revogada e com relógio adiantado;
- pagamento aprovado, negado, timeout, estornado e duplicado;
- NFC-e autorizada, rejeitada, cancelada e em contingência;
- impressão em 58/80 mm, corte, falta de papel e reimpressão;
- etiqueta 50×30 mm com gap e quantidade múltipla;
- backup, restauração e migração;
- permissões por papel e ações auditáveis.

### Aceitação com usuário

- operador abre caixa, faz venda por scanner, recebe dinheiro, imprime e fecha;
- operador fica sem internet, continua dentro da política e sincroniza depois;
- gerente autoriza desconto e cancelamento;
- administrador cadastra produto, entrada, custo médio e etiqueta;
- dono acompanha terminal, vendas e saldo no iOS/PWA;
- suporte identifica um terminal com fila presa sem acessar o banco diretamente.

## 12. Decisões registradas

1. **Etiquetas ficam no administrador da empresa.** A Epson TM-T20 é uma impressora térmica de recibo; impressão de etiqueta 50×30 mm exige impressora/driver com avanço e gap apropriados.
2. **O terminal imprime recibo, não etiqueta.** Isso reduz erro operacional e evita que o caixa escolha uma fila incompatível.
3. **`local_pdv/` é legado.** A evolução deve ocorrer em `terminal/`, `cloud/`, `web/` e `shared/`; o legado deve ser marcado ou removido em uma tarefa própria após confirmar que nenhum Compose o utiliza.
4. **Offline não significa duas bases independentes.** A base local é uma borda com outbox; a nuvem mantém a autoridade e recebe eventos idempotentes.
5. **Fiscal e pagamento devem ter adaptadores.** A aplicação não deve acoplar o fluxo de venda a um único provedor.

## 13. Conclusão

O PDV2 está em uma boa etapa de fundação e já pode continuar em piloto não fiscal supervisionado. As próximas entregas de maior retorno são: confiabilidade offline com conflitos visíveis, operação do worker como serviço, backup externo, layout de caixa orientado a teclado/toque, MFA e saúde do SaaS, seguida de homologação fiscal e integração de pagamentos.

Este documento deve ser usado como backlog de produto e checklist de release. Cada item P0 precisa ter uma issue, teste de aceitação, responsável e evidência antes de ser marcado como concluído.

## 14. Evidência desta auditoria

Em 9 de outubro de 2026 foram executados os testes disponíveis nos containers do projeto:

- `cloud`: 32 testes, todos aprovados;
- `terminal`: 36 testes, todos aprovados;
- validação de interpolação do Compose com `.runtime/local.env`: aprovada;
- os testes `shared/tests` não rodaram no Python do host porque a dependência `cryptography` não está instalada; a tentativa no container não encontrou esse diretório no contexto de imagem. Isso deve ser corrigido na esteira CI para que licenciamento e releases sejam sempre testados junto com o restante do projeto.

Esses resultados validam a base automatizada existente, mas não substituem os testes físicos de periféricos, emissão fiscal, pagamentos, rede interrompida e restauração descritos acima.

## 15. Incremento aplicado após a auditoria — 9 de outubro de 2026

Este incremento aplica a primeira tranche P0 sem declarar homologações que ainda dependem de terceiros:

- `SyncConflict` no cloud, com tenant/loja/terminal, evento, saldo observado, estado, resolução, responsável e auditoria;
- criação automática de conflito quando uma venda offline aceita deixa o saldo central negativo;
- endpoints protegidos para listar e resolver conflitos no painel da empresa;
- heartbeat autenticado do terminal a cada 30 segundos, com versão, fila pendente e presença para o monitoramento;
- fila SQLite local para conflitos e diagnóstico no terminal, sem apagar eventos ou vendas;
- política única documentada em 72 horas para licença normal (24 horas durante atraso), alinhada ao emissor cloud;
- navegação **Administração → Conflitos offline** e métricas locais de conflitos;
- testes de integração para conflito resolvível e heartbeat;
- documentação atualizada em `docs/SYNC-VENDAS.md`, `docs/API-V2.md` e `docs/STATUS-V2.md`.

Continuam bloqueadores externos de produção: homologação NFC-e/contingência com certificado e SEFAZ, TEF/adquirência, Pix dinâmico, MFA da plataforma, backup externo restaurado no ambiente final, matriz física de periféricos e deploy com domínio/TLS/segredos reais. O produto deve ser anunciado como **piloto não fiscal** até essas evidências existirem.
