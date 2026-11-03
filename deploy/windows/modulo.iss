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
;   BUNDLE_DIR\modulo.exe
;   BUNDLE_DIR\modulo-runtime.json
; The marker is part of the native launcher contract. It must be shipped by
; the real runtime build and is deliberately required at compile time.
;
; Build example:
;   iscc /DVERSION_NUM=0.4.0 /DBUNDLE_ARCH=amd64 /DBUNDLE_DIR=C:\bundle modulo.iss

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

#ifexist "{#BUNDLE_DIR}\modulo.exe"
#else
  #error Required native runtime executable is missing: {#BUNDLE_DIR}\modulo.exe
#endif
#ifexist "{#BUNDLE_DIR}\modulo-runtime.json"
#else
  #error Required native runtime marker is missing: {#BUNDLE_DIR}\modulo-runtime.json
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
ArchitecturesInstallIn64BitMode=x64
ArchitecturesAllowed=x64
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
UninstallDisplayIcon={app}\modulo.exe
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
Source: "{#BUNDLE_DIR}\modulo.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#BUNDLE_DIR}\modulo-runtime.json"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#BUNDLE_DIR}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs; Excludes: "modulo.exe,modulo-runtime.json"

[Icons]
Name: "{group}\Modulo"; Filename: "{app}\modulo.exe"; WorkingDir: "{app}"
Name: "{commondesktop}\Modulo"; Filename: "{app}\modulo.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
; The service contract is modulo.exe service. Runtime readiness and the local
; HTTP listener remain the native launcher's responsibility.
Filename: "sc.exe"; Parameters: "create Modulo binPath= \"{app}\modulo.exe service\" start= auto"; StatusMsg: "Installing Modulo service..."; Flags: runhidden waituntilterminated
Filename: "sc.exe"; Parameters: "start Modulo"; StatusMsg: "Starting Modulo service..."; Flags: runhidden waituntilterminated
Filename: "{#MODULO_URL}"; Description: "Open Modulo in your browser"; Flags: postinstall shellexec unchecked; Check: ShouldOpenModulo

[UninstallRun]
Filename: "sc.exe"; Parameters: "stop Modulo"; StatusMsg: "Stopping Modulo service..."; Flags: runhidden waituntilterminated
Filename: "sc.exe"; Parameters: "delete Modulo"; StatusMsg: "Removing Modulo service..."; Flags: runhidden waituntilterminated

[UninstallDelete]
Type: filesandordirs; Name: "{app}"

[Code]
const
  MAX_INSTALL_LOGS = 5;
  MAX_LOG_BYTES = 1048576; { 1 MB }
  LOG_DIR_NAME = 'Modulo';
  LOG_PREFIX = 'install-';
  LOG_EXT = '.log';
  SUPPORT_REF_LENGTH = 8;

var
  SupportRef: string;
  LogFilePath: string;
  LogFileHandle: Integer;
  CurrentStage: string;
  LogActive: Boolean;
  InstallFailed: Boolean;

function GenerateSupportRef: string;
var
  Bytes: array of Byte;
  I: Integer;
  S: string;
begin
  SetArrayLength(Bytes, 4);
  { Use cryptographically random bytes where available }
  if not CryptGenRandom(Bytes) then begin
    { Fallback: pseudo-random }
    Randomize;
    for I := 0 to 3 do
      Bytes[I] := Random(256);
  end;
  S := '';
  for I := 0 to 3 do
    S := S + IntToHex(Bytes[I], 2);
  Result := Lowercase(S);
end;

