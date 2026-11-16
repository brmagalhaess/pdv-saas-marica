# Estado real do PDV2 V2

## Novembro/2026: o que mudou

16/11/2026. A [auditoria de novembro/2026](AUDITORIA-2026-11.md) encontrou quatro defeitos críticos. Os quatro foram corrigidos no código, com teste de regressão. Nada disso foi para produção ainda: o ambiente continua local.

| Defeito crítico | O que era | Como ficou |
|---|---|---|
| A fila do caixa podia travar para sempre | Um evento recusado pela nuvem derrubava o lote inteiro e o caixa reenviava os mesmos eventos sem fim | A nuvem aceita evento por evento. O que aconteceu no balcão é fato: divergência vira conflito para o administrador, e o que não pode ser aplicado vai para uma quarentena visível, sem bloquear os seguintes. Contrato em [SYNC-VENDAS.md](SYNC-VENDAS.md) |
| O backup nunca acusava falha | `infra/backup.sh` gravava "último sucesso" mesmo com falha no `pg_dump` ou no envio | O script detecta a falha e não registra sucesso |
| O caixa no Windows não concluía venda | O comprovante dependia de um fuso horário ausente no Windows, dentro da transação da venda | O fuso do comprovante passou a ser seguro no Windows |
| `/control/` aceitava só senha | O admin do Django ficava publicado sem a verificação em duas etapas | O admin fica desligado em produção, a não ser com `PDV_ENABLE_DJANGO_ADMIN=1`; ligado, só abre para superusuário com a MFA da plataforma concluída |

Também entregue nesta rodada (Onda 0 e Onda 1 do plano da auditoria):

- **Caixa Rápido**: nova frente de caixa com régua de teclas F, pagamento na tela, sangria e suprimento com botão, temas claro, escuro e alto contraste, e caixa de demonstração em `scripts/demo_terminal.py`. Ver [TERMINAL-V2.md](TERMINAL-V2.md).
- **Terminal resiliente**: evento recusado, catálogo inválido e falha do sincronizador não param o caixa.
- **Regras da nuvem**: o operador de caixa não altera mais o limite de fiado; código de barras de fábrica no cadastro; custo da venda e relatórios considerando devoluções.
- **Painel**: paginação além de 100 cadastros, reativação de inativos, busca nos seletores, listas em ordem de nome.
- **Logs de produção**: tudo em stdout com identificador do pedido; o worker registra o job, o tipo e a empresa de cada tarefa que falha.
- **Marca CrownTech** na landing, no login, na tela offline, no manifesto e no cupom (leiaute do DANFE NFC-e, ainda **não fiscal**).
- **Nuvem**: `/robots.txt` e `/sitemap.xml`; `auth/me` informa a versão em execução; a câmera é liberada para a contagem de inventário pelo celular (`Permissions-Policy: camera=(self)`) e o catálogo exibe fotos de produto por URL externa em HTTPS.

Suítes executadas em 16/11/2026, todas sem falha: 161 testes da nuvem (`core` + `operations`), 124 do terminal, 1 contrato terminal-nuvem, 22 de infraestrutura, 11 do painel (`node --test`), e `makemigrations --check` sem migração pendente.

O que **não** mudou: os bloqueios das seções abaixo (fiscal, pagamento, hardware, implantação) continuam valendo. Os defeitos graves de cobrança (Asaas) e fiscal (Focus NFe) listados na auditoria ainda estão abertos, assim como troca e recuperação de senha por e-mail. Próximos passos em [ROADMAP-COMPLETUDE.md](ROADMAP-COMPLETUDE.md).

## Estado em 29/09/2026

29/09/2026. Ambiente: Docker Desktop local. Nenhum domínio registrado, VPS alterada ou pagamento/documento fiscal real processado.

## Implementado

Três superfícies: SaaS, empresa e terminal. PostgreSQL, sessões/CSRF, papéis, limites por plano e consultas por empresa. Licenças assinadas vinculam dispositivo e resumo criptográfico dos operadores. SQLite mantém catálogo, caixa e fila persistente.

O terminal executa venda, desconto autorizado, pagamento dividido, troco, caixa, sangria/suprimento, suspensão, cancelamento, consulta e spool. O painel administra catálogo/variações, lojas, contatos, estoque/entradas, financeiro, assinaturas, equipe, terminais e auditoria.

Asaas e Focus NFe têm conectores executáveis, fila persistente e credenciais criptografadas. Seus testes usam respostas simuladas. XML só é arquivado se corresponde à chave e ao protocolo. Solicitação de cobrança/nota não significa pagamento/autorização concluídos.

## Evidências

- Contrato terminal→Django: pareamento, PIN, quantidade fracionária, perda de ACK, reenvio idempotente e estorno.
- Testes de assinatura, adulteração, PIN, CSRF, host/origem local, spool, limites e isolamento.
- Webhook autenticado/deduplicado, conciliação canônica e bloqueio de repetição de cobranças incertas.
- Backup Restic restaurado em banco separado; relatório `.runtime/backup-restore-report.json`.
- Teste HTTP com interrupção do container cloud: `scripts/smoke_local.py --offline`. O relatório `.runtime/http-smoke-report.json` só é gravado ao completar o fluxo.

