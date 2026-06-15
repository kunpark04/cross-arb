---
from: coding-agent (self-review pass)
run_id: 20260614-wc-dutchbook
timestamp: 2026-06-14T05:00:17Z
scope_reviewed: [bot-rs/src/types.rs:121-200 (3-leg types), bot-rs/src/signal.rs:151-261 (dutch_book), bot-rs/src/exec.rs (TripleAck/submit_triple/run_triple), bot-rs/src/risk.rs:271-410 (evaluate_triple), bot-rs/src/unwind.rs:53-72 (triple_unwind_orders), bot-rs/src/discovery.rs (TripleSpec + soccer3 emit), bot-rs/src/main.rs (TripleMeta/TripleState/build_quote/basket_depth/build_triple/evaluate_triple_frame/apply_triple_outcome/recover_naked_basket + loop wiring)]
critical_count: 0
warn_count: 2
info_count: 3
launch_recommendation: PROCEED WITH FIXES
self_review: true
cross_references: [tasks/_agent_bus/20260614-wcrust/code-logic-reviewer.md, bot/colisted_map.py, tasks/lessons.md]
---

## Goal Understanding
Add a SECOND arb on a WC game — the full-game 3-leg Dutch book (buy YES on all 3 outcomes, each on its cheapest venue; lock iff the 3 cheapest YES asks sum < $1 net of fees + void tail) — running PARALLEL to the per-outcome binary arbs (e50ecbb), with the proven 2-leg core left byte-unchanged. The highest-stakes piece is the 3-leg execution + the naked-PAIR partial-fill recovery.

## Scope Reviewed
- The new 3-leg types, `dutch_book` signal, `submit_triple` exec, `evaluate_triple` gate, `triple_unwind_orders`, the discovery `TripleSpec` emit, and the whole main.rs basket path (routing, build, recovery, loop wiring).
- ADVERSARIAL POSTURE ENGAGED: from here I am a skeptical reviewer hunting the mis-hedge / silent-naked-leg / 2-leg-regression before a launch does — not the author defending the diff.

## Findings

### CRITICAL (must fix before launch)
- None.

### WARN (fix or justify)
- **Held baskets are never released from exposure on settlement.** `triples_held` is inserted on an all-filled lock but has NO removal path (WC baskets have no unwind/postpone source, and no settlement-detection releases them). Over a multi-day run, settled baskets keep their reserved notional → the caps eventually block NEW basket entries. JUSTIFICATION (deferred, not fixed): the failure mode is SAFE (the bot stops OPENING baskets; it never over-trades), the safe-default caps are tiny, and the per-outcome binary path has the analogous held-position lifecycle. A proper fix needs settlement-release for `triples_held` (when a game drops from discovery, decrement its held-basket exposure) — threading `triples_held` into the refresh task. Logged as a follow-on; out of scope for "build the 3-leg arb + recovery".
- **`refresh_loop` rebuilds `triple_state` wholesale but cannot see `triples_held`.** A settled-and-still-held game is dropped from the routing index (correct — no re-entry) but its held basket lingers in `triples_held` (the WARN above). No correctness bug for the recovery/lock path; it's the same exposure-leak surface. The per-outcome binary W16 held-position prune-guard has no triple analogue because WC baskets aren't postpone-unwound — noted for the same follow-on.