function GetLogDir: string;
begin
  Result := ExpandConstant('{localappdata}\' + LOG_DIR_NAME);
end;

function EnsureLogDir: Boolean;
var
  LogDir: string;
begin
  LogDir := GetLogDir;
  if not DirExists(LogDir) then begin
    Result := ForceDirectories(LogDir);
    if Result then
      SetFilePermissions(LogDir, 'ALL', True); { Best-effort permissions }
  end else
    Result := True;
  if not Result then
    Log('WARNING: cannot create log directory ' + LogDir);
end;

procedure PruneInstallLogs;
var
  LogDir: string;
  Files: TArrayOfString;
  KeepCount: Integer;
begin
  LogDir := GetLogDir;
  if not DirExists(LogDir) then Exit;
  KeepCount := MAX_INSTALL_LOGS - 1;
  if KeepCount < 1 then KeepCount := 1;
  if FindFiles(Files, LogDir + '\' + LOG_PREFIX + '*' + LOG_EXT, False) then begin
    DeleteOldFiles(LogDir, LOG_PREFIX + '*' + LOG_EXT, KeepCount);
  end;
end;

function InitializeSetup: Boolean;
begin
  { Generate support reference early }
  SupportRef := GenerateSupportRef;
  CurrentStage := 'starting up (reading arguments)';
  LogActive := False;
  InstallFailed := False;

  { Try to initialize logging }
  if EnsureLogDir then begin
    PruneInstallLogs;
    LogFilePath := GetLogDir + '\' + LOG_PREFIX + GetDateTimeString('yyyymmdd"T"hhmmss"Z"', '#', '#') + '-' + SupportRef + LOG_EXT;
    LogFileHandle := FileCreate(LogFilePath);
    if LogFileHandle <> -1 then begin
      FileClose(LogFileHandle);
      LogFileHandle := FileOpen(LogFilePath, fmOpenWrite or fmShareDenyNone);
      if LogFileHandle <> -1 then begin
        LogActive := True;
        Log('Modulo installer started. Support reference: ' + SupportRef);
        Log('Version: ' + '{#VERSION_NUM}');
        Log('Architecture: ' + '{#BUNDLE_ARCH}');
        Log('Stage: ' + CurrentStage);
      end else begin
        Log('WARNING: cannot open log file for writing: ' + LogFilePath);
        LogFilePath := '';
      end;
    end else begin
      Log('WARNING: cannot create log file: ' + LogFilePath);
      LogFilePath := '';
    end;
  end else begin
    Log('WARNING: cannot create log directory; install transcript will not be saved.');
  end;

  Result := True;
end;

procedure DeinitializeSetup;
begin
  if LogActive and (LogFileHandle <> -1) then begin
    Log('Installer finished. Success: ' + BoolToStr(not InstallFailed, True));
    FileClose(LogFileHandle);
    LogFileHandle := -1;
  end;
  LogActive := False;
end;

procedure SetStage(const Stage: string);
begin
  CurrentStage := Stage;
  if LogActive and (LogFileHandle <> -1) then
    Log('Stage: ' + Stage);
  TrimLog;
end;

function GetTargetDescription: string;
begin
  { The version is known from the ISPP define }
  Result := 'bundle-v{#VERSION_NUM} (windows-{#BUNDLE_ARCH})';
end;

function Redact(const Text: string): string;
var
  S: string;
begin
  S := Text;
  { Redact URLs with credentials: scheme://user:pass@host -> scheme://[redacted]@host }
  S := StringChangeEx(S, '://[^:@\s]+@', '://[redacted]@', True);
  { Redact common credential patterns }
  S := StringChangeEx(S, '(?i)(authorization|token|secret|password|passwd|api[_-]?key|signature|credential)\s*[=:]\s*(bearer|basic|token\s+)?[^\s"]+', '\1=[redacted]', True);
  Result := S;
end;

procedure TrimLog;
var
  Size: Int64;
  Keep: Int64;
  TempPath: string;
  TempHandle: Integer;
  Buffer: array of Char;
  BytesRead: Integer;
begin
  if not LogActive or (LogFilePath = '') or not FileExists(LogFilePath) then Exit;
  Size := GetFileSize(LogFilePath);
  if Size <= MAX_LOG_BYTES then Exit;
  Keep := MAX_LOG_BYTES - 96;
  if Keep < 0 then Keep := 0;
  TempPath := LogFilePath + '.tmp';
  TempHandle := FileCreate(TempPath);
  if TempHandle = -1 then Exit;
  try
    { Write truncation notice }
    var Notice: string;
    Notice := '[log truncated: this transcript is capped at ' + IntToStr(MAX_LOG_BYTES) + ' bytes; earlier output was discarded]' + #13#10;
    FileWrite(TempHandle, Notice[1], Length(Notice) * SizeOf(Char));
    { Seek to the last Keep bytes of the original file }
    LogFileHandle := FileOpen(LogFilePath, fmOpenRead or fmShareDenyWrite);
    if LogFileHandle <> -1 then begin
      try
        FileSeek(LogFileHandle, Size - Keep, soFromBeginning);
        SetArrayLength(Buffer, 4096);
        while True do begin
          BytesRead := FileRead(LogFileHandle, Buffer[0], Length(Buffer) * SizeOf(Char));
          if BytesRead <= 0 then Break;
          FileWrite(TempHandle, Buffer[0], BytesRead);
        end;
      finally
        FileClose(LogFileHandle);
      end;
    end;
    FileClose(TempHandle);
    { Replace original with truncated }
    if FileExists(LogFilePath) then
      DeleteFile(LogFilePath);
    RenameFile(TempPath, LogFilePath);
    { Reopen for continued writing }
    LogFileHandle := FileOpen(LogFilePath, fmOpenWrite or fmShareDenyNone);
    if LogFileHandle = -1 then
      LogActive := False;
  except
    if FileExists(TempPath) then DeleteFile(TempPath);
    LogActive := False;
  end;
end;

procedure PrintSupportBlock;
var
  TargetDesc, LogDisplay: string;
  Msg: string;
begin
  TargetDesc := GetTargetDescription;
  if LogFilePath <> '' then
    LogDisplay := LogFilePath
  else
    LogDisplay := '(no log was created — the installer failed before it could start logging)';
  Msg := #13#10 +
    '----- Modulo install failed: support reference ' + SupportRef + ' -----' + #13#10 +
    '  Install target   : ' + Redact(TargetDesc) + #13#10 +
    '  Failing stage    : ' + Redact(CurrentStage) + #13#10 +
    '  Install log      : ' + Redact(LogDisplay) + #13#10 +
    '  Support reference: ' + SupportRef + #13#10 +
    #13#10 +
    'Send this block AND the install log file above to Modulo support so the' + #13#10 +
    'failure can be diagnosed: https://github.com/farnalabs/modulo/issues' + #13#10 +
    '----- end of Modulo install support block -----' + #13#10;
  { Write to the installer log }
  Log(Msg);
  { Also show as a message box for visibility on failure }
  MsgBox(Msg, mbError, MB_OK);
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  { Track stage transitions through wizard pages }
  case CurPageID of
    wpWelcome: SetStage('welcome page');
    wpLicense: SetStage('license agreement');
    wpSelectDir: SetStage('select install directory');
    wpSelectComponents: SetStage('select components');
    wpSelectTasks: SetStage('select additional tasks');
    wpReady: SetStage('ready to install');
    wpInstalling: SetStage('installing files');
    wpFinished: SetStage('finished');
  end;
  Result := True;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  case CurStep of
    ssInstall: SetStage('installing files');
    ssPostInstall: SetStage('post-install (service registration)');
    ssDone: begin
      SetStage('finished');
      if InstallFailed then
        PrintSupportBlock;
    end;
  end;
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := False;
end;

procedure Log(const Msg: string);
begin
  if LogActive and (LogFileHandle <> -1) then begin
    var Line: string;
    Line := GetDateTimeString('yyyy-mm-dd"T"hh:nn:ss"Z"', '#', '#') + ' ' + Msg + #13#10;
    FileWrite(LogFileHandle, Line[1], Length(Line) * SizeOf(Char));
  end;
end;

function ShouldOpenModulo: Boolean;
begin
  Result := not IsUninstaller;
end;
