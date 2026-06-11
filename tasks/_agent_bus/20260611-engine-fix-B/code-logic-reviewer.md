---
from: coding-agent (self-review pass)
run_id: 20260611-engine-fix-B
timestamp: 2026-06-11T14:29:08Z
scope_reviewed: [bot-rs/src/main.rs:run_live+helpers, bot-rs/src/main.rs:refresh_loop, bot-rs/src/risk.rs:evaluate, bot-rs/src/exec.rs:ExecutionBackend]
critical_count: 1
warn_count: 3
info_count: 3
launch_recommendation: PROCEED WITH FIXES
self_review: true
cross_references: [tasks/_agent_bus/20260611-engine-review/B-risk-config.md, tasks/_agent_bus/20260611-engine-review/E-loop-concurrency.md]
---

## Goal Understanding
Route every order submission on the live path through a single SPAWNED + ACKED channel so the single-task
event loop never blocks on network I/O and the postponement-unwind arm stays hot, while de-duplicating
concurrent in-flight work and failing closed on a naked live leg; plus six gate hardenings (C7/C8/C9/W4/W5/W6)
and four loop-lifecycle fixes (C1/C3/C6/W16/W17). This is the real-money order path — every existing safety
property (dry-run logs only, kill-switch, caps, `leg_prices_come_from_books_not_edge`) had to survive.

## Scope Reviewed
- main.rs `run_live` + `spawn_submit`/`apply_outcome`/`spawn_unwind`/`naked_leg_failclose`/`reserve_exposure`/
  `release_exposure`/`track_position`/`supervise_fatal`/`poll_opt`/`lock` — the core refactor + C1/C3/C5/C6/W14/W16/W17.
- main.rs `affordable` (W4), `realized_edge_clears_floor` (W6), `refresh_loop` (W16/C9), `main()` + `banner()` (C8).
- risk.rs `evaluate` step 1b (W5), step 3b (C7).
- exec.rs `ExecutionBackend` (`&self` + `Send + Sync`).

## Findings

### CRITICAL (must fix before launch)
- W14 naked-leg fail-close is correct but the FILLED LEG IS NOT AUTO-FLATTENED — self-resolved scope boundary
  - Location: main.rs `naked_leg_failclose` (~637)
  - Issue: on a live one-leg fill the code engages the runtime halt + logs the filled leg, but it does NOT
    place a closing order for that naked directional leg. It is left open for MANUAL flattening.
  - Why it matters: a naked leg is real directional exposure; until flattened it is an open market risk.
  - Resolution: this is the SPEC'd behavior verbatim ("log a CRITICAL line, ENGAGE the kill-switch … keep the
    filled leg visible (log it). Do NOT silently continue."). Auto-flattening a half-filled entry is an
    execution-time concern the prompt explicitly routes to the stage-2 leg-fill-timeout path, NOT this run.
    Recorded as CRITICAL because a launch operator MUST know that a W14 trip requires a human flatten; the
    halt-and-log is the designed first line, not the full remediation. Status: reported (behavior is correct
    per spec; flagged so the runbook documents the manual step). No code change — auto-flatten would be
    out-of-scope scope creep on the order path.

### WARN (fix or justify)
- Re-entry stacks exposure on a slug a held position already contributes to; the unwind path frees the WHOLE
  per-pair bucket
  - Location: main.rs `apply_outcome` unwind branch (uses `decrement_exposure`, ~621) vs `reserve_exposure`/
    `release_exposure` (additive)
  - Issue: the entry de-dup gate (line ~454) blocks a second entry only while one is *in flight*
    (`pending_entries`) — it does NOT block a new entry for a slug that already has a RECORDED held position.
    With a per-pair cap large enough to admit a second fill, `reserve_exposure` stacks `per_pair[slug]`, and an
    unwind's `decrement_exposure` removes the whole bucket (both contributions). At the staged-rollout default
    (`MAX_NOTIONAL_PER_PAIR=$1`, 1 contract) the per-pair cap rejects the re-entry, so this is latent.
  - Justification / why not fixed here: this is a PRE-EXISTING "one position per pmus slug" modeling assumption
    (documented on `decrement_exposure` before this run) and the C6 fix only *acknowledges* re-entry for `prev`
    preservation. A correct fix is per-position exposure tracking (a list of positions per slug), which is a
    larger redesign than this task authorizes. I made the release path EXACT (`release_exposure`) so the
    in-flight-fail case is correct; the residual is only the recorded-then-re-entered case under a raised cap.
    Surfaced for a follow-up; safe at current caps.
