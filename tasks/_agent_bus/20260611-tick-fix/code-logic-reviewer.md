---
from: coding-agent (self-review pass)
run_id: 20260611-tick-fix
timestamp: 2026-06-11T21:02:07Z
scope_reviewed: [bot-rs/src/main.rs:1328-1359 (cents/quantize_to_tick), bot-rs/src/main.rs:1290-1318 (build_legs), bot-rs/src/main.rs:770-842 (recover_naked_leg), bot-rs/src/main.rs:1410-1418 (flatten_exit_cents), bot-rs/src/main.rs:1376-1397 (position_from_intents), bot-rs/src/types.rs:148-162 (PositionLeg)]
critical_count: 0
warn_count: 0
info_count: 2
launch_recommendation: PROCEED
self_review: true
cross_references: []
---

## Goal Understanding
Round a marketable order's limit TOWARD-MARKETABLE so it still crosses: a BUY ceils to the cent/tick (limit >= touch), a SELL floors (limit <= touch). Nearest-rounding could place a marketable order at a resting limit -> the leg rests -> the sibling goes naked -> recovery/halt. W1 = the entry-leg + unwind price path; W2 = thread the pmus tick onto the held leg so the recovery SELL floor-quantizes to a valid tick (else a coarse-tick pmus market rejects the flatten -> needless halt). Both dormant today (all live pmus ticks = 0.001, where whole cents are valid multiples) but wrong.

## Scope Reviewed
- main.rs `cents` / `quantize_to_tick` — direction-aware rounding + L10 float-noise epsilon.
- main.rs `build_legs` — entry legs (Buy) thread `Action::Buy`.
- main.rs `unwind_exit_cents` / `flatten_exit_cents` — SELLs thread `Action::Sell`; the recovery flatten floors to `PositionLeg::pm_min_tick`.
- main.rs `position_from_intents` — records the pmus tick onto the pmus leg only.
- types.rs `PositionLeg` — new `Option<f64>` field; `Eq` dropped (kept `PartialEq`).

## Findings

### CRITICAL (must fix before launch)
- None.

### WARN (fix or justify)
- None.

### INFO (optional improvements / simplifications)
- `flatten_exit_cents` and `unwind_exit_cents` now both compute a SELL-priced cent from a leg+book, but `unwind_exit_cents` does NOT apply the pmus tick floor (it predates W2 and uses `cents(exit_price(...), Sell)` directly). For weather/econ/Kalshi (no pmus tick) they're equivalent; a coarse-tick pmus market's UNWIND SELL (postponement path) would still skip the tick floor. Out of scope for this task (W2 names only the recovery SELL), and harmless today (no coarse-tick pmus markets live), but a natural follow-up is to route `unwind_exit_cents` through `flatten_exit_cents` so the postpone-unwind gets the same tick floor. Flagged, not fixed.
- The whole-cent `price_cents: u8` precision FLAG is the task's explicit non-fix; left as a TODO + README note as instructed.

## Checks Passed
- **Ordering of the economics re-check**: `build_legs` (ceil'd prices) runs at main.rs:505 BEFORE `realized_edge_clears_floor` at :509, which reads the ceil'd `price_cents` — so a BUY rounded UP that erodes net below the floor is rejected pre-fire. The ceil-for-BUY safety claim holds.
- **NO-leg direction**: a NO leg is an `Action::Buy` priced at `1 - yes_bid`; ceiling it UP is correctly toward-marketable for buying NO. The `realized_edge_clears_floor` cost sums both ceil'd legs = worst-case (highest) cost. Conservative.
- **Float-noise boundary (L10)**: verified `cents(0.07, Buy) = 7` (not 8) and `quantize_to_tick(0.10, 0.05, Buy) = 0.10` (not 0.15) under the 1e-6 epsilon; the epsilon (1e-6 cents/ticks) is ~7 orders of magnitude above the observed noise (~1e-13) and ~6 below half a tick, so it snaps true boundaries and never crosses a real one. Direct unit test asserts both directions + exact-cent stability.
- **SELL never rounds above the touch**: `floor(cx + 1e-6)` with `cx=7.6` -> 7 (<= touch); a noisy `cx=7.9999999` -> 8, but that value IS ~8c, so flooring to 8c is still ON the touch (marketable). No over-round.
- **Tick recorded on the right leg, index-agnostic**: `position_from_intents` assigns `pm_tick` iff `legs[i].venue == Pmus` — correct for sports PK (pmus = leg0), sports KP (pmus = leg1), and weather/econ. Asserted in `coarse_tick_pmus_leg_still_recovers` (leg0 = Some(0.05), leg1 = None).
- **Fail-safe preserved**: `flatten_exit_cents` returns `None` on a one-sided book OR a sub-1c/over-99c floor -> `recover_naked_leg` returns false -> halt backstop. No path fires an invalid/0c SELL; no gate weakened; dry-run default untouched.
- **`Eq` removal blast radius**: grep confirms `PositionLeg` is used only in `assert_eq!` (needs `PartialEq`) and inside the non-`Eq` `Position` — dropping `Eq` compiles and changes no behavior.
- **Dormancy**: with tick=0.001, `quantize_to_tick` is a verified no-op on whole cents and `cents` is unchanged for whole-cent inputs -> live behavior on today's 0.001-tick pmus markets is identical (the fix only bites on a hypothetical coarse tick / a genuinely sub-cent book price).
- **Build/test/lint**: 125 tests pass (123 baseline + 2 new); clippy `--all-targets` clean.

## Launch Recommendation
PROCEED — the change is correctness-only, dormant on live data, fail-safe-preserving, and fully covered by direction + float-boundary + end-to-end recovery tests.

## Self-review caveat
This is an author self-review (authorship bias applies). The change is low-risk (dormant today, no gate weakened), so independent review is not strictly required — but it touches the live-money recovery path, so if this feeds an armed pmus pilot, an independent reviewer should confirm the INFO follow-up (unwind SELL also wanting the tick floor) before a coarse-tick pmus market is ever traded.
