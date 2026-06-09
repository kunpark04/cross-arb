# CLAUDE.md — cross-arb

Index + working agreement for this project. Read this first every session. It points to every
managerial doc; it does **not** duplicate their content.

## What this is

Research into a **cross-venue arbitrage** between two US-legal, CFTC-regulated prediction markets:
**Kalshi** and **polymarket.us** (QCX). The bet: when the *same real-world event* is listed on both
venues and **both grade off the identical deterministic public number** (e.g. NWS CLI high temp), a
price gap is a structurally clean, US-legal arb — buy YES on the cheap venue + NO on the dear venue,
hold to settlement, collect the gap net of fees.

**Phase: READ-ONLY.** No capital is deployed and no orders are placed. All market data on both venues
is public (no auth needed for books). We are measuring whether a real, persistent, scalable edge
exists before wiring up trading keys. The read-only **persistence monitor** (`bot/monitor.py`, todo #10)
is **DEPLOYED + LIVE** since 2026-06-09 — running 24/7 on a DigitalOcean droplet as a confined `cross-arb`
user ([0006](decisions/0006-deploy-on-digitalocean-consult-first.md)), collecting the event-date-partitioned
persistence dataset pulled daily to `Kalshi/data/cross-arb/` ([0009](decisions/0009-event-date-partition-copy-keep-pull.md)).
See [deploy/README.md](deploy/README.md).

## Core findings (as of 2026-06-09)

- The clean-settlement universe (CPI/FOMC/crypto) is **US-blocked** — it only lists on *international*
  Polymarket, which a US person can't legally trade. The US-legal overlap is **weather + sports**.
- **Settlement identity for weather is VERIFIED** (`scripts/verify_settlement.py`, 2026-06-09): all 5 mapped
  cities grade off the **same** NWS Climatological Report (Daily), at the **same station** (incl. NYC =
  Central Park), with **matching** sampled bucket boundaries — refuting the old NWS-vs-Wunderground
  source-divergence fear (`miami-temp-arb.html`). Settlement *timing* researched + live-object-read (2026-06-09):
  both grade off the **same morning CLI**; one narrow residual risk — a *downward* morning CLI correction.
  Kalshi's side is now **primary-source confirmed** (rules say *"final value"*, a live MIA market expires
  10 AM EDT — it waits past 8 AM); pmus's *"locks at 8 AM"* is **still third-party** (its FAQ *and* live market
  object carry no timing language). The **revision rate** is now logged live (`monitor.py` `cli_stream` →
  `cli.jsonl` → `scripts/cli_revisions.py`). See [research/settlement-verification.md](research/settlement-verification.md).
- Edge appears in **inefficient corners, not deep books** — deep liquid books (tennis/UFC/ITF) are ~$0
  cross-venue; live edges show in **MLB** (new-venue line lag) + **weather** (intermittent). **Magnitudes
  are PRELIMINARY:** an independent review (2026-06-09,
  [tasks/independent-review-2026-06-09.md](tasks/independent-review-2026-06-09.md)) found the earlier
  "$/day" prose unsupported by the thin data + a wrong fee model — treat edge size as unproven until the
  live monitor + `scripts/capital_sim.py` accumulate. The bot decides what to trade.
- **Hardened post-review:** per-order fee + crossed-book + entry-guard fixes (`bot/ledger.py`,
  `bot/monitor.py`) plus per-transition depth + staleness instrumentation, so a persistent-**fillable** edge
  is distinguished from a persistent-**stale-phantom** one. Open items tracked in [tasks/todo.md](tasks/todo.md).

## Managerial docs index

Every doc that governs *how this project is worked on*. Keep this list current — adding a managerial
doc without linking it here leaves the index incomplete.

