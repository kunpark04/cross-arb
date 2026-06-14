---
from: coding-agent
run_id: 20260614-scalein-reentry
timestamp: 2026-06-14T09:41:00Z
task_summary: Add SCALE-IN + RE-ENTRY (multi-position-per-slug) to bot-rs per tasks/scale-in-reentry-design.md — N:1 positions per slug with exact per-position exposure release (R1).
files_changed: [bot-rs/src/config.rs, bot-rs/src/postpone.rs, bot-rs/src/main.rs, bot-rs/src/risk.rs, bot-rs/src/exec.rs]
files_created: []
context_loaded: [bot-rs/CLAUDE.md(project root), C:/Users/kunpa/.claude/CLAUDE.md, C:/Users/kunpa/.claude/agent-memory/coding-agent/MEMORY.md, tasks/scale-in-reentry-design.md, bot-rs/README.md, tasks/lessons.md, .claude/projects/.../memory/MEMORY.md, bot-rs/src/main.rs, bot-rs/src/postpone.rs, bot-rs/src/config.rs, bot-rs/src/risk.rs, bot-rs/src/types.rs, bot-rs/src/unwind.rs, bot-rs/src/exec.rs]
libraries_consulted: []
self_review_artifact: tasks/_agent_bus/20260614-scalein-reentry/code-logic-reviewer.md
---

## What I changed

- **R1 (the load-bearing fix):** deleted `decrement_exposure` (its whole-bucket `per_pair.remove`) and renamed `release_exposure` → `subtract_exposure` (`main.rs:677`) — the SINGLE exact inverse of `reserve_exposure` (`cost_per*size` saturating, `open_positions -= 1`). Both the entry-non-fill release AND the unwind decrement now call it. Grep confirms zero `per_pair.remove` remain.
- **postpone.rs:** replaced `HeldPosition` with `SlugPositions { league,date,team_a,team_b, prev, legs: Vec<HeldLeg> }` + new `HeldLeg { pos, cost_per, entry_net, entry_dir }`. `prev`/metadata are slug-level (one per slug). The MLB poll now iterates slugs (one game-match per slug) and emits one `UnwindRequest`/slug (`postpone.rs` snapshot loop).
- **main.rs `positions` map** value → `SlugPositions` (decl + `apply_outcome`/`spawn_unwind`/`refresh_loop` signatures).
- **`track_position` (`main.rs:704`)** now APPENDS a `HeldLeg` (new params `cost_per`/`entry_net`/`entry_dir`); metadata set on first insert (Vacant) only; append never touches `prev` → the C6 concern is now structural.
- **Entry guard (`main.rs:~516`):** kept `halt || pending_entries.contains || flattening.contains` EXACTLY (C5). Replaced the `contains_key(&slug)` clause with `qualifying_add` (new pure fn `main.rs:751`): same-direction + `net >= max(entry_net)+add_tau_gain` + count-cap + scale-in/re-entry flag gate. Added `edge_live: HashSet<slug>` per-frame proxy (read-before-write `was_live`) for SCALE-IN vs RE-ENTRY, and the `[live] ADD(scale-in|re-entry)` log tag.
- **Unwind outcome arm (`main.rs:828`):** v1 one-at-a-time — `spawn_unwind` fires the FRONT leg; the both-filled arm pops `legs.remove(0)`, subtracts ITS stored `cost_per`, drops the slug key only when `legs` empties (R5). The poll's re-emit drives the next leg.
- **config.rs:** 4 new fields `enable_scale_in`/`enable_reentry` (default FALSE), `add_tau_gain` (0.01), `max_positions_per_slug` (default 1) — struct + `from_env` + `test_default`. Banner prints a loud `*** ADD-TO-HELD ARMED ***` line only when a knob departs from the safe default.
- **R7:** the 4 fields added to all 3 `exec.rs` inline Config literals + the `risk.rs` literal (crate compiles).
- Adapted the ~12 shape-only tests (positions-map shape, `track_position` arity, `hp.pos`→`sp.legs[0].pos`, `SubmitOutcome` literals) — SHAPE only, semantics unchanged. Added all 10 §6 tests (`test1`..`test10`).

## Verification
- `cargo test` → **146 passed, 0 failed** (136 baseline-green after mechanical adaptation + 10 new).
- `cargo clippy --all-targets` → clean (0 warnings).
- `cargo run -- --smoke` → behavior IDENTICAL at defaults (NO armed banner; same gate decisions). Armed-env smoke → loud `ADD-TO-HELD ARMED` banner fires.

## Why (non-obvious only)
- Unwind arm subtracts the **removed front leg's OWN stored `cost_per`** (read at pop time), not the spawn-time `out.cost_per` — the exact-release guarantee even if they ever differed. `flattening` serialization + back-of-Vec append prove the front IS the just-flattened leg.
- `edge_live` is mutated every frame (even before the in-flight guard) but is read ONLY by `qualifying_add` via `was_live`; adds are blocked when halted/pending/flattening, so the mutation is inert there.
