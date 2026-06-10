# 0011 — ECON co-listing: map CPI/U-3/NFP/GDP/Fed, but only SAME-orientation pairs

- **Date:** 2026-06-09
- **Status:** Accepted
- **Deciders:** owner + Claude

## Context

Coverage ([0008](0008-colisted-map-discovery-and-coverage-audit.md)) mapped only weather + sports, so **econ —
the structurally cleanest US-legal subset** ([0001](0001-us-legal-only-venue-pair.md),
[research/us-legal-overlap-audit.md](../research/us-legal-overlap-audit.md)) — was silently excluded from every
analysis (a hidden subset-filter, against [0002](0002-comprehensive-coverage-no-pruning.md)). Econ co-lists
richly (live: CPI/U-3/NFP/GDP/Fed, 36 pmus macro markets vs the matching Kalshi series), but has three
settlement-identity subtleties weather lacks, surfaced by `scripts/verify_econ_settlement.py`:
1. **Inequality** — pmus uses closed `≤T` / `≥T`; Kalshi "Above T" is strict `>T`. So pmus `≥T` YES ≡ Kalshi
   "Above T" YES only *away from* the boundary; a print landing **exactly on T** resolves them oppositely.
2. **Structure** — pmus CPI *middle* markets are "exactly X%" **point buckets** (no cumulative Kalshi twin);
   only CPI *tails* + the all-cumulative GDP/NFP/U-3 map directly. Fed is a 5-way categorical (label==label).
3. **Orientation** — the pmus `outcomes` array order varies (`["Yes","No"]` vs `["No","Yes"]`), but the pmus
   `/book` is **YES-oriented regardless** (verified: pm book mid ≈ Kalshi YES mid for the same `≥` threshold).

## Decision

Map econ in `bot/colisted_map.ECON` (mirrored in `scan_all.py`) on **family + period + threshold**, and co-list
**only SAME-orientation pairs**: pmus `≥T` YES ↔ Kalshi "Above T" YES, and Fed categorical label↔label. **Skip and
loudly flag** the rest — pmus `≤`-tails (pmus-YES = Kalshi-NO, opposite orientation) and "exactly X%" point
buckets (no cumulative Kalshi twin). Track econ via the weather-style 1:1 `MarketTracker`. Result: **24 clean
co-listed pairs** (U-3 9, GDP 6, Fed 5, NFP 3, CPI 1).

## Alternatives considered

- **Map `≤`-tails too by flipping orientation in the tracker** — rejected: needs per-entry YES/NO inversion, an
  invariant-#2 (false-positive) hazard for one extra pair (CPI `≤3.7`); not worth the complexity/risk.
- **Replicate point buckets by summing Kalshi cumulative buckets** (e.g. CPI "=3.8" = "Above 3.7" − "Above 3.8")
  — rejected: more legs, more fees, not a clean 1:1 lock; out of scope for now.
- **Leave econ unmapped** — rejected: silently excludes the cleanest subset from every analysis (the thing the
  "include all series" directive corrected).

## Consequences

- The monitor + all analyses now cover the **full US-legal universe** (weather + sports + econ), not a subset.
  Econ slugs (`cpic/gdpc/nfpc/urc/rdc-`) are a recognized category (`analyze_persistence.category`); econ gets
  **0 void-haircut** (cleanest settlement — no postpone/void tail).
- **Residual invariant**: the `≥`-vs-`>` boundary — a print landing exactly on `T` resolves the two legs
  oppositely (a narrow, *directional* both-legs risk, akin to the weather downward-correction). Flag, don't size
  through it without a tail-event check.
- **Caveat (capital):** econ is capital-*inefficient* — long pmus `endDate`, and **no early-exit** (the outcome
  is known only at the far-future release; book frozen at resolution — see
  [research/execution-feasibility-2026-06-09.md](../research/execution-feasibility-2026-06-09.md) §5). So econ
  widens *coverage* but is unlikely to be the deploy target on a capital-velocity basis.
- **Revisit if** pmus adds co-listable `≤`/point-bucket structures, lists **politics** (103 markets, still
  unmapped), or changes the `/book` orientation convention. Settlement identity is rules-text-verified per family
  (BLS/BEA/Fed) but not yet empirically confirmed (pmus finalization lag).