Essas evidências cobrem as condições dos testes, não homologação com hardware, bancos ou SEFAZ.

## Necessário antes de atender uma loja real

1. **Fiscal:** credenciar emitente, configurar certificado/CSC, validar cadastro/tributação com responsável fiscal e homologar emissão/cancelamento/XML. O payload fiscal ainda precisa ser fornecido à API. Não há motor tributário abrangente, emissão automática para cada venda nem contingência fiscal local.
2. **Pagamento:** configurar Asaas/webhook real para assinaturas. O caixa registra pagamentos declarados pelo operador; não integra TEF, adquirentes, Pix dinâmico ou confirmação bancária.
3. **Hardware:** validar encoding, corte/gaveta, etiqueta/gap, drivers e conexão nos equipamentos alvo. Protocolos implementados não garantem compatibilidade com todos os modelos.
4. **Implantação:** domínio/HTTPS, VPS, backup externo, SMTP, monitoramento, incidentes e restauração no ambiente final. O backup local não cobre perda do computador.
5. **Segurança operacional:** teste de carga/concorrência, revisão independente, rotação/guarda de chaves e política de acesso. MFA TOTP da plataforma está implementada e fica obrigatória em produção. Não publicar DEBUG local na internet.
6. **Mobilidade:** validar PWA em aparelhos reais. Apps nativos e publicação nas lojas ainda não existem; instalador Windows/Python ainda exige teste em máquina real.
7. **Operação comercial:** migração dos dados antigos, treinamento, contrato, política de suporte e pilotos acompanhados.

## Segmentos e módulos restantes

O núcleo cobre varejo de produtos por unidade/quantidade fracionária e variações de catálogo: base para mercados, mercearias e varejo geral, sujeita às exigências de cada operação.

Ainda faltam mesas/comandas/cozinha/delivery; agenda/ordens de serviço; farmácia/SNGPC; lotes/validade/rastreabilidade; protocolo específico de cada fabricante de balança e teste físico do piloto; escrituração/importação completa de compras; promoções/fidelidade; conciliação bancária/TEF; motor fiscal abrangendo regimes/UFs. Não anunciar cobertura integral desses segmentos.

## Limites offline

O painel web (`8000`) não é o caixa offline: ele depende da API em nuvem. A operação offline deve ser feita no serviço local do terminal (`8010`), já pareado e com catálogo/operadores baixados. O terminal grava vendas e caixa em SQLite, mantém a fila pendente, envia heartbeat periódico e sincroniza quando a conexão retorna. Divergências de estoque aparecem no painel da empresa em **Administração → Conflitos offline**.

- Licença normal até 72 horas, podendo ser encurtada por vencimentos; o terminal não adiciona prazo após `expires_at`.
- Expiração preserva leitura/exportação e envio de registros pendentes; novas vendas exigem renovação.
- Caixas desconectados podem vender o mesmo saldo. A nuvem recebe o histórico, registra um conflito por evento quando o saldo central fica negativo e mantém a decisão auditável; estoque global instantâneo exige comunicação.
- Revogação de pessoa/dispositivo depende de reconexão ou vencimento da licença já emitida.
- ESC/POS imprime comprovante **não fiscal**. Persistir venda offline não equivale a emitir NFC-e em contingência. Prazos fiscais são independentes dos prazos da licença.

## Lançamento

A V2 é uma base operacional para validação e pilotos supervisionados. Liberação comercial de produção depende das pendências acima. Containers e testes automatizados isoladamente não encerram essa validação.

## Fechamento desta validação local

Após a auditoria de 09/10/2026, a suíte reconstruída com as migrações atuais passou por:

- 34 testes cloud (`core` + `operations`), incluindo heartbeat e conflito offline resolvível;
- 37 testes do terminal (`test_terminal` + `test_receipts`);
- 1 contrato terminal-cloud em banco SQLite temporário;
- 4 testes compartilhados de licenciamento/releases;
- `makemigrations --check --dry-run`: nenhuma migração pendente.

O `check --deploy` em Compose local continua emitindo cinco avisos esperados porque o perfil local usa HTTP e DEBUG. O check deve ser repetido com o ambiente de produção, domínio e TLS configurados.

- 18 testes Django de core/operations passaram com PostgreSQL em banco de teste separado.
- 14 testes do terminal passaram, incluindo contrato com Django e persistência após reinício.
- 4 testes de licença/versão e 4 testes de conectores/XML passaram: 40 testes V2 no total.
- Roteiro HTTP executado com cloud desligado: venda local persistida, reenvio sem duplicação, reconexão, sincronização, estorno e fechamento.
- Login, painel empresa, catálogo, painel SaaS e terminal inspecionados no navegador. Painel também conferido a 390 px de largura.
- Backup SQLite manual criado. Restauração Restic/PostgreSQL validada em banco temporário, incluindo vendas de teste estornadas.
- Conta/PIN temporários de validação foram desativados e removidos do catálogo de operadores do terminal.
- Compose de produção validado sintaticamente. Django `check --deploy` passou sem erros, com aviso de HSTS preload ainda não habilitado; domínio definitivo não configurado.

Estes resultados não incluem testes físicos de impressoras, emissão real de NFC-e, processamento real de pagamentos ou teste do instalador em Windows.
