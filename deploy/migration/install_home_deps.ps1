# Installs the pinned Hermes dependencies into ONE company home and makes them read-only for its identity.
# Called ELEVATED by 2_setup_identities.ps1 (one call per home, ~4 minutes, ~1.2 GB each).
# Same rule as the pilot (deploy/windows-agent-user/1d_pilot_dependencies.ps1): Hermes keeps dependency state per
# HERMES_HOME and refuses a venv outside it; the identity may write only what Hermes writes at run time (the
# generation .leases folders and the lock files). The owner keeps modify: the Console runs board commands as the owner.
#Requires -RunAsAdministrator
[CmdletBinding(SupportsShouldProcess = $true)]
param([Parameter(Mandatory)][string]$HomeDir, [Parameter(Mandatory)][string]$AgentUser,
      [string]$OwnerUser = "aliab", [string]$Root = "C:\HermesFederation", [switch]$SkipInstall)
$ErrorActionPreference = "Stop"
$Agent = "$env:COMPUTERNAME\$AgentUser"
$Owner = "$env:COMPUTERNAME\$OwnerUser"
$runtime = Join-Path $Root "runtime"
$py = (Get-ChildItem "$runtime\tools" -Recurse -Filter python.exe | Where-Object { $_.FullName -notmatch "\\venv\\" } |
    Select-Object -First 1).FullName
if (-not $py) { throw "no runtime python under $runtime\tools" }

if (-not $SkipInstall -and $PSCmdlet.ShouldProcess($HomeDir, "install the pinned Hermes dependencies (pm install)")) {
    $env:HERMES_HOME = $HomeDir
    $env:UV_CACHE_DIR = "$runtime\cache\uv"
    Push-Location "$runtime\hermes-agent"
    try {
        # pm writes progress to stderr; under "Stop" Windows PowerShell 5.1 would turn that into an error
        $ErrorActionPreference = "Continue"
        & $py -m pm.cli install --without agent-browser --without cua-driver 2>&1 | ForEach-Object { "$_" }
        if ($LASTEXITCODE) { throw "pm install failed for $HomeDir ($LASTEXITCODE)" }
    } finally { $ErrorActionPreference = "Stop"; Pop-Location; Remove-Item Env:HERMES_HOME, Env:UV_CACHE_DIR }
}

$locked = "installs", "tools", "cache" | ForEach-Object { Join-Path $HomeDir $_ } | Where-Object { Test-Path $_ }
if ($PSCmdlet.ShouldProcess(($locked -join ", "), "$AgentUser read+execute only")) {
    foreach ($d in $locked) {
        icacls $d /setowner "*S-1-5-32-544" /T /C /Q | Out-Null
        icacls $d /inheritance:r /grant:r "*S-1-5-18:(OI)(CI)F" "*S-1-5-32-544:(OI)(CI)F" "${Owner}:(OI)(CI)M" `
            "${Agent}:(OI)(CI)RX" /Q | Out-Null
        if ($LASTEXITCODE) { throw "icacls failed: $d" }
        Get-ChildItem $d -Force | ForEach-Object { icacls $_.FullName /reset /T /C /Q | Out-Null }
    }
    Get-ChildItem $locked -Recurse -Force -Filter ".lease-managed" -ErrorAction SilentlyContinue |
        ForEach-Object { New-Item -ItemType Directory -Force -Path (Join-Path $_.DirectoryName ".leases") | Out-Null }
    Get-ChildItem $locked -Recurse -Force -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -in ".install.lock", ".prepare.lock", ".leases" -or ($_.Name -eq ".lock" -and -not $_.PSIsContainer -and
                       $_.FullName -notlike "$HomeDir\cache\*") } |
        ForEach-Object { icacls $_.FullName /grant "${Agent}:$(if ($_.PSIsContainer) { '(OI)(CI)M' } else { 'M' })" /Q | Out-Null }
}
$venv = Get-ChildItem "$HomeDir\installs" -Recurse -Filter pyvenv.cfg -ErrorAction SilentlyContinue | Select-Object -First 1
"$AgentUser dependency environment: $(if ($venv) { $venv.DirectoryName } else { 'MISSING' })"
if (-not $venv) { throw "no dependency environment in $HomeDir" }
