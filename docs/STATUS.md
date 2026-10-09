# Estado real do produto

Este repositório é um **incremento de desenvolvimento**. A landing apresenta o produto planejado e os preços propostos; ela não significa que já exista um PDV fiscal pronto para clientes.

## Entregue nesta etapa

- Base SaaS com empresas, loja inicial, contas e sessões.
- Planos configurados para a proposta comercial.
- Painel web de entrada e administração inicial.
- Catálogo e estoque por loja com permissões e auditoria.
- Caixa local experimental com venda transacional, baixa de estoque e outbox.
- Recepção idempotente de eventos do terminal no SaaS e lista de vendas recebidas.
- Backup consistente do banco de desenvolvimento.
- Testes básicos de isolamento entre empresas.

O código do catálogo e do caixa local ainda exige piloto supervisionado. Vendas sincronizadas geram uma baixa única no saldo central e divergências negativas entram em uma fila operacional de conciliação no painel da empresa; o terminal também envia heartbeat e mantém a fila local. Emissão fiscal, impressoras reais, cobrança, segurança e operação de produção seguem pendentes.

## Portões para uma loja piloto

1. Catálogo de mercearia com código de barras, produtos por unidade e peso, tributos e importação confiável.
2. PDV Windows instalado na máquina alvo com leitor, impressora térmica, etiquetas e, se aplicável, balança testados fisicamente.
3. Venda local, caixa, pagamentos e recuperação de falhas com reconciliação comprovada.
4. NFC-e RJ em homologação e produção, com especialista fiscal, tratamento de contingência e XML.
5. Backup externo criptografado e restauração ensaiada.
6. Segurança de acesso, HTTPS, monitoramento, suporte e procedimento de incidente.
7. Três pilotos supervisionados antes de anunciar operação comercial regular.

## Portões para SaaS comercial

Além do piloto: cobrança e webhooks idempotentes, licenças e dispositivos, atualização assinada e gradual, aplicativo Android/iOS publicado, console de suporte, infraestrutura escalável e contratos/privacidade adequados.

## Decisões já tomadas

- Primeiro segmento: mercados e mercearias em Maricá.
- “bpk” = backup.
- Cliente inicial: uma loja, um terminal; servidor local opcional no futuro.
- Senha: mínimo de 8 caracteres com maiúscula, minúscula, número e símbolo. A senha sugerida pelo proprietário satisfaz esse padrão, mas não é armazenada no código.
- Nome provisório da identidade visual: PDV2. A busca inicial não substitui consulta de marca ou de disponibilidade de domínio.

## Próximo incremento técnico recomendado

Automatizar o fluxo de catálogo do SaaS para o caixa, mostrar pendências e divergências de sincronização na interface operacional e testar com equipamentos físicos. Só depois ligar o fluxo fiscal e a impressão a modelos validados em bancada.
