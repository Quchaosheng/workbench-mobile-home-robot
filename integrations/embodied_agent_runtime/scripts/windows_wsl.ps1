[CmdletBinding()]
param(
    [ValidateSet('Check', 'BuildTest')]
    [string]$Mode = 'Check',

    [ValidateNotNullOrEmpty()]
    [string]$Distribution = 'Ubuntu-24.04',

    [switch]$DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repository = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$isolationRoot = 'WSL:$HOME/.cache/embodied-agent-runtime-wsl'
$linuxHelper = 'scripts/windows_wsl_inner.sh'

if ($DryRun) {
    Write-Output "mode=$Mode"
    Write-Output "distribution=$Distribution"
    Write-Output 'required_ros=jazzy'
    Write-Output "repository=$repository"
    Write-Output 'check_environment=true'
    Write-Output "linux_helper=$linuxHelper"
    if ($Mode -eq 'BuildTest') {
        Write-Output "isolation_root=$isolationRoot"
        Write-Output 'colcon_build_test=true'
    }
    Write-Output 'invoke_wsl=false'
    exit 0
}

$wsl = Get-Command 'wsl.exe' -ErrorAction SilentlyContinue
if ($null -eq $wsl) {
    [Console]::Error.WriteLine(
        'wsl.exe is unavailable. Enable WSL2, then install Ubuntu-24.04.'
    )
    exit 2
}

$rawDistributions = @(& $wsl.Source --list --quiet 2>$null)
$listExitCode = $LASTEXITCODE
$installedDistributions = @(
    $rawDistributions |
        ForEach-Object { ($_ -replace "`0", '').Trim() } |
        Where-Object { $_ }
)

if ($listExitCode -ne 0 -or
    -not ($installedDistributions | Where-Object { $_ -ieq $Distribution })) {
    [Console]::Error.WriteLine(
        "WSL distribution '$Distribution' is unavailable. " +
        "Install it explicitly with: wsl.exe --install --distribution $Distribution"
    )
    exit 2
}

Write-Output "mode=$Mode"
Write-Output "distribution=$Distribution"
& $wsl.Source -d $Distribution --cd $repository -- bash $linuxHelper $Mode
exit $LASTEXITCODE
