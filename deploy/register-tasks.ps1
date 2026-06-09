#requires -Version 7
<#
.SYNOPSIS
  Register (or refresh) the two Windows Task-Scheduler jobs for cross-arb. Run ONCE.

.DESCRIPTION
  Uses the STABLE WindowsApps app-alias path for pwsh — a bare/versioned pwsh path fails under Task
  Scheduler with 0x80070002 FILE_NOT_FOUND (the gotcha weather-alpha hit). -StartWhenAvailable runs a
  missed schedule when the machine next wakes.
    PullCrossArbData     daily 8:30am   -> pull-data.ps1   (mirror + verified move of finalized days)
    CrossArbHealthcheck  every 30 min   -> healthcheck.ps1 (alerts if the collector has stopped)
#>
$ErrorActionPreference = 'Stop'

$repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$pull = (Resolve-Path (Join-Path $PSScriptRoot 'pull-data.ps1')).Path
$hc   = (Resolve-Path (Join-Path $PSScriptRoot 'healthcheck.ps1')).Path
$pwsh = Join-Path $env:LOCALAPPDATA 'Microsoft\WindowsApps\pwsh.exe'      # stable across PS updates (Store)
if (-not (Test-Path $pwsh)) { $pwsh = (Get-Command pwsh).Source }

function Register-CA([string]$name, [string]$script, $trigger) {
    $act = New-ScheduledTaskAction -Execute $pwsh -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$script`"" -WorkingDirectory $repo
    $set = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun -AllowStartIfOnBatteries `
                                        -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 1)
    Register-ScheduledTask -TaskName $name -Action $act -Trigger $trigger -Settings $set -Force | Out-Null
    Write-Host "registered '$name'"
}

Register-CA 'PullCrossArbData' $pull (New-ScheduledTaskTrigger -Daily -At 8:30am)

$hcTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) `
             -RepetitionInterval (New-TimeSpan -Minutes 30) -RepetitionDuration (New-TimeSpan -Days 3650)
Register-CA 'CrossArbHealthcheck' $hc $hcTrigger

Write-Host ""
Write-Host "run now:   Start-ScheduledTask PullCrossArbData ; Start-ScheduledTask CrossArbHealthcheck"
Write-Host "inspect:   Get-ScheduledTaskInfo -TaskName CrossArbHealthcheck"
Write-Host "remove:    Unregister-ScheduledTask -TaskName PullCrossArbData,CrossArbHealthcheck -Confirm:`$false"
Get-ScheduledTask -TaskName 'PullCrossArbData', 'CrossArbHealthcheck' | Select-Object TaskName, State
