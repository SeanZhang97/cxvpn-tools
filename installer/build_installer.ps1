[CmdletBinding()]
param([string]$Compiler, [switch]$Offline)
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
if ($lock.architecture -cne 'x64') { throw 'Only x64 prerequisites are supported' }
$digests = @{}
$sizes = @{}
$assetURLs = @{}
$fallbackURLs = @{}
$minimumVCVersion = ''
function Get-VerifiedURL([object]$Value, [string]$FileName, [switch]$GitHub) {
    if ($Value -isnot [string] -or $Value -notmatch '\Ahttps://[A-Za-z0-9/._-]+\z') { throw 'Invalid prerequisite URL characters' }
    $uri = [Uri]$Value
    if (-not $uri.IsAbsoluteUri -or $uri.Scheme -cne 'https' -or $uri.UserInfo -or $uri.Query -or $uri.Fragment) { throw 'Invalid prerequisite URL' }
    if ($GitHub) {
        $expected = '\A/SeanZhang97/cxvpn-tools/releases/download/prerequisites-[A-Za-z0-9._-]+/' + [regex]::Escape($FileName) + '\z'
        if ($uri.Host -cne 'github.com' -or $uri.AbsolutePath -cnotmatch $expected) { throw 'Unexpected GitHub prerequisite asset' }
    } elseif ($uri.Host -notin @('msedge.sf.dl.delivery.mp.microsoft.com', 'download.visualstudio.microsoft.com')) {
        throw 'Unexpected Microsoft prerequisite source'
    }
    return $Value
}
foreach ($entry in $lock.dependencies) {
    if ($entry.file -notin @('MicrosoftEdgeWebView2RuntimeInstallerX64.exe', 'VC_redist.x64.exe')) {
        throw 'Unexpected prerequisite filename'
    }
    if ($entry.kind -notin @('VC', 'WebView2') -or $digests.ContainsKey($entry.kind)) { throw 'Invalid or duplicate prerequisite kind' }
    $expectedFile = if ($entry.kind -eq 'VC') { 'VC_redist.x64.exe' } else { 'MicrosoftEdgeWebView2RuntimeInstallerX64.exe' }
    if ($entry.file -cne $expectedFile) { throw 'Prerequisite kind and filename mismatch' }
    if ($entry.sha256 -isnot [string] -or $entry.sha256 -cnotmatch '\A[0-9a-fA-F]{64}\z' -or
        ($entry.size -isnot [int] -and $entry.size -isnot [long]) -or $entry.size -le 0 -or $entry.size -gt 2147483647) {
        throw 'Invalid prerequisite digest or size'
    }
    if ($entry.kind -eq 'VC') {
        if ($entry.minimum_version -isnot [string] -or $entry.minimum_version -cnotmatch '\A\d+\.\d+\.\d+\.\d+\z') { throw 'Invalid VC minimum_version' }
        $minimumVCVersion = $entry.minimum_version
        foreach ($part in $minimumVCVersion.Split('.')) {
            if ([long]$part -gt 65535) { throw 'VC minimum_version component exceeds Windows version range' }
        }
    }
    $assetURLs[$entry.kind] = Get-VerifiedURL $entry.asset_url $entry.file -GitHub
    $fallbackURLs[$entry.kind] = Get-VerifiedURL $entry.resolved_url $entry.file
    if ($Offline) {
        $path = Join-Path $PSScriptRoot ('prereqs\' + $entry.file)
        $file = Get-Item -LiteralPath $path
        $digest = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash
        if ($digest -ne $entry.sha256 -or $file.Length -ne $entry.size) { throw "Prerequisite differs from lock: $($entry.file)" }
        $signature = Get-AuthenticodeSignature -LiteralPath $path
        if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -ne $entry.signer) {
            throw "Prerequisite signature invalid: $($entry.file)"
        }
        Write-Output "Verified Microsoft signature and SHA256: $($entry.file)"
    }
    $digests[$entry.kind] = [string]$entry.sha256
    $sizes[$entry.kind] = [long]$entry.size
}
if (-not $digests.VC -or -not $digests.WebView2) { throw 'Both prerequisite lock entries are required' }
if (-not $minimumVCVersion) { throw 'VC minimum_version is required in prerequisite lock' }
$payload = Join-Path $root 'dist\CXVPNTools'
$sensitive = Get-ChildItem -LiteralPath $payload -Recurse -File | Where-Object {
    $_.Name -match '^(config\.json(\.bak)?|state\.sqlite3.*|run\.log|startup\.log|netlog\.jsonl)$' -or
    $_.FullName -match '\\(webview_data|browser_data[^\\]*|captcha_cache)\\'
}
if ($sensitive) { throw 'Build payload contains user data; distribution stopped without deleting files' }
$logDirectory = Join-Path $root 'build_tmp\installer-tools'
New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
$log = Join-Path $logDirectory 'compile.log'
$compilerArgs = @("/DWebViewSHA256=$($digests.WebView2)", "/DVCSHA256=$($digests.VC)",
    "/DWebViewSize=$($sizes.WebView2)", "/DVCSize=$($sizes.VC)", "/DVCMinVersion=$minimumVCVersion",
    "/DVCAssetURL=$($assetURLs.VC)", "/DWebViewAssetURL=$($assetURLs.WebView2)",
    "/DVCFallbackURL=$($fallbackURLs.VC)", "/DWebViewFallbackURL=$($fallbackURLs.WebView2)")
if ($Offline) { $compilerArgs += '/DOfflineBundle=1' }
$compilerArgs += (Join-Path $PSScriptRoot 'CXVPNTools.iss')
& $Compiler @compilerArgs *> $log
if ($LASTEXITCODE -ne 0) {
    Get-Content -LiteralPath $log -Tail 25 -Encoding UTF8
    throw "Installer compilation failed; see $log"
}
$suffix = if ($Offline) { '-setup-offline.exe' } else { '-setup.exe' }
$setup = Join-Path $root "dist\CXVPNTools-$version$suffix"
$hash = (Get-FileHash -LiteralPath $setup -Algorithm SHA256).Hash.ToLowerInvariant()
[IO.File]::WriteAllText($setup + '.sha256', "$hash  $(Split-Path $setup -Leaf)" + [Environment]::NewLine, [Text.UTF8Encoding]::new($false))
Write-Output "Installer built: $setup"
Write-Output "SHA256: $hash"
Get-Content -LiteralPath $log -Tail 6 -Encoding UTF8