### INFO (optional improvements / simplifications)
- `WC_VOID_TAIL` is a hardcoded `0.005` const (no env knob), matching how the per-outcome pairs absorb the tail implicitly via `settle_clean`. A future `ECON_TWIN`-style env knob could calibrate it against the measured void rate; deferred deliberately (the plan defers full void-tail cost integration).
- The `flattening` set is now SHARED across the 2-leg recovery (keyed by pmus slug), the postpone-unwind, and the 3-leg recovery (keyed by leg market = slug OR ticker). A WC outcome's pmus slug is a legitimate shared key; I made the basket FAIL-CLOSED (halt) if a filled basket leg's market is already flattening for a different position (a size-mismatch can't be trusted) — self-resolved within run, the safe choice.
- `OutcomeTag::B` maps to the THIRD array slot while `D` (draw) is the SECOND — the emit order is [A, draw, B]. Intentional + tested (`soccer3_emits_three_per_outcome_binary_pairs`, `build_triple_builds_three_cheapest_venue_yes_legs`), but worth a reviewer's eye since the per-outcome `Pair` emit order is [A, B, draw] (cosmetic; the basket keys by tag, not slot).

## Checks Passed
- **(a) The 2-leg path is BYTE-UNCHANGED.** `git diff HEAD` over `src/` shows 1766 insertions / **9 deletions**, and EVERY one of the 9 deleted lines is a surgical insertion-point edit (a doc comment extended, an import line extended, `let initial = ` → `let (initial, initial_triples) = `, the discovery println extended). `submit_pair`/`run_pair`/`PairAck`/`both_filled`/`evaluate`/`unwind_orders`/`recover_naked_leg`/`build_legs`/`plan_legs`/`Position`/`PositionLeg` bodies are absent from the deletion set. The 135 baseline tests pass UNCHANGED — behavioral equivalence confirmed.
- **(b) The naked-PAIR recovery flattens EVERY filled leg + fails-closed.** Proven by 3 dedicated tests: `basket_two_of_three_fill_flattens_both_filled_legs` (2 filled → 2 flatten SELLs fired + both markets marked flattening + resting 3rd leg cancelled, NO bare halt, NO lock recorded); `basket_one_of_three_fill_flattens_the_single_leg` (exactly 1 SELL); `basket_recovery_failure_fails_closed_with_halt` (an unpriceable filled leg → HALT engaged, while the priceable filled leg is STILL flattened — never skip a flatten we can do). A silently-un-flattened 2-of-3 cannot happen: `recover_naked_basket` sets `all_recovered=false` for any filled leg it can't price/route, and `apply_triple_outcome` calls `triple_naked_failclose` (halt) when `all_recovered` is false; each per-leg SELL routes back as `SubmitKind::Recovery` which re-halts if the SELL itself doesn't fill (reusing the proven 2-leg recovery arm).
- **(c) A basket fires ONLY when net>0 AND all 3 legs have depth.** `dutch_book` sets `no_arb` on net≤0; `evaluate_triple` rejects `NonPositiveEdge`/`BelowEdgeFloor`; `q.depth` is the cross-outcome `basket_depth` walk (0 if ANY leg's chosen ladder is empty) → `size=min(depth.c2,…)` → `NoFillableSize` if 0. Tested: `evaluate_triple_requires_all_three_to_have_depth` (a 0-depth draw leg → NoFillableSize), `dutch_book_basket_over_one_dollar_is_no_arb`, `evaluate_triple_rejects_non_positive_and_below_floor`.
- **(d) Dry-run is honored.** `DryRunBackend::submit_triple` only logs + simulates; `LiveBackend::submit_triple` returns `KeysUnavailable` for all 3 legs when keys are absent (the safe-by-default rail). Tested `dry_run_fires_three_legs_simulated` + `live_triple_without_keys_refuses_to_send`. Smoke output shows `[DRY-RUN] would submit` ×3, no send.
- **(e) Idempotent coids.** Entry `xarb3-<game>-<tag>` (A/D/B distinct), recovery `recover3-<game>-<tag>`, unwind `unwind3-<game>-<idx>` — all distinct per leg; Kalshi dedupes on `client_order_id` (a retry re-uses the coid). Matches the 2-leg convention.
- **Cheapest-venue-per-outcome selection is cross-venue, not single-venue.** `dutch_book_picks_cross_venue_minimum_not_one_venue` proves the basket sums the per-outcome minima (mixing venues), not one venue's 3 asks (the overround always sums > 1). Crossed books rejected (`dutch_book_crossed_book_is_rejected`, L12).
- **`basket_depth` stops at the lock boundary.** `basket_depth_stops_when_marginal_basket_hits_one_dollar` proves the 3-pointer walk excludes deeper rungs that push the marginal basket ≥ $1 (depth=10 not 30), and the 2c bucket is tighter than 0c on a sub-2c-edge rung. This was a self-caught WARN in my first draft (a naive per-leg sum over-stated depth) — self-resolved within run.
- **Routing keeps WC binaries AND the basket independent.** `evaluate_triple_frame` runs AFTER the 2-leg block on the same frame; both arbs evaluate on the same game (smoke shows `xarb-…-ger` binary + `xarb3-ger-cuw-…` basket firing). The shared per-game cluster cap correctly bounds the COMBINED game exposure (basket + binaries are correlated). `TripleState` indexes all 6 per-outcome keys → game (`triple_state_indexes_and_frees_all_six_keys`).
- **Discovery join has no false positives.** The `TripleSpec` reuses the SAME exact-date + alias + `used`-set + all-3-distinct bind as the per-outcome pairs (`pick_wc_game`); a missing sibling emits NO triple (`soccer3_incomplete_game_emits_no_triple`). No new identity logic.
- **No leakage / NaN / lookahead.** Discovery is pure slug-mapping; `days_to_event` reuses the proven `ymd_to_epoch_days` (None → proximity gate dormant, fail-safe); `evaluate_triple` fails-closed on a non-finite `days_to_event` (W5, same as `evaluate`); `lock_days(Cat::Sports, …)` div-by-zero-safe.
- **No 2-leg type was made `PartialEq`/changed.** `Depth` still lacks `PartialEq` (I compare its fields in tests rather than derive it on a 2-leg type).

## Launch Recommendation
PROCEED WITH FIXES — where the only "fixes" are the two WARNs (held-basket exposure release on settlement), which are SAFE-failure-mode follow-ons, not launch blockers. The lock/recovery/no-false-join invariants all hold; the 2-leg core is byte-unchanged; dry-run is the default and live submission stays the owner's gated step.

## Self-review caveat
Authorship bias acknowledged: I wrote this diff, so "I already reasoned about it" can mask a flaw. The strongest objective checks here are (1) the `git diff` deletion set proving the 2-leg core is byte-unchanged, and (2) the three recovery tests that assert the silent-un-flattened-basket cannot occur. This is the HIGHEST-STAKES code in the bot (3-leg execution + partial-fill recovery) and it feeds the live order path; before arming the Dutch book past dry-run, an INDEPENDENT review (or `/llm-council`) on the recovery fail-close + the held-basket exposure lifecycle is warranted, and the two WARN follow-ons (settlement-release of held-basket exposure; void-tail calibration) should land first.
