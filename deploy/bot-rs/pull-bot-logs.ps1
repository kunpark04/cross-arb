# pull-bot-logs.ps1 — mirror the droplet LIVE-bot logs to Kalshi/data/cross-arb-bot/ (parity with the laptop run).
# The bot writes the SAME two files as `./run-live.sh > bot-live.out`: bot-live.out (stdout) + executions.jsonl.
#
#   pwsh deploy/bot-rs/pull-bot-logs.ps1                 # default host alias cross-arb-droplet
#   pwsh deploy/bot-rs/pull-bot-logs.ps1 root@1.2.3.4
#
# Schedule it like the monitor pull (register-tasks.ps1) for a periodic mirror. decisions 0015 / 0028.
param([string]$DropletHost = "cross-arb-droplet")
$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)   # ...\Kalshi\cross-arb
$KalshiRoot = Split-Path -Parent $RepoRoot                          # ...\Kalshi
$Dest = Join-Path $KalshiRoot "data\cross-arb-bot"
New-Item -ItemType Directory -Force -Path $Dest | Out-Null

foreach ($f in @("bot-live.out", "executions.jsonl")) {
    $remote = "${DropletHost}:/opt/cross-arb-bot/$f"
    $local  = Join-Path $Dest $f
    Write-Output "pulling $f ..."
    scp $remote $local
    if (Test-Path $local) {
        $sz = [math]::Round((Get-Item $local).Length / 1KB, 1)
        Write-Output "  -> $local  (${sz} KB)"
    }
}
Write-Output "done. mirror: $Dest"
