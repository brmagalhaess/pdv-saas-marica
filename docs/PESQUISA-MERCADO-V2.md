# Pesquisa funcional e decisões do PDV2

Consulta em 29/09/2026, usando páginas dos próprios fornecedores e documentação técnica. A pesquisa identifica capacidades de referência; não comprova desempenho, homologação ou disponibilidade de marca do PDV2.

## Referências observadas

- [Loyverse — produto](https://loyverse.com/br) e [operação offline](https://help.loyverse.com/br/help/offline-work-of-pos): venda e turnos offline, fila de recibos e sincronização posterior; reembolso e pagamentos integrados têm limitações sem conexão. Decisão: o terminal deve persistir localmente antes do sucesso e mostrar pendências.
- [vhsys — frente de caixa](https://www.vhsys.com.br/funcionalidades/pdv-ponto-venda/) e [controle de caixa](https://ajuda.vhsys.com.br/categorias/vendas/pdv-controle-dos-caixas): caixa integrado com catálogo, estoque, financeiro e permissões, incluindo sangria e reforço. Decisão: esses fluxos compõem o núcleo, com dinheiro em centavos e estoque em milésimos.
- [MarketUP — manual](https://files.marketup.com/pdf/mktp_manual.pdf): PDV offline e sincronização ao voltar a conexão. Decisão: a aplicação de gestão pode depender da nuvem; o caixa tem banco e serviço próprios no computador da loja.

## Perfis e segmentos

O núcleo atende venda de balcão e varejo por unidade/peso: mercearias, conveniência, pequenos mercados, lojas de variedades, vestuário com variantes e produtos de pet shop. Categoria, unidade, variante, preço, estoque, permissões e plano são dados configuráveis.

Food service requer comandas/mesas, complementos, cozinha e eventual delivery. Serviços requerem orçamento/ordem de serviço, agenda e NFS-e. Farmácias, combustíveis, transporte e segmentos com controle específico não devem ser anunciados como suportados por compartilhar a mesma tela de venda. Cada módulo precisa de regra e validação próprias. A matriz de implementação atual está em STATUS-V2.md.

## Impressão

- [Epson — referência ESC/POS](https://download4.epson.biz/sec_pubs/pos/reference_en/escpos/index.html), revisão exibida 3.40, lista TM-T20X e outras TM. Nem todo comando existe em toda revisão/modelo.
- [Zebra — ZPL Code 128](https://docs.zebra.com/us/en/printers/software/zpl-pg/c-zpl-zpl-commands/r-zpl-bc.html): etiquetas com linguagem ZPL; inicialização e fechamento do layout precisam seguir o protocolo.
- [Elgin L42 Pro Full](https://www.elgin.com.br/Produtos/automacao/impressoras-de-etiquetas/L42PROFULL): referência de modelo para bancada de etiquetas. Revisão/interface devem ser confirmadas na unidade comprada.

Decisão: camada de impressão no terminal, spool persistente, formatos ESC/POS 58/80 mm e ZPL/EPL, envio de rede ou spool do Windows. Compatibilidade de protocolo é diferente de teste físico. Nenhum equipamento foi homologado nesta sessão; registrar modelo/firmware/interface e resultado antes de oferecer garantia comercial.

## Fiscal, cobrança e operação

- [SEFAZ-RJ — NFC-e](https://portal.fazenda.rj.gov.br/dfe/wp-content/uploads/sites/17/2023/01/DF-e_NFC-e.pdf): contingência fiscal envolve documento próprio e transmissão posterior. Gravar venda offline não substitui esse processo.
- [Focus NFe — autenticação](https://doc.focusnfe.com.br/reference/autenticacao), [NFC-e](https://doc.focusnfe.com.br/reference/emitir_nfce), [ambientes](https://doc.focusnfe.com.br/reference/ambiente): conector com referência estável, consulta de status e homologação separada.
- [Asaas — assinatura](https://docs.asaas.com/reference/criar-nova-assinatura) e [webhook](https://docs.asaas.com/docs/create-new-webhook-via-api): criação da recorrência não é pagamento; eventos devem ser autenticados e deduplicados. Reconciliar com a cobrança atual no provedor evita regredir o estado por eventos atrasados.
- [Django — implantação](https://docs.djangoproject.com/en/5.2/howto/deployment/checklist/): servidor de produção, HTTPS, hosts/segredos explícitos, backup e verificação de configuração.
- [Dokploy — Compose](https://docs.dokploy.com/docs/core/docker-compose): infraestrutura reproduzível para cloud, banco e workers; terminal fica na loja.

A qualidade competitiva depende também de medir tempo de venda, suporte, restauração, perdas de conexão e retenção em lojas piloto. Nenhuma página de concorrente permite afirmar que todos os tipos de empresa estão cobertos.
