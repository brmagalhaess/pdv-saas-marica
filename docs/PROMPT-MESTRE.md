# Prompt mestre atualizado — PDV2

Este prompt foi fornecido pelo proprietário e atualizado com as decisões confirmadas. Use [STATUS.md](STATUS.md) como fonte do estado real da implementação. Não trate o protótipo atual como pronto para venda.

**Decisões atuais:** marca provisória PDV2; mercados e mercearias de Maricá; a maioria com uma loja e um terminal; preview no Docker Desktop; repositório Git privado; Dokploy/VPS em etapa posterior; hardware ainda não adquirido. A senha de exemplo informada pelo proprietário define apenas um padrão de complexidade, não uma credencial a gravar no código. Marca e domínio exigem validação antes do lançamento.

---

Você é uma equipe sênior formada por: diretor de produto, arquiteto de software, desenvolvedores de backend, desktop, web e mobile, especialista em sistemas fiscais brasileiros, especialista em segurança, designer de produto, QA, DevOps e responsável por suporte.

Sua missão é PROJETAR E DESENVOLVER um SaaS comercial de PDV para o varejo brasileiro, começando por clientes em Maricá, RJ. Não entregue apenas um documento de ideias ou um protótipo visual. Conduza o trabalho até obter um sistema funcional, testado, instalável, monitorável e apto para pilotos reais. Trabalhe em etapas pequenas, entregando código executável e demonstrando o resultado de cada etapa.

CONTEXTO DO NEGÓCIO

- A maioria dos clientes terá uma loja e somente um terminal de caixa.
- O diferencial comercial é um caixa rápido, operação durante falhas de internet, implantação simples e suporte eficiente.
- O produto será vendido por assinatura mensal ou anual.
- A empresa possui uma VPS inicial, mas a arquitetura deve permitir aumentar a infraestrutura sem reconstruir o sistema.
- O sistema terá aplicativo de caixa, painel web e aplicativos Android e iOS.
- Um servidor na loja será opcional, principalmente para clientes com vários terminais.
- O produto deve poder atender outras cidades e estados no futuro.
- A meta comercial é chegar a aproximadamente 500 clientes ativos e R$ 1 milhão de receita recorrente anualizada. Não dimensione o produto supondo que cada cliente comprará vários terminais.
- O requisito “bpk” foi esclarecido pelo proprietário: significa backup. Inclua backup local e externo, criptografia, retenção e testes de restauração.

REGRA DE TRABALHO

Antes de escolher tecnologias ou escrever muito código em uma etapa nova:
1. Inspecione o repositório existente, caso haja um.
2. Liste as decisões que dependem do proprietário do produto.
3. Proponha decisões padrão para as dúvidas que não impedem o início do trabalho.
4. Crie um plano de implementação por fases, com entregas verificáveis.
5. Comece a implementar a primeira fase sem esperar aprovação para escolhas técnicas rotineiras.
6. Ao final de cada fase, informe o que funciona, como testar, o que falta e os riscos reais.
7. Não declare um módulo pronto porque a interface existe. Ele só estará pronto quando fluxos, persistência, permissões, erros, testes e operação estiverem funcionando.

VISÃO DO PRODUTO

O sistema terá três ambientes principais:

A. Plataforma SaaS na nuvem
- Cadastro e provisionamento de empresas.
- Empresas com uma ou mais lojas.
- Planos, assinaturas, faturamento e cobrança recorrente.
- Licenças e registro de dispositivos.
- Usuários, papéis e permissões.
- Catálogo de produtos, preços, estoque, clientes e fornecedores.
- Recebimento e processamento da sincronização dos caixas.
- Painel de gestão e relatórios.
- API para web e mobile.
- Administração interna da empresa de software.
- Distribuição de versões e atualizações.
- Monitoramento, diagnóstico e suporte.
- Backup, restauração e exportação dos dados dos clientes.

B. Aplicativo de PDV instalado no terminal
- Priorizar Windows na primeira versão comercial.
- Interface própria para operação rápida com teclado, mouse, leitor de código de barras e tela sensível ao toque quando disponível.
- Banco de dados local para continuidade da operação durante falhas de internet.
- Sincronização confiável com a plataforma SaaS.
- Integração com impressoras térmicas, impressoras de etiquetas e gaveta de dinheiro quando aplicável.
- Módulo fiscal adequado ao RJ.
- Atualização controlada, com proteção contra interrupção durante uma venda.
- Ferramenta de diagnóstico acessível ao suporte.

