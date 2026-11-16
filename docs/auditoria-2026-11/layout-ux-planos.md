# Layout, usabilidade, planos e permissões — auditoria de novembro/2026

## Caixa (terminal)

### Proposta

## Proposta: Caixa Rápido CrownTech

**Estrutura fixa em 4 faixas (sem rolagem de página)**

1. **Barra de status (40px, topo)**: lockup "CrownTech PDV" · loja/caixa/operador · Online/Offline · Fila N · Licença · Impressora · Balança · relógio. Nada é ocultado por largura; a barra inteira muda de cor: verde (ok), âmbar (offline/fila), vermelho (licença ou caixa fechado).
2. **Zona de itens (60%)**: campo único de leitura (aceita código, nome e `3*código`), bloco **Último item** (nome 28px, qtd x unitário, subtotal 40px) e lista zebra numerada em 18px com auto-scroll. Setas selecionam, `Del` cancela, `*` muda quantidade.
3. **Zona de valor (40%)**: **TOTAL em 72-96px** (tabular-nums), economia de promoções, e pagamento inline (sem modal): `1` Dinheiro, `2` Pix, `3` Débito, `4` Crédito, `5` Fiado; valores rápidos R$ 10/20/50/100; **TROCO em 56px**. `Enter` confirma.
4. **Régua de teclas (56px, rodapé, sempre visível e tocável)**:
`F1 Ajuda · F2 Buscar · F3 Quantidade · F4 Cliente/CPF · F5 Desconto · F6 Em espera · F7 Sangria/Suprimento · F8 Cancelar item · F9 Reimprimir · F10 Pagar · F11 Foco · F12 Fechar caixa · Esc Voltar`
A régua é contextual (no pagamento mostra as formas), esmaece o indisponível e marca com cadeado o que pede PIN. Sai de um único mapa de atalhos no JS.

**Fluxo alvo**: leitura dos itens -> `F10` -> `1` -> valor -> `Enter`. Tela "Troco R$ X" fica até a próxima leitura; o cupom imprime em segundo plano.

**Modos**
- **Scanner** (mercado): sem grade de produtos; catálogo vira overlay do `F2` com setas + Enter.
- **Toque** (moda, conveniência): grade com favoritos e categorias fixas, botões >= 48px.
- **Foco (`F11`)**: tela cheia, esconde abas; só venda.

**Temas e tokens**
Claro, Escuro e Alto contraste via `data-theme`; tokens `--bg --surface --ink --muted --brand --ok --warn --danger`. Base 16px, mínimo 13px, contraste >= 4.5:1, alvos >= 44px. Sugestão de marca: azul-noite `#0d2232` + verde `#0b8f6f` como ação, âmbar `#b86b20` aviso, vermelho `#b3404b` erro.

**Componentes novos**
- Modal único (Esc, trap de foco, Enter = primária, erro inline).
- Faixa de erro de leitura em largura total + bipe ok/erro (Web Audio).
- Painel lateral "Em espera" e "Sangria/Suprimento".
- Menu do operador: Trocar operador / Fechar caixa.
- Login com teclado numérico tocável; manutenção atrás de PIN.

**Diferenciais (além de SysPDV, Linx, Bling, MarketUP)**
- Paleta de comandos (`Ctrl+K` ou `?`): digitar "sangria", "cpf", "reimprimir".
- Régua F que ensina: modo treinamento destaca a próxima tecla.
- Segundo display do cliente (`/display`): itens, total, QR Pix e marca.
- Fechamento cego com contagem por cédulas.
- Indicador de fila offline com previsão ("12 vendas aguardando envio").

**Marca CrownTech**
Lockup na barra e no login, tela ociosa com logo + QR, rodapé do cupom "Sistema CrownTech PDV" (remoção como benefício de plano white-label).

**Cupom/NFC-e**
Cabeçalho do emitente em destaque, itens em duas linhas (descrição / qtd x unitário = total), TOTAL em largura dupla, bloco de pagamento e troco, QR centralizado, tributos, e assinatura CrownTech após "Volte sempre".

**Ordem de implementação**
1. Mapa de atalhos + régua F + Enter no pagamento.
2. Total/troco grandes, último item, auto-scroll.
3. Barra de status e marca.
4. Tokens/temas e limpeza do CSS.
5. Modos Scanner/Toque, display do cliente, paleta de comandos.

### Problemas encontrados

