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

var
  SupportRef: string;
  LogFilePath: string;
  CurrentStage: string;
  LogActive: Boolean;
  InstallFailed: Boolean;
  InstallStarted: Boolean;
  InstallCompleted: Boolean;
  UserCancelled: Boolean;

function BoolText(const Value: Boolean): string;
begin
  if Value then
    Result := 'true'
  else
    Result := 'false';
end;

function WriteInstallLog(const Msg: string): Boolean;
var
  Line: string;
begin
  Result := False;
  if (not LogActive) or (LogFilePath = '') then Exit;
  Line := GetDateTimeString('yyyy-mm-dd"T"hh:nn:ss"Z"', '#', '#') + ' ' + Msg + #13#10;
  Result := SaveStringToFile(LogFilePath, Line, True);
  if not Result then
    LogActive := False;
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
  if DirExists(LogDir) then
    Result := True
  else
    Result := ForceDirectories(LogDir);
  if not Result then
    Log('WARNING: cannot create log directory ' + LogDir);
end;

procedure PruneInstallLogs;
var
  LogDir: string;
  FindRec: TFindRec;
  Names: TArrayOfString;
  I, J, KeepCount, DeleteCount: Integer;
  Temp: string;
begin
  LogDir := GetLogDir;
  if not DirExists(LogDir) then Exit;
  SetArrayLength(Names, 0);
  if FindFirst(LogDir + '\' + LOG_PREFIX + '*' + LOG_EXT, FindRec) then begin
    try
      repeat
        SetArrayLength(Names, GetArrayLength(Names) + 1);
        Names[GetArrayLength(Names) - 1] := FindRec.Name;
      until not FindNext(FindRec);
    finally
      FindClose(FindRec);
    end;
  end;
  if GetArrayLength(Names) > 1 then begin
    for I := 0 to GetArrayLength(Names) - 2 do begin
      for J := I + 1 to GetArrayLength(Names) - 1 do begin
        if CompareText(Names[I], Names[J]) > 0 then begin
          Temp := Names[I];
          Names[I] := Names[J];
          Names[J] := Temp;
        end;
      end;
    end;
  end;
  KeepCount := MAX_INSTALL_LOGS - 1;
  if KeepCount < 1 then
    KeepCount := 1;
  DeleteCount := GetArrayLength(Names) - KeepCount;
  if DeleteCount > 0 then begin
    for I := 0 to DeleteCount - 1 do
      DeleteFile(LogDir + '\' + Names[I]);
  end;
end;

function GenerateSupportRef: string;
var
  Stamp: string;
begin
  { Trailing 8 digits of the timestamp. Unique enough for a support
    reference without depending on external WinAPI calls. }
  Stamp := GetDateTimeString('yyyymmdd', '#', '#') + GetDateTimeString('hhnnss', '#', '#');
  Result := Copy(Stamp, Length(Stamp) - 7, 8);
end;

function PosFrom(const SubStr, S: string; StartPos: Integer): Integer;
begin
  Result := Pos(SubStr, Copy(S, StartPos, Length(S) - StartPos + 1));
  if Result > 0 then
    Result := Result + StartPos - 1;
end;

function RedactUrlUserInfo(const Text: string): string;
var
  S, Rest: string;
  SchemePos, AtPos, SlashPos: Integer;
begin
  S := Text;
  SchemePos := Pos('://', S);
  if SchemePos > 0 then begin
    Rest := Copy(S, SchemePos + 3, Length(S) - SchemePos - 2);
    AtPos := Pos('@', Rest);
    SlashPos := Pos('/', Rest);
    if (AtPos > 1) and ((SlashPos = 0) or (AtPos < SlashPos)) then begin
      Delete(S, SchemePos + 3, AtPos - 1);
      Insert('[redacted]', S, SchemePos + 3);
    end;
  end;
  Result := S;
end;

function RedactAfterMarker(const Text, Marker: string): string;
var
  S: string;
  P, ValueStart, ValueEnd: Integer;
  Trimming, Scanning: Boolean;
begin
  S := Text;
  P := Pos(Lowercase(Marker), Lowercase(S));
  while P > 0 do begin
    ValueStart := P + Length(Marker);
    Trimming := True;
    while (ValueStart <= Length(S)) and Trimming do begin
      if (S[ValueStart] = ' ') or (S[ValueStart] = #9) then
        ValueStart := ValueStart + 1
      else
        Trimming := False;
    end;
    if (ValueStart <= Length(S)) and ((S[ValueStart] = '=') or (S[ValueStart] = ':')) then begin
      ValueStart := ValueStart + 1;
      Trimming := True;
      while (ValueStart <= Length(S)) and Trimming do begin
        if (S[ValueStart] = ' ') or (S[ValueStart] = #9) then
          ValueStart := ValueStart + 1
        else
          Trimming := False;
      end;
      ValueEnd := ValueStart;
      Scanning := True;
      while (ValueEnd <= Length(S)) and Scanning do begin
        if (S[ValueEnd] = ' ') or (S[ValueEnd] = #9) or (S[ValueEnd] = #13) or
           (S[ValueEnd] = #10) or (S[ValueEnd] = '"') or (S[ValueEnd] = '''') or
           (S[ValueEnd] = ',') or (S[ValueEnd] = ';') then
          Scanning := False
        else
          ValueEnd := ValueEnd + 1;
      end;
      if ValueEnd > ValueStart then begin
        Delete(S, ValueStart, ValueEnd - ValueStart);
        Insert('[redacted]', S, ValueStart);
      end;
      P := PosFrom(Lowercase(Marker), Lowercase(S), ValueStart + Length('[redacted]'));
    end else begin
      P := PosFrom(Lowercase(Marker), Lowercase(S), P + Length(Marker));
    end;
  end;
  Result := S;
end;

function Redact(const Text: string): string;
var
  S: string;
begin
  S := RedactUrlUserInfo(Text);
  S := RedactAfterMarker(S, 'authorization');
  S := RedactAfterMarker(S, 'token');
  S := RedactAfterMarker(S, 'secret');
  S := RedactAfterMarker(S, 'password');
  S := RedactAfterMarker(S, 'passwd');
  S := RedactAfterMarker(S, 'api_key');
  S := RedactAfterMarker(S, 'apikey');
  S := RedactAfterMarker(S, 'api-key');
  S := RedactAfterMarker(S, 'signature');
  S := RedactAfterMarker(S, 'credential');
  Result := S;
end;

procedure TrimLog;
var
  Lines, Kept: TArrayOfString;
  I, Count, Total, Dest, LogSize: Integer;
  Notice: string;
  Fits: Boolean;
begin
  if (not LogActive) or (LogFilePath = '') or (not FileExists(LogFilePath)) then Exit;
  if not FileSize(LogFilePath, LogSize) then Exit;
  if LogSize <= MAX_LOG_BYTES then Exit;
  if not LoadStringsFromFile(LogFilePath, Lines) then Exit;
  Notice := '[log truncated: this transcript is capped at ' + IntToStr(MAX_LOG_BYTES) + ' bytes; earlier output was discarded]';
  Count := GetArrayLength(Lines);
  Total := Length(Notice) + 2;
  I := Count - 1;
  Fits := True;
  while (I >= 0) and Fits do begin
    if Total + Length(Lines[I]) + 2 > MAX_LOG_BYTES then
      Fits := False
    else begin
      Total := Total + Length(Lines[I]) + 2;
      I := I - 1;
    end;
  end;
  { I now points at the last line that did not fit; keep I+1 .. Count-1. }
  SetArrayLength(Kept, Count - I);
  Kept[0] := Notice;
  Dest := 1;
  while I + Dest < Count do begin
    Kept[Dest] := Lines[I + Dest];
    Dest := Dest + 1;
  end;
  if not SaveStringsToFile(LogFilePath, Kept, False) then
    LogActive := False;
end;

function GetTargetDescription: string;
begin
  Result := 'bundle-v{#VERSION_NUM} (windows-{#BUNDLE_ARCH})';
end;

procedure PrintSupportBlock;
var
  TargetDesc, LogDisplay, Msg: string;
begin
  TargetDesc := GetTargetDescription;
  if LogFilePath <> '' then
    LogDisplay := LogFilePath
  else
    LogDisplay := '(no log was created - the installer failed before it could start logging)';
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
  WriteInstallLog(Msg);
  if not WizardSilent then
    MsgBox(Msg, mbError, MB_OK);
end;

procedure SetStage(const Stage: string);
begin
  CurrentStage := Stage;
  WriteInstallLog('Stage: ' + Stage);
  TrimLog;
end;

function InitializeSetup: Boolean;
var
  Stamp: string;
begin
  SupportRef := GenerateSupportRef;
  CurrentStage := 'starting up (reading arguments)';
  LogActive := False;
  InstallFailed := False;
  InstallStarted := False;
  InstallCompleted := False;
  UserCancelled := False;

  if EnsureLogDir then begin
    PruneInstallLogs;
    Stamp := GetDateTimeString('yyyymmdd"T"hhmmss"Z"', '#', '#');
    LogFilePath := GetLogDir + '\' + LOG_PREFIX + Stamp + '-' + SupportRef + LOG_EXT;
    LogActive := True;
    WriteInstallLog('Modulo installer started. Support reference: ' + SupportRef);
    WriteInstallLog('Version: ' + '{#VERSION_NUM}');
    WriteInstallLog('Architecture: ' + '{#BUNDLE_ARCH}');
    WriteInstallLog('Stage: ' + CurrentStage);
    if not FileExists(LogFilePath) then begin
      Log('WARNING: cannot write install log: ' + LogFilePath);
      LogActive := False;
      LogFilePath := '';
    end;
  end else
    Log('WARNING: cannot create log directory; install transcript will not be saved.');
  Result := True;
end;

procedure DeinitializeSetup;
begin
  if InstallStarted and (not InstallCompleted) and (not UserCancelled) then
    InstallFailed := True;

  if InstallFailed then
    PrintSupportBlock;

  if LogActive then
    WriteInstallLog('Installer finished. Success: ' + BoolText(not InstallFailed));
  LogActive := False;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
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

procedure CancelButtonClick(CurPageID: Integer; var Cancel, Confirm: Boolean);
begin
  UserCancelled := True;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  case CurStep of
    ssInstall: begin
      InstallStarted := True;
      SetStage('installing files');
    end;
    ssPostInstall: SetStage('post-install (service registration)');
    ssDone: begin
      SetStage('finished');
      InstallCompleted := True;
    end;
  end;
end;

function ShouldOpenModulo: Boolean;
begin
  Result := not IsUninstaller;
end;
