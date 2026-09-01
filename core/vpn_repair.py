# -*- coding: utf-8 -*-
"""core/vpn_repair.py - RasMan / IKE / IPsec 服务级故障修复 (连接卡死兜底)

VPN 卡在"正在连接/断开"死锁、RasMan 无法正常停止、IKE/IPsec 服务异常时的
最后手段: 断开当前 VPN -> 停依赖服务 -> 停 RasMan (卡死时严格隔离校验后只
终止 RasMan 专属 svchost, 绝不按名杀 svchost) -> 重启 PolicyAgent/IKEEXT/RasMan。

设计 (借鉴同事 Repair-Company-IKEv2-VPN.cmd, Python 内联 PowerShell):
- 非管理员: Start-Process -Verb RunAs 自提权 (弹 UAC, 用户同意后提权运行)
- 结果落 %TEMP%\\cxvpn_repair_result.json, Python 同步读取
- 严格隔离校验: 仅在 RasMan 进程只托管 RasMan 单服务 且 路径=System32\\svchost.exe
  时才终止该进程, 杜绝误杀共享 svchost (绝不 taskkill svchost.exe)
"""
import ctypes
import json
import os
import subprocess
import tempfile
import time

_CREATE_NO_WINDOW = 0x08000000


def _is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


_REPAIR_PS = r'''
$ErrorActionPreference = 'Stop'
$ResultFile = $script:ResultFile = $args[0]

function Write-Result($Ok, $Msg) {
    try {
        @{ ok = [bool]$Ok; msg = [string]$Msg } |
            ConvertTo-Json -Compress |
            Set-Content -LiteralPath $script:ResultFile -Encoding UTF8
    } catch { }
}

function Get-ServiceCim($Name) {
    $s = @(Get-CimInstance -ClassName Win32_Service -Filter "Name='$Name'" -ErrorAction Stop)
    if ($s.Count -ne 1) { throw "Service '$Name' not found uniquely." }
    return $s[0]
}

function Wait-ServiceState($Name, [string[]]$Accepted, $TimeoutMs) {
    $t = [Diagnostics.Stopwatch]::StartNew()
    do {
        $s = Get-ServiceCim $Name
        if ($Accepted -contains [string]$s.State) { return $s }
        Start-Sleep -Milliseconds 400
    } while ($t.ElapsedMilliseconds -lt $TimeoutMs)
    return (Get-ServiceCim $Name)
}

function Invoke-NativeBounded($File, [string[]]$ArgList, $TimeoutMs, $Op) {
    $si = New-Object Diagnostics.ProcessStartInfo
    $si.FileName = $File
    $si.Arguments = ($ArgList | ForEach-Object {
        if ($_ -match '[\s"]') { '"' + ($_ -replace '"','""') + '"' } else { $_ }
    }) -join ' '
    $si.UseShellExecute = $false
    $si.CreateNoWindow = $true
    $si.RedirectStandardOutput = $true
    $si.RedirectStandardError = $true
    $p = New-Object Diagnostics.Process
    $p.StartInfo = $si
    try {
        if (-not $p.Start()) { throw "Unable to start $Op" }
        $so = $p.StandardOutput.ReadToEndAsync()
        $se = $p.StandardError.ReadToEndAsync()
        if (-not $p.WaitForExit($TimeoutMs)) {
            try { $p.Kill() } catch { }
            $p.WaitForExit(2000) | Out-Null
            throw "$Op timed out"
        }
        $p.WaitForExit()
        $result = [pscustomobject]@{ ExitCode = $p.ExitCode; Out = "$so $se" }
        if ($p.ExitCode -ne 0) {
            throw "$Op failed (exit=$($p.ExitCode)): $($result.Out)"
        }
        $result
    } finally {
        $p.Dispose()
    }
}

function Stop-ServiceBounded($Name, $TimeoutMs = 12000) {
    $s = Get-ServiceCim $Name
    if ($s.State -eq 'Stopped') { return }
    if ($s.State -ne 'Stop Pending') {
        try { Invoke-NativeBounded "$env:SystemRoot\System32\sc.exe" @('stop',$Name) 5000 "stop $Name" | Out-Null }
        catch {
            $s2 = Get-ServiceCim $Name
            if (@('Stopped','Stop Pending') -notcontains [string]$s2.State) { throw }
        }
    }
    $s = Wait-ServiceState $Name @('Stopped') $TimeoutMs
    if ($s.State -ne 'Stopped') { throw "$Name did not stop (state=$($s.State))" }
}

function Ensure-Running($Name, $TimeoutMs = 12000) {
    $s = Get-ServiceCim $Name
    if ($s.State -eq 'Running') { return }
    if ($s.State -eq 'Stop Pending') {
        $s = Wait-ServiceState $Name @('Stopped','Running') 5000
        if ($s.State -eq 'Running') { return }
    }
    if ($s.State -ne 'Start Pending') {
        try { Invoke-NativeBounded "$env:SystemRoot\System32\sc.exe" @('start',$Name) 5000 "start $Name" | Out-Null }
        catch {
            $s2 = Get-ServiceCim $Name
            if (@('Running','Start Pending') -notcontains [string]$s2.State) { throw }
        }
    }
    $s = Wait-ServiceState $Name @('Running') $TimeoutMs
    if ($s.State -ne 'Running') { throw "$Name did not start (state=$($s.State))" }
}

function Stop-IsolatedRasMan([string[]]$AcceptedStates = @('Stop Pending')) {
    # 仅在 RasMan 状态符合预期且其 svchost 仅托管 RasMan 单服务、
    # 路径=System32\svchost.exe 时, 才终止该进程; 杜绝误杀共享 svchost。
    $s = Get-ServiceCim 'RasMan'
    if ($AcceptedStates -notcontains [string]$s.State) { return $false }
    $rasPid = [int]$s.ProcessId
    if ($rasPid -le 0) { return $false }
    $hosted = @(Get-CimInstance Win32_Service -Filter "ProcessId=$rasPid" -ErrorAction Stop)
    if ($hosted.Count -ne 1 -or [string]$hosted[0].Name -ne 'RasMan') { return $false }
    $procs = @(Get-CimInstance Win32_Process -Filter "ProcessId=$rasPid" -ErrorAction Stop)
    if ($procs.Count -ne 1) { return $false }
    $pr = $procs[0]
    if ([string]$pr.Name -ine 'svchost.exe') { return $false }
    $exp = [IO.Path]::GetFullPath((Join-Path $env:SystemRoot 'System32\svchost.exe'))
    if ([string]::IsNullOrWhiteSpace($pr.ExecutablePath)) { return $false }
    $act = [IO.Path]::GetFullPath($pr.ExecutablePath)
    if (-not [string]::Equals($act,$exp,'OrdinalIgnoreCase')) { return $false }
    try {
        $po = Get-Process -Id $rasPid -ErrorAction Stop
        if ([string]$po.ProcessName -ine 'svchost') { return $false }
        $h = $po.Handle  # 取 handle 证明进程仍存活
        if ($h -eq [IntPtr]::Zero) { return $false }
        Stop-Process -InputObject $po -Force -ErrorAction Stop
        return $true
    } catch { return $false }
}

function Stop-RasManBounded {
    $s = Get-ServiceCim 'RasMan'
    if ($s.State -eq 'Stopped') { return }
    $oldPid = [int]$s.ProcessId
    if ($s.State -ne 'Stop Pending') {
        try { Invoke-NativeBounded "$env:SystemRoot\System32\sc.exe" @('stop','RasMan') 5000 'stop RasMan' | Out-Null }
        catch { }
    }
    $s = Wait-ServiceState 'RasMan' @('Stopped') 12000
    if ($s.State -eq 'Stopped') { return }
    if (@('Running','Stop Pending') -contains [string]$s.State) {
        $killed = Stop-IsolatedRasMan @('Running','Stop Pending')
        if ($killed) {
            $t = [Diagnostics.Stopwatch]::StartNew()
            do {
                $s = Get-ServiceCim 'RasMan'
                if ($s.State -eq 'Stopped') { return }
                if ($s.State -eq 'Running' -and [int]$s.ProcessId -ne $oldPid) {
                    return  # SCM 已用新进程拉起，旧 RAS 状态已经清空
                }
                Start-Sleep -Milliseconds 400
            } while ($t.ElapsedMilliseconds -lt 12000)
        }
    }
    throw "RasMan did not reset (state=$($s.State), pid=$($s.ProcessId))"
}

$mainErr = ''
try {
    # 1) 断开当前所有 VPN
    try { Invoke-NativeBounded "$env:SystemRoot\System32\rasdial.exe" @('/disconnect') 6000 'disconnect all' | Out-Null } catch { }
    Start-Sleep -Milliseconds 600

    # 2) 停 RasMan 依赖服务
    $dep = @(Get-Service -Name RasMan -DependentServices -ErrorAction SilentlyContinue |
        Where-Object { [string]$_.Status -ne 'Stopped' } | ForEach-Object { [string]$_.Name })
    foreach ($d in $dep) { try { Stop-ServiceBounded $d 10000 } catch { } }

    # 3) 停 + 重启 RasMan / IKE / IPsec
    Stop-RasManBounded
    Ensure-Running 'PolicyAgent'
    Ensure-Running 'IKEEXT'
    Ensure-Running 'RasMan'
    foreach ($d in $dep) { try { Ensure-Running $d } catch { } }

    foreach ($n in 'PolicyAgent','IKEEXT','RasMan') {
        if ((Get-ServiceCim $n).State -ne 'Running') { throw "Final check failed: $n not running" }
    }
    Write-Result $true 'RasMan 与 IKE/IPsec 服务已重启, 请稍候重试连接'
    exit 0
} catch {
    $mainErr = $_.Exception.Message
}
# 兜底: 无论成败, 确保三个核心服务在运行
foreach ($n in 'PolicyAgent','IKEEXT','RasMan') { try { Ensure-Running $n } catch { } }
Write-Result $false ('修复未完成: ' + $mainErr)
exit 1
'''


