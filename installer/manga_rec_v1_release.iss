; 漫画推荐 V1 社区版安装脚本（Inno Setup 6）
; 编译: iscc.exe installer\manga_rec_v1_release.iss
; 种子数据（config.json / folder_list.txt / data\）用 onlyifdoesntexist：重复安装不覆盖用户数据

#define MyAppName "Manga Sommelier V1"
#define MyAppVersion "1.0.0"
#define MyAppExeName "MangaRecV1.exe"

[Setup]
AppId={{9F3B8C24-7A6D-4E2B-9C31-5D8E6A1F0C77}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher=myainygem
DefaultDirName={localappdata}\Programs\MangaRecV1
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\release
OutputBaseFilename=MangaRecV1-Setup-1.0.0
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayName={#MyAppName}
UninstallDisplayIcon={app}\{#MyAppExeName}
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "chinesesimp"; MessagesFile: "ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional tasks:"
Name: "portable"; Description: "Portable mode (keep all data inside the install folder)"; GroupDescription: "Additional tasks:"

[Files]
Source: "..\dist\MangaRecV1\*"; DestDir: "{app}"; Excludes: "config.json,folder_list.txt,data\*"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "..\dist\MangaRecV1\_internal\config.json"; DestDir: "{app}"; Flags: onlyifdoesntexist ignoreversion
Source: "..\dist\MangaRecV1\_internal\folder_list.txt"; DestDir: "{app}"; Flags: onlyifdoesntexist ignoreversion
Source: "..\dist\MangaRecV1\_internal\data\*"; DestDir: "{app}\data"; Flags: recursesubdirs createallsubdirs onlyifdoesntexist ignoreversion
Source: "portable.flag"; DestDir: "{app}"; Tasks: portable; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent
