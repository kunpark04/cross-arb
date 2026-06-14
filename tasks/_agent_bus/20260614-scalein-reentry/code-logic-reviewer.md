---
from: coding-agent (self-review pass)
run_id: 20260614-scalein-reentry
timestamp: 2026-06-14T09:42:00Z
scope_reviewed: [bot-rs/src/main.rs:677-687(subtract_exposure), bot-rs/src/main.rs:828-856(Unwind arm R1), bot-rs/src/main.rs:704-738(track_position append), bot-rs/src/main.rs:751-776(qualifying_add), bot-rs/src/main.rs:516-560(entry guard + edge_live), bot-rs/src/main.rs:1014-1056(spawn_unwind front-leg), bot-rs/src/postpone.rs(SlugPositions/HeldLeg + poll), bot-rs/src/config.rs(4 fields)]
critical_count: 0
warn_count: 2
info_count: 2
launch_recommendation: PROCEED WITH FIXES
self_review: true
cross_references: [tasks/scale-in-reentry-design.md, tasks/lessons.md(L30 origin), C:/Users/kunpa/.claude/agent-memory/coding-agent/MEMORY.md(pattern_sentinel_padding_defeats_safety_check, feedback_threshold_ge_vs_gt)]
---

## Goal Understanding
Convert bot-rs position tracking from one-per-slug to N-per-slug to support adding to a held position (SCALE-IN while the base edge is live; RE-ENTRY after it closed), WITHOUT reintroducing the exposure desync the one-position guard prevented. The load-bearing requirement (R1) is that every reserved position is released exactly once for exactly `cost_per*size`, replacing the old whole-bucket `per_pair.remove`. Safe-by-default: both flags off + cap=1 ⇒ zero adds admitted ⇒ behavior byte-identical to today.

## Scope Reviewed
- `subtract_exposure` (the unified inverse) + the deleted whole-bucket remove — R1.
- The Unwind both-filled arm (front-leg pop + exact subtract + slug-drop-when-empty) — R1/R5/§1.5.
- `track_position` append (prev/metadata untouched on append) — C6/§1.
- `qualifying_add` gate (same-dir / tau / cap / flag) — §2/§3/R6.
- The entry guard + `edge_live` proxy (read-before-write) — C5/§3/R3.
- `spawn_unwind` front-leg targeting — §1.5/R2.
- `postpone.rs` SlugPositions/HeldLeg + the slug-iterating poll.
- All config literal sites — R7.

## Findings

### CRITICAL (must fix before launch)
- None. I specifically tried to break R1 and could not: every reserve has exactly one matching subtract using the leg's OWN stored `cost_per`; Test 1 proves stacked A+B → subtract A leaves B exactly (not the old whole-bucket wipe to 0) → subtract B → ~0 + slug key gone. The whole-bucket `per_pair.remove` is grep-confirmed GONE.

