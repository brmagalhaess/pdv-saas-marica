# installer/

Tudo o que gera o instalador comercial do PDV2 para Windows. O guia completo (o que o instalador faz, como publicar, como voltar atrás e a lista de testes manuais) está em [`docs/INSTALADOR-WINDOWS.md`](../docs/INSTALADOR-WINDOWS.md).

| Arquivo | Para que serve |
|---|---|
| `build.ps1` | Gera tudo em `dist\windows`: executáveis, bundle de atualização, instalador e metadados. |
| `pdv2.spec` | PyInstaller em modo pasta: `PDV2.exe` (atalho), `PDV2Server.exe` (caixa local) e `PDV2Service.exe` (serviço). |
| `pdv2.iss` | Inno Setup: instala em `Program Files\PDV2`, registra o serviço, cria atalhos, migra dados antigos. |
| `entry_*.py` | Pontos de entrada dos três executáveis. |
| `make_icon.py` | Desenha o símbolo PDV2 e grava `pdv2.ico` (só biblioteca padrão). |
| `pdv2.ico` | Ícone já gerado (16 a 256 px). |
| `requirements-build.txt` | Ferramentas só de build (PyInstaller). |

## Gerar

Em um Windows 10/11 de 64 bits com Python 3.12 e Inno Setup 6.3 ou mais novo:

```powershell
powershell -ExecutionPolicy Bypass -File installer\build.ps1
```

Sem Windows à mão, use o workflow `PDV2 Windows installer` do GitHub Actions (`.github/workflows/windows-installer.yml`), que publica o instalador e o bundle como artefatos.

**Estado desta entrega:** escrito e testado em macOS. O `pdv2.spec` foi executado com PyInstaller em macOS e o servidor empacotado respondeu; `build.ps1`, `pdv2.iss` e o serviço do Windows ainda **não** rodaram em Windows.
