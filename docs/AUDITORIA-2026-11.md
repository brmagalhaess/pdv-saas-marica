# PDV2 · CrownTech — Auditoria e plano de novembro/2026

16/11/2026. Auditoria feita por cerca de 250 agentes em três rodadas: leitura de todo o código por subsistema, verificação adversarial de cada achado, avaliação de layout e pesquisa de mercado e de integrações. Nada foi alterado no código.

Os detalhes estão em [docs/auditoria-2026-11/](auditoria-2026-11/):

| Arquivo | Conteúdo |
|---|---|
| [achados-codigo.md](auditoria-2026-11/achados-codigo.md) | 223 achados com arquivo, linha, cenário de falha e correção |
| [layout-ux-planos.md](auditoria-2026-11/layout-ux-planos.md) | 117 problemas de layout e as propostas de redesenho, planos e permissões |
| [pesquisa-concorrentes-e-inovacoes.md](auditoria-2026-11/pesquisa-concorrentes-e-inovacoes.md) | o que os concorrentes têm e 12 ideias inéditas |
| [integracao-asaas.md](auditoria-2026-11/integracao-asaas.md) · [integracao-stripe.md](auditoria-2026-11/integracao-stripe.md) · [integracao-nfce.md](auditoria-2026-11/integracao-nfce.md) · [outras-integracoes.md](auditoria-2026-11/outras-integracoes.md) | passo a passo de cada integração |
| [app-gestao.md](auditoria-2026-11/app-gestao.md) | plano para finalizar o app e publicar na Apple e no Google |

## 1. Números

| | Crítico | Alto | Médio | Baixo |
|---|---|---|---|---|
| Achados de código confirmados | 6 | 54 | 118 | 45 (não verificados) |

- 2 achados foram refutados na verificação e descartados.
- Os 6 críticos são 4 defeitos distintos (dois aparecem em mais de uma área).
- Layout: 117 problemas, 31 deles graves.

## 2. Os defeitos que precisam ser corrigidos antes de qualquer cliente

### 2.1 A fila do caixa pode travar para sempre (crítico)

O terminal envia as vendas em lotes. Se a nuvem recusa **um** evento, o lote inteiro falha e o terminal reenvia os mesmos eventos para sempre: não existe quarentena. Nada mais daquele caixa chega à nuvem.

Situações comuns que disparam isso hoje:

- o dono reduz o limite de desconto ou troca a unidade de um produto (UN/KG) com vendas offline ainda na fila;
- o operador cancela uma venda em dinheiro depois de uma sangria: o caixa esperado fica negativo e a nuvem recusa o fechamento;
- uma venda é cancelada no painel e depois estornada ou devolvida no caixa;
- uma venda com NFC-e autorizada é estornada no caixa.

A regra a adotar: **o que aconteceu no balcão é fato**. A nuvem aceita o evento, registra a divergência como conflito para o administrador e segue. Um evento que não pode ser aceito de jeito nenhum vai para uma quarentena visível, sem bloquear os seguintes.

### 2.2 O backup nunca acusa falha (crítico)

`infra/backup.sh` grava "último sucesso" mesmo quando o `pg_dump` ou o envio ao Restic falham. O painel mostraria backup em dia sem haver backup.

### 2.3 O caixa no Windows não conclui nenhuma venda (crítico)

O comprovante usa o fuso `America/Sao_Paulo`, que no Windows exige o pacote `tzdata`, ausente. A exceção acontece dentro da transação da venda.

### 2.4 `/control/` aceita só senha (crítico)

O Django admin está publicado em `/control/` sem a verificação em duas etapas da plataforma e sem limite de tentativas. Quem tiver a senha do superusuário edita assinaturas, vínculos e dados de todas as empresas, sem auditoria.

### 2.5 Outros defeitos graves, por área

**Cobrança (Asaas)**
- Troca de plano, suspensão ou cancelamento feitos no admin não chegam ao Asaas e são desfeitos no pagamento seguinte. Não existe upgrade nem downgrade para quem já paga.
- Uma solicitação de assinatura que falha deixa a empresa sem poder tentar de novo.
- Estorno ou chargeback não revoga o acesso até o fim do período pago.

**Fiscal (Focus NFe)**
- O conector rejeitaria a resposta real de uma nota autorizada: ela ficaria "pendente" para sempre.
- Nota rejeitada ou que falhou no envio nunca pode ser corrigida e reenviada.
- Devolução ignora a NFC-e, e filiais emitiriam com o CNPJ da matriz.

