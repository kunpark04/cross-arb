#requires -Version 7
<#
.SYNOPSIS
  Pull cross-arb persistence data from the droplet to this machine — FOOLPROOF (copy-keep + checksum).

.DESCRIPTION
  The droplet's event-date-partitioned logs ((transitions|ladders|trades)-<YYYY-MM-DD>.jsonl +
  sessions.jsonl + cli.jsonl + fee_changes.jsonl) are the append-only source. This script MIRRORS them
  into Kalshi/data/cross-arb/, sha256-verifying every transfer before it is trusted (sidecar
  <name>.sha256), and is idempotent: a file already present with a matching hash is skipped. Finalized
  days (event-date < today UTC, hence immutable) are gzipped locally to <name>.jsonl.gz and then DELETED
  on the droplet — but ONLY after the local .gz is verified to decompress back to the remote sha256
  (verify-before-delete). Today's files + the undated live files (sessions/cli/fee_changes/health) are
  LIVE and never deleted. Set CA_KEEP_REMOTE=1 for pure copy-keep.

  Batched to ~5 SSH round-trips total (Windows OpenSSH has NO ControlMaster multiplexing):
    1 ssh  list remote *.jsonl with sha256
    1 ssh  tar the still-needed files into one archive   (only if something is needed)
    1 scp  pull that one archive
    1 ssh  remove our temp archive
    1 ssh  delete the finalized days now verified-safe locally   (move mode; skipped if CA_KEEP_REMOTE)

  Config via env: CA_HOST (default 'cross-arb-droplet' ssh-config alias), CA_REMOTE_DIR, CA_LOCAL_DIR.
  Needs passwordless key SSH. Read-only w.r.t. the droplet's data.
#>
$ErrorActionPreference = 'Stop'

$RemoteHost = if ($env:CA_HOST)       { $env:CA_HOST }       else { 'cross-arb-droplet' }
$RemoteDir  = if ($env:CA_REMOTE_DIR) { $env:CA_REMOTE_DIR } else { '/opt/cross-arb/scripts/_data' }
$LocalDir   = if ($env:CA_LOCAL_DIR)  { $env:CA_LOCAL_DIR }  else { Join-Path (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path 'data\cross-arb' }
$RemoteTar  = '.ca-pull.tar.gz'                              # temp archive INSIDE $RemoteDir (never data)
$TodayUtc   = [DateTime]::UtcNow.ToString('yyyy-MM-dd')
$KeepRemote = [bool]$env:CA_KEEP_REMOTE                      # CA_KEEP_REMOTE=1 -> never delete (copy-keep)

# Disable multiplexing (Windows OpenSSH can't ControlMaster; a stray block breaks every connection);
# BatchMode so a missing key fails fast instead of prompting; sane connect timeout.
$SshOpt = @('-o','BatchMode=yes','-o','ConnectTimeout=20','-o','ControlMaster=no','-o','ControlPath=none')
function Invoke-Ssh([string]$cmd) { ssh @SshOpt $RemoteHost $cmd }
function Sidecar([string]$name)   { Join-Path $LocalDir "$name.sha256" }
function Have([string]$name, [string]$hash) {
    $s = Sidecar $name
    (Test-Path -LiteralPath $s) -and ((Get-Content -LiteralPath $s -Raw).Trim() -eq $hash)
}
function Get-GzHash([string]$gzPath) {                       # sha256 of a .gz's DECOMPRESSED content
    $in = [IO.File]::OpenRead($gzPath)
    try {
        $gz  = [IO.Compression.GZipStream]::new($in, [IO.Compression.CompressionMode]::Decompress)
        $sha = [Security.Cryptography.SHA256]::Create()
        try { return [BitConverter]::ToString($sha.ComputeHash($gz)).Replace('-', '').ToLower() }
        finally { $sha.Dispose(); $gz.Dispose() }
    } finally { $in.Dispose() }
}

New-Item -ItemType Directory -Force -Path $LocalDir | Out-Null

# 1) one ssh: sha256 of every remote *.jsonl  ->  @{ name = hash }
$listing = Invoke-Ssh "cd '$RemoteDir' 2>/dev/null && sha256sum *.jsonl 2>/dev/null"
if ($LASTEXITCODE -ne 0) { throw "ssh listing failed (exit $LASTEXITCODE) — check CA_HOST / SSH key / CA_REMOTE_DIR" }
$remote = [ordered]@{}
foreach ($ln in ($listing -split "`n")) {
    $ln = $ln.Trim(); if (-not $ln) { continue }
    if ($ln -match '^([0-9a-f]{64})\s+(.+)$') { $remote[$Matches[2]] = $Matches[1] }
}
if ($remote.Count -eq 0) { Write-Host 'no remote *.jsonl yet — nothing to pull'; return }

# 2) which files do we still need? (no verified local copy with a matching hash)
$needed = @($remote.Keys | Where-Object { -not (Have $_ $remote[$_]) } | Sort-Object)

if ($needed.Count -gt 0) {
    $fileArgs = ($needed | ForEach-Object { "'$_'" }) -join ' '
    Invoke-Ssh "cd '$RemoteDir' && tar czf '$RemoteTar' $fileArgs" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "remote tar failed (exit $LASTEXITCODE)" }

    $tmpTar = Join-Path ([IO.Path]::GetTempPath()) 'ca-pull.tar.gz'
    $tmpDir = Join-Path ([IO.Path]::GetTempPath()) ('ca-pull-' + [IO.Path]::GetRandomFileName())
    scp @SshOpt -q "${RemoteHost}:$RemoteDir/$RemoteTar" $tmpTar
    if ($LASTEXITCODE -ne 0) { throw "scp of archive failed (exit $LASTEXITCODE)" }
    New-Item -ItemType Directory -Force -Path $tmpDir | Out-Null
    tar xzf $tmpTar -C $tmpDir
    if ($LASTEXITCODE -ne 0) { throw "local extract failed (exit $LASTEXITCODE)" }

    foreach ($name in $needed) {
        $src = Join-Path $tmpDir $name
        if (-not (Test-Path -LiteralPath $src)) { Write-Warning "missing from archive: $name"; continue }
        $h = (Get-FileHash -LiteralPath $src -Algorithm SHA256).Hash.ToLower()
        if ($h -ne $remote[$name]) { Write-Warning "checksum mismatch (NOT stored): $name"; continue }
        Copy-Item -LiteralPath $src -Destination (Join-Path $LocalDir $name) -Force
        Set-Content -LiteralPath (Sidecar $name) -Value $h -NoNewline
    }
    Invoke-Ssh "rm -f '$RemoteDir/$RemoteTar'" | Out-Null      # remove ONLY our temp archive, never data
    Remove-Item -LiteralPath $tmpTar, $tmpDir -Recurse -Force -ErrorAction SilentlyContinue
}

