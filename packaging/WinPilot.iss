; Inno Setup script for WinPilot. Build with packaging\build.cmd, which passes the version.
; Installs per user (no admin prompt) into %LOCALAPPDATA%\Programs\WinPilot.

#ifndef AppVersion
  #define AppVersion "2.0.0"
#endif

[Setup]
AppId={{6F2C7E53-8A1B-4F0D-9C3E-5A7B2D1E4C90}
AppName=WinPilot
AppVersion={#AppVersion}
AppVerName=WinPilot {#AppVersion}
AppPublisher=Auto-Win-Mate
AppPublisherURL=https://github.com/ps29/Auto-Win-Mate
DefaultDirName={autopf}\WinPilot
DefaultGroupName=WinPilot
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=..\dist
OutputBaseFilename=WinPilot-Setup-{#AppVersion}
SetupIconFile=winpilot.ico
UninstallDisplayIcon={app}\WinPilot.exe
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
CloseApplications=yes
RestartApplications=no

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "startup"; Description: "Start WinPilot in the tray when &Windows starts"; GroupDescription: "Shortcuts:"; Flags: unchecked

[Files]
Source: "..\dist\WinPilot\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\README.md"; DestDir: "{app}"; DestName: "README.md"; Flags: ignoreversion
Source: "..\WinPilot\README.md"; DestDir: "{app}"; DestName: "WinPilot-README.md"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\WinPilot"; Filename: "{app}\WinPilot.exe"; Comment: "Tell an AI what to do on your desktop"
Name: "{autodesktop}\WinPilot"; Filename: "{app}\WinPilot.exe"; Tasks: desktopicon
Name: "{userstartup}\WinPilot"; Filename: "{app}\WinPilot.exe"; Parameters: "--hidden"; Tasks: startup

[Run]
Filename: "{app}\WinPilot.exe"; Description: "Start WinPilot now"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; Stop a running copy so its files can be removed (it lives in the tray, so it may be open).
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM WinPilot.exe"; Flags: runhidden; RunOnceId: "StopWinPilot"
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM WinControlMCP.exe"; Flags: runhidden; RunOnceId: "StopServer"

[Code]
// Settings, API key and logs live in %APPDATA%\WinPilot and %LOCALAPPDATA%\WinPilot.
// Ask before removing them, so a reinstall can keep them. A silent uninstall keeps them (IDNO).
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if (CurUninstallStep = usPostUninstall) and
     (SuppressibleMsgBox('Also delete your WinPilot settings, saved API key and activity logs?', mbConfirmation,
                         MB_YESNO or MB_DEFBUTTON2, IDNO) = IDYES) then
  begin
    DelTree(ExpandConstant('{userappdata}\WinPilot'), True, True, True);
    DelTree(ExpandConstant('{localappdata}\WinPilot'), True, True, True);
  end;
end;
