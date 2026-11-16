## Benchmark PDV2 x concorrentes de varejo no Brasil (nov/2026)

### Base usada
- **O que o PDV2 já tem** (`docs/ROADMAP-COMPLETUDE.md` linhas 13-27): operador e vendedor na venda, caixa na nuvem com conferência e quebra, comissão, fiado com limite que funciona offline, devolução, troca e crédito na loja, saldo de estoque materializado, formas de pagamento configuráveis, grade de variações, compra pelo XML da NF-e, inventário pelo celular, transferência entre lojas, promoções offline e relatórios em CSV (ABC, lucro, fluxo de caixa, perdas).
- **Ainda listado como "Próximos"** (linha 27): fidelidade e cashback, importador de planilha, lote e validade, DRE e gráficos.
- **Bloqueios de produção** (`docs/STATUS-V2.md` linhas 25-26 e 37): não há emissão real de NFC-e nem contingência fiscal. Não há TEF, Pix dinâmico ou confirmação bancária. Também faltam conciliação, lote e validade.
- **Conferido no código:**
  - Já existe código de balança: `scale_code` em `cloud/core/models.py:149`.
  - Já existem etiqueta de código de barras (`web/assets/app.js:610`) e configuração de etiquetas (`cloud/core/views_labels.py:31`).
  - Não encontrei nada de WhatsApp, cashback, vitrine pública, consignação ou condicional, nem modelo de vários códigos de barras por produto (A9/4.6).
- **Concorrentes:**
  - Bling: NFC-e, Pix por QR integrado à conta própria, Smart POS e TEF via CPlug. Tem assistente de IA e conector MCP.
  - Kyte: catálogo online com pedido por WhatsApp, link de pagamento e uma IA ("Kira") que atende clientes.
  - Tiny/Olist: estoque unificado com mais de 40 marketplaces.
  - Conta Azul: PDV integrado ao financeiro e à contabilidade. A Conta Azul IA lê documentos enviados por WhatsApp.
  - Omie: o sistema de gestão funciona pelo WhatsApp. Também tem TEF homologado e uma IA para parametrizar tributos.
  - PagBank PagVendas e Stone: PDV grátis amarrado à própria maquininha, com Pix. A Stone tem TEF pelo Connect 2.0.
  - MarketUP: plano gratuito que funciona offline.
  - TOTVS/Linx: self-checkout e plataforma de agentes de IA (LYNN).
  - Sistemas de moda (Una, Vestuar): condicional, crediário e etiqueta com grade.
- **Limites da pesquisa:** não achei material específico de Hiper, Nex, eGestor, Saipos varejo e Consumer.

### (a) O que falta no PDV2 e todo concorrente tem, em ordem de prioridade
1. **NFC-e em produção com contingência offline.** Sem isso o produto não pode ser vendido (STATUS-V2 linha 25).
2. **Pix dinâmico e TEF/Smart POS integrados.** Bling, Omie, Stone e PagBank confirmam o pagamento sem o operador digitar o valor. Isso elimina erro de valor e dá conciliação automática.
3. **Importador de planilha para migração.** Sem ele o lojista não troca de sistema.
4. **Vários códigos de barras por produto (fardo e unidade, A9).** É essencial para mercearia.
5. **Lote e validade com alerta de vencimento.** Pesa muito em perecíveis.
6. **Fidelidade e cashback.**
7. **DRE, gráficos no painel e exportação em PDF/XLSX.**
8. **Condicional (mala de peças levada pelo cliente) com acerto.** É padrão nos sistemas de moda.
9. **Catálogo online e pedido por WhatsApp** (o Kyte oferece no plano grátis).
10. **Conciliação bancária (OFX) e de recebíveis de cartão.**
11. **Pedido de compra com recebimento parcial.**
12. **Integração com e-commerce e marketplaces** (Tiny, Bling, Conta Azul). Pesa mais para moda.
13. **Etiqueta de gôndola com R$/kg em lote,** puxada pelas mudanças de preço.
14. **Comprovante digital por WhatsApp e cobrança do fiado vencido.**

