# bot/ — accounting core + (future) live bot

## `ledger.py` — cross-venue position ledger + PnL simulator

The bot's accounting core for **one** binary market with **identical settlement on both venues**
(P = polymarket.us, K = Kalshi). A cross-venue arb entry buys YES on one venue + NO on the other (a
complementary pair); if the pair costs < $1, the difference is locked. It tracks holdings per venue,
cash, and cost basis, and models the layer / rotate / hold / leg-risk decisions.

Run it — it's a **self-verifying harness** (no network):

```bash
python bot/ledger.py
```

It walks 5 scenarios (S1–S5) and asserts the invariants hold.

### Invariants it demonstrates + asserts

1. **Additive** — settlement PnL of a set of held arb pairs == sum of each entry's net edge.
2. **Outcome-independent** — for a balanced book, settlement PnL is identical whether YES or NO wins.
3. **Hold, don't panic-unwind** — a locked arb can be mark-to-market *negative* mid-life yet settle
   *positive*. Never unwind at an MTM loss; hold + layer. A genuine **flip** (proceeds > $1 > cost) is
   the only case where early rotation beats holding.
4. **Leg risk is the real downside** — if only one leg fills, the position is directional, not a
   locked arb, and can settle negative. A later opportunity is used to *repair* the naked leg, not to stack.

### Decision rule (encoded in the scenarios)

> **Layer by default. Rotate only if** the first tranche's MTM exceeds its locked edge plus round-trip
> cost **and** exit depth exists. Never unwind at an MTM loss. A single unhedged leg is the only real
> loss path.

Rationale recorded in [decisions/0004](../decisions/0004-ledger-layer-by-default.md).

## Fee models

- **Kalshi** taker: `ceil(0.07 · N · P·(1−P))` per contract (maker = 0.25×).
- **polymarket.us** taker: `0.05 · P·(1−P)` per contract (maker = 0).

## Roadmap (not built — read-only phase)

`tasks/todo.md` item: wire `ledger.py` to live `scan_all.py` signals — trade selection, capital
allocation, per-market layer/rotate decisions, and leg-risk fill management. Gated on the persistence
study (`tasks/todo.md` #10) and an explicit move out of the read-only phase.
