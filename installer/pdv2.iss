; Instalador do PDV2 para Windows (Inno Setup 6). Gerado por installer\build.ps1:
;   ISCC /DAppVersion=2.0.1 /DStageDir=...\dist\windows\stage /DOutputDir=...\dist\windows pdv2.iss
;
; Na máquina:
;   {app}\app-<versão>\   programa (uma pasta por versão; a atualização automática cria as próximas)
;   {app}\bin\            cópia estável: serviço PDV2Service e alvo dos atalhos
;   {app}\current.txt     versão em uso
;   {commonappdata}\PDV2  dados (banco, backups, registros, downloads) - NUNCA removidos
;
; ATENÇÃO: este roteiro ainda não foi compilado nem executado em Windows real.
; Siga a lista de testes manuais de docs\INSTALADOR-WINDOWS.md antes de entregar.

#ifndef AppVersion
  #define AppVersion "2.1.0"
#endif
#ifndef StageDir
  #define StageDir "..\dist\windows\stage"
#endif
#ifndef OutputDir
  #define OutputDir "..\dist\windows"
#endif
#define ServiceName "PDV2Service"
#define FirewallRule "PDV2 - Servidor da loja"
#define TerminalPort "8010"
#define LegacyMarker "\AppData\Local\PDV2\.venv"
#define LegacyRunningExit "3"

