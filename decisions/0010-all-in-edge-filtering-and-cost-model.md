# 0010 — Trade-filtering is on the ALL-IN expected edge; the collector logs the cost inputs

- **Date:** 2026-06-09
- **Status:** Accepted
- **Deciders:** owner + Claude

## Context

The hardening pass ([review](../tasks/independent-review-2026-06-09.md), lessons L10/L15) set the rule:
*filter a trade only when its actual edge is ≤ 0.* But the edge the monitor currently filters on —
`net_edge = 1 − yes_ask − no_ask − fees` — is the **quoted, top-of-book, TAKER, fee-net** edge. It includes:

- **exchange fees** (marginal, both venues), and
- **the bid-ask spread you cross** (it uses the *asks you'd pay to lift both offers*, not mid).

It does **not** include:

- **size slippage** beyond the touch (the edge fades as you walk the book — *logged* as `depth`, not in the test),
- **latency / quote-drift** between observing the quote and both orders landing (`age` *measures* staleness; it
  doesn't haircut the edge), or
- **leg-fill failure** — one leg fills, the other doesn't, leaving a naked directional position (the dominant
  real cost; in **no** number yet).

So a quoted-positive arb can be ≤ 0 **all-in**. "Only ≤ 0 is filtered" is true today **only for the quoted
taker-fee-net edge**, which is the *ceiling* on what's realizable.

## Decision

The trade-filtering criterion is the **all-in expected edge**, not the quoted edge:

```
all_in = quoted_touch_edge − slippage(size) − latency_haircut − leg_risk_EV − settlement_void_EV
```

Drop a candidate **only** if `all_in ≤ 0` (at the size you'd trade) or the data is unreliable (stale/crossed =
effectively ≤ 0). Magnitude/liquidity cutoffs stay **opt-in analysis lenses**, never silent filters (L15).

During the **read-only collection phase** this is deliberately **split**:

- the **monitor logs every quoted-positive arb** (`net_edge > 0` on the marginal-fee taker edge) **plus the raw
  inputs** to compute the haircut — `depth` (slippage-vs-size) and `age` (staleness / latency proxy);
- the **all-in haircut is applied in analysis / the future trade-selection layer**, *not* pre-baked into the
  collector — so we never discard real signal on an un-measured latency/leg-risk assumption.

## Costs to model (forward spec — none of these is in the filter yet)

1. **Size slippage** — input exists (`depth`). The trade logic sizes to where the **marginal** (not touch)
   all-in edge stays > 0; the depth metric should also move from the gross-≥2c proxy to a true **net-marginal**
   curve (it currently slightly *over*-counts fillable size, so it over-sizes rather than over-filters).
2. **Latency / quote-drift** — **unmeasured** (review B3). Build the order-ack latency study; subtract a measured
   haircut and/or gate on `age`. `scripts/capital_sim.py --haircut` is the manual stand-in (defaults 0).
3. **Leg-fill failure** — **unmodeled** (review B1, the dominant cost). Model:
   `all_in = P(both fill)·quoted − P(one fills)·E[naked-leg loss]`. One naked-leg loss erases many good arbs.
3b. **Settlement-void divergence (sports)** — **FIRST PASS BUILT 2026-06-09** (review C5,
   [sports-settlement-verification.md](../research/sports-settlement-verification.md)). The venues' postpone/void
   rules diverge — materially for **MLB**: Kalshi waits for a replay only if rescheduled ≤2 days (else voids to a
   fair price), pmus ≤2 weeks (else last-traded) → a replay in that gap breaks the lock into a naked leg.
   `capital_sim.void_haircut()` charges `P(postpone≈1.3%)·P(2d-2wk gap≈0.4)·loss(≈0.5)` (MLB ~0.26c/contract,
   other sports ~0.10c; weather 0). Tunable via `--void-mult`; the `p_gap`/`loss_frac` are ESTIMATES pending data.
   The trade layer must additionally **unwind an MLB pair before Kalshi's 2-day window** (don't hold through a
   postponement); `colisted_map` tags every sports pair `void_clean=False`.
4. **One-sided-book over-strictness — FIXED 2026-06-09.** `make_px` / `signal` / `game_edge` now price **each
   direction on only its two quotes** (the ask where you buy YES + the bid where you buy NO), so a one-sided
   book no longer kills the direction that doesn't use the missing quote. `make_px` returns the partial quad
   when ≥1 direction is fully quoted; the crossed-book check moved into `signal` (a venue is "crossed" only
   when both its touches are present and bid>ask, so a missing touch isn't mistaken for crossed).

## Consequences

- Treat a quoted positive edge as the **ceiling**, not the realizable number, until items 1–3 are built. The
  all-in filter is a **trade-selection deliverable** (`ledger.py` → live), not part of the read-only collector.
- The collector intentionally **over-collects** (logs arbs that may be ≤ 0 all-in) so the analysis layer isn't
  blind to the tail; the cost model assembles the real number. Revisit when the live trade logic is wired.
