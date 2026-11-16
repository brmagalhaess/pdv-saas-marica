<#
Gera o instalador do PDV2 para Windows e o bundle de atualização automática.

    powershell -ExecutionPolicy Bypass -File installer\build.ps1
    powershell -ExecutionPolicy Bypass -File installer\build.ps1 -Version 2.0.1 -SkipInstaller

Saída em dist\windows:
    PDV2-Instalar-<versão>.exe                      instalador completo
    PDV2-Instalar-<versão>.metadata.json            metadados (kind=installer)
    PDV2-Terminal-<versão>-win64-bundle.zip         atualização automática (app-<versão>\)
    PDV2-Terminal-<versão>-win64-bundle.metadata.json   metadados (kind=bundle)

Requisitos: Windows 10/11 x64, Python 3.12 no PATH, Inno Setup 6.3+ (ISCC.exe).
Assinatura (opcional): defina PDV_SIGN_PFX (caminho do .pfx) e PDV_SIGN_PASSWORD, ou
use -CertificatePath / -CertificatePassword. Sem certificado os arquivos saem sem assinatura.

ATENÇÃO: este roteiro foi escrito em macOS e ainda não rodou em Windows real.
#>
param(
    [string]$Version,
    [switch]$SkipInstaller,
    [switch]$SkipSmoke,
    [string]$CertificatePath = $env:PDV_SIGN_PFX,
    [string]$CertificatePassword = $env:PDV_SIGN_PASSWORD,
    [string]$TimestampUrl = "http://timestamp.digicert.com"
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if (-not $Version) { $Version = (Get-Content (Join-Path $Root "VERSION") -Raw).Trim() }
if ($Version -notmatch '^\d+\.\d+\.\d+$') { throw "Versão inválida: $Version (use x.y.z)" }

$Out = Join-Path $Root "dist\windows"
$Stage = Join-Path $Out "stage"
$App = Join-Path $Stage "app-$Version"
$Work = Join-Path $Root "build\pyinstaller"

function Invoke-Checked {
    param([string]$What, [scriptblock]$Command)
    & $Command
    if ($LASTEXITCODE -ne 0) { throw "$What falhou (código $LASTEXITCODE)" }
}

function Find-SignTool {
    $Command = Get-Command signtool.exe -ErrorAction SilentlyContinue
    if ($Command) { return $Command.Source }
    $Kits = Join-Path ${env:ProgramFiles(x86)} "Windows Kits\10\bin"
    if (Test-Path $Kits) {
        $Found = Get-ChildItem $Kits -Recurse -Filter signtool.exe -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -match '\\x64\\' } | Sort-Object FullName -Descending | Select-Object -First 1
        if ($Found) { return $Found.FullName }
    }
    return $null
}

$script:SignTool = $null
$script:Signing = $false
if ($CertificatePath) {
    if (-not (Test-Path $CertificatePath)) { throw "Certificado não encontrado: $CertificatePath" }
    $script:SignTool = Find-SignTool
    if (-not $script:SignTool) { throw "signtool.exe não encontrado (instale o Windows SDK)" }
    $script:Signing = $true
} else {
    Write-Warning "Sem certificado de assinatura: os executáveis e o instalador sairão SEM assinatura (o Windows mostrará o alerta de aplicativo desconhecido)."
}

function Sign-File {
    param([string]$Path)
    if (-not $script:Signing) { return }
    $Arguments = @("sign", "/fd", "SHA256", "/tr", $TimestampUrl, "/td", "SHA256", "/f", $CertificatePath)
    if ($CertificatePassword) { $Arguments += @("/p", $CertificatePassword) }
    $Arguments += $Path
    & $script:SignTool @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Assinatura falhou: $Path" }
}

function Find-InnoSetup {
    $Command = Get-Command iscc.exe -ErrorAction SilentlyContinue
    if ($Command) { return $Command.Source }
    foreach ($Base in @(${env:ProgramFiles(x86)}, $env:ProgramFiles)) {
        if ($Base) {
            $Candidate = Join-Path $Base "Inno Setup 6\ISCC.exe"
            if (Test-Path $Candidate) { return $Candidate }
        }
    }
    return $null
}

