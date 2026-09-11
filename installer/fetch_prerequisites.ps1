[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$lock = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'prereqs.lock.json') -Encoding UTF8 -Raw | ConvertFrom-Json
$destination = Join-Path $PSScriptRoot 'prereqs'
New-Item -ItemType Directory -Path $destination -Force | Out-Null
foreach ($entry in $lock.dependencies) {
    if ($entry.file -notin @('MicrosoftEdgeWebView2RuntimeInstallerX64.exe', 'VC_redist.x64.exe')) { throw 'Unexpected dependency filename' }
    $target = Join-Path $destination $entry.file
    if (-not (Test-Path -LiteralPath $target)) {
        $uri = [Uri]$entry.resolved_url
        if ($uri.Scheme -ne 'https' -or $uri.Host -notin @('msedge.sf.dl.delivery.mp.microsoft.com', 'download.visualstudio.microsoft.com')) { throw 'Unexpected dependency source' }
        $temporary = $target + '.partial'
        try {
            Invoke-WebRequest -Uri $uri.AbsoluteUri -OutFile $temporary -TimeoutSec 300 -UseBasicParsing
            if ((Get-FileHash -LiteralPath $temporary -Algorithm SHA256).Hash -ne $entry.sha256) { throw 'Downloaded file does not match prerequisite lock' }
            $signature = Get-AuthenticodeSignature -LiteralPath $temporary
            if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -ne $entry.signer) { throw 'Microsoft signature verification failed' }
            Move-Item -LiteralPath $temporary -Destination $target
        } finally {
            if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force }
        }
    }
    if ((Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash -ne $entry.sha256) { throw "Hash mismatch: $($entry.file)" }
    $signature = Get-AuthenticodeSignature -LiteralPath $target
    if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -ne $entry.signer) { throw "Signature verification failed: $($entry.file)" }
    Write-Output "Verified: $($entry.file)"
}
