---
from: coding-agent (self-review pass)
run_id: 20260611-outcome-fix
timestamp: 2026-06-11T19:24:46Z
scope_reviewed: [bot-rs/src/exec.rs:24-210, bot-rs/src/exec.rs:386-589, bot-rs/src/main.rs:658-720, bot-rs/src/types.rs:3-13,143-160]
critical_count: 0
warn_count: 2
info_count: 2
launch_recommendation: PROCEED
self_review: true
cross_references: [tasks/_agent_bus/20260611-rust-review/, tasks/_agent_bus/20260611-engine-review/, ~/.claude/agent-memory/coding-agent/pattern_rest_envelope_probe.md]
---

## Goal Understanding
The bot was treating an order-create HTTP 2xx as proof the leg FILLED. Verified against venue docs that
is false (pmus matching is async; a 2xx is acceptance, fills land on the order stream / require
`synchronousExecution`; Kalshi reports fills synchronously incl. a `resting` non-fill). The change must
(1) distinguish FILLED from ACCEPTED per venue so an accepted-but-resting leg is routed down the naked-leg
path not recorded as a hedge, (2) stop implying pmus idempotency (no clientOrderId), (3) make `cancel`
functional + persist the venue order id. Compile + unit-test only; keep 112 green; safe-by-default intact.

## Scope Reviewed
- exec.rs — `Ack.filled`, `both_filled()`, `kalshi_order_filled`/`pmus_order_filled`, pmus payload sync flags + no coid, `CancelTarget` + `cancel_request`/`build_cancel`/`cancel_one`/`run_cancel`, `LiveBackend::cancel`.
- main.rs — `apply_outcome` (persist venue_order_id on both-filled entry), `naked_leg_failclose` (filled-aware predicates), `position_from_intents` (default id), test ack builders.
- types.rs — `OrderIntent::client_order_id` doc, `PositionLeg.venue_order_id` (+`Default` on PositionLeg/Venue/Side), `unwind.rs` test literals.

## Findings

### CRITICAL (must fix before launch)
- none.

### WARN (fix or justify)
- **pmus fill detection relies on a partly-unverified response envelope.**
  - Location: `exec.rs pmus_order_filled` (branches 1–4).
  - Issue: context7 docs show the *documented* single-create success body as `{orderId,intent,outcomeSide,action}` with NO inline `executions[]`; the task asserts `synchronousExecution:true` returns `executions[]`/`cumQuantity`/`remainingQty`. The exact field names are not all doc-confirmed for the synchronous create body (fills are documented on the ORDER STREAM, field `lastShares`).
  - Why it matters: a guessed envelope key fails SILENTLY (`.get("cumQuantity")`→absent), which here is the SAFE direction — absence → `filled:false` → naked-leg fail-close (halt), never a falsely-recorded hedge. So the failure mode is conservative, not catastrophic. But it could make a genuinely-filled pmus leg read as not-filled and trip the halt unnecessarily.
  - Suggested fix: a live pmus synchronous-create probe (owner env) to pin the actual fill fields, then narrow the parser. TODO already in code + handoff.
  - Status: reported (defensive multi-field parse + explicit TODO landed; live confirmation deferred to owner env — cannot probe here).
- **`pmus_order_filled` branch-4 status match is a loose substring.**
  - Location: `exec.rs pmus_order_filled` (terminal-status fallback).
  - Issue: `status.contains("FILL") && !status.contains("PARTIAL")` would also match a hypothetical "UNFILLED". The documented enum (NEW/PARTIAL_FILL/FILL/CANCELED/REPLACE/REJECTED/EXPIRED/DONE_FOR_DAY) contains no such value, so it is safe against the documented set; it is a fallback after the quantitative branches 1–3.
  - Why it matters: low — only a fallback, and safe against documented values.
  - Suggested fix: pin the exact terminal-status field/value when the live probe runs; otherwise leave (don't over-fit a guessed field).
  - Status: reported; left as-is intentionally.

### INFO (optional improvements / simplifications)
- `naked_leg_failclose` and `both_filled` both encode "actually filled live" — could share one predicate helper. Left inline (two call sites, different "other-leg" logic) to avoid an indirection for 2 lines.
- `CancelTarget.market` is unused for Kalshi (only pmus needs the slug). A per-venue enum payload would be "cleaner" but adds a type for no behavioural gain — kept the single struct.

## Checks Passed
- **Dry-run safety preserved**: DryRun ack `filled:true` → `both_filled` stays true in dry-run; smoke run records both legs + gates + unwind exactly as before (ran `cargo run -- --smoke`).
- **both_filled truth table**: both-filled→hedge; one-resting→not; both-resting→not; one-errored→not (unit-tested `both_filled_requires_both_legs_actually_filled`).
- **Naked-leg path completeness**: A filled-live + B resting → halt + no position + reservation released (new test `outcome_entry_one_resting_leg_is_naked_not_a_hedge`); A filled-live + B err → halt (existing); both err → release, no halt (existing); simulated partial → no halt (existing). All four combos covered.
- **venue_order_id persistence**: both-filled entry copies ack.a→legs[0], ack.b→legs[1] (new test `outcome_entry_persists_venue_order_ids_on_legs`); positional mapping verified against `position_from_intents` build order.
- **cancel per-venue request**: Kalshi DELETE by id-in-path + empty body; pmus POST /v1/order/{id}/cancel + `{marketSlug}` body; signing path mirrors URL path; KeysUnavailable when keys absent (new test `cancel_builds_correct_per_venue_request`). Matches context7 cancel-order schema.
- **pmus payload**: `synchronousExecution:true`, `maxBlockTime` is a STRING (schema-correct), NO `clientOrderId` (unit-tested); maxBlockTime floors `leg_fill_timeout_ms=500`→0s→min 1s (guarded).
- **Kalshi unchanged where live-verified**: `"type":"limit"` kept; TIF still omitted; NO-leg `no_price` mapping untouched; escaping (serde_json) untouched.
- **No external callers broken**: `.cancel(` only in exec impls + new tests; `both_filled`/`Ack` only consumed by exec.rs + main.rs (grep-confirmed).
- **No signing/market-data/discovery touched** (per instruction): auth.rs, book/venue parsers, discovery unchanged.
- **Edge cases**: number-or-string `fill_count` ("0.00"); empty `executions[]`; absent fields; partial fill (count < requested) → not-filled — all unit-tested.
- 118/118 tests pass; clippy --all-targets clean.

## Launch Recommendation
PROCEED — the change is conservative by construction (any fill-detection ambiguity resolves to
not-filled → naked-leg fail-close/halt, never a falsely-recorded hedge), dry-run behaviour is byte-unchanged,
and the suite + clippy are green. The two WARNs are "confirm the pmus synchronous-create fill fields live in
the owner env, then narrow the parser" — they do not block compile/test/dry-run and fail safe.

## Self-review caveat
I authored this diff, so authorship bias applies — I may under-weight the pmus-envelope WARN because I
"reasoned about it." This is real-money order-outcome code on the live path: an owner-env live pmus
synchronous-create probe to pin the actual fill fields, plus a SELL_*/cancel live verification, is warranted
before arming pmus at >1 contract. An independent review (the now-merged code-logic-reviewer axis, or owner
read) of `pmus_order_filled` specifically would be prudent.
