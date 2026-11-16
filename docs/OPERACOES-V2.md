# Operação e integrações V2

Todas as rotas abaixo usam prefixo `/api/v1/ops/`. Rotas de usuário exigem sessão e CSRF em alterações. `X-Tenant-ID` seleciona a empresa, sempre validada contra a associação do usuário.

## Empresa e integrações

- `GET/PATCH company`: proprietário/admin altera nome e documento da própria empresa.
- `GET/POST/PATCH integrations/focusnfe`: proprietário/admin configura token, ambiente `homologation` ou `production` e `enabled`.
- `GET/POST/PATCH integrations/asaas`: somente superusuário configura token global, ambiente `sandbox` ou `production` e `enabled`.

Tokens são criptografados com Fernet e não são retornados pelas APIs. A chave de criptografia fica fora do banco/repositório.

## Cobrança

`POST billing/subscribe` aceita `plan_id`, `cycle` MONTHLY/YEARLY, `name`, `document`, `email`, `billing_type` PIX/BOLETO/UNDEFINED e `idempotency_key`. O servidor determina o preço a partir do plano; o cliente não fornece o preço.

A criação vira tarefa e não ativa a assinatura como paga. `POST webhooks/asaas` exige header `asaas-access-token`. O evento é deduplicado e apenas agenda uma consulta ao provedor; o status enviado no webhook não é suficiente para liberar acesso. A conciliação confere assinatura, pagador e valor.

A licença empresarial é criada com a assinatura, inclusive no teste inicial. Um pagamento confirmado e reconciliado muda a origem da licença para `subscription`; somente então a assinatura entra nas métricas de MRR pago/contratado. O superusuário da plataforma pode conceder VIP por período e plano, sem gerar cobrança nem receita fictícia. O limite de caixas continua vindo do plano; cada caixa recebe licença offline assinada após pareamento.

Se uma criação tem resposta incerta, ela fica `needs_review`, sem repetição automática. Administrador financeiro pode conferir a referência externa no Asaas e solicitar `POST jobs/<id>/reconcile` com `subscription_id`. O worker consulta a assinatura e exige referência, pagador, ciclo e valor correspondentes antes de vinculá-la. Isso não gera outra cobrança.

Não existe coleta de número de cartão neste sistema. Mudanças de assinatura já ativa exigem conciliação administrativa; o endpoint bloqueia uma segunda assinatura para evitar cobrança duplicada.

## Fiscal

`POST fiscal/submit`: `sale_id` e `fiscal_payload` Focus NFe revisado para o emitente. Exige venda concluída da empresa, total/emitente/itens/quantidade correspondentes. A referência é estável por venda. Uma nota não é marcada autorizada sem chave de 44 dígitos e protocolo.

O conector consulta o documento pendente. Depois da autorização, agenda download XML exclusivamente da origem configurada do provedor, sem redirecionamento de credenciais. Confere chave, protocolo e status no XML, guarda o conteúdo no PostgreSQL e registra SHA-256; o backup do banco inclui esse arquivo.

- `GET fiscal/documents`: documentos e indicação `xml_archived`.
- `GET fiscal/documents/<id>/xml`: download autenticado, somente da empresa do usuário.
- `POST fiscal/documents/<id>/cancel`: justificativa de 15–255 caracteres. Confirma cancelamento consultando o provedor.

Cancelamento fiscal e estorno comercial são operações separadas. O payload tributário ainda é fornecido à API, não gerado automaticamente pelo terminal. Nenhuma regra genérica substitui cadastro tributário/homologação. Contingência fiscal local não está disponível.

## Tarefas e saúde

`GET jobs` lista tarefas da empresa para owner/admin/manager/finance. Sem `X-Tenant-ID`, lista da plataforma exige equipe billing/ops. `POST jobs/<id>/reconcile` retoma consultas seguras ou agenda consulta fiscal após resposta incerta; não repete uma mutação indiscriminadamente.

`GET health` mostra quantidade de pendentes/revisões e instante do último backup. O webhook retorna sucesso após persistir o evento, não após completar o pagamento.

`GET/POST backups` é restrito a `platform_ops` e superusuário. O GET acompanha
estado, última cópia, snapshots e restauração; o POST aceita `{"action":"run"}`
para solicitar uma cópia imediata ou `{"action":"restore_test","snapshot_id":"...","confirm":true}`
para validar um snapshot em banco temporário isolado. A restauração não altera o
banco ativo.

## Versões

`GET/POST releases`: equipe platform_ops/superusuário administra metadados. Publicação exige `component` terminal/cloud, `channel` stable/pilot, versão semântica de três números, URL HTTPS, SHA-256, tamanho e notas. Versões publicadas são imutáveis; retirada via `POST releases/<id>/withdraw`.

`GET releases/current?component=terminal&channel=stable` retorna manifesto Ed25519. Verifique com a chave pública previamente fixada no pareamento; não confie em uma chave nova recebida junto do pacote. O terminal mostra aviso, sem baixar/executar código automaticamente.

Geração local: `python scripts/package_terminal.py`. Verificação antes de instalar: `python scripts/verify_update.py pacote.zip manifesto.json chave-confiavel.pem`. Hospedagem pública/privada do pacote, acesso de download e assinatura do instalador Windows ainda precisam ser definidos no ambiente final.

O menu **Plataforma → Versões** mostra a versão realmente carregada pela nuvem
(`PDV_VERSION`), a última versão cloud publicada e o manifesto estável do
terminal. Publicar um registro cloud não reinicia a nuvem: o Dokploy precisa
fazer o deploy do commit/imagem correspondente. O terminal consulta o
manifesto assinado durante o bootstrap e periodicamente pelo worker, exibe o
aviso de versão e só instala depois da validação manual do pacote e do backup
local.

Se a resposta incerta ocorreu ao criar o cliente (antes de existir `customer_id` salvo), a equipe financeira pode solicitar `POST jobs/<id>/reconcile` com `recover_customer: true`. A recuperação consulta a referência externa e confere o documento, sem criar outro cliente. Havendo um único cliente correspondente, continua a solicitação original a partir da etapa de assinatura.

`GET platform/metrics` calcula contagens globais, sem depender da primeira página. MRR considera assinaturas ativas vigentes, com valor anual dividido por 12 quando contratado anualmente. Só billing/superusuário recebem valores financeiros. MRR contratado e soma de cobranças confirmadas são métricas distintas.
