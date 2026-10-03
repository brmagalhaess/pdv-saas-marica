# PDV2 Monitor para iOS

Aplicativo SwiftUI somente leitura para o proprietário da empresa. Ele usa o
mesmo login do SaaS, mas o servidor só libera o endpoint de monitoramento para
uma associação com papel `owner`. Usuários `admin`, `manager` ou `cashier` não
conseguem abrir este painel.

## Abrir e executar

1. Abra `PDV2OwnerMonitor.xcodeproj` no Xcode 27 ou superior.
2. Selecione um simulador iPhone e execute o scheme `PDV2OwnerMonitor`.
3. No simulador, informe `http://127.0.0.1:8000` como URL da API. O simulador
   acessa o servidor local do Mac nesse endereço.
4. Em um iPhone físico, informe a URL HTTPS do domínio público da VPS. O
   projeto permite rede local para desenvolvimento, mas produção deve usar
   HTTPS.

O aplicativo não grava a senha. A sessão usa o cookie seguro de login e é
removida ao sair. A URL da API é salva apenas como preferência local.

## O que o painel mostra

- terminais online e offline (online significa heartbeat nos últimos cinco
  minutos);
- faturamento, quantidade de vendas e ticket médio do dia;
- recebimentos por dinheiro, Pix, crédito e débito;
- contas a receber e a pagar quando o plano possui o módulo financeiro;
- gráfico e lista de vendas por terminal;
- versão e último contato de cada terminal.

Os dados são atualizados ao abrir, ao puxar para atualizar e automaticamente a
cada 30 segundos. O endpoint é `GET /api/v1/owner/monitoring` e responde com
`Cache-Control: private, no-store`.

## Compilação pela linha de comando

```bash
xcodebuild -project PDV2OwnerMonitor.xcodeproj \
  -scheme PDV2OwnerMonitor \
  -sdk iphonesimulator \
  -destination 'generic/platform=iOS Simulator' \
  CODE_SIGNING_ALLOWED=NO build
```
