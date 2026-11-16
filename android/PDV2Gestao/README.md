# PDV2 Gestão — Android

App nativo (Kotlin + Jetpack Compose, Material 3) para **acompanhar** a empresa pelo celular.
É só de consulta: não vende, não cobra, não tem preço, plano nem link de assinatura.
O acesso é liberado pelo administrador do PDV2 no painel web (`/platform` → "Acessos ao app").

- `applicationId` / `namespace`: `br.com.crowntech.pdv2gestao`
- Nome: **PDV2 Gestão** · versão `1.0.0` (`versionCode 1`)
- `minSdk 26`, `compileSdk 36`, `targetSdk 36`, JDK 17
- Contrato da API: `docs/auditoria-2026-11/app-gestao.md` + contrato de integração (rotas `/api/v1/app/...`)

## Telas (iguais ao iOS)

Abertura com biometria → Entrar → Código de verificação (só equipe CrownTech) → Acesso não liberado →
Início com abas:

- Empresa: `Resumo | Vendas | Caixa | Estoque | Mais`
- Equipe CrownTech: `Plataforma | Resumo | Vendas | Caixa | Mais` (Estoque fica em Mais)

Mais: Fiado, Terminais, Conflitos de sincronização e Conta (aparelhos conectados, biometria,
política de privacidade, suporte, excluir minha conta, sair).

## Estrutura

```
app/src/main/kotlin/br/com/crowntech/pdv2gestao/
  core/api        cliente Ktor (Bearer, X-Tenant-ID, timeout 20 s, erros pelo "code" do contrato)
  core/model      DTOs @Serializable (dinheiro em centavos Long)
  core/session    SessionManager (renovação "single-flight" com Mutex, rotação do refresh)
  core/security   chave AES-256-GCM no Android Keystore + DataStore (secure_session); preferências
  core/biometric  BiometricPrompt (biometria forte ou bloqueio de tela)
  core/format     R$ 1.234,56 · 1,250 kg · 16/11/2026 14:03 · "há 3 min" (fuso America/Sao_Paulo)
  feature/...     login, mfa, accessdenied, home, summary, sales, cash, stock, more,
                  credit, terminals, conflicts, account, platform
  ui/             tema (cores da marca, claro/escuro, sem cor dinâmica) e componentes
```

Sem Retrofit, Firebase ou Hilt (porta aberta para Kotlin Multiplatform).

## Segurança

- Access token só em memória. Refresh + id do aparelho + endereço do servidor ficam cifrados
  (Keystore, alias `pdv2_session_key`). Se a chave sumir, o app apaga tudo e volta ao login.
- `allowBackup=false` e `data_extraction_rules` excluindo tudo: o token não vai para backup.
- Release só fala `https://pdv2.crowntech.com.br` e recusa `http`. Texto claro só no Debug, para
  `127.0.0.1`, `localhost` e `10.0.2.2` (`src/debug/res/xml/network_security_config.xml`).
- Permissões: `INTERNET` e `USE_BIOMETRIC`. Nada mais.

## Compilar e testar

```bash
cd android/PDV2Gestao
export JAVA_HOME=/opt/homebrew/opt/openjdk@17/libexec/openjdk.jdk/Contents/Home
# local.properties (não versionado): sdk.dir=/Users/<você>/Library/Android/sdk
./gradlew --no-daemon :app:testDebugUnitTest :app:assembleDebug :app:bundleRelease
```

Os testes (`app/src/test`) decodificam todos os exemplos JSON do contrato
(`app/src/test/resources/fixtures/`), conferem a formatação, o mapa `code` → ação, a recusa de
`http` no Release e a rotação/renovação de tokens com um servidor simulado (Ktor MockEngine).

### Debug contra o servidor local

```bash
adb reverse tcp:8000 tcp:8000   # o app Debug usa http://127.0.0.1:8000
```

A tela de login do Debug tem o campo "Servidor (desenvolvimento)" para trocar o endereço.

## Assinatura (chave de upload da Google Play)

A chave **não** fica no repositório. Informe por variáveis de ambiente:

```bash
export PDV2_UPLOAD_STORE_FILE=/caminho/seguro/upload.jks
export PDV2_UPLOAD_STORE_PASSWORD=...
export PDV2_UPLOAD_KEY_ALIAS=upload
export PDV2_UPLOAD_KEY_PASSWORD=...
./gradlew :app:bundleRelease   # app/build/outputs/bundle/release/app-release.aab
```

ou crie `android/PDV2Gestao/keystore.properties` (ignorado pelo git) com `storeFile`,
`storePassword`, `keyAlias` e `keyPassword`. Sem nenhum dos dois, o `bundleRelease` gera um AAB
**não assinado** (serve para conferir o build, não para enviar à loja). Use o Play App Signing.

## Loja

Textos da ficha em `store/google/`.
