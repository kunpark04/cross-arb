---
from: coding-agent (self-review pass)
run_id: 20260611-residual-fix
timestamp: 2026-06-11T20:44:54Z
scope_reviewed: [bot-rs/src/main.rs:recover_naked_leg+apply_outcome+build_legs+spawn_flatten, bot-rs/src/exec.rs:pmus_order_filled+submit, bot-rs/src/discovery.rs:Pair+pm_order_constraints]
critical_count: 1
warn_count: 1
info_count: 2
launch_recommendation: PROCEED WITH FIXES
self_review: true
cross_references: [tasks/_agent_bus/20260611-venue-contract-review (empty), agent-memory/coding-agent/pattern_rest_envelope_probe.md, agent-memory/coding-agent/pattern_event_loop_inline_blocking_io.md]
---

## Goal Understanding
Close 4 order-path correctness gaps in the Rust live bot: auto-recover a half-filled entry (cancel + flatten) instead of just halting; pin pmus fill-detection to the real OpenAPI field names; enforce per-market pmus tick/min-size in the leg builder; fix a stale slug-identity comment. Compile + unit-test only, fail SAFE on the money path, no weakened gates / no broken dry-run default.

## Scope Reviewed
- main.rs `recover_naked_leg` / `apply_outcome` (Entry + new Recovery arm) / `build_legs` + `quantize_to_tick` / `spawn_flatten` / `spawn_cancel` / `naked_filled_idx` — FIX A + FIX C.
- exec.rs `pmus_order_filled` / `Ack.filled` doc / `submit` trait+impls / `pmus_base` comment — FIX B + FIX D.
- discovery.rs `Pair` fields / `pm_order_constraints` / 3 Pair builders / `PM_MARKETS` comment — FIX C + FIX D.
- All edits within task scope; no adjacent refactors. No signing/market-data/book/signal/matcher parser touched.

## Findings

### CRITICAL (must fix before launch)
- Failed recovery flatten did not engage the halt (authorship-bias trap; SELF-RESOLVED)
  - Location: main.rs `apply_outcome` / `spawn_flatten` (original design)
  - Issue: the first recovery design reported the single SELL through a `SubmitKind::Unwind` ack padded with a SIMULATED+filled sentinel in the unused leg `b`. The shared `naked_leg_failclose` only halts on a `live_filled` (`Ok && filled && !simulated`) leg; when the recovery SELL itself failed, leg `a` was not-filled and leg `b` was the simulated sentinel, so the predicate saw NO live naked leg and DID NOT halt — silently abandoning the still-naked directional position. The docstring even claimed "a SELL that itself fails re-trips the halt" (false). Not caught by any test (the SELL-fills happy path worked).
  - Why it matters: a real-money directional leg left unhedged and unflagged — exactly the catastrophic case the whole fix exists to prevent.
  - Fix: introduced a dedicated `SubmitKind::Recovery` variant + outcome arm; the flatten's leg `b` is now an explicit `Err` placeholder (never a "filled" sentinel); the Recovery arm halts iff the SELL did not fill. Added test `recovery_flatten_that_does_not_fill_engages_halt` (both the fail→halt and fill→clean branches). Recorded the general pattern to agent memory (`pattern_sentinel_padding_defeats_safety_check`).
  - Status: self-resolved within run

### WARN (fix or justify)
- Recovery SELL price is not quantized to a coarse pmus tick
  - Location: main.rs `recover_naked_leg` (the `cents(exit_price(..))` step)
  - Issue: FIX C quantizes ENTRY pmus legs to `orderPriceMinTickSize`, but the recovery flatten SELL uses a whole-cent price and the `Position` doesn't carry the tick. On a hypothetical coarse-tick (>0.01) pmus market the flatten could be rejected by the venue.
  - Why it matters: a rejected flatten does NOT silently leave a naked leg — it falls through to the `Recovery` halt backstop (fail-safe). So it degrades recovery success, not safety.
  - Suggested fix: thread the pmus tick onto `Position` (or pass the `LivePair`) and quantize the flatten too. Left as a precise inline TODO; deferred to avoid rippling `Position`'s shape for a market shape not yet observed (all live pmus ticks seen are 0.001, finer than a cent → no-op).

### INFO (optional improvements / simplifications)
- `remainingQty` is retained as a defensive alias in `pmus_order_filled` though the OpenAPI schema only defines `leavesQuantity` — harmless, costs one `.or_else`. Kept for robustness against an undocumented body.
- The two recovery spawns (cancel + flatten) are independent tasks; if desired they could be one task that awaits the cancel before the flatten, but concurrent is correct (different orders, no shared state) and lower-latency.

## Checks Passed
- pmus fill fields verified against the AUTHORITATIVE OpenAPI orders-schema.json (not the task prompt alone): cumQuantity/leavesQuantity/quantity = number, state enum incl. ORDER_STATE_FILLED/PARTIALLY_FILLED, executions[].lastShares = string, GetOrderResponse `{order:{..}}` wrapper. Absence/ambiguity -> `filled:false` (fail-safe, the `pattern_rest_envelope_probe` corollary).
- FIX B fill criteria only ever return `true` early; the final fallthrough is the only `false` path -> a partial (cumQuantity<q) under one criterion cannot mask a full-fill under another. Realistic-body tests cover fully-filled→true, accept-0→false, partial→false, wrapped, both string+number quantities.
- FIX A book lookups key correctly (Kalshi ticker / pmus slug = `filled_leg.market`); `exit_price` matches the postpone-unwind exit (YES→bid, NO→1-ask).
- FIX A dry-run safety: dry-run `submit_pair` is simulated+filled → `both_filled` true → recovery branch never reached in dry-run; `naked_filled_idx` excludes simulated legs (test pinned).
- FIX C fail-safe direction: sub-min pmus size → skip whole pair (no naked single leg); quantize can round a BUY up by ≤½ tick but `realized_edge_clears_floor` runs AFTER on the rounded prices and skips a below-floor pair; `cents()` range check still rejects a quantized 0/≥100c price.
- No gate weakened: `EXECUTION_MODE` dry-run default, `pmus_post_signing_verified`, prod-consent, settle-clean gate all unchanged; new `submit`/`run_one` enforce the same keys-absent + pmus-signing gates as `submit_pair`.
- rustls only, no new deps; `cargo build` + `cargo test` (123 pass) + `cargo clippy --all-targets` all clean; offline `--smoke` runs (gates fire, postpone unwind path intact).
- Project invariants: no false-positive join logic touched (matcher untouched); settlement-identity gates intact; per-leg venue-native market ids preserved on the recovery SELL.

## Launch Recommendation
PROCEED WITH FIXES — the CRITICAL was self-resolved within the run and is regression-tested; the WARN is a fail-safe-degraded recovery-success gap with an explicit TODO, not a safety hole.

## Self-review caveat
This is an authorship self-review and DID surface a CRITICAL my own first design missed — evidence that an independent pass has value. This change feeds the live order path (decision 0015) behind the safe-by-default rails; before an ARMED (live+prod) pilot the owner should want an independent review of `recover_naked_leg` + the `Recovery` outcome arm, and the recovery SELL path is exercised live for the first time on the first real half-fill (the SELL_* intents remain doc-derived, not yet live-probed).