Push-Location $Root
try {
    if (Test-Path $Out) { Remove-Item $Out -Recurse -Force }
    New-Item -ItemType Directory -Force -Path $Stage | Out-Null

    Write-Host "== Dependências =="
    Invoke-Checked "pip install" { python -m pip install --disable-pip-version-check -r terminal\requirements.txt -r installer\requirements-build.txt }

    Write-Host "== Ícone =="
    Invoke-Checked "make_icon" { python installer\make_icon.py }

    Write-Host "== PyInstaller =="
    $env:PYTHONPATH = $Root
    Invoke-Checked "PyInstaller" { python -m PyInstaller installer\pdv2.spec --noconfirm --distpath (Join-Path $Out "pyinstaller") --workpath $Work }
    Move-Item (Join-Path $Out "pyinstaller\PDV2") $App
    Remove-Item (Join-Path $Out "pyinstaller") -Recurse -Force
    foreach ($Exe in @("PDV2.exe", "PDV2Server.exe", "PDV2Service.exe")) {
        if (-not (Test-Path (Join-Path $App $Exe))) { throw "PyInstaller não gerou $Exe" }
    }
    $Marker = '{"version": "' + $Version + '", "format": "frozen-win64"}'
    [System.IO.File]::WriteAllText((Join-Path $App "bundle.json"), $Marker + "`n", (New-Object System.Text.UTF8Encoding($false)))

    if (-not $SkipSmoke) {
        Write-Host "== Teste de fumaça do servidor empacotado =="
        $SmokeData = Join-Path $env:TEMP ("pdv2-smoke-" + [guid]::NewGuid().ToString("N"))
        $StopFile = Join-Path $SmokeData "stop"
        New-Item -ItemType Directory -Force -Path $SmokeData | Out-Null
        $env:PDV_TERMINAL_DATA = $SmokeData
        $env:PDV_TERMINAL_PORT = "8019"
        $env:PDV_STOP_FILE = $StopFile
        $env:PDV_VERSION = $Version
        $Server = Start-Process -FilePath (Join-Path $App "PDV2Server.exe") -PassThru
        try {
            $Healthy = $false
            foreach ($Attempt in 1..60) {
                Start-Sleep -Seconds 1
                if ($Server.HasExited) { break }
                try {
                    $Response = Invoke-RestMethod -Uri "http://127.0.0.1:8019/health" -TimeoutSec 2
                    if ($Response.status -eq "ok") { $Healthy = $true; break }
                } catch { }
            }
            if (-not $Healthy) {
                Get-ChildItem (Join-Path $SmokeData "logs") -ErrorAction SilentlyContinue | ForEach-Object { Get-Content $_.FullName }
                throw "O servidor empacotado não respondeu em /health"
            }
            Invoke-WebRequest -Uri "http://127.0.0.1:8019/" -UseBasicParsing -TimeoutSec 5 | Out-Null
            New-Item -ItemType File -Path $StopFile | Out-Null
            if (-not $Server.WaitForExit(20000)) { throw "O servidor empacotado não parou com o pedido de parada" }
            Write-Host "Servidor empacotado respondeu e parou corretamente."
        } finally {
            if (-not $Server.HasExited) { $Server.Kill() }
            Remove-Item Env:PDV_TERMINAL_DATA, Env:PDV_TERMINAL_PORT, Env:PDV_STOP_FILE, Env:PDV_VERSION -ErrorAction SilentlyContinue
            Remove-Item $SmokeData -Recurse -Force -ErrorAction SilentlyContinue
        }

        Write-Host "== Teste de fumaça do caixa adicional empacotado =="
        # O mesmo executável no modo caixa adicional: sem isto, um pacote sem station.html ou com
        # erro de importação em station.py só quebraria no cliente, ao escolher "caixa adicional".
        $SmokeData = Join-Path $env:TEMP ("pdv2-smoke-station-" + [guid]::NewGuid().ToString("N"))
        $StopFile = Join-Path $SmokeData "stop"
        New-Item -ItemType Directory -Force -Path $SmokeData | Out-Null
        $env:PDV_TERMINAL_DATA = $SmokeData
        $env:PDV_TERMINAL_PORT = "8019"
        $env:PDV_STOP_FILE = $StopFile
        $env:PDV_VERSION = $Version
        $env:PDV_MODE = "station"
        $Server = Start-Process -FilePath (Join-Path $App "PDV2Server.exe") -PassThru
        try {
            $Healthy = $false
            foreach ($Attempt in 1..60) {
                Start-Sleep -Seconds 1
                if ($Server.HasExited) { break }
                try {
                    $Response = Invoke-RestMethod -Uri "http://127.0.0.1:8019/health" -TimeoutSec 2
                    if ($Response.status -eq "ok" -and $Response.mode -eq "station") { $Healthy = $true; break }
                } catch { }
            }
            if (-not $Healthy) {
                Get-ChildItem (Join-Path $SmokeData "logs") -ErrorAction SilentlyContinue | ForEach-Object { Get-Content $_.FullName }
                throw "O caixa adicional empacotado não respondeu em /health com mode = station"
            }
            foreach ($Path in @("/station/", "/station/static/station.js")) {
                $Page = Invoke-WebRequest -Uri ("http://127.0.0.1:8019" + $Path) -UseBasicParsing -TimeoutSec 5
                if ($Page.StatusCode -ne 200) { throw "O caixa adicional empacotado não serviu $Path" }
            }
            New-Item -ItemType File -Path $StopFile | Out-Null
            if (-not $Server.WaitForExit(20000)) { throw "O caixa adicional empacotado não parou com o pedido de parada" }
            Write-Host "Caixa adicional empacotado respondeu e parou corretamente."
        } finally {
            if (-not $Server.HasExited) { $Server.Kill() }
            Remove-Item Env:PDV_TERMINAL_DATA, Env:PDV_TERMINAL_PORT, Env:PDV_STOP_FILE, Env:PDV_VERSION, Env:PDV_MODE -ErrorAction SilentlyContinue
            Remove-Item $SmokeData -Recurse -Force -ErrorAction SilentlyContinue
        }
    }

    Write-Host "== Assinatura dos executáveis =="
    foreach ($Exe in @("PDV2.exe", "PDV2Server.exe", "PDV2Service.exe")) { Sign-File (Join-Path $App $Exe) }

    Write-Host "== Bundle de atualização =="
    Invoke-Checked "package_terminal (bundle)" { python scripts\package_terminal.py --out $Out --version $Version --frozen-dir $App }

    if (-not $SkipInstaller) {
        Write-Host "== Instalador (Inno Setup) =="
        $Iscc = Find-InnoSetup
        if (-not $Iscc) { throw "Inno Setup 6 não encontrado. Instale (https://jrsoftware.org/isdl.php) ou use -SkipInstaller." }
        Invoke-Checked "ISCC" { & $Iscc "/DAppVersion=$Version" "/DStageDir=$Stage" "/DOutputDir=$Out" (Join-Path $Root "installer\pdv2.iss") }
        $Installer = Join-Path $Out "PDV2-Instalar-$Version.exe"
        if (-not (Test-Path $Installer)) { throw "O Inno Setup não gerou $Installer" }
        Sign-File $Installer
        Invoke-Checked "package_terminal (instalador)" { python scripts\package_terminal.py --out $Out --version $Version --installer $Installer }
    }

    Write-Host ""
    Write-Host "Pronto. Arquivos em $Out :"
    Get-ChildItem $Out -File | ForEach-Object { Write-Host ("  {0}  ({1:N0} bytes)" -f $_.Name, $_.Length) }
    if (-not $script:Signing) { Write-Warning "Arquivos NÃO assinados." }
    Write-Host "Publique o .zip e/ou o .exe em HTTPS e cadastre os metadados (.metadata.json) no painel de versões."
} finally {
    Pop-Location
}
