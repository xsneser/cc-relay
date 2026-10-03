; =====================================================================
; CC Relay - Inno Setup 6 安装与卸载配置脚本
; 支持集成 ASR 离线环境与 SenseVoice 模型打包
; 支持卸载时同步询问与协同卸载 Codex (CPA)、Gemini (Anti) 和 语音伴侣
; =====================================================================

#define MyAppName "CC Relay"
#define MyAppVersion "2.4.10"
#define MyAppPublisher "CC Relay Team"
#define MyAppURL "https://github.com/xsneser/cc-relay"
#define MyAppExeName "cc-relay.exe"

[Setup]
AppId={{8B7B2E3D-9D22-4B2E-99C1-7FE523C6D381}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
DefaultDirName={localappdata}\Programs\CC-Relay
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
; Per-User 当前用户安装模式：无需管理员提权，与 Antigravity Tools (%LOCALAPPDATA%) 权限完全对齐
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=commandline
OutputDir=output
OutputBaseFilename=CC-Relay-Setup-v{#MyAppVersion}
SetupIconFile=compiler:SetupClassicIcon.ico
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
CloseApplications=force
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}
ArchitecturesInstallIn64BitMode=x64

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "addtopath"; Description: "将 CC Relay wrapper 目录添加至当前用户 PATH 环境变量 (直接运行 claude 命令直通中转)"; GroupDescription: "系统环境配置:"; Flags: checkedonce
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加快捷方式:"; Flags: unchecked
Name: "autostart"; Description: "开机自动在后台启动 CC Relay 服务"; GroupDescription: "启动选项:"; Flags: unchecked

[Files]
; 主服务二进制可执行程序 (由 PyInstaller 生成)
Source: "installer_build\staging\cc-relay.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "installer_build\staging\ui.html"; DestDir: "{app}"; Flags: ignoreversion
Source: "installer_build\staging\stop_relay.bat"; DestDir: "{app}"; Flags: ignoreversion
Source: "installer_build\staging\start_relay.bat"; DestDir: "{app}"; Flags: ignoreversion
Source: "installer_build\staging\build_info.json"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist
Source: "installer_build\staging\THIRD_PARTY_NOTICES.md"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist

; 配置文件模板 (始终覆盖为最新模板)
Source: "installer_build\staging\config.example.json"; DestDir: "{app}"; Flags: ignoreversion
; 用户运行时配置：仅当不存在时释放，覆盖升级安装时绝对不覆盖现有配置与密钥
Source: "installer_build\staging\config.example.json"; DestDir: "{app}"; DestName: "config.json"; Flags: onlyifdoesntexist uninsneveruninstall

; 【组件 1】Codex CPA 代理网关目录 (包含 cli-proxy-api.exe)
Source: "installer_build\staging\codex-proxy\*"; DestDir: "{app}\codex-proxy"; Flags: ignoreversion recursesubdirs createallsubdirs

; 【组件 3】语音伴侣完整运行环境 (包含 runtime 便携 Python 与 models 离线 SenseVoice 模型)
Source: "installer_build\staging\tools\voice_input\*"; DestDir: "{app}\tools\voice_input"; Flags: ignoreversion recursesubdirs createallsubdirs

; Claude CLI 自动包装层 (加入 PATH 后让 claude 命令直通中转)
Source: "installer_build\staging\wrapper\*"; DestDir: "{app}\wrapper"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName} 控制台"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\停止 {#MyAppName} 服务"; Filename: "{app}\stop_relay.bat"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName} 控制台"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Registry]
; 开机自启动项 (可选)
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "{#MyAppName}"; ValueData: """{app}\{#MyAppExeName}"""; Flags: uninsdeletevalue; Tasks: autostart

[Run]
; 安装完成后自动拉起主程序
Filename: "{app}\{#MyAppExeName}"; Description: "立即启动 {#MyAppName} 并打开监控控制台"; Flags: postinstall nowait skipifsilent

[Code]
const
  WM_SETTINGCHANGE = $001A;
  SMTO_ABORTIFHUNG = $0002;

// Win32 API 声明：通知 Windows Shell 与终端环境变量已发生变更
function SendMessageTimeout(hWnd: HWND; Msg: UINT; wParam: Longint; lParam: String; fuFlags: UINT; uTimeout: UINT; out lpdwResult: DWORD): Longint;
  external 'SendMessageTimeoutW@user32.dll stdcall';

