---
from: coding-agent (self-review pass)
run_id: 20260614-scalein-reentry
timestamp: 2026-06-14T00:00:00Z
scope_reviewed: [bot-rs/src/main.rs:~280-310 (FlatKind + flattening decl), bot-rs/src/main.rs:~939-951 (recover_naked_leg fix), bot-rs/src/main.rs:~1055-1075 (spawn_unwind), bot-rs/src/main.rs (w1 regression test)]
critical_count: 0
warn_count: 0
info_count: 1
launch_recommendation: PROCEED
self_review: true
cross_references: [tasks/_agent_bus/20260614-scalein-reentry/independent-review.md, tasks/scale-in-reentry-design.md]
---

## Goal Understanding
W-1: an armed scale-in/re-entry could leave a SILENT naked leg when a half-filled ADD raced an in-flight UNWIND on the same slug — `recover_naked_leg` short-circuited on a bare `flattening.contains(slug)` and fired neither recovery nor halt. The fix records WHICH kind holds the flatten slot so recovery treats ONLY a recovery-for-this-leg as "covered"; an unwind-held slot (covers a different leg) now fails CLOSED (halt). Invariant guaranteed: a filled leg is NEVER left without EITHER a fired recovery OR a halt.

## Scope Reviewed
- `FlatKind` enum + `flattening: HashMap<String, FlatKind>` — new slot-owner tracking; all touch points migrated (in-flight guard, spawn_unwind dedup+insert, recover insert).
- `recover_naked_leg` `match flattening.get(slug)` — the core fix.
- `spawn_unwind` — dedup still kind-agnostic (`contains_key`); marks `Unwind`.
- New `w1_halffilled_add_while_unwind_in_flight_fails_closed` regression test + the migrated existing flatten tests.

## Findings

### CRITICAL (must fix before launch)
- None.

### WARN (fix or justify)
- None.

### INFO (optional improvements / simplifications)
- The `Some(FlatKind::Recovery) => return true` branch is, in practice, UNREACHABLE: only one entry/add is ever in flight per slug (the `pending_entries` guard blocks a 2nd, and the main-loop `flattening` guard blocks a new add once a recovery marks the slot), and `recover_naked_leg` runs synchronously within one outcome application — so it cannot be re-entered for a slug whose recovery it just launched. The branch is kept as a correct, defensive statement of intent (a genuine in-flight recovery for this slug's leg IS covering it). Not worth removing; documents the invariant.

## Checks Passed (adversarial interleave sweep — the mandated question: is there ANY remaining path where a filled leg is left without recovery OR halt?)
- **Single-task model confirmed**: `spawn_unwind`, `apply_outcome`→`recover_naked_leg`, and the entry/add path all mutate `flattening`/`positions`/`exposure` on the ONE `select!` loop turn; spawned tasks do only I/O + send outcomes. "Interleave" = channel arrival order, not a data race.
- **unwind↔add (the W-1 case)**: unwind holds slot=Unwind, half-filled add lands → `recover_naked_leg` returns false → `naked_leg_failclose` engages halt. FAIL-CLOSED. ✓ (regression test green)
- **add↔unwind (reverse)**: a recovery holds slot=Recovery; an unwind request arrives → `spawn_unwind` `contains_key` → returns early, pops no leg, fires nothing; poll re-emits. No abandon. ✓
- **unwind↔unwind**: 2nd unwind refused by `contains_key`; a half-filled unwind → `naked_leg_failclose` halts (`main.rs:854`). ✓
- **recovery↔{add,recovery}**: at most ONE entry/add in flight per slug (`pending_entries` blocks a 2nd add; the main-loop `flattening` guard blocks a new add once a recovery marks the slot) → no second concurrent recovery/add on a slug. ✓
- **Exposure exactness on the fail-close**: the add's `subtract_exposure` (release) runs BEFORE the recovery attempt and uses the add's OWN `cost_per`/`pos` — releases exactly the add's reservation; the base leg's reservation and the base leg itself are untouched. Test asserts `exp.total == base (0.90*3)`, `open_positions==1`, base legs.len()==1. ✓
- **No second flatten spawned in the fail-close**: the `Unwind` branch returns before any `spawn_flatten`/`flattening.insert`, so the unwind's slot ownership of `legs[0]` is preserved — no competing SELL races the unwind's pop. Test asserts `flat[slug]==Unwind` and `rx.try_recv().is_err()`. ✓
- **Safe-by-default**: the new branch only fires on a LIVE naked leg (impossible in dry-run, where acks are simulated+filled → both_filled). Smoke at defaults is byte-identical; no `ADD-TO-HELD ARMED` banner. ✓
- **Dtype/migration completeness**: grepped the crate — `flattening`/`FlatKind` are confined to `main.rs` (config.rs hit is the word "flattening" in a comment). All `HashSet`→`HashMap` touch points migrated; build + clippy clean.
- **R5 compounding-symptom note (reviewer)**: with the fix the bot HALTS on this race (CRITICAL log + manual-flatten surface); the held base leg stays in `positions` (≠ book map), so a later book prune doesn't lose it — same posture as every other fail-close path, not a new hole.

## Launch Recommendation
PROCEED. The W-1 silent-naked-leg path now fail-closes (halt); the invariant "a filled leg is never left without a recovery OR a halt" holds across all four interleave classes; safe-by-default behavior is byte-identical (147 tests, clippy clean, smoke unchanged).

## Self-review caveat
Authorship bias acknowledged: I both wrote and reviewed this fix. The change is small and the adversarial interleave sweep is exhaustive over the two naked-leg sources, but it sits on a money-path (armed scale-in/re-entry) and the original W-1 was itself found by an INDEPENDENT reviewer (not the implementer). An independent `code-logic-reviewer`/owner pass flipping the same R4-A probe — and a check of the unwind↔unwind half-fill re-fire (W-2, pre-existing, out of this scope) — is warranted before arming a flag with `MAX_POSITIONS_PER_SLUG >= 2`.
