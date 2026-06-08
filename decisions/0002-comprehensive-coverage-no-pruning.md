# 0002 — Comprehensive coverage: the scanner prunes nothing

- **Date:** 2026-06-08
- **Status:** Accepted
- **Deciders:** Project owner + Claude

## Context

Early scans dropped markets that showed "not enough edge" right now (e.g. the deep, efficient
tennis/UFC/ITF books at $0 cross-venue). That bakes a trading decision into the measurement layer and
loses the data needed to study how edges *appear and move* over a day. The goal of this phase is to
know the **whole** co-listed universe and its dynamics, not to pre-select winners.

## Decision

**Keep every co-listed market in scope and compute the same metrics (net edge, fillable size, $) for
all of them.** "Not enough edge" is the *live bot's* call at trade time, not the scanner's. The
scanner measures; it does not prune.

## Alternatives considered

- **Filter to currently-profitable markets** — rejected: discards the efficient-market baseline and
  the time-series needed to see edges open/flip/fade; conflates measurement with trading policy.
- **Sample a subset for cost** — rejected at this scale (~200 co-listed markets is cheap to sweep);
  if sampling ever becomes necessary, it must be logged, not silent.

## Consequences

- `scripts/scan_all.py` retains the full universe (weather 5 cities + ~12 sports leagues, ~200
  markets) with uniform metrics → `_data/scan_all.json`.
- Provides the complete input for the persistence study ([0003](0003-event-driven-persistence.md))
  and, later, the bot's trade-selection layer.
- **Revisit if:** the co-listed universe grows large enough that a full sweep strains REST limits —
  then move to event-driven coverage (already the persistence-layer direction), still without
  dropping markets from *scope*.
