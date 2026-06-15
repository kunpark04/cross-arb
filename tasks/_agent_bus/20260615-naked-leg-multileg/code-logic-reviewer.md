---
from: coding-agent (self-review pass)
run_id: 20260615-naked-leg-multileg
timestamp: 2026-06-15T09:54:00Z
scope_reviewed: [bot-rs/src/bookkeeping.rs:344-500 (recover_naked_leg + naked_filled_indices), bot-rs/src/bookkeeping.rs:1080-1175 (2 new tests), bot-rs/src/bookkeeping.rs:243-262 (Recovery outcome arm — read, unchanged)]
critical_count: 0
warn_count: 1
info_count: 2
launch_recommendation: PROCEED WITH FIXES
self_review: true
cross_references: [git working-tree diff (the uncommitted pmus-partial fix this builds on)]
---

## Goal Understanding
Make naked-leg auto-recovery venue-agnostic and complete: an entry is either a clean both-FULL lock, or EVERY leg with `fill_qty>0` is flattened at its OWN fill_qty — no partial position ever persists on either venue. The reachable-at-10-contracts gap was a Kalshi second-leg FOK partial (fill_qty=7, filled=false) that the single-index `naked_filled_idx` recovery abandoned naked. Fail-CLOSE on any unwind error; do not regress the pmus-first single-leg partial path.

## Scope Reviewed
- `naked_filled_indices` — new helper returning all live-naked indices.
- `recover_naked_leg` — rewritten: per-leg ambiguity fail-close, atomic price-all-then-fire, one SELL per naked leg sized to its own fill_qty.
- 2 new tests (both-naked happy path + both-naked one-unpriceable fail-close).
- Recovery outcome arm (read-only) — confirms per-SELL halt-on-unfilled still holds with two SELLs.

## Findings

### CRITICAL (must fix before launch)
- None found. (See Checks Passed for what was actively verified.)

### WARN (fix or justify)
- Fractional `count`/`quantity` on a whole-share Kalshi recovery SELL
  - Location: exec.rs payload builders (PRE-EXISTING, untouched) + bookkeeping.rs:~470 (sets `frac_qty: Some(filled_qty)` on every recovery SELL, including the Kalshi leg).
  - Issue: a Kalshi recovery SELL now serializes `count: 7.0` (float) rather than `count: 7` (int). Kalshi is a whole-share venue; whether it accepts a float `count` for a whole number is unverified live.
  - Why it matters: if Kalshi rejects a float `count`, the recovery SELL fails -> the Recovery arm halts (fail-CLOSE), so it is SAFE (no silent naked leg) but the auto-flatten would not actually reduce risk on a Kalshi partial — it would degrade to a manual-flatten halt.
  - Suggested fix: in the payload builder, only emit the fractional form when `frac_qty` is non-integral OR the venue is pmus; for an integral Kalshi qty emit the int. OUT OF SCOPE for this change (pre-existing author decision; the Kalshi >1-contract FOK-partial is itself "unverified" per exec.rs ~419). Flagging for the owner.
  - Status: reported (deferred — pre-existing, fail-closes safely).

### INFO (optional improvements / simplifications)
- The `W-1` doc comment in `recover_naked_leg` still says "this naked leg" (singular) though it can now cover two. Cosmetic; left as-is to keep the diff minimal.
- `naked_filled_idx` (singular) and `naked_filled_indices` (plural) now coexist. The singular is still load-bearing for the failclose-backstop and the log string; not worth unifying (the plural's `Vec` alloc is undesirable on those hot-ish log paths). Documented in the handoff.

## Checks Passed
- Goal alignment: the flagged scenario (pmus full 10 + Kalshi partial 7) now fires TWO SELLs (10 and 7), neither abandoned — pinned by `both_legs_partial_recovers_each_sized_to_own_fill_qty`.
- Per-leg own-size: each SELL carries `frac_qty = its own fill_qty` (pmus 10, Kalshi 7), never the intent `pos.size`, never the other leg's qty — no over-sell. Fractional preserved exactly (existing 0.01 test still green).
- Single-leg NON-regression: the pre-existing pmus-only-partial test (`partial_pmus_fill_recovers_sized_to_fill_qty_not_intent`) passes unchanged — one naked index -> one SELL, byte-identical path. The `HedgeNotFilled` sentinel still routes through (not treated as the ambiguous Err).
- Multi-leg fail-CLOSE (atomic launch): `both_legs_partial_one_unpriceable_fails_closed_fires_nothing` proves an unpriceable second leg -> halt, ZERO SELLs fired, slug NOT marked flattening — a half-recovery is impossible.
- Ambiguous-Err fail-close preserved: a non-naked leg that ERR'd (non-`HedgeNotFilled`) still halts (could be a hidden LOCK) — the per-leg loop generalizes the old single `resting_ack.is_err()` check; with both legs naked the loop body is skipped (no "other" leg), correctly.
- Both-FULL lock NOT mis-routed: `both_filled()` true short-circuits in `apply_outcome` before `recover_naked_leg` is reached, so `naked_filled_indices == [0,1]` for two full legs is unreachable on the lock path; the recovery's `[0,1]` only arises when >=1 leg is `filled=false` (a real partial).
- Simulated/dry-run excluded: `live_has_fill` requires `!simulated`, so dry-run acks never trigger a recovery (their `both_filled` is true anyway). `naked_filled_indices` of two sim acks = `[]`.
- Per-SELL halt-on-unfilled: the Recovery outcome arm halts independently for each SELL that doesn't fill, so even if only one of two SELLs fails the bot halts (the failed leg surfaces).
- Indexing: legs[i] <-> ack {a,b} positional invariant respected throughout.
- cargo test 169/169 green; cargo clippy --all-targets clean.

## Launch Recommendation
PROCEED WITH FIXES — the money-path gap is closed and fail-closes safely; the one WARN (fractional Kalshi `count`) is pre-existing, out of scope, and degrades to a safe halt rather than a silent naked leg.

## Self-review caveat
Authorship bias applies: I wrote this code and these tests. The both-partial Kalshi FOK at >1 contract is itself unverified live (the codebase treats Kalshi kill-on-miss as documented-but-not-live-verified), so the recovery path it exercises is defensive-by-design and has no live evidence behind it yet. Given this feeds the safe-by-default live bot (0015) on the money path, an independent reviewer pass and a live probe of Kalshi's float-`count` acceptance are warranted before the 1-contract->scale rollout relies on auto-recovery of a Kalshi partial.