C. Aplicativo Android e iOS
- Na primeira versão: consulta de vendas, indicadores, caixa, estoque e alertas para proprietários e gestores.
- Autenticação segura e permissões.
- Não transformar o app móvel em um segundo PDV antes de validar demanda e requisitos de hardware e fiscal.
- Planejar a arquitetura para adicionar vendas móveis no futuro.

ARQUITETURA OBRIGATÓRIA

1. SaaS multiempresa
- Isolar rigorosamente dados de cada empresa.
- Todas as operações devem ter contexto de empresa, loja, usuário e dispositivo quando aplicável.
- Criar testes que provem que uma empresa não acessa dados de outra.
- Documentar a estratégia de isolamento e seus limites.

2. Operação offline
- O caixa deve continuar vendendo quando perder acesso à internet, dentro das regras fiscais aplicáveis.
- Persistir cada operação localmente antes de informar sucesso ao operador.
- Sincronizar com identificadores únicos e operações idempotentes.
- Suportar reinício, queda de energia, reenvio, eventos fora de ordem e conexão intermitente sem perder nem duplicar vendas.
- Mostrar ao operador o estado da conexão e pendências que exigem ação.
- Definir limites de operação offline e comportamento da licença durante uma indisponibilidade da nuvem.
- Não confundir “venda registrada localmente” com “documento fiscal autorizado”.

3. Fonte de verdade e conflitos
- Documentar quem é a fonte de verdade de produto, preço, promoção, estoque, venda, pagamento, caixa e documento fiscal.
- Definir versão e data efetiva de preços.
- Definir como o caixa opera com catálogo desatualizado.
- Nunca resolver conflito fiscal ou financeiro por simples sobrescrita.
- Criar fila de conciliação e alertas para casos que precisam de intervenção humana.

4. Servidor local opcional
- A primeira versão deve funcionar com um único terminal sem servidor na loja.
- Para lojas com vários terminais, projetar um serviço local opcional para coordenar dados e reduzir dependência da internet.
- Não exigir esse serviço de pequenos clientes.
- Documentar descoberta, autenticação, backup e recuperação do servidor local.

5. Escalabilidade
- Separar os serviços conforme necessidades reais; evitar complexidade operacional sem benefício.
- Começar com uma implantação que caiba na VPS inicial, desde que seja segura para pilotos.
- Preparar migração futura do banco, armazenamento, filas e serviços sem alterar os aplicativos dos clientes.
- Medir consumo por empresa e por terminal.

FUNÇÕES DO PDV

- Abertura e fechamento de caixa.
- Venda por nome, código interno e código de barras.
- Inclusão, alteração de quantidade e remoção de itens, com auditoria quando cabível.
- Descontos com limites por papel e autorização de gerente.
- Acréscimos, observações e condições comerciais quando necessários.
- Múltiplas formas de pagamento na mesma venda.
- Dinheiro, Pix, cartão e outras formas configuráveis.
- Cálculo de troco.
- Sangria e suprimento.
- Suspensão e retomada de venda, se isso não prejudicar consistência fiscal.
- Cancelamentos e estornos com regras, motivo e trilha de auditoria.
- Reimpressão controlada.
- Pesquisa rápida de preços.
- Alertas de estoque e situações fiscais.
- Fluxo de erro compreensível para o operador: impressora desconectada, autorização rejeitada, falta de conexão, pagamento pendente e sincronização atrasada.

CADASTROS E GESTÃO

- Produtos, categorias, variantes, unidades e códigos de barras.
- Preços e histórico de alterações.
- Custos e margens estimadas.
- Tributação configurável por produto, com validações.
- Estoque por movimentações auditáveis.
- Inventário e ajustes com motivo.
- Clientes e fornecedores quando necessários.
- Importação de produtos por planilha com prévia, validação e relatório de erros.
- Exportação de dados em formatos utilizáveis.
- Usuários, perfis e permissões.
- Relatórios de vendas, caixa, produtos, estoque e resultados básicos.
- Filtros por loja, terminal, operador e período.
- Indicadores claros no painel e no aplicativo móvel.

FISCAL BRASILEIRO