### (b) Ideias inovadoras e viáveis
Todas aproveitam o que o PDV2 já guarda: eventos do terminal, `CashSession`, `StockBalance`, promoções e fiado.
1. **Sugestão de compra com previsão de ruptura.** Calcula giro, sazonalidade e prazo de cada fornecedor e gera o pedido de compra pronto.
2. **Precificação assistida.** Sugere preço a partir do custo real do XML, da margem-alvo e da elasticidade observada, com alerta de margem negativa depois de uma promoção.
3. **Assistente do dono por WhatsApp e voz.** Responde "quanto vendi hoje?" e "o que está acabando?", manda o resumo diário e as aprovações por PIN. O Omie e a Conta Azul fazem só a parte de gestão financeira. Nenhum concorrente une isso ao caixa offline.
4. **Detecção de fraude no caixa.** Procura padrões suspeitos: cancelamento logo após o pagamento, desconto recorrente sempre do mesmo operador, gaveta aberta sem venda, quebras repetidas. Gera um score por operador e manda push para o app iOS.
5. **Fechamento de caixa por foto.** O operador fotografa as notas e moedas, a visão computacional conta e o sistema compara com o fechamento cego. Também fotografa o comprovante da maquininha.
6. **Cashback automático pelo CPF.** O saldo fica no crédito na loja que já existe, sem cartão e sem app. O cliente recebe aviso pelo WhatsApp e as regras rodam offline.
7. **Pedido por link que cai no terminal como venda suspensa.** O pagamento é por Pix e a retirada é na loja. Reaproveita a suspensão de venda que já existe.
8. **Quiosque de autoatendimento no celular.** O cliente escaneia com o próprio celular ou num tablet e paga Pix na hora. É para mercadinho com 1 ou 2 caixas: segundo a APAS, só 8% das lojas com até 5 caixas usam autoatendimento.
9. **Etiqueta eletrônica de baixo custo.** Usa tela e-ink ou uma "gôndola digital" em tablet, sincronizada com as promoções. Hoje só redes grandes têm.
10. **Cobrança inteligente do fiado.** Calcula um score de risco por cliente, sugere o limite e envia lembrete por Pix Cobrança no WhatsApp.
11. **Conector MCP e assistente de IA no painel.** O Bling já lançou. Para o PDV2, seria um diferencial frente aos PDVs ligados a maquininha.
12. **Validade inteligente.** Sugere desconto automático para produtos perto do vencimento, como promoção temporária no motor que já existe.

### Passos concretos
1. Fechar o fiscal e o pagamento: NFC-e, Pix dinâmico e TEF.
2. Fazer o importador de planilha e os vários códigos de barras: destravam a migração do lojista.
3. Fazer lote e validade e o cashback sobre o crédito na loja.
4. Lançar o diferencial principal: assistente por WhatsApp, sugestão de compra e detecção de fraude, como recurso de plano superior (`hasFeature`).
5. Depois: condicional para moda, pedido por link e quiosque.

