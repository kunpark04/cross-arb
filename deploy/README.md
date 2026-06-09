# deploy/ — droplet deployment for the read-only persistence monitor

Artifacts to run `bot/monitor.py` as a long-lived, supervised, **read-only** logger on a DigitalOcean
droplet — the gated data-collection run that feeds the go/no-go gate ([tasks/todo.md](../tasks/todo.md)).

> **GATED.** This deploy must not be run without explicit owner sign-off
> ([decision 0006](../decisions/0006-deploy-on-digitalocean-consult-first.md)). These files make the
> deploy *one command* **when** greenlit — they do not greenlight it. The monitor places no orders.

## Recommended droplet

The monitor is **featherweight and I/O-bound**: one asyncio process holding two WebSocket connections
(polymarket.us + Kalshi), a few hundred small in-memory order books, recomputing a trivial edge calc per
delta. The real requirement is *runs reliably 24/7*.

**Measured, not assumed** (`scripts/probe_monitor_footprint.py`, 2026-06-08 — read-only, no creds):
tracking **all** weather + **all** sports = **227 pmus slugs / 394 Kalshi tickers** right now (60 weather
markets + 167 sports games), the full tracker/book state is **7.2 MB of heap** (40-level ladders = upper
bound) ⇒ **~80 MB resident RSS**. The monitor **prunes settled markets** (`run_live` frees any market gone
from discovery for `PRUNE_THRESHOLD` heartbeats), so over a simulated **30-day** run the heap stays **flat
at ~7 MB** instead of growing ~7 MB/day — **memory does NOT scale with run length**. Each logged
transition is **119 bytes**.

| Spec | Pick | Why |
|---|---|---|
| **Plan** | Basic / Regular | shared CPU is fine — single-thread asyncio, never pegs a core |
| **Size** | **1 vCPU · 1 GB · 25 GB SSD ($6/mo)** | resident RSS is ~80 MB and **flat for the life of the run** (idle-market pruning, verified to 30 days) → a 1 GB box sits ~90% idle, **no restart hygiene needed**. The two deps (`websockets`, `cryptography`) install from wheels on 1 GB with no swap. Step to **2 GB ($12)** only for headroom to `grep`/pandas the growing log on-box. |
| **Region** | **NYC1 / NYC3** | both venue endpoints (`api.elections.kalshi.com`, `api.polymarket.us`) are US-hosted; lowest WS latency ⇒ fewer seq-gap resubscribes, cleaner deltas |
| **OS** | **Ubuntu 24.04 LTS** | what `provision.sh` assumes (`apt`, `python3-venv`, systemd) |

Disk is a non-issue: at **119 bytes/transition**, even 100k transitions/day is ~12 MB/day — months of
runway on 25 GB. Bandwidth (small order-book delta frames) is far under DO's included monthly transfer.

**Bottom line:** verified tiny **and flat over time** — the binding constraint is "stays up 24/7," not
capacity. **$6 / 1 GB / NYC1 / Ubuntu 24.04 LTS** is the right pick; 2 GB only buys on-box analysis room.

## What's here

| File | Runs where | Purpose |
|---|---|---|
| [`deploy.sh`](deploy.sh) | your laptop | one command: ship the runtime cone → secrets out-of-band → provision → start |
| [`provision.sh`](provision.sh) | the droplet (root) | create the confined `cross-arb` user, venv + pinned deps, `0700` the dir, install + enable the unit |
| [`cross-arb-monitor.service`](cross-arb-monitor.service) | the droplet | systemd unit: runs as `cross-arb`, `--forever`, `Restart=always`, read-only-FS hardening, journald |
| [`pull-data.ps1`](pull-data.ps1) | your laptop | **foolproof pull** — sha256-verified + idempotent mirror to `Kalshi/data/cross-arb/`; gzips finalized days, then verified-deletes them on the droplet |
| [`healthcheck.ps1`](healthcheck.ps1) | your laptop | liveness check — alerts (desktop balloon + `ALERT.txt` + non-zero exit) if the collector stops |
| [`register-tasks.ps1`](register-tasks.ps1) | your laptop | register both jobs: `PullCrossArbData` (daily) + `CrossArbHealthcheck` (every 30 min) |

## Deploy (once greenlit)

From the repo root, on your machine (needs `ssh` + `scp` — Git Bash, WSL, or macOS/Linux; **no rsync**):

```bash
deploy/deploy.sh cross-arb-droplet        # an ~/.ssh/config Host alias (User root), or root@<ip>
```

Five steps: (1) make the dir skeleton; (2) `scp` **only the runtime cone** — the 4 bot modules
(`ledger`, `kalshi_book`, `colisted_map`, `monitor`) + `requirements.txt` + the unit/provision — *not* the
whole repo; (3) `scp` the two secrets **out-of-band**; (4) `provision.sh`; (5) `systemctl restart` + status.

### Confinement

