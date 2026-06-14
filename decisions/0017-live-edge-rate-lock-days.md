# 0017 — Live edge-RATE reservation + corrected days-to-grade lock-days (without un-freezing 0014-H2)

- **Date:** 2026-06-13
- **Status:** Accepted
- **Deciders:** owner (chose "edge-RATE allocation" as the session's work), Claude (design + freeze-preserving implementation)

## Context

`bot-rs` evaluated each arb in isolation and fired FIFO; the only value filter was the **flat**
`edge_floor_cents`. A flat floor inverts capital efficiency when the bankroll binds: a 13¢ econ arb that
locks capital ~21 days = **0.6¢/$-day** is a *worse* use of capital than a 3¢ weather arb at 1.2 days =
**2.5¢/$-day**, yet the flat floor prefers the econ one. The pre-registered **0014-H2** arm
(`booked_edge ÷ expected_lock_days`) exists precisely to fix this; it was the last deferred `bot-rs` code
feature (todo "NEXT SESSION").

Two facts forced the shape of this decision:

1. **The lock-day priors were corrected *after* 0014 froze.** 0014-H2 froze its denominator at
   **weather 1.2 / sports 15 / econ days-to-release** (matching `capital_velocity.py::LOCKUP_PASSIVE`,
   "hold to the pmus endDate"). The owner's **2026-06-11** correction (CLAUDE.md) showed that model
   conflated *can't-sell-early* (the book IS frozen at resolution — measured) with *cash-locked*
   (assumed): pmus credits cash **at grade**, so capital is really locked **entry → grade ≈ days-to-event**,
   not ~15 d. Sports/econ are therefore much faster than the frozen prior says, and the right denominator
   is **dynamic** (a game today locks ~1 d; a game in 7 d locks ~7 d), not a flat 15.

2. **0014 is a pre-registered confirmatory test (L19).** Its consequences forbid silently re-tuning the
   frozen text; the whole value of the multi-week test is that the rule was fixed before the data existed.

## Decision

Ship the edge-RATE layer **in the live bot** on the **corrected days-to-grade** lock-days, while leaving
0014-H2's frozen backtest priors **untouched**:

- **Live lock-days model** (`risk::lock_days`, documented consts): **weather 1.2** (floor; settles
  ~same-evening, `days_to_event` is 0/None for weather); **sports = live `days_to_event`** floored at 0.4
  (dynamic — the correction); **econ = 21-day fallback** because `days_to_event` is `None` for econ today
  (no release calendar in the bot). Always finite & > 0 (a `None`/NaN/±inf collapses to the category
  prior), so `edge_rate = booked_edge ÷ lock_days` can never divide by zero or go non-finite.
- **Reservation floor, not batch ranking** (`Reject::BelowEdgeRateFloor`, config `MIN_EDGE_RATE_CPD`,
  filed at **0 = OFF**; **ENABLED at 1.0 on 2026-06-13** by owner directive — a conservative floor below
  the fast-category rates (weather ≥1.7, sports ≥4) so it only cuts genuinely-slow arbs, inert on current
  data, uncalibrated; `edge_rate` now logged on every live ENTRY for later calibration): skip arbs whose
  edge-rate is below the threshold — the online form of H2's
  "reserve capital for higher-rate arbs." `edge_rate` is **always computed and returned in `Approved`**
  (logged) so the owner can calibrate the threshold against the live opportunity distribution; the gate
  itself changes nothing until deliberately enabled. **Sizing is untouched** (H2 is reservation-only;
  H1's caps own sizing).
- **0014-H2 stays frozen.** The live bot using corrected (dynamic) lock-days does **not** rewrite the
  confirmatory backtest's frozen priors. Which lock-days the *confirmatory backtest* runs on when the
  multi-week data arrives is deferred to that point as a **labelled sensitivity arm** (per 0014 §1's
  "any post-epoch measured lock-day update is a labelled arm"), not a silent edit. This is a prior
  correction from a direct account observation, **not** a change motivated by the multi-week data, so it
  does not invalidate the confirmatory bit.

## Alternatives considered

- **Un-freeze 0014-H2 and set sports = dynamic there too** — cleanest single source of truth, but it
  rewrites a pre-registered test before its data exists; rejected per L19. The live bot and the backtest
  are allowed to differ — the backtest validates the *policy class*, the live bot runs the best-current
  parameterization.
- **Batch ranking (buffer frames, sort by edge-rate, fire the best)** — true cross-arb ranking, but it
  adds latency on a path where ~29% of edges die <250 ms (probe #2), and capital binds at ~10 positions
  then waits for settlement regardless, so the online value is low. The reservation floor captures the
  benefit without the latency tax.
- **Rate-aware sizing (deploy more into high-rate arbs)** — increases concentration; out of scope and
  not what H2 specifies (it is a reservation/ordering rule).
- **Compute a real econ release date** — needs a BLS/BEA release calendar the bot doesn't have; the 21-day
  fallback ranks econ correctly (last) without fabricating per-release precision. Logged as a follow-up.

## Consequences

- The bot **can** reserve capital for high-velocity arbs once the owner sets `MIN_EDGE_RATE_CPD`;
  until then, **zero behavior change** (default off, proven by a regression test).
- **Sports lock-days are now dynamic** — a near-game arb correctly outranks a far-game one (smoke: a
  1-day-out sports arb = 3.0¢/$-day, above weather's 2.5).
- Creates a documented **divergence between the live lock-days and the frozen 0014-H2 priors**; anyone
  running the confirmatory backtest must treat the corrected lock-days as a labelled sensitivity arm, not
  the frozen primary.
- **Follow-up:** econ uses a 21-day fallback because `days_to_event` is `None` for econ (discovery
  computes no release date). A real per-release horizon needs a release-calendar feed — until then econ
  edge-rate is a category-constant approximation, fine for ranking econ last but not for intra-econ
  ordering.
- **Follow-up:** `edge_rate` is computed + returned in `Approved` + printed in the report/smoke path, but
  the live approve path doesn't yet thread it into the persisted order record (it passes `cost_per`, not
  `edge_rate`, to `spawn_submit`). Wiring it through is the small step that turns the metric into the live
  opportunity-distribution data the owner needs to **calibrate `MIN_EDGE_RATE_CPD`** — do this before
  enabling the gate, so the threshold is set from measured rates, not guessed.
