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

- **Econ (CPI/FOMC/GDP/NFP/U-3) IS live and US-legal on polymarket.us** — 36 live macro markets, each settling
  on the *same* government print Kalshi uses (BLS/BEA/Fed); per [research/us-legal-overlap-audit.md](research/us-legal-overlap-audit.md)
  this is the *structurally cleanest* (identical-deterministic-number) subset, though episodic/consensus-priced
  so the spread is event-driven. **Only crypto is genuinely US-blocked** (absent from polymarket.us). So the
  US-legal overlap is **econ + weather + sports** (+ politics). *(Corrected 2026-06-09 reviewer audit C7 — the
  earlier "CPI/FOMC US-blocked, overlap = weather+sports" was refuted by the project's own live catalog pull.)*
  Econ is now **MAPPED + tracked** (`bot/colisted_map.py` `ECON`, 2026-06-09): **24 clean co-listed pairs**
  (U-3 9, GDP 6, Fed 5, NFP 3, CPI 1) on same family/period/threshold + same govt source, settlement identity
  verified (`scripts/verify_econ_settlement.py`); only SAME-orientation `≥`-threshold + Fed-categorical pairs
  mapped (pmus `≤`-tails = opposite orientation, and "exactly X%" point-buckets, are skipped+flagged). So the
  monitor/analyses now cover **the full US-legal universe** (weather + sports + econ), not a subset.
- **Settlement identity for weather: source + station + bucket boundaries VERIFIED**
  (`scripts/verify_settlement.py`, 2026-06-09): all 5 mapped cities grade off the **same** NWS Climatological
  Report (Daily), at the **same station** (incl. NYC = Central Park). Bucket boundaries verified on a low-tail
  AND a **middle 2° bucket** (SFO `66-67°` ↔ pmus `gte66lt67f`, both inclusive `[66,67]`, same source/station);
  `colisted_map.py` now **enforces** boundary-number equality (`pm_bounds`/`kbounds`) and refuses+flags any
  mismatch. Refutes the old NWS-vs-Wunderground fear (`miami-temp-arb.html`). **Settlement timing RESOLVED**:
  both sides now **primary-source confirmed** — Kalshi waits past 8 AM for the *final* (MIA expiry 10 AM EDT),
  and the pmus weather **FAQ** specifies **8 AM**, delaying to **11 AM only for a CLI-vs-METAR inconsistency**
  (not a downward CLI correction). So the one residual is a *downward* 8–11 AM CLI correction on a boundary day
  (asymmetry **narrowed**, not eliminated); its **rate** is logged live (`monitor.py` `cli_stream` → `cli.jsonl`
  → `scripts/cli_revisions.py`). See [research/settlement-verification.md](research/settlement-verification.md).
- **Settlement identity for SPORTS: clean only for games that COMPLETE on schedule** (new 2026-06-09,
  `scripts/verify_sports_settlement.py`, 10 leagues read): normal completed games agree, but the **postpone/
  void tail diverges — materially for MLB, the depth-and-edge proof case.** Kalshi waits for a replay only if
  rescheduled **≤2 days** (else voids to *"a fair price"*); pmus waits **≤2 weeks** (else *last-traded*). So a
  rain-postponed game replayed in that **2-day–2-week gap settles to the real winner on pmus but a fair-price
  void on Kalshi** → the YES/NO legs stop offsetting → **both-legs loss** (and even a symmetric void doesn't
  net: Kalshi *"fair price"* ≠ pmus *"last-traded"*). Esports/WNBA non-completion: pmus → last-price, Kalshi
  silent. **Mitigation TODO**: don't hold an MLB pair through a postponement (unwind before the 2-day window);
  model the void EV term (0010). See [research/sports-settlement-verification.md](research/sports-settlement-verification.md).
- **Edge-location and scale-capacity are DIFFERENT axes.** *Edge* (the gap) lives in **inefficient corners**
  (thin, intermittent — line lag, settlement quirks); *capacity* (deployable size before you walk the book
  past the edge) lives in **depth**, and the efficient deep books (tennis/UFC/ITF) are ~$0 cross-venue
  precisely *because* they're arbitraged. The scalable money is their **intersection** — a deep book
  *transiently* dislocated: the **one observed instance** of depth-and-edge co-occurring is **MLB new-venue
  line-lag** (`lad-pit`, n=1: c2 grew ~4.8k→16k on a fresh pre-game book over one 8h overnight window) — a
  **hypothesis to confirm across more games, not yet a class property** (the 06-10 MLB markets did *not*
  replicate it). So scaling = **breadth of depth-AND-edge events** sized to each book's depth, not bigger
  clips in one corner ([tasks/lessons.md](tasks/lessons.md) L16).
  **Magnitudes are PRELIMINARY** and come from a **single ~8h morning window** (no afternoon/evening coverage;
  all "/day" figures are a ×3 extrapolation). Two adversarial reviews
  ([independent-review](tasks/independent-review-2026-06-09.md), [reviewer-audit](tasks/reviewer-audit-2026-06-09.md))
  found the earlier "$/day" prose unsupported and a capital double-count (~6–7× — now fixed in `capital_sim.py`:
  corrected peak ≈ $13.6k / ~6%/day, still preliminary). Treat edge size as unproven until the live monitor +
  `scripts/capital_sim.py` accumulate. The bot decides what to trade.
- **Hardened post-review (two passes):** per-order fee + crossed-book + entry-guard fixes (`bot/ledger.py`,
  `bot/monitor.py`) plus per-transition depth + staleness instrumentation. `age` is a coarse staleness hint
  (a resting-but-tradeable quote and a wedged stream both accrue large `age`); **depth** does the real
  fillability work. Second audit (2026-06-09) added: WS reconnect (no more silent half-dead collector),
  invariant-#2 guards in the live matcher (exact-date game binding, bucket boundary-equality, orientation
  price-guard), and the econ-legality correction. Open items tracked in [tasks/todo.md](tasks/todo.md);
  full findings in [tasks/reviewer-audit-2026-06-09.md](tasks/reviewer-audit-2026-06-09.md).