**Segurança e contas**
- Não existe troca nem recuperação de senha para usuários das empresas; nenhum e-mail é enviado pelo sistema.
- O operador de caixa consegue aumentar o limite de fiado de qualquer cliente.
- O bloqueio por tentativas do PIN de gerente é apagado a cada 30 segundos.
- O PIN do gerente fica guardado em claro na memória da página do caixa.
- O `DELETE` em uma conta a receber cancela fiado gerado por venda, burlando a trava do `PATCH`.

**Painel**
- A promoção "Leve X pague Y" não pode ser criada pela tela.
- Listas com mais de 100 cadastros ficam inalcançáveis, e os seletores mostram só 50 itens, sem busca.
- Um cadastro desativado não pode ser reativado.
- Não é possível limpar um campo na edição.
- O cadastro de produto não aceita o código de barras de fábrica nem o custo.

**Caixa**
- Sangria e suprimento existem no servidor, mas não têm botão na tela.
- O indicador mostra "Offline" com o caixa conectado.
- A balança sobrescreve a quantidade de itens vendidos por unidade.
- Em monitor de 1024 px, "Sair" e "Bloquear" ficam fora da tela.

**Relatórios**
- O lucro usa o custo atual do produto para vendas antigas, e a curva ABC ignora devoluções.

**Operação**
- Não há log estruturado nem alerta de erro: uma falha em produção não deixa rastro.
- `certifi` está fixado em versão com alerta de segurança.
- Os defeitos de sincronização não têm teste de regressão.

## 3. Layout: o que muda

### 3.1 Caixa — "Caixa Rápido"

Hoje: um único atalho (F2), total e troco pequenos, 4 cliques com o mouse para finalizar, sem modo teclado.

Proposta, em 4 faixas fixas e sem rolagem:

1. **Barra de status** (topo): loja, caixa, operador, online/offline, fila, impressora, balança, relógio. A barra inteira muda de cor: verde, âmbar ou vermelho.
2. **Itens** (60%): um campo único que aceita código, nome ou `3*código`; bloco "último item" em letra grande; lista numerada com rolagem automática.
3. **Valor** (40%): **TOTAL em 72–96 px**, economia das promoções, pagamento na própria tela (`1` Dinheiro, `2` Pix, `3` Débito, `4` Crédito, `5` Fiado), valores rápidos e **TROCO em 56 px**.
4. **Régua de teclas** (rodapé, sempre visível e clicável):

```
F1 Ajuda · F2 Buscar · F3 Quantidade · F4 Cliente/CPF · F5 Desconto · F6 Em espera
F7 Sangria/Suprimento · F8 Cancelar item · F9 Reimprimir · F10 Pagar · F11 Foco · F12 Fechar caixa
```

A régua muda conforme o contexto, esmaece o que não está disponível e marca com cadeado o que pede PIN.

Fluxo alvo: bipar os itens → `F10` → `1` → valor → `Enter`.

Diferenciais frente a SysPDV, Linx, Bling e MarketUP:
- paleta de comandos (`Ctrl+K`: digitar "sangria", "cpf", "reimprimir");
- modo treinamento, em que a régua destaca a próxima tecla;
- segundo display para o cliente, com itens, total e QR do Pix;
- modos Scanner (mercado), Toque (moda) e Foco (tela cheia);
- temas claro, escuro e alto contraste.

### 3.2 Painel da empresa

Hoje: 23 itens de menu sem agrupamento coerente, dashboard sem gráfico, sem busca global.

Proposta:
- **6 áreas** em vez de 23 itens: Início · Vendas e caixa · Produtos e estoque · Clientes e financeiro · Relatórios · Configurações.
- **Barra superior** com seletor de loja e período, busca (`Ctrl+K`), botão "+ Novo" e sino de pendências.
- **Dashboard** com indicadores comparados ao período anterior, gráfico de vendas por dia, mapa de calor por hora, formas de pagamento, top 5 produtos e o bloco "Precisa de atenção".
- **Tabela única** com ordenação, totais, seleção em massa e versão em cartões no celular.
- **Painel lateral** para criar e editar, com máscaras de CNPJ, CEP e telefone.
- **Primeiros passos**: checklist de 6 etapas para a empresa nova.
- Tema claro e escuro.

### 3.3 Admin do SaaS — "Console CrownTech"

