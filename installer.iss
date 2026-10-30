; Instalador de Pliegue (antes PDFKit) — compilar con Inno Setup 6:
;   ISCC.exe installer.iss
; Produce installer\Pliegue_Setup.exe

#define MyAppName "Pliegue"
#define MyAppVersion "1.4.1"
; Debe coincidir con ui/startup.py: une los accesos directos y la ventana en un solo botón de la barra de tareas
#define MyAppUserModelID "Pliegue.App"
#define MyAppPublisher "Blureckzhyto"
#define MyAppURL "https://github.com/Blureckzhyto/pliegue"
#define MyAppExeName "Pliegue.exe"
; Nombre anterior (hasta 1.3.x): se limpian su carpeta y sus accesos directos al actualizar
#define FormerName "PDFKit"
#define FormerExeName "PDFKit.exe"
; NO cambiar: es el AppId de PDFKit (ver [Setup]). La prueba del instalador lo reemplaza
; por otro para no tocar nunca una instalación real.
#define MyAppIdGuid "7E64A9D1-52F3-4C08-B1A4-9D0E6C2F5B83"

[Setup]
; NO cambiar el AppId: es el mismo de PDFKit, así 1.4.0 se instala como actualización
; y en "Agregar o quitar programas" queda una sola entrada, ahora llamada Pliegue.
AppId={{{#MyAppIdGuid}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}/releases
UninstallDisplayName={#MyAppName}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
; con el nombre nuevo, la carpeta y el grupo del menú Inicio son nuevos aunque haya una
; instalación anterior de PDFKit (la vieja se borra después de instalar, ver [Code])
UsePreviousAppDir=no
UsePreviousGroup=no
DisableProgramGroupPage=yes
; Pide elevación para instalar en C:\Program Files\Pliegue
; (permite también instalación por usuario vía /CURRENTUSER o el diálogo)
PrivilegesRequired=admin
PrivilegesRequiredOverridesAllowed=dialog commandline
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=installer
OutputBaseFilename=Pliegue_Setup
SetupIconFile=assets\icon.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern

[Languages]
Name: "spanish"; MessagesFile: "compiler:Languages\Spanish.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[InstallDelete]
; La app es una carpeta (Pliegue.exe + _internal\): al actualizar se borra la interna anterior.
Type: filesandordirs; Name: "{app}\_internal"
Type: filesandordirs; Name: "{app}\Licencias"
; Accesos directos de PDFKit (el nombre anterior). El ancla de la barra de tareas no se
; puede quitar desde el instalador: las notas de la versión piden volver a anclar.
Type: files; Name: "{autodesktop}\{#FormerName}.lnk"
Type: files; Name: "{userdesktop}\{#FormerName}.lnk"
Type: filesandordirs; Name: "{autoprograms}\{#FormerName}"
Type: files; Name: "{autoprograms}\{#FormerName}.lnk"

[Files]
Source: "dist\Pliegue\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion
Source: "dist\Pliegue\_internal\*"; DestDir: "{app}\_internal"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "README.md"; DestDir: "{app}"; Flags: ignoreversion
; licencia AGPL-3.0 de Pliegue y licencias de todos los componentes de terceros
Source: "dist\Pliegue\Licencias\*"; DestDir: "{app}\Licencias"; Flags: ignoreversion recursesubdirs createallsubdirs

[UninstallDelete]
; lo que Python pueda haber creado dentro de la carpeta (cachés)
Type: filesandordirs; Name: "{app}\_internal"

[Icons]
; Entrada en el menú Inicio
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\{#MyAppExeName}"; AppUserModelID: "{#MyAppUserModelID}"
; Acceso directo en el Escritorio (marcado por defecto)
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\{#MyAppExeName}"; AppUserModelID: "{#MyAppUserModelID}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent

[Code]
{ Carpeta de la instalación anterior (PDFKit), leída antes de instalar del registro de esta
  misma desinstalación (mismo AppId). Después de instalar Pliegue se borra, pero solo si es
  otra carpeta y contiene PDFKit.exe: nunca se borra a ciegas. }
var
  FormerDir: String;

function UninstallKey(): String;
begin
  Result := 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{{#MyAppIdGuid}}_is1';
end;

function InitializeSetup(): Boolean;
var
  Dir: String;
begin
  FormerDir := '';
  { solo el mismo modo de instalación: una instalación por usuario nunca toca la de todos }
  if IsAdminInstallMode then
  begin
    if RegQueryStringValue(HKLM, UninstallKey(), 'Inno Setup: App Path', Dir) then
      FormerDir := Dir;
  end
  else if RegQueryStringValue(HKCU, UninstallKey(), 'Inno Setup: App Path', Dir) then
    FormerDir := Dir;
  Result := True;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if (CurStep = ssPostInstall) and (FormerDir <> '') and
     (CompareText(RemoveBackslashUnlessRoot(FormerDir), RemoveBackslashUnlessRoot(ExpandConstant('{app}'))) <> 0) and
     FileExists(AddBackslash(FormerDir) + '{#FormerExeName}') then
  begin
    if DelTree(FormerDir, True, True, True) then
      Log('Carpeta anterior de {#FormerName} borrada: ' + FormerDir)
    else
      Log('No se pudo borrar por completo la carpeta anterior: ' + FormerDir);
  end;
end;
