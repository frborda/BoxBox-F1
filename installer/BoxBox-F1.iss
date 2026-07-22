; Instalador de BoxBox-F1 (Inno Setup 6).
; Se compila desde build.ps1 con:  ISCC.exe /DAppVersion=x.y.z BoxBox-F1.iss
; Instala en %LOCALAPPDATA%\Programs (sin permisos de administrador), así el
; actualizador automático integrado puede reemplazar los archivos sin UAC.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{7C1F4B0A-9D34-4E1B-8B37-F1E2C39A6D51}
AppName=BoxBox-F1
AppVersion={#AppVersion}
AppPublisher=frborda
AppPublisherURL=https://github.com/frborda/F1-Live-Telemetry
DefaultDirName={autopf}\BoxBox-F1
PrivilegesRequired=lowest
DisableProgramGroupPage=yes
DisableDirPage=no
OutputBaseFilename=BoxBox-F1-setup
Compression=lzma2
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\BoxBox-F1.exe
UninstallDisplayName=BoxBox-F1
CloseApplications=yes
RestartApplications=no
WizardStyle=modern

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; Flags: unchecked

[Files]
Source: "..\dist\BoxBox-F1\*"; DestDir: "{app}"; \
    Flags: recursesubdirs createallsubdirs ignoreversion

[InstallDelete]
; restos de instalaciones con el nombre anterior (F1 Live Telemetry): mismo
; AppId, así que el upgrade cae en la misma carpeta y hay que barrer los
; exes y accesos directos viejos que Inno no reemplaza solo
Type: files; Name: "{app}\F1LiveTelemetry.exe"
Type: files; Name: "{app}\capture\F1TelemCapture.exe"
Type: files; Name: "{autoprograms}\F1 Live Telemetry.lnk"
Type: files; Name: "{autoprograms}\F1 Telemetry Capture.lnk"
Type: files; Name: "{autodesktop}\F1 Live Telemetry.lnk"

[Icons]
Name: "{autoprograms}\BoxBox-F1"; Filename: "{app}\BoxBox-F1.exe"
Name: "{autoprograms}\BoxBox-F1 Capture"; \
    Filename: "{app}\capture\BoxBox-F1-Capture.exe"
Name: "{autodesktop}\BoxBox-F1"; Filename: "{app}\BoxBox-F1.exe"; \
    Tasks: desktopicon

[Registry]
; protocolo f1telemetry:// -> capturador (la extensión de Chrome entrega el
; token F1TV con el diálogo nativo "Abrir BoxBox-F1" del navegador)
Root: HKCU; Subkey: "Software\Classes\f1telemetry"; ValueType: string; \
    ValueData: "URL:BoxBox-F1"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\f1telemetry"; ValueType: string; \
    ValueName: "URL Protocol"; ValueData: ""
Root: HKCU; Subkey: "Software\Classes\f1telemetry\shell\open\command"; \
    ValueType: string; ValueData: """{app}\capture\BoxBox-F1-Capture.exe"" ""%1"""

[Run]
Filename: "{app}\BoxBox-F1.exe"; \
    Description: "Launch BoxBox-F1"; Flags: nowait postinstall skipifsilent