Hoje: não há indicadores de dono de SaaS (só o MRR do momento), não dá para entrar como o cliente, não há ciclo de vida automático.

Proposta:
- Visual próprio (grafite com faixa dourada e selo **ADMIN SAAS**), para nunca confundir com o painel do cliente.
- **Início**: MRR, clientes pagantes, churn e conversão de teste; gráfico de MRR em 12 meses; lista "Precisa de você hoje".
- **Ficha do cliente** com saúde de 0 a 100, notas internas, histórico e o botão **Entrar como cliente** (motivo obrigatório, 30 minutos, somente leitura, auditado dos dois lados).
- **Régua de cobrança** configurável: avisos antes do fim do teste; no atraso, aviso → somente leitura → terminais bloqueados → cancelado.
- **Planos e recursos** em matriz com chaves, sem JSON digitado.
- Cupons, comunicados em massa, chamados, canal de versão por empresa.

### 3.4 Marca CrownTech

A palavra "CrownTech" não aparece em nenhum arquivo do produto, e a tela de login diz que "PDV2 é nome provisório".

Princípio: **PDV2 é o produto, CrownTech é o selo**. Nunca na área de venda; sempre nas bordas e no que sai da loja.

| Onde | O que entra |
|---|---|
| Cupom impresso | última linha, pequena: `PDV2 · crowntech.com.br` (ocultável em plano white-label) |
| Caixa (login, bloqueio, tela ociosa) | rodapé "PDV2 · um produto CrownTech · Suporte" |
| Login e painel | "PDV2 é um produto CrownTech © 2026" e rodapé da barra lateral |
| Landing | faixa "Quem faz", rodapé com CNPJ e contato, termos e privacidade |
| Link compartilhado | imagem e título com a marca |
| E-mails e boletos | remetente "PDV2 por CrownTech" |
| App | tela Sobre e identificador `br.com.crowntech...` |

### 3.5 Cupom e nota fiscal

O comprovante atual não segue o leiaute do DANFE NFC-e. O novo modelo, para 58 e 80 mm, tem: cabeçalho do emitente, título "Documento Auxiliar da Nota Fiscal de Consumidor Eletrônica", itens em duas linhas, totais, pagamento e troco, área de mensagem fiscal (contingência ou homologação), consulta pela chave, consumidor, número/série/protocolo, QR Code, tributos da Lei 12.741 e a linha de marca. O comprovante não fiscal e os vouchers usam o mesmo esqueleto. O desenho completo está em [layout-ux-planos.md](auditoria-2026-11/layout-ux-planos.md).

## 4. Funcionalidades

### 4.1 O que os concorrentes têm e falta no PDV2

Em ordem de prioridade para mercadinho, mercearia e loja de roupa:

1. NFC-e em produção, com contingência offline.
2. Pix dinâmico e maquininha integrados (o valor não é digitado duas vezes).
3. Importador de planilha: sem ele o lojista não troca de sistema.
4. Vários códigos de barras por produto (fardo e unidade).
5. Lote e validade com alerta de vencimento.
6. Fidelidade e cashback.
7. DRE, gráficos e exportação em PDF/XLSX.
8. Condicional (mala de peças) para moda.
9. Catálogo online com pedido pelo WhatsApp.
10. Conciliação bancária e de recebíveis de cartão.
11. Pedido de compra com recebimento parcial.
12. Etiqueta de gôndola com R$/kg.
13. Comprovante e cobrança de fiado pelo WhatsApp.

### 4.2 O que quase ninguém tem

1. **Assistente do dono pelo WhatsApp**: "quanto vendi hoje?", resumo diário, aprovações.
2. **Detecção de fraude no caixa**: cancelamento logo após o pagamento, desconto recorrente do mesmo operador, quebras repetidas; alerta no app.
3. **Sugestão de compra com previsão de ruptura**, gerando o pedido pronto.
4. **Cashback automático pelo CPF**, sem cartão nem app, funcionando offline.
5. **Cobrança inteligente do fiado**: score por cliente, sugestão de limite, lembrete com Pix.
6. **Precificação assistida** a partir do custo real da nota de compra.
7. **Fechamento de caixa por foto** das cédulas.
8. **Autoatendimento pelo celular do cliente**, com Pix.
9. **Desconto automático perto da validade.**
10. **Assistente de IA no painel** e conector para IAs externas.

## 5. Integrações

### 5.1 Asaas — boleto por e-mail

