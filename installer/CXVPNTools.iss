; Build with installer/build_installer.ps1 after verifying the signed prerequisites.
#define AppName "CXVPNTools"
#define AppExeName "CXVPNTools.exe"
#define AppBinary SourcePath + "..\dist\CXVPNTools\" + AppExeName
#define AppVersion GetStringFileInfo(AppBinary, "ProductVersion")
#define AppFileVersion GetVersionNumbersString(AppBinary)
#ifndef VCMinVersion
  #error Missing locked VC runtime minimum version.
#endif
#ifndef WebViewSHA256
  #error Build using installer/build_installer.ps1 to verify signatures and hashes.
#endif
#ifndef VCSHA256
  #error Missing verified VC runtime digest.
#endif

[Setup]
AppId={{8D3D2C8E-6F03-4C2A-9B11-5B4F9E7A2B5E}
AppName={#AppName}
AppVersion={#AppVersion}
VersionInfoVersion={#AppFileVersion}
AppPublisher=CXVPNTools
AppPublisherURL=https://github.com/SeanZhang97/cxvpn-tools
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableDirPage=no
UsePreviousAppDir=yes
DisableProgramGroupPage=yes
OutputDir=..\dist
#ifdef OfflineBundle
OutputBaseFilename=CXVPNTools-{#AppVersion}-setup-offline
#else
OutputBaseFilename=CXVPNTools-{#AppVersion}-setup
#endif
SetupIconFile=..\icon.ico
UninstallDisplayIcon={app}\{#AppExeName}
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
PrivilegesRequired=admin
CloseApplications=no
RestartApplications=no
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
WizardSizePercent=110
SetupLogging=yes
UninstallLogging=yes

[Languages]
Name: "chinesesimplified"; MessagesFile: "languages\ChineseSimplified.isl"

[LangOptions]
DialogFontName=Microsoft YaHei UI
DialogFontSize=9

[Files]
; Core setup only carries dependency metadata. Optional offline build embeds files.
#ifdef OfflineBundle
Source: "prereqs\VC_redist.x64.exe"; Flags: dontcopy
Source: "prereqs\MicrosoftEdgeWebView2RuntimeInstallerX64.exe"; Flags: dontcopy
#endif
Source: "run-prerequisite.ps1"; Flags: dontcopy
Source: "uninstall-service.ps1"; DestDir: "{app}\installer"; Flags: ignoreversion
Source: "prereqs.lock.json"; Flags: dontcopy
Source: "..\dist\CXVPNTools\CXVPNTools.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\dist\CXVPNTools\_internal\*"; DestDir: "{app}\_internal"; Flags: recursesubdirs ignoreversion; Excludes: "config.json,config.json.bak,*.log,*.jsonl,*.db,*.sqlite*,webview_data\*,browser_data*\*,captcha_cache\*"

[Icons]
Name: "{autodesktop}\CXVPNTools"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"
Name: "{group}\CXVPNTools"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"
Name: "{group}\卸载 CXVPNTools"; Filename: "{uninstallexe}"

[Run]
Filename: "{app}\{#AppExeName}"; Flags: nowait skipifdoesntexist runasoriginaluser; Check: ShouldAutoRestartApplication
Filename: "{app}\{#AppExeName}"; Description: "启动 CXVPNTools"; Flags: nowait postinstall skipifsilent runasoriginaluser; Check: CanStartApplication

[Code]
var
  DependencyRestart: Boolean;
  DeleteUserData: Boolean;
  DependencyDownloadPage: TDownloadWizardPage;
  DependencyDownloadStarted: Int64;
  DependencyDownloadSize: Int64;
  DependencyDownloadTimedOut: Boolean;

function GetTickCount64(): Int64;
  external 'GetTickCount64@kernel32.dll stdcall';

function OpenEvent(Access: Cardinal; Inherit: Boolean; Name: String): THandle;
  external 'OpenEventW@kernel32.dll stdcall';
function SignalEvent(Handle: THandle): Boolean;
  external 'SetEvent@kernel32.dll stdcall';
function CloseEvent(Handle: THandle): Boolean;
  external 'CloseHandle@kernel32.dll stdcall';

function ValidVersion(Value: String): Boolean;
var
  Packed: Int64;
begin
  Result := StrToVersion(Value, Packed) and (Packed > 0);
end;

function WebView2Installed(): Boolean;
var
  Version: String;
begin
  { All-users install: a runtime registered only to the elevated user is insufficient. }
  Result := RegQueryStringValue(HKLM32,
    'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}',
    'pv', Version) and ValidVersion(Version);
end;

function VCRuntimeInstalled(): Boolean;
var
  Installed: Cardinal;
  Version: String;
  Actual, Required: Int64;
begin
  Result := False;
  if not RegQueryDWordValue(HKLM64,
    'SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64', 'Installed', Installed) then Exit;
  if Installed <> 1 then Exit;
  if not RegQueryStringValue(HKLM64,
    'SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64', 'Version', Version) then Exit;
  if Copy(Version, 1, 1) = 'v' then Delete(Version, 1, 1);
  if not StrToVersion(Version, Actual) then Exit;
  if not StrToVersion('{#VCMinVersion}', Required) then Exit;
  Result := ComparePackedVersion(Actual, Required) >= 0;
end;

function ConfirmStopRunningApplicationForUpgrade(): Boolean; forward;

function DependencyDownloadProgress(const Url, FileName: String;
  const Progress, ProgressMax: Int64): Boolean;
begin
  DependencyDownloadTimedOut := GetTickCount64 - DependencyDownloadStarted > 600000;
  Result := (not DependencyDownloadTimedOut) and (Progress <= DependencyDownloadSize);
end;

procedure InitializeWizard;
begin
  DependencyDownloadPage := CreateDownloadPage('准备运行组件',
    '仅下载缺失组件；取消或下载失败不会关闭旧版程序。', @DependencyDownloadProgress);
  DependencyDownloadPage.ShowBaseNameInsteadOfUrl := True;
end;

function VerifyDependency(FileName, Kind, Digest: String; Size: Int64): Boolean;
var
  Params: String;
  Code: Integer;
begin
  Result := False;
  if not FileExists(ExpandConstant('{tmp}\') + FileName) then Exit;
  ExtractTemporaryFile('run-prerequisite.ps1');
  Params := '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' +
    ExpandConstant('{tmp}\run-prerequisite.ps1') + '" -Installer "' +
    ExpandConstant('{tmp}\') + FileName + '" -Kind ' + Kind +
    ' -ExpectedSHA256 ' + Digest + ' -ExpectedSize ' + IntToStr(Size) + ' -VerifyOnly';
  Log(Kind + ': verifying hash, size and Microsoft signature');
  Result := Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
    Params, '', SW_HIDE, ewWaitUntilTerminated, Code) and (Code = 0);
end;

function AcquireDependency(FileName, Kind, Digest, AssetURL, FallbackURL: String;
  Size: Int64): String;
var
  Target, CacheDir, CacheFile, Candidate, SourceURL, Failure: String;
  Index: Integer;
begin
  Result := '';
  Target := ExpandConstant('{tmp}\') + FileName;
  if VerifyDependency(FileName, Kind, Digest, Size) then Exit;
#ifdef OfflineBundle
  ExtractTemporaryFile(FileName);
  if not VerifyDependency(FileName, Kind, Digest, Size) then
    Result := Kind + ' 离线组件校验失败。';
#else
  CacheDir := ExpandConstant('{localappdata}\CXVPNTools\prerequisites\') + Digest;
  CacheFile := CacheDir + '\' + FileName;
  for Index := 0 to 2 do begin
    if Index = 0 then Candidate := ExpandConstant('{src}\prereqs\') + FileName
    else if Index = 1 then Candidate := CacheFile
    else Candidate := ExpandConstant('{app}\prereqs\') + FileName;
    if FileExists(Candidate) then begin
      Log(Kind + ': checking local dependency candidate');
      if FileCopy(Candidate, Target, False) and VerifyDependency(FileName, Kind, Digest, Size) then Exit;
      DeleteFile(Target);
    end;
  end;
  Failure := '';
  for Index := 0 to 1 do begin
    if Index = 0 then SourceURL := AssetURL else SourceURL := FallbackURL;
    DependencyDownloadPage.Clear;
    DependencyDownloadPage.Add(SourceURL, FileName, Digest);
    DependencyDownloadStarted := GetTickCount64;
    DependencyDownloadSize := Size;
    DependencyDownloadTimedOut := False;
    Log(Kind + ': download submitted; source=' + SourceURL + '; deadline=600s');
    DependencyDownloadPage.Show;
    try
      try
        Log(Kind + ': download received; starting');
        DependencyDownloadPage.Download;
        if not VerifyDependency(FileName, Kind, Digest, Size) then
          RaiseException('组件大小、摘要或 Microsoft 签名校验失败');
        if ForceDirectories(CacheDir) then FileCopy(Target, CacheFile, False);
        Log(Kind + ': download and verification completed');
        Exit;
      except
        Failure := GetExceptionMessage;
        Log(Kind + ': download failed: ' + Failure);
        DeleteFile(Target);
        if DependencyDownloadPage.AbortedByUser then begin
          Result := '已取消组件下载，旧版程序保持运行。';
          Exit;
        end;
      end;
    finally
      DependencyDownloadPage.Hide;
    end;
  end;
  Result := Kind + ' 组件准备失败。可将离线组件放在安装包旁的 prereqs 目录后重试。' + #13#10 + Failure;
#endif
end;

function InstallDependency(Name, FileName, Kind, Digest: String; Size: Int64): String;
var
  ResultCode: Integer;
  InstallerPath, Params: String;
  Started: Int64;
begin
  Result := '';
  Started := GetTickCount64;
  Log(Name + ': request submitted; source=verified dependency; timeout=600s');
  try
    WizardForm.StatusLabel.Caption := '正在验证 ' + Name + ' 安装包，请稍候...';
    WizardForm.Refresh;
    ExtractTemporaryFile('run-prerequisite.ps1');
    InstallerPath := ExpandConstant('{tmp}\') + FileName;
    if CompareText(GetSHA256OfFile(InstallerPath), Digest) <> 0 then
      RaiseException(Name + ' 安装包完整性校验失败');
    WizardForm.StatusLabel.Caption := '正在安装 ' + Name + '，最多等待 10 分钟...';
    Params := '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' +
      ExpandConstant('{tmp}\run-prerequisite.ps1') + '" -Installer "' + InstallerPath +
      '" -Kind ' + Kind + ' -ExpectedSHA256 ' + Digest +
      ' -ExpectedSize ' + IntToStr(Size) + ' -TimeoutSeconds 600';
    Log(Name + ': starting bounded prerequisite worker');
    if not Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
      Params, '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
      RaiseException(Name + ' 安装进程未启动，错误码 ' + IntToStr(ResultCode));
    Log(Name + ': worker exit=' + IntToStr(ResultCode) +
      '; elapsed_ms=' + IntToStr(GetTickCount64 - Started));
    if (ResultCode = 3010) or (ResultCode = 1641) then
      DependencyRestart := True
    else if ResultCode <> 0 then
      RaiseException(Name + ' 安装失败或超时，错误码 ' + IntToStr(ResultCode));
  except
    Result := GetExceptionMessage;
    Log(Name + ': failed: ' + Result);
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  WizardForm.StatusLabel.Caption := '正在检查本机运行组件...';
  WizardForm.Refresh;
  Log('Checking machine-wide prerequisites before closing the running application');
  if not VCRuntimeInstalled() then begin
    Result := AcquireDependency('VC_redist.x64.exe', 'VC', '{#VCSHA256}',
      '{#VCAssetURL}', '{#VCFallbackURL}', {#VCSize});
    if Result <> '' then Exit;
  end;
  if not WebView2Installed() then begin
    Result := AcquireDependency('MicrosoftEdgeWebView2RuntimeInstallerX64.exe', 'WebView2',
      '{#WebViewSHA256}', '{#WebViewAssetURL}', '{#WebViewFallbackURL}', {#WebViewSize});
    if Result <> '' then Exit;
  end;
  if not ConfirmStopRunningApplicationForUpgrade() then begin
    Result := '用户已取消升级。';
    Exit;
  end;
  WizardForm.StatusLabel.Caption := '运行组件已就绪，正在准备安装...';
  WizardForm.Refresh;
  Log('Dependency acquisition completed; entering installation phase');
  if not VCRuntimeInstalled() then begin
    WizardForm.StatusLabel.Caption := '检测到 Visual C++ Runtime 缺失，准备离线安装...';
    WizardForm.Refresh;
    Result := InstallDependency('Visual C++ Runtime', 'VC_redist.x64.exe',
      'VC', '{#VCSHA256}', {#VCSize});
    if Result <> '' then begin NeedsRestart := DependencyRestart; Exit; end;
    if not VCRuntimeInstalled() then begin
      Result := 'Visual C++ Runtime 安装后检测未通过，请修复依赖后重试。';
      NeedsRestart := DependencyRestart;
      Exit;
    end;
  end else Log('Visual C++ Runtime: installed version satisfies requirement; skipped');
  if not WebView2Installed() then begin
    WizardForm.StatusLabel.Caption := '检测到 WebView2 Runtime 缺失，准备离线安装...';
    WizardForm.Refresh;
    Result := InstallDependency('WebView2 Runtime', 'MicrosoftEdgeWebView2RuntimeInstallerX64.exe',
      'WebView2', '{#WebViewSHA256}', {#WebViewSize});
    if Result <> '' then begin NeedsRestart := DependencyRestart; Exit; end;
    if not WebView2Installed() then begin
      Result := 'WebView2 Runtime 安装后检测未通过，请修复依赖后重试。';
      NeedsRestart := DependencyRestart;
      Exit;
    end;
  end else Log('WebView2 Runtime: installed; skipped');
  Log('Prerequisite verification completed');
end;

function NeedRestart(): Boolean;
begin
  Result := DependencyRestart;
end;

function CanStartApplication(): Boolean;
begin
  Result := not DependencyRestart;
end;

function ShouldAutoRestartApplication(): Boolean;
begin
  Result := WizardSilent and CanStartApplication and
    (CompareText(ExpandConstant('{param:CXVPNAUTORESTART|0}'), '1') = 0);
end;

function AppIsRunning(): Boolean;
begin
  Result := CheckForMutexes('Local\CXVPNTools.Singleton.v1') or
    CheckForMutexes('Local\CXVPNManager.Singleton.v1');
end;

function StopRunningApplication(): Boolean;
var
  Handle: THandle;
  Started: Int64;
begin
  Result := False;
  Started := GetTickCount64;
  Log('Graceful exit requested; waiting for routing transactions; timeout=90s');
  if not IsUninstaller then begin
    WizardForm.StatusLabel.Caption := '正在等待 CXVPNTools 完成当前操作并退出（最多 90 秒）...';
    WizardForm.Refresh;
  end;
  Handle := OpenEvent($0002, False, 'Local\CXVPNTools.Shutdown.v1');
  if Handle = 0 then begin
    if not ((IsUninstaller and UninstallSilent) or ((not IsUninstaller) and WizardSilent)) then
      MsgBox('当前版本不支持自动退出，请手动退出 CXVPNTools 后重试。', mbInformation, MB_OK);
    Exit;
  end;
  try
    if not SignalEvent(Handle) then Exit;
  finally
    CloseEvent(Handle);
  end;
  while AppIsRunning() and (GetTickCount64 - Started < 90000) do
    Sleep(100);
  if AppIsRunning() then begin
    Log('Graceful exit timed out; application preserved, installation cancelled');
    if not ((IsUninstaller and UninstallSilent) or ((not IsUninstaller) and WizardSilent)) then
      MsgBox('当前操作尚未结束，已保留应用运行。请等待操作完成后重试；安装器不会强制终止程序。', mbInformation, MB_OK);
    Exit;
  end;
  Log('Graceful exit confirmed; elapsed_ms=' + IntToStr(GetTickCount64 - Started));
  Result := True;
end;

function ConfirmStopRunningApplication(): Boolean;
var
  Form: TSetupForm;
  Description: TNewStaticText;
  ForceButton, CancelButton: TNewButton;
begin
  Result := True;
  if not AppIsRunning() then Exit;
  if UninstallSilent then begin
    Result := StopRunningApplication();
    Exit;
  end;
  Form := CreateCustomForm(ScaleX(520), ScaleY(190), False, True);
  try
    Form.Caption := 'CXVPNTools 正在运行';
    Description := TNewStaticText.Create(Form);
    Description.Parent := Form;
    Description.SetBounds(ScaleX(16), ScaleY(16), ScaleX(480), ScaleY(92));
    Description.AutoSize := False;
    Description.WordWrap := True;
    Description.Caption := '卸载前需要退出 CXVPNTools。' + #13#10 +
      '将等待当前配置操作完成后退出；无法安全退出时停止卸载。';
    ForceButton := TNewButton.Create(Form);
    ForceButton.Parent := Form;
    ForceButton.SetBounds(ScaleX(256), ScaleY(134), ScaleX(160), ScaleY(28));
    ForceButton.Caption := '退出并继续卸载';
    ForceButton.ModalResult := mrOk;
    CancelButton := TNewButton.Create(Form);
    CancelButton.Parent := Form;
    CancelButton.SetBounds(ScaleX(424), ScaleY(134), ScaleX(80), ScaleY(28));
    CancelButton.Caption := '取消卸载';
    CancelButton.ModalResult := mrCancel;
    CancelButton.Cancel := True;
    Form.ActiveControl := CancelButton;
    if Form.ShowModal() <> mrOk then begin
      Result := False;
      Exit;
    end;
    Result := StopRunningApplication();
  finally
    Form.Free();
  end;
end;

function ConfirmStopRunningApplicationForUpgrade(): Boolean;
var
  Form: TSetupForm;
  Heading, Description: TNewStaticText;
  ForceButton, CancelButton: TNewButton;
begin
  Result := True;
  if not AppIsRunning() then Exit;
  if WizardSilent then begin
    Result := StopRunningApplication();
    Exit;
  end;
  Form := CreateCustomForm(ScaleX(460), ScaleY(174), False, True);
  try
    Form.Caption := '升级 CXVPNTools';
    Form.Color := clWhite;
    Heading := TNewStaticText.Create(Form);
    Heading.Parent := Form;
    Heading.SetBounds(ScaleX(24), ScaleY(20), ScaleX(412), ScaleY(26));
    Heading.Font.Size := 12;
    Heading.Font.Style := [fsBold];
    Heading.Caption := '退出应用后继续升级';
    Description := TNewStaticText.Create(Form);
    Description.Parent := Form;
    Description.SetBounds(ScaleX(24), ScaleY(56), ScaleX(412), ScaleY(50));
    Description.AutoSize := False;
    Description.WordWrap := True;
    Description.Font.Color := $00555555;
    Description.Caption := 'CXVPNTools 正在运行。将等待当前配置操作完成后退出。' + #13#10 +
      '用户配置将保留；无法安全退出时停止升级。';
    ForceButton := TNewButton.Create(Form);
    ForceButton.Parent := Form;
    ForceButton.SetBounds(ScaleX(184), ScaleY(122), ScaleX(144), ScaleY(32));
    ForceButton.Caption := '退出并继续升级';
    ForceButton.ModalResult := mrOk;
    CancelButton := TNewButton.Create(Form);
    CancelButton.Parent := Form;
    CancelButton.SetBounds(ScaleX(340), ScaleY(122), ScaleX(96), ScaleY(32));
    CancelButton.Caption := '暂不升级';
    CancelButton.ModalResult := mrCancel;
    CancelButton.Cancel := True;
    Form.ActiveControl := CancelButton;
    if Form.ShowModal() <> mrOk then begin
      Result := False;
      Exit;
    end;
    Result := StopRunningApplication();
  finally
    Form.Free();
  end;
end;

function InitializeUninstall(): Boolean;
var
  Form: TSetupForm;
  Choice: TNewCheckBox;
  Description: TNewStaticText;
  ContinueButton, CancelButton: TNewButton;
begin
  DeleteUserData := False;
  Result := True;
  if not ConfirmStopRunningApplication() then begin
    Result := False;
    Exit;
  end;
  if UninstallSilent then Exit;
  Form := CreateCustomForm(ScaleX(520), ScaleY(200), False, True);
  try
    Form.Caption := '卸载 CXVPNTools';
    Choice := TNewCheckBox.Create(Form);
    Choice.Parent := Form;
    Choice.SetBounds(ScaleX(16), ScaleY(16), ScaleX(480), ScaleY(24));
    Choice.Caption := '删除用户配置、凭据和缓存';
    Choice.Checked := False;
    Description := TNewStaticText.Create(Form);
    Description.Parent := Form;
    Description.SetBounds(ScaleX(16), ScaleY(52), ScaleX(480), ScaleY(88));
    Description.AutoSize := False;
    Description.WordWrap := True;
    Description.Caption := '不勾选将保留数据。勾选后清理以下目录：' + #13#10 +
      ExpandConstant('{localappdata}\CXVPNTools') + #13#10 +
      ExpandConstant('{commonappdata}\CXVPNManager\RoutingService');
    ContinueButton := TNewButton.Create(Form);
    ContinueButton.Parent := Form;
    ContinueButton.SetBounds(ScaleX(304), ScaleY(158), ScaleX(96), ScaleY(28));
    ContinueButton.Caption := '继续卸载';
    ContinueButton.ModalResult := mrOk;
    CancelButton := TNewButton.Create(Form);
    CancelButton.Parent := Form;
    CancelButton.SetBounds(ScaleX(408), ScaleY(158), ScaleX(96), ScaleY(28));
    CancelButton.Caption := '取消';
    CancelButton.ModalResult := mrCancel;
    CancelButton.Cancel := True;
    Form.ActiveControl := CancelButton;
    Result := Form.ShowModal() = mrOk;
    DeleteUserData := Result and Choice.Checked;
  finally
    Form.Free();
  end;
end;

procedure CurUninstallStepChanged(Step: TUninstallStep);
var
  Params: String;
  ResultCode: Integer;
begin
  if Step = usUninstall then begin
    Log('Service cleanup submitted; target=CXVPNRoutingService; stop timeout=30s; delete timeout=10s');
    Params := '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' +
      ExpandConstant('{app}\installer\uninstall-service.ps1') + '"';
    if not Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
      Params, '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
      RaiseException('路由服务清理进程未启动，请关闭软件后重试。');
    Log('Service cleanup completed; exit=' + IntToStr(ResultCode));
    if ResultCode <> 0 then
      RaiseException('路由服务清理失败，已停止卸载。错误码：' + IntToStr(ResultCode));
  end;
  if (Step = usPostUninstall) and DeleteUserData then begin
    if not DelTree(ExpandConstant('{localappdata}\CXVPNTools'), True, True, True) then
      MsgBox('部分用户数据未能删除，请在关闭程序后检查用户数据目录。', mbError, MB_OK);
    if not DelTree(ExpandConstant('{commonappdata}\CXVPNManager\RoutingService'), True, True, True) then
      MsgBox('部分服务数据未能删除，请检查服务数据目录。', mbError, MB_OK);
  end;
end;
