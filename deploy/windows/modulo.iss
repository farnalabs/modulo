; Modulo Windows Installer — Inno Setup script
; Native Windows packaging is fail-closed until a real Windows bundle exists.
;
; Required ISPP defines:
;   VERSION_NUM - release version
;   BUNDLE_ARCH - amd64 or arm64
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
Source: "{#BUNDLE_DIR}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs; Excludes: "modulo.exe;modulo-runtime.json"

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
function ShouldOpenModulo: Boolean;
begin
  Result := not IsUninstaller;
end;