- C1 supervisor treats a CLEAN task return as fatal — correct for streams, but `refresh_loop`/`poll` returning
  cleanly would also halt
  - Location: main.rs `supervise_fatal` (~660) + the four supervisor `select!` arms
  - Issue: the streams/refresh/poll are all `loop {}` and only return on an unrecoverable error (bad WS request
    URL) or panic, so "task ended" ⇒ dead data source ⇒ halt is right. But it is worth stating that ANY future
    edit that makes one of those tasks return cleanly on a recoverable condition would (correctly, by this
    guard) halt trading. Documented so it is a deliberate contract, not a surprise.
  - Justification: matches the spec ("treats ANY of those tasks ending/panicking as FATAL … A dead collector
    must HALT"). No change.
- W6 realized-edge re-check computes fees at the ROUNDED price, not the exact book price
  - Location: main.rs `realized_edge_clears_floor` (~510)
  - Issue: `signal`/`game_signal` compute marginal fees at the unrounded book prices; my re-check uses the
    rounded `price_cents`. So `realized_net` differs from `edge.net` by both the cost-rounding (intended) and a
    sub-cent fee-rounding term.
  - Justification: the booked order IS at the rounded price, so charging the fee at the booked price is the
    honest realized number — this is *more* correct than reusing the gated fee. Intentional; noted for clarity.

### INFO (optional improvements / simplifications)
- `apply_outcome` entry-both-filled guards `if let (Some(pos), Some(pair))`; for an Entry both are always
  `Some` by construction (`spawn_submit` call site). The guard is defensive; if it ever failed it would
  strand the reservation (the SAFE direction — over-counts exposure). Could be an `expect` to assert the
  invariant, but the silent-keep is safer for a real-money loop. Left as-is.
- `SubmitOutcome.cost_per` is unused for an Unwind (passed as `0.0`). A two-variant struct/enum split could
  drop it, but the single struct keeps `spawn_submit` uniform. Not worth the churn.
- The poison-tolerant `lock()` recovers a poisoned guard silently. That is correct for plain book/exposure
  state (a partial write is at worst a stale read the gates tolerate), but if a future invariant-bearing
  structure goes behind one of these locks, the recover-silently policy should be revisited per-lock.

## Checks Passed
- Concurrency: ALL exposure + `positions` + in-flight-set mutation happens on the event-loop turn (reserve at
  the spawn site in the loop body; settle in the `outcome_rx` arm). The spawned task does ONLY `submit_pair` +
  send — zero shared-state mutation — so `exposure` stays lock-free and single-threaded. No new lock nesting;
  the E-review's clean lock-ordering audit is preserved (the spawn holds no lock).
- C5 double-fire: `pending_entries`/`flattening` checked before every spawn; the poll's per-cycle re-emit is
  dropped while a flatten is in flight; cleared on the matching outcome.
- C4 non-blocking: `block_in_place` runs on the SPAWNED task's worker (multithread runtime, `features=["full"]`),
  never on the loop; the unwind/outcome arms stay pollable during an entry's RTT.
- W14 dry-run safety: simulated acks ⇒ `both_filled` always true ⇒ never trips; the explicit
  `!a.simulated` predicate is tested (`outcome_naked_live_leg_engages_halt` + the simulated-partial negative case).
- Exposure round-trip: reserve → record (keep) and reserve → fail (exact release) and record → unwind-flatten
  (decrement) all tested to return total/open_positions to zero.
- C3: a just-cleared Kalshi book is blocked by `k_fresh` until its post-reconnect snapshot frame arrives; a 1:1
  pair whose single ticker just re-snapshotted trades (pmus book persists). Coarse `stream_paused` left as-is.
- C7: divergent-but-uncrossed `k_b` rejected; coherent `k_b` passes; weather/econ (`k_b=None`) unaffected.
  Verified the comparator is `>` AND the test value is strictly past 40c (caught + fixed an exactly-at-40c test
  that falsely passed — the borderline-threshold trap from my own failure-mode memory).
- C8: live+prod + settle-clean-off REFUSES (exit 2) without the second consent, UNLOCKS (exit 0) with it,
  banner shouts when disabled — all three verified by running the binary with env toggles.
- C9: `fresh.truncated` skips the prune (still applies adds) and does NOT advance the miss-count for off-page
  slugs (comment corrected to match).
- W4/W5/W16/W17: affordable subtracts open exposure (tested incl. over-deploy clamp); NaN proximity rejected;
  refresh won't prune/free a held slug's book; the unwrap is gone.
- Invariant `leg_prices_come_from_books_not_edge`: untouched (build_legs/plan_legs unchanged); its test green.
- Tests 112/112; clippy 3 pre-existing/out-of-scope warnings, zero new; smoke exercises every gate path.

## Launch Recommendation
PROCEED WITH FIXES — the code is correct and all tests/clippy/smoke are green; the one CRITICAL is a
documentation/runbook obligation (a W14 trip needs a manual flatten, by design) rather than a code defect, and
the WARNs are pre-existing modeling limits that are safe at the staged-rollout caps.

## Self-review caveat
This is authorship self-review with attendant bias; I caught a real borderline-threshold test slip and a
remove-bucket-vs-exact-release exposure bug during the pass, but an INDEPENDENT review is warranted before any
live+prod scale-up — this change is on the signed-prereg-adjacent real-money order path (decision 0015), and
the re-entry exposure-stacking WARN in particular would benefit from a second set of eyes deciding whether
per-position exposure tracking should land before caps are raised past 1 contract / $1-per-pair.
