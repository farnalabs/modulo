; Modulo Windows Installer - Inno Setup script
; Part of the single-install native distribution (ADR 031, FAR-679)
; Builds a Windows installer for the Modulo native bundle
;
; Requires: Inno Setup 6.2+ (Unicode)
; Build (from the repo root, on a Windows runner):
;   iscc /Qp /Obuild\windows modulo.iss
;        /DVERSION_NUM=0.4.0 /DBUNDLE_ARCH=amd64 /DBUNDLE_DIR=<abs bundle root>
;
; Required /D defines (a missing one is a hard compile error, never a silent
; default - see the guards below):
;   VERSION_NUM   - version string (e.g. 0.4.0)
;   BUNDLE_ARCH   - amd64 or arm64
;   BUNDLE_DIR    - absolute path to the extracted bundle root
;
; Signing is NOT done by Inno. The installer is signed after compilation by
; .github/workflows/windows-installer.yml (Azure Artifact Signing), so this
; script has no SignTool configuration and no signing conditionals.

#ifndef VERSION_NUM
#error VERSION_NUM is required: pass /DVERSION_NUM=<x.y.z>
#endif
#ifndef BUNDLE_ARCH
#error BUNDLE_ARCH is required: pass /DBUNDLE_ARCH=<amd64|arm64>
#endif
#ifndef BUNDLE_DIR
#error BUNDLE_DIR is required: pass /DBUNDLE_DIR=<absolute bundle root>
#endif

