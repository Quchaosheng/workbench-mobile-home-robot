[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$tool = Join-Path $PSScriptRoot 'windows_wsl.ps1'
if (-not (Test-Path -LiteralPath $tool -PathType Leaf)) {
    throw "Missing tool under test: $tool"
}
$helper = Join-Path $PSScriptRoot 'windows_wsl_inner.sh'
if (-not (Test-Path -LiteralPath $helper -PathType Leaf)) {
    throw "Missing Linux helper under test: $helper"
}

$hostExecutable = if ($PSVersionTable.PSEdition -eq 'Desktop') {
    Join-Path $PSHOME 'powershell.exe'
} else {
    Join-Path $PSHOME 'pwsh.exe'
}

function Invoke-ToolProcess {
    param([string[]]$ToolArguments)

    $previousErrorAction = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $lines = @(
            & $hostExecutable -NoProfile -ExecutionPolicy Bypass -File $tool @ToolArguments 2>&1
        )
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousErrorAction
    }
    [pscustomobject]@{
        ExitCode = $exitCode
        Output = $lines -join [Environment]::NewLine
    }
}

function Assert-Contains {
    param(
        [string]$Actual,
        [string]$Expected
    )

    if (-not $Actual.Contains($Expected)) {
        throw "Expected output to contain '$Expected'. Actual output:`n$Actual"
    }
}

$check = Invoke-ToolProcess @('-Mode', 'Check', '-DryRun')
if ($check.ExitCode -ne 0) {
    throw "Check DryRun failed with exit code $($check.ExitCode):`n$($check.Output)"
}
Assert-Contains $check.Output 'mode=Check'
Assert-Contains $check.Output 'distribution=Ubuntu-24.04'
Assert-Contains $check.Output 'required_ros=jazzy'
Assert-Contains $check.Output 'check_environment=true'
Assert-Contains $check.Output 'linux_helper=scripts/windows_wsl_inner.sh'
Assert-Contains $check.Output 'invoke_wsl=false'

$buildTest = Invoke-ToolProcess @('-Mode', 'BuildTest', '-DryRun')
if ($buildTest.ExitCode -ne 0) {
    throw "BuildTest DryRun failed with exit code $($buildTest.ExitCode):`n$($buildTest.Output)"
}
Assert-Contains $buildTest.Output 'mode=BuildTest'
Assert-Contains $buildTest.Output 'embodied-agent-runtime-wsl'
Assert-Contains $buildTest.Output 'check_environment=true'
Assert-Contains $buildTest.Output 'colcon_build_test=true'
Assert-Contains $buildTest.Output 'invoke_wsl=false'

$unsupported = Invoke-ToolProcess @('-Mode', 'Unsupported', '-DryRun')
if ($unsupported.ExitCode -eq 0) {
    throw "Unsupported mode unexpectedly succeeded:`n$($unsupported.Output)"
}

$missingDistribution = Invoke-ToolProcess @(
    '-Mode', 'Check', '-Distribution', 'Definitely-Missing-Distribution'
)
if ($missingDistribution.ExitCode -ne 2) {
    throw (
        "Missing WSL prerequisite returned $($missingDistribution.ExitCode), expected 2:" +
        "`n$($missingDistribution.Output)"
    )
}

Write-Output 'PASS: windows_wsl.ps1 behavior'
