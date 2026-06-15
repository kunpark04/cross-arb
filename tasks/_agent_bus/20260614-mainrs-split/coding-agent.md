---
from: coding-agent
run_id: 20260614-mainrs-split
timestamp: 2026-06-14T21:01:33Z
task_summary: Mechanically split the 2970-line bot-rs/src/main.rs monolith into 7 focused modules (pair/pricing/bookkeeping/refresh/smoke/live/test_support) with ZERO behavior change.
files_changed: [bot-rs/src/main.rs]
files_created:
  - bot-rs/src/pair.rs
  - bot-rs/src/pricing.rs
  - bot-rs/src/bookkeeping.rs
  - bot-rs/src/refresh.rs
  - bot-rs/src/smoke.rs
  - bot-rs/src/live.rs
  - bot-rs/src/test_support.rs
context_loaded:
  - bot-rs/src/main.rs
  - bot-rs/README.md
  - bot-rs/src/types.rs
  - CLAUDE.md
  - C:/Users/kunpa/.claude/agent-memory/coding-agent/ (pattern_* + feedback_* index)
libraries_consulted: []
self_review_artifact: tasks/_agent_bus/20260614-mainrs-split/code-logic-reviewer.md
---

## What I changed
- Split `main.rs` (2970 lines) into 7 new sibling modules under `bot-rs/src/`; `main.rs` now holds only the crate doc header, `#![allow(dead_code)]`, all 19 `mod` decls + `#[cfg(test)] mod test_support`, the `use` lines, `fn main`, `fn banner` (`bot-rs/src/main.rs:142` total).
- `pair.rs` — `LivePair`/`PairState`/`SubmitKind`/`FlatKind`/`SubmitOutcome` + `lock`/`pair_tickers`; struct fields made `pub(crate)` per the cross-module access requirement.
- `pricing.rs` — pure leg planning/pricing/exit pricing (`plan_legs`, `build_legs`, `cents`, `quantize_to_tick`, `affordable`, `realized_edge_clears_floor`, `position_from_intents`, `exit_price`, `flatten_exit_cents`, `unwind_exit_cents`, `round4`, `TICK_EPS`). `PlannedLeg` kept fully private.
- `bookkeeping.rs` — exposure/position/submission/recovery/unwind (`reserve_exposure`, `subtract_exposure`, `track_position`, `qualifying_add`, `apply_outcome`, `naked_filled_idx`, `recover_naked_leg`, `spawn_submit`/`spawn_flatten`/`spawn_cancel`/`spawn_unwind`, `naked_leg_failclose`).
- `refresh.rs` — `report_coverage`, `diff_targets`, `prune_step`, `refresh_loop`.
- `smoke.rs` — `smoke`, `report`, `fire_legs`.
- `live.rs` — `run_live` (367-line event loop), `poll_opt`, `supervise_fatal`, `led_by_from_prior`.
- `test_support.rs` — the 3 cross-module fixtures `q_pk`/`wx_pair`/`wc_pair` as `pub(crate) fn`; the 39 tests were distributed into each owning module's `#[cfg(test)] mod tests` exactly per the task partition (pair 2 / pricing 11 / bookkeeping 24 / refresh 2).

## Why (non-obvious only)
- One unavoidable token change: `smoke.rs:122-126` rewrites `types::Position` / `types::PositionLeg` (×3) to `crate::types::Position` / `crate::types::PositionLeg`. At the crate root `types::` resolved; in a submodule the path qualifier must be `crate::`. This is a path-relocation mechanic (the same import was already glob-imported as `use crate::types::*`), not a logic/comment/constant change. Every other type reference in the moved code uses the unqualified glob form and is byte-identical.

## Gate evidence
- `cargo test` -> `test result: ok. 147 passed; 0 failed; 0 ignored` (locked baseline matched).
- `cargo clippy --all-targets` -> exit 0, `Finished` with no warnings/errors.
- Smoke: program output SHA-256 `02f6f9d5...` identical to the reference's program portion; the only `diff` vs `/tmp/smoke_phase1.txt` is its 3 leading cargo cold-compile lines (`Compiling`/`Finished in 19.06s`/`Running`) which a warm `cargo build && cargo run` does not re-emit and which contain a non-reproducible wall-clock timestamp. The 41 program lines are byte-identical.
- `git status --short` for source: `M bot-rs/src/main.rs` + the 7 `?? bot-rs/src/*.rs`; all 13 forbidden modules + Cargo.toml show empty `git diff` (untouched).
