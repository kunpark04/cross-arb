# 0009 — Persistence data: event-date partitioning + checksum-verified move-pull

- **Date:** 2026-06-09
- **Status:** Accepted
- **Deciders:** owner + Claude

## Context

The droplet monitor ([0006](0006-deploy-on-digitalocean-consult-first.md)) produces the persistence
dataset that feeds the go/no-go gate. The first cut wrote a single append-only `transitions.jsonl` and a
manual `scp`. Asked "is a daily zip the best way / what happens when a market gets cut off / what's the
most foolproof way," we found a wall-clock daily file **splits a market's edge lifecycle** at UTC midnight
(an MLB edge that OPENs 23:55Z and CLOSEs 00:20Z lands its two ends in different files), and a monitor
restart re-emits an OPEN with no marker — both corrupt per-market persistence analysis.

## Decision

Organize and move the data by **market identity, never destroy the source**:

1. **Partition by event-date parsed from the market key** — `transitions-<YYYY-MM-DD>.jsonl`, where the
   date is the one embedded in the slug (`aec-mlb-sea-bal-2026-06-10`, `tc-temp-laxhigh-2026-06-09-…`), NOT
   the wall-clock day. A market's whole lifecycle stays in one file regardless of midnight crossings.
2. **Restart marker** — each monitor boot appends a `session_start` line to `sessions.jsonl`.
3. **Verified-move pull** (`deploy/pull-data.ps1`) — mirror to `Kalshi/data/cross-arb/`, every transfer
   **sha256-verified** (sidecar `<name>.sha256`) and **idempotent**. A finalized day (event-date < today
   UTC) is gzipped locally and then **deleted on the droplet — only after the local `.gz` is verified to
   decompress back to the remote sha256** (verify-before-delete). Live files (today's transitions,
   `sessions.jsonl`, `health.json`) are copied, never deleted. `CA_KEEP_REMOTE=1` forces pure copy-keep.
   Scheduled daily (`PullCrossArbData`); a `CrossArbHealthcheck` job + the monitor's `health.json` beacon
   alert if collection stops.

## Alternatives considered

- **Wall-clock daily zip (weather-alpha-style `.zip` per day).** Rejected: weather-alpha zips a *folder of
  many per-market files* keyed by event-date; our single line-stream isn't that shape, and a *wall-clock*
  day splits lifecycles. (weather-alpha itself files by event-date, not poll-day — same principle.)
- **Pure copy-keep (never delete).** Considered; still available via `CA_KEEP_REMOTE=1`. Not the default —
  the owner wants the droplet bounded. The risk that made copy-keep tempting (delete is the only data-loss
  step) is neutralized by **verify-before-delete**: the remote file is removed only after the local gzip is
  proven to decompress to the exact remote sha256, so a bad/partial pull can never trigger a delete.
- **Single growing `transitions.jsonl`.** Rejected: no per-market self-containment, no clean archival, and
  "move" semantics are impossible on an always-open file.

## Consequences

- Per-market persistence analysis is a single-file read, robust to midnight and to restarts (cross-check
  `sessions.jsonl`). gzip is lossless tidiness, **not** the integrity mechanism — integrity is (1)
  organize-by-identity + (2) never-mutate-the-source + (3) checksum-verify.
- New invariant: `TransitionLogger` writes are keyed on `event_partition(market)`; analysis code and the
  pull both assume the `transitions-<date>.jsonl[.gz]` + `sessions.jsonl` layout. A market key with **no**
  parseable date falls back to `transitions-misc.jsonl`.
- After the verified move, past days live **only locally** (the gzipped archive is canonical) and the
  droplet holds just live files, so its `_data` stays bounded. The integrity guard is verify-before-delete
  + the decompress-check — never an unverified deletion. `CA_KEEP_REMOTE=1` restores a dual-copy posture.
- Liveness is observable: the monitor's `health.json` beacon + the `CrossArbHealthcheck` alert close the
  "is it still collecting?" gap — a hung-but-`active` process is caught by beacon staleness, not just by
  systemd (which only restarts on a hard crash).