; ---------------------------------------------------------------------------
; Header and metadata
; ---------------------------------------------------------------------------
[Setup]
AppId={{5D0B2E77-0C1C-4F2E-9C0A-7B1D3A64E5F1}
AppName=Modulo
AppVersion={#VERSION_NUM}
AppPublisher=Farna Labs Ltd
AppPublisherURL=https://modulo.run
AppSupportURL=https://modulo.run/docs
AppUpdatesURL=https://modulo.run/changelog
DefaultDirName={autopf}\Modulo
DefaultGroupName=Modulo
; Relative to this script's directory (deploy\windows): the repo-root build dir.
OutputDir=..\..\build\windows
OutputBaseFilename=modulo-{#VERSION_NUM}-windows-{#BUNDLE_ARCH}
Compression=lzma2/ultra64
SolidCompression=yes
InternalCompressLevel=ultra64
PrivilegesRequired=admin
ArchitecturesInstallIn64BitMode=x64
ArchitecturesAllowed=x64
DisableProgramGroupPage=yes
DisableDirPage=auto
DisableReadyMemo=yes
DisableReadyPage=yes
DisableFinishedPage=yes
WizardStyle=modern
AppMutex=ModuloInstallerMutex
CreateAppDir=yes
UninstallDisplayIcon={app}\modulo.exe
UninstallDisplayName=Modulo {#VERSION_NUM}
ChangesEnvironment=yes
UsePreviousAppDir=yes
UsePreviousGroup=yes
UsePreviousLanguage=yes

; ---------------------------------------------------------------------------
; Languages
; ---------------------------------------------------------------------------
[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

; ---------------------------------------------------------------------------
; Files
; ---------------------------------------------------------------------------
[Files]
Source: "{#BUNDLE_DIR}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

; ---------------------------------------------------------------------------
; Icons
; ---------------------------------------------------------------------------
[Icons]
Name: "{group}\Modulo"; Filename: "{app}\modulo.exe"; WorkingDir: "{app}"
Name: "{commondesktop}\Modulo"; Filename: "{app}\modulo.exe"; WorkingDir: "{app}"; Tasks: desktopicon

; ---------------------------------------------------------------------------
; Run
; ---------------------------------------------------------------------------
[Run]
Filename: "{app}\modulo.exe"; Description: "Launch Modulo"; Flags: nowait postinstall shellexec skipifsilent
Filename: "{sys}\sc.exe"; Parameters: "start Modulo"; StatusMsg: "{cm:StartingService}"; Flags: runhidden waituntilterminated; Check: ServiceKeyExists

; ---------------------------------------------------------------------------
; Registry (PATH and service configuration)
; ---------------------------------------------------------------------------
[Registry]
; Add to the system PATH for all users (requires admin). The value is removed
; on uninstall by RemoveFromPath in [Code] - never with uninsdeletevalue, which
; would delete the ENTIRE system PATH.
Root: HKLM; Subkey: "SYSTEM\CurrentControlSet\Control\Session Manager\Environment"; ValueType: expandsz; ValueName: "Path"; ValueData: "{olddata};{app}"; Check: NeedsPathEntry('{app}')

; Service registration for the NT SERVICE account. DependOnService is
; deliberately absent: it is a REG_MULTI_SZ value and Inno cannot write the
; embedded NUL separators a multisz requires.
Root: HKLM; Subkey: "SYSTEM\CurrentControlSet\Services\Modulo"; ValueType: string; ValueName: "ImagePath"; ValueData: "{app}\modulo.exe service"
Root: HKLM; Subkey: "SYSTEM\CurrentControlSet\Services\Modulo"; ValueType: string; ValueName: "DisplayName"; ValueData: "Modulo Agentic SDLC Platform"
Root: HKLM; Subkey: "SYSTEM\CurrentControlSet\Services\Modulo"; ValueType: string; ValueName: "Description"; ValueData: "Modulo Agentic SDLC Platform - native service"
Root: HKLM; Subkey: "SYSTEM\CurrentControlSet\Services\Modulo"; ValueType: dword; ValueName: "Start"; ValueData: 2
Root: HKLM; Subkey: "SYSTEM\CurrentControlSet\Services\Modulo"; ValueType: string; ValueName: "ObjectName"; ValueData: "NT SERVICE\Modulo"
Root: HKLM; Subkey: "SYSTEM\CurrentControlSet\Services\Modulo"; ValueType: string; ValueName: "ErrorControl"; ValueData: 1

; ---------------------------------------------------------------------------
; Tasks
; ---------------------------------------------------------------------------
[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

; ---------------------------------------------------------------------------
; Custom messages
; ---------------------------------------------------------------------------
[CustomMessages]
english.StartingService=Starting Modulo service...
english.StoppingService=Stopping Modulo service...
english.RemovingService=Removing Modulo service...

; ---------------------------------------------------------------------------
; Code
;
; Inno's PascalScript has NO service API: IsServiceInstalled / CreateService /
; StartService / StopService / DeleteService do not exist. The service is
; registered through the [Registry] section above and started with sc.exe in
; [Run]; teardown uses sc.exe/net in [UninstallRun].
; ---------------------------------------------------------------------------
[Code]
function NeedsPathEntry(const Path: String): Boolean;
var
  CurrentPath: String;
begin
  if not RegQueryStringValue(HKLM, 'SYSTEM\CurrentControlSet\Control\Session Manager\Environment', 'Path', CurrentPath) then
    Result := True
  else
    Result := Pos(';' + Path + ';', ';' + CurrentPath + ';') = 0;
end;

function ServiceKeyExists: Boolean;
begin
  Result := RegKeyExists(HKLM, 'SYSTEM\CurrentControlSet\Services\Modulo');
end;

function IsWin10OrLater(): Boolean;
var
  Version: TWindowsVersion;
begin
  GetWindowsVersionEx(Version);
  Result := (Version.Major > 10) or ((Version.Major = 10) and (Version.Minor >= 0));
end;

function InitializeSetup(): Boolean;
var
  UninstallKey: String;
begin
  Result := True;

  if not IsWin10OrLater then
  begin
    MsgBox('Modulo requires Windows 10 or later.', mbError, MB_OK);
    Result := False;
    exit;
  end;

  { Check if already installed and offer upgrade. Inno writes the uninstall key
    as <AppId>_is1 with the ESCAPED AppId collapsed back to a single brace, so
    '{#SetupSetting("AppId")}' (which yields the doubled '{{GUID}') has to be
    un-doubled before it can match. }
  UninstallKey := '{#SetupSetting("AppId")}_is1';
  StringChangeEx(UninstallKey, '{{', '{', False);

  if RegKeyExists(HKLM, 'Software\Microsoft\Windows\CurrentVersion\Uninstall\' + UninstallKey) then
    if MsgBox('Modulo is already installed. Do you want to upgrade?', mbConfirmation, MB_YESNO) = IDNO then
      Result := False;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  { Pre-install checks }
  if not IsAdminLoggedOn then
    Result := 'Administrator privileges are required to install Modulo.';
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := False;
  { Skip informational pages for silent install }
  if WizardSilent then
  begin
    case PageID of
      wpWelcome, wpLicense, wpInfoBefore, wpSelectDir, wpSelectComponents, wpSelectTasks, wpReady: Result := True;
    end;
  end;
end;

procedure RemoveFromPath(const Dir: String);
var
  CurrentPath, Remaining, NewPath, Token: String;
  P: Integer;
  Changed: Boolean;
begin
  if not RegQueryStringValue(HKLM, 'SYSTEM\CurrentControlSet\Control\Session Manager\Environment', 'Path', CurrentPath) then
    exit;

  { Rebuild PATH entry-by-entry so only a whole-segment match is removed. A
    plain StringChangeEx would also strip 'C:\Program Files\Modulo' out of
    'C:\Program Files\Modulo-extra'. }
  Changed := False;
  NewPath := '';
  Remaining := CurrentPath;
  while Remaining <> '' do
  begin
    P := Pos(';', Remaining);
    if P = 0 then
    begin
      Token := Remaining;
      Remaining := '';
    end
    else
    begin
      Token := Copy(Remaining, 1, P - 1);
      Remaining := Copy(Remaining, P + 1, Length(Remaining));
    end;

    if (Token <> '') and (CompareText(Token, Dir) = 0) then
      Changed := True
    else
    begin
      if NewPath <> '' then
        NewPath := NewPath + ';';
      NewPath := NewPath + Token;
    end;
  end;

  if Changed then
    RegWriteExpandStringValue(HKLM, 'SYSTEM\CurrentControlSet\Control\Session Manager\Environment', 'Path', NewPath);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
    RemoveFromPath(ExpandConstant('{app}'));
end;

; ---------------------------------------------------------------------------
; Uninstall
; ---------------------------------------------------------------------------
[UninstallDelete]
Type: filesandordirs; Name: "{app}"

[UninstallRun]
Filename: "{sys}\net.exe"; Parameters: "stop Modulo"; StatusMsg: "{cm:StoppingService}"; Flags: runhidden waituntilterminated
Filename: "{sys}\sc.exe"; Parameters: "delete Modulo"; StatusMsg: "{cm:RemovingService}"; Flags: runhidden waituntilterminated
