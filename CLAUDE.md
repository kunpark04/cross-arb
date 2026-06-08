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
exists before wiring up trading keys. See `tasks/todo.md` for the live work item.

## Core findings (as of 2026-06-08)

- The clean-settlement universe (CPI/FOMC/crypto) is **US-blocked** — it only lists on *international*
  Polymarket, which a US person can't legally trade. The US-legal overlap is **weather + sports**.
- Edge lives in **inefficient corners, not deep books.** Deep liquid books (tennis/UFC/ITF) are
  fully arbitraged ($0 cross-venue). Real edge: **MLB** (~$23 right now, new-venue line lag) and
  **weather** (~$20/day gross, intermittent). Both kept in scope; the bot decides what to trade.
- The original fat ~24¢ "edge" (`miami-temp-arb.html`) was a **source divergence** (Kalshi=NWS vs intl
  Polymarket=Wunderground). The US-legal venue grades on the *same* NWS number, so that gap collapses
  to ~5¢ — real and clean, but small.

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
| [bot/README.md](bot/README.md) | The accounting core (`ledger.py`) and bot roadmap |

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
└─ bot/                ← ledger.py: cross-venue PnL accounting core
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
- **Scripts are throwaway-grade probes**, named for what they answer. Raw pulls land in
  `scripts/_data/` (gitignored). Conclusions get promoted into a `research/` brief.

## When you do substantive work here

- Capture load-bearing decisions as a numbered entry under `decisions/` (convention in its README).
- Append a `docs/sessions.md` entry for any session that ships code, makes a decision, or changes scope.
- After any user correction, add a `tasks/lessons.md` rule. (The `/update-managerial-docs` skill
  automates this refresh at session end.)
- Keep this index in sync when you add a managerial doc.
