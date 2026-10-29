; Modulo Windows Installer — Inno Setup script
; Native Windows packaging is fail-closed until a real Windows bundle exists.
;
; Required ISPP defines:
;   VERSION_NUM - release version
;   BUNDLE_ARCH - amd64 or arm64
;   BUNDLE_DIR  - extracted native Windows bundle root
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
DisableFinishedPage=yes
WizardStyle=modern
AppId={{7B7A9F3B-69DA-4EA0-9EB8-0D8F2D4B8A6B}}
UninstallDisplayIcon={app}\modulo.exe
UninstallDisplayName=Modulo {#VERSION_NUM}
CloseApplications=yes
RestartApplications=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; skipifsourcedoesntexist keeps the dry-run compiler check honest without
; manufacturing a fake executable. A real native bundle is required before
; any non-dry-run workflow can publish an installer.
Source: "{#BUNDLE_DIR}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs skipifsourcedoesntexist

[Icons]
Name: "{group}\Modulo"; Filename: "{app}\modulo.exe"; WorkingDir: "{app}"
Name: "{commondesktop}\Modulo"; Filename: "{app}\modulo.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\modulo.exe"; Description: "Launch Modulo"; Flags: nowait postinstall shellexec skipifdoesntexist
Filename: "sc.exe"; Parameters: "create Modulo binPath= ""{app}\modulo.exe service"" start= auto"; StatusMsg: "Installing Modulo service..."; Flags: runhidden waituntilterminated skipifdoesntexist
Filename: "sc.exe"; Parameters: "start Modulo"; StatusMsg: "Starting Modulo service..."; Flags: runhidden waituntilterminated skipifdoesntexist

[UninstallRun]
Filename: "sc.exe"; Parameters: "stop Modulo"; StatusMsg: "Stopping Modulo service..."; Flags: runhidden waituntilterminated skipifdoesntexist
Filename: "sc.exe"; Parameters: "delete Modulo"; StatusMsg: "Removing Modulo service..."; Flags: runhidden waituntilterminated skipifdoesntexist

[UninstallDelete]
Type: filesandordirs; Name: "{app}"
