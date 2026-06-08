# 0004 — Ledger decision rule: layer by default, rotate only on a real flip

- **Date:** 2026-06-08
- **Status:** Accepted
- **Deciders:** Project owner + Claude

## Context

When a cross-venue edge reappears in a market we already hold, the bot must choose: **layer** (hold
the existing pair, add the new one) or **rotate** (unwind the existing pair, redeploy into the new
one). `bot/ledger.py` models both and proves two invariants for locked arb books: settlement PnL is
**additive** (sum of entry edges) and **outcome-independent** (same whether YES or NO wins). A locked
arb can also be **mark-to-market negative mid-life yet settle positive** — so MTM is a paper number,
not a reason to act.

## Decision

**Layer by default. Rotate only if** the first tranche's MTM exceeds its locked edge plus round-trip
cost **and** sufficient exit depth exists (i.e. a *genuine flip* where proceeds > $1 > new cost).
**Never unwind at an MTM loss.** Treat a single unhedged leg as the only real loss path — use a later
opportunity to **repair** a naked leg, never to stack onto it.

## Alternatives considered

- **Always rotate to recycle capital** — rejected: locks in a loss whenever the first tranche is
  merely MTM-negative (it still settles positive); only wins when the flip exceeds round-trip cost.
- **Always hold** — rejected: leaves capital tied up through a real flip that could be profitably
  recycled (scenario S3).
- **Act on MTM** — rejected: MTM-negative ≠ a real loss for a held arb (scenario S4).

## Consequences

- Encoded and self-verified in `bot/ledger.py` scenarios S1–S5 (run `python bot/ledger.py`).
- The rotate trigger depends on detecting *real flips*, which is exactly what the event-driven
  persistence monitor ([0003](0003-event-driven-persistence.md)) is built to catch.
- **Leg risk is the named downside:** the live bot's fill management must guarantee both legs or treat
  a one-leg fill as a directional position to repair, not an arb.
- **Revisit if:** fee schedules change (round-trip cost is the rotate threshold) or a venue adds an
  early-redemption mechanism that changes capital-recycling economics.
