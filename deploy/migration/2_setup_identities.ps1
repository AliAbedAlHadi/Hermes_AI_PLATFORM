# Stage 2 of the per-company identity migration (MIGRATION_PLAN.md) - run ELEVATED once by the owner (aliab):
#   powershell -NoProfile -ExecutionPolicy Bypass -File 2_setup_identities.ps1 [-WhatIf]
# ~25-30 minutes (one Hermes dependency install per home), ~7 GB. Re-runnable: existing accounts, credentials and
# installs are kept (-SkipInstall skips the dependency installs).
#
# Creates, next to the pilot and WITHOUT touching the live federation (%LOCALAPPDATA%\hermes, the 118 profiles, the
# databases, the Console):
#   * 6 standard users hermes-isc, -auvia, -tabbeqai, -spectra, -spectrasol, -fed (names: 20 characters max). Their passwords are random,
#     never shown, and stored only DPAPI-encrypted for aliab (%LOCALAPPDATA%\hermes-federation-launcher\<user>.xml)
#   * C:\HermesFederation\fed        copy of the federation code (guard, memory tool, workflow, roster): everyone RX
#   * C:\HermesFederation\shared     the shared databases go here in stage 3: owner + every identity modify (R1)
#   * C:\HermesFederation\homes\<g>  one HERMES_HOME per group: owner + that identity only
#   * C:\HermesFederation\workspaces\<g>  owner + that identity (hermes-fed reads all, for fed-hermes-admin/fed-audit)
#   * C:\HermesFederation\runs\<identity>  launcher files: owner + that identity only
# Rollback: 9_rollback_identities.ps1.
#Requires -RunAsAdministrator
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$Root = "C:\HermesFederation",
    [string]$OwnerUser = "aliab",
    [string]$FedSource = "$env:USERPROFILE\OneDrive\Documents\Hermes",
    [switch]$SkipInstall
)
$ErrorActionPreference = "Stop"
if ($env:USERNAME -ne $OwnerUser) { throw "run this as $OwnerUser (Run as administrator), not as $env:USERNAME: the credentials are encrypted for the account that runs it" }
$Machine = $env:COMPUTERNAME
$Owner = "$Machine\$OwnerUser"
$Groups = [ordered]@{ isc = "hermes-isc"; auvia = "hermes-auvia"; tabbeqai = "hermes-tabbeqai"; spectra = "hermes-spectra"
                      spectrasolutions = "hermes-spectrasol"; federation = "hermes-fed" }
$CredDir = Join-Path $env:LOCALAPPDATA "hermes-federation-launcher"
$SYS, $ADM = "*S-1-5-18", "*S-1-5-32-544"
function Step($m) { Write-Host "== $m" -ForegroundColor Cyan }
function Do-It($what, [scriptblock]$action) { if ($PSCmdlet.ShouldProcess($what)) { & $action } }
function Acl($path, [string[]]$grants) {
    Do-It "ACL $path : $($grants -join ', ')" {
        New-Item -ItemType Directory -Force -Path $path | Out-Null
        icacls $path /setowner $ADM /C /Q | Out-Null
        $all = @("${SYS}:(OI)(CI)F", "${ADM}:(OI)(CI)F") + $grants
        icacls $path /inheritance:r /grant:r $all /Q | Out-Null
        if ($LASTEXITCODE) { throw "icacls failed: $path" }
    }
}
function New-Password {
    $b = New-Object byte[] 24
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b)
    ConvertTo-SecureString ("Hf9!" + [Convert]::ToBase64String($b)) -AsPlainText -Force
}

