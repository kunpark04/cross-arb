# cross-arb — Kalshi × polymarket.us cross-venue arbitrage (research)

A research project measuring whether a **structurally clean, US-legal arbitrage** exists between two
CFTC-regulated prediction markets — **Kalshi** and **polymarket.us** (QCX) — by pairing markets that
settle on the *identical* deterministic public number and pricing the cross-venue gap net of fees.

> **Status: read-only research phase.** No capital deployed, no orders placed. All pricing below comes
> from *public* order books on both venues. Trading keys are set up only to verify read access.

## The thesis in one paragraph

If the same event is listed on both venues and **both grade off the same named number** (e.g. the NWS
CLI daily high temperature, or a league's official game result), then a price difference between the
venues is a clean arb: buy the cheap side on one venue + the complementary side on the other for a
combined cost < $1, hold to settlement, keep the difference. The hard parts are (a) proving settlement
is *truly* identical (or you can lose both legs), (b) joining markets by real event identity (not
fuzzy text), and (c) confirming the edge is large and persistent enough to be worth the capital.

## What we found (2026-06-08)

- **Econ is live and US-legal — only crypto is blocked.** Macro econ (CPI, U-3, GDP, NFP, Fed) **is
  listed and US-legal on polymarket.us** — a same-day live pull found **36 live macro markets**, each
  grading on the same BLS/BEA/Fed print Kalshi uses. That makes econ the *structurally cleanest* subset
  (identical government number, deterministic), though **episodic** (it only trades around scheduled
  releases). The US-legal overlap is therefore **econ + weather + sports** (politics too); **only crypto**
  is genuinely US-blocked (international-only). (Corrects an earlier "econ is international-only" claim.)
  **Caveat (2026-06-10):** "same print" ≠ "same market" — pmus thresholds are **inclusive** (`≥T`, "at
  least") while Kalshi's are **strict** ("Above T"), so the settlement-identical pair is pmus `≥T` ↔
  Kalshi `>T−step` on the print grid; pairing equal threshold numbers manufactures a phantom "edge" equal
  to the market-priced P(print==T). **14 identical econ pairs** survive the corrected join
  ([research/econ-settlement-identity-2026-06-10.md](research/econ-settlement-identity-2026-06-10.md)).
- **Edge is in inefficient corners.** Deep, liquid books (tennis / UFC / ITF) are already arbitraged
  to ~$0 cross-venue. The live edge appears in **MLB** (~$23 in one snapshot, from the newer venue's
  line lagging) and **weather** (~$20/day gross, intermittent — appears mid-day, settles by EOD).
  **These magnitudes are PRELIMINARY and unproven:** each is drawn from a thin sample (the MLB figure
  from ~15 min of one market; the weather figure summed over a single ~8h window) computed under a
  fee model since found wrong, and is not yet corrected for book-walk depth decay. Treat them as
  order-of-magnitude placeholders until the live monitor + `scripts/capital_sim.py` accumulate.
- **The seed idea shrank but survived.** `miami-temp-arb.html` showed a fat ~24¢ Miami weather edge —
  but that came from Kalshi (NWS) vs *international* Polymarket (Weather Underground), two different
  thermometers. On the US-legal venue both grade on NWS, so the gap is ~5¢: real, clean, and small.

Verdict so far: a genuine US-legal edge exists but is **modest and capacity-limited** today. The open
question (`tasks/todo.md` item 10) is whether it **persists** enough to scale a bot around.

## How to run (read-only scans)

```bash
cd scripts
cp .env.example .env          # only needed for AUTH'd checks; market data is public
python weather_arb_scan.py    # live, fee-netted, depth-aware weather cross-venue scan
python scan_all.py            # unified scan across the full co-listed universe → _data/scan_all.json
python ../bot/ledger.py       # self-verifying PnL accounting harness (no network)
```

Outputs land in `scripts/_data/` (gitignored). See [scripts/README.md](scripts/README.md) for the
full script index.

## Repo map

- **[research/](research/README.md)** — the evidence base: settlement identity, US-legality, venue
  audits, and the first validated live-edge findings.
- **[scripts/](scripts/README.md)** — read-only probes and scanners; raw pulls in `_data/`.
- **[bot/](bot/README.md)** — `ledger.py`, the cross-venue PnL accounting core (layer/rotate/hold logic).
- **[tasks/todo.md](tasks/todo.md)** — current plan and the live work item.
- **[decisions/](decisions/README.md)** — why the project is shaped the way it is.
- **[CLAUDE.md](CLAUDE.md)** — the working agreement + full managerial-doc index (for AI agents).

## Constraints

- **US-legal venues only** (Kalshi + polymarket.us). International Polymarket is out of scope — blocked
  for US persons, and its UMA/Wunderground settlement breaks the identity guarantee.
- **Identical settlement required** for any pair we call an arb.
- **No false positives** in matching — a wrong join invents fake edges (see `tasks/lessons.md`).
- **Secrets never committed:** `scripts/.env`, `*.pem`, `*.key`, and `scripts/_data/` are gitignored.
