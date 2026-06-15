---
from: coding-agent (self-review pass)
run_id: 20260615-1358
timestamp: 2026-06-15T13:59:00Z
scope_reviewed: [bot-rs/src/bookkeeping.rs:307-380, bot-rs/src/bookkeeping.rs:181-200, bot-rs/src/bookkeeping.rs:488-505, bot-rs/src/pricing.rs:81-122, bot-rs/src/live.rs:425-435, bot-rs/src/smoke.rs:176-178, bot-rs/src/flatten.rs:1-18, bot-rs/src/flatten.rs:175-200]
critical_count: 0
warn_count: 1
info_count: 2
launch_recommendation: PROCEED WITH FIXES
self_review: true
cross_references: [tasks/_agent_bus/20260615-1320/code-logic-reviewer.md, tasks/_agent_bus/20260615-1320/coding-agent.md, tasks/_agent_bus/20260614-scalein-reentry/independent-review.md, tasks/scale-in-reentry-design.md]
---

## Goal Understanding

Make the OFF-by-default scale-in/re-entry "add" config (0019; `ENABLE_SCALE_IN`/`ENABLE_REENTRY`,
`MAX_POSITIONS_PER_SLUG>1`) both SAFE and FUNCTIONAL. Two blockers, one root cause: the entry coid was
deterministic per (slug, leg), so an add re-firing a held slug reused the held position's Kalshi coid →
Kalshi dedup-409 `order already exists`. (1) SAFETY: the prior `is_definite_not_filled` treated EVERY 4xx as
definite-no-fill, so that dedup-409 would AUTO-FLATTEN the good (filled) leg → un-hedge a real lock (the
cardinal sin). (2) FUNCTIONALITY: even fixed to halt, the add would halt on every collision. Change 1 narrows
the classification so a dedup-409 halts (fail-close) not flattens; Change 2 makes the coid unique-per-position
so the add doesn't collide in the first place; Change 3 hardens the operator `--flatten` escape hatch.

## Scope Reviewed

- bookkeeping.rs:307-380 — `is_definite_not_filled` narrowed (4xx + names-no-fill + not-dedup/conflict + not-408/425).
- bookkeeping.rs:181-200, 488-505 — the two call-site doc comments updated to the narrowed semantics.
- pricing.rs:81-122 — `build_legs` gains `pos_index`; coid = `xarb-{slug}-{idx}-{tag}`.
- live.rs:425-435 — fire site passes `held_legs.len()` as `pos_index`.
- smoke.rs:176-178 — single-position smoke passes `0`.
- flatten.rs:1-18, 175-200 — softened docstring + unmistakable resolved-order confirmation echo.
- Tests: pricing.rs `entry_coid_…`; bookkeeping.rs `is_definite_not_filled_…` rewrite.

## Findings

### CRITICAL (must fix before launch)
None.

### WARN (fix or justify)

