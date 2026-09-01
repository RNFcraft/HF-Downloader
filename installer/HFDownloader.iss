#define AppName "HF Downloader"
#ifndef AppVersion
  #error AppVersion must be supplied by build.ps1
#endif
#define AppPublisher "HF Downloader"
#define AppExeName "HF Downloader.exe"

[Setup]
AppId={{A51556B3-661B-4BA1-A2E8-BBD75B1B8D41}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\HF Downloader
DisableDirPage=no
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir=..\release
OutputBaseFilename=HF-Downloader-Setup-{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=lowest
UninstallDisplayIcon={app}\{#AppExeName}
SetupLogging=yes
CloseApplications=yes
RestartApplications=yes
UsePreviousAppDir=yes

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
Source: "..\dist\HF Downloader\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Создать ярлык на рабочем столе"; GroupDescription: "Дополнительные значки:"

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Запустить {#AppName}"; Flags: nowait postinstall skipifsilent

[Code]
function WebView2Installed: Boolean;
var
  Version: String;
begin
  Result := RegQueryStringValue(HKLM32, 'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', Version) or
            RegQueryStringValue(HKCU, 'Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', Version);
  Result := Result and (Version <> '') and (Version <> '0.0.0.0');
end;

function InitializeSetup: Boolean;
begin
  Result := True;
  if (not WebView2Installed) and (not WizardSilent) then
    MsgBox('Для интерфейса требуется Microsoft Edge WebView2 Runtime. Обычно он уже установлен в Windows 10/11. Если приложение не откроется, установите WebView2 Runtime с сайта Microsoft.', mbInformation, MB_OK);
end;
