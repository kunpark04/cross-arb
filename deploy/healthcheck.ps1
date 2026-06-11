#requires -Version 7
<#
.SYNOPSIS
  Liveness check for the cross-arb droplet monitor — alerts if collection has stopped.

.DESCRIPTION
  One SSH probe reads (a) `systemctl is-active cross-arb-monitor` and (b) the monitor's `health.json`
  liveness beacon (overwritten every heartbeat). It ALERTS when the droplet is unreachable, the service
  is not active, the beacon is missing, or the beacon is stale (older than CA_MAX_AGE_SEC) — the latter
  catches a hung-but-"active" process that systemd wouldn't restart. An alert = a desktop balloon
  (best-effort) + a durable ALERT.txt in the data dir + a non-zero exit (so the Task-Scheduler run shows
  failed). A healthy run clears any prior ALERT.txt. Intended to run every ~30 min via Task Scheduler.
  Also polls the public Kalshi /series/fee_changes tripwire each run and raises the same alert path when
  it is non-empty (a scheduled fee change can silently invalidate the edge math — fee-pin 2026-06-10).

  Config via env: CA_HOST (default 'cross-arb-droplet'), CA_REMOTE_DIR, CA_LOCAL_DIR, CA_MAX_AGE_SEC
  (default 1200 = 20 min; the beacon refreshes every ~300 s, so this tolerates a few missed cycles).
#>
$ErrorActionPreference = 'Stop'

$RemoteHost = if ($env:CA_HOST)        { $env:CA_HOST }        else { 'cross-arb-droplet' }
$RemoteDir  = if ($env:CA_REMOTE_DIR)  { $env:CA_REMOTE_DIR }  else { '/opt/cross-arb/scripts/_data' }
$LocalDir   = if ($env:CA_LOCAL_DIR)   { $env:CA_LOCAL_DIR }   else { Join-Path (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path 'data\cross-arb' }
$MaxAgeSec  = if ($env:CA_MAX_AGE_SEC) { [int]$env:CA_MAX_AGE_SEC } else { 1200 }
$AlertFile  = Join-Path $LocalDir 'ALERT.txt'
$SshOpt = @('-o','BatchMode=yes','-o','ConnectTimeout=20','-o','ControlMaster=no','-o','ControlPath=none')
New-Item -ItemType Directory -Force -Path $LocalDir | Out-Null

function Show-Alert([string]$msg, [string]$title = 'cross-arb collector DOWN') {
    $stamp = [DateTime]::Now.ToString('yyyy-MM-dd HH:mm:ss')
    Write-Warning "$title — $msg"
    Set-Content -LiteralPath $AlertFile -Value "[$stamp] $title`n$msg`nhost: $RemoteHost"
    try {                                                   # best-effort desktop balloon (no module needed)
        Add-Type -AssemblyName System.Windows.Forms, System.Drawing
        $ni = [System.Windows.Forms.NotifyIcon]@{ Icon = [System.Drawing.SystemIcons]::Warning; Visible = $true }
        $ni.ShowBalloonTip(15000, $title, $msg, [System.Windows.Forms.ToolTipIcon]::Warning)
        Start-Sleep -Seconds 2; $ni.Dispose()
    } catch {
        try { if (Get-Module -ListAvailable BurntToast) { Import-Module BurntToast; New-BurntToastNotification -Text $title, $msg } }
        catch { Write-Warning "(no desktop toast available — see ALERT.txt + Task Scheduler history)" }
    }
    exit 1
}

# one SSH probe: service state (line 1) + health.json beacon (line 2, may be absent)
$probe   = ssh @SshOpt $RemoteHost "systemctl is-active cross-arb-monitor; cat '$RemoteDir/health.json' 2>/dev/null"
$sshExit = $LASTEXITCODE
if ($sshExit -eq 255 -or -not $probe) { Show-Alert "droplet unreachable over SSH (exit $sshExit) — host down, network, or key problem" }

$out    = @($probe -split "`n" | ForEach-Object { $_.Trim() } | Where-Object { $_ })
$active = $out[0]
if ($active -ne 'active') { Show-Alert "systemd service is '$active' (not active) — the monitor is not running" }
if ($out.Count -lt 2)     { Show-Alert "service active but NO health.json beacon — monitor may be starting or wedged before first heartbeat" }

try { $beacon = $out[1] | ConvertFrom-Json } catch { Show-Alert "health.json is unparseable: $($out[1])" }
$age = [int]([DateTimeOffset]::UtcNow.ToUnixTimeSeconds() - [int64]$beacon.t)
if ($age -gt $MaxAgeSec) { Show-Alert "beacon is STALE: last heartbeat ${age}s ago (> ${MaxAgeSec}s) — process is hung (still 'active' but not collecting)" }

# fee tripwire (wave-2): a NON-EMPTY Kalshi /series/fee_changes = a SCHEDULED per-series fee change that
# can invalidate the pinned fee coefficients (research/fee-pin-2026-06-10.md) — alert through the same
# path so it can't pass silently. Envelope key VERIFIED live 2026-06-11: {"series_fee_change_arr":[...]}.
# A transient poll failure only warns (the droplet-side monitor tripwire + the next 30-min run cover it).
try {
    $fees = Invoke-RestMethod -Uri 'https://api.elections.kalshi.com/trade-api/v2/series/fee_changes' -TimeoutSec 20
    $fc = @($fees.series_fee_change_arr | Where-Object { $null -ne $_ })
    if ($fc.Count -gt 0) {
        Show-Alert "Kalshi has $($fc.Count) SCHEDULED fee change(s) — edge math may be invalidated: $(($fc | ConvertTo-Json -Compress -Depth 5))" 'cross-arb FEE CHANGE scheduled'
    }
    Write-Host "OK  fee tripwire: /series/fee_changes empty"
} catch {
    Write-Warning "fee_changes poll failed ($($_.Exception.Message)) — not alerting (transient; monitor-side tripwire still covers)"
}

Remove-Item -LiteralPath $AlertFile -ErrorAction SilentlyContinue       # healthy -> clear any prior alert
Write-Host ("OK  cross-arb healthy: service active, beacon {0}s old, tracking {1} pmus / {2} kalshi ({3} weather + {4} sports)" -f `
            $age, $beacon.pmus, $beacon.kalshi, $beacon.weather, $beacon.sports)
exit 0