def repair(log=print, timeout=120):
    """执行 RasMan/IKE/IPsec 服务修复, 返回 (ok, msg)。

    非管理员时弹 UAC 申请提权; 结果写入临时 json 后由本函数读取。
    超时 (含未响应 UAC) 返回失败。
    """
    result_file = os.path.join(tempfile.gettempdir(),
                               'cxvpn_repair_result.json')
    try:
        os.remove(result_file)
    except OSError:
        pass
    ps1 = os.path.join(tempfile.gettempdir(), 'cxvpn_repair.ps1')
    # utf-8-sig 带 BOM: PowerShell 5.1 的 -File 默认按 ANSI(GBK)读无 BOM
    # 文件, 中文会被错位解析; 带 BOM 才能正确按 UTF-8 读取。
    with open(ps1, 'w', encoding='utf-8-sig') as f:
        f.write(_REPAIR_PS)
    admin = _is_admin()
    if log:
        log('[repair] %s' % ('已具备管理员权限'
                             if admin else '将申请管理员权限 (UAC)'))
    try:
        if admin:
            subprocess.run(
                ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                 '-File', ps1, result_file],
                timeout=timeout + 30,
                creationflags=_CREATE_NO_WINDOW)
        else:
            inner = ('-NoProfile -ExecutionPolicy Bypass -File '
                     '"%s" "%s"' % (ps1, result_file))
            cmd = ("Start-Process powershell -Verb RunAs -WindowStyle Hidden -Wait "
                   "-ArgumentList '%s'" % inner)
            subprocess.run(
                ['powershell', '-NoProfile', '-Command', cmd],
                timeout=timeout + 60,
                creationflags=_CREATE_NO_WINDOW)
    except subprocess.TimeoutExpired:
        return False, '修复超时 (服务无响应或未响应 UAC 授权)'
    # -Wait 偶发不可靠: 再轮询几秒等结果文件落盘
    deadline = time.time() + 10
    while time.time() < deadline and not os.path.exists(result_file):
        time.sleep(0.4)
    if not os.path.exists(result_file):
        return False, '修复未返回结果 (可能未授权 UAC)'
    try:
        # Windows PowerShell 5.1 的 Set-Content -Encoding UTF8 会写 BOM。
        with open(result_file, encoding='utf-8-sig') as f:
            data = json.load(f)
        return bool(data.get('ok')), str(data.get('msg') or '')
    except Exception as e:
        return False, '结果解析失败: %s' % e


if __name__ == '__main__':
    ok, msg = repair()
    print(('成功' if ok else '失败'), msg)
