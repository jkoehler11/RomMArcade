; Inno Setup script for the RomMArcade Windows installer.
; Build from the repository root:
;   ISCC.exe /DMyAppVersion=0.1.0 packaging\windows\RomMArcade.iss

#ifndef MyAppVersion
#define MyAppVersion "0.0.0"
#endif

#define MyAppName "RomMArcade"
#define MyAppPublisher "jkoehler11"
#define MyAppURL "https://github.com/jkoehler11/RomMArcade"
#define MyAppExeName "RomMArcade.exe"

[Setup]
AppId={{7D3C2E56-6E49-4A2E-9B8B-7D4A9C1F0A11}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}/releases
DefaultDirName={localappdata}\Programs\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\..\dist
OutputBaseFilename=RomMArcade-{#MyAppVersion}-windows-x64-setup
SetupIconFile=..\rommarcade.ico
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64
UninstallDisplayIcon={app}\{#MyAppExeName}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional icons:"; Flags: unchecked

[Files]
Source: "..\..\dist\RomMArcade\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent
