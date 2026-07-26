[Setup]
AppName=AudioCleanerPro
AppVersion=1.0
DefaultDirName={pf}\AudioCleanerPro
DefaultGroupName=AudioCleanerPro
OutputBaseFilename=AudioCleanerPro_Setup
Compression=lzma
SolidCompression=yes
WizardStyle=modern
DisableProgramGroupPage=yes

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"

[Files]
Source: "dist\AudioCleanerPro\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\AudioCleanerPro"; Filename: "{app}\AudioCleanerPro.exe"
Name: "{commondesktop}\AudioCleanerPro"; Filename: "{app}\AudioCleanerPro.exe"

[Run]
Filename: "{app}\AudioCleanerPro.exe"; Description: "Запустить AudioCleanerPro"; Flags: nowait postinstall skipifsilent
