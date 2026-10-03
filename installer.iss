#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{9E12E834-BDB5-4F07-9868-8B7CD8252937}
AppName=SSH 隧道助手
AppVersion={#AppVersion}
AppVerName=SSH 隧道助手 {#AppVersion}
AppPublisher=Elpco
AppPublisherURL=https://github.com/Zx1234567891/ssh-tool
AppSupportURL=https://github.com/Zx1234567891/ssh-tool/issues
AppUpdatesURL=https://github.com/Zx1234567891/ssh-tool/releases
DefaultDirName={localappdata}\Programs\SshTunnelManager
DefaultGroupName=SSH 隧道助手
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=dist
OutputBaseFilename=SshTunnelManager-Setup-{#AppVersion}
SetupIconFile=assets\logo.ico
UninstallDisplayIcon={app}\SshTunnelManager.exe
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
UsePreviousAppDir=yes

; Upgrade policy:
; - The installer owns only {app}, the program installation directory.
; - User config, proxy maps, logs, diagnostics, and update state live under
;   %APPDATA%\SshTunnelManager and %LOCALAPPDATA%\SshTunnelManager.
; - Do not add UninstallDelete entries for those user data directories.

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加快捷方式："; Flags: unchecked

[Files]
Source: "dist\SshTunnelManager.exe"; DestDir: "{app}"; Flags: ignoreversion

[InstallDelete]
; Remove the previous program payload before copying the new build. This keeps
; upgrades from leaving stale application files while preserving all user data.
Type: files; Name: "{app}\SshTunnelManager.exe"
Type: files; Name: "{app}\SshTunnelManager-Portable-*.exe"

[Icons]
Name: "{group}\SSH 隧道助手"; Filename: "{app}\SshTunnelManager.exe"
Name: "{group}\卸载 SSH 隧道助手"; Filename: "{uninstallexe}"
Name: "{autodesktop}\SSH 隧道助手"; Filename: "{app}\SshTunnelManager.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\SshTunnelManager.exe"; Description: "启动 SSH 隧道助手"; Flags: nowait postinstall skipifsilent