- **Index reuse after front-removal can dedup-collide a Kalshi add → safe HALT (not double-fill / not un-hedge).**
  - Location: pricing.rs:121 (coid = `held_legs.len()`), bookkeeping.rs:246 (`sp.legs.remove(0)`).
  - Issue: `pos_index = held_legs.len()` is the COUNT of currently-held legs, not a monotonic counter. The
    unwind path removes from the FRONT (`legs.remove(0)`). So with `MAX_POSITIONS_PER_SLUG>=2`: hold positions
    at coid-idx 0 and 1; unwind the front one → `legs.len()` drops to 1; a NEW add then computes `pos_index=1`
    → Kalshi coid `xarb-{slug}-1-A`, which COLLIDES with the still-held idx-1 position's order → Kalshi
    `409 order already exists`.
  - Why it matters: it BLOCKS that add and (post-Change-1) HALTS the bot. It is NOT a double-fill and NOT an
    un-hedge — Change 1 routes the dedup-409 to fail-close. So the failure is safe-degrading (an armed add is
    forgone + a manual reconcile), not catastrophic. It is UNREACHABLE at the default config (`cap=1` ⇒ a slug
    never holds 2 ⇒ `remove(0)` always empties+drops the slug ⇒ index always restarts at 0).
  - Suggested fix: replace the count-based index with a MONOTONIC per-slug `next_index` stored on
    `SlugPositions` (increment on every `track_position`, never reuse). That touches the design-of-record's
    position struct, so it is deliberately OUT OF SCOPE for this run (the task specified the `legs.len()`
    approach). Recommend it as the follow-up to do BEFORE arming `cap>=2` with active unwinds.
  - Status: reported (in-scope-safe; fix deferred — out of the task's specified `legs.len()` approach).

### INFO (optional improvements / simplifications)

- **The belt-and-suspenders keyword branch in `is_definite_not_filled` is unreachable from the live path.**
  `post_leg` only emits `Rejected` as `transport: {e}` (excluded) or `{status} {text}` (always has a leading
  status), so a keyword-only-no-status body never arises live; the `fill_or_kill`/`insufficient`/`rejected`
  fallthrough is purely defensive. Kept (it's cheap and the test exercises it) — noting it isn't load-bearing.
- **`--flatten` confirmation echo is print-only (no interactive confirm).** The harness runs non-interactive,
  so a y/n prompt isn't viable here; the loud echo + REAL-MONEY banner is the right altitude. No change.

## Checks Passed

- (a) No ambiguous error can still auto-flatten: the ONLY TRUE path requires a `Rejected` body that is
  not-transport/panic, not-dedup/conflict, not-408/425, not-5xx, AND names a no-fill term — that is a terminal
  venue rejection by construction. 5xx/transport/timeout/RateLimited/sentinels all FALSE (asserted exhaustively
  in `is_definite_not_filled_classifies_only_unmistakable_rejections`).
- (b) The dedup-409 now halts: `409 order already exists` / `duplicate` / bare `409 conflict` → FALSE (halt);
  conflict-exclusion precedes the no-fill keyword (`409 rejected: order already exists` → FALSE). Asserted.
- (c) The new coid is unique-per-position AND stable-per-fire-retry: idx 0 vs idx 1 → distinct coids; a re-fire
  at the same idx → identical coid (asserted in `entry_coid_…`). The index advances only on a both-filled lock
  (`track_position`); `pending_entries` blocks any concurrent same-slug re-fire, so there is no double-fill
  window within one in-flight add.
- (d) Default single-position config behavior byte-for-byte unchanged: with `cap=1` the only coid delta is the
  cosmetic `-0-` segment; NO consumer parses the entry coid (cancel uses the persisted `venue_order_id`,
  exec.rs:107; the venue only uses the coid value for dedup, and `xarb-{slug}-0-{tag}` is a stable unique token).
  Greps for `xarb-` confirm no string-match dependency. 175 prior tests unchanged + 1 new = 176 green.
- pmus carries NO coid (exec.rs:430-433) → a pmus add leg can never dedup-collide; the whole coid story is
  Kalshi-only, consistent with the fix.
- Change 1 makes both call sites (pmus-first abort bookkeeping.rs:197; `recover_naked_leg` :505) STRICTLY MORE
  conservative (more cases halt) — the safe direction for the cardinal-sin axis.
- L31 (removing a structural guard → audit every shared resource): the front-removal index-reuse WARN is exactly
  the class L31 warns about; surfaced here, fails closed.
- `cargo test` 176 passed / 0 failed; `cargo clippy --all-targets` clean.

## Launch Recommendation

PROCEED WITH FIXES — both blockers are correctly closed and all checks pass; the one WARN (index reuse after
front-removal) is safe-degrading and unreachable at the default config, but its monotonic-counter fix should
land BEFORE arming `MAX_POSITIONS_PER_SLUG>=2` alongside live unwinds.

## Self-review caveat

This is the author's own diff on a money path with a 2-incident naked-position history; authorship bias is real.
0019 explicitly requires a FINAL fresh INDEPENDENT review before arming the add config past dry-run — this
self-review is a first line of defense, not the last. An independent `code-logic-reviewer`/user pass (especially
on the WARN's index-reuse reasoning and the Change-1 classification boundary) is warranted before any armed
`cap>=2` launch.
