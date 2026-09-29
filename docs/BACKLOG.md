# Backlog da Fase 1

## Implementado nesta entrega

- [x] Estrutura executável sem dependências externas.
- [x] Criação de empresa, proprietário, loja principal e assinatura em teste.
- [x] Planos iniciais no banco.
- [x] Login, logout e sessão com cookie HttpOnly.
- [x] Consulta de empresa, plano, lojas e usuários com filtro por empresa.
- [x] Proprietário cria lojas e usuários gerente/operador.
- [x] Auditoria inicial de criação de empresa, loja e usuário.
- [x] Painel web simples para cadastro, login e lojas.
- [x] Teste HTTP de isolamento entre empresas.
- [x] Comando de backup local consistente e verificado.
- [x] Catálogo e movimentações de estoque por loja.
- [x] Caixa local experimental com venda atômica e outbox.
- [x] Registro de terminal e recepção idempotente de eventos.
- [x] Landing com planos e painel de catálogo, estoque e vendas recebidas.

## Pendente para concluir a Fase 1 de produção

- [ ] PostgreSQL central, migrações versionadas e configuração por ambiente.
- [ ] Servidor HTTP de produção, HTTPS e política de proxy confiável.
- [ ] Verificação de e-mail, redefinição de senha, MFA para administração e limite de tentativas de login.
- [ ] Convite seguro para novos usuários em vez de senha definida pelo proprietário.
- [ ] Console interno de suporte e administração de planos, com auditoria forte.
- [ ] Fluxos de assinatura, cobrança e webhooks idempotentes.
- [ ] Testes de permissões e isolamento para todas as rotas futuras.
- [ ] Testes de carga e monitoramento.
- [ ] Backup externo criptografado, retenção, alertas e restauração automatizada.
- [ ] Pipeline de build/teste/deploy e ambiente de homologação implantado.
- [ ] Conciliação do saldo central após sincronização de vendas e tratamento de divergências.
- [ ] Provisionamento de catálogo e licença no caixa local.
- [ ] Interface operacional Windows, NFC-e, impressão física e integração de pagamentos.
- [ ] Aplicativos Android e iOS.

## Próximo incremento recomendado

Implementar catálogo multiempresa (`products`, código de barras e preço versionado), com importação assistida para um piloto de mercearia. Em paralelo, desenhar o contrato de sincronização e comprar os equipamentos de teste.
