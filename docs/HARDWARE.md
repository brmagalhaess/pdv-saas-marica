# Pesquisa de periféricos para o primeiro piloto

Pesquisa documental feita em 28/09/2026. **Nenhum equipamento foi testado fisicamente.** A lista orienta a compra de uma bancada de testes; não é uma declaração de compatibilidade do PDV.

## Bancada recomendada

1. **Cupom térmico: Epson TM-T20X USB/Serial.** A [página oficial do produto](https://epson.com.br/Para-empresas/Impressoras/Impressoras-de-Pontos-de-Venda/Impressora-de-Recibos-Epson-TM-T20X-%28Serial-USB%29/p/C31CH26031) especifica papel de 80 mm, guilhotina e interfaces USB/serial; o [suporte Epson](https://epson.com.br/Suporte/Ponto-de-venda/Impressoras-de-recibos/Epson-TM-T20X/s/SPT_C31CH26031) oferece guia técnico e drivers. Começar pelo driver do Windows e, depois, avaliar comandos ESC/POS.
2. **Alternativa de cupom: Elgin i9 Full.** A [página oficial](https://www.elgin.com.br/impressora-termica-de-cupom-i9-full-nfc-e-sat-300-mm-seg-corte-automatico/p) identifica o modelo e o centro de suporte; a [documentação da linha Elgin](https://blog.elgin.com.br/automacao/digitalizacao-do-varejo-primeira-edicao/) descreve interfaces USB, Ethernet e serial e bibliotecas. Confirmar a revisão exata adquirida antes de implementar.
3. **Etiqueta: Elgin L42 Pro Full.** Segundo a [página oficial](https://www.elgin.com.br/Produtos/automacao/impressoras-de-etiquetas/L42PROFULL), tem USB, Ethernet e suporte a EPL/ZPL/PPLA/PPLB. Usar ZPL ou driver Windows em um protótipo de etiqueta de preço e código de barras.
4. **Leitor: Elgin Flash II USB.** A [página oficial](https://www.elgin.com.br/leitor-codigo-barras-elgin-flash-ii/p) apresenta o leitor linear. Para QR Code e códigos 2D, escolher outro modelo da [linha oficial de leitores 1D/2D](https://www.elgin.com.br/automacao-comercial/leitor-de-codigo-de-barras). Validar comportamento de teclado, sufixo Enter e foco da tela.
5. **Balança: definir após entrevista do piloto.** A [linha de balanças da Elgin](https://www.elgin.com.br/automacao-comercial/balanca-para-pdv) mostra a DP30-T, mas a página consultada não especifica um protocolo de comunicação com o PDV. Antes de comprar, solicitar ao fabricante o manual de integração/protocolo e confirmar se a loja usa etiqueta de pesável ou leitura direta da balança.

## Roteiro de teste físico

- Registrar modelo exato, interface, firmware, driver e versão do Windows.
- Desconectar cabo, retirar papel, reiniciar durante impressão e repetir trabalho sem duplicar venda.
- Imprimir cupom longo com acentos, QR Code e corte; aferir largura e legibilidade.
- Imprimir etiquetas em diferentes tamanhos; conferir alinhamento e leitura do EAN.
- Ler 100 códigos em sequência, incluindo produto não encontrado e código de pesável.
- Ensaiar operação sem internet e recuperar fila de impressão.

**Compra mínima sugerida:** uma impressora de cupom, uma de etiquetas e um leitor; a balança pode aguardar a escolha da primeira mercearia piloto e o protocolo confirmado pelo fabricante.
