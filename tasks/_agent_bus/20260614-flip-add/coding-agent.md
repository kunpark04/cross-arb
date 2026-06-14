---
from: coding-agent
run_id: 20260614-flip-add
timestamp: 2026-06-14T07:45:10Z
task_summary: READ-ONLY feasibility backtest (scripts/flip_add_backtest.py) measuring whether the unbuilt FLIP (close+reverse) and ADD (scale-in) strategy extensions would add profit on the historical persistence data.
files_changed: []
files_created: [scripts/flip_add_backtest.py, tasks/_agent_bus/20260614-flip-add/coding-agent.md, tasks/_agent_bus/20260614-flip-add/code-logic-reviewer.md]
context_loaded: [CLAUDE.md, tasks/lessons.md, scripts/capital_sim.py, bot/ledger.py, scripts/analyze_persistence.py, bot/monitor.py, bot-rs/src/main.rs (re-entry block), scripts/README.md, "data/cross-arb real transition records (06-08..07-02)"]
libraries_consulted: []
self_review_artifact: tasks/_agent_bus/20260614-flip-add/code-logic-reviewer.md
---

## What I changed
- New `scripts/flip_add_backtest.py` — reuses `load/build_episodes/category` (analyze_persistence) + `capturable/one_per_market/settle_t/void_haircut/DEPTH_BOUNDARY_NET` (capital_sim) + `signal/pfee/kfee/Ledger` (ledger) + `game_edge` (monitor). No re-implementation of the cohort or economics ([L28]).
- FLIP detection (`flip_events`): per phantom-filtered filled arb (capturable one-per-market), scans logged FLIP records over the HELD POSITION's lifetime `[open_t, settle_t]` for an OPPOSITE-direction bigger arb (`rdir != dir0` AND reverse-net − orig-net ≥ tau_gain).
- FLIP close mark (`close_pnl_weather`): reuses `Ledger.enter` + `unwind_all` so fees/signs are pinned to the bot's accounting core; close priced from the REAL flip-time touch prices in the record `px`, returns None (never fabricated) if a sell-leg touch is missing. `close_pnl_game` is a documented stub returning None — the sports `px` logs only the two Kalshi team YES *asks*, never the Kalshi YES *bid* needed to mark a Kalshi-backed leg's close.
- ADD detection (`add_events`): per filled arb, the FIRST (causal, non-oracle [L19]) same-direction WIDEN over `[open_t, settle_t]` beating orig-net by ≥ tau_gain, sized by the widen's crossable `c2` capped at `--add-cap-frac` of the base clip; reports incremental PnL + the correlated-settlement concentration.
- `--selftest` proves: close-mark == Ledger cash; flip detected & same-dir widen excluded; same-dir reverse (rdir==dir0) excluded; opposite-dir widen excluded from adds; tau_gain gate; sports close = None.

## Why (non-obvious only)
- Field shapes verified against real records FIRST ([L28]): two distinct `px` shapes — weather/econ MarketTracker `{p_yb,p_ya,k_yb,k_ya}` dir `P`/`K` (SINGLE-letter — the L28 trap), sports GameTracker `{pm_b,pm_a,ka,kb}` dir `PK`/`KP`. Any touch can be `None` (`k_yb:null` seen live) — handled.
- The held position spans to SETTLEMENT, not to the first edge-CLOSE — a logged CLOSE means the EDGE left the book, not that the locked pair was sold. Two CRITICAL bugs found in self-review and fixed: (1) scoping flips to `[open_t, episode close_t]` missed 65/68 logged flips (they land in LATER edge episodes of the still-held market); (2) without an `rdir != dir0` guard, 4/14 "flips" were same-direction bigger arbs (the ADD path), not cross-overs. Both self-resolved within run; recorded in the review artifact.
- The episode's `dir` is mutated to the post-flip direction by `build_episodes`, so the ENTRY direction is taken from the OPEN record, not `ep["dir"]` (using the mutated dir marked the close on the wrong leg).

## Result (PAPER/GROSS, span 5.08 d, span<4d so PRELIMINARY)
- FLIP base rate: weather 0/101, sports 10/604 (1.7%), econ 0/4. Weather+econ log ZERO FLIP transitions at any size (1:1 bucket basis doesn't cross — matches the prior 0/14 station-days). Overall 1.4%.
- FLIP PnL: 0 of 10 flips have a computable close mark (all sports → Kalshi YES bid not logged). Partial (reverse leg only, close excluded, a LOWER bound): sports orig-net avg +0.45c vs reverse-arb net avg +5.81c → +5.36c/arb plus an unknown positive close gain.
- ADD PnL: 348 markets offered a same-dir bigger arb while held; incremental ~$135 paper-gross over single-entry (mostly sports), at 20%/pair cap. Concentration flagged.

## Verification
- `flip_add_backtest.py --selftest` passes; upstream `analyze_persistence/capital_sim/ledger` selftests still pass (import-only, no upstream edits).
- Phantom filters confirmed applied: 0 restart-censored leaked into filled, 0 c2<1, econ [L21] quarantine fired (36 records). Filter removed 121 markets (830→709).