- **Execution feasibility — read-only tests run (2026-06-09, [research/execution-feasibility-2026-06-09.md](research/execution-feasibility-2026-06-09.md)).**
  **Latency MEASURED** (~86–261 ms RTT, network-bound → compute language is noise; Rust deferred — see
  [research/latency-playbook.md](research/latency-playbook.md)). **Leg-fill is the gating risk**: shadow-fill shows
  hit-rate collapses with latency (39% naked at 1 s, 67% at 2 s), but the sub-second regime where real fills live
  was below the old integer-second data resolution — now fixed (`monitor.py` logs **ms timestamps** + per-venue
  `px`), so it + adverse-selection become measurable after the next deploy. **Settlement identity empirically
  OPEN**: `settle_recon.py` found pmus `closed`≠finalized (~2-week lag, unreliable interim outcomes — one verified
  wrong), so invariant #1 stays rules-verified-only until pmus finalizes. **None of this needs capital — it needs
  the next gated deploy + weeks of data.** Reinforces: this is a *measurement rig*, not yet a go.
- **Capital velocity — MEASURED (2026-06-09, `scripts/exit_liquidity.py` + `capital_velocity.py`).** Velocity,
  not edge, separates the categories. pmus **freezes the order book at resolution** (10/10 resolved markets had
  empty books) → **no early-exit** → capital is locked to the far-future `endDate`. With the `endDate` probe
  (weather ~1.2d, sports/econ ~15d): **weather is the capital-efficiency standout** (fast natural settlement),
  while **sports (~15d, book frozen) and econ (weeks-mo, outcome known only at the far-future release) are
  capital-locked** — the early-exit "rescue" for sports is refuted. So weather = clean+fast+thin; sports = deep
  but capital-slow + void tail; econ = cleanest but slowest. No category wins {clean, fast capital, deep}.

## Managerial docs index

Every doc that governs *how this project is worked on*. Keep this list current — adding a managerial
doc without linking it here leaves the index incomplete.

| Doc | Purpose |
|---|---|
| [README.md](README.md) | Human-facing project overview, thesis, how to run |
| [tasks/todo.md](tasks/todo.md) | Live plan + checklist; the current work item lives here |
| [tasks/lessons.md](tasks/lessons.md) | Mistakes made → rules to not repeat them (self-improvement loop) |
| [tasks/independent-review-2026-06-09.md](tasks/independent-review-2026-06-09.md) · [tasks/reviewer-audit-2026-06-09.md](tasks/reviewer-audit-2026-06-09.md) | The two adversarial reviews + their fix logs |
| [docs/sessions.md](docs/sessions.md) | Session log / process changelog |
| [decisions/README.md](decisions/README.md) | Decision-log convention + index of entries |
| [decisions/template.md](decisions/template.md) | Skeleton for a new decision entry |
| [research/README.md](research/README.md) | Index of the 12 research briefs + the latency playbook (the evidence base) |
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