- **[alto] Só existe um atalho de função (F2); nenhuma operação de caixa tem tecla F** (`terminal/static/terminal.js:485-487`): O único tratamento de tecla de função no arquivo inteiro é `if(event.key==='F2'){event.preventDefault();focusScannerInput(true);return}`. Não há F1, F3..F12, nem Ctrl/Alt (linha 504 só os ignora). Pagar, desconto, quantidade, cancelar item, CPF, sangria, reimprimir, fechar caixa e trocar de aba dependem de mouse/toque. Em SysPDV/Linx toda a venda roda em F-keys. **Correção:** Criar um mapa central de atalhos (objeto {tecla, rótulo, ação, permissão, contexto}) consumido por um único listener e pela régua visual: F1 ajuda, F2 buscar, F3 quantidade, F4 cliente/CPF, F5 desconto, F6 espera, F7 sangria/suprimento, F8 cancelar item, F9 reimprimir, F10 pagar, F11 modo foco, F12 fechar caixa.
- **[alto] Legenda de atalhos quase invisível e some em telas menores** (`terminal/templates/index.html:68 e terminal/static/terminal.css:21,23`): A única dica é `<span class="shortcut-hint"><kbd>F2</kbd> buscar <kbd>Enter</kbd> adicionar</span>`, com `.shortcut-hint{color:#81959b;font-size:11px}` e kbd de 10px (contraste calculado 2,79:1 sobre #edf3f4). Em `@media(max-width:850px)` há `.shortcut-hint{display:none}`. **Correção:** Régua de teclas fixa no rodapé do caixa (56px), sempre visível, com keycap + rótulo em 14px e contraste >= 4.5:1; cada item também é botão tocável (>= 48px). Nunca ocultar; em telas estreitas vira rolagem horizontal.
- **[alto] Finalizar venda exige mouse e 4 cliques; Enter não confirma no modal de pagamento** (`terminal/static/terminal.js:392-399, 404-407, 416-417; terminal/static/terminal.css:174`): Pagamento fica oculto na tela (`.checkout-details-home{display:none}`) e só aparece no modal aberto por "Revisar e confirmar venda". No modal, `onKey` trata apenas Escape e Tab; Enter no campo `payCash` não faz nada. Caminho em dinheiro: clicar Revisar -> clicar Dinheiro -> digitar -> clicar Confirmar venda -> fechar o diálogo de comprovante (`await receiptDialog(...)` sempre abre). Por teclado são 3 Tabs (Dividir pagamento, Voltar, Confirmar) + Enter. O foco do comprovante cai em "Imprimir no sistema" (linha 175), então Enter abre a impressão do navegador. **Correção:** Pagamento inline no trilho direito (sem modal): F10 entra no pagamento, teclas 1-5 escolhem a forma, Enter confirma. Comprovante imprime em segundo plano; a prévia vira opcional (F9). Meta: venda em dinheiro = leitura + F10 + valor + Enter.
- **[alto] Troco em 12px e, após a venda, só em um toast de 5 segundos** (`terminal/static/terminal.css:180; terminal/static/terminal.js:331, 362, 26`): `.checkout-change{...font-size:12px;font-weight:800}` exibe "Troco: R$ ...". Depois de confirmar, o troco aparece apenas em `toast('Venda concluída: ... · Troco ...')`, que some em 5000 ms no canto inferior direito, e dentro da prévia do cupom. **Correção:** Bloco TROCO em 56px+ ao lado do total durante o pagamento e uma tela de conclusão ("Troco R$ X") que permanece até a próxima leitura ou tecla. Botões de valor rápido (R$ 10/20/50/100, valor exato).
- **[alto] Sangria, suprimento e venda em espera existem no código mas não têm acesso na tela** (`terminal/templates/index.html:117; terminal/static/terminal.js:422-424, 468`): `supplyBtn` e `withdrawBtn` estão dentro de `<div id="cashStateProxy" class="hidden" aria-hidden="true">`; os handlers `cashAction('supplied'|'withdrawn')` nunca podem ser acionados. `suspend()` e `loadSuspended()` referenciam `$('suspendedList')`, id que não existe em index.html, e nenhuma chamada as invoca (as rotas /api/carts/suspended existem em terminal/app.py:336-347). **Correção:** Expor F7 (sangria/suprimento) e F6 (colocar/retomar venda em espera) na régua, com painel lateral de vendas suspensas; remover o proxy oculto.
- **[alto] Hierarquia visual invertida: total pequeno para leitura à distância e cabeçalho decorativo grande** (`terminal/static/terminal.css:177 e :21; terminal/templates/index.html:68, 86`): `.sale-total-prominent strong{font-size:34px}` num trilho de 390-440px; ao mesmo tempo `.section-head h1{font-size:32px}` ("Frente de caixa" + eyebrow + subtítulo estáticos) ocupa cerca de 70px de altura. Nome do item no carrinho tem 13px, preço 17px. Não há `tabular-nums` (grep sem ocorrências), então os dígitos mudam de largura. **Correção:** Total em 72-96px com `font-variant-numeric:tabular-nums`, legível a 2 m; remover o h1 do caixa e usar a altura para o bloco "Último item" (nome 28px, qtd x unitário, subtotal).
- **[alto] Busca por nome não funciona só com teclado** (`terminal/static/terminal.js:454-455, 279`): `processScannerInput` só adiciona se houver correspondência exata de barcode/SKU/id ou exatamente 1 resultado; com vários resultados na grade, Enter mostra `toast('Nenhum produto encontrado para este código',true)`. Não há navegação por setas na grade (`.product` só tem `onclick`). Tab também é interceptado no campo quando há texto (linha 455). **Correção:** Lista de resultados com seleção ativa: setas movem, Enter adiciona o destacado, números 1-9 escolhem direto. Mensagem distinta para "vários resultados".
- **[alto] Carrinho mostra poucos itens em 1366x768, sem rolagem automática nem destaque do último item** (`terminal/static/terminal.css:66-74, 21; terminal/static/terminal.js:329`): `.cart-items{min-height:120px}` divide o trilho com cabeçalho, total, desconto, botão e nota legal; somando os valores do CSS o mínimo do trilho fica em torno de 566px, enquanto em 1366x768 a grade tem cerca de 580px em quiosque (menos com a barra do navegador), sobrando 2-3 linhas de item; abaixo disso o botão de venda sai da área visível (`.cart{overflow:auto}`). `renderCart` recria o innerHTML sem `scrollIntoView`/`scrollTop` (grep sem ocorrências) e sem marcar o item recém-lido. Estimativa por leitura do CSS, não medida em navegador. **Correção:** Lista de itens como área principal (60% da tela), linhas zebra de 18px numeradas, auto-scroll para o último, destaque por 1,5 s no item lido e bloco fixo "Último item".
- **[alto] Indicadores de offline e fila de sincronização somem em monitores comuns de caixa** (`terminal/static/terminal.css:79, 55-56; terminal/templates/index.html:16`): `@media(max-width:1250px){.top-status .badge:nth-child(2),.top-status .badge:nth-child(4){display:none}}` oculta exatamente `networkBadge` (2o) e `pendingBadge` (4o). Além disso `.top-status{overflow:hidden;flex-wrap:nowrap}` corta os selos restantes sem aviso e `.badge{max-width:190px;text-overflow:ellipsis}` trunca "Empresa · Loja · Caixa". **Correção:** Barra de status dedicada com prioridade fixa: Online/Offline e Fila nunca ocultos; ícone + texto curto; a barra inteira muda de cor (verde/âmbar/vermelho) conforme o pior estado.
- **[médio] Teclas digitadas fora do campo de busca são descartadas** (`terminal/static/terminal.js:504-508, 493-502`): Com foco em algo não editável, cada tecla imprimível vai para `scannerCapture.buffer` e recebe `event.preventDefault()`; só vira leitura se terminar em Enter/Tab em até 250 ms com intervalo <= 150 ms entre teclas. Digitação humana (após clicar numa categoria, por exemplo) some sem feedback. O mesmo preventDefault atinge a barra de espaço em botões focados, o que tende a impedir a ativação por teclado no Chromium. **Correção:** Redirecionar qualquer tecla imprimível para o campo de leitura (focar e inserir o caractere) em vez de descartar; manter a detecção de rajada apenas para decidir o auto-Enter.
- **[médio] Erros dentro dos diálogos de devolução e fiado ficam atrás do fundo escurecido** (`terminal/static/terminal.css:1 (#toast z-index:50) e :8 (.confirm-backdrop z-index:80); terminal/static/terminal.js:239, 272`): `returnDialog` e `receiveCreditDialog` reportam erro com `toast(error.message,true)` enquanto o backdrop (`position:fixed;inset:0;background:rgba(9,27,38,.72);backdrop-filter:blur(4px)`) cobre a tela com z-index maior que o do toast. **Correção:** Erro inline no próprio diálogo (como `confirm-error` já faz em textDialog) e toast com z-index acima dos backdrops.
- **[médio] Diálogos de devolução, fiado e variação fora do padrão dos demais** (`terminal/static/terminal.js:207-224, 247-259, 284-291; terminal/static/terminal.css:8, 224, 234`): Esses três não registram `activeConfirm` (o F2 e a captura do leitor continuam ativos por trás), não têm trap de Tab; devolução e fiado não fecham com Esc nem clique fora. `.return-dialog`/`.variant-dialog` redefinem só `display:grid;gap` e herdam `grid-template-columns:auto 1fr` de `.confirm-dialog`, enquanto os diálogos de checkout e comprovante resetam para `minmax(0,1fr)` (css:93, 182) - provável layout em duas colunas não intencional. **Correção:** Um único componente de modal (Esc, trap de foco, Enter = ação primária, activeConfirm, `grid-template-columns:1fr`) usado por todos.
- **[médio] Chips de categoria desaparecem ao filtrar e dependem dos 150 produtos carregados** (`terminal/static/terminal.js:279, 277; terminal/store.py:924`): `availableCategories` é derivado de `state.products` (resultado atual), não de `state.categories` que a API devolve. Ao escolher uma categoria, `loadProducts` busca só ela e a barra passa a mostrar apenas "Todos" + a categoria ativa (2 cliques para trocar). A consulta tem `ORDER BY name LIMIT 150`, sem aviso de "há mais resultados". **Correção:** Renderizar chips a partir de `state.categories`; indicar "mostrando 150 de N" e paginar/rolar; em mercado, trocar a grade por favoritos + busca.
- **[médio] Estado vazio enganoso na busca e rótulo de botão inexistente** (`terminal/static/terminal.js:279; terminal/templates/index.html:71`): Busca sem resultado mostra o mesmo texto de catálogo vazio: "Nenhum produto disponível neste terminal. Clique em “Atualizar catálogo”...", mas o botão se chama "Atualizar". Produtos sem estoque são filtrados da grade (`available`), então o operador não distingue "sem estoque" de "não cadastrado"; a regra `.product.out` ficou morta. **Correção:** Três estados: catálogo vazio (com botão de ação no próprio estado), "nada encontrado para 'termo'" e item sem estoque exibido esmaecido com selo.
- **[médio] Erro de leitura sem alerta forte nem som** (`terminal/static/terminal.js:454; terminal/static/terminal.css:17`): Código não encontrado gera um ponto de 8px (`.scanner-dot.error`) em texto de 10px e um toast no canto; não há nenhum uso de Audio/beep/vibrate no arquivo (grep sem ocorrências). Com fila, o operador segue passando itens sem perceber. **Correção:** Bipe distinto para ok/erro (Web Audio, sem arquivo), faixa vermelha de largura total com o código lido até a próxima leitura válida.
- **[médio] Quantidade só pelo campo pequeno do carrinho; sem multiplicador** (`terminal/static/terminal.js:293, 329, 454; terminal/static/terminal.css:21`): `addProduct` soma sempre 1000 (1 un); alterar exige clicar no input da linha (`.cart-row input{height:35px}` em coluna de 70px) e sair do campo (`onchange`). Não há sintaxe `3*código` nem tecla de quantidade, padrão em SysPDV/Linx. **Correção:** Aceitar `N*código` e `N*` + leitura no campo único; F3 ou `*` edita a quantidade do item selecionado; +/- no item ativo.
- **[médio] Texto pequeno e contraste abaixo de WCAG AA em quase todos os rótulos** (`terminal/static/terminal.css:1, 21, 48`): Base `font-size:14px` com muitos rótulos de 9-11px. Razões calculadas: `.legal-note` #9aabb0 10px = 2,38:1; estado vazio #93a4aa = 2,58:1; `.cart-row small` = 2,82:1; `.payment-method small` 10px = 3,09:1; `.product .stock` 10px = 3,37:1; `.scanner-status` 10px = 3,88:1; botão primário branco sobre #0d9c7b = 3,47:1. **Correção:** Base 16px, mínimo 13px, texto secundário >= 4.5:1 (ex.: #51666e sobre branco), primário mais escuro (#087a63) para texto branco.
- **[médio] Alvos de toque abaixo de 44px** (`terminal/static/terminal.css:1, 21, 58, 62`): `.remove{font-size:22px}` em coluna de 22px; `.small{min-height:30px}` (Nova venda, Atualizar, ações de Vendas); `.category{padding:8px 14px}` com fonte 12px (cerca de 31px de altura); `.top-nav .nav-item{padding:8px 10px;font-size:11px}`; `.operator .secondary{min-height:31px}`; `.text-button{padding:8px}`. **Correção:** Mínimo 44x44px (48px nas ações do caixa), espaçamento >= 8px; remover item por tecla Del/F8 além do botão.
- **[médio] Nenhuma marca CrownTech no caixa nem no cupom** (`terminal/templates/index.html:9, 15; terminal/printers.py:250-251`): Busca por "crowntech" no repositório não retorna nada. A tela mostra apenas "PDV2 TERMINAL LOCAL" e o cupom termina em "OBRIGADO PELA PREFERENCIA!" / "Volte sempre", sem assinatura do fornecedor. **Correção:** Lockup "CrownTech PDV" na barra de status e na tela de login, tela ociosa com marca + QR, e linha de rodapé no cupom ("Sistema CrownTech PDV"), configurável por plano (white-label nos planos superiores).
- **[médio] Sem temas, modo escuro, alto contraste ou tela cheia** (`terminal/static/terminal.css:20 e arquivo inteiro`): Não há `prefers-color-scheme`, `data-theme`, `prefers-reduced-motion` nem `requestFullscreen` (grep sem ocorrências). Só 7 variáveis em `:root` e a maior parte das cores está fixa em hexadecimal espalhada pelas regras. **Correção:** Tokens semânticos (--bg, --surface, --ink, --brand, --ok, --warn, --danger) com temas Claro, Escuro e Alto contraste; preferência salva por terminal; F11 alterna tela cheia/modo foco.
- **[médio] "Sair" abre o fechamento de caixa e duplica o botão "Fechar caixa"** (`terminal/static/terminal.js:448; terminal/templates/index.html:24`): `async function logout(){if(state.session?.cash){await closeCashAndRefresh();return}...}`: com caixa aberto, "Sair" faz o mesmo que "Fechar caixa"; a troca de operador é "Bloquear caixa". Três botões de texto de 11px lado a lado com resultados que o rótulo não antecipa. **Correção:** Menu do operador com ações nomeadas pelo efeito: "Trocar operador (bloquear)" e "Fechar caixa (F12)"; remover "Sair" enquanto houver caixa aberto.
- **[médio] Fechamento de caixa já vem preenchido com o valor esperado** (`terminal/static/terminal.js:420`): `input.value=(summary.expected_cents/100).toLocaleString(...)` no campo "Dinheiro contado no caixa" e o relatório mostra o esperado antes da contagem; Enter confirma sem contar. **Correção:** Fechamento cego configurável: campo vazio, contagem por cédulas/moedas opcional, esperado e diferença só após confirmar.
- **[médio] Ações destrutivas expostas na tela de login, sem autenticação** (`terminal/templates/index.html:58-59; terminal/app.py:164-181`): "Corrigir pareamento sem operações" e "Criar novo perfil para outra empresa" aparecem para qualquer pessoa no login; `/api/new-profile` exige apenas `confirm:true` e ausência de operador logado (sem PIN de gerente), trocando o banco ativo do terminal. **Correção:** Mover para Sistema > Manutenção, atrás de PIN de gerente/proprietário; na tela de login deixar só operador, PIN (com teclado numérico tocável) e "Atualizar operadores".
- **[baixo] Status técnicos em inglês e ícones Unicode repetidos** (`terminal/static/terminal.js:425, 427, 428; terminal/templates/index.html:15, 18, 98`): Lista de vendas mostra `esc(s.status)` cru (completed/voided), fila de impressão mostra `j.status` e `j.driver`, auditoria mostra `e.action`. O glifo ▣ é usado para a marca, a aba Caixa, Dinheiro e o diálogo de caixa; os ícones dependem da fonte do sistema. **Correção:** Dicionário de rótulos em português com selo colorido; conjunto de ícones SVG inline próprio (um por conceito).
- **[baixo] Desconto só em percentual, com clique obrigatório e PIN sempre** (`terminal/templates/index.html:88; terminal/static/terminal.js:300`): Campo "Desconto (%)" + botão "Aplicar desconto"; `applyDiscount` sempre abre o diálogo de PIN, mesmo quando o operador logado é gerente; não há desconto em R$ nem por item. **Correção:** F5 abre desconto com alternância %/R$ e escopo item/venda; PIN só quando exceder o limite do papel do operador.
- **[baixo] CSS acumulado em camadas de sobrescrita, com regras mortas** (`terminal/static/terminal.css:1, 26, 33, 43, 70, 76`): A linha 1 é um bloco minificado único; `.sidebar`, `.bottom-grid`, `.cash-control-card` e `.product.out` não têm elemento correspondente; `.payment-grid` tem `grid-template-columns` redefinido várias vezes (linhas 1, 26, 27, 33, 44, 48); a regra de 1300px para `.checkout-grid` (linha 43) é anulada pela linha 70; `.topbar` muda de 76px para 72px e de sticky para relative (linha 214). **Correção:** Reescrever em arquivo por componente (tokens, layout, caixa, modais, cupom) com uma única definição por regra, antes de aplicar o redesenho.

## Painel da empresa

### Proposta

## Redesenho proposto do painel da empresa

**1. Estrutura por tarefas (6 áreas em vez de 23 itens)**
- **Início** — dashboard e primeiros passos
- **Vendas e caixa** — abas Vendas | Caixas | Comissões | Promoções
- **Produtos e estoque** — abas Produtos | Categorias | Saldos | Entradas | Inventário | Transferências
- **Clientes e financeiro** — abas Clientes | Fornecedores | A receber | A pagar
- **Relatórios** — galeria em cartões
- **Configurações** — página única com seções: Empresa, Lojas, Equipe, Terminais, Pagamentos, Impressoras, Fiscal e integrações, Assinatura, Auditoria

Sidebar recolhível (só ícones), com contador de pendências. No celular, barra inferior com 5 ícones.

**2. Topbar útil**
Seletor global de loja e período, busca (Ctrl+K), botão "+ Novo", sino de pendências (estoque baixo, títulos vencidos, conflitos offline, caixa com quebra), estado da sincronização dos terminais e menu do perfil com alternador de tema.

**3. Command palette e atalhos**
- `Ctrl+K` ou `/`: buscar tela, produto, cliente, venda; executar ações
- `N`: novo registro da tela atual
- `G` + `I/V/P/F/R/C`: ir para cada área
- `?`: painel de atalhos; `Esc`: fechar
Dica do atalho visível nos botões (ex.: "Novo produto  N").

**4. Dashboard**
- Linha de KPIs com variação vs período anterior e sparkline: faturamento, lucro bruto, vendas, ticket médio
- Gráfico de vendas por dia (barras, com linha do período anterior)
- Mapa de calor por hora, mix de formas de pagamento, top 5 produtos
- Bloco "Precisa de atenção" com links diretos
- Terminais: online/offline e fila pendente
- Chips: Hoje, Ontem, 7 dias, Mês, Personalizado
Fonte: relatórios já existentes em `/reports` (sales_by_day, sales_by_hour, sales_by_payment, products_abc, profit_by_day).

**5. Componentes**
- **DataTable**: ordenação, cabeçalho fixo, linha clicável, ação principal + menu "⋯", seleção em massa, rodapé com totais, cartões no celular, filtros em chips gravados na URL
- **Drawer lateral** para criar/editar, com seções, máscaras (CNPJ, CEP, telefone), confirmação ao descartar e atualização só da linha salva
- **Combobox** com busca no servidor para produto, cliente e fornecedor
- **Estado vazio** com ilustração, explicação e botão da ação (ex.: "Importar produtos por CSV")
- **Toasts** únicos; erros persistentes com "Tentar novamente"; banner de offline

**6. Primeiros passos**
Checklist de 6 etapas no Início, com progresso: empresa → loja → produtos → entrada de estoque → operador com PIN → parear caixa.

**7. Visual**
- Tokens semânticos e tema claro/escuro (`data-theme` + `prefers-color-scheme`)
- Claro: fundo `#F5F7F4`, superfície `#FFFFFF`, texto `#18322F`, primária `#148765`
- Escuro: fundo `#0D1B21`, superfície `#14272F`, texto `#E6EFEC`, primária `#35C28F`
- Acento dourado `#E6AC43` (coroa CrownTech) para destaque e foco
- Fonte base 14px, números tabulares, ícones SVG de traço
- Contraste mínimo 4,5:1; foco movido ao título a cada troca de tela

**8. Marca**
"PDV2 by CrownTech" com logotipo no rodapé da sidebar, tela de carregamento, login, manifest e rodapé de relatórios e etiquetas impressos; remover "nome provisório" do login.

**Ordem sugerida**
1. Navegação nova + rota inicial + marca
2. Dashboard com gráficos
3. DataTable e drawer (aplicar em Produtos e Vendas primeiro)
4. Command palette e atalhos
5. Primeiros passos
6. Tema escuro e ajustes de acessibilidade

Correções pontuais imediatas: rótulos ausentes em Série/Próximo número NFC-e (`web/assets/app.js:2662-2663`), larguras de coluna de Produtos (`web/assets/ui.css:339-345`) e toast duplicado (`web/assets/app.js:84-92`).

### Problemas encontrados

- **[alto] Menu lateral com 23 itens planos e agrupamento incoerente** (`Sidebar — /Users/brunomagalhaes/Documents/Codex/2026-09-28/se-x20/outputs/pdv-saas-phase1/web/assets/app.js:33-57 e 3663-3676`): tenantNav tem 23 entradas renderizadas como lista única com rolagem. 'Financeiro' e 'Comissões' estão no grupo 'Relacionamento' (linhas 45-46); 'Categorias' e 'Promoções' estão soltas em 'Operação' em vez de dentro de Produtos; 'Inventário' (39) fica separado de 'Estoque' (42); 'Administração' tem 10 itens (47-56), incluindo telas raras como Impressoras e Conflitos offline. Não há recolher grupo, favoritos nem contadores de pendência. **Correção:** Reduzir para 6 áreas por tarefa (Início, Vendas e caixa, Produtos e estoque, Clientes e financeiro, Relatórios, Configurações) e mover subitens para abas dentro da página. Configurações vira uma página única com seções. Mostrar contador nos itens com pendência (conflitos offline, títulos vencidos, estoque baixo).
- **[alto] Dashboard sem nenhum gráfico e com poucos indicadores** (`Visão geral — web/assets/app.js:927-989; cloud/core/views_sales.py:103-140; web/assets/ui.css:258-261`): dashboardPage mostra 4 cartões (faturamento, vendas, ticket médio, estoque baixo), uma tabela de vendas recentes e duas linhas de financeiro. O endpoint /dashboard só devolve agregados, sem série diária, sem comparação com período anterior, sem mix de pagamento e sem top produtos. As classes .chart-bars/.chart-labels existem no CSS mas não são usadas em nenhum arquivo de web/ (grep sem ocorrências fora do ui.css). Filtro de período são dois inputs de data sem atalhos (Hoje, 7 dias, Mês). **Correção:** Dashboard com KPIs com variação percentual vs período anterior e sparkline, gráfico de vendas por dia, mapa de vendas por hora, mix de formas de pagamento, top 5 produtos e bloco 'Precisa de atenção'. Os dados já existem em /reports (sales_by_day, sales_by_hour, sales_by_payment, products_abc, profit_by_day em cloud/core/views_reports.py:252-263); falta um endpoint agregado ou chamadas paralelas. Chips de período pré-definidos.
- **[alto] Sem busca global, command palette ou atalhos de teclado** (`Topbar e app inteiro — web/app.html:42-48; web/assets/app.js:351-352 e 448`): A topbar tem só breadcrumb, data, selo 'Acesso antecipado' e avatar. Os únicos tratamentos de teclado no app.js são Escape (fecha diálogo e menu de perfil) e Tab (prende foco no diálogo). Busca existe apenas em Produtos (linha 812-820) e Estoque (2229-2231); Vendas, Clientes, Fornecedores, Financeiro e Auditoria não têm busca. **Correção:** Campo de busca na topbar que abre uma command palette com Ctrl+K (ou /): navegar para telas, buscar produto/cliente/venda por nome, código ou ID e executar ações ('Novo produto', 'Registrar entrada', 'Novo título'). Atalhos: N = novo registro da tela, G+letra = ir para área, ? = lista de atalhos.
- **[alto] Nenhum onboarding de primeiro uso** (`Primeiro acesso — web/assets/app.js:3701-3740, 804-806, 2473, 2534`): init() abre direto a primeira rota. Não há checklist, tour nem estado de 'conta nova' (grep por onboarding/primeiros passos/checklist sem resultados). As instruções do caminho real (cadastrar loja → produtos → entrada de estoque → equipe com PIN → criar terminal e parear) estão espalhadas em faixas de aviso de quatro telas diferentes. **Correção:** Cartão 'Primeiros passos' no topo do Início com 6 etapas marcáveis (dados da empresa, primeira loja, importar/cadastrar produtos, entrada de estoque, operador com PIN, parear caixa), cada uma com botão que abre a ação correta e barra de progresso. Some quando concluído.
- **[alto] Marca CrownTech ausente no painel** (`Sidebar, carregamento, login — web/app.html:11,18,22-25; web/login.html:33; web/manifest.webmanifest:3-5`): Busca por 'crowntech'/'crown' em web/app.html, app.js, ui.css, login.html, landing.html e manifest não retorna nada. O login ainda exibe 'PDV2 é nome provisório. Marca e domínio serão verificados antes do lançamento.' (login.html:33). **Correção:** Assinatura 'PDV2 by CrownTech' com logotipo no rodapé da sidebar, na tela de carregamento, no login, no manifest (name/description) e no rodapé de relatórios exportados/impressos. Remover o texto de nome provisório.
- **[alto] Tabela genérica sem ordenação, totais, seleção nem versão para celular** (`Todas as listagens — web/assets/app.js:191-224; web/assets/ui.css:215-226, 313, 363`): table() cria apenas thead/tbody: sem ordenação por coluna (nenhum aria-sort/handler), sem cabeçalho fixo, sem rodapé de totais, sem seleção em massa, linha não clicável. Ações são até 4 botões de texto por linha (Extrato, Editar, Imprimir etiquetas, Desativar — linhas 856-863). No celular a única adaptação é overflow-x:auto; a tabela de produtos força min-width:760px (ui.css:363). **Correção:** Componente DataTable único: ordenação por clique no cabeçalho, cabeçalho fixo, linha clicável que abre painel lateral de detalhe, ação principal visível + menu '⋯' para as demais, seleção em massa (desativar, imprimir etiquetas, exportar), rodapé com contagem e totais, e layout em cartões abaixo de 650px.
- **[médio] Tela inicial abre em Relatórios, não na Visão geral** (`Roteamento — web/assets/app.js:34-35 e 3658-3662`): activeSection() devolve navigation[0] quando não há hash, e o primeiro item de tenantNav é 'reports' (linha 34), antes de 'dashboard' (35). Quem tem o recurso 'reports' cai numa tabela de relatório ao entrar. **Correção:** Colocar o Início/dashboard como primeiro item e rota padrão explícita; opcionalmente lembrar a última tela por usuário.
- **[médio] Vendas sem filtro de data, estado ou busca, e paginação sem volta** (`Vendas — web/assets/app.js:1129-1188`): Filtros são só Loja, Operador e Vendedor (1135-1138). Não há período, estado (concluída/cancelada), forma de pagamento nem busca por número/cliente. 'Próxima página' chama load(next_cursor) e substitui as linhas (1177) sem botão para voltar. A venda é identificada por 8 caracteres do UUID (1166). Não há total do período. **Correção:** Barra de filtros com período (chips), estado, forma de pagamento e busca; paginação 'carregar mais' ou anterior/próxima; resumo no topo (quantidade, total, ticket médio do filtro); número sequencial amigável da venda; exportar CSV.
- **[médio] Tela de Produtos: estilos de coluna desalinhados, KPIs parciais e três avisos fixos** (`Produtos — web/assets/app.js:502-510, 803-807, 834-843; web/assets/ui.css:334-345`): A tabela tem 8 colunas (Produto, Códigos, NCM, Unidade, Financeiro, Margem, Status, Ações) mas o CSS por nth-child foi escrito para 7: a 4ª (agora 'Unidade') recebe 22% e a 5ª (agora o bloco 'Financeiro' de três linhas) recebe 8% com nowrap e fonte de destaque; além disso table-layout:fixed usa a primeira linha (th sem largura). Os 4 cartões ('Produtos encontrados', 'ativos'…) contam apenas rows da primeira página (835-843) e não são recalculados em 'Carregar mais'. Três faixas info() permanentes ficam acima da lista (804-806). **Correção:** Definir larguras com <colgroup> ou classes por coluna, não nth-child. KPIs vindos do servidor (totais reais). Trocar os avisos fixos por ajuda contextual (ícone '?' ou dica dispensável lembrada por usuário). Adicionar filtros por categoria, estado e 'sem custo/sem NCM'.
- **[médio] Formulário 'Minha empresa' com campos sem rótulo e sem seções** (`Minha empresa — web/assets/app.js:2662-2664`): Em 'Série NFC-e' e 'Próximo número NFC-e' o código cria seriesLabel/nextNumberLabel mas faz apenas seriesField.append(series) e nextNumberField.append(nextNumber): o rótulo nunca entra na tela, ficando dois campos numéricos sem identificação. O formulário coloca 17 campos + horário em uma grade única (2664), sem separar Identificação, Endereço, Fiscal e Regras do caixa, sem máscara de CNPJ/CEP nem preenchimento por CEP. **Correção:** Anexar os rótulos (append(label, input)). Dividir em seções com título ou abas (Identificação, Endereço, Fiscal, Regras do caixa, Horários), máscara de CNPJ/CEP/UF, busca de endereço por CEP e barra de salvar fixa que só aparece quando há alteração.
- **[médio] Diálogos perdem dados e salvar recarrega a tela inteira** (`Diálogos e formulários — web/assets/app.js:360, 779-781, 3677-3687`): Clique fora do diálogo chama closeDialog() sem perguntar (linha 360), descartando formulários longos (produto com 13 campos, importação de NF-e). Após salvar, resourceForm chama renderRoute() (781), que reconstrói a página: a busca digitada, filtros, registros de 'Carregar mais' e a posição de rolagem são perdidos. Filtros não ficam na URL. **Correção:** Detectar formulário alterado e pedir confirmação ao fechar; usar painel lateral (drawer) para criar/editar; após salvar, atualizar apenas a linha afetada; guardar filtros e busca no hash (#products?q=...).
- **[médio] Estoque: seis botões no cabeçalho e quatro tabelas empilhadas** (`Estoque — web/assets/app.js:2243-2251, 2268-2327; web/assets/ui.css:301-302`): O cabeçalho recebe até 6 botões (Ajustar, Registrar entrada, Importar CSV, Importar XML, Transferir, Transferências). No celular .page-actions .button{flex:1} os empilha antes do conteúdo. A página carrega 3 endpoints e mostra Saldos, Movimentações, Entradas e Histórico de custos em sequência, sem abas; não há filtro 'só abaixo do mínimo'. Inventário e transferências ficam em outra tela/diálogo. **Correção:** Uma ação primária ('Entrada de mercadoria' com menu: manual, CSV, XML da NF-e) + menu 'Mais'. Abas: Saldos | Movimentações | Entradas | Transferências | Inventário. Chips de filtro (abaixo do mínimo, zerado, sem custo) e valor total do estoque no topo.
- **[médio] Feedback de erro duplicado e com mensagem inadequada** (`Toasts e erros — web/assets/app.js:73-93; web/app.html:52`): handleError dispara, para status 0 ou >=500, um toast 'Sem conexão com a nuvem… abra o terminal local em http://127.0.0.1:8010/' e, sem target, um segundo toast com errorText (linhas 84-92). A mensagem aparece também para erro 500 e para quem está no celular. Todo toast some em 5,2 s (77), inclusive erros, sem botão de fechar nem ação 'Tentar novamente'. **Correção:** Um único toast por erro; distinguir offline (banner fixo no topo com reconexão automática) de falha do servidor; erros permanecem até fechar e trazem 'Tentar novamente'; toasts de sucesso com 'Desfazer' onde couber (desativar).
- **[médio] Excesso de avisos permanentes e selo 'Acesso antecipado' fixo** (`Várias telas — web/app.html:47; web/assets/app.js:930, 1001, 1132, 804-806, 2620, 3104`): Há 55 chamadas a info() no app.js. Dashboard, Vendas, Produtos, Minha empresa, Integrações e o detalhe da venda abrem com faixa amarela dizendo que algo 'ainda passa por validação' ou 'não está homologado'. O selo 'Acesso antecipado' é fixo no HTML. Passa imagem de produto inacabado e empurra o conteúdo para baixo. **Correção:** Um único indicador de estado fiscal na topbar (ex.: 'Fiscal: não configurado' com link para configurar). Avisos contextuais dispensáveis, exibidos só quando a condição se aplica (dados da API), e memorizados por usuário.
- **[médio] Sem tema escuro e cores escritas à mão fora dos tokens** (`Estilos — web/assets/ui.css:1-18 e todo o arquivo; web/assets/app.js (24 usos de .style.)`): Não há prefers-color-scheme nem data-theme (grep sem resultados). O :root define 12 variáveis, mas o restante do CSS usa dezenas de hex literais (#cbdad1, #33544b, #70857a, #fafcf9, #edf1ed…), e o JS aplica estilos inline (marginTop em 1014, 1020, 2237, 2297, 2326). Isso impede tema e gera inconsistência (dois blocos .checkbox-line: linhas 77 e 379). **Correção:** Tokens semânticos (--bg, --surface, --text, --text-muted, --border, --primary, --danger, --warn) usados em todo o CSS; tema claro/escuro por data-theme com alternador no menu do perfil e respeito a prefers-color-scheme; remover estilos inline em favor de classes utilitárias.
- **[médio] Acessibilidade: texto pequeno, contraste baixo, ícones lidos e foco não acompanha a rota** (`Geral — web/assets/ui.css:175, 187, 217-218; web/assets/app.js:196, 3670, 3677-3687`): Corpo de tabela em 12px, cabeçalho em 10px, breadcrumb e metadados em 10-11px. Breadcrumb #83948a sobre branco tem contraste aproximado de 3,2:1 (abaixo de 4,5:1). O link do menu é texto 'ícone + rótulo' (item.icon+"  "+item.label, linha 3670), então o leitor de tela lê o glifo; a classe .nav-glyph existe mas não é usada. th sem scope (196). renderRoute troca o conteúdo sem mover o foco para o main/h1 nem anunciar a nova página. Botão desabilitado usa cursor: wait (ui.css:41). **Correção:** Base de 14px (mínimo 12px para metadados), cores de texto secundário com contraste >= 4,5:1, ícones SVG em <span aria-hidden>, th scope=col, focar o h1 (tabindex=-1) a cada troca de rota, e aria-busy nas áreas em carregamento.
- **[médio] Financeiro sem filtros, totais ou destaque de vencidos** (`Financeiro — web/assets/app.js:2385-2406`): A tela é uma tabela única com Tipo, Descrição, Valor, Vencimento e Estado. Não há filtro por tipo, estado ou período, nem total a pagar/receber, nem destaque para vencidos (o selo 'Vencido' só existe no extrato do cliente, linha 1228). O subtítulo informa que a liquidação é só pelo valor integral (2389). Pagar e receber ficam misturados. **Correção:** Abas A receber | A pagar | Fluxo de caixa; cartões no topo (vencido, vence hoje, próximos 7 dias); filtros por período/estado/cliente/fornecedor; linha vencida em destaque; baixa parcial e ação em massa 'Liquidar selecionados'.
- **[baixo] Relatórios só em tabela, sem gráfico, atalhos de período ou impressão** (`Relatórios — web/assets/app.js:1419-1483`): reportsPage monta um select de tipo, duas datas, loja e 'Exportar CSV'; o resultado é sempre table(). 'Vendas por dia', 'por hora' e 'por forma de pagamento' são séries que pedem gráfico. Não há comparação com período anterior nem saída em PDF/impressão. **Correção:** Galeria de relatórios em cartões por tema (Vendas, Lucro, Estoque, Caixa); cada relatório com gráfico no topo + tabela, chips de período, comparação e botões Exportar CSV / Imprimir.
- **[baixo] Campos de seleção carregam só a primeira página de registros** (`Formulários — web/assets/app.js:668-672, 1070, 2363-2364`): fieldNode type 'source' faz um único api.get(field.source) e preenche um <select>; em empresas com muitos clientes/fornecedores/categorias, os que estão além da primeira página não aparecem (ex.: Cliente e Fornecedor no novo título, linhas 2363-2364; devolução usa /customers?limit=100 na linha 1070). O seletor de produto usa um campo de busca separado acima de um <select> (1975-2012). **Correção:** Componente único de autocomplete (combobox) com busca no servidor para produto, cliente, fornecedor e categoria, com opção 'Cadastrar novo' na própria lista.
- **[baixo] Textos técnicos expostos ao usuário** (`Assinatura, Auditoria, Integrações — web/assets/app.js:2609-2613, 2698-2702, 3135-3136`): 'Recursos do plano' lista as chaves cruas de plan.features (ex.: cloud_dashboard, stock). Auditoria mostra row.action, subject_type e subject_id sem tradução nem filtro. Tarefas de integração mostram row.kind e last_error crus. **Correção:** Dicionário de rótulos para recursos, ações de auditoria e tipos de tarefa; auditoria com filtro por pessoa, período e tipo, e frase legível ('Maria alterou o preço de Arroz 5kg').
- **[baixo] Ícones repetidos e breadcrumb que não corresponde ao menu** (`Sidebar e cabeçalho — web/assets/app.js:41, 51, 59, 67, 790, 2387; web/app.html:45`): O glifo '◈' é usado em Categorias (41) e Formas de pagamento (51); '◫' em Visão geral e Backups (59, 67). Os ícones são caracteres Unicode geométricos sem relação com o conteúdo. resourcePage usa a seção 'Gestão' (790), que não existe no menu; Financeiro aparece como 'Relacionamento' (2387). O breadcrumb não é clicável. **Correção:** Conjunto de ícones SVG de traço (ex.: Lucide) com um ícone distinto por área; breadcrumb gerado a partir da mesma definição do menu e com links.

## Admin do SaaS

### Proposta

## Console CrownTech — redesenho do admin SaaS

**Identidade**
- Tema próprio, diferente do painel do cliente: barra lateral grafite `#12181F`, faixa dourada `#C9A227` no topo com selo **ADMIN SAAS**, verde `#1E7358` só para estados positivos. Rodapé e carregamento: "PDV2 por CrownTech". Selo de ambiente vindo do servidor (Produção/Testes).

**Navegação (4 grupos, ícones SVG únicos)**
- **Clientes:** Início · Empresas · Terminais · Chamados
- **Receita:** Assinaturas · Cobranças · Planos e recursos · Cupons
- **Operação:** Serviços e filas · Versões · Backups · Comunicados
- **Segurança:** Equipe e permissões · Auditoria · MFA

**Atalhos (visíveis ao lado de cada item)**
- `Ctrl+K` busca global (empresa, CNPJ, e-mail, terminal, fatura)
- `G E` Empresas, `G A` Assinaturas, `G C` Cobranças, `G S` Serviços
- `/` foca a busca da lista, `N` novo registro, `Esc` fecha painel, `?` mostra todos

**Início**
1. Quatro KPIs grandes com variação e minigráfico: MRR, clientes pagantes, churn do mês, conversão de trial.
2. Gráfico de MRR 12 meses (novo, expansão, cancelado).
3. **Precisa de você hoje**: trials vencendo em 3 dias, cobranças vencidas (aging 1-7 / 8-30 / 30+), tarefas em revisão, terminais offline há 24 h. Cada linha abre a lista já filtrada.
4. Últimos cadastros (no lugar da lista alfabética).

**Componente de lista único**
- Busca, chips de filtro, ordenação por coluna, "x de y", exportar CSV, filtros gravados na URL. Linha clicável abre painel lateral (sem perder a lista).

**Ficha do cliente (painel lateral com abas)**
- Cabeçalho: nome, CNPJ, plano, selo de status, **saúde 0-100** (última venda, sincronização, pagamentos, módulos usados).
- Botões: **Entrar como cliente** (motivo obrigatório, 30 min, somente leitura, faixa vermelha no painel dele, auditado nos dois lados), Prorrogar acesso, Trocar plano, Aplicar cupom, Suspender.
- Abas: Resumo · Assinatura e cobranças · Usuários · Lojas e terminais · Recursos (liga/desliga por cliente e limites extras com validade) · Notas internas · Chamados · Histórico.

**Planos e recursos**
- Matriz planos x recursos com chaves, vinda de um catálogo fixo do backend (sem JSON digitado).
- Limites: terminais, lojas, usuários, produtos, NFC-e/mês. Dias de teste, plano padrão do cadastro, público/oculto, ordem.
- Antes de salvar: "afeta N empresas".

**Régua de cobrança (configurável)**
- Trial: avisos D-3, D-1, D0 → expirado.
- Atraso: D+1 aviso → D+3 somente leitura → D+10 terminais bloqueados → D+30 cancelado.
- Linha do tempo visual na ficha mostrando em que etapa o cliente está; e-mail e banner por etapa.
- Toda mudança (plano, cancelamento, pausa) é enviada ao provedor por job, nunca só local.

**Cobranças**
- Totais no topo (a receber, vencido, recebido no mês); ações: reenviar e-mail, copiar link, consultar provedor. Formas: Pix, boleto e cartão (Asaas) + Stripe.

**Equipe e permissões**
- Aba Equipe (só staff): MFA, último acesso, redefinir MFA, encerrar sessões, convite por e-mail.
- Matriz papel x ação visível; novo papel "Somente leitura".

**Operação**
- Serviços: semáforo por serviço (cobrança, fiscal, worker, backup), fila "em revisão" primeiro, com nome da empresa e tipo em português.
- Versões: canal Estável/Piloto por empresa, barra de adoção por versão, terminais atrasados.
- Comunicados: banner segmentado por plano/status + e-mail em massa com prévia.

**Auditoria**
- Empresa por nome, ação em português, linha expansível com antes/depois e motivo; filtros por pessoa, empresa, tipo e período.

**Ordem sugerida de entrega**
1. Corrigir os 5 defeitos graves (assinatura, Asaas, Serviços, PATCH de usuários).
2. Lista única + ficha do cliente + impersonação.
3. Métricas históricas e régua de cobrança com e-mail.
4. Catálogo de recursos, cupons e overrides.
5. Chamados, comunicados, cartão/Stripe.

### Problemas encontrados

- **[alto] Editar assinatura sempre rebaixa o cliente para "Ativação manual" e o tira do MRR** (`cloud/core/views_platform.py:340-341 + web/assets/app.js:2882-2898`): O formulário envia sempre plan_id e status (campos required, app.js:2882-2883). No backend, `if {"plan","status","current_period_end"}.intersection(data): row.license_source = "trial" if row.status == "trial" else "manual"`. Qualquer edição (até só prorrogar a data) converte 'Assinatura paga' ou VIP em 'manual'. O MRR filtra `license_source='subscription'` (operations/views.py:310-311), então o cliente pagante some da receita até o próximo pagamento conciliado. **Correção:** Enviar apenas os campos alterados (diff no front) e, no backend, só trocar license_source quando plano/status realmente mudarem de valor. Criar ação separada 'Prorrogar acesso por N dias' que não altera a origem da licença e exige motivo.
- **[alto] Troca de plano, suspensão, cancelamento e desativação não chegam ao Asaas e são revertidos no próximo pagamento** (`integrations/billing.py:36-55; operations/services.py:91-98; cloud/core/views_platform.py:296-344 e 203-209`): A classe Asaas só tem create_customer/create_subscription/get_* (não existe cancelar nem alterar valor). subscription_detail e tenant_detail não tocam em BillingBinding. Em reconcile_payment, `paid_takeover = sub.license_source != 'subscription'` e, com pagamento pago, `sub.plan=binding.plan; sub.status='active'`. Resultado: o admin cancela ou faz upgrade, o cliente continua sendo cobrado no valor antigo e o próximo boleto pago reativa a assinatura no plano antigo. **Correção:** Adicionar update_subscription/cancel_subscription no cliente Asaas; jobs billing.change_plan e billing.cancel enfileirados pelo admin; atualizar BillingBinding (plano, valor, active=False) na mesma transação; bloquear reativação automática quando status foi cancelado/suspenso manualmente.
- **[alto] Não existe ciclo de vida automático (trial vencido, inadimplência, suspensão gradual, avisos)** (`operations/worker.py:20-35; operations/services.py:99-102; cloud/core/api_common.py:153-165; cloud/core/views_auth.py:152-153; cloud/pdvcloud/settings.py:123`): O worker só executa run_one() e limpa sessões; não há tarefa agendada. past_due só é gravado quando chega webhook OVERDUE/REFUNDED. Trial é fixo em `timedelta(days=14)` e a tolerância é fixa em `timedelta(hours=72)`. Trial vencido fica com status 'trial' para sempre e não aparece em nenhuma métrica (operations/views.py:315 conta só trial_ends_at futuro). Nenhum send_mail no código (só EMAIL_BACKEND configurado). **Correção:** Job diário 'billing.lifecycle': trial D-3/D-1/D0 (e-mail + banner), trial vencido -> status expired; atraso D+1 aviso, D+3 somente leitura, D+10 terminais bloqueados, D+30 cancelado. Régua configurável em tela (dias e textos), com prazo de trial e tolerância por plano.
- **[alto] Sem impersonação auditada; no lugar, o superusuário pode trocar e-mail e senha de qualquer usuário de cliente** (`cloud/core/views_platform.py:110-152`): platform_user_detail busca `User.objects.filter(pk=pk, is_superuser=False)` — qualquer usuário do sistema, inclusive caixa de uma empresa — e aceita PATCH de name/email/password/active. A auditoria grava `audit(None, actor, "platform.staff_updated", row, {"fields": list(values)})`: sem tenant (o cliente não vê no próprio histórico) e sem valores. Busca por 'impersonat|login_as|act_as' no repositório não retorna nada. **Correção:** Restringir o PATCH a contas is_staff (equipe). Criar POST /platform/tenants/<id>/support-session: exige motivo, expira em 30 min, somente leitura por padrão, faixa vermelha fixa no painel do cliente ('Você está vendo como Loja X'), evento de auditoria no tenant e na plataforma e aviso por e-mail ao proprietário.
- **[alto] Página Serviços quebra inteira para o papel Atendimento** (`web/assets/app.js:66 e 3147-3151; operations/views.py:138`): O menu libera 'services' para platform_support, mas a página faz Promise.all com `api.get("/ops/jobs",{tenant:false})`, e o backend exige `platform_user(request,roles=('platform_ops','platform_billing'))`. O 403 derruba o Promise.all e o atendente vê 'Falha ao carregar' em vez da saúde do serviço. **Correção:** Carregar a lista de tarefas só quando platformCan('platform_ops','platform_billing') (ou usar Promise.allSettled e renderizar cada bloco de forma independente). Alternativa: liberar leitura de /ops/jobs para suporte, sem o campo result.
- **[alto] Indicadores de dono de SaaS ausentes: só MRR instantâneo, sem churn, conversão, inadimplência em R$ nem histórico** (`operations/views.py:303-319; web/assets/app.js:2766-2777`): platform_metrics devolve 12 contagens instantâneas. 'Assinaturas ativas' usa `active.count()` que inclui VIP e manual (views.py:305,314) apesar do rótulo. `paid_invoices_cents` soma todas as faturas pagas da história, sem período (views.py:312). Não há ARR, MRR novo/expansão/churn, taxa trial->pago, ticket médio, valor em atraso, clientes vencidos (status active com current_period_end passado não entra em nenhum contador). **Correção:** Tabela MetricSnapshot diária (MRR, pagantes, trials, cancelados, vencidos) gravada pelo worker; endpoint /platform/metrics?range=30d|90d|12m com MRR por movimento, churn de clientes e de receita, conversão de trial, aging de inadimplência (1-7, 8-30, 30+ dias) e receita recebida no mês.
- **[médio] Empresas: sem busca, filtros, ficha do cliente nem cadastro assistido** (`web/assets/app.js:2787-2806; cloud/core/views_platform.py:155-161, 174-183`): A tela chama platformApi("/tenants") sem campo de busca (o backend aceita `q`, mas só por `name__icontains`, não por CNPJ/e-mail). `_tenant` faz uma consulta de Subscription por linha (N+1). `tenants` aceita só GET: não dá para criar empresa pelo admin. Não há detalhe do cliente; ações só para superusuário. **Correção:** Busca por nome, CNPJ e e-mail do proprietário; filtros por status, plano, origem da licença; select_related('subscription__plan'); POST para cadastro assistido (venda consultiva); linha clicável abrindo a ficha 360 do cliente.
- **[médio] Planos: recursos em JSON cru, sem catálogo, parte deles nem é aplicada; limites pobres; cadastro preso ao slug 'essential'** (`web/assets/app.js:2838; cloud/core/views_platform.py:246-248; cloud/core/migrations/0002_seed_plans.py:7-12; cloud/core/models.py:19-28; cloud/core/views_auth.py:143-145`): Campo 'Recursos (JSON)' digitado à mão; backend aceita qualquer texto até 80 caracteres (um erro de digitação desliga o módulo sem aviso). Os planos semeados têm 'pdv_local', 'stock' e 'priority_support', mas require_feature só é chamado com cloud_dashboard, finance e reports ('stock' é filtrado apenas no menu, app.js:39,42). Plan só tem terminal_limit e shop_limit. O cadastro público usa `Plan.objects.filter(slug="essential", active=True)`: se o admin desativar o Essencial, o cadastro devolve 503. **Correção:** Catálogo fixo de recursos no backend (código, nome, descrição) exposto em /platform/features e validado; editor com chaves liga/desliga agrupadas por módulo e matriz comparativa de planos; novos limites (usuários, produtos, NFC-e/mês, promoções), dias de teste, plano padrão do cadastro configurável, plano público/oculto e ordem de exibição.
- **[médio] Sem cupons, descontos, notas internas, etiquetas, feature flags e limites por cliente** (`cloud/core/models.py:34-83; operations/models.py:36-45`): Tenant e Subscription não têm campos de observação, etiqueta, responsável, desconto ou exceção de recurso/limite. BillingBinding guarda só amount_cents fixo. A única exceção comercial possível é o VIP 100% gratuito (views_platform.py:347-417). **Correção:** Modelos Coupon (percentual/valor, duração em meses, validade, limite de usos), TenantNote (texto, autor, fixada), TenantOverride (features_add/remove, terminal_limit, shop_limit, validade). require_feature e capacity_fits passam a consultar o override antes do plano.
- **[médio] Cobranças: lista somente leitura, sem filtros, totais ou ações; status aparecem em inglês; sem cartão** (`cloud/core/views_platform.py:472-481; web/assets/app.js:3335-3355, 19, 186-188; operations/views.py:150; integrations/billing.py:42`): invoices não aceita filtro de status, empresa ou período e a tabela não tem ações (passa `null`). statusNames não tem 'pending', 'overdue', 'disputed' nem 'done' (status de sucesso do worker, services.py:223), então o selo mostra o texto cru; 'overdue' cai no tom neutro em vez de alerta. Formas aceitas: apenas PIX/BOLETO/UNDEFINED; não há nenhuma referência a Stripe no repositório. **Correção:** Filtros por status/período/empresa, totais no topo (a receber, vencido, recebido no mês), ações 'Reenviar por e-mail', 'Copiar link', 'Consultar no provedor', 'Marcar recebido em dinheiro'. Traduzir todos os estados. Incluir CREDIT_CARD no Asaas e provedor Stripe com a mesma interface de BillingBinding.
- **[médio] Auditoria ilegível e sem filtros** (`cloud/core/views_platform.py:458-469; web/assets/app.js:3364-3367`): A coluna 'Empresa' renderiza `row.tenant_id` (UUID cru); 'Ação' mostra o código ('platform.vip_granted'); o serializer não devolve `detail`, então o motivo do VIP ou da revogação é gravado e nunca pode ser lido. Só existe filtro tenant_id no backend e a UI não usa nenhum. **Correção:** Devolver tenant_name e detail (select_related('tenant')); dicionário de ações em português; filtros por empresa, pessoa, tipo de ação e período; linha expansível com o antes/depois; exportação CSV.
- **[médio] Terminais: não dá para filtrar por empresa, reativar, ver quem está offline ou desatualizado** (`web/assets/app.js:2955-2970; cloud/core/views_platform.py:425, 447-450`): A UI só oferece 'Revogar' (`row.revoked?[]:[...]`), embora o backend aceite revoked=false com checagem de limite. O filtro tenant_id existe na API e não é usado. 'Último contato' e 'Versão' são texto puro, sem indicar offline ou versão antiga. **Correção:** Filtros por empresa, estado e versão; selo Online/Offline há X h/Sem contato; destaque quando version < versão estável publicada; ação 'Reativar' com motivo; contador de fila com alerta acima de um limite.
- **[médio] Usuários e permissões mistura a equipe com todos os proprietários de clientes; sem controle de MFA da equipe; papéis fixos** (`cloud/core/views_platform.py:83-85; web/assets/app.js:3428-3445; cloud/pdvcloud/urls.py:31-36; cloud/core/api_common.py:175`): A consulta lista `is_staff=True` OU qualquer membership owner ativo: com 500 clientes, a tela de equipe terá 500 linhas de clientes. Não há coluna de MFA nem rota para o superusuário redefinir o MFA de um colega (só rotas de autoatendimento). Os papéis são 3 grupos fixos (PLATFORM_GROUPS) com permissões embutidas no código. **Correção:** Duas abas: 'Equipe' (só is_staff, com MFA ativo/pendente, último acesso, 'Redefinir MFA', 'Encerrar sessões') e 'Promover proprietário' com busca. Matriz de permissões visível (papel x ação) e papel adicional somente leitura; convite por e-mail em vez de senha definida pelo admin.
- **[médio] Versões: canal Piloto não tem efeito, sem visão de adoção nem liberação por cliente** (`web/assets/app.js:3274; terminal/sync.py:117, 128; operations/views_releases.py:14-24`): O formulário oferece canal 'Piloto', mas o terminal consulta fixo `channel=stable` e rejeita manifesto de outro canal. Não existe campo de canal em Tenant/Terminal. A tela não cruza Terminal.version com as versões publicadas. **Correção:** Campo release_channel no Tenant (ou Terminal) definido pelo admin; terminal envia o canal recebido na licença; painel de adoção (barra por versão, lista de terminais atrasados); liberação gradual por lista de empresas.
- **[médio] Visão geral sem hierarquia nem fila de ação** (`web/assets/app.js:2766-2783; web/assets/ui.css:205-210`): Nove cartões idênticos em grade 4 colunas misturam receita, contagens técnicas e fila de integração; nenhum é clicável. A tabela abaixo é 'Primeiros registros em ordem alfabética' com limit=8, sem utilidade de gestão. Não há gráfico nem tendência. **Correção:** Faixa de 4 KPIs grandes com variação (MRR, pagantes, churn, conversão de trial), gráfico de MRR 12 meses, e bloco 'Precisa de você hoje' (trials vencendo, boletos vencidos, tarefas em revisão, terminais offline), cada item clicável levando à lista já filtrada.
- **[médio] Navegação e identidade: 12 itens num grupo só, ícones repetidos, painel SaaS idêntico ao do cliente, sem marca CrownTech** (`web/assets/app.js:58-70; web/app.html:11, 21-24, 48`): Todos os itens têm group 'Plataforma'; o ícone '◫' é usado em Visão geral (59) e Backups (67). app.html é o mesmo para /app e /platform (mesma barra lateral verde, título 'Painel · PDV2', selo fixo 'Acesso antecipado'). A busca por 'crowntech' em web/ e terminal/templates retorna 0 ocorrências. **Correção:** Agrupar em Clientes / Receita / Operação / Segurança; ícones SVG únicos; tema próprio da plataforma (barra lateral escura com faixa dourada e selo 'ADMIN SAAS'); rodapé e tela de carregamento 'PDV2 por CrownTech'; selo de ambiente vindo do servidor (Produção/Testes).
- **[médio] Listas sem total, busca, ordenação ou exportação** (`cloud/core/api_common.py:199-209; web/assets/app.js:225-248`): page() devolve só results e next_cursor (offset); pagedContent mostra 'Mais registros disponíveis' e um botão 'Carregar mais'. Nenhuma tela da plataforma tem campo de busca, cabeçalho ordenável ou exportação. **Correção:** Incluir total_count na resposta; componente de lista único com busca, chips de filtro, ordenação por coluna, 'x de y' e exportar CSV; preservar filtros na URL (#tenants?status=past_due).
- **[médio] Tarefas de integração: 100 últimas, sem empresa, tipo técnico cru** (`operations/views.py:139-140; web/assets/app.js:3167-3171`): jobs devolve `rows.order_by('-created_at')[:100]` sem tenant_name; a tabela mostra 'billing.subscribe', 'fiscal.poll' como texto e não permite filtrar as que pedem revisão. **Correção:** Incluir tenant_name e paginação; nomes em português ('Contratação', 'Consulta fiscal'); aba padrão 'Precisam de revisão' com contador; link para a ficha do cliente.
- **[médio] Sem chamados, comunicação em massa, avisos no painel e saúde do cliente** (`cloud/pdvcloud/urls.py:94-106; operations/urls.py:3-17`): As rotas de plataforma são só tenants, plans, subscriptions, terminals, audit, invoices e users; em ops há integração, jobs, backups, releases e métricas. Não existe nenhuma rota ou modelo de ticket, anúncio, mensagem ou pontuação de uso. **Correção:** Modelos Ticket (aberto pelo painel do cliente, com SLA por plano — dá sentido ao recurso 'priority_support'), Announcement (banner segmentado por plano/status + e-mail em massa) e cálculo diário de saúde (última venda, sincronização, pagamentos, uso de módulos).
- **[baixo] Desativar empresa sem confirmação nem motivo** (`web/assets/app.js:2816-2828; cloud/core/views_platform.py:203-209`): Um checkbox 'Empresa ativa' no mesmo formulário de nome e documento; desmarcar bloqueia todos os membros e terminais. O backend não exige motivo e a auditoria registra apenas `{"fields": [...]}`. **Correção:** Ação separada 'Suspender empresa' em vermelho, com confirmação digitando o nome, motivo obrigatório gravado em detail e opção de pausar a cobrança junto.
- **[baixo] Datas de vigência inconsistentes e impossibilidade de limpar uma data** (`web/assets/app.js:2894-2895, 2926, 742`): A edição de assinatura grava fim de período às 12:00 UTC (09:00 de Brasília do último dia), enquanto o VIP grava 23:59:59 UTC. formPayload descarta campos vazios não obrigatórios, então não há como remover 'Fim do teste' pela tela. **Correção:** Padronizar fim de vigência em 23:59:59 de America/Sao_Paulo no backend (receber só a data); botão 'Limpar' que envia null.
- **[baixo] Estados crus em inglês na tela de Backups** (`web/assets/app.js:3211, 3214`): `metric("Estado do backup",result.state||"—")` e `String(restore.status)` exibem 'healthy', 'stale', 'unknown' direto, embora statusNames (app.js:19) tenha as traduções. **Correção:** Usar statusNames[...] e cor do cartão conforme o estado (verde, âmbar, vermelho).

## Marca CrownTech e landing

### Proposta

## Sistema de assinatura "PDV2 por CrownTech"

**Princípio:** endosso discreto e constante — PDV2 é o protagonista, CrownTech é o selo de garantia. Nunca na área de venda do caixa; sempre nas bordas, nas telas ociosas e no que sai da loja.

### 1. Ativos (uma fonte só)
- `logo-pdv2.svg` vira a origem de tudo: símbolo "P2" + paleta única navy `#102630` / verde `#148765` / menta `#d9f4e6`.
- Novo lockup `pdv2-por-crowntech.svg` (horizontal e empilhado): "PDV2" em peso 850 + "por CrownTech" em 60% do tamanho, cor `--muted`.
- Regenerar favicon, icon-192/512 (any + maskable separados), favicon do terminal e AppIcon iOS a partir do mesmo símbolo.
- Constante `VENDOR` (nome, site, WhatsApp, CNPJ) em `shared/` e `web/assets/core.js`; todos os pontos leem dela. Tudo self-hosted (CSP `img-src 'self'`).

### 2. Onde entra (do mais visto ao menos)
| Superfície | Inserção |
|---|---|
| Cupom (58/80 mm) | Última linha, sem negrito: `PDV2 - crowntech.com.br`. Ocultável em plano white-label |
| Terminal bloqueado / PIN / pareamento | Rodapé fixo: "PDV2 · um produto CrownTech · Suporte WhatsApp" |
| Terminal > Sistema | Card "Sobre": versão, CrownTech, suporte |
| Landing | Faixa "Quem faz" + rodapé 3 colunas (Produto, CrownTech com CNPJ/contato, Legal) |
| Login | Substituir "nome provisório" por "PDV2 é um produto CrownTech © 2026" |
| Painel | Pé da sidebar: "PDV2 v2.x · CrownTech · Ajuda" |
| Link compartilhado | `og:image` 1200x630 com lockup; `og:site_name` "PDV2 · CrownTech" |
| E-mail / boleto Asaas / MFA | Remetente e descrição "PDV2 por CrownTech" |
| iOS | Rodapé "por CrownTech", tela Sobre, bundle `br.com.crowntech.pdv2.monitor` |
| offline.html e /status/ | Lockup + telefone de suporte |

### 3. Landing como peça de venda (ordem das seções)
1. **Hero:** H1 "Sistema de caixa (PDV) que vende mesmo sem internet". Sub com "a partir de R$ 149/mês". CTA primário "Testar grátis por 14 dias" (sem cartão), secundário "Falar no WhatsApp". Captura real do caixa com dados demonstrativos.
2. **Faixa de confiança:** "Desenvolvido e suportado pela CrownTech, em Maricá" + 3 selos (offline, suporte local, sem fidelidade). Depois dos pilotos: depoimentos com nome da loja.
3. **Segmentos** (manter) → **Recursos** (manter, 6 cards) → **App do gestor**.
4. **Planos:** preço firme, "Anual: 2 meses grátis", CTA carrega `?plan=`. Aba equipamentos com "Pedir proposta" (WhatsApp/lead).
5. **Quem faz o PDV2:** logo CrownTech, 2 linhas, foto da equipe/loja piloto, contato.
6. **FAQ** reduzido a 8 perguntas visíveis + "ver todas"; limitações atuais vão para cá e para /roadmap.
7. **Fechamento + rodapé completo.**
- Mobile: barra inferior fixa com os dois CTAs.

### 4. SEO e técnica
- Title "PDV2 — Sistema PDV e frente de caixa offline | CrownTech"; canonical; `meta author`.
- JSON-LD `SoftwareApplication` (publisher CrownTech, offers) e `FAQPage` em arquivo externo; `/robots.txt`, `/sitemap.xml`; `noindex` em /app, /platform, /login.
- Manter o peso atual (~29 KB gzip): capturas em WebP, lazy, com dimensões fixas.
- Publicar `/termos/` e `/privacidade/` com aceite no cadastro.

### 5. Limpeza
- Um único rótulo de CTA; remover "referência"/"acesso antecipado" repetidos e o erro técnico em `landing.js:93`.
- Arquivar `index.html`/`app.py` legados da raiz.
- Antes de publicar: registrar domínio e consultar a marca no INPI (docs/STATUS.md:38 indica que ainda não foi feito).

### Problemas encontrados

- **[alto] A marca CrownTech não aparece em nenhum lugar do produto (zero ocorrências no repositório)** (`repositório inteiro; web/landing.html:228; web/login.html:33; terminal/templates/index.html:15; terminal/printers.py:250-251`): `grep -rIi crown` em todo o repositório (fora .git) retorna 0 linhas. O rodapé da landing é só "PDV2 — Gestão para o varejo local. Acesso antecipado." (landing.html:228); o cabeçalho do terminal é "PDV2 / TERMINAL LOCAL" (index.html:15); o cupom termina em "OBRIGADO PELA PREFERENCIA! / Volte sempre" (printers.py:250-251). Não há razão social, CNPJ, site ou contato do fabricante em nenhuma superfície. **Correção:** Criar uma única fonte de verdade da assinatura (ex.: constante VENDOR = {name:'CrownTech', url, tagline:'PDV2 por CrownTech'} em shared/ e em web/assets/core.js) e aplicá-la em: rodapé da landing, lateral do login, rodapé da sidebar do painel, tela de PIN/pareamento do terminal, última linha do cupom, manifest, iOS, e-mails e descrição da cobrança. Detalhes de cada ponto nos itens abaixo.
- **[alto] Tela de login declara publicamente que a marca é provisória** (`web/login.html:33`): `<small class="auth-aside-foot">PDV2 é nome provisório. Marca e domínio serão verificados antes do lançamento.</small>` — texto visível a todo visitante que clica em Entrar/Criar conta. Soma-se a login.html:27 ("O caixa local segue em validação técnica") e :88 ("A operação fiscal e os periféricos ainda exigem validação antes de uso comercial"). **Correção:** Trocar a linha 33 por "PDV2 é um produto CrownTech · © 2026" com link para o site da CrownTech. Mover os avisos de validação técnica para a documentação/onboarding interno; no login manter só um selo discreto "Acesso antecipado".
- **[alto] Cupom impresso e recibo em tela não carregam assinatura do sistema** (`terminal/printers.py:249-251 e 313; terminal/static/terminal.js:109`): receipt_layout termina com `text("OBRIGADO PELA PREFERENCIA!", "center", bold=True)` e `text("Volte sempre", "center")`; voucher_layout termina em `text("")` (linha 313); o recibo HTML termina em `<footer>Obrigado pela preferência!</footer>`. O cupom é o material que mais circula fora da loja (cada cliente leva um) e hoje não divulga nada. **Correção:** Adicionar uma linha final pequena, sem negrito, centralizada, após "Volte sempre": `PDV2 por CrownTech - crowntech.com.br` (cabe em 32 colunas no papel de 58 mm: abreviar para "PDV2 - crowntech.com.br"). Repetir em voucher_layout e no <footer> do terminal.js com fonte menor. Opcional: flag de plano "white-label" para ocultar em planos superiores (vira argumento de upsell).
- **[alto] Landing não tem seção "quem faz", contato, CNPJ, política de privacidade nem termos** (`web/landing.html:226-228; web/login.html:78-85`): Contagem de `mailto:|tel:|wa.me|whatsapp|<form|<img` em landing.html = 0. O rodapé (linha 228) tem só logo, uma frase e "Acessar painel". O formulário de cadastro (login.html:78-85) coleta nome, empresa e e-mail sem link para termos/privacidade; não existe nenhum arquivo de política em web/ (grep por privacidade/termos/LGPD só acha docs internos). **Correção:** Criar antes do fechamento uma faixa "Quem faz o PDV2" (logo CrownTech, 2 linhas sobre a empresa em Maricá, botão WhatsApp e e-mail de suporte) e um rodapé de 3 colunas: Produto | Empresa (CrownTech, CNPJ, endereço, contato) | Legal (Termos, Privacidade/LGPD, Status). Publicar /termos/ e /privacidade/ e exigir aceite no cadastro.
- **[alto] Pacote com equipamentos é "mediante proposta", mas não existe como pedir a proposta** (`web/landing.html:186-187`): `<a class="button" href="/login/?mode=register">Conhecer o sistema ↗</a>` seguido de "O pacote com equipamentos é contratado mediante proposta." A oferta de maior ticket (R$ 699 a R$ 1.899/mês, linhas 182-184) termina no mesmo cadastro de teste de software; não há formulário de lead, WhatsApp ou telefone. **Correção:** Trocar o CTA por "Pedir proposta pelo WhatsApp" (link wa.me com mensagem pré-preenchida citando o pacote escolhido) e/ou um formulário curto (nome, loja, cidade, nº de caixas) gravado em um endpoint de leads. Assinar como "Equipe CrownTech".
- **[médio] Sem Open Graph, Twitter Card, canonical, robots.txt, sitemap ou dados estruturados** (`web/landing.html:3-16; cloud/pdvcloud/urls.py:18-26`): grep por `og:|twitter:|canonical|robots|sitemap|ld+json|schema.org` em web/*.html não retorna nada. urls.py só expõe /, /login/, /app/, /platform/, manifest, sw.js, offline e health. Link compartilhado no WhatsApp (principal canal com lojista) aparece sem imagem, título rico nem nome da empresa. As 21 perguntas do FAQ (landing.html:203-223) não usam schema FAQPage. **Correção:** Adicionar og:title, og:description, og:image (1200x630 self-hosted com "PDV2 por CrownTech"), og:site_name="PDV2 · CrownTech", twitter:card, link canonical e `<meta name="author" content="CrownTech">`. Incluir JSON-LD SoftwareApplication (publisher=Organization CrownTech, offers com os 3 planos) e FAQPage — como arquivo .json externo ou com hash na CSP, pois a CSP é script-src 'self' (operations/middleware.py:10). Servir /robots.txt e /sitemap.xml e marcar /app/, /platform/ e /login/ como noindex.
- **[médio] Quatro logotipos e três paletas diferentes para o mesmo produto** (`web/landing.html:22 vs :228; web/assets/icon-512.png; terminal/templates/index.html:15; terminal/static/favicon.svg; ios/.../AppIcon.png; ios/.../DesignSystem.swift:4-7`): Cabeçalho da landing usa SVG "P+2" (linha 22), o rodapé usa o texto "2" (linha 228), o ícone PWA é um "2" branco em quadrado verde, o terminal usa o glifo "▣" com verde-água #35ddb1/#0b9f7d sobre azul #0d2232 (terminal.css:21), o favicon do terminal é #12b38e, e o ícone iOS é um gráfico de linha em azul-marinho com tema azul (DesignSystem.swift:4-6). theme-color também diverge: #102630 (web), #132434 (terminal), #12352b (index.html raiz). **Correção:** Definir um único símbolo (o "P2" do logo-pdv2.svg) e uma paleta (navy #102630 + verde #148765) e gerar dele: favicon.svg, icon-192/512, ícone maskable separado, AppIcon iOS e a marca do terminal. Criar o lockup oficial "PDV2 por CrownTech" em SVG (horizontal e empilhado) e reutilizá-lo em todos os pontos.
- **[médio] Terminal não tem splash, rodapé nem "sobre" identificando o fabricante** (`terminal/templates/index.html:9, 15, 27-63, 138`): Título "PDV2 Terminal · Caixa local" (linha 9); marca "▣ PDV2 TERMINAL LOCAL" (linha 15); as telas de pareamento (27-43) e de PIN (45-63) — as que ficam expostas no monitor do caixa quando bloqueado, visíveis a clientes e a outros lojistas — não têm nenhuma assinatura; a aba Sistema (linha 138) mostra versão mas não quem fornece nem como chamar suporte. **Correção:** (1) Nas telas gate (pairView/loginView) adicionar rodapé fixo discreto: "PDV2 · um produto CrownTech · suporte (WhatsApp)". (2) Na aba Sistema criar card "Sobre" com versão, CrownTech, site e telefone de suporte. (3) Na tela de caixa bloqueado/ociosa usar um protetor com o lockup grande. Não colocar marca na tela de venda em si, para não disputar espaço com total e atalhos.
- **[médio] Proposta de valor vaga no hero e sem prova social ou imagem real do produto** (`web/landing.html:43-54, 56-84, 74, 154-156`): H1 "Seu comércio no ritmo de quem está no balcão." não diz o que é o produto (a palavra PDV/caixa/sistema não aparece no H1). O visual do hero é um mock em HTML com valores "—" (linha 74) e rótulos em 7px (landing.css:50); o mock do app mostra "R$ —" (linhas 154-156). Não há nenhuma <img>, captura real, depoimento, logotipo de cliente, número de lojas ou vídeo. hero-proof (51-53) lista recursos, não provas. **Correção:** H1 direto: "Sistema de caixa (PDV) que vende mesmo sem internet" + sub com preço inicial ("a partir de R$ 149/mês, sem fidelidade"). Substituir o mock por captura real do caixa e do painel com dados demonstrativos preenchidos (WebP self-hosted, loading=lazy fora do hero). Após os pilotos, inserir faixa de prova: 2-3 depoimentos com nome da loja/bairro em Maricá e contador "X vendas registradas". Enquanto não houver clientes, usar "Desenvolvido e suportado pela CrownTech, em Maricá" como prova de confiança.
- **[médio] Linguagem hesitante sabota a venda: "acesso antecipado", "preços de referência", seção de limitações** (`web/landing.html:59, 128, 170, 176, 186, 193-197, 228; web/app.html:47`): "Acesso antecipado" aparece 4 vezes e "referência" 4 vezes na landing (ex.: linha 170 "Preços de referência do acesso antecipado. A contratação depende da configuração de cobrança"; botão anual "2 meses de referência", linha 169). A seção 192-199 lista o que NÃO funciona ("A emissão de NFC-e em produção ainda depende da integração fiscal", "transação na maquininha feita fora do PDV") logo antes do FAQ. **Correção:** Manter a honestidade, mas mover as limitações para o FAQ e para uma página /roadmap. Preço firme: "R$ 149/mês" e "Anual: 2 meses grátis". Substituir "Acesso antecipado" por um único selo no hero ("Lançamento em Maricá · vagas para pilotos"), que vira escassez em vez de ressalva.
- **[médio] CTAs inconsistentes e teste sem condições explícitas** (`web/landing.html:35, 47, 128, 172-174, 186, 226`): Seis rótulos diferentes para o mesmo destino /login/?mode=register: "Criar conta" (35), "Criar minha conta" (47), "Conhecer o acesso antecipado" (128), "Criar conta de teste" (172-174, 226), "Conhecer o sistema" (186). Em nenhum lugar se informa duração do teste, se pede cartão, ou o que acontece depois. Os três cards de plano levam ao mesmo cadastro sem carregar o plano escolhido. **Correção:** Um único CTA primário em toda a página: "Testar grátis por 14 dias" + microcopy "sem cartão · cancele quando quiser". Passar o plano na URL (/login/?mode=register&plan=management) e pré-selecionar no cadastro. CTA secundário fixo: "Falar com a CrownTech no WhatsApp". No mobile, barra inferior fixa com os dois.
- **[médio] Erro técnico bruto exibido na seção de preços** (`web/assets/landing.js:88-93`): `$("#price-source").textContent="Não foi possível atualizar os planos agora. Valores exibidos são referências da oferta inicial. "+errorText(error);` — a mensagem do erro da API é concatenada na página de vendas. Também a linha 90 mostra "Planos indisponíveis no servidor" ao visitante. **Correção:** Em falha, manter silenciosamente os preços iniciais (starterPlans) e não exibir nada ao visitante; registrar com console.warn. Remover o texto "carregados da plataforma" (linha 88), que é jargão interno.
- **[médio] App iOS sem identificação da CrownTech e com bundle id em domínio que não é da empresa** (`ios/PDV2OwnerMonitor/PDV2OwnerMonitor/RootView.swift:151; ios/PDV2OwnerMonitor/PDV2OwnerMonitor/Info.plist:9-10; project.pbxproj:192,209`): Rodapé do login: `Text("PDV2 • Monitoramento para proprietários")`. Bundle id `br.com.pdv2.OwnerMonitor` — o reverso de um domínio pdv2.com.br que, segundo docs/STATUS.md:38 e STATUS-V2.md:3, não foi registrado. Não há tela "Sobre" nem link de suporte/privacidade no app. **Correção:** Antes da primeira submissão (o bundle id não muda depois): `br.com.crowntech.pdv2.monitor`. Rodapé: "PDV2 Monitor · por CrownTech". Adicionar tela Sobre com versão, link do site, suporte e política de privacidade (exigida pela App Store). Publicar com a conta de desenvolvedor em nome da CrownTech para o nome aparecer como vendedor na loja.
- **[médio] E-mails, MFA, admin e cobrança assinam só como "PDV2" — e o sistema ainda não envia nenhum e-mail** (`cloud/pdvcloud/settings.py:123-129; cloud/core/views_auth.py:86,94; cloud/pdvcloud/urls.py:15-16; integrations/billing.py:20,46`): `DEFAULT_FROM_EMAIL = ... "PDV2 <no-reply@pdv2.local>"`; grep por send_mail/EmailMessage no código (fora testes) não retorna nada — não há boas-vindas, recuperação de senha nem aviso de cobrança próprios. MFA: `issuer_name="PDV2 SaaS"`. Admin: `site_header = "PDV2 • Administração da plataforma"`. Asaas: `"description":"Assinatura PDV2"` e `User-Agent: PDV2/2.0` — é o texto que aparece no boleto e no e-mail de cobrança que o Asaas envia ao lojista. **Correção:** Remetente padrão "PDV2 por CrownTech <nao-responda@dominio-crowntech>"; criar template base de e-mail (cabeçalho com lockup, rodapé com CNPJ/endereço/suporte CrownTech) e usá-lo em boas-vindas, redefinição de senha, fatura e trial expirando. MFA issuer "PDV2 (CrownTech)". Asaas description "Assinatura PDV2 - CrownTech". Admin header "PDV2 · CrownTech — Administração da plataforma".
- **[baixo] Manifest/PWA sem menção ao fabricante e com descrição restrita** (`web/manifest.webmanifest:3-5, 13-14`): `"name": "PDV2 Gestão"`, `"description": "Painel de gestão PDV2 para mercados e mercearias."` — a landing vende para 7 segmentos (landing.html:94-105). Os dois ícones usam `"purpose": "any maskable"` no mesmo arquivo, o que corta o símbolo em launchers com máscara. **Correção:** `"name": "PDV2 Gestão · CrownTech"`, descrição "Painel de gestão do PDV2, um produto CrownTech, para o varejo."; separar ícones `any` e `maskable` (este com margem de segurança de 20%).
- **[baixo] Painel, tela offline e página de status sem assinatura; não existe página pública de status** (`web/app.html:18, 21-26, 34-38; web/offline.html:13; cloud/core/views_pages.py:17-21`): Loader "PDV2 Preparando sua área de gestão…" (app.html:18); o rodapé da sidebar (34-38) só tem usuário e Sair; offline.html:13 mostra apenas a marca "2 PDV2". /health devolve só `{"status":"ok"}` em JSON (views_pages.py:17-21) — não há página de status legível. **Correção:** No pé da sidebar, abaixo de "Sair da conta", linha de 10-11px: "PDV2 v{versão} · CrownTech · Ajuda". Em offline.html, rodapé "PDV2 por CrownTech · suporte: (telefone)" — é justamente quando o lojista precisa do contato. Criar /status/ público simples (API, sincronização, cobrança, fiscal) com o lockup.
- **[baixo] index.html da raiz é uma segunda landing antiga, divergente da oficial** (`index.html:6-9; app.py:454-463`): index.html (329 linhas) tem título "PDV2 — mais ritmo para o seu negócio", theme-color #12352b e CSS próprio; é servido por app.py:454 junto com /logo.svg (app.py:462-463). A landing oficial é web/landing.html, servida por cloud/core/views_pages.py:9. Duas peças de marca com textos, cores e descrições diferentes. **Correção:** Remover ou arquivar index.html/app.py legados (ou redirecionar para a landing oficial) para que exista uma única peça de marca a manter.
- **[baixo] SEO local e acessibilidade de detalhe na landing** (`web/landing.html:10-11, 25, 56; web/assets/landing.css:13, 25-26, 50`): Title "PDV2 · Gestão que acompanha o ritmo do seu comércio" não contém "sistema PDV", "frente de caixa" nem "Maricá"; 37 declarações de fonte de 9-11px na landing.css (hero-kicker 11px, hero-proof 10-11px, preview 7px); `aria-label` em <div class="hero-visual"> sem role (linha 56); botão de menu usa o caractere "☰" (linha 25). Ponto positivo: a página pesa ~29 KB gzip no total, sem imagens nem fontes externas. **Correção:** Title: "PDV2 — Sistema PDV e frente de caixa offline para mercados e lojas | CrownTech". Subir textos de apoio para no mínimo 13px. role="img" no hero-visual. Criar páginas por segmento (/pdv-para-mercado, /pdv-para-moda) e por cidade quando expandir. Preservar o orçamento de peso ao incluir capturas (WebP, lazy, width/height definidos).

## Cupom e DANFE NFC-e

### Proposta

## Layout final proposto (80 mm = 48 col; 58 mm = 32 col)

O modelo canônico fica em `receipt_layout`, que passa a ter um campo `fiscal.state` com os valores `none|authorized|contingency|homologation`. A prévia em `receiptMarkup` continua só renderizando os blocos.

### Cupom fiscal (DANFE NFC-e)
```
        MERCADO BOA VISTA            (negrito, 2x)
     BOA VISTA COMERCIO DE ALIM. LTDA
 CNPJ 12.345.678/0001-90  IE 123456789
 R. das Flores, 100 - Centro - Maricá/RJ
           CEP 24900-000
------------------------------------------------
  DOCUMENTO AUXILIAR DA NOTA FISCAL DE
       CONSUMIDOR ELETRÔNICA         (negrito)
------------------------------------------------
# CÓDIGO        DESCRIÇÃO
          QTDE UN x VL UNIT          VL TOTAL
001 7891234567890 ARROZ TIPO 1 5KG
          2 UN x 24,90                  49,80
------------------------------------------------
Qtde. total de itens                        12
Valor total R$                          187,40
Desconto R$                             -10,00
VALOR A PAGAR R$                        177,40 (negrito, alto)
FORMA DE PAGAMENTO                 VALOR PAGO
Dinheiro                                200,00
Troco R$                                 22,60
------------------------------------------------
[ÁREA DE MENSAGEM FISCAL, só se houver]
     EMITIDA EM CONTINGÊNCIA (negrito)
       Pendente de autorização
 ou EMITIDA EM AMBIENTE DE HOMOLOGAÇÃO - SEM VALOR FISCAL
------------------------------------------------
  Consulte pela Chave de Acesso em
  www.fazenda.rj.gov.br/nfce/consulta
3326 1112 3456 7800 0190 6500 1000 0012
3410 0012 3456
------------------------------------------------
CONSUMIDOR CPF 123.456.789-09 - JOÃO SILVA
 (ou CONSUMIDOR NÃO IDENTIFICADO)
------------------------------------------------
NFC-e nº 000001234 Série 001 15/11/2026 14:32:10
Protocolo de autorização: 333260000123456
Data de autorização: 15/11/2026 14:32:12
       Via consumidor  (só em contingência)
            [QR CODE >= 25 mm]
     Consulta via leitor de QR Code
------------------------------------------------
Tributos Totais Incidentes (Lei Federal
12.741/2012): R$ 31,20  Fonte: IBPT
------------------------------------------------
Venda 3f9a1c2e · Caixa 02 · Op. Ana
Obrigado pela preferência!
          PDV2 · CrownTech  (fonte B, pequena)
```
- **58 mm:** os mesmos blocos. O nome fantasia sai só em altura dupla, sem largura dupla, para não estourar 16 colunas. A chave ocupa 3 linhas de 4 blocos e a identificação da NFC-e, 2 linhas. O QR fica centralizado, com módulo de 3 dots.
- **80 mm:** o leiaute lateral é opcional. O QR ocupa cerca de 30 mm à esquerda, com identificação e protocolo à direita (impresso em modo raster).
- **Contingência:** imprimir 2 vias seguidas (consumidor e estabelecimento), com corte parcial entre elas.

### Comprovante não fiscal
```
        MERCADO BOA VISTA
 CNPJ 12.345.678/0001-90
------------------------------------------------
      COMPROVANTE DE VENDA (negrito)
     NÃO É DOCUMENTO FISCAL
15/11/2026 14:32 · Venda 3f9a1c2e · Caixa 02
------------------------------------------------
(itens, totais e pagamento no mesmo formato do DANFE)
------------------------------------------------
Tributos aprox. (Lei 12.741/2012): R$ 31,20
Obrigado pela preferência!
          PDV2 · CrownTech
```
Os vouchers de devolução e de fiado usam o mesmo esqueleto: título em altura dupla e linha de marca no rodapé.

### Implementação
1. **Primeiro, tirar o NCM e os rótulos 'Unit:' e 'Total item:'**. A linha de itens passa a ser uma `pair(f"{qtd} {un} x {unit}", total)` alinhada à direita.
2. **Criar `fiscal_message(state)`** para a Área de Mensagem Fiscal. A nota em homologação deve ser identificada por `issuer.fiscal_environment`.
3. **Ampliar o objeto `fiscal`**: `issued_at`, `url_consulta`, `contingency`, `taxes_federal/state/municipal_cents` e `infcpl`.
4. **Gerar a linha de marca com ESC M 1 (fonte B, 64 col em 80 mm)**. Na prévia, essa linha fica com a classe `.receipt-text-small`.
5. **Corrigir a acentuação** de todos os textos fixos (CP850 já está selecionado).
6. **Remover `legacyReceiptMarkup`** e mostrar na prévia um selo com o estado fiscal.

*Observação:* o PDF do manual v5/v6 não pôde ser lido como texto aqui. A numeração das divisões varia entre versões (a v4.2 coloca os totais na Divisão IV), por isso convém conferir no MOC DANFE NFC-e 6.00 da NT 2025.001.

**Fontes:** [Manual DANFE NFC-e (SEFAZ-RR)](https://www.sefaz.rr.gov.br/Manual_do_DANFE_NFC-e_QR_Code.pdf), [NT 2025.001 – QR 3.00 e MOC 6.00 (Inventti)](https://inventti.com.br/nf-e-nfc-e-nt-2025-001-versao1-00-novo-leiaute-qr-code-3-00-e-moc-danfe-nfc-e-6-00), [NT 2026.003 (Tecnospeed)](https://blog.tecnospeed.com.br/?p=35340), [Manual v4.2](https://migrate.info/desenvolvedores/web/BR/InvoiCy/Brasil/Documentos/Manual_DANFE_NFCe_Versao4.2.pdf), [Fórum ACBr](https://www.projetoacbr.com.br/forum/topic/55544-novo-manual-de-especifica%C3%A7%C3%A3o-t%C3%A9cnica-do-danfe-da-nfc-e/)

### Problemas encontrados

- **[alto] Não existe DANFE em contingência offline: NFC-e sem protocolo vira 'COMPROVANTE NAO FISCAL'** (`terminal/printers.py:132, 155, 219-248`): authorized = bool(fiscal.get('authorized') and fiscal.get('access_key') and fiscal.get('protocol')). Uma NFC-e emitida em contingência (tpEmis=9) tem chave e QR, mas ainda não tem protocolo, e por isso cai no ramo else, que imprime 'NFC-e nao emitida / Documento sem valor fiscal'. Não há nenhum campo de contingência no código: o grep por 'contingen|tpEmis' em terminal/, integrations/ e shared/ não encontrou nada. Para um caixa feito para operar offline, essa é a principal lacuna. **Correção:** Trocar o booleano por um estado fiscal: none | authorized | contingency | homologation | cancelled. Em contingência, imprimir a Área de Mensagem Fiscal 'EMITIDA EM CONTINGÊNCIA' / 'Pendente de autorização' (centralizada e em negrito), mostrar chave, QR e número/série sem a linha de protocolo e gerar duas vias ('Via consumidor' e 'Via estabelecimento').
- **[alto] Falta a mensagem de homologação, embora o ambiente já chegue ao terminal** (`cloud/core/views_terminal.py:77; terminal/printers.py:130-155`): _issuer_profile envia 'fiscal_environment', mas receipt_layout nunca lê esse campo. Uma nota de homologação sairia como 'DANFE NFC-e' normal, sem o aviso 'EMITIDA EM AMBIENTE DE HOMOLOGAÇÃO – SEM VALOR FISCAL'. **Correção:** Se issuer.fiscal_environment != 'production', imprimir na Área de Mensagem Fiscal 'EMITIDA EM AMBIENTE DE HOMOLOGAÇÃO - SEM VALOR FISCAL' em negrito e centralizado. Isso também vale para o comprovante não fiscal de terminais em teste.
- **[alto] Divisão IV incompleta: falta 'Consulte pela Chave de Acesso em' com a URL da SEFAZ da UF** (`terminal/printers.py:222-224`): O código imprime apenas text('CHAVE DE ACESSO') e a chave em blocos de 4. Não imprime a URL de consulta da UF (urlChave do XML), que é obrigatória junto da chave. O objeto fiscal também não tem nenhum campo para essa URL. **Correção:** Adicionar fiscal.url_consulta (vindo de infNFeSupl/urlChave, retornado pela Focus) e imprimir 'Consulte pela Chave de Acesso em' + URL (com quebra) + chave em 11 blocos de 4. Em 48 colunas a chave cabe em 2 linhas de 6+5 blocos; em 32 colunas, em 3 linhas de 4 blocos.
- **[médio] Título do documento fora do padrão** (`terminal/printers.py:155`): Imprime 'DANFE NFC-e' (ou 'COMPROVANTE NAO FISCAL'). O cabeçalho oficial pede o texto por extenso 'Documento Auxiliar da Nota Fiscal de Consumidor Eletrônica'. **Correção:** Imprimir 'DOCUMENTO AUXILIAR DA NOTA FISCAL' / 'DE CONSUMIDOR ELETRÔNICA', centralizado e em negrito: 2 linhas em 58 e em 80 mm.
- **[médio] Identificação da NFC-e (Divisão VI/VII) sem data/hora de emissão e com protocolo sem rótulo de data** (`terminal/printers.py:158-159, 221, 225-227`): Hoje sai 'NFC-e: {number}  Serie: {series}', sem dhEmi na mesma linha. A data impressa no topo (159) é created_at da venda, não a data de emissão. Depois do protocolo, authorized_at sai sozinho, sem 'Data de autorização'. O número não tem os 9 dígitos com zeros à esquerda nem a série os 3. **Correção:** Usar 'NFC-e nº 000001234  Série 001  15/11/2026 14:32:10' a partir de fiscal.issued_at, depois 'Protocolo de autorização: 333260000123456' e 'Data de autorização: 15/11/2026 14:32:12'. Para 58 mm, dividir em 2 linhas.
- **[médio] Informações extras entre o cabeçalho e os itens (Venda, Caixa, Operador, Vendedor, NCM)** (`terminal/printers.py:160-166, 172-173`): Venda (id[:16]), Caixa, Operador e Vendedor ficam logo abaixo do título, e o NCM entra na linha de descrição do item. O manual reserva esse tipo de dado para a mensagem de interesse do contribuinte (Divisão IX), e o NCM não faz parte das colunas do detalhe. Em 32 colunas o NCM ainda força quebra de linha. **Correção:** Tirar o NCM do cupom. Mover Venda, Caixa, Operador e Vendedor para o bloco final de informações do contribuinte, em uma ou duas linhas compactas.
- **[médio] Detalhe dos itens sem cabeçalho de colunas e com rótulos verbosos** (`terminal/printers.py:168-189`): Usa 'ITENS DA COMPRA' em vez da linha de colunas 'CÓDIGO DESCRIÇÃO QTDE UN VL UNIT VL TOTAL', e cada item gasta 2 a 3 linhas com 'Unit:' e 'Total item:' (181, 187). Os descontos por item saem sem sinal negativo (189). **Correção:** Em 80 mm (48 col): cabeçalho 'Cód  Descrição' / 'Qtde UN x Vl Unit = Vl Total'; linha 1 com '001 7891234567890 ARROZ TIPO 1 5KG' e linha 2 alinhada à direita com '2 UN x 24,90   49,80'. Em 58 mm (32 col), o mesmo em 2 linhas. Descontos e acréscimos sempre como '-R$' e '+R$'.
- **[médio] Totais não seguem a sequência oficial e mostram 'Descontos' zerado sem sinal** (`terminal/printers.py:191-197`): A sequência é 'Quantidade de itens', 'Subtotal', 'Promocoes', 'Descontos' (sempre impresso, mesmo com R$ 0,00, e sem '-'), linha vazia e 'TOTAL A PAGAR'. O padrão é 'Qtde. total de itens', 'Valor total R$', 'Desconto R$', 'Acréscimos R$' (frete/outros) e 'Valor a Pagar R$'. Acréscimos não existem no layout. **Correção:** Mostrar desconto e acréscimo apenas quando forem diferentes de zero, sempre com sinal. Somar promoções em 'Desconto R$' no DANFE (o detalhe pode ir na mensagem do contribuinte) e mudar o rótulo final para 'Valor a Pagar R$'.
- **[médio] Tributos da Lei 12.741 com rótulo genérico e ausentes no não fiscal** (`terminal/printers.py:243-244`): pair('Tributos informados', ...) só aparece quando authorized. Falta a expressão legal 'Informação dos Tributos Totais Incidentes (Lei Federal 12.741/2012)' e a fonte (IBPT), e o comprovante não fiscal nunca mostra tributos. **Correção:** Usar 'Tributos Totais Incidentes (Lei Federal 12.741/2012): R$ X', opcionalmente separados em Federal/Estadual/Municipal e com 'Fonte: IBPT'. Imprimir também no comprovante não fiscal quando houver valor aproximado.
- **[médio] QR Code some sem aviso quando passa de 700 bytes; DANFE sem QR é inválido** (`terminal/printers.py:229; terminal/printers.py:46-47`): if qr_url and len(...) <= 700: senão o bloco não é criado e o cupom sai sem QR, sem erro. _qr_escpos também devolve b'' acima de 700. O rótulo 'Consulte pelo QR Code' (242) foge da frase do manual. **Correção:** Se uma nota autorizada ou em contingência não tiver QR válido, bloquear a impressão do DANFE com um erro claro, ou imprimir o QR em modo raster. Trocar o rótulo para 'Consulta via leitor de QR Code' e garantir no mínimo 25x25 mm com zona de silêncio. Em 80 mm, permitir o leiaute lateral (QR à esquerda, identificação e protocolo à direita).
- **[baixo] Falta a Divisão IX (mensagem de interesse do contribuinte / infCpl)** (`terminal/printers.py:249-251`): No lugar dela, o layout fixa 'OBRIGADO PELA PREFERENCIA!' / 'Volte sempre' e não tem campo para informações complementares (infCpl) nem para a mensagem configurável da loja. **Correção:** Criar o bloco final 'Informações complementares' com infCpl, a mensagem da loja, dados de venda/caixa/operador e a linha de marca 'PDV2 · CrownTech'.
- **[baixo] Textos fixos sem acentuação, embora o código já imprima em CP850** (`terminal/printers.py:155, 176, 194, 200-201, 218, 247-250`): 'COMPROVANTE NAO FISCAL', 'Promocao', 'Promocoes', 'Cartao', 'CONSUMIDOR NAO IDENTIFICADO', 'nao emitida', 'PREFERENCIA'. Mas layout_bytes seleciona CP850 (ESC t 2, linha 323), e _receipt_text já codifica em cp850, que tem acentos e o '·'. **Correção:** Escrever os textos em português correto: 'CONSUMIDOR NÃO IDENTIFICADO', 'Cartão de crédito', 'NÃO É DOCUMENTO FISCAL'.
- **[baixo] Venda cancelada continua imprimindo layout de DANFE válido** (`terminal/printers.py:155-157`): Com status voided e authorized=True, o cupom sai como 'DANFE NFC-e' com '*** VENDA CANCELADA ***' logo abaixo, mantendo QR e protocolo como se a nota fosse válida. **Correção:** Para NFC-e cancelada, não reimprimir o DANFE. Imprimir um comprovante de cancelamento não fiscal com a chave e o protocolo do evento de cancelamento.
- **[baixo] Prévia legada diverge do impresso e lê campos que o backend não envia** (`terminal/static/terminal.js:100, 104, 107-108`): A prévia legada considera a nota autorizada só com authorized+access_key, sem exigir protocol (no impresso, printers.py:132 exige). Ela também mostra a URL do QR como texto, usa fiscal.qr_data_uri, que nenhum código preenche (o QR real vem em block.data_uri), e converte com Number(sale.created_at), que gera 'Invalid Date' quando a data vem em ISO. **Correção:** Remover legacyReceiptMarkup e sempre pedir o receipt canônico (receiptDialog já busca /api/sales/:id). Na prévia, mostrar um selo de estado: Não fiscal, Contingência ou Homologação.
- **[baixo] Etiqueta ZPL dimensionada para 75x35 mm, não 50x30 mm** (`terminal/printers.py:345, 358`): ^PW600^LL280 a 203 dpi equivale a cerca de 75x35 mm, mas a mensagem da linha 345 fala em etiquetas de 50x30 mm (400x240 dots). O código de barras em ^FO20,145 com altura 85 ultrapassa 240 dots. **Correção:** Parametrizar o tamanho (^PW400^LL240 para 50x30) e reposicionar nome, preço e código de barras.

## Planos e permissões

### Proposta

## Matriz atual (O=owner A=admin G=gerente C=caixa E=estoque F=financeiro V=consulta)

| Ação | Quem pode hoje |
|---|---|
| Ver vendas, dashboard, estoque (API) | todos, sem papel |
| Cancelar venda (painel) | O A G · estornar: O A F · devolver: O A G F (botão só O A G) |
| Relatórios | O A G F V + plano `reports` |
| Caixas (leitura) | O A G F V |
| Produtos, grade, promoções, categorias | escrita O A G E |
| Clientes, incluindo limite de fiado | escrita O A G C |
| Estoque, NF-e, transferências | escrita O A G E · leitura +F V |
| Inventário | abrir O A G E · contar +C · fechar O A G |
| Fiado, recebimento no painel | O A G F |
| Financeiro | ler O A G F V · escrever O A F |
| Comissões | ler O A G F · pagar O A F |
| Equipe, terminais, pagamentos, empresa, assinatura, auditoria | O A |
| Terminal: vender, abrir caixa, sangria, receber fiado | O A G C |
| Terminal: fechar caixa | sempre PIN de supervisor |
| Terminal: cancelar, devolver, remover item | G ou PIN de supervisor |
| Terminal: desconto | C 10%, G 50%, O/A 100%, todos limitados por `max_discount_bps` |

## 1. Permissões granulares

- **Catálogo fixo de códigos** em `shared/permissions.py`, cada código mapeado à feature do plano:
  - vendas: `sales.view`, `sales.view_all_shops`, `sales.cancel`, `sales.return`, `sales.refund`
  - caixa: `cash.open`, `cash.withdraw`, `cash.supply`, `cash.close`, `cash.view`
  - fiado: `credit.sell`, `credit.limit.set`, `credit.receive`
  - catálogo: `catalog.edit`, `price.edit`, `cost.view`, `promotions.manage`, `commissions.configure`
  - estoque: `stock.adjust`, `stock.nfe_import`, `inventory.open`, `inventory.count`, `inventory.close`, `transfer.send`, `transfer.receive`
  - financeiro: `finance.view`, `finance.write`, `commissions.pay`
  - painel: `reports.view`, `dashboard.financial`
  - administração: `users.manage`, `owners.manage`, `terminals.manage`, `payments.configure`, `fiscal.configure`, `fiscal.issue`, `billing.manage`
- **Modelo `AccessProfile`** (tenant, nome, `permissions` JSON tri-estado `allow|pin|deny`, `limits` JSON `{discount_bps, withdraw_cents, return_cents}`). `Membership.profile` (FK) e `Membership.shops` (M2M) para escopo por loja.
- **Migração:** os 7 papéis atuais viram perfis-sistema com o comportamento de hoje. O perfil Proprietário não pode ser editado, e `owners.manage`/`billing.manage` não podem ser delegados.
- **Cloud:** `require_perm(member, code)` substitui `roles=`. Ele também chama `require_feature`, porque o mapa perm→feature está no catálogo.
- **Terminal:** o lease leva a lista de operadores com suas permissões e limites (o `roles_version` já existe). O `store` deixa SELL_ROLES/MANAGER_ROLES/DISCOUNT_BPS; o estado `pin` vira `manager_override`.
- **Painel:** `/auth/me` devolve as permissões efetivas, e navSpecs passa a usar `perm:` no lugar de `roles`.
- **Tela "Perfis":** grade de checkboxes por módulo, com o seletor Permitido / Com PIN / Negado e campos de limite em R$ e %. Cada alteração fica na auditoria.

## 2. Nova grade de planos

| | Essencial R$149 | Gestão R$229 | Avançado R$349 |
|---|---|---|---|
| Lojas/terminais | 1/1 | 1/2 | 3/5 |
| Usuários | 3 | 8 | 25 |
| Produtos | 2.000 | 10.000 | ilimitado |
| Recursos | PDV offline, caixas e conferência, formas de pagamento, devoluções e trocas, fiado básico, estoque por loja, promoção de preço fixo, painel do dia | + financeiro, comissões, relatórios e CSV, grade, NF-e de entrada, inventário guiado, promoções avançadas, perfis editáveis | + transferências, relatórios multiloja, escopo por loja, auditoria completa, suporte prioritário |

**Add-ons:**
- terminal extra R$39
- loja extra R$79 (já com 1 terminal)
- pacote de 5 usuários R$19
- emissão NFC-e R$49 por loja
- implantação e migração por orçamento

**Implementação:**
- `Plan` ganha `user_limit` e `product_limit`.
- Novo `SubscriptionAddon` (tipo, quantidade); o limite efetivo é plano + add-ons, aplicado em `capacity_fits`.
- Features com códigos por módulo: `pos`, `cash`, `credit_account`, `returns`, `stock`, `inventory`, `transfers`, `nfe_inbound`, `grid`, `promotions_basic`, `promotions_advanced`, `finance`, `commissions`, `reports`, `multi_shop`, `custom_profiles`, `fiscal_nfce`, `audit`.
- Uma migração de dados converte as listas atuais para esses códigos.
- A landing passa a ler os planos de `/plans`.
- O trial roda no Gestão. No rebaixamento, os dados acima do limite ficam só leitura.

### Problemas encontrados

- **[alto] Visão geral expõe faturamento e contas a pagar/receber a caixa e estoquista** (`cloud/core/views_sales.py:103-140; web/assets/app.js:35`): dashboard() chama membership_context(request) sem roles (l.105) e devolve revenue_cents, open_payables_cents e open_receivables_cents (l.135-139). O menu 'Visão geral' (app.js:35) inclui cashier e inventory. Também /sales (views_sales.py:26), /sales/people (l.46) e /stock (views_stock.py:31) não têm restrição de papel e mostram todas as lojas. **Correção:** Separar 'dashboard.operational' (vendas do dia da própria loja) de 'dashboard.financial' (faturamento, títulos). Exigir papel ou permissão em dashboard, sales e stock. Omitir payables/receivables para quem não tem finance.view.
- **[alto] Admin redefine o PIN do proprietário e pode desativá-lo** (`cloud/core/views_users.py:120-136 e 85-115`): user_pin usa roles=MANAGE={'owner','admin'} (l.14,122) e não verifica se o alvo é owner. Assim um admin define o PIN do dono e opera o terminal como owner (DISCOUNT_BPS 100%, MANAGER_ROLES em terminal/store.py:22,46). Em user_detail só a troca de 'role' é protegida (l.103-104). 'active' só passa por _last_owner (l.26-31), então um admin desativa um proprietário se houver mais de um. **Correção:** Bloquear PIN, nome, comissão e ativação de membership owner para quem não é owner. Tornar 'gerir proprietários' permissão não delegável.
- **[alto] Sangria e suprimento sem supervisor nem limite** (`terminal/app.py:272-277; terminal/store.py:1029-1046`): /api/cash/movement exige apenas SELL_ROLES (inclui cashier). cash_movement não chama manager_override e só barra uma sangria maior que o esperado na gaveta (l.1037). Para comparar: close_cash sempre pede PIN (l.1052) e void/return pedem gerente (l.1281,1353). **Correção:** Criar as permissões cash.withdraw e cash.supply com limite em centavos por perfil. Acima do limite, exigir PIN de supervisor e gravar o supervisor_id no evento.
- **[alto] Operador de caixa define o limite de fiado do cliente** (`cloud/core/views_resources.py:21, 88-90, 115`): customers tem write_roles CONTACT_WRITE|{'cashier'} e form_values aceita credit_limit_cents para customers. O painel também libera criar e editar cliente para o cashier (app.js:514). **Correção:** Permissão própria credit.limit.set (padrão: owner, admin, finance). O cashier continua cadastrando cliente, mas sem o campo de limite.
- **[alto] Limites de usuários e produtos não existem** (`cloud/core/models.py:19-28; cloud/core/views_users.py:35-81; views_resources.py:183-202`): Plan só tem terminal_limit e shop_limit. Esses dois são aplicados: terminais em views_terminal.py:156,188 e views_platform.py:449, lojas em views_resources.py:192,232. Já users() cria Membership sem contar usuários, e collection('products') e product_groups criam sem teto. capacity_fits (operations/services.py:19-20) também só olha lojas e terminais. **Correção:** Adicionar user_limit e product_limit (null = ilimitado) ao Plan e somar add-ons. Checar com select_for_update como já é feito em shops e estender capacity_fits.
- **[médio] Recursos novos sem require_feature (o plano não restringe nada além de finance/reports)** (`cloud/core/api_common.py:169-172; views_inventory/transfers/purchases/promotions/payment_methods/customer_credit/cash/product_groups; operations/views.py:190`): require_feature só aparece em views_finance, views_commissions, views_reports, views_owner e views_sales (dashboard). Inventário, transferências, NF-e de entrada, grade, promoções, fiado, formas de pagamento, caixas e fiscal_submit não têm gate. A feature 'stock' só é checada no front (app.js:39,42). 'pdv_local' e 'priority_support' nunca são lidas. **Correção:** Definir um catálogo de features por módulo e aplicar require_feature em cada endpoint, no front (navSpecs) e no terminal.
- **[médio] O terminal recebe as features do plano e as ignora** (`cloud/core/views_terminal.py:93; terminal/store.py:471`): O lease envia 'features': sub.plan.features e o store só guarda o valor (l.471). Nenhuma checagem em sale/receive_credit/return_sale/promoções, e terminal.js não lê features. Fiado, crédito na loja e promoções funcionam em qualquer plano. **Correção:** Store.require_feature(code) com base nos claims assinados do lease, aplicado em vendas a prazo, promoções avançadas e devoluções com crédito.
- **[médio] Devolução pelo painel liberada para finance na API, mas sem botão; estorno ('refund') exclui o gerente e não tem UI** (`cloud/core/views_sales.py:68, 81; web/assets/app.js:1037-1040`): sale_return aceita {'owner','admin','manager','finance'}, mas o botão 'Registrar devolução' fica dentro de if(canSales()) (owner/admin/manager), então o finance nunca o vê. sale_reverse refund aceita {'owner','admin','finance'} e cancel aceita {'owner','admin','manager'}. Nenhuma tela chama /refund. **Correção:** Unificar como permissões sales.cancel, sales.return e sales.refund. Renderizar cada botão pela sua própria permissão e remover o endpoint órfão ou dar UI a ele.
- **[médio] Papel 'inventory' altera a comissão por categoria** (`cloud/core/views_resources.py:16, 19, 82`): categories usa CATALOG_WRITE (inclui inventory), e form_values de categories aceita commission_bps. Isso muda quanto será pago em comissões (views_commissions PAY={'owner','admin','finance'}). **Correção:** Mover commission_bps para a permissão commissions.configure (owner, admin, finance).
- **[médio] Admin contrata e troca o plano, mexe no fiscal e no teto de desconto** (`operations/views.py:18, 22-28, 144`): OWNER=('owner','admin') protege company (inclui fiscal_environment, nfce_next_number e max_discount_bps) e subscribe (cobrança). **Correção:** billing.manage como permissão padrão só do owner, delegável explicitamente. fiscal.configure separada de company.edit.
- **[médio] Desconto por papel fixo no código e prazo do fiado não editável** (`terminal/store.py:46; cloud/core/models.py:55-57; operations/views.py:25-28`): DISCOUNT_BPS={'cashier':1000,'manager':5000,...} está hardcoded. A empresa só controla o teto global max_discount_bps. credit_term_days existe no modelo, mas nenhum endpoint o aceita (fields de company não o listam). **Correção:** Limites por perfil (discount_bps, withdraw_cents, return_cents, credit_limit_cents) dentro do perfil editável, enviados no lease. Expor credit_term_days em company.
- **[médio] Seed do Avançado diverge da landing** (`cloud/core/migrations/0002_seed_plans.py:11; 0006_advanced_terminal_limit.py:7-8; web/landing.html:174`): A seed cria o Avançado com 1 terminal e 2 lojas. A 0006 só sobe para 3 terminais se o plano não tiver sido customizado. A landing promete 'Até 3 terminais · até 2 lojas' em texto fixo, sem ler /api/v1/plans. 'Fiado com limite' aparece no Essencial (l.172) e 'comissão' no Gestão sem gate próprio (vem de finance). **Correção:** Gerar os cards da landing a partir de /plans (views_auth.py:337) e versionar os planos por slug+versão.
- **[médio] Trial começa no Essencial e esconde os recursos pagos** (`cloud/core/views_auth.py:143-153`): register procura o Plan slug='essential' e cria trial de 14 dias nele, então quem testa nunca vê financeiro, comissões ou relatórios. **Correção:** Trial no Gestão, com rebaixamento para o Essencial ao fim (dados excedentes ficam só leitura).
- **[médio] Sem escopo por loja** (`cloud/core/models.py:95-107`): Membership não tem vínculo com Shop. Qualquer papel lê vendas, estoque e caixas de todas as lojas (views_sales.py:26-29, views_cash.py:43). **Correção:** Membership.shops (M2M, vazio = todas) e filtro obrigatório nos querysets de leitura e escrita.
- **[baixo] Pequenas incoerências entre menu e API** (`web/assets/app.js:41,51; cloud/core/views_users.py:15; views_resources.py:19-20; views_customer_credit.py:11 vs terminal/store.py:1457`): A API de Equipe permite manager ler (READ inclui manager), mas o menu mostra só owner/admin. Categories e products são legíveis por finance e cashier na API, mas o menu 'Categorias' omite os dois. O recebimento de fiado no painel exclui o cashier, enquanto no terminal o cashier recebe (SELL_ROLES). **Correção:** Gerar o menu a partir das mesmas permissões da API (/auth/me devolve a lista efetiva).