| Doc | Purpose |
|---|---|
| [README.md](README.md) | Human-facing project overview, thesis, how to run |
| [tasks/todo.md](tasks/todo.md) | Live plan + checklist; the current work item lives here |
| [tasks/lessons.md](tasks/lessons.md) | Mistakes made → rules to not repeat them (self-improvement loop) |
| [docs/sessions.md](docs/sessions.md) | Session log / process changelog |
| [decisions/README.md](decisions/README.md) | Decision-log convention + index of entries |
| [decisions/template.md](decisions/template.md) | Skeleton for a new decision entry |
| [research/README.md](research/README.md) | Index of the 9 research briefs (the evidence base) |
| [scripts/README.md](scripts/README.md) | Index of probe/scan scripts + `_data/` outputs |
| [bot/README.md](bot/README.md) | Accounting core (`ledger.py`) + the dual-stream monitor (`monitor.py`, `kalshi_book.py`, `colisted_map.py`) |
| [deploy/README.md](deploy/README.md) | Droplet deploy artifacts (systemd unit, provision/deploy/pull-logs scripts) + droplet sizing — GATED ([0006](decisions/0006-deploy-on-digitalocean-consult-first.md)) |

## Repo layout

```
cross-arb/
├─ CLAUDE.md            ← you are here (index + working agreement)
├─ README.md           ← human overview
├─ miami-temp-arb.html ← the seed idea (the ORIGINAL fat-edge artifact; now superseded — see findings)
├─ miami-screenshot.png
├─ docs/sessions.md    ← session log
├─ decisions/          ← decision log (README convention + numbered entries)
├─ tasks/              ← todo.md (plan) + lessons.md (corrections)
├─ research/           ← 9 settlement / legality / venue / edge briefs
├─ scripts/            ← Python probes + scanners (read-only); _data/ outputs are gitignored
├─ bot/                ← ledger.py (PnL core) + monitor.py (dual-stream logger) + kalshi_book.py + colisted_map.py
├─ deploy/             ← droplet deploy: systemd unit + provision/deploy/pull-logs scripts + runbook (GATED, 0006)
└─ requirements.txt    ← pinned runtime deps for the monitor (websockets, cryptography)
```

## Working agreement (project-specific)

- **Read-only until told otherwise.** Do not write order-placement code or commit anything that
  could place a trade. Auth keys exist only to *verify* read access; `scripts/.env` is gitignored.
- **Deployment is gated.** Live loggers (`bot/monitor.py`) + bot run on a DigitalOcean droplet —
  **consult the owner before any deploy** ([decisions/0006](decisions/0006-deploy-on-digitalocean-consult-first.md)).
  Dev stays local + read-only; `bot/monitor.py --live` is gated. Never push secrets to a remote host.
- **Two load-bearing invariants** for any "arb" claim:
  1. **Settlement identity** — both venues must grade off the *same* named deterministic number, else
     a "locked" pair can lose *both* legs. See [decisions/0001](decisions/0001-us-legal-only-venue-pair.md).
  2. **No false positives** — a join must be team/event-identity-correct, not text-similar. The
     first sports matcher produced a fake edge from ambiguous city names; see [tasks/lessons.md](tasks/lessons.md).
- **Comprehensive coverage, no pruning** — keep every co-listed market in scope and compute the same
  metrics for all; "not enough edge" is the live bot's call, not the scanner's.
  See [decisions/0002](decisions/0002-comprehensive-coverage-no-pruning.md).
- **Edge persistence is event-driven, not fixed-cadence polled.**
  See [decisions/0003](decisions/0003-event-driven-persistence.md).
- **Secrets:** never commit `scripts/.env`, `*.pem`, `*.key`, or `scripts/_data/` (all gitignored).
  Market data is public; only order placement needs keys.
- **Version control:** private GitHub repo `kunpark04/cross-arb` (`origin/main`). Commit in focused units
  when the user asks; verify nothing sensitive is staged before any push (gitignore covers `.env`/`*.pem`/`_data/`).
- **Scripts are throwaway-grade probes**, named for what they answer. Raw pulls land in
  `scripts/_data/` (gitignored). Conclusions get promoted into a `research/` brief.

## When you do substantive work here

- Capture load-bearing decisions as a numbered entry under `decisions/` (convention in its README).
- Append a `docs/sessions.md` entry for any session that ships code, makes a decision, or changes scope.
- After any user correction, add a `tasks/lessons.md` rule. (The `/update-managerial-docs` skill
  automates this refresh at session end.)
- Keep this index in sync when you add a managerial doc.
