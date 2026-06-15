---
from: coding-agent (self-review pass)
run_id: 20260615-1320
timestamp: 2026-06-15T13:25:00Z
scope_reviewed: [bot-rs/src/bookkeeping.rs:~325-355 (is_definite_not_filled), bot-rs/src/bookkeeping.rs:~183-210 (AmbiguousAbort arm), bot-rs/src/bookkeeping.rs:~470-500 (recover_naked_leg ambiguous-leg gate), bot-rs/src/flatten.rs (whole), bot-rs/src/main.rs:~14,~78-92 (module + dispatch)]
critical_count: 0
warn_count: 2
info_count: 2
launch_recommendation: PROCEED WITH FIXES
self_review: true
cross_references: [decisions/0020-pmus-first-serial-plus-recovery-cost-gate.md, decisions/0021-fill-or-kill-orders-and-churn-cooldown.md, decisions/0024-partial-fill-flatten-any-non-clean-lock.md, tasks/lessons.md L34/L36/L37]
---

## Goal Understanding

Two money-path changes: (1) the entry fail-close should AUTO-FLATTEN (self-heal) when the unfilled leg is an
UNMISTAKABLE venue rejection (`409 fill_or_kill_insufficient_resting_volume` / 4xx) — the order definitively did
not fill, so the other (filled) leg is safely naked — while still HALTING on any genuinely ambiguous error (the
cardinal-sin guard: never un-hedge a possible lock). (2) A surgical `--flatten` one-shot CLI to manually SELL a
single named naked leg, dry-run-safe, gated like the live path.

## Scope Reviewed
- `bookkeeping.rs` `is_definite_not_filled` — new Err classifier (the cardinal boundary).
- `bookkeeping.rs` `recover_naked_leg` ambiguous-leg gate — the primary 409 fix site (reject lands here).
- `bookkeeping.rs` `apply_outcome` `EntryMiss::AmbiguousAbort` — the symmetric pmus-hedge-rejected abort path.
- `flatten.rs` — parse + build-sell + one-shot run + book read.
- `main.rs` — module decl + dispatch placement (after the prod-consent gate).

## Findings

### CRITICAL (must fix before launch)
- None.

### WARN (fix or justify)
- **Self-resolved within run — `AmbiguousAbort` read the wrong Err on slot-order.** First draft used
  `find_map(.err())` which could return the `HedgeNotFilled` sentinel instead of the real pmus hedge error
  (both legs are `Err` in the abort case), making the definite/ambiguous classification depend on a/b order.
  Fixed to `filter_map(.err()).find(!HedgeNotFilled)`; added `pmus_first_abort_clean_on_definite_reject_halts_on_ambiguous`
  that asserts both sentinel slots. Status: resolved + tested.
- **`--flatten` book read has no settle-clean / staleness gate; it trusts the operator.** By design — it is a
  manual reduce-only close of a KNOWN naked leg, and it only ever fires a SELL (can only reduce). The 10s
  book-frame timeout + the one-sided-book → fire-nothing guard are the safety rails. Justified: a flatten must
  work even on a thin/stale book (that is exactly when a leg is stranded); gating it like an entry would defeat
  its purpose. The SELL is floored to a marketable cent so it crosses. Documented in the module header.

### INFO (optional improvements / simplifications)
- `read_one_book` aborts the stream task on return; a slightly cleaner shutdown would send a close frame, but
  `abort()` is fine for a one-shot that exits the process immediately after. Not worth the complexity.
- `flatten.rs` leaves `pm_min_tick: None` (cent granularity) rather than discovering the pmus per-market tick.
  Safe because a SELL floors and live pmus ticks (0.001) leave whole cents valid (per README). If a future
  coarse-tick pmus market is flattened, the cent-floored price could (rarely) sit one coarse-tick inside the
  bid; the SELL still rests-or-fills marketably (a SELL floor never crosses the wrong way). Acceptable for a
  manual tool; noted.

## Checks Passed
- **Err-classification boundary — no ambiguous error leaks into auto-flatten.** 5xx, `transport:`, panicked,
  `RateLimited` (timeout), `KeysUnavailable`/`LiveDisabled`/`TransportNotWired` all return FALSE (halt). Only a
  4xx / FOK-named body returns TRUE. Asserted exhaustively in `is_definite_not_filled_classifies...` and
  end-to-end in `outcome_409_fok_reject_recovers_while_transport_err_still_halts` (transport → halt) and the
  abort test (transport/rate-limit → halt). The cardinal sin (un-hedging a real lock) is structurally avoided.
- **0024 partial-fill flatten unchanged.** The `naked_filled_indices` / per-leg `fill_qty` unwind and the
  atomic price-all-then-fire path are untouched; `partial_pmus_fill_recovers...` and `both_legs_partial...`
  still green. A non-zero partial still flattens.
- **Existing halt paths preserved.** `naked_leg_failclose`, the Recovery-SELL-didn't-fill halt, and the W-1
  unwind-slot fail-close are untouched; an unpriceable book still halts (`outcome_naked_live_leg_engages_halt...`).
- **`HedgeNotFilled` sentinel semantics intact** — still the one KNOWN-no-order exception that continues.
- **`--flatten` fires EXACTLY one SELL on the named leg** — `build_flatten_sell_prices_one_marketable_sell`
  pins action=Sell, exact venue/market/side, exact qty, one-sided → none. `dry_run_flatten...` proves it sends
  nothing in dry-run (simulated ack). Coid namespaced `flatten-…` so it can't collide with entry/recovery coids.
- **Prod-consent gate inherited** — the `--flatten` dispatch sits after `main`'s live+prod hard gate; a
  malformed request exits(2).
- **`cargo test` 175 passed / 0 failed; `cargo clippy --all-targets` clean.**

## Launch Recommendation
PROCEED WITH FIXES — the one slot-order WARN was self-resolved + tested within the run; the remaining WARN/INFO
are justified design choices for a manual reduce-only tool.

## Self-review caveat
This is authored-then-self-reviewed code on a money path that has already produced two live naked-position
incidents (0021/0024). The Err-classification boundary is the single highest-risk line — a wrongly-widened
`is_definite_not_filled` could un-hedge a real lock. I have constrained it to 4xx/FOK-named bodies only and
tested the ambiguous side, but an INDEPENDENT `code-logic-reviewer` pass (and a DEMO observation of a real FOK
reject before live re-arm) is warranted before this self-heal is trusted with real money, per the 0021/0024
"verify venue BEHAVIOUR live, not just enum/logic" discipline.
