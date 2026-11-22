; Per-user installer for winget (no administrator rights). Built by build.ps1:
;   ISCC /DAppVersion=x.y.z /DSourceDir=<staged bundle> /DOutputDir=<dist> system-steward.iss
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{8F3C2B7A-5D1E-4C9A-9B2F-6E4D7A1C3B58}
AppName=System Steward
AppVersion={#AppVersion}
AppVerName=System Steward {#AppVersion}
AppPublisher=Rajesh Budhathoki
AppPublisherURL=https://rajoyish.github.io/system-steward/
AppSupportURL=https://github.com/rajoyish/system-steward/issues
DefaultDirName={localappdata}\Programs\System Steward
DisableProgramGroupPage=yes
DisableDirPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
ChangesEnvironment=yes
OutputDir={#OutputDir}
OutputBaseFilename=system-steward-{#AppVersion}-windows-x64-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayName=System Steward
LicenseFile=..\..\LICENSE

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[InstallDelete]
; Upgrades replace the bundle wholesale so files removed from a release do not linger.
Type: filesandordirs; Name: "{app}\app"
Type: filesandordirs; Name: "{app}\python"

[UninstallDelete]
; Python writes __pycache__ folders at run time.
Type: filesandordirs; Name: "{app}"

[Icons]
Name: "{autoprograms}\System Steward"; Filename: "{app}\system-steward.cmd"; WorkingDir: "{userdocs}"

[Registry]
Root: HKCU; Subkey: "Environment"; ValueType: expandsz; ValueName: "Path"; ValueData: "{olddata};{app}"; Check: NeedsAddPath(ExpandConstant('{app}'))

[Code]
function NeedsAddPath(Dir: string): Boolean;
var
  Current: string;
begin
  if not RegQueryStringValue(HKEY_CURRENT_USER, 'Environment', 'Path', Current) then
    Result := True
  else
    Result := Pos(';' + Uppercase(Dir) + ';', ';' + Uppercase(Current) + ';') = 0;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Current, Dir: string;
begin
  if CurUninstallStep <> usPostUninstall then
    exit;
  if not RegQueryStringValue(HKEY_CURRENT_USER, 'Environment', 'Path', Current) then
    exit;
  Dir := ExpandConstant('{app}');
  Current := ';' + Current + ';';
  StringChangeEx(Current, ';' + Dir + ';', ';', True);
  while (Length(Current) > 0) and (Current[1] = ';') do
    Delete(Current, 1, 1);
  while (Length(Current) > 0) and (Current[Length(Current)] = ';') do
    Delete(Current, Length(Current), 1);
  RegWriteExpandStringValue(HKEY_CURRENT_USER, 'Environment', 'Path', Current);
end;