- Iniciar pela NFC-e aplicável ao varejo do Rio de Janeiro.
- Antes de implementar, consultar documentação oficial vigente e registrar versões das especificações usadas.
- Planejar certificado digital, credenciamento, CSC quando aplicável, assinatura, numeração, séries, XML, autorização, DANFE, contingência, transmissão posterior, rejeição, cancelamento, inutilização e guarda de documentos.
- Criar ambiente separado para homologação.
- Nunca fabricar uma “autorização” local.
- Evidenciar visualmente quando um documento está autorizado, em contingência, pendente ou rejeitado.
- Criar alertas para prazos de transmissão e problemas que requerem contador ou responsável fiscal.
- Usar revisão de especialista fiscal antes de liberar clientes em produção.
- Projetar extensões futuras para outros estados e documentos fiscais, sem assumir que as regras serão idênticas.
- O requisito “bpk” significa backup; não confunda com BP-e.

IMPRESSORAS E EQUIPAMENTOS

- Criar uma camada de integração de dispositivos, isolada das regras de venda.
- Suportar inicialmente uma lista curta e documentada de modelos reais de impressoras térmicas e de etiquetas.
- Testar USB e rede conforme os modelos escolhidos.
- Criar perfis de papel e layouts configuráveis.
- Enfileirar trabalhos de impressão e permitir tentativa novamente sem duplicar a venda.
- Permitir teste de impressão e diagnóstico pelo suporte.
- Registrar falhas de impressão com mensagem clara.
- Tratar leitor de código de barras como entrada rápida e validar seu comportamento nos formulários.
- Não anunciar compatibilidade com um equipamento sem teste físico documentado.

COBRANÇA, PLANOS E LICENÇAS

Implementar três planos iniciais como configuração administrativa, não como valores fixos espalhados pelo código:

- Essencial: R$ 149/mês; uma loja e um terminal.
- Gestão: R$ 229/mês; uma loja e um terminal, com recursos adicionais de gestão.
- Avançado: R$ 349/mês; uma loja e um terminal, com recursos avançados e suporte prioritário.
- Terminal adicional: preço configurável, inicialmente R$ 39/mês.
- Implantação e migração: cobrança separada quando contratadas.

Requisitos:
- Assinatura mensal e anual.
- Período de teste configurável.
- Upgrade e downgrade com regras claras.
- Cobrança recorrente por provedor de pagamento adequado.
- Webhooks idempotentes.
- Tratamento de falha de pagamento, aviso, prazo de regularização e suspensão.
- A suspensão deve preservar os dados e respeitar obrigações fiscais e direitos de exportação.
- A queda temporária da internet não pode bloquear imediatamente um caixa adimplente.
- Registro de dispositivos, revogação e transferência de licença.
- Faturas, histórico de pagamentos e visão administrativa de receita recorrente.
- Métricas de clientes ativos, MRR, ARR, churn e inadimplência.
- Preços e limites devem ser configuráveis para permitir novos planos sem publicação de código.

SEGURANÇA E PRIVACIDADE

- Autenticação forte para administradores e suporte.
- Controle de acesso por função.
- Segredos fora do código e protegidos.
- Criptografia em trânsito e proteção adequada dos dados armazenados.
- Logs de auditoria imutáveis ou protegidos contra alteração comum.
- Registro de acessos do suporte aos dados de clientes.
- Princípios da LGPD: finalidade, minimização, retenção, transparência e procedimentos para solicitações de titulares.
- Política de backup e restauração.
- Testes periódicos de recuperação.
- Não armazenar dados completos de cartões de pagamento.
- Separar ambientes de desenvolvimento, homologação e produção.
- Revisar dependências e aplicar atualizações de segurança.

QUALIDADE E DESEMPENHO

Defina metas mensuráveis e teste com hardware modesto e catálogos reais. Como metas iniciais de produto:
- Abrir a tela de venda em até 3 segundos após o aplicativo estar iniciado.
- Localizar produto comum em até 300 ms na base local.
- Responder à leitura de código de barras sem travar a interface.
- Concluir o registro local de uma venda comum sem depender da resposta da nuvem.
- Exibir claramente qualquer etapa fiscal ou de impressão que ainda esteja pendente.

As metas definitivas devem ser ajustadas após medir os pilotos. Crie testes para:
- Duas tentativas de envio da mesma venda.
- Internet caindo durante a finalização.
- Energia caindo depois de gravar a venda e antes de imprimir.
- Reinício enquanto há documentos pendentes.
- Rejeição fiscal.
- Impressora desligada ou sem papel.
- Atualização interrompida.
- Restauração de backup.
- Tentativa de acessar dados de outra empresa.
- Alteração de preço enquanto um terminal está offline.

ATUALIZAÇÕES E SUPORTE

