# Run with Windows PowerShell 5.1; uses harmless fixture executables, no real installers.
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$base = Join-Path $root 'build_tmp\installer-tests'
New-Item -ItemType Directory -Path $base -Force | Out-Null
$testDir = Join-Path $base ([Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $testDir | Out-Null
$passed = 0
try {
    Copy-Item -LiteralPath (Join-Path $root 'installer\run-prerequisite.ps1') -Destination $testDir
    $source = @"
using System;
using System.IO;
using System.Threading;
public class PrerequisiteFixture {
    public static int Main() {
        string name = Path.GetFileNameWithoutExtension(System.Reflection.Assembly.GetExecutingAssembly().Location);
        if (name == "timeout") { Thread.Sleep(10000); return 0; }
        if (name == "reboot") return 3010;
        if (name == "failure") return 42;
        return 0;
    }
}
"@
    $fixture = Join-Path $testDir 'success.exe'
    Add-Type -TypeDefinition $source -OutputAssembly $fixture -OutputType ConsoleApplication
    foreach ($name in @('reboot', 'failure', 'timeout')) {
        Copy-Item -LiteralPath $fixture -Destination (Join-Path $testDir ($name + '.exe'))
    }
    $cases = @(
        @{Name='success'; Code=0; BadHash=$false},
        @{Name='reboot'; Code=3010; BadHash=$false},
        @{Name='failure'; Code=42; BadHash=$false},
        @{Name='timeout'; Code=1460; BadHash=$false},
        @{Name='success'; Code=9001; BadHash=$true}
    )
    foreach ($case in $cases) {
        $binary = Join-Path $testDir ($case.Name + '.exe')
        $hash = (Get-FileHash -LiteralPath $binary -Algorithm SHA256).Hash
        if ($case.BadHash) { $hash = '0' * 64 }
        $runner = Join-Path $testDir 'run-prerequisite.ps1'
        $arguments = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' + $runner + '" -Installer "' + $binary +
            '" -Kind VC -ExpectedSHA256 ' + $hash + ' -TimeoutSeconds 1'
        $p = Start-Process -FilePath (Join-Path $PSHOME 'powershell.exe') -ArgumentList $arguments -WindowStyle Hidden -PassThru
        try {
            if (-not $p.WaitForExit(15000)) { $p.Kill(); throw 'Test worker exceeded 15 seconds' }
            if ($p.ExitCode -ne $case.Code) { throw "Expected $($case.Code), got $($p.ExitCode): $($case.Name)" }
        } finally { $p.Dispose() }
        $passed++
    }
    $log = Get-Content -LiteralPath (Join-Path $testDir 'prerequisite-VC.log') -Raw -Encoding UTF8
    foreach ($stage in @('worker received', 'starting verified installer', 'completed', 'timeout', 'failed')) {
        if (-not $log.Contains($stage)) { throw "Missing lifecycle log: $stage" }
    }
    Write-Output "$passed offline prerequisite cases passed; lifecycle logs verified"
} finally {
    $resolvedBase = [IO.Path]::GetFullPath($base).TrimEnd('\') + '\'
    $resolvedTest = [IO.Path]::GetFullPath($testDir)
    if (-not $resolvedTest.StartsWith($resolvedBase, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Refusing cleanup outside installer test directory'
    }
    Remove-Item -LiteralPath $resolvedTest -Recurse -Force
}