### WARN (fix or justify)
- **R3 proxy one-frame lag on SCALE-IN vs RE-ENTRY classification.** `edge_live` is populated on the first book frame AFTER a base both-fills (the fill happens in the outcome arm, not the frame loop). An add candidate arriving on that very first post-fill frame reads `was_live=false` → classifies as RE-ENTRY even though the edge never closed. **Justification (accept):** this only selects WHICH flag (`enable_reentry` vs `enable_scale_in`) gates the add — never a money/exposure desync (R3 explicitly scopes the proxy as "can misclassify which FLAG, not a desync"). Inert at defaults (both flags off). Mitigated by the conservative proxy + the ADD log tag + Test 3. Independent reviewer should confirm they accept the proxy's frame-granularity before arming either flag.
- **R2 half-filled-unwind re-fire is pre-existing, now spans multiple legs.** On a half-filled unwind (one SELL fills, one doesn't), the arm halts and does NOT pop the leg; `flattening` is cleared so the poll re-emits and `spawn_unwind` re-fires the SAME front leg's two SELLs — including the already-sold one. This is UNCHANGED from the pre-existing one-position design (idempotent `unwind-{slug}-{i}` coids dedup the Kalshi re-send; pmus isn't idempotent but `halt` is engaged → manual-intervention state). Not a regression introduced here, but with multiple stacked legs the halt-then-manual surface is larger. Flagged for the reviewer; the design's "multi-fire with an in-flight count" follow-up would address it.

### INFO (optional improvements / simplifications)
- **Self-resolved within run:** the baseline test `reserve_track_and_decrement_exposure_round_trips` had a doc comment naming the now-deleted `decrement_exposure`; updated the doc to `subtract_exposure` (left the function NAME unchanged to avoid renaming a passing baseline test — "decrement" still reads as "round-trips to zero"). Also adapted its final assertion from key-absence to per-pair-value≈0 (the R1 semantics: value-subtract, not bucket-remove).
- `held_legs` is cloned out of the `positions` lock once per frame (even for unheld slugs, where it's empty) — one extra brief lock acquisition vs today. Negligible; kept for code clarity (avoids holding the lock across the guard).

## Checks Passed
- **R1 exact release:** reserve(base)+reserve(add) − subtract(base) − subtract(add) = 0, proven by Test 1 (distinct cost_pers 0.90/0.95 so a wrong-amount subtract is detectable) and Test 7 (a failed ADD releases ONLY its reservation; base per-pair contribution and base leg both intact).
- **Whole-bucket remove gone:** grep `per_pair.remove` → 0 hits; `decrement_exposure`/`release_exposure` → only doc-comment/test-name references.
- **Single-position byte-equivalence at defaults:** traced the entry guard for held + unheld slugs at cap=1/flags-off → held → `qualifying_add` returns None (cap clause) → `continue` (== old `contains_key` continue); unheld → `add_tag=None` → falls to `evaluate` unchanged. Test 10 + the 136-green baseline + the smoke (no armed banner, identical gate decisions) corroborate.
- **Front-leg pop is the just-flattened leg:** appends push to the BACK; no append while `flattening` (entry add path requires `!flattening.contains`); removals are single-threaded on the loop; `flattening` dedups a 2nd unwind. So `legs[0]` is unchanged between spawn and outcome. Subtract uses the popped leg's OWN cost_per regardless.
- **R5 slug-key drop:** `drop_slug = legs.is_empty()` after the pop → `remove`. Test 1 + Test 8 assert the key vanishes only after the LAST leg; W16 prune comment updated (held ⟺ map entry exists).
- **R6 opposite-direction blocked:** `qualifying_add` clause 1 (`all entry_dir == edge.dir`) + `same_dir_live` both require same direction. Test 4 asserts an opposite-dir 0.20 arb → None.
- **§ exactly-at-threshold (feedback_threshold_ge_vs_gt):** the tau gate is `edge.net < base_net + add_tau_gain → block`, i.e. ADMIT on `>=`. Test 4 explicitly pins `net == base + tau` → Some("scale-in") (named as passing, not borderline-fail).
- **Caps bind across positions (strengthening):** the held leg's notional is in `per_pair[slug]`, so `evaluate`'s `pair_room`/`clus_room`/`tot_room` bound the SUM. Test 6 → PairCap + ClusterCap.
- **Sentinel-padding check (pattern_sentinel_padding_defeats_safety_check):** verified the new add path does NOT reuse a benign sentinel to slip a safety predicate — adds go through the SAME `evaluate` + `realized_edge_clears_floor` + naked-leg recovery as fresh entries; the only new gate (`qualifying_add`) is additive (it can only BLOCK, never admit something `evaluate` would reject).
- **Safe-by-default arming banner:** silent at defaults; loud `*** ADD-TO-HELD ARMED ***` when any knob departs (verified both ways in the smoke). Dry-run stays the default; nothing armed.
- **R7 config:** all 3 exec.rs literals + risk.rs literal + struct/from_env/test_default carry the 4 fields (crate compiles + clippy clean).

## Launch Recommendation
PROCEED WITH FIXES — the R1 exposure-desync axis is closed and proven exact; the two WARNs are (a) the design-acknowledged R3 proxy frame-lag (flag-selection only, never a desync) and (b) a pre-existing half-fill-unwind re-fire surface. Both are safe to defer because both feature flags ship OFF (cap=1) so NO add can fire until the owner explicitly arms it; the change is byte-identical to today at defaults.

## Self-review caveat
I authored this diff, so authorship bias is real — I am most confident about the parts I tested hardest (R1/Test 1/Test 7) and least able to see a blind spot in the cross-frame `edge_live` proxy semantics. This is the riskiest money-path change since the Dutch book and the design itself requires an independent review of R1–R4 before arming past dry-run; a follow-up independent `code-logic-reviewer` (or the owner) should focus on: (1) is the front-leg-pop / `flattening`-serialization argument airtight under a postpone that interleaves with a concurrent recovery on the same slug (R4)? and (2) is the R3 proxy frame-granularity acceptable for the SCALE-IN/RE-ENTRY flag distinction before either flag is armed?
