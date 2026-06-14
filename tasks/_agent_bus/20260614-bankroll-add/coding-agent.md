---
from: coding-agent
run_id: 20260614-bankroll-add
timestamp: 2026-06-14T08:54:01Z
task_summary: READ-ONLY bankroll-walked backtest netting scale-in/re-entry adds against the $250/$250 separated-wallet two-pool model; reports the DELTA vs base.
files_changed: [scripts/flip_add_backtest.py]
files_created: [scripts/bankroll_add_backtest.py]
context_loaded: [CLAUDE.md, scripts/venue_split_backtest.py, scripts/flip_add_backtest.py, scripts/capital_sim.py, scripts/backtest_current_strategy.py, scripts/velocity_gate_experiment.py, scripts/analyze_persistence.py, tasks/lessons.md, memory/venue-wallets-separated.md, memory/MEMORY.md, agent-memory/coding-agent/MEMORY.md, data/cross-arb/transitions-*.jsonl]
libraries_consulted: []
self_review_artifact: tasks/_agent_bus/20260614-bankroll-add/code-logic-reviewer.md
---

## What I changed
- NEW `scripts/bankroll_add_backtest.py` — composes venue_split's two-pool walk + flip_add's `add_events` into
  ONE causal two-pool walk: base entries + add attempts share the SAME $250/$250 pools; an add deploys only
  if BOTH pools have residual room for >=1 contract of its two legs, else SKIP (counted). 4 scenarios
  (BASE / +true-scale-in / +re-entry / +both) x 2 cohorts (weather-only / all-verified). `bankroll_add_backtest.py:84` (walk),
  `:163` (`_scenario`), `:188` (report).
- `scripts/flip_add_backtest.py:236` — additive: capture `px` in the WIDEN index tuple (now 6-tuple) and
  surface `widen_t` + `widen_px` on each `add_events` output dict (`:268`), so a downstream bankroll walk can
  leg-split the add and time it causally. No existing field/behavior changed; flip_add selftest still green.
- `--selftest` proves: (i) add FUNDED iff BOTH pools have room, SKIPPED when EITHER full; (ii) delta-vs-base =
  funded add profit, cap-0 add -> 0 delta; (iii) true-add vs re-entry routed to their own scenarios; (iv)
  capital frees at settlement (post-settlement add reuses the pool, concurrent one skipped); (v) leg_split
  [L28] single-letter binary dir splits correctly; (vi) the [L18] crowd-out property (an in-window add inflates
  dReal above the honest dTOTAL when it displaces an out-of-window base pair).

## Why (non-obvious only)
- The report leads with **dTOTAL (realized+unrealized)**, not realized-only. On all-verified, the one funded
  re-entry add shows dReal +$8.22 but dTOTAL -$0.34: it settles in-window and crowds out base pairs that
  settle out-of-window, so the realized-only gain is a 5-day-window artifact ([L18]), not new edge. dTOTAL is
  window-invariant and is the decision-useful marginal figure. This was the single biggest correction the
  Self-Review Pass forced.
- My BASE-only walk is bit-for-bit identical to `venue_split.two_pool_walk` on both cohorts (realized,
  unrealized, entered, both peaks) — verified — so the delta is measured against the TRUE proven reference.
  I rewrote the walk as a unified stream (to interleave base+add causally) but it reuses venue_split's
  `leg_split` / `SETTLE_OFFSET_H` / `settle_t` / cost+trapezoid economics / fat-edge haircut verbatim.

## Headline numbers (PAPER/GROSS, effective-n tiny, 5.1d, sports/econ settlement UNVERIFIED)
- WEATHER-ONLY (tradeable today): base $7.04 real / $12.85 total (+2.57%); marginal dTOTAL — true +0.00 |
  re-entry +0.04 | both +0.04. Adds: 1 FUNDED / 0 skipped of 1.
- ALL-VERIFIED (post-recon preview): base $30.56 real / $41.17 total (+8.23%); marginal dTOTAL — true +0.00 |
  re-entry -0.34 | both -0.34. Adds: 1 FUNDED / 3 SKIPPED-for-capital of 4 (pools hit 100%).
- Bottom line: bankroll-netted, adds are worth ~$0 (the base strategy already saturates both $250 pools), far
  below flip_add's unconstrained per-market UB ($0.04 weather / $10.88 all-verified).
