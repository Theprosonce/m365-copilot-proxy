; Inno Setup script for M365 Copilot Proxy.
; Per-user install (no admin): drops the Nuitka --standalone .dist folder into
; %LOCALAPPDATA%\Programs and wires a Start Menu shortcut + optional run-at-login.
; Built by packaging\installer_nuitka.ps1 (which compiles the payload, signs it, then runs ISCC).

#define AppName "M365 Copilot Proxy"
; AppVersion is passed by packaging\installer_nuitka.ps1 via /DAppVersion=<pkg __version__>; default for manual runs.
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#define AppExe "m365-copilot-proxy.exe"
#define AppPublisher "MassimilianoPili"
#ifndef PayloadDir
  #define PayloadDir "..\dist-nuitka\build_entry.dist"
#endif

[Setup]
AppId={{8F3C2A91-5E6D-4B72-9A1C-7D0E4F2B6C30}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
; Per-user install, no admin elevation.
PrivilegesRequired=lowest
DefaultDirName={localappdata}\Programs\M365CopilotProxy
UsePreviousAppDir=yes
DisableProgramGroupPage=yes
DisableDirPage=yes
; Close a running instance before upgrading the folder, don't auto-restart it.
CloseApplications=yes
RestartApplications=no
Compression=lzma2/max
SolidCompression=yes
SetupIconFile=..\assets\icon.ico
OutputDir=..\dist-installer
OutputBaseFilename=M365CopilotProxy-Setup-{#AppVersion}
WizardStyle=modern

[Tasks]
Name: "startup"; Description: "Avvia all'accesso (consigliato per un'app da tray)"; GroupDescription: "Avvio:"; Flags: checkedonce

[Files]
Source: "{#PayloadDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{userstartup}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: startup

[Run]
Filename: "{app}\{#AppExe}"; Description: "Avvia {#AppName}"; Flags: nowait postinstall skipifsilent