O conector já cria cliente e assinatura e já concilia por webhook. Falta:

1. Guardar no sistema o link do boleto, a linha digitável e o Pix copia-e-cola de cada cobrança.
2. Enviar o e-mail do boleto pelo próprio PDV2 quando a cobrança é criada (o aviso do Asaas pode não sair para cobranças de assinatura).
3. Vencimento em hoje + N dias (hoje o boleto vence no mesmo dia).
4. Tela "Minhas faturas" para a empresa.
5. Cancelamento, troca de plano e nova tentativa após falha, sempre refletidos no Asaas.
6. Segunda etapa: o lojista cobrar o fiado dos clientes dele por boleto ou Pix, com a conta Asaas dele.

### 5.2 Stripe — cartão de crédito

Entra como segundo provedor, ao lado do Asaas:

1. Camada única de provedor de cobrança (hoje o código assume Asaas).
2. Cada plano ligado ao preço correspondente no Stripe.
3. Pagamento pelo Stripe Checkout em modo assinatura, em reais.
4. Webhook com verificação de assinatura; o acesso só é liberado com a fatura paga.
5. Portal do cliente para trocar o cartão e cancelar.
6. Upgrade e downgrade pelo sistema, nunca só no painel do Stripe.

### 5.3 NFC-e — Focus NFe

Hoje só existe o esqueleto do conector. Falta:

1. Perfil fiscal da empresa: certificado A1, CSC, regime, inscrição estadual e série por loja.
2. Tributação estruturada do produto: NCM, CEST, CFOP, CSOSN/CST, origem e os campos novos de IBS/CBS.
3. Montagem do payload no servidor e emissão automática a cada venda sincronizada.
4. Retorno da autorização ao caixa, com impressão do DANFE e do QR Code.
5. Cancelamento, inutilização e reenvio de nota rejeitada.
6. Contingência offline no terminal (a parte mais trabalhosa).
7. NF-e modelo 55 para devolução e transferência entre lojas.

### 5.4 Outras, em ordem recomendada

1. E-mail transacional (Resend ou Amazon SES) — pré-requisito de senha, boleto e avisos.
2. Pix dinâmico no caixa com confirmação automática.
3. Consulta de CNPJ e CEP no cadastro.
4. WhatsApp (comprovante e cobrança de fiado).
5. Maquininha integrada (Mercado Pago Point primeiro; Stone e PagBank depois).
6. Cadastro de produto pelo código de barras (Cosmos).
7. Pacote mensal para o contador.
8. Balanças Toledo e Filizola pelo protocolo próprio.
9. Depois: TEF tradicional, e-commerce e marketplaces, conciliação bancária.

## 6. Planos e permissões

**Permissões.** Hoje são 7 papéis fixos, espalhados no código, com incoerências (ex.: o caixa altera limite de fiado). Proposta: um catálogo de cerca de 40 permissões por ação e **perfis editáveis** por empresa, em que cada ação fica como *Permitido*, *Com PIN* ou *Negado*, com limites em R$ e %. Os 7 papéis atuais viram perfis prontos. O terminal recebe as permissões junto com a licença e continua funcionando offline.

**Planos.** Proposta de grade:

| | Essencial R$ 149 | Gestão R$ 229 | Avançado R$ 349 |
|---|---|---|---|
| Lojas / terminais | 1 / 1 | 1 / 2 | 3 / 5 |
| Usuários | 3 | 8 | 25 |
| Produtos | 2.000 | 10.000 | ilimitado |
| Inclui | caixa offline, conferência de caixa, devoluções e trocas, fiado, estoque, promoção simples | + financeiro, comissões, relatórios, grade, nota de compra, inventário, promoções avançadas, perfis editáveis | + transferências, relatórios multiloja, acesso por loja, auditoria completa, suporte prioritário |

Adicionais: terminal extra R$ 39, loja extra R$ 79, 5 usuários R$ 19, emissão de NFC-e R$ 49 por loja. Os preços são sugestão; a decisão é sua.

## 7. App de gestão (Apple e Google)

Estado atual: um protótipo iOS de uma tela, que só funciona no simulador. O acesso está ao contrário do que você definiu: só o dono da loja entra, e o admin do SaaS não consegue fazer login.