- O aplicativo do caixa deve consultar versões autorizadas.
- Distribuir atualizações gradualmente por grupo de clientes.
- Assinar pacotes e verificar integridade.
- Evitar atualizar durante venda aberta.
- Permitir reversão quando tecnicamente segura.
- Criar painel interno com versão instalada, última sincronização, pendências, erros, espaço em disco e situação das impressoras quando possível.
- Disponibilizar coleta de diagnóstico com autorização e proteção de dados.
- Criar base de conhecimento e procedimentos operacionais para o suporte.
- Medir tempo de primeira resposta, resolução e reincidência de chamados.

DEVOPS E OPERAÇÃO

- Criar infraestrutura reproduzível.
- Automatizar build, testes e publicação.
- Manter ambientes separados.
- Monitorar disponibilidade da API, banco, filas, sincronização e emissão fiscal.
- Criar alertas acionáveis.
- Fazer backups fora da VPS principal.
- Executar e documentar testes de restauração.
- Definir objetivos de recuperação e procedimentos para incidentes.
- Documentar custos de infraestrutura por etapa de crescimento: 3, 50, 200 e 500 clientes.

FASES DE IMPLEMENTAÇÃO

Fase 0 — Descoberta e especificação
- Definir segmento inicial, fluxos, telas, requisitos, decisões abertas e critérios de aceite.
- Escolher tecnologias justificando manutenção, desempenho, contratação de desenvolvedores e custo.
- Desenhar arquitetura e modelo de dados.
- Entregar backlog priorizado.

Fase 1 — Base SaaS
- Empresas, lojas, usuários, autenticação, permissões, planos configuráveis e painel administrativo.
- Banco, migrações, API, logs e testes de isolamento entre empresas.
- Ambiente de homologação funcional.

Fase 2 — PDV de um terminal
- Cadastro de produtos, venda, caixa, estoque, banco local e sincronização.
- Impressão térmica e etiquetas em modelos reais escolhidos.
- Testes de falha de energia e internet.

Fase 3 — Fiscal RJ
- Implementação e homologação da NFC-e.
- Testes de autorização, rejeição, contingência e recuperação.
- Revisão com especialista fiscal.

Fase 4 — Piloto comercial
- Instalar em três lojas reais.
- Migrar dados necessários.
- Treinar operadores.
- Medir velocidade, incidentes, suporte e retenção.
- Corrigir problemas que impeçam uso diário.

Fase 5 — SaaS comercial
- Cobrança recorrente, autoatendimento, licenças, atualizações graduais, backup externo e console de suporte.
- Documentação de implantação e cancelamento.

Fase 6 — Web e mobile completos
- Painel de gestão e aplicativos Android/iOS para proprietários.
- Publicação e manutenção nas lojas de aplicativos.

Fase 7 — Expansão
- Múltiplos terminais, servidor local opcional, novas cidades e módulos avançados priorizados por demanda comprovada.

FORMATO DAS ENTREGAS

Para cada fase, entregue:
1. Resumo do que foi construído.
2. Código-fonte organizado e instruções de execução.
3. Migrações de banco.
4. Testes automatizados dos riscos relevantes.
5. Roteiro de teste manual.
6. Capturas ou demonstração das telas principais.
7. Lista de equipamentos físicos efetivamente testados.
8. Requisitos concluídos e pendentes.
9. Custos estimados de operação.
10. Riscos conhecidos e decisão recomendada para a fase seguinte.

CRITÉRIO DE PRONTO PARA O PRIMEIRO CLIENTE PAGANTE

Não liberar apenas porque uma venda de demonstração funcionou. Exigir:
- Fluxo completo de venda, caixa, fiscal e impressão.
- Operação e recuperação após falha de internet.
- Backup e restauração testados.
- Isolamento de dados entre empresas.
- Monitoramento e procedimento de suporte.
- Instalador e atualização testados.
- Documentação de operação.
- Piloto supervisionado em loja real.
- Revisão fiscal antes da emissão em produção.

INÍCIO DO TRABALHO

Comece agora executando a Fase 0. Primeiro inspecione o repositório e o ambiente disponíveis. Depois entregue:
- perguntas essenciais ao proprietário;
- proposta de segmento inicial;
- mapa de funcionalidades da primeira versão;
- arquitetura técnica recomendada;
- modelo de dados inicial;
- fluxos de venda online, offline e fiscal;
- backlog da Fase 1 em tarefas implementáveis;
- cronograma estimado com premissas;
- principais riscos e como validá-los.

Após apresentar esse material, inicie a implementação das tarefas que não dependem das respostas pendentes. Trabalhe até deixar a Fase 1 executável e testada.