# 3) finalize pass: gzip immutable past-day files locally (event-date < today UTC); raw -> .jsonl.gz.
#    Covers every dated prefix (transitions|ladders|trades — wave-2 spec compat item 2: without this the
#    new files would accumulate on the droplet unbounded). Today's dated files + the undated live files
#    (sessions/cli/fee_changes) stay raw (still being appended). Verified content only.
$finalized = 0
foreach ($raw in (Get-ChildItem -LiteralPath $LocalDir -Filter '*.jsonl' -File -ErrorAction SilentlyContinue)) {
    if ($raw.Name -notmatch '^(transitions|ladders|trades)-(\d{4}-\d{2}-\d{2})\.jsonl$') { continue }
    if ($Matches[2] -ge $TodayUtc) { continue }                          # today/future -> still live
    if (-not (Test-Path -LiteralPath (Sidecar $raw.Name))) { continue }  # only verified content
    if (Test-Path -LiteralPath "$($raw.FullName).gz") {
        # The day was already archived, then the file REAPPEARED remotely (a late append for a past
        # event-date recreates it after our verified delete). NEVER re-gzip: Create() truncates, which
        # would replace the canonical full-day .gz with the late-records-only file (data loss). Keep
        # the raw (it holds ONLY the late records) NEXT TO the .gz -- loaders glob both and
        # analyze_persistence dedups per date -- and leave the remote copy alone (step 4 only deletes
        # when the local .gz decompresses to the remote hash, which it now won't).
        Write-Warning "late-append on archived day: $($raw.Name) kept RAW beside its .gz (not re-gzipped; remote kept)"
        continue
    }
    $in = [IO.File]::OpenRead($raw.FullName)
    try {
        $out = [IO.File]::Create("$($raw.FullName).gz")
        try {
            $gz = [IO.Compression.GZipStream]::new($out, [IO.Compression.CompressionLevel]::Optimal)
            $in.CopyTo($gz); $gz.Dispose()
        } finally { $out.Dispose() }
    } finally { $in.Dispose() }
    Remove-Item -LiteralPath $raw.FullName -Force
    $finalized++
}

