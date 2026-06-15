---
from: coding-agent
run_id: 20260615-naked-leg-multileg
timestamp: 2026-06-15T09:53:40Z
task_summary: Generalize naked-leg recovery to flatten EVERY leg with a live fill (not just one), so a Kalshi second-leg partial can't be abandoned naked.
files_changed: [bot-rs/src/bookkeeping.rs]
files_created: []
context_loaded: [CLAUDE.md, ~/.claude/agent-memory/coding-agent/MEMORY.md, bot-rs/src/bookkeeping.rs, bot-rs/src/exec.rs, bot-rs/src/pricing.rs, bot-rs/src/types.rs, bot-rs/src/book.rs, bot-rs/src/test_support.rs, bot-rs/src/postpone.rs, (working-tree git diff)]
libraries_consulted: []
self_review_artifact: tasks/_agent_bus/20260615-naked-leg-multileg/code-logic-reviewer.md
---

## What I changed
- Added `naked_filled_indices(&PairAck) -> Vec<usize>` (bookkeeping.rs ~344): every leg with a live `fill_qty>0` (`!simulated`), not just one. Single-leg case = `[i]`; both-partial = `[0,1]`.
- Rewrote `recover_naked_leg` (bookkeeping.rs ~360-500) to recover ALL naked legs: per-leg ambiguity fail-close (an unfilled NON-naked leg that ERR'd with unknown fate halts; `HedgeNotFilled` excepted), then ATOMIC LAUNCH — price every naked leg's flatten SELL BEFORE firing any; if any is unpriceable, return false (halt) and fire NOTHING; otherwise mark `flattening` once and spawn one SELL per naked leg, each sized to its OWN `fill_qty` via `frac_qty`.
- Kept `naked_filled_idx` (singular) untouched — still used by `apply_outcome`'s log string and `naked_leg_failclose`'s backstop halt (both only need "a" naked index; both still resolve correctly in the both-partial case).
- Added 2 tests: `both_legs_partial_recovers_each_sized_to_own_fill_qty` (pmus FULL 10 + Kalshi PARTIAL 7 -> two SELLs, sized 10 and 7) and `both_legs_partial_one_unpriceable_fails_closed_fires_nothing` (Kalshi leg unpriceable -> halt, zero SELLs, slug not marked flattening).

## Why (non-obvious only)
- ATOMIC LAUNCH (price-all-then-fire) is the multi-leg fail-close: pricing one leg, firing its SELL, then discovering the second leg is unpriceable would leave the second abandoned — the exact bug. Pricing both first makes a half-recovery impossible.
- The two recovery SELLs share one per-slug `flattening` slot intentionally: the `SubmitKind::Recovery` outcome arm halts independently per SELL that fails to fill, so an abandoned partial still surfaces as a halt; the second `flattening.remove` is a harmless no-op.

## Verification
- `cargo test`: 169 passed / 0 failed (was 167; +2 new). Prior pmus-only-partial test still green.
- `cargo clippy --all-targets`: clean, no warnings.
- Did NOT commit (per instruction).
