---
from: coding-agent (self-review pass)
run_id: 20260615-0938-partial-fill
timestamp: 2026-06-15T09:40:00Z
scope_reviewed: [bot-rs/src/exec.rs:24-52, bot-rs/src/exec.rs:189-310, bot-rs/src/exec.rs:516-591, bot-rs/src/types.rs:128-150, bot-rs/src/bookkeeping.rs:266-310, bot-rs/src/bookkeeping.rs:348-455]
critical_count: 0
warn_count: 1
info_count: 3
launch_recommendation: PROCEED WITH FIXES
self_review: true
cross_references: [tasks/lessons.md L32, tasks/lessons.md L33, tasks/lessons.md L34]
---

## Goal Understanding
pmus ignores fill-or-kill and runs entries IMMEDIATE_OR_CANCEL, so a `qty=5` order can return `cumQuantity:0.01` (a partial) with the rest expired. The bot's fill detection returned a bool, so a partial read as `filled:false` and the leg was treated as a clean abort — leaving a real ~0.01 naked pmus position untracked (the M'Chich incident, the untracked-naked class 0020/0021 + L33/L34 exist to prevent). The change exposes the actual filled quantity on the `Ack`, treats any non-zero fill (full or partial) as a real naked position, and UNWINDS it at the exact `fill_qty` (never rounded, never the requested intent qty), failing closed if the unwind errors.

## Scope Reviewed
- `exec.rs` `Ack.fill_qty` + `kalshi_fill_qty`/`pmus_fill_qty` refactor + `post_leg` population — in scope.
- `types.rs` `OrderIntent.frac_qty` — in scope (the minimal carrier for an exact fractional unwind; `qty:u32` left untouched to avoid a 150-site blast radius).
- `bookkeeping.rs` `naked_filled_idx` (partial-aware), `classify_entry_miss` (partial → NakedOrOther), `recover_naked_leg` (size to `fill_qty`; `HedgeNotFilled` excluded from the ambiguous-Err halt) — in scope.
- `pricing.rs`/`unwind.rs`/`probe.rs` + test-literal `frac_qty: None` — mechanical, in scope. No adjacent refactors.

## Findings

### CRITICAL (must fix before launch)
- None found.

### WARN (fix or justify)
- Two simultaneous partials are not fully recovered (theoretically). Location: `bookkeeping.rs` `naked_filled_idx` (266-283). If BOTH legs came back `Ok` partial (`fill_qty>0, !filled`), the function returns `Some(0)` and the recovery flattens only leg `a`; leg `b`'s partial is not recovered (though `not_full(b)` is satisfied). Why it matters: a doubly-partial pair would leave one side partially naked. Justification for deferring: UNREACHABLE under the 0020 pmus-first invariant — the Kalshi (second) leg fires ONLY after the pmus leg FULLY fills, and fires FOK (kill-on-miss), so at most one leg can be partial. The other entry points (`both_filled` true, or pmus-first abort) cannot produce two `Ok` partials. Documented here rather than adding dead defensive code; if pmus-first is ever relaxed to concurrent fire this must be revisited.

### INFO (optional improvements / simplifications)
- `exec_log::order_submit` logs `"qty": intent.qty` (the integer fallback, e.g. `1` for a `0.01` recovery SELL), not `frac_qty`. The exact fractional is still present in the logged raw venue response and in the `frac_qty` SELL body, so forensics are intact, but a `jq` on `qty` alone would read `1`. Could add `frac_qty` to the exec-log line for a cleaner audit trail (out of scope; not a safety issue).
- The integer fallback `frac_qty.ceil().max(1.0) as u32` is only consumed by the dry-run/log path; the live payload always uses `frac_qty`. Fine as-is; a comment already states this.
- `pmus_fill_qty` returns `max(cumQuantity, summed executions)`. For the captured live shape both agree; `max` is the safe choice (never under-reports a fill → never under-sells the recovery). No change needed.

## Checks Passed
- A partial (`0 < fill_qty < qty`) is detected as a naked position (`naked_filled_idx` keys on `fill_qty>0`, excludes simulated acks) — pinned by `partial_pmus_fill_recovers_sized_to_fill_qty_not_intent`.
- The recovery SELL sizes to the EXACT `fill_qty` (`frac_qty == Some(0.01)`), never the intent `qty` (would open a 2.99 short) — asserted on the captured SELL.
- Fractional is preserved unrounded end-to-end: `pmus_fill_qty` reads `0.01`; `build_pmus_payload` serializes `"quantity":0.01` — pinned by `pmus_partial_fill_reports_exact_qty_but_not_full` + `frac_qty_overrides_the_venue_quantity_unrounded`.
- A partial does NOT fire the Kalshi leg: under pmus-first a partial pmus → `filled:false` → `run_pair`'s `leg_filled` is false → `HedgeNotFilled` on the Kalshi leg; `classify_entry_miss` routes the partial to `NakedOrOther` not `CleanAbort` — pinned by the augmented classify test.
- FAIL-CLOSED preserved: a genuine transport/reject Err on the unfilled leg still halts (ambiguous fate); only the `HedgeNotFilled` sentinel (known-unsent) proceeds to flatten. The unpriceable-book and failed-recovery-SELL halts (L34) are untouched and still trip via the partial-aware `naked_filled_idx`.
- FAIL-SAFE preserved: an absent/garbage venue body → `fill_qty = 0.0` → never a fabricated fill (the silent-naked-leg axis, L32). `dry-run`/simulated acks excluded from `live_has_fill`.
- BYTE-IDENTICAL entry path: `frac_qty: None` → integer `count`/`quantity` payload unchanged (the existing `"count":1` / `"quantity":2` payload tests still pass).
- `cargo test` 167/0; `cargo clippy --all-targets` clean.

## Launch Recommendation
PROCEED WITH FIXES — the WARN is an unreachable-under-pmus-first defensive note (no code change needed today); the INFO items are optional. No CRITICAL.

## Self-review caveat
This is a money-path change on the untracked-naked-position axis that decisions 0020/0021 and L33/L34 guard; authorship bias is real here. The `HedgeNotFilled`-vs-ambiguous-Err distinction in `recover_naked_leg` is the subtle load-bearing line and warrants an independent `code-logic-reviewer` (or owner) pass before the pmus leg is armed past dry-run — especially to confirm the WARN's "two-partials unreachable" claim against the live pmus-first ordering.