var
  // 卸载交互界面复选框全局引用
  ChkUninstallVoice: TNewCheckBox;
  ChkUninstallCodex: TNewCheckBox;
  ChkUninstallAnti:  TNewCheckBox;
  ChkCleanUserData:  TNewCheckBox;

  // 卸载选项布尔缓存 (防止 Form.Free 后访问失效指针)
  OptUninstallVoice: Boolean;
  OptUninstallCodex: Boolean;
  OptUninstallAnti:  Boolean;
  OptCleanUserData:  Boolean;

  // 组件探针状态
  HasVoice: Boolean;
  HasCodex: Boolean;
  HasAnti:  Boolean;
  AntiUninstallPath: String;
  CodexUninstallPath: String;

// =====================================================================
// 环境变量 PATH 安全增删实用函数
// =====================================================================

function AddPathToUserEnv(PathToAdd: String): Boolean;
var
  CurrentPath: String;
  NewPath: String;
  Dummy: DWORD;
begin
  Result := False;
  if not RegQueryStringValue(HKCU, 'Environment', 'PATH', CurrentPath) then
    CurrentPath := '';

  if Pos(';' + Uppercase(PathToAdd) + ';', ';' + Uppercase(CurrentPath) + ';') = 0 then
  begin
    if (CurrentPath <> '') and (CurrentPath[Length(CurrentPath)] <> ';') then
      CurrentPath := CurrentPath + ';';
    NewPath := CurrentPath + PathToAdd;
    if RegWriteStringValue(HKCU, 'Environment', 'PATH', NewPath) then
    begin
      SendMessageTimeout(HWND_BROADCAST, WM_SETTINGCHANGE, 0, 'Environment', SMTO_ABORTIFHUNG, 3000, Dummy);
      Result := True;
    end;
  end;
end;

function RemovePathFromUserEnv(PathToRemove: String): Boolean;
var
  CurrentPath: String;
  P, L: Integer;
  UpperCurrent, UpperRemove: String;
  Dummy: DWORD;
begin
  Result := False;
  if RegQueryStringValue(HKCU, 'Environment', 'PATH', CurrentPath) then
  begin
    UpperCurrent := ';' + Uppercase(CurrentPath) + ';';
    UpperRemove := ';' + Uppercase(PathToRemove) + ';';
    P := Pos(UpperRemove, UpperCurrent);
    if P > 0 then
    begin
      L := Length(PathToRemove);
      Delete(CurrentPath, P, L);
      // 清理可能产生的双分号
      StringChangeEx(CurrentPath, ';;', ';', True);
      if (Length(CurrentPath) > 0) and (CurrentPath[1] = ';') then
        Delete(CurrentPath, 1, 1);
      if (Length(CurrentPath) > 0) and (CurrentPath[Length(CurrentPath)] = ';') then
        Delete(CurrentPath, Length(CurrentPath), 1);
      RegWriteStringValue(HKCU, 'Environment', 'PATH', CurrentPath);
      SendMessageTimeout(HWND_BROADCAST, WM_SETTINGCHANGE, 0, 'Environment', SMTO_ABORTIFHUNG, 3000, Dummy);
      Result := True;
    end;
  end;
end;

// =====================================================================
// 安装阶段生命周期回调
// =====================================================================

function InitializeSetup(): Boolean;
var
  ResultCode: Integer;
