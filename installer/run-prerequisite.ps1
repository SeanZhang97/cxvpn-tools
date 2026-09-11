[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Installer,
    [Parameter(Mandatory)][ValidateSet('VC', 'WebView2')][string]$Kind,
    [Parameter(Mandatory)][ValidatePattern('^[0-9a-fA-F]{64}$')][string]$ExpectedSHA256,
    [ValidateRange(1, 1200)][int]$TimeoutSeconds = 600
)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [Console]::OutputEncoding
$logPath = Join-Path $PSScriptRoot ('prerequisite-' + $Kind + '.log')
function Write-Stage([string]$Message) {
    $line = [DateTime]::UtcNow.ToString('o') + ' ' + $Message + [Environment]::NewLine
    try { [IO.File]::AppendAllText($logPath, $line, [Text.UTF8Encoding]::new($false)) } catch { }
}
$process = $null
$watch = [Diagnostics.Stopwatch]::StartNew()
try {
    Write-Stage "worker received; kind=$Kind; timeout=$($TimeoutSeconds)s"
    if ((Get-FileHash -LiteralPath $Installer -Algorithm SHA256).Hash -ne $ExpectedSHA256) {
        throw 'Prerequisite digest mismatch'
    }
    $arguments = if ($Kind -eq 'VC') { '/install /quiet /norestart' } else { '/silent /install' }
    Write-Stage "starting verified installer: $Installer"
    $process = Start-Process -FilePath $Installer -ArgumentList $arguments -WindowStyle Hidden -PassThru
    if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
        Write-Stage "timeout; pid=$($process.Id); elapsed=$($watch.Elapsed.TotalSeconds)"
        # Shared MSI/Edge services may serve other apps. Only stop our direct child.
        try { $process.Kill() } catch { Write-Stage "terminate failed: $($_.Exception.GetType().Name)" }
        exit 1460
    }
    $code = $process.ExitCode
    Write-Stage "completed; exit=$code; elapsed=$($watch.Elapsed.TotalSeconds)"
    exit $code
} catch {
    Write-Stage "failed; type=$($_.Exception.GetType().FullName); message=$($_.Exception.Message); elapsed=$($watch.Elapsed.TotalSeconds)"
    exit 9001
} finally {
    if ($null -ne $process) { $process.Dispose() }
}
