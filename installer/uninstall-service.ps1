[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$OutputEncoding = [Console]::OutputEncoding
$service = $null
$process = $null
try {
    $service = Get-Service -Name 'CXVPNRoutingService' -ErrorAction SilentlyContinue
    if (-not $service) { exit 0 }
    if ($service.Status -ne [ServiceProcess.ServiceControllerStatus]::Stopped) {
        # A normal service stop lets the existing host restore the system proxy.
        $service.Stop()
        $service.WaitForStatus([ServiceProcess.ServiceControllerStatus]::Stopped, [TimeSpan]::FromSeconds(30))
    }
    $service.Dispose()
    $service = $null
    $process = Start-Process -FilePath (Join-Path $env:SystemRoot 'System32\sc.exe') -ArgumentList 'delete CXVPNRoutingService' -WindowStyle Hidden -PassThru
    if (-not $process.WaitForExit(10000)) { $process.Kill(); exit 1460 }
    if ($process.ExitCode -notin @(0, 1060)) { exit $process.ExitCode }
    exit 0
} catch {
    try {
        $log = Join-Path ([IO.Path]::GetTempPath()) 'CXVPNTools-uninstall-service.log'
        [IO.File]::AppendAllText($log, [DateTime]::UtcNow.ToString('o') + ' ' + $_.Exception.ToString() + [Environment]::NewLine, [Text.UTF8Encoding]::new($false))
    } catch { }
    exit 9001
} finally {
    if ($null -ne $service) { $service.Dispose() }
    if ($null -ne $process) { $process.Dispose() }
}
