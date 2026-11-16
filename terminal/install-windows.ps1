# LEGADO - instalador de DESENVOLVIMENTO. Não use em cliente.
# Exige Python 3.12 e internet, instala por usuário em %LOCALAPPDATA%\PDV2, sem serviço
# e sem atualização automática. O instalador comercial é gerado por installer\build.ps1
# (PDV2-Instalar-<versão>.exe) e migra os dados desta instalação para %ProgramData%\PDV2.
# Consulte docs\INSTALADOR-WINDOWS.md.
param(
    [string]$SourceRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")),
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA "PDV2"),
    [switch]$Autostart
)

$ErrorActionPreference = "Stop"
Write-Warning "Instalador de desenvolvimento (legado). Para clientes use o PDV2-Instalar.exe gerado por installer\build.ps1."
$Python = Get-Command py -ErrorAction SilentlyContinue
if (-not $Python) { throw "Python 3.12 não encontrado. Instale Python e execute novamente." }
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
$DataDir = Join-Path $InstallDir "data"
$Database = Join-Path $DataDir "terminal.sqlite3"
if (Test-Path $Database) {
    $BackupDir = Join-Path $DataDir "backups"
    New-Item -ItemType Directory -Force -Path $BackupDir | Out-Null
    $BackupFile = Join-Path $BackupDir ("before-update-" + (Get-Date -Format "yyyyMMdd-HHmmssfff") + ".sqlite3")
    $BackupCode = @'
import sqlite3, sys
source = sqlite3.connect(sys.argv[1])
target = sqlite3.connect(sys.argv[2])
try:
    source.backup(target)
    if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise RuntimeError("SQLite backup integrity check failed")
finally:
    target.close()
    source.close()
'@
    & py -3.12 -c $BackupCode $Database $BackupFile
    if ($LASTEXITCODE -ne 0) {
        Remove-Item $BackupFile -ErrorAction SilentlyContinue
        throw "Falha no backup do banco; atualização cancelada"
    }
    Write-Host "Backup do banco antes da atualização: $BackupFile"
}
$AppDir = Join-Path $InstallDir "app"
New-Item -ItemType Directory -Force -Path $AppDir | Out-Null
foreach ($Module in @("terminal", "shared")) {
    $Previous = Join-Path $AppDir $Module
    if (Test-Path $Previous) { Remove-Item $Previous -Recurse -Force }
}
Copy-Item (Join-Path $SourceRoot "terminal") $AppDir -Recurse -Force
Copy-Item (Join-Path $SourceRoot "shared") $AppDir -Recurse -Force
$Venv = Join-Path $InstallDir ".venv"
& py -3.12 -m venv $Venv
if ($LASTEXITCODE -ne 0) { throw "Falha ao criar ambiente Python" }
$Pip = Join-Path $Venv "Scripts\pip.exe"
& $Pip install --disable-pip-version-check -r (Join-Path $AppDir "terminal\requirements.txt")
if ($LASTEXITCODE -ne 0) { throw "Falha ao instalar dependências" }
$Launcher = Join-Path $InstallDir "PDV2-Terminal.cmd"
$PythonExe = Join-Path $Venv "Scripts\python.exe"
$LauncherText = "@echo off`r`nset `"PDV_TERMINAL_DATA=$DataDir`"`r`nset `"PDV_TERMINAL_DB=$DataDir\terminal.sqlite3`"`r`nset `"PYTHONPATH=$AppDir`"`r`n`"$PythonExe`" -m terminal`r`n"
[System.IO.File]::WriteAllText($Launcher, $LauncherText, [System.Text.Encoding]::ASCII)
if ($Autostart) {
    $Startup = [Environment]::GetFolderPath("Startup")
    $Shortcut = (New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $Startup "PDV2 Terminal.lnk"))
    $Shortcut.TargetPath = $Launcher
    $Shortcut.WorkingDirectory = $InstallDir
    $Shortcut.Save()
}
Write-Host "Instalado em $InstallDir"
Write-Host "Execute $Launcher e abra http://127.0.0.1:8010"
Write-Host "O instalador não foi testado em uma máquina Windows real; valide impressoras e permissões antes do piloto."
