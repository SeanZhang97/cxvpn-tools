[CmdletBinding()]
param([string]$Compiler)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$OutputEncoding = [Console]::OutputEncoding
$root = Split-Path $PSScriptRoot -Parent
if (-not $Compiler) {
    $candidates = @(
        (Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 6\ISCC.exe'),
        (Join-Path ([Environment]::GetFolderPath('ProgramFilesX86')) 'Inno Setup 6\ISCC.exe'),
        (Join-Path $env:ProgramFiles 'Inno Setup 6\ISCC.exe')
    )
    $command = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    if ($command) { $candidates += $command.Source }
    $Compiler = $candidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
}
if (-not $Compiler -or -not (Test-Path -LiteralPath $Compiler)) { throw 'Inno Setup 6 ISCC.exe not found' }
$exe = Get-Item -LiteralPath (Join-Path $root 'dist\CXVPNTools\CXVPNTools.exe')
$version = $exe.VersionInfo.ProductVersion
$versionSource = Get-Content -LiteralPath (Join-Path $root 'core\version.py') -Raw -Encoding UTF8
$sourceVersion = [regex]::Match($versionSource, "(?m)^APP_VERSION = '([0-9.]+)'").Groups[1].Value
if ($version -notmatch '^\d+\.\d+\.\d+$' -or $version -ne $sourceVersion) {
    throw 'Application version metadata is missing or stale; run build.py first'
}
$lock = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'prereqs.lock.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$digests = @{}
foreach ($entry in $lock.dependencies) {
    if ($entry.file -notin @('MicrosoftEdgeWebView2RuntimeInstallerX64.exe', 'VC_redist.x64.exe')) {
        throw 'Unexpected prerequisite filename'
    }
    $path = Join-Path $PSScriptRoot ('prereqs\' + $entry.file)
    $file = Get-Item -LiteralPath $path
    $digest = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash
    if ($digest -ne $entry.sha256 -or $file.Length -ne $entry.size) { throw "Prerequisite differs from lock: $($entry.file)" }
    $signature = Get-AuthenticodeSignature -LiteralPath $path
    if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -ne $entry.signer) {
        throw "Prerequisite signature invalid: $($entry.file)"
    }
    $digests[$entry.kind] = $digest
    Write-Output "Verified Microsoft signature and SHA256: $($entry.file)"
}
if (-not $digests.VC -or -not $digests.WebView2) { throw 'Both offline prerequisite installers are required' }
$payload = Join-Path $root 'dist\CXVPNTools'
$sensitive = Get-ChildItem -LiteralPath $payload -Recurse -File | Where-Object {
    $_.Name -match '^(config\.json(\.bak)?|state\.sqlite3.*|run\.log|startup\.log|netlog\.jsonl)$' -or
    $_.FullName -match '\\(webview_data|browser_data[^\\]*|captcha_cache)\\'
}
if ($sensitive) { throw 'Build payload contains user data; distribution stopped without deleting files' }
$logDirectory = Join-Path $root 'build_tmp\installer-tools'
New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
$log = Join-Path $logDirectory 'compile.log'
& $Compiler "/DWebViewSHA256=$($digests.WebView2)" "/DVCSHA256=$($digests.VC)" (Join-Path $PSScriptRoot 'CXVPNTools.iss') *> $log
if ($LASTEXITCODE -ne 0) {
    Get-Content -LiteralPath $log -Tail 25 -Encoding UTF8
    throw "Installer compilation failed; see $log"
}
$setup = Join-Path $root "dist\CXVPNTools-$version-setup.exe"
$hash = (Get-FileHash -LiteralPath $setup -Algorithm SHA256).Hash.ToLowerInvariant()
[IO.File]::WriteAllText($setup + '.sha256', "$hash  $(Split-Path $setup -Leaf)" + [Environment]::NewLine, [Text.UTF8Encoding]::new($false))
Write-Output "Installer built: $setup"
Write-Output "SHA256: $hash"
Get-Content -LiteralPath $log -Tail 6 -Encoding UTF8