# 4) MOVE finalized days off the droplet (unless CA_KEEP_REMOTE): delete a remote dated file ONLY after
#    the local .gz is verified to DECOMPRESS to the remote sha256. Live files (today's dated files +
#    sessions/cli/fee_changes/health) stay.
$moved = 0
if (-not $KeepRemote) {
    $cand = @()
    foreach ($name in $remote.Keys) {
        if ($name -notmatch '^(transitions|ladders|trades)-(\d{4}-\d{2}-\d{2})\.jsonl$') { continue }  # dated data files only
        if ($Matches[2] -ge $TodayUtc) { continue }                                     # finalized (past) only
        $gz = Join-Path $LocalDir "$name.gz"
        if (-not (Test-Path -LiteralPath $gz)) { continue }                             # need the local archive
        if ((Get-GzHash $gz) -ne $remote[$name]) { Write-Warning "local .gz != remote for $name — NOT deleting remote"; continue }
        $cand += $name
    }
    # RE-HASH the LIVE remote NOW, just before deleting, to close the list->delete TOCTOU: only delete a file
    # whose CURRENT remote hash still equals the step-1 listing hash. A past-event-date market still appending
    # across UTC-midnight will have grown -> hash differs -> we skip it (re-pulled next run) instead of truncating.
    $del = @()
    if ($cand.Count -gt 0) {
        $fresh = @{}
        $reCmd = "cd '$RemoteDir' && sha256sum " + (($cand | ForEach-Object { "'$_'" }) -join ' ') + " 2>/dev/null"
        foreach ($ln in (Invoke-Ssh $reCmd -split "`n")) {
            if ($ln.Trim() -match '^([0-9a-f]{64})\s+(.+)$') { $fresh[$Matches[2]] = $Matches[1] }
        }
        foreach ($name in $cand) {
            if ($fresh.ContainsKey($name) -and $fresh[$name] -eq $remote[$name]) { $del += $name }
            else { Write-Warning "remote $name changed since listing (still appending?) — NOT deleting" }
        }
    }
    if ($del.Count -gt 0) {
        $rmArgs = ($del | ForEach-Object { "'$RemoteDir/$_'" }) -join ' '
        Invoke-Ssh "rm -f $rmArgs" | Out-Null
        if ($LASTEXITCODE -ne 0) { Write-Warning "remote delete exited $LASTEXITCODE" } else { $moved = $del.Count }
    }
}

$rawN = @(Get-ChildItem -LiteralPath $LocalDir -Filter '*.jsonl'    -File).Count
$gzN  = @(Get-ChildItem -LiteralPath $LocalDir -Filter '*.jsonl.gz' -File).Count
Write-Host "pull complete -> $LocalDir"
Write-Host "  fetched/updated: $($needed.Count)   gzipped finalized: $finalized   moved off droplet: $moved   (raw live: $rawN, archived .gz: $gzN)"
$mode = if ($KeepRemote) { 'copy-keep (CA_KEEP_REMOTE set)' } else { 'finalized days verified-then-deleted on droplet; live files kept' }
Write-Host "  $mode; every transfer sha256-verified."

# 5) daily settlement recon (probe-program 2026-06-11, owner-ask #3): the scheduled 8:30am ET pull
#    lands ~12:30Z = ~T+0.5h after Kalshi's ~12:02Z weather settlement — exactly the previously
#    unobserved 0–13.5h pmus-finality window. Read-only; a failure NEVER blocks the pull (warn only);
#    ALERT.txt is raised only on a real DIVERGE (the both-legs-loss tail). Skip with CA_NO_RECON=1.
if (-not $env:CA_NO_RECON) {
    try {
        $repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
        $log  = Join-Path $LocalDir 'settle_recon_daily.log'
        $out  = & python (Join-Path $repo 'scripts\settle_recon.py') --days-back 3 --no-sports --no-nws 2>&1 | Out-String
        Add-Content -LiteralPath $log -Value ("`n===== {0:u} =====`n{1}" -f [DateTime]::UtcNow, $out)
        if ($out -match '!!! DIVERGE') {
            Add-Content -LiteralPath (Join-Path $LocalDir 'ALERT.txt') -Value ("{0:u} settle_recon DIVERGENCE — see settle_recon_daily.log" -f [DateTime]::UtcNow)
            Write-Warning 'daily settle-recon: DIVERGENCE detected (settle_recon_daily.log / ALERT.txt)'
        } else {
            Write-Host '  daily settle-recon: no divergence (settle_recon_daily.log)'
        }
    } catch { Write-Warning "daily settle-recon skipped: $_" }
}