Step "1. accounts (standard users) and their credentials (DPAPI, $OwnerUser only)"
Do-It "credential folder $CredDir, readable by $OwnerUser only" {
    New-Item -ItemType Directory -Force -Path $CredDir | Out-Null
    icacls $CredDir /inheritance:r /grant:r "${Owner}:(OI)(CI)F" "${SYS}:(OI)(CI)F" /Q | Out-Null
    if ($LASTEXITCODE) { throw "icacls failed: $CredDir" }
}
foreach ($g in $Groups.Keys) {
    $u = $Groups[$g]
    $cred = Join-Path $CredDir "$u.xml"
    $exists = Get-LocalUser -Name $u -ErrorAction SilentlyContinue
    if (-not $exists -or -not (Test-Path $cred)) {
        Do-It "$(if ($exists) { 'reset password of' } else { 'create' }) $u and store its credential" {
            $pw = New-Password
            if ($exists) { Set-LocalUser -Name $u -Password $pw }
            else {
                New-LocalUser -Name $u -Password $pw -PasswordNeverExpires -UserMayNotChangePassword `
                    -Description "Hermes Federation agents of $g" | Out-Null
                Add-LocalGroupMember -Group "Users" -Member $u
            }
            New-Object Management.Automation.PSCredential ("$Machine\$u", $pw) | Export-Clixml -LiteralPath $cred
        }
    }
    foreach ($grp in "Administrators", "docker-users", "Remote Desktop Users") {
        if (Get-LocalGroupMember -Group $grp -ErrorAction SilentlyContinue | Where-Object { $_.Name -eq "$Machine\$u" }) {
            Do-It "remove $u from $grp" { Remove-LocalGroupMember -Group $grp -Member $u }
        }
    }
}

Step "2. folders and permissions"
$ids = @($Groups.Values)
# the runtime (Hermes code, the Python the hooks use, agent_exec.py): every identity reads and executes
Do-It "runtime: identities read+execute" {
    foreach ($u in $ids) { icacls "$Root\runtime" /grant "$Machine\${u}:(OI)(CI)RX" /Q | Out-Null }
}
Acl "$Root\fed" (@("${Owner}:(OI)(CI)RX") + ($ids | ForEach-Object { "$Machine\${_}:(OI)(CI)RX" }))
Acl "$Root\shared" (@("${Owner}:(OI)(CI)M") + ($ids | ForEach-Object { "$Machine\${_}:(OI)(CI)M" }))
Acl "$Root\homes" @("${Owner}:(OI)(CI)M")
Acl "$Root\workspaces" @("${Owner}:(OI)(CI)M")
Acl "$Root\runs" @("${Owner}:(OI)(CI)M", "$Machine\hermes-agent:(OI)(CI)M")   # pilot runs stay where they are
foreach ($g in $Groups.Keys) {
    $u = "$Machine\$($Groups[$g])"
    Do-It "home, workspace and runs of $g" {
        foreach ($d in "$Root\homes\$g", "$Root\workspaces\$g", "$Root\runs\$($Groups[$g])") {
            New-Item -ItemType Directory -Force -Path $d | Out-Null
            icacls $d /grant "${u}:(OI)(CI)M" /Q | Out-Null
            if ($LASTEXITCODE) { throw "icacls failed: $d" }
        }
        if ($g -ne "federation") { icacls "$Root\workspaces\$g" /grant "$Machine\hermes-fed:(OI)(CI)RX" /Q | Out-Null }
        # runs\ also grants the pilot account (hermes-agent): a company's run files (prompts, replies) must not inherit it
        icacls "$Root\runs\$($Groups[$g])" /inheritance:r /grant:r "${SYS}:(OI)(CI)F" "${ADM}:(OI)(CI)F" "${Owner}:(OI)(CI)M" `
            "${u}:(OI)(CI)M" /Q | Out-Null
        if ($LASTEXITCODE) { throw "icacls failed: runs\$($Groups[$g])" }
    }
}

Step "3. federation code -> $Root\fed (admin-owned; identities read+execute)"
Do-It "copy security, memory, workflow, roster from $FedSource" {
    foreach ($d in "security", "memory", "workflow", "roster") {
        robocopy (Join-Path $FedSource $d) "$Root\fed\$d" /MIR /XD __pycache__ tests /XF *.log *.bak* /NFL /NDL /NJH /NJS /NP | Out-Null
        if ($LASTEXITCODE -ge 8) { throw "robocopy failed for $d ($LASTEXITCODE)" }
    }
    icacls "$Root\fed" /setowner $ADM /T /C /Q | Out-Null
    Get-ChildItem "$Root\fed" -Force | ForEach-Object { icacls $_.FullName /reset /T /C /Q | Out-Null }
}

Step "4. Hermes dependencies, one install per home (~4 minutes each)"
foreach ($g in $Groups.Keys) {
    Do-It "dependencies of homes\$g" {
        & (Join-Path $PSScriptRoot "install_home_deps.ps1") -HomeDir "$Root\homes\$g" -AgentUser $Groups[$g] `
            -OwnerUser $OwnerUser -Root $Root -SkipInstall:$SkipInstall
    }
}

Step "done"
Write-Host "Next, as $OwnerUser in a normal (not elevated) PowerShell:"
Write-Host "  python -I $PSScriptRoot\canary.py"