Decisões do plano:
- **Tecnologia:** manter o iOS em SwiftUI e escrever o Android nativo em Kotlin. Empacotar o site não serve: a Apple rejeita.
- **Acesso:** o admin do SaaS entra sempre (com verificação em duas etapas) e libera usuários um a um, por empresa e loja, com revogação imediata.
- **Versão 1.0, só leitura:** resumo do dia, vendas, caixas e quebras, estoque baixo, fiado, terminais. Para o admin do SaaS, a aba Plataforma com empresas, liberação de acesso e dispositivos.
- **Gratuito:** sem compra no app, sem preço e sem link de assinatura.

Publicar tem custo para a CrownTech, não para o usuário: Apple Developer Program (US$ 99 por ano) e Google Play (US$ 25, uma vez).

## 8. Produção no Dokploy — `pdv2.crowntech.com.br`

O projeto já tem `compose.production.yaml` e o guia de deploy, e o domínio já resolve para o servidor. Falta:

1. Corrigir os críticos da seção 2 (backup e `/control/` primeiro).
2. Criar o projeto no Dokploy a partir do repositório, com `compose.production.yaml`.
3. Gerar os segredos: chave do Django, senha do banco, par de chaves de licença, chave de criptografia, senha do backup.
4. Configurar o destino externo do backup e testar uma restauração.
5. Configurar o e-mail de envio.
6. Apontar o domínio para o serviço `cloud`, porta 8080, com HTTPS.
7. Criar o primeiro administrador e ativar a verificação em duas etapas.
8. Cadastrar o webhook do Asaas.

## 9. Ordem de execução proposta

| Onda | Entrega | Por quê |
|---|---|---|
| **0 · Estancar** | Fila do caixa que não trava, backup que acusa falha, Windows, `/control/`, limite de fiado, PIN, regressões de teste | sem isso não dá para ter cliente |
| **1 · Cara nova do caixa** | Caixa Rápido com a régua de teclas F, total e troco grandes, sangria e suprimento na tela, marca CrownTech, cupom no leiaute novo | é o que você pediu como mais importante e o que o operador vê |
| **2 · Produção** | Deploy no Dokploy, e-mail, troca e recuperação de senha, logs e alertas | colocar `pdv2.crowntech.com.br` no ar com segurança |
| **3 · Cobrança** | Asaas completo (boleto por e-mail, faturas, troca de plano), Stripe com cartão, régua de cobrança | começar a receber |
| **4 · Painel e admin** | Navegação em 6 áreas, dashboard com gráficos, tabela e busca novas, Console CrownTech, perfis de permissão e planos novos | gestão fácil para o cliente e para você |
| **5 · App** | Acesso liberado pelo admin, iOS e Android, envio às lojas | depende do servidor em produção |
| **6 · Fiscal** | NFC-e pela Focus de ponta a ponta, DANFE, cancelamento, contingência | exige certificado e homologação com contador |
| **7 · Diferenciais** | Pix dinâmico, importador de planilha, vários códigos de barras, WhatsApp, cashback, detecção de fraude, sugestão de compra | o que vende contra os concorrentes |

As ondas 1 e 2 podem andar juntas. A onda 6 pode começar antes se o certificado estiver disponível.

## 10. O que só você pode providenciar

| Para | O que é preciso |
|---|---|
| Dokploy | acesso ao painel ou a criação do projeto; destino do backup (S3, Backblaze ou similar) |
| E-mail | conta no Resend ou Amazon SES e os registros DNS no domínio |
| Asaas | conta, chave de API (testes e produção) e o token do webhook |
| Stripe | conta ativada no Brasil, chaves e os produtos/preços criados |
| NFC-e | conta na Focus NFe, certificado A1 da empresa emissora, CSC da SEFAZ e a validação do contador |
| Apple | conta Apple Developer como organização (pede D-U-N-S) |
| Google | conta Play Console como organização |
| Marca | logotipo da CrownTech, CNPJ, telefone e e-mail de suporte, texto de termos e privacidade |
| GitHub | regularizar a cobrança do Actions, que impede os testes automáticos de rodar |

## 11. Perguntas em aberto

1. **Nome do produto:** continua "PDV2" com o selo "por CrownTech", ou muda para "CrownTech PDV"?
2. **Quem libera acesso ao app:** só você (superusuário) ou também a equipe de suporte?
3. **Empresa inadimplente:** o usuário liberado continua vendo os dados no app?
4. **Preços e limites dos planos:** a grade da seção 6 serve?
5. **Remover a marca do cupom** entra como benefício de um plano mais caro?
