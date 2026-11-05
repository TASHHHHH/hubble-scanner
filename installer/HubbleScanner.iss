#define AppName "Hubble Scanner"
#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif
[Setup]
AppId=HubbleScannerDesktop
AppName={#AppName}
AppVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\Hubble Scanner
DisableDirPage=yes
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
OutputBaseFilename=HubbleScanner-Setup
OutputDir=..\dist
Compression=lzma2
WizardStyle=modern
UninstallDisplayName={#AppName}
CloseApplications=yes

[Tasks]
Name: "ai"; Description: "Install AI dependencies for adaptive worker PCs (large download)"; Flags: unchecked

[Files]
Source: "..\*.py"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\requirements*.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\*.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "configure.ps1"; DestDir: "{app}"; Flags: ignoreversion
Source: "runtime\python-3.11.9-amd64.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall

[Icons]
Name: "{userprograms}\Hubble Scanner"; Filename: "{app}\.venv\Scripts\pythonw.exe"; Parameters: """{app}\app.py"""; WorkingDir: "{app}"

[UninstallDelete]
Type: filesandordirs; Name: "{app}\.venv"
Type: filesandordirs; Name: "{app}\__pycache__"

[Code]
procedure CurStepChanged(CurStep: TSetupStep);
var
  Params: String;
  ResultCode: Integer;
begin
  if CurStep <> ssPostInstall then Exit;
  WizardForm.StatusLabel.Caption := 'Installing Python dependencies. Keep the setup window open.';
  Params := '-NoProfile -ExecutionPolicy Bypass -File "' + ExpandConstant('{app}\configure.ps1') +
            '" -Runtime "' + ExpandConstant('{tmp}\python-3.11.9-amd64.exe') + '"';
  if WizardIsTaskSelected('ai') then Params := Params + ' -AI';
  if not Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'), Params,
      ExpandConstant('{app}'), SW_SHOW, ewWaitUntilTerminated, ResultCode) or (ResultCode <> 0) then
    RaiseException('Dependency setup failed. See %LOCALAPPDATA%\HubbleScanner\setup.log, then rerun this installer.');
end;