begin
  Result := True;
  // 安装前若发现正在运行 cc-relay，自动请求结束其进程以防文件锁定
  Exec('taskkill.exe', '/F /IM cc-relay.exe', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
  begin
    // 若用户勾选了添加 PATH 任务，执行安全写入并广播
    if WizardIsTaskSelected('addtopath') then
      AddPathToUserEnv(ExpandConstant('{app}\wrapper'));
  end;
end;

// =====================================================================
// 卸载阶段生命周期与伴侣组件深度协同 (CPA, Anti, Voice)
// =====================================================================

function InitializeUninstall(): Boolean;
var
  Form: TSetupForm;
  LblTitle, LblPrompt: TNewStaticText;
  BtnOK, BtnCancel: TNewButton;
  VoicePath, CodexPath, RegAntiStr: String;
begin
  Result := False;

  // 1. 探针：检查 Gemini (Antigravity Tools) 是否安装
  HasAnti := False;
  AntiUninstallPath := '';
  if RegQueryStringValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Uninstall\Antigravity Tools', 'UninstallString', RegAntiStr) then
  begin
    // 清理双引号
    StringChangeEx(RegAntiStr, '"', '', True);
    if FileExists(RegAntiStr) then
    begin
      AntiUninstallPath := RegAntiStr;
      HasAnti := True;
    end;
  end;
  if not HasAnti then
  begin
    AntiUninstallPath := ExpandConstant('{localappdata}\Antigravity Tools\uninstall.exe');
    HasAnti := FileExists(AntiUninstallPath);
  end;

  // 2. 探针：检查 Codex (CLIProxyAPI) 是否安装
  CodexPath := ExpandConstant('{app}\codex-proxy\cli-proxy-api.exe');
  HasCodex := FileExists(CodexPath);
  CodexUninstallPath := ExpandConstant('{app}\codex-proxy\uninstall.exe');

  // 3. 探针：检查 语音伴侣 (Voice ASR) 是否安装
  VoicePath := ExpandConstant('{app}\tools\voice_input');
  HasVoice := DirExists(VoicePath);

  // 4. 构建原生卸载确认与协同卸载交互对话框
  Form := CreateCustomForm(ScaleX(500), ScaleY(330), False, True);
  try
    Form.Caption := '{#MyAppName} 卸载向导';

    LblTitle := TNewStaticText.Create(Form);
    LblTitle.Parent := Form;
    LblTitle.SetBounds(ScaleX(20), ScaleY(15), ScaleX(460), ScaleY(24));
    LblTitle.Font.Size := 11;
    LblTitle.Font.Style := [fsBold];
    LblTitle.Caption := '协同组件卸载与清理确认';

    LblPrompt := TNewStaticText.Create(Form);
    LblPrompt.Parent := Form;
    LblPrompt.SetBounds(ScaleX(20), ScaleY(45), ScaleX(460), ScaleY(40));
    LblPrompt.WordWrap := True;
    LblPrompt.Caption := '检测到以下与 {#MyAppName} 深度集成的伴侣服务组件。请选择您希望一并卸载或清理的项目：';

    // 复选框 1: 语音 ASR 离线环境与模型 (~380MB)
    ChkUninstallVoice := TNewCheckBox.Create(Form);
    ChkUninstallVoice.Parent := Form;
    ChkUninstallVoice.SetBounds(ScaleX(30), ScaleY(95), ScaleX(440), ScaleY(22));
    ChkUninstallVoice.Caption := '卸载 语音伴侣 (Voice ASR) 离线运行环境与 SenseVoice 模型 (~380MB)';
    ChkUninstallVoice.Checked := HasVoice;
    ChkUninstallVoice.Enabled := HasVoice;

    // 复选框 2: Codex (CLIProxyAPI)
    ChkUninstallCodex := TNewCheckBox.Create(Form);
    ChkUninstallCodex.Parent := Form;
    ChkUninstallCodex.SetBounds(ScaleX(30), ScaleY(125), ScaleX(440), ScaleY(22));
    if FileExists(CodexUninstallPath) then
      ChkUninstallCodex.Caption := '卸载 Codex 代理网关 (拉起 CLIProxyAPI 独立卸载程序)'
    else
      ChkUninstallCodex.Caption := '卸载 Codex 代理网关 (停止进程并清理 codex-proxy 目录)';
    ChkUninstallCodex.Checked := False; // 涉及账户登录 Token，默认不主动勾选以防误删
    ChkUninstallCodex.Enabled := HasCodex;

    // 复选框 3: Gemini (Antigravity Tools)
    ChkUninstallAnti := TNewCheckBox.Create(Form);
    ChkUninstallAnti.Parent := Form;
    ChkUninstallAnti.SetBounds(ScaleX(30), ScaleY(155), ScaleX(440), ScaleY(22));
    if HasAnti then
      ChkUninstallAnti.Caption := '卸载 Gemini 伴侣 (拉起 Antigravity Tools 官方卸载程序)'
    else
      ChkUninstallAnti.Caption := '卸载 Gemini 伴侣 (系统未检测到 Antigravity Tools)';
    ChkUninstallAnti.Checked := False; // 第三方独立工具，默认不主动勾选
    ChkUninstallAnti.Enabled := HasAnti;

    // 复选框 4: 用户自定义数据与配置
    ChkCleanUserData := TNewCheckBox.Create(Form);
    ChkCleanUserData.Parent := Form;
    ChkCleanUserData.SetBounds(ScaleX(30), ScaleY(195), ScaleX(440), ScaleY(22));
    ChkCleanUserData.Caption := '彻底清除用户个人配置文件 (包含 config.json、历史会话与 API 密钥)';
    ChkCleanUserData.Checked := False; // 默认保留，保护用户个人资产

    BtnOK := TNewButton.Create(Form);
    BtnOK.Parent := Form;
    BtnOK.SetBounds(ScaleX(300), ScaleY(270), ScaleX(85), ScaleY(30));
    BtnOK.Caption := '开始卸载';
    BtnOK.ModalResult := mrOk;
    BtnOK.Default := True;

    BtnCancel := TNewButton.Create(Form);
    BtnCancel.Parent := Form;
    BtnCancel.SetBounds(ScaleX(395), ScaleY(270), ScaleX(85), ScaleY(30));
    BtnCancel.Caption := '取消';
    BtnCancel.ModalResult := mrCancel;

    if Form.ShowModal = mrOk then
    begin
      OptUninstallVoice := ChkUninstallVoice.Checked;
      OptUninstallCodex := ChkUninstallCodex.Checked;
      OptUninstallAnti  := ChkUninstallAnti.Checked;
      OptCleanUserData  := ChkCleanUserData.Checked;
      Result := True;
    end;
  finally
    Form.Free;
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  ResultCode: Integer;
begin
  if CurUninstallStep = usUninstall then
  begin
    // -------------------------------------------------------------
    // 步骤 1: 安全停止主程序与相关后台进程
    // -------------------------------------------------------------
    Exec('taskkill.exe', '/F /IM cc-relay.exe', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
    Exec('taskkill.exe', '/F /IM pythonw.exe /FI "WINDOWTITLE eq *cc_relay*"', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);

    // -------------------------------------------------------------
    // 步骤 2: 处理 语音伴侣 (ASR)
    // -------------------------------------------------------------
    if OptUninstallVoice then
    begin
      // 停止占用 8401 端口的语音进程
      Exec('cmd.exe', '/C for /f "tokens=5" %a in (''netstat -aon ^| findstr :8401 ^| findstr LISTENING'') do taskkill /F /PID %a', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
      // 清空语音 runtime 和 models 目录
      DelTree(ExpandConstant('{app}\tools\voice_input\runtime'), True, True, True);
      DelTree(ExpandConstant('{app}\tools\voice_input\models'), True, True, True);
      DelTree(ExpandConstant('{app}\tools\voice_input'), True, True, True);
    end;

    // -------------------------------------------------------------
    // 步骤 3: 处理 Codex (CLIProxyAPI)
    // -------------------------------------------------------------
    if OptUninstallCodex then
    begin
      // 停止 CPA 进程
      Exec('taskkill.exe', '/F /IM cli-proxy-api.exe', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
      // 优先拉起独立卸载 exe
      if FileExists(CodexUninstallPath) then
      begin
        Exec(CodexUninstallPath, '', '', SW_SHOW, ewWaitUntilTerminated, ResultCode);
      end
      else
      begin
        // 绿色便携包，直接清理目录
        DelTree(ExpandConstant('{app}\codex-proxy'), True, True, True);
      end;
    end;

    // -------------------------------------------------------------
    // 步骤 4: 处理 Gemini (Antigravity Tools)
    // -------------------------------------------------------------
    if OptUninstallAnti and HasAnti then
    begin
      // 停止 Antigravity Tools 进程
      Exec('taskkill.exe', '/F /IM antigravity-tools.exe', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
      // 拉起 Anti 官方原生卸载器并等待其完成
      Exec(AntiUninstallPath, '', '', SW_SHOW, ewWaitUntilTerminated, ResultCode);
    end;

    // -------------------------------------------------------------
    // 步骤 5: 处理用户个人配置与历史日志
    // -------------------------------------------------------------
    if OptCleanUserData then
    begin
      DeleteFile(ExpandConstant('{app}\config.json'));
      DeleteFile(ExpandConstant('{app}\records.jsonl'));
      DeleteFile(ExpandConstant('{app}\records.v1.jsonl'));
      DeleteFile(ExpandConstant('{app}\relay_launch.log'));
      DeleteFile(ExpandConstant('{app}\serve.log'));
      DeleteFile(ExpandConstant('{app}\voice.out.log'));
      DeleteFile(ExpandConstant('{app}\voice.err.log'));
      DeleteFile(ExpandConstant('{app}\.voice.pid'));
      DeleteFile(ExpandConstant('{app}\.watch.pid'));
    end;

    // -------------------------------------------------------------
    // 步骤 6: 清理用户 PATH 环境变量
    // -------------------------------------------------------------
    RemovePathFromUserEnv(ExpandConstant('{app}\wrapper'));
  end;
end;
