---
from: coding-agent (self-review pass)
run_id: 20260614-mainrs-split
timestamp: 2026-06-14T21:01:33Z
scope_reviewed:
  - bot-rs/src/main.rs (stripped to header/mods/use/main/banner)
  - bot-rs/src/pair.rs
  - bot-rs/src/pricing.rs
  - bot-rs/src/bookkeeping.rs
  - bot-rs/src/refresh.rs
  - bot-rs/src/smoke.rs
  - bot-rs/src/live.rs
  - bot-rs/src/test_support.rs
critical_count: 0
warn_count: 0
info_count: 1
launch_recommendation: PROCEED
self_review: true
cross_references: []
---

## Goal Understanding
A PURELY MECHANICAL relocation of a 2970-line money-path Rust monolith into 7 focused modules with ZERO
behavior change — not one byte of logic, comment, numeric constant, or test assertion may change; only the
file a given item lives in (plus the minimum `pub(crate)`/`use`/path-qualifier mechanics relocation forces)
may change. The bar is set by 3 hard gates: 147 tests, clippy-clean, byte-identical smoke.

## Scope Reviewed
- bot-rs/src/main.rs — now header + `#![allow(dead_code)]` + 19 `mod` + `#[cfg(test)] mod test_support` + `use` + `fn main` + `fn banner`; verified no dangling references to any moved symbol.
- bot-rs/src/{pair,pricing,bookkeeping,refresh,smoke,live}.rs + test_support.rs — the moved items, each with a `use` header + (for prod fns/structs/fields) `pub(crate)`.

## Findings

### CRITICAL (must fix before launch)
None.

### WARN (fix or justify)
None.

### INFO (optional improvements / simplifications)
- The 3 shared fixtures `q_pk`/`wx_pair`/`wc_pair` now live in `test_support.rs`; the task anticipated these
  three as the only cross-module fixtures and that held exactly (pricing.rs is the only consumer; pair.rs and
  refresh.rs tests need none of them). No further shared fixture was required. No action.

## Checks Passed
- **Verbatim-move integrity (the core risk).** Byte-diffed every moved function body against `HEAD:main.rs`
  (visibility-normalized): all 12 bookkeeping fns, all 11 pricing fns + private `plan_legs`, all 4 refresh
  fns, all 4 live fns (incl. the 367-line `run_live` and 119-line `refresh_loop`), all 3 smoke fns, and
  `lock`/`pair_tickers` — every one IDENTICAL except `pub(crate)`. The single exception is the documented
  `types::` -> `crate::types::` (×3) path qualifier in `smoke.rs`, an unavoidable submodule-relocation
  mechanic (no logic/comment/constant token changed).
- **Struct/enum fidelity.** Field/variant lists of `LivePair` (10 fields, same order), `PairState` (2),
  `SubmitKind` (3), `FlatKind` (2), `SubmitOutcome` (8), `PlannedLeg` (5) all match. impl blocks
  (`kalshi_tickers`, `From<discovery::Pair>`, `PairState::insert/remove`) byte-identical.
- **Visibility correctness.** `PlannedLeg` + its fields stay fully PRIVATE (only pricing.rs uses it); every
  cross-module item is `pub(crate)`; struct FIELDS of `LivePair`/`PairState`/`SubmitOutcome` are `pub(crate)`
  as required. The compiler enforced no over- or under-exposure (clean build, clean clippy).
- **Test-suite integrity.** 39 `#[test]`/`#[tokio::test]` attrs in the original, 39 in the new files; the
  partition matches the task spec EXACTLY (pair 2 / pricing 11 / bookkeeping 24 / refresh 2 = 39). Byte-diffed
  the highest-risk safety tests (`w1_halffilled_add_while_unwind_in_flight_fails_closed` 56 lines,
  `test1_multi_position...desync`, `recovery_flatten_that_does_not_fill_engages_halt`,
  `outcome_naked_leg_recovers_with_cancel_and_sell`, `test6_notional_caps...`,
  `realized_edge_recheck...`, `leg_prices_come_from_books_not_edge`, `cents_rounds_toward_marketable`) —
  all byte-identical. Shared fixtures `q_pk`/`wc_pair` token-identical (indentation-only relocation).
- **No fixture cross-contamination.** Module-local fixtures (`seed_legs`, `wx_entry_pair`, `held_leg`,
  `add_cfg`, `run_apply`, `sim_ack`, `live_ack`, `set`, etc.) live only in their owning module; no test
  reaches into another module's local fixture (verified the only cross-module fixtures are the 3 in
  test_support).
- **`#![allow(dead_code)]` scope.** Kept crate-level at the top of main.rs, so it still covers stage-1
  dead fields/variants across all new modules (clippy stayed clean with no per-module re-add needed).
- **No untouched-file violation.** `git diff` empty for all 13 forbidden modules (book, signal, risk, types,
  exec, discovery, matcher, postpone, unwind, venue, config, ledger, auth) and Cargo.toml.
- **Money-path invariants preserved by construction** (no logic edited): direction-aware tick rounding
  (BUY ceil / SELL floor, `pricing::cents`/`quantize_to_tick`); recovery SELL pmus-tick floor
  (`flatten_exit_cents`); W-1 flatten-slot kind discrimination (recovery vs unwind in `recover_naked_leg`);
  exact per-position exposure subtract (`subtract_exposure`); reserve-on-spawn (`reserve_exposure`);
  fail-closed naked-leg backstop (`naked_leg_failclose`). These are exactly the patterns in agent memory
  (pattern_direction_aware_tick_rounding, pattern_flatten_slot_covers_wrong_leg,
  pattern_unify_inverse_paths_distinct_magnitudes) and a verbatim move cannot regress them — confirmed they
  are byte-identical and their guarding tests still pass.

## Launch Recommendation
PROCEED. All three hard gates pass (147 tests, clippy-clean, smoke program output byte-identical via SHA-256);
every moved item is byte-identical to its origin modulo the required `pub(crate)`/path-qualifier mechanics;
no forbidden file touched.

## Self-review caveat
This is authorship self-review of a money-path file. The verbatim-move claim is backed by mechanical byte-diffs
of every function/struct/test rather than by my own judgment, which substantially de-risks authorship bias for a
relocation refactor (there is no new logic to mis-judge — only "did the bytes move unchanged," which a diff
answers objectively). The one editorial judgment is the smoke.rs `crate::types::` qualifier; an independent
reviewer should confirm they agree that is a relocation mechanic, not a behavior change. Because this is a
relocation (not a logic change) feeding no new preregistration, a full independent code-logic review is
lower-value than usual, but a human skim of the 3-line smoke.rs path-qualifier delta is the one worthwhile check.
