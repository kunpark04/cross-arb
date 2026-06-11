---
from: coding-agent
run_id: 20260611-tick-fix
timestamp: 2026-06-11T21:02:07Z
task_summary: Make price->tick rounding direction-aware (BUY ceils / SELL floors) so a marketable order can't be placed at a resting limit (W1), and floor-quantize the recovery SELL to the held pmus leg's tick (W2).
files_changed: [bot-rs/src/main.rs, bot-rs/src/types.rs, bot-rs/README.md]
files_created: []
context_loaded: [bot-rs/README.md, tasks/lessons.md, C:/Users/kunpa/.claude/agent-memory/coding-agent/MEMORY.md, bot-rs/src/main.rs, bot-rs/src/types.rs, bot-rs/src/exec.rs, CLAUDE.md, memory/MEMORY.md]
libraries_consulted: []
self_review_artifact: tasks/_agent_bus/20260611-tick-fix/code-logic-reviewer.md
---

## What I changed
- W1: `cents()` (main.rs:~1345) + `quantize_to_tick()` (~1328) now take an `Action` and round TOWARD-MARKETABLE — BUY ceils, SELL floors. Callers threaded: `build_legs` (entries = Buy), `unwind_exit_cents` (Sell), `flatten_exit_cents` (Sell).
- W1 float-noise guard (L10 class): added `TICK_EPS=1e-6` so an exact-cent/exact-tick value (e.g. `0.07*100 = 7.0000000001`) snaps onto the boundary before ceil/floor instead of jumping a whole cent. Without it a BUY at book 0.07 ceil'd to 8c. EPS << half a tick, so it never crosses a real boundary.
- W2: added `pm_min_tick: Option<f64>` to `PositionLeg` (types.rs); populated ONLY on the pmus leg in `position_from_intents` (now takes `pm_tick`); extracted `flatten_exit_cents()` (sibling of `exit_price`) that FLOOR-quantizes the recovery SELL to that tick, and `recover_naked_leg` calls it. Stale TODO at the old recovery-pricing site removed (W2 resolves it).
- `PositionLeg` derive dropped `Eq` (kept `PartialEq`) — `f64` has no `Eq`; nothing required `Eq` (only `assert_eq!` + the non-`Eq` `Position`).
- README: documented direction-aware rounding + the recovery-SELL tick floor; added the FLAG (whole-cent `price_cents: u8` is coarser than pmus 0.001 — a bounded <=0.5c/leg COST, dormant today, finer-price TODO before pmus is armed past dry-run).
- Tests: rewrote `cents_validates_tick_range` -> `cents_rounds_toward_marketable` (BUY 0.074->8c, SELL 0.076->7c, exact-cent stability); updated the FIX-C tick test (BUY 0.07 on a 0.05 tick now ceils to 10c); added `flatten_sell_floors_to_the_pmus_tick` (pure) + `coarse_tick_pmus_leg_still_recovers` (end-to-end). 123 -> 125 tests, all green; clippy clean.

## Why (non-obvious only)
- The ceil-for-BUY is safe because `realized_edge_clears_floor` runs AFTER `build_legs` on the ceil'd prices (main.rs:505 then 509), so a BUY rounded up that erodes the edge below the floor is rejected before firing.
- `flatten_exit_cents` was extracted (not inlined) so the W2 floor-to-tick is directly unit-testable — the dry-run backend doesn't echo the SELL price, so a behavioral-only test couldn't assert the floored cents. Mirrors the existing `exit_price`/`unwind_exit_cents` pure-helper pattern.
