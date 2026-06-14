# 0018 — Settlement identity = identical OUTCOME, not identical source STRING (4-category priced gate)

- **Date:** 2026-06-13
- **Status:** Accepted (refines [0001](0001-us-legal-only-venue-pair.md))
- **Deciders:** owner (the outcome-vs-source refinement + the tail-vs-structural distinction), Claude (the programmatic gate)

## Context

[0001] requires both venues to grade off "the same named deterministic number." Turning that into a
per-market programmatic gate (`scripts/settlement_identity.py`, this session) surfaced two ways the literal
"same named number" rule is too blunt:

1. **Source string ≠ outcome.** A FIFA World Cup match graded via ESPN (Kalshi) vs via FIFA's official
   result (pmus) settles to the SAME winner regardless — different *reporters* of one deterministic event.
   Requiring identical source STRINGS would wrongly reject it. Conversely for WEATHER the source genuinely
   matters: NWS CLI Daily vs raw METAR can read different temperatures.
2. **Most "divergences" are a small priceable TAIL, not a structural conflict.** The entire live DIVERGENT
   set was the one sports void/reschedule-window difference (Kalshi 2-day vs pmus 14-day) — a ~1.3%-postpone
   tail that [0010](0010-all-in-edge-filtering-and-cost-model.md) already prices at ~0.26¢/contract (MLB).
   Hard-excluding it throws away the bulk of the tradeable sports universe over a cost smaller than a typical edge.

## Decision

Refine the invariant-#1 gate on two axes:

- **Identity is of the OUTCOME, not the source string.** Score the *outcome-determining* dimensions per
  category; treat the named source as cosmetic where different sources report the same deterministic event.
  Source identity is scored ONLY where different sources yield different underlying numbers: **weather**
  (CLI vs METAR/station) and **econ** (settling agency). For **sports** the source string is recorded but
  NOT scored — the divergence lives in the outcome RULES (result-timing basis, void/reschedule handling,
  2-vs-3-way structure). Outcome-count is **discovered from market structure** (pmus `marketSides`, Kalshi's
  set of distinct outcome markets), never hard-coded.
- **Four-category verdict, not binary.** `IDENTICAL` (zero settlement risk) · **`TAIL`** (differs only on a
  low-probability tail whose expected cost is QUANTIFIED in ¢/contract — void-window via [0010]'s
  `void_haircut`, CLI-revision via `cli_revisions` — *tradeable iff edge > tail cost*) · `DIVERGENT`
  (STRUCTURAL / un-priceable only: boundary mismatch, opposite orientation, outcome-count mismatch, wrong
  event) · `NEEDS_MANUAL` (unextractable; conservative default). Precedence
  `DIVERGENT > NEEDS_MANUAL > TAIL > IDENTICAL`; `IDENTICAL` requires every dimension provably clean
  (no-false-positive, L1 — a false IDENTICAL is a both-legs loss).

Live result: weather 24 IDENTICAL / 36 NEEDS_MANUAL · sports **52 TAIL** (MLB 0.26¢, ATP/WTA/UFC 0.10¢) /
53 NEEDS_MANUAL · econ 13 IDENTICAL · **0 structural DIVERGENT**.

## Alternatives considered

- **Keep binary IDENTICAL/DIVERGENT/NEEDS_MANUAL** — simpler, but hard-excludes ~52 sports pairs over a
  ~0.26¢ priceable tail; contradicts [0010]'s "price the risk, don't binary-exclude."
- **Require identical source strings** (literal 0001) — rejects the World Cup (ESPN vs FIFA) and every
  cross-reporter pair for no settlement reason.
- **Hard-code 2-vs-3-way per league** — brittle (some markets YES/NO, others YES/NO/TIE); structure-discovery
  is robust to new leagues.

## Consequences

- The gate **prices** settlement tail risk (the bot trades a TAIL pair iff its edge clears the quantified
  cost) instead of excluding it — widening the tradeable universe (the breadth the owner pushed for) without
  weakening the both-legs-loss guard (structural conflicts stay DIVERGENT; unknowns stay NEEDS_MANUAL).
- Creates a soccer/World-Cup dependency: `colisted_map` must bind the Tie market (else a soccer pair reads
  2-way-vs-3-way → false DIVERGENT) and [0010]'s `void_haircut` must extend to the `atc-` (soccer) prefix
  before TAIL-pricing applies. Both fail conservative-safe until done.
- The gate is `scripts/settlement_identity.py` (`--audit` runs it over the live universe); wiring it into
  live discovery + the bot trading path is the next step.
