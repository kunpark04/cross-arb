---
from: coding-agent
run_id: 20260614-scalein-reentry
timestamp: 2026-06-14T00:00:00Z
task_summary: Fix W-1 silent-naked-leg defect — recovery must fail-CLOSE when an UNWIND (not a recovery for this leg) holds the slug's flatten slot.
files_changed: [bot-rs/src/main.rs]
files_created: []
context_loaded: [tasks/_agent_bus/20260614-scalein-reentry/independent-review.md, tasks/scale-in-reentry-design.md, bot-rs/README.md, bot-rs/src/main.rs, bot-rs/src/postpone.rs]
libraries_consulted: []
self_review_artifact: tasks/_agent_bus/20260614-scalein-reentry/code-logic-reviewer-w1fix.md
---

## What I changed
- `bot-rs/src/main.rs` — new `FlatKind { Recovery, Unwind }` enum (next to `SubmitKind`): records WHAT holds a slug's in-flight flatten slot.
- `flattening` changed `HashSet<String>` → `HashMap<String, FlatKind>` (decl `main.rs:~305`); every touch migrated:
  - main-loop in-flight guard `main.rs:~545` `.contains` → `.contains_key` (any flatten still blocks a new entry/add — semantics unchanged).
  - `spawn_unwind` `main.rs:~1066` dedup `.contains` → `.contains_key`; insert now `FlatKind::Unwind`.
  - `recover_naked_leg` insert now `FlatKind::Recovery`.
- THE FIX — `recover_naked_leg` `main.rs:~947`: the bare `if flattening.contains(slug) { return true }` (always "already covered") became a `match`:
  - `Some(FlatKind::Recovery)` → `return true` (this slug's own recovery is genuinely flattening this leg).
  - `Some(FlatKind::Unwind)` → `return false` → caller fail-CLOSES (halt): an unwind covers only `legs[0]`, NOT a freshly-naked add leg, so it is NOT covered.
  - `None` → fire the recovery.
- Regression test `w1_halffilled_add_while_unwind_in_flight_fails_closed` (`main.rs`, end of the SCALE-IN test block): the reviewer's R4-A repro with the assertion flipped to `halt==true`; also asserts the unwind slot is not clobbered, no second flatten spawned, plus two direct-call branch checks (recovery-held → true; unwind-held → false).
- Migrated all existing test-side `flat`/`flat2` `HashSet` decls + `.contains`/`.insert` to the `HashMap<String, FlatKind>` shape (kind tagged per the scenario each test models: Unwind for the unwind/postpone tests, Recovery for the recovery tests).

## Why (non-obvious only)
- A second flatten is deliberately NOT fired in the unwind-held case: `flattening` is per-slug and a competing recovery SELL would race the unwind's pop of `legs[0]` through the single slot. Halt is the correct fail-safe (the design's R4 + the reviewer's option (a)+(c)).
- The regression test makes the add leg's book PRICEABLE on purpose — to prove the halt is the *intended* fail-close for the race, not an artifact of an unpriceable book.

## Verification
- 147 tests pass (146 baseline + 1 new W-1 regression), 0 failed.
- `cargo clippy --all-targets` clean.
- `cargo run -- --smoke` at defaults: no `ADD-TO-HELD ARMED` banner, all APPROVED/REJECTED/DRY-RUN lines byte-identical to before (the change is inert at the safe defaults — `recover_naked_leg`'s new branch is only reachable on a LIVE naked leg, impossible in dry-run).
