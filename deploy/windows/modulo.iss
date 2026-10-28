; Modulo Windows Installer — Inno Setup script
; Part of the single-install native distribution (ADR 031, FAR-679)
; Builds a Windows installer for the Modulo native bundle
;
; Requires: Inno Setup 6.2+ (Unicode), Microsoft SignTool, Azure Trusted Signing
; Build: Run on Windows runner (GitHub Actions windows-latest)
;
; Usage: iscc /Qp /Obuild/windows /Fmodulo-${VERSION_NUM}-windows-${ARCH} modulo.iss
;
; Defines (passed via /D):
;   VERSION_NUM     - version string (e.g. 0.4.0)
;   BUNDLE_ARCH     - amd64 or arm64
;   BUNDLE_DIR      - path to extracted bundle root (modulo-<version>-windows-<arch>)
;   SIGNTOOL_PATH   - path to signtool.exe (optional, defaults to Windows SDK)
;   AZURE_SIGNING   - 1 to enable Azure Trusted Signing, 0 to skip
;   AZURE_TENANT_ID - Azure AD tenant ID
;   AZURE_CLIENT_ID - Azure AD client ID (managed identity or app registration)
;   AZURE_CERT_NAME - certificate name/profile in Azure Trusted Signing

#pragmaSection 1: Header and Metadata

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
WizardImageFile=..\modulo\docs\assets\wizard-image.bmp
WizardSmallImageFile=..\modulo\docs\assets\wizard-small.bmp
LicenseFile=..\LICENSE
InfoBeforeFile=..\docs\assets\install-info.txt
AppId={{modulo-windows-installer}}
AppMutex=ModuloInstallerMutex
CreateAppDir=yes
UninstallDisplayIcon={app}\modulo.exe
UninstallDisplayName=Modulo {#VERSION_NUM}
ChangesAssociations=yes
UsePreviousAppDir=yes
UsePreviousGroup=yes
UsePreviousLanguage=yes
CloseApplications=force
RestartApplications=yes
RestartIfNeededByRun=yes

#pragmaSection 2: Languages

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

#pragmaSection 3: Files

[Files]
Source: "{#BUNDLE_DIR}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

; Native binaries (PostgreSQL, Redis, Python runtime)
; These are already in the bundle under pg/, redis/, runtime/

; Launcher executable
Source: "{#BUNDLE_DIR}\modulo.exe"; DestDir: "{app}"; Flags: ignoreversion

#pragmaSection 4: Icons

[Icons]
Name: "{group}\Modulo"; Filename: "{app}\modulo.exe"; WorkingDir: "{app}"
Name: "{commondesktop}\Modulo"; Filename: "{app}\modulo.exe"; WorkingDir: "{app}"; Tasks: desktopicon

#pragmaSection 5: Run

[Run]
Filename: "{app}\modulo.exe"; Description: "Launch Modulo"; Flags: nowait postinstall shellexec

#pragmaSection 6: Registry (PATH and service configuration)

[Registry]
; Add to system PATH for all users (requires admin)
Root: HKLM; Subkey: "SYSTEM\CurrentControlSet\Control\Session Manager\Environment"; ValueType: expandsz; ValueName: "Path"; ValueData: "{olddata};{app}"; Check: NeedsPathEntry("{app}"); Flags: uninsdeletevalue

; Service configuration for NT SERVICE
Root: HKLM; Subkey: "SYSTEM\CurrentControlSet\Services\Modulo"; ValueType: string; ValueName: "ImagePath"; ValueData: "{app}\modulo.exe service"
Root: HKLM; Subkey: "SYSTEM\CurrentControlSet\Services\Modulo"; ValueType: string; ValueName: "DisplayName"; ValueData: "Modulo Agentic SDLC Platform"
Root: HKLM; Subkey: "SYSTEM\CurrentControlSet\Services\Modulo"; ValueType: string; ValueName: "Description"; ValueData: "Modulo Agentic SDLC Platform - native service"
Root: HKLM; Subkey: "SYSTEM\CurrentControlSet\Services\Modulo"; ValueType: dword; ValueName: "Start"; ValueData: 2  ; Automatic
Root: HKLM; Subkey: "SYSTEM\CurrentControlSet\Services\Modulo"; ValueType: string; ValueName: "ObjectName"; ValueData: "NT SERVICE\Modulo"
Root: HKLM; Subkey: "SYSTEM\CurrentControlSet\Services\Modulo"; ValueType: string; ValueName: "ErrorControl"; ValueData: 1
Root: HKLM; Subkey: "SYSTEM\CurrentControlSet\Services\Modulo"; ValueType: multisz; ValueName: "DependOnService"; ValueData: "PostgreSQL\0Redis\0"

#pragmaSection 7: Tasks

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

#pragmaSection 8: Custom Messages

[CustomMessages]
english.InstallingService=Installing Modulo service...
english.StartingService=Starting Modulo service...
english.UninstallingService=Removing Modulo service...

#pragmaSection 8: Code

[Code]
var
  ServiceInstalled: Boolean;

function NeedsPathEntry(const Path: String): Boolean;
var
  CurrentPath: String;
begin
  if not RegQueryStringValue(HKLM, 'SYSTEM\CurrentControlSet\Control\Session Manager\Environment', 'Path', CurrentPath) then
    Result := True
  else
    Result := Pos(';' + Path + ';', ';' + CurrentPath + ';') = 0;
end;

function InitializeSetup(): Boolean;
begin
  Result := True;
  { Check if already installed and offer upgrade }
  if RegKeyExists(HKLM, 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{#SetupSetting("AppId")}') then
  begin
    if MsgBox('Modulo is already installed. Do you want to upgrade?', mbConfirmation, MB_YESNO) = IDNO then
      Result := False;
  end;
end;

function InitializeUninstall(): Boolean;
begin
  Result := True;
  { Stop and remove service on uninstall }
  if IsServiceInstalled('Modulo') then
  begin
    if not StopService('Modulo') then
      MsgBox('Failed to stop Modulo service. Please stop it manually before uninstalling.', mbError, MB_OK);
    DeleteService('Modulo');
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
  begin
    { Install and start the Modulo service }
    if not IsServiceInstalled('Modulo') then
    begin
      if not CreateService('Modulo', 'Modulo Agentic SDLC Platform', ExpandConstant('"{app}\modulo.exe" service'), 2, 'NT SERVICE\Modulo', '', '') then
        MsgBox('Failed to install Modulo service. You may need to install it manually.', mbError, MB_OK)
      else
      begin
        ServiceInstalled := True;
        if not StartService('Modulo') then
          MsgBox('Failed to start Modulo service. You can start it manually with: net start Modulo', mbError, MB_OK);
      end;
    end;
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
  begin
    { Clean up PATH entry }
    { The registry entry is removed by the [Registry] section with uninsdeletevalue }
  end;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  { Custom logic for page navigation if needed }
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := False;
  { Skip certain pages for silent install }
  if WizardSilent then
  begin
    case PageID of
      wpWelcome, wpLicense, wpInfoBefore, wpSelectDir, wpSelectComponents, wpSelectTasks, wpReady: Result := True;
    end;
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  { Pre-install checks }
  if not IsAdminLoggedOn then
    Result := 'Administrator privileges are required to install Modulo.';
end;

function NeedRestart(): Boolean;
begin
  Result := False;
  { Check if restart is needed (e.g., if files in use) }
end;

{ Custom function to check if we're running on supported Windows version }
function IsWin10OrLater(): Boolean;
var
  Version: TWindowsVersion;
begin
  GetWindowsVersionEx(Version);
  Result := (Version.Major > 10) or ((Version.Major = 10) and (Version.Minor >= 0));
end;

function InitializeSetupEx(): Boolean;
begin
  if not IsWin10OrLater then
  begin
    MsgBox('Modulo requires Windows 10 or later.', mbError, MB_OK);
    Result := False;
  end
  else
    Result := True;
end;

#pragmaSection 9: Uninstall

[UninstallDelete]
Type: filesandordirs; Name: "{app}"

#pragmaSection 10: Uninstall Run

[UninstallRun]
Filename: "net"; Parameters: "stop Modulo"; StatusMsg: "Stopping Modulo service..."; Flags: runhidden waituntilterminated runascurrentuser
Filename: "sc"; Parameters: "delete Modulo"; StatusMsg: "Removing Modulo service..."; Flags: runhidden waituntilterminated runascurrentuser

#pragmaSection 11: Signing (Azure Trusted Signing)

#if AZURE_SIGNING = "1"
[Setup]
SignTool=signtool
SignToolParams=sign /fd sha256 /tr http://timestamp.acs.microsoft.com /td sha256 /a /d "Modulo" /du "https://modulo.run" /as /atc "http://timestamp.acs.microsoft.com" /kp "#AZURE_CERT_NAME" /id "#AZURE_TENANT_ID" /ic "#AZURE_CLIENT_ID" $f
#endif
