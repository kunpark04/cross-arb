---
from: coding-agent
run_id: 20260615-1358
timestamp: 2026-06-15T13:58:00Z
task_summary: Three money-path changes that make the scale-in/re-entry "add" config (0019) safe AND functional — narrow the 4xx auto-flatten classification, give each position a unique-but-retry-stable entry coid, and harden --flatten.
files_changed: [bot-rs/src/bookkeeping.rs, bot-rs/src/pricing.rs, bot-rs/src/live.rs, bot-rs/src/smoke.rs, bot-rs/src/flatten.rs]
files_created: []
context_loaded: [CLAUDE.md, bot-rs/README.md, tasks/lessons.md, tasks/scale-in-reentry-design.md, decisions/0019-scale-in-reentry-multi-position.md, decisions/0020-pmus-first-serial-plus-recovery-cost-gate.md, decisions/0021-fill-or-kill-orders-and-churn-cooldown.md, decisions/0024-partial-fill-flatten-any-non-clean-lock.md, bot-rs/src/pricing.rs, bot-rs/src/flatten.rs, bot-rs/src/exec.rs, bot-rs/src/probe.rs, bot-rs/src/live.rs, bot-rs/src/bookkeeping.rs, bot-rs/src/smoke.rs, "git diff (prior uncommitted 409-auto-flatten + --flatten pass)"]
libraries_consulted: []
self_review_artifact: tasks/_agent_bus/20260615-1358/code-logic-reviewer.md
---

## What I changed

- **Change 1 (SAFETY — narrow the 4xx classification).** `is_definite_not_filled` (bookkeeping.rs:307-380)
  no longer returns TRUE for any 4xx. A 4xx is now NECESSARY but NOT sufficient: it returns TRUE only if the
  body NAMES a no-fill condition (`fill_or_kill`/`insufficient`/`rejected`) AND does NOT name a dedup/conflict
  (`already exists`/`duplicate`/bare `conflict`); a dedup-409, a bare `408 request timeout`, a `425 too early`,
  any 5xx, transport/panic, and `RateLimited`/sentinels all route to FALSE (halt, fail-close). The legitimate
  `409 fill_or_kill_insufficient_resting_volume` still returns TRUE (the auto-flatten). Updated the two
  call-site doc comments (bookkeeping.rs:181-189, ~488-496) to match.
- **Change 2 (FUNCTIONALITY — unique-per-position, stable-per-retry coid).** `build_legs` (pricing.rs:81)
  gained a `pos_index: u32` arg woven into the entry coid: `xarb-{slug}-{idx}-{tag}` (pricing.rs:121). The
  live loop passes `held_legs.len()` (live.rs:~430), which is 0 for the initial entry and advances by 1 only
  when a NEW position actually locks (`track_position`) — so an add gets a fresh idx (no Kalshi dedup-409
  collision with the held coid) while a within-fire retry reuses the same idx/coid (the deterministic-coid
  dedup still prevents a double-fill). `smoke.rs:177` + all `pricing.rs` test call sites pass `0`.
- **Change 3 (harden `--flatten`).** Softened the line-11 docstring (flatten.rs:10-17) — a SELL is reduce-only
  ONLY if (venue,market,side,qty) match a held leg; a mis-aimed SELL OPENS a short (cites exec.rs:440-446). Made
  the resolved-order confirmation a prominent boxed echo (flatten.rs:~180) printing venue/market/side/qty/price/
  coid before any send, with a loud REAL-MONEY/sandbox/dry-run line.
- **Tests:** new `entry_coid_is_unique_per_position_and_stable_per_index` (pricing.rs); rewrote
  `is_definite_not_filled_classifies_only_unmistakable_rejections` (bookkeeping.rs) for the narrowed table
  (adds dedup-409/408/425/generic-409 → FALSE, bare-400/422/499 flip to FALSE, FOK-409 stays TRUE). 176 tests
  green, clippy clean.

## Why (non-obvious only)

- The coid-collision problem is **Kalshi-only**: pmus carries NO idempotency key (exec.rs:430-433), so a pmus
  add leg never dedup-collides regardless of index — Change 2 only needs to protect the Kalshi leg, and Change 1
  correctly halts on a Kalshi dedup-409. The two changes are complementary: Change 2 prevents the common
  collision; Change 1 makes the residual collision (see the reviewer artifact's WARN-1, index-reuse after
  front-removal under armed cap≥2) fail SAFE (halt) instead of un-hedging a real lock.
- I built ON TOP of the prior uncommitted pass (the 409-auto-flatten refinement + `--flatten` CLI), not over it.
