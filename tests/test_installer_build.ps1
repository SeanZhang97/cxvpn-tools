# Offline validation-only tests. Does not execute the builder or any installer.
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    (Join-Path $root 'installer\build_installer.ps1'), [ref]$null, [ref]$errors)
if ($errors) { throw ($errors | Out-String) }
$urlFunction = $ast.Find({ param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Get-VerifiedURL'
}, $true)
$loop = $ast.Find({ param($node)
    $node -is [System.Management.Automation.Language.ForEachStatementAst] -and $node.Condition.Extent.Text -eq '$lock.dependencies'
}, $true)
if (-not $urlFunction -or -not $loop) { throw 'Prerequisite validation implementation not found' }
. ([scriptblock]::Create($urlFunction.Extent.Text))
$validate = [scriptblock]::Create($loop.Extent.Text)
function New-Lock {
    return @{ dependencies = @(
        @{ file='VC_redist.x64.exe'; kind='VC'; sha256=('a' * 64); size=1234;
           minimum_version='14.51.36247.0';
           asset_url='https://github.com/SeanZhang97/cxvpn-tools/releases/download/prerequisites-2026-09-x64/VC_redist.x64.exe';
           resolved_url='https://download.visualstudio.microsoft.com/example/VC_redist.x64.exe' },
        @{ file='MicrosoftEdgeWebView2RuntimeInstallerX64.exe'; kind='WebView2'; sha256=('b' * 64); size=5678;
           asset_url='https://github.com/SeanZhang97/cxvpn-tools/releases/download/prerequisites-2026-09-x64/MicrosoftEdgeWebView2RuntimeInstallerX64.exe';
           resolved_url='https://msedge.sf.dl.delivery.mp.microsoft.com/example/MicrosoftEdgeWebView2RuntimeInstallerX64.exe' }
    ) }
}
function Invoke-Validation($lock) {
    $Offline = $false
    $digests = @{}
    $sizes = @{}
    $assetURLs = @{}
    $fallbackURLs = @{}
    $minimumVCVersion = ''
    . $validate
    if ($digests.Count -ne 2 -or -not $minimumVCVersion) { throw 'Missing prerequisite entries' }
}
Invoke-Validation (New-Lock)
Invoke-Validation (Get-Content -LiteralPath (Join-Path $root 'installer\prereqs.lock.json') -Raw -Encoding UTF8 | ConvertFrom-Json)
$cases = @(
    @{field='sha256'; value=('a' * 64 + "`n/DInjected=1")},
    @{field='size'; value='1234'},
    @{field='size'; value=-1},
    @{field='size'; value=$true},
    @{field='size'; value=123.5},
    @{field='minimum_version'; value='14.0.0.0" /DInjected=1'},
    @{field='minimum_version'; value='14.0.999999.0'},
    @{field='minimum_version'; value=123},
    @{field='asset_url'; value='http://github.com/SeanZhang97/cxvpn-tools/releases/download/prerequisites-1/VC_redist.x64.exe'},
    @{field='asset_url'; value='https://github.com/other/repo/releases/download/prerequisites-1/VC_redist.x64.exe'},
    @{field='asset_url'; value='https://github.com/SeanZhang97/cxvpn-tools/releases/latest/download/VC_redist.x64.exe'},
    @{field='asset_url'; value='https://github.com/SeanZhang97/cxvpn-tools/releases/download/prerequisites-1/other.exe'},
    @{field='resolved_url'; value='https://example.invalid/VC_redist.x64.exe'},
    @{field='kind'; value='WebView2'}
)
foreach ($case in $cases) {
    $lock = New-Lock
    $lock.dependencies[0][$case.field] = $case.value
    $rejected = $false
    try { Invoke-Validation $lock } catch { $rejected = $true }
    if (-not $rejected) { throw "Invalid $($case.field) accepted" }
}
Write-Output "$($cases.Count + 2) offline build-lock validation cases passed"
