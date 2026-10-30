# Undo stage 2 (2_setup_identities.ps1) - run ELEVATED. Refuses once any group is migrated (layout.json): after
# cutover the company homes and the shared databases hold live data - roll groups back with migrate.py first.
#   powershell -NoProfile -ExecutionPolicy Bypass -File 9_rollback_identities.ps1 [-WhatIf]
# Removes the 6 company accounts, their credentials, and fed, shared, homes, workspaces, runs\<identity>.
# The pilot (hermes-agent, runtime, home-pilot, workspaces-pilot) and the live federation are not touched.
#Requires -RunAsAdministrator
[CmdletBinding(SupportsShouldProcess = $true)]
param([string]$Root = "C:\HermesFederation")
$ErrorActionPreference = "Stop"
$ids = "hermes-isc", "hermes-auvia", "hermes-tabbeqai", "hermes-spectra", "hermes-spectrasol", "hermes-fed"
$layout = Join-Path $Root "homes\layout.json"
if (Test-Path $layout) {
    $migrated = (Get-Content $layout -Raw | ConvertFrom-Json).groups.PSObject.Properties |
        Where-Object { $_.Value.migrated } | ForEach-Object Name
    if ($migrated) { throw "groups still migrated: $($migrated -join ', ') - run migrate.py --rollback for each first" }
}
foreach ($d in "fed", "shared", "homes", "workspaces") {
    $p = Join-Path $Root $d
    if ((Test-Path $p) -and $PSCmdlet.ShouldProcess($p, "remove")) { Remove-Item $p -Recurse -Force }
}
foreach ($u in $ids) {
    $p = Join-Path $Root "runs\$u"
    if ((Test-Path $p) -and $PSCmdlet.ShouldProcess($p, "remove")) { Remove-Item $p -Recurse -Force }
    $c = Join-Path $env:LOCALAPPDATA "hermes-federation-launcher\$u.xml"
    if ((Test-Path $c) -and $PSCmdlet.ShouldProcess($c, "remove")) { Remove-Item $c -Force }
    if ((Get-LocalUser -Name $u -ErrorAction SilentlyContinue) -and $PSCmdlet.ShouldProcess($u, "remove local user")) {
        Remove-LocalUser -Name $u
        $prof = Get-CimInstance Win32_UserProfile | Where-Object { $_.LocalPath -like "*\$u" }
        if ($prof -and $PSCmdlet.ShouldProcess($prof.LocalPath, "remove user profile")) { $prof | Remove-CimInstance }
    }
}
"stage 2 rolled back"
