; Inno Setup script for Art Downloader (Windows).
; Built automatically by .github/workflows/windows.yml:
;   iscc /DAppVersion=1.2.3 windows\installer.iss
; Expects the PyInstaller output in dist\ArtDownloader\.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{EDF25B7B-B98F-41B0-9462-5798ED9C98A9}
AppName=Art Downloader
AppVersion={#AppVersion}
AppPublisher=GreenClowns
AppPublisherURL=https://github.com/GreenClowns/Art-Downloader
AppSupportURL=https://github.com/GreenClowns/Art-Downloader/issues
DefaultDirName={localappdata}\Programs\Art Downloader
DefaultGroupName=Art Downloader
DisableProgramGroupPage=yes
; Per-user install: no administrator rights needed
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=ArtDownloader-{#AppVersion}-Setup
SetupIconFile=..\assets\icon.ico
UninstallDisplayIcon={app}\ArtDownloader.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "..\dist\ArtDownloader\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\Art Downloader"; Filename: "{app}\ArtDownloader.exe"
Name: "{autodesktop}\Art Downloader"; Filename: "{app}\ArtDownloader.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\ArtDownloader.exe"; Description: "{cm:LaunchProgram,Art Downloader}"; Flags: nowait postinstall skipifsilent