[Setup]
AppId={{6F0B8E7C-52C1-4C55-9A3D-0E7B1F4C2D91}
AppName=PDV2
AppVersion={#AppVersion}
AppVerName=PDV2 {#AppVersion}
AppPublisher=CrownTech
DefaultDirName={autopf}\PDV2
DisableDirPage=yes
DefaultGroupName=PDV2
DisableProgramGroupPage=yes
DisableReadyPage=no
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.17763
OutputDir={#OutputDir}
OutputBaseFilename=PDV2-Instalar-{#AppVersion}
SetupIconFile=pdv2.ico
UninstallDisplayIcon={app}\bin\PDV2.exe
UninstallDisplayName=PDV2
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=no
RestartApplications=no
VersionInfoVersion={#AppVersion}.0
VersionInfoCompany=CrownTech
VersionInfoDescription=Instalador do PDV2

[Languages]
Name: "ptbr"; MessagesFile: "compiler:Languages\BrazilianPortuguese.isl"

[Tasks]
Name: "desktopicon"; Description: "Criar atalho na área de trabalho"
; Rede entre caixas (TCP 8011 e UDP 8012). Sem marcar, o serviço cria a mesma regra sozinho quando o
; caixa principal passa a ter caixas adicionais (terminal\lan.py, ensure_firewall).
; A regra vale para todos os perfis de rede (Particular, Domínio e Pública), sempre limitada à sub-rede local:
; com a rede da loja marcada como Pública os caixas continuam se enxergando (docs\ESTACOES.md, 1.1).
Name: "lanserver"; Description: "Este computador será o caixa principal de uma loja com mais de um caixa (libera a rede local para os caixas adicionais)"; Flags: unchecked

[Dirs]
Name: "{commonappdata}\PDV2"

[InstallDelete]
; Um download ou uma troca de versão pela metade não vale para a instalação nova.
Type: filesandordirs; Name: "{commonappdata}\PDV2\updates"
Type: filesandordirs; Name: "{commonappdata}\PDV2\run"

[Files]
Source: "{#StageDir}\app-{#AppVersion}\*"; DestDir: "{app}\app-{#AppVersion}"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "{#StageDir}\app-{#AppVersion}\*"; DestDir: "{app}\bin"; Excludes: "bundle.json"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{autoprograms}\PDV2"; Filename: "{app}\bin\PDV2.exe"; WorkingDir: "{app}\bin"; Comment: "Abrir o caixa PDV2"
Name: "{autodesktop}\PDV2"; Filename: "{app}\bin\PDV2.exe"; WorkingDir: "{app}\bin"; Comment: "Abrir o caixa PDV2"; Tasks: desktopicon

[Run]
; Apaga antes de criar: "netsh add" não substitui, e reinstalar com a opção marcada duplicaria a regra.
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""{#FirewallRule}"""; Flags: runhidden; Tasks: lanserver
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""{#FirewallRule}"" dir=in action=allow protocol=TCP localport=8011 remoteip=localsubnet profile=any"; Flags: runhidden; Tasks: lanserver
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""{#FirewallRule}"" dir=in action=allow protocol=UDP localport=8012 remoteip=localsubnet profile=any"; Flags: runhidden; Tasks: lanserver
Filename: "{app}\bin\PDV2.exe"; Description: "Abrir o PDV2 agora"; Flags: postinstall nowait skipifsilent runasoriginaluser

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM PDV2.exe"; Flags: runhidden; RunOnceId: "PDV2Launcher"
Filename: "{app}\bin\PDV2Service.exe"; Parameters: "--uninstall"; Flags: runhidden waituntilterminated; RunOnceId: "PDV2Service"
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""{#FirewallRule}"""; Flags: runhidden; RunOnceId: "PDV2Firewall"

[Code]
function ProfilesDir(): String;
begin
  { C:\Users\Public\Documents -> C:\Users }
  Result := ExtractFileDir(ExtractFileDir(ExpandConstant('{commondocs}')));
end;

procedure RunHidden(const FileName, Params: String);
var
  Code: Integer;
begin
  Exec(FileName, Params, '', SW_HIDE, ewWaitUntilTerminated, Code);
end;

function RunHiddenCode(const FileName, Params: String): Integer;
var
  Code: Integer;
begin
  Result := -1;
  if Exec(FileName, Params, '', SW_HIDE, ewWaitUntilTerminated, Code) then
    Result := Code;
end;

function PowerShellExe(): String;
begin
  Result := ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe');
end;

{ The old per-user install (terminal\install-windows.ps1) starts at logon as
  "<LOCALAPPDATA>\PDV2\.venv\Scripts\python.exe -m terminal" on the same port.
  Same rule as is_legacy_process in terminal\service_win.py. }
procedure StopLegacyTerminal();
begin
  RunHidden(PowerShellExe(),
    '-NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "' +
    'Get-CimInstance Win32_Process | Where-Object { $_.ProcessId -ne $PID -and $_.Name -like ''python*'' -and ' +
    '(($_.ExecutablePath -like ''*{#LegacyMarker}\*'') -or ($_.CommandLine -like ''*{#LegacyMarker}\*'')) } | ' +
    'ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"');
end;

{ True while anything still listens on the terminal port. }
function TerminalPortBusy(): Boolean;
begin
  Result := RunHiddenCode(PowerShellExe(),
    '-NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "' +
    '$c = New-Object Net.Sockets.TcpClient; ' +
    'try { $c.Connect(''127.0.0.1'', {#TerminalPort}); $c.Close(); exit 1 } catch { exit 0 }"') = 1;
end;

function WaitTerminalPortFree(): Boolean;
var
  Attempt: Integer;
begin
  Result := True;
  for Attempt := 1 to 10 do
  begin
    if not TerminalPortBusy() then
      exit;
    Sleep(500);
  end;
  Result := False;
end;

procedure StopRunningCopy();
begin
  { "net stop" waits for the clean stop. The service itself is never killed:
    Windows would restart it (recovery policy) in the middle of the copy. }
  RunHidden(ExpandConstant('{sys}\net.exe'), 'stop {#ServiceName}');
  RunHidden(ExpandConstant('{sys}\taskkill.exe'), '/F /IM PDV2.exe');
  RunHidden(ExpandConstant('{sys}\taskkill.exe'), '/F /IM PDV2Server.exe');
  Sleep(1000);
end;

procedure RemoveOtherVersions();
var
  Found: TFindRec;
  Root: String;
begin
  Root := ExpandConstant('{app}');
  if FindFirst(Root + '\app-*', Found) then
  begin
    try
      repeat
        if ((Found.Attributes and FILE_ATTRIBUTE_DIRECTORY) <> 0) and (Found.Name <> 'app-{#AppVersion}') then
          DelTree(Root + '\' + Found.Name, True, True, True);
      until not FindNext(Found);
    finally
      FindClose(Found);
    end;
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  StopRunningCopy();
  StopLegacyTerminal();
  { Nothing was copied or migrated yet: stopping here leaves the machine as it was. }
  if not WaitTerminalPortFree() then
  begin
    Result := 'O PDV2 antigo ainda está aberto neste computador (a porta {#TerminalPort} continua em uso).' + #13#10 + #13#10 +
      'Feche o PDV2 antigo - a janela preta "PDV2-Terminal" ou o processo python.exe no Gerenciador de Tarefas - ' +
      'e execute o instalador de novo. Nenhum dado foi alterado.';
    exit;
  end;
  RemoveOtherVersions();
end;

{ Old per-user installs: terminal\install-windows.ps1 used %LOCALAPPDATA%\PDV2\data
  and the first terminals used %LOCALAPPDATA%\PDV2. }
function LegacyCandidates(): String;
var
  Found: TFindRec;
  Users, Base: String;
begin
  Result := '';
  Base := ExpandConstant('{localappdata}\PDV2');
  if FileExists(Base + '\data\terminal.sqlite3') then Result := Result + ' "' + Base + '\data"';
  if FileExists(Base + '\terminal.sqlite3') then Result := Result + ' "' + Base + '"';
  Users := ProfilesDir();
  if FindFirst(Users + '\*', Found) then
  begin
    try
      repeat
        if ((Found.Attributes and FILE_ATTRIBUTE_DIRECTORY) <> 0) and (Found.Name <> '.') and (Found.Name <> '..') then
        begin
          Base := Users + '\' + Found.Name + '\AppData\Local\PDV2';
          if FileExists(Base + '\data\terminal.sqlite3') then Result := Result + ' "' + Base + '\data"';
          if FileExists(Base + '\terminal.sqlite3') then Result := Result + ' "' + Base + '"';
          DeleteFile(Users + '\' + Found.Name + '\AppData\Roaming\Microsoft\Windows\Start Menu\Programs\Startup\PDV2 Terminal.lnk');
        end;
      until not FindNext(Found);
    finally
      FindClose(Found);
    end;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Data, Service, Legacy: String;
begin
  if CurStep <> ssPostInstall then
    exit;
  Data := ExpandConstant('{commonappdata}\PDV2');
  Service := ExpandConstant('{app}\bin\PDV2Service.exe');
  { Only the service (SYSTEM) and administrators read the database, which holds the
    device token and PIN hashes. SIDs keep this independent of the Windows language. }
  RunHidden(ExpandConstant('{sys}\icacls.exe'),
    '"' + Data + '" /inheritance:r /grant:r *S-1-5-18:(OI)(CI)F *S-1-5-32-544:(OI)(CI)F');
  if not FileExists(Data + '\terminal.sqlite3') then
  begin
    Legacy := LegacyCandidates();
    if (Legacy <> '') and (RunHiddenCode(Service, '--migrate-legacy' + Legacy) = {#LegacyRunningExit}) then
    begin
      { The old terminal came back after PrepareToInstall. Without a database in the
        new folder the next run of the installer migrates again, so the service is
        not registered now (it would create an empty database). }
      SuppressibleMsgBox('O PDV2 antigo voltou a abrir durante a instalação e os dados dele NÃO foram copiados.' + #13#10 + #13#10 +
        'Feche o PDV2 antigo e execute o instalador de novo. Até lá o PDV2 novo não será iniciado.',
        mbError, MB_OK, IDOK);
      exit;
    end;
  end;
  SaveStringToFile(ExpandConstant('{app}\current.txt'), '{#AppVersion}' + #10, False);
  { Registers PDV2Service (automatic start, restart on failure) and starts it. }
  RunHidden(Service, '--install');
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
  begin
    { Versions created by the automatic update are not in the uninstall log. }
    DelTree(ExpandConstant('{app}'), True, True, True);
    if not UninstallSilent() then
      MsgBox('O PDV2 foi removido. Os dados da loja (vendas, backups e configurações) foram mantidos em ' +
        ExpandConstant('{commonappdata}\PDV2') + '.' + #13#10 + #13#10 +
        'Apague essa pasta manualmente apenas se tiver certeza de que não precisa mais deles.',
        mbInformation, MB_OK);
  end;
end;
