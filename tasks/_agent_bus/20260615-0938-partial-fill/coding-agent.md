---
from: coding-agent
run_id: 20260615-0938-partial-fill
timestamp: 2026-06-15T09:38:32Z
task_summary: Make a pmus PARTIAL fill a visible naked position that the bot unwinds at the exact filled qty, instead of vanishing as filled:false.
files_changed: [bot-rs/src/types.rs, bot-rs/src/exec.rs, bot-rs/src/bookkeeping.rs, bot-rs/src/pricing.rs, bot-rs/src/unwind.rs, bot-rs/src/probe.rs]
files_created: []
context_loaded: [cross-arb/CLAUDE.md, bot-rs/README.md, tasks/lessons.md, bot-rs/src/exec.rs, bot-rs/src/bookkeeping.rs, bot-rs/src/types.rs, bot-rs/src/pricing.rs, bot-rs/src/pair.rs, bot-rs/src/test_support.rs, bot-rs/src/exec_log.rs, bot-rs/src/probe.rs, bot-rs/src/unwind.rs]
libraries_consulted: []
self_review_artifact: tasks/_agent_bus/20260615-0938-partial-fill/code-logic-reviewer.md
---

## What I changed
- `exec.rs`: added `Ack.fill_qty: f64` (the ACTUAL filled quantity, fail-safe `0.0`); `filled` is now derived as `fill_qty >= qty`. Refactored the fill detectors into `kalshi_fill_qty(v)->f64` / `pmus_fill_qty(v)->f64` (return the exact, possibly fractional, filled qty; `*_order_filled` = `fill_qty >= qty`). `post_leg` computes `(fill_qty, filled)` and sets both on the `Ack`; dry-run acks carry the requested qty.
- `types.rs`: added `OrderIntent.frac_qty: Option<f64>` — when `Some`, the venue payload sends EXACTLY that fractional quantity (for a partial-fill recovery SELL). `None` on every entry/unwind leg → byte-identical integer payload.
- `exec.rs` payload builders: `build_kalshi_payload`/`build_pmus_payload` emit `frac_qty` as the `count`/`quantity` when `Some`, else the integer `qty` unchanged.
- `bookkeeping.rs`: `naked_filled_idx` now treats ANY live non-zero fill (`fill_qty > 0`, FULL **or** PARTIAL) as a real naked position. `classify_entry_miss` routes a partial pmus hedge (`Ok`, `!filled`, `fill_qty>0`) to `NakedOrOther` (recover/unwind), NOT `CleanAbort` (which would abandon the partial). `recover_naked_leg` sizes the recovery SELL to the filled leg's exact `fill_qty` via `frac_qty` (integer `qty` is a ceil≥1 fallback), and the ambiguous-Err fail-close now EXCLUDES the `HedgeNotFilled` sentinel (a deliberately-unsent leg has a KNOWN fate → proceed to flatten the partial; only a genuine transport/reject Err halts). See `recover_naked_leg` bookkeeping.rs ~line 384.
- `pricing.rs`/`unwind.rs`/`probe.rs` + ~25 test literals: `frac_qty: None` on whole-share intents.

## Why (non-obvious only)
- `HedgeNotFilled` had to be carved OUT of the 2026-06-15 ambiguous-Err fail-close: under pmus-first a partial pmus fill leaves the Kalshi leg as `Err(HedgeNotFilled)`, which is a KNOWN "never sent" fate, not an ambiguous "may have landed". Without this carve-out the partial would only halt (safe but not the auto-unwind the task wants); the test `partial_pmus_fill_recovers_sized_to_fill_qty_not_intent` pins it.
- Kept `OrderIntent.qty: u32` (changing it to f64 has a 150+-site blast radius across risk/discovery/pricing sizing); a `frac_qty: Option<f64>` override is the minimal way to honor "unwind EXACTLY fill_qty, no rounding".

## Tests added
- `exec.rs`: `pmus_partial_fill_reports_exact_qty_but_not_full` (cumQuantity 0.01 of 5, cum 2 of 5, summed executions; + clean-miss/full-fill unchanged), `kalshi_partial_fill_qty_is_exact_and_failsafe`, `frac_qty_overrides_the_venue_quantity_unrounded`.
- `bookkeeping.rs`: partial-routing assertions in `classify_entry_miss_routes_the_pmus_first_abort`; `partial_pmus_fill_recovers_sized_to_fill_qty_not_intent` (RecordingBackend asserts the SELL's `frac_qty == Some(0.01)`, `qty != 3`).

`cargo test` → 167 passed / 0 failed. `cargo clippy --all-targets` → clean. NOT committed.