## Itens
- [high/G] **NFC-e real com contingência offline**: Bling, Conta Azul, Tiny, MarketUP e PagVendas emitem NFC-e. O PDV2 ainda não emite nem tem contingência (STATUS-V2 linha 25). Sem isso não dá para vender a mercadinho nem a loja de roupas.
- [high/G] **Pix dinâmico e TEF/Smart POS integrados**: Bling tem Pix por QR e Smart POS; Omie tem TEF homologado; Stone tem Connect 2.0; PagBank e Ton aceitam Pix na maquininha. O PDV2 só registra o pagamento declarado pelo operador (STATUS-V2 linha 26). Integrar evita erro de valor e permite conciliação.
- [high/M] **Importador de planilha (produtos, clientes, saldos)**: Tiny e Bling importam em massa. É o que destrava a troca de sistema; está em 'Próximos' no ROADMAP (linha 27).
- [high/P] **Vários códigos de barras e fator de fardo**: Padrão em ERP de mercado (o Odoo aceita vários códigos por produto). No PDV2 o item A9/4.6 continua pendente: hoje há um código por produto.
- [high/G] **Lote e validade com alerta**: Exigido em perecíveis e mercearia (ROADMAP 4.7, STATUS-V2 linha 37). Reduz perdas.
- [medium/M] **Fidelidade e cashback**: Shopify PDV e vários ERPs de varejo têm pontos. O PDV2 não tem nada no código ainda; pode reaproveitar o CustomerCredit.
- [medium/M] **DRE, gráficos e exportação PDF/XLSX**: Conta Azul e Omie vendem pelo financeiro. O ROADMAP (linha 16) registra que DRE, gráficos e PDF faltam.
- [medium/M] **Condicional (mala) para moda**: Una ERP e Vestuar têm condicional com acerto e estoque reservado. É essencial em loja de roupas; não existe no PDV2.
- [medium/M] **Catálogo online e pedido por WhatsApp**: O Kyte oferece no plano grátis, com link de pagamento; PagVendas tem catálogo. No PDV2 o pedido pode cair como venda suspensa.
- [medium/G] **Conciliação bancária e de recebíveis de cartão**: Omie, Conta Azul e Stone fazem. Está na Onda 5, ainda não entregue.
- [low/G] **Integração com e-commerce e marketplaces**: Tiny tem mais de 40 conectores (segundo agregador); Conta Azul tem mais de 10. Pesa mais para moda e pouco para mercadinho.
- [medium/P] **Etiqueta de gôndola com R$/kg em lote**: Una ERP e Shopify geram etiquetas. O PDV2 já imprime código de barras (views_labels.py); falta a gôndola com unidade de medida, puxada pelas mudanças de preço.
- [high/M] **INOVAÇÃO: sugestão de compra e previsão de ruptura com IA**: PDVs pequenos param no alerta de estoque mínimo (Conta Azul, Tiny). Usando giro, sazonalidade e prazo do fornecedor, o PDV2 gera o pedido pronto a partir de StockBalance e do histórico.
- [high/M] **INOVAÇÃO: assistente do dono por WhatsApp e voz**: Omie e Conta Azul têm WhatsApp para gestão e documentos, mas nenhum liga isso ao caixa em tempo real. Responde vendas do dia, quebras e rupturas, e envia aprovações e resumo diário.
- [high/M] **INOVAÇÃO: detecção de fraude no caixa**: Nenhum PDV pequeno oferece. Analisa cancelamentos após o pagamento, descontos recorrentes, gaveta sem venda e quebras repetidas por operador, com alerta push no app iOS. Os dados já existem (operador na venda, CashSession).
- [medium/M] **INOVAÇÃO: precificação assistida**: Exclusiva de grandes redes (IA de preço em supermercados). Usa custo real do XML, margem-alvo e elasticidade, e avisa quando uma promoção deixa a margem negativa.
- [medium/G] **INOVAÇÃO: fechamento de caixa por foto**: Inédito no mercado. A visão computacional conta cédulas e lê o comprovante da maquininha, e o resultado é comparado com o fechamento cego que já existe.
- [high/M] **INOVAÇÃO: cashback automático pelo CPF com aviso no WhatsApp**: Sem cartão e sem app; o saldo fica no crédito na loja e as regras rodam offline no terminal. Concorrentes pequenos não têm.
- [medium/G] **INOVAÇÃO: autoatendimento pelo celular ou tablet**: Só 8% das lojas com até 5 caixas usam autoatendimento (APAS), e o self-checkout é coisa de TOTVS/Linx em redes grandes. O cliente escaneia e paga Pix; reduz fila em horário de pico.
- [high/M] **INOVAÇÃO: cobrança inteligente do fiado**: Score de risco por cliente, sugestão de limite e lembrete com Pix Cobrança pelo WhatsApp. Aproveita o fiado que já existe; nenhum concorrente pequeno faz.
- [medium/M] **INOVAÇÃO: conector MCP e assistente de IA no painel**: Só o Bling (MCP verificado em out/2026) e a Omie (conector com ChatGPT, Claude e Grok) têm algo parecido. Seria um diferencial frente aos PDVs de maquininha.
- [low/G] **INOVAÇÃO: etiqueta eletrônica de baixo custo**: Hoje só grandes redes usam (Grupo Mateus, Petz). Um tablet ou tela e-ink sincronizado com as promoções elimina divergência de preço entre gôndola e caixa.
- [medium/P] **INOVAÇÃO: desconto automático perto da validade**: Combina lote e validade com o motor de promoções offline para gerar remarcação automática e reduzir perdas. Concorrentes pequenos não têm.

Fontes:
https://blog.bling.com.br/novo-frente-de-caixa-do-bling/
https://blog.bling.com.br/assistente-de-ia/
https://blog.bling.com.br/?p=21609
https://docs.kyteapp.com/en/articles/9504323-activating-online-ordering-in-the-catalog
https://www.kyteapp.com/kira-ai
https://contaazul.com/funcionalidades/conta-azul-ia/
https://contaazul.com/para-negocios/varejo/
https://www.omie.com.br/blog/omie-e-o-primeiro-sistema-de-gestao-do-brasil-e-do-mundo-que-pode-ser-utilizado-via-whatsapp/
https://jornalcontabil.com.br/noticia/omie-integra-erp-a-chatgpt-claude-e-grok-para-automatizar-rotinas-de-pmes/
https://www.shopify.com/br/blog/sistema-de-pdv-para-pequenas-empresas
https://www.shopify.com/br/blog/melhores-sistemas-pdv-em-nuvem
https://www.idinheiro.com.br/negocios/melhores-sistemas-pdv/
https://www.calculadoradetaxas.com.br/marcas/stone-vs-ton
https://itforum.com.br/?p=9618518
https://www.band.com.br/noticias/jornal-da-band/ultimas/autoatendimento-avanca-em-mercados-202602252022
https://www.unasistemas.com.br/una-erp-para-loja-de-roupas/
https://consumidormoderno.com.br/etiquetas-eletronicas-gondola-comunicacao/
docs/ROADMAP-COMPLETUDE.md
docs/STATUS-V2.md
cloud/core/models.py:149
cloud/core/views_labels.py:31
web/assets/app.js:610