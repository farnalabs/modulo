; Modulo Windows Installer — Inno Setup script
; Native Windows packaging is fail-closed until a real Windows bundle exists.
;
; Required ISPP defines:
;   VERSION_NUM - release version
;   BUNDLE_ARCH - amd64 (the only supported installer architecture)
;   BUNDLE_DIR  - extracted native Windows bundle root
; Optional ISPP define:
;   MODULO_URL  - local URL opened by the post-install checkbox
;
; Bundle contract:
;   BUNDLE_DIR\bin\modulo.exe
;   BUNDLE_DIR\modulo-runtime.json
; The launcher lives in bin\ because it resolves the bundle root as the parent of
; its own directory (runtime\, backend\, pg\, redis\ are siblings of bin\), so it
; installs to {app}\bin\modulo.exe. The marker is part of the native launcher contract. It must be shipped by
; the real runtime build and is deliberately required at compile time.
;
; Build example:
;   iscc /DVERSION_NUM=0.4.0 /DBUNDLE_ARCH=amd64 /DBUNDLE_DIR=C:\bundle modulo.iss
;
; The [Code] section is Inno Setup 6.2.2 Pascal Script. Only documented support
; functions are used; there is no WinAPI import. scripts/test-windows-installer.ps1
; compiles this file with ISCC 6.2.2 in CI, so a script that cannot build fails
; the build rather than shipping.
;
; Two rules the 6.2.2 compiler enforces that are easy to trip over:
;   1. A line whose first non-blank character is "#" is an ISPP directive, so a
;      character-code literal may never start a line (#13#10 belongs mid-line).
;   2. Only the functions in Inno Setup's support-function tables exist. There
;      is no Randomize (Setup seeds the generator itself at startup), and
;      dynamic arrays are resized with SetArrayLength, not SetLength.

#ifndef VERSION_NUM
  #error VERSION_NUM is required
#endif
#ifndef BUNDLE_ARCH
  #error BUNDLE_ARCH is required
#endif
#ifndef BUNDLE_DIR
  #error BUNDLE_DIR is required
#endif
#ifndef MODULO_URL
  #define MODULO_URL "http://127.0.0.1:8000"
#endif

#if BUNDLE_ARCH != "amd64"
  #error Unsupported BUNDLE_ARCH: only amd64 Windows installers are supported
#endif

; A line that begins with "#" is an ISPP directive, and a directive's argument is
; an ISPP expression - inline {#name} substitution only happens on ordinary
; lines. So the bundle checks below use BUNDLE_DIR as an expression rather than
; "{#BUNDLE_DIR}", which would test for a file literally named {#BUNDLE_DIR}.
#ifexist BUNDLE_DIR + "\bin\modulo.exe"
#else
  #error Required native runtime executable is missing from the bundle (BUNDLE_DIR\bin\modulo.exe)
#endif
#ifexist BUNDLE_DIR + "\modulo-runtime.json"
#else
  #error Required native runtime marker is missing from the bundle (BUNDLE_DIR\modulo-runtime.json)
#endif

[Setup]
AppName=Modulo
AppVersion={#VERSION_NUM}
AppPublisher=Farna Labs Ltd
AppPublisherURL=https://modulo.run
AppSupportURL=https://modulo.run/docs
AppUpdatesURL=https://modulo.run/changelog
DefaultDirName={autopf}\Modulo
DefaultGroupName=Modulo
OutputDir=build/windows
OutputBaseFilename=modulo-{#VERSION_NUM}-windows-{#BUNDLE_ARCH}
Compression=lzma2/ultra64
SolidCompression=yes
PrivilegesRequired=admin
; x64compatible was introduced by Inno Setup 6.3, which deprecated x64. The
; release workflow still pins 6.2.2, which only knows x64, so the identifier is
; version-gated rather than swapped: the newer compiler then emits no
; deprecation warning and the older one still compiles.
#if VER >= EncodeVer(6,3,0)
ArchitecturesInstallIn64BitMode=x64compatible
ArchitecturesAllowed=x64compatible
#else
ArchitecturesInstallIn64BitMode=x64
ArchitecturesAllowed=x64
#endif
DisableProgramGroupPage=yes
DisableDirPage=auto
DisableReadyMemo=yes
DisableReadyPage=yes
DisableFinishedPage=no
WizardStyle=modern
AppId={{7B7A9F3B-69DA-4EA0-9EB8-0D8F2D4B8A6B}}
LicenseFile={#SourcePath}\..\..\LICENSE
InfoBeforeFile={#SourcePath}\INSTALLER-INFO.txt
Uninstallable=yes
UninstallDisplayIcon={app}\bin\modulo.exe
UninstallDisplayName=Modulo {#VERSION_NUM}
CloseApplications=yes
RestartApplications=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; The compile-time contract above is the primary guard. These explicit entries
; keep the installer payload contract visible and prevent optional-source flags
; from turning a missing runtime into a partial installation.
Source: "{#BUNDLE_DIR}\bin\modulo.exe"; DestDir: "{app}\bin"; Flags: ignoreversion
Source: "{#BUNDLE_DIR}\modulo-runtime.json"; DestDir: "{app}"; Flags: ignoreversion
; Everything else the bundle ships. skipifsourcedoesntexist is deliberate: the
; declared bundle contract is exactly the two files above, so a conforming
; bundle leaves this wildcard with nothing to copy, which Inno Setup otherwise
; rejects as "No files found matching ...".
Source: "{#BUNDLE_DIR}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs skipifsourcedoesntexist; Excludes: "bin\modulo.exe,modulo-runtime.json"

[Icons]
Name: "{group}\Modulo"; Filename: "{app}\bin\modulo.exe"; WorkingDir: "{app}"
Name: "{commondesktop}\Modulo"; Filename: "{app}\bin\modulo.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
; Service registration lives in [Code] (RegisterService) rather than here, so
; that a failed "sc.exe create/start" is observable and can be turned into a
; support block instead of being silently ignored by the [Run] runner.
Filename: "{#MODULO_URL}"; Description: "Open Modulo in your browser"; Flags: postinstall shellexec unchecked; Check: ShouldOpenModulo

[UninstallRun]
Filename: "sc.exe"; Parameters: "stop Modulo"; StatusMsg: "Stopping Modulo service..."; Flags: runhidden waituntilterminated; RunOnceId: "StopModuloService"
Filename: "sc.exe"; Parameters: "delete Modulo"; StatusMsg: "Removing Modulo service..."; Flags: runhidden waituntilterminated; RunOnceId: "DeleteModuloService"

[UninstallDelete]
Type: filesandordirs; Name: "{app}"

[Code]
const
  MAX_INSTALL_LOGS = 5;
  MAX_LOG_BYTES = 1048576; { approximate cap on one install transcript }
  LOG_DIR_NAME = 'Modulo';
  LOG_PREFIX = 'install-';
  LOG_EXT = '.log';
  SUPPORT_REF_LENGTH = 8;
  HEX_CHARS = '0123456789abcdef';

var
  SupportRef: string;
  LogFilePath: string;
  CurrentStage: string;
  LogActive: Boolean;
  LogBytes: Integer;
  TruncationNoted: Boolean;
  InstallFailed: Boolean;
  InstallCompleted: Boolean;
  UserCancelled: Boolean;
  SupportBlockShown: Boolean;

function GetLogDir: string;
begin
  Result := ExpandConstant('{localappdata}\' + LOG_DIR_NAME);
end;

{ Named WriteInstallLog, not Log: "Log" is an Inno Setup built-in support
  function and declaring our own would shadow it. }
procedure WriteInstallLog(const Msg: string);
var
  Line: string;
begin
  if not LogActive then
    Exit;
  Line := GetDateTimeString('yyyy-mm-dd hh:nn:ss', '-', ':') + ' ' + Msg + #13#10;
  if LogBytes + Length(Line) > MAX_LOG_BYTES then
  begin
    if not TruncationNoted then
    begin
      TruncationNoted := True;
      SaveStringToFile(LogFilePath,
        GetDateTimeString('yyyy-mm-dd hh:nn:ss', '-', ':') +
        ' [log truncated: transcript capped at ' + IntToStr(MAX_LOG_BYTES) +
        ' characters; later output discarded]' + #13#10, True);
    end;
    Exit;
  end;
  { Append via SaveStringToFile: Pascal Script has no file-handle API, and
    opening one handle per write would leak it. }
  if SaveStringToFile(LogFilePath, Line, True) then
    LogBytes := LogBytes + Length(Line)
  else
    LogActive := False;
end;

function EnsureLogDir: Boolean;
var
  LogDir: string;
begin
  LogDir := GetLogDir;
  if DirExists(LogDir) then
    Result := True
  else
    Result := ForceDirectories(LogDir);
end;

procedure PruneInstallLogs;
var
  LogDir: string;
  FindRec: TFindRec;
  Names: TArrayOfString;
  Count, I, J: Integer;
  TempName: string;
begin
  LogDir := GetLogDir;
  if not DirExists(LogDir) then
    Exit;
  Count := 0;
  if FindFirst(LogDir + '\' + LOG_PREFIX + '*' + LOG_EXT, FindRec) then
  begin
    try
      repeat
        { FindFirst also returns directories; a directory is never a log file. }
        if not DirExists(LogDir + '\' + FindRec.Name) then
        begin
          SetArrayLength(Names, Count + 1);
          Names[Count] := FindRec.Name;
          Count := Count + 1;
        end;
      until not FindNext(FindRec);
    finally
      FindClose(FindRec);
    end;
  end;

  { Called before the current run creates its own log, so keep one slot free. }
  if Count < MAX_INSTALL_LOGS then
    Exit;

  { Log names embed an ISO-8601 timestamp, so a descending string sort is a
    newest-first sort. }
  for I := 0 to Count - 2 do
    for J := 0 to Count - 2 - I do
      if CompareText(Names[J], Names[J + 1]) < 0 then
      begin
        TempName := Names[J];
        Names[J] := Names[J + 1];
        Names[J + 1] := TempName;
      end;

  for I := MAX_INSTALL_LOGS - 1 to Count - 1 do
    DeleteFile(LogDir + '\' + Names[I]);
end;

function GenerateSupportRef: string;
var
  I: Integer;
  S: string;
begin
  { A support reference is a correlation id printed in the log, not a secret,
    so Random() is sufficient and needs no WinAPI. Pascal Script has no
    Randomize: Setup calls it itself in InitializeCommonVars before any script
    runs, so the generator is already seeded when this executes. }
  S := '';
  for I := 1 to SUPPORT_REF_LENGTH do
    S := S + Copy(HEX_CHARS, Random(16) + 1, 1);
  Result := S;
end;

function GetTargetDescription: string;
begin
  { The version is known from the ISPP define }
  Result := 'bundle-v{#VERSION_NUM} (windows-{#BUNDLE_ARCH})';
end;

{ Everything between the REDACT-SOURCE markers is extracted verbatim by
  scripts/test-windows-installer.ps1, compiled with Free Pascal and executed
  against fixture cases. Keep this block free of Inno-only constructs. }
{ BEGIN REDACT-SOURCE }
function CharAt(const S: string; Index: Integer): Char;
begin
  if (Index >= 1) and (Index <= Length(S)) then
    Result := S[Index]
  else
    Result := #0;
end;

function FindFrom(const Needle, Haystack: string; Start: Integer): Integer;
var
  P: Integer;
begin
  if (Start < 1) or (Start > Length(Haystack)) then
  begin
    Result := 0;
    Exit;
  end;
  P := Pos(Needle, Copy(Haystack, Start, Length(Haystack) - Start + 1));
  if P = 0 then
    Result := 0
  else
    Result := Start + P - 1;
end;

function IsAuthorityDelimiter(C: Char): Boolean;
begin
  Result := (C = #0) or (C = '/') or (C = ' ') or (C = '@') or (C = '"') or
            (C = #13) or (C = #10);
end;

function IsValueDelimiter(C: Char): Boolean;
begin
  Result := (C = #0) or (C = ' ') or (C = '"') or (C = '''') or (C = ',') or
            (C = ';') or (C = ')') or (C = #13) or (C = #10);
end;

{ Replaces the value of one credential-shaped key ("key=value", "key: value")
  with [redacted]. Case-insensitive on the key, literal on the value. }
function RedactKeyValue(const Text, Token: string): string;
var
  S: string;
  P, ValueStart, ValueEnd: Integer;
begin
  S := Text;
  P := FindFrom(Token, Lowercase(S), 1);
  while P > 0 do
  begin
    ValueStart := P + Length(Token);
    while CharAt(S, ValueStart) = ' ' do
      ValueStart := ValueStart + 1;
    if (CharAt(S, ValueStart) = '=') or (CharAt(S, ValueStart) = ':') then
    begin
      ValueStart := ValueStart + 1;
      while (CharAt(S, ValueStart) = ' ') or (CharAt(S, ValueStart) = '"') or
            (CharAt(S, ValueStart) = '''') do
        ValueStart := ValueStart + 1;
      { Keep the authentication scheme visible; only the credential goes. }
      if Copy(Lowercase(S), ValueStart, 7) = 'bearer ' then
        ValueStart := ValueStart + 7
      else if Copy(Lowercase(S), ValueStart, 6) = 'basic ' then
        ValueStart := ValueStart + 6;
      ValueEnd := ValueStart;
      while not IsValueDelimiter(CharAt(S, ValueEnd)) do
        ValueEnd := ValueEnd + 1;
      if ValueEnd > ValueStart then
      begin
        Delete(S, ValueStart, ValueEnd - ValueStart);
        Insert('[redacted]', S, ValueStart);
        P := ValueStart + Length('[redacted]');
      end
      else
        P := ValueStart + 1;
    end
    else
      P := P + Length(Token);
    P := FindFrom(Token, Lowercase(S), P);
  end;
  Result := S;
end;

{ Removes credentials from installer diagnostics before they reach the install
  log or the support block. Two shapes are handled:
    1. scheme://user:password@host  ->  scheme://[redacted]@host
    2. key=value / key: value for a conservative list of credential keys.
  StringChangeEx is literal-only, so this is a real scanner, not a regex. }
function Redact(const Text: string): string;
var
  S: string;
  P, Q: Integer;
begin
  S := Text;

  P := FindFrom('://', S, 1);
  while P > 0 do
  begin
    Q := P + 3;
    while not IsAuthorityDelimiter(CharAt(S, Q)) do
      Q := Q + 1;
    if CharAt(S, Q) = '@' then
    begin
      { Only userinfo that carries a password separator is a credential. }
      if Pos(':', Copy(S, P + 3, Q - (P + 3))) > 0 then
      begin
        Delete(S, P + 3, Q - (P + 3));
        Insert('[redacted]', S, P + 3);
        Q := P + 3 + Length('[redacted]');
      end
      else
        Q := Q + 1;
    end
    else
      Q := P + 3;
    P := FindFrom('://', S, Q);
  end;

  S := RedactKeyValue(S, 'password');
  S := RedactKeyValue(S, 'passwd');
  S := RedactKeyValue(S, 'token');
  S := RedactKeyValue(S, 'secret');
  S := RedactKeyValue(S, 'api_key');
  S := RedactKeyValue(S, 'apikey');
  S := RedactKeyValue(S, 'api-key');
  S := RedactKeyValue(S, 'authorization');
  S := RedactKeyValue(S, 'access_key');
  S := RedactKeyValue(S, 'signature');
  S := RedactKeyValue(S, 'credential');
  S := RedactKeyValue(S, 'credentials');

  Result := S;
end;
{ END REDACT-SOURCE }

procedure PrintSupportBlock;
var
  TargetDesc: string;
  LogDisplay: string;
  Msg: string;
begin
  if SupportBlockShown then
    Exit;
  SupportBlockShown := True;
  TargetDesc := GetTargetDescription;
  if LogFilePath <> '' then
    LogDisplay := LogFilePath
  else
    LogDisplay := '(no log was created - the installer failed before logging started)';
  Msg := #13#10 +
    '----- Modulo install failed: support reference ' + SupportRef + ' -----' + #13#10 +
    '  Install target   : ' + Redact(TargetDesc) + #13#10 +
    '  Failing stage    : ' + Redact(CurrentStage) + #13#10 +
    '  Install log      : ' + Redact(LogDisplay) + #13#10 +
    '  Support reference: ' + SupportRef + #13#10 + #13#10 +
    'Send this block AND the install log file above to Modulo support so the' + #13#10 +
    'failure can be diagnosed: https://github.com/farnalabs/modulo/issues' + #13#10 +
    '----- end of Modulo install support block -----' + #13#10;
  WriteInstallLog(Msg);
  { MsgBox from [Code] is not suppressible by /SUPPRESSMSGBOXES (Inno Setup
    documents it among the five non-suppressible boxes), so an unattended
    /SILENT install would block on it. The transcript above already carries the
    support block; skip the dialog when Setup is running silently. }
  if not WizardSilent then
    MsgBox(Msg, mbError, MB_OK);
end;

procedure SetStage(const Stage: string);
begin
  CurrentStage := Stage;
  WriteInstallLog('Stage: ' + Stage);
end;

procedure MarkFailure(const Stage, Detail: string);
begin
  InstallFailed := True;
  CurrentStage := Stage;
  WriteInstallLog('FAILURE: ' + Detail);
end;

{ Registers the Windows service and reports whether it actually started, because
  the [Run] section cannot report a failing sc.exe. }
function RegisterService: Boolean;
var
  ResultCode: Integer;
  ServiceExe: string;
begin
  Result := False;
  ServiceExe := ExpandConstant('{app}') + '\bin\modulo.exe';

  SetStage('service registration (create)');
  { Only the executable path is quoted. The launcher argument must sit OUTSIDE
    those quotes so sc.exe stores ImagePath as the executable, not as
    "<path> service" - a path that does not exist. }
  if not Exec('sc.exe',
    'create Modulo binPath= "' + ServiceExe + '" service start= auto',
    '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
  begin
    WriteInstallLog('sc.exe create could not be started');
    Exit;
  end;
  if ResultCode <> 0 then
  begin
    WriteInstallLog('sc.exe create failed with exit code ' + IntToStr(ResultCode));
    Exit;
  end;

  SetStage('service registration (start)');
  if not Exec('sc.exe', 'start Modulo', '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
  begin
    WriteInstallLog('sc.exe start could not be started');
    Exit;
  end;
  if ResultCode <> 0 then
  begin
    WriteInstallLog('sc.exe start failed with exit code ' + IntToStr(ResultCode));
    Exit;
  end;

  Result := True;
end;

function InitializeSetup: Boolean;
begin
  SupportRef := GenerateSupportRef;
  CurrentStage := 'starting up (reading arguments)';
  LogFilePath := '';
  LogActive := False;
  LogBytes := 0;
  TruncationNoted := False;
  InstallFailed := False;
  InstallCompleted := False;
  UserCancelled := False;
  SupportBlockShown := False;

  if EnsureLogDir then
  begin
    PruneInstallLogs;
    LogFilePath := GetLogDir + '\' + LOG_PREFIX +
      GetDateTimeString('yyyymmddThhnnssZ', '-', ':') + '-' + SupportRef + LOG_EXT;
    if FileExists(LogFilePath) then
      DeleteFile(LogFilePath);
    LogActive := True;
    LogBytes := 0;
    WriteInstallLog('Modulo installer started. Support reference: ' + SupportRef);
    WriteInstallLog('Version: ' + '{#VERSION_NUM}');
    WriteInstallLog('Architecture: ' + '{#BUNDLE_ARCH}');
    WriteInstallLog('Stage: ' + CurrentStage);
  end;
  { Logging is best-effort: when the directory cannot be created the install
    still proceeds, and PrintSupportBlock says no log was captured. }

  Result := True;
end;

procedure DeinitializeSetup;
begin
  if InstallFailed then
  begin
    if not UserCancelled then
      PrintSupportBlock;
  end
  else if InstallCompleted then
    WriteInstallLog('Installer finished successfully.')
  else if not UserCancelled then
  begin
    { Setup terminated without reaching ssDone: a fatal error, not a cancel. }
    InstallFailed := True;
    PrintSupportBlock;
  end;
  LogActive := False;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  if CurPageID = wpWelcome then
    SetStage('welcome page')
  else if CurPageID = wpLicense then
    SetStage('license agreement')
  else if CurPageID = wpSelectDir then
    SetStage('select install directory')
  else if CurPageID = wpSelectTasks then
    SetStage('select additional tasks')
  else if CurPageID = wpReady then
    SetStage('ready to install');
  Result := True;
end;

procedure CancelButtonClick(CurPageID: Integer; var Cancel, Confirm: Boolean);
begin
  { A user cancel is not an install failure and must not print the support block. }
  UserCancelled := True;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssInstall then
    SetStage('installing files')
  else if CurStep = ssPostInstall then
  begin
    SetStage('post-install (service registration)');
    if not RegisterService then
    begin
      MarkFailure('post-install (service registration)',
        'the Modulo service could not be registered or started');
      PrintSupportBlock;
      Abort;
    end;
  end
  else if CurStep = ssDone then
  begin
    SetStage('finished');
    InstallCompleted := True;
  end;
end;

function ShouldOpenModulo: Boolean;
begin
  Result := not IsUninstaller;
end;