A dedicated unprivileged **`cross-arb`** system user (no login shell) owns **only** `/opt/cross-arb`, mode
**`0700`** (readable by that user + root only). The unit runs as `cross-arb` with `ProtectSystem=strict`
(whole FS read-only **except** `scripts/_data/` via `ReadWritePaths`), `ProtectHome`, `NoNewPrivileges`,
`PrivateTmp`, plus syscall/namespace/address-family restrictions. This filesystem + syscall hardening means a
compromised process **cannot modify its own code or read other users' data** — but it is FS-scoped, **not** an
egress restriction: the box itself retains outbound network. Order-placement is impossible **only** because the
loaded Kalshi key is **read-only on Kalshi's side** (a venue-side ACL, [decision 0007](../decisions/0007-readonly-kalshi-key-least-privilege.md)),
not because of droplet confinement. So **never stage a trade-capable key here.**

> systemd has **no inline comments** — `Directive=value   # note` silently breaks the value. Keep comments
> on their own line. (This bit us: `ProtectSystem=strict  # …` parsed as a bad value and was ignored until
> caught in verification — see [tasks/lessons.md](../tasks/lessons.md).)

### Secrets (off-git, by hand-off only)

The monitor authenticates two **read-only** WS handshakes, so the box needs:

- `scripts/.env` — `PMUS_ACCESS_KEY`, `PMUS_SECRET`, `KALSHI_ACCESS_KEY`, `KALSHI_PRIVATE_KEY_PATH`
- `scripts/kalshi_readonly.pem` — the Kalshi **read-only** RSA key ([decision 0007](../decisions/0007-readonly-kalshi-key-least-privilege.md))

Gitignored; live only on your machine + the droplet. `deploy.sh` `scp`s them and `provision.sh` `chmod 600`s
them. **Never** commit them or bake them into a snapshot. The trade-capable Kalshi key never goes near the box.

## Data — event-date partitioned, foolproof pull ([decision 0009](../decisions/0009-event-date-partition-copy-keep-pull.md))

The monitor partitions the log by the **event-date embedded in each market key**
(`transitions-<YYYY-MM-DD>.jsonl`), not by wall-clock day — so a market's whole edge lifecycle stays in
**one** file even when it straddles UTC midnight (no "cut-off"). Each boot appends a `session_start` line to
`sessions.jsonl` so restart-aware analysis won't mistake a post-restart re-OPEN for a new edge.

`pull-data.ps1` mirrors those files to **`Kalshi/data/cross-arb/`**:

- **Checksum-verified + idempotent** — every transfer is sha256-checked before it's trusted (sidecar
  `<name>.sha256`); unchanged files are skipped; a still-growing live file self-heals on the next pull.
- **Verified move of finalized days** — a finalized day (event-date < today UTC, immutable) is gzipped
  locally to `transitions-<date>.jsonl.gz`, **then deleted on the droplet — but only after the local `.gz`
  is verified to decompress back to the remote sha256** (verify-before-delete). The local archive is the
  canonical copy of past days; the droplet keeps only live files. (`CA_KEEP_REMOTE=1` = never delete.)
- **Live files stay** — today's `transitions-<today>.jsonl`, `sessions.jsonl`, and the `health.json` beacon
  are still being written, so they are copied, never deleted.

Install both scheduled jobs once: `pwsh deploy/register-tasks.ps1` → **`PullCrossArbData`** (daily 8:30am)
runs the pull and **`CrossArbHealthcheck`** (every 30 min) runs the liveness check; `-StartWhenAvailable`
covers missed runs. Run either on demand: `pwsh deploy/pull-data.ps1` · `pwsh deploy/healthcheck.ps1`.

## Operate

```bash
ssh cross-arb-droplet systemctl status  cross-arb-monitor     # health
ssh cross-arb-droplet journalctl -u cross-arb-monitor -f      # live OPEN/CLOSE/FLIP/WIDEN/NARROW + heartbeat
ssh cross-arb-droplet systemctl restart cross-arb-monitor     # restart (re-discovers; settled markets pruned)
ssh cross-arb-droplet systemctl stop    cross-arb-monitor     # stop
pwsh deploy/pull-data.ps1                                     # pull data -> Kalshi/data/cross-arb/
pwsh deploy/healthcheck.ps1                                   # liveness check now (alerts if collector down)
```

Each JSONL line is durable (`open`/append/`close` per write), so a stop / reboot / crash loses nothing in
flight — `Restart=always` brings it back and the heartbeat re-discovers the live universe.

## Notes / hardening

- **Liveness/alerting:** the monitor overwrites `health.json` every heartbeat; `CrossArbHealthcheck`
  (every 30 min) alerts if the droplet is unreachable, the service is down, or the beacon goes stale (a
  hung-but-`active` process systemd wouldn't restart) — desktop balloon + `Kalshi/data/cross-arb/ALERT.txt`
  + a failed Task-Scheduler run. For richer toasts: `Install-Module BurntToast -Scope CurrentUser`.
- **Resilience:** `Restart=always` covers process crashes; `StartLimitBurst=20 / 300s` stops a real fault
  (bad creds / dead endpoint) from hammering the venues. Optionally enable a DO **firewall** (inbound SSH
  only) + weekly snapshots. Note: after the verified move, past days live **only locally** — set
  `CA_KEEP_REMOTE=1` if you'd rather the droplet keep a copy too.
