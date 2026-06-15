---
from: coding-agent
run_id: 20260615-1320
timestamp: 2026-06-15T13:20:00Z
task_summary: Refine the entry-path fail-close so a DEFINITE venue rejection (409 FOK-insufficient / 4xx) auto-flattens the naked leg instead of halting, and add a surgical `--flatten` one-shot CLI to sell a single named naked leg.
files_changed: [bot-rs/src/bookkeeping.rs, bot-rs/src/main.rs]
files_created: [bot-rs/src/flatten.rs]
context_loaded: [CLAUDE.md, bot-rs/README.md, decisions/0020-pmus-first-serial-plus-recovery-cost-gate.md, decisions/0021-fill-or-kill-orders-and-churn-cooldown.md, decisions/0024-partial-fill-flatten-any-non-clean-lock.md, tasks/lessons.md (L34/L35/L36/L37), bot-rs/src/exec.rs, bot-rs/src/pricing.rs, bot-rs/src/types.rs, bot-rs/src/config.rs, bot-rs/src/pair.rs, bot-rs/src/live.rs, bot-rs/src/probe.rs, bot-rs/src/venue.rs, bot-rs/src/book.rs, bot-rs/src/discovery.rs, bot-rs/src/exec_log.rs]
libraries_consulted: []
self_review_artifact: tasks/_agent_bus/20260615-1320/code-logic-reviewer.md
---

## What I changed

- **Change 1 — 409-auto-flatten refinement** (`bookkeeping.rs`):
  - New pure classifier `is_definite_not_filled(&ExecError) -> bool` (`bookkeeping.rs:~330`): TRUE only for an
    unmistakable venue REJECTION — a `Rejected` body with a leading HTTP **4xx** status (the live `409
    fill_or_kill_insufficient_resting_volume`), or a body naming `fill_or_kill`/`insufficient`/`rejected`.
    FALSE (ambiguous → halt) for 5xx, `transport:`/panicked, `RateLimited` (timeout), and the
    never-sent/sentinel variants. Conservative by design: only a proven no-fill returns true.
  - `recover_naked_leg`'s ambiguous-unfilled-leg check (`bookkeeping.rs:~470`): an unfilled `Err` that
    `is_definite_not_filled` is now treated as a CLEAN miss → CONTINUE to auto-flatten the naked leg (no halt);
    a genuinely ambiguous `Err` still FAILS CLOSED (return false → caller halts). `HedgeNotFilled` exception unchanged.
  - `apply_outcome`'s `EntryMiss::AmbiguousAbort` arm (`bookkeeping.rs:~183`): a pmus-first abort whose pmus
    hedge ERR'd with a DEFINITE rejection is now a CLEAN abort (no position, no halt — a killed FOK order never
    rested, nothing to cancel); a genuinely ambiguous hedge Err still halts. Reads the REAL hedge error (skips
    the `HedgeNotFilled` sentinel), so the classification is slot-order-independent.
  - Tests: rewrote `errd_unfilled_leg_fails_closed...` → `errd_unfilled_leg_fails_closed_or_recovers_by_err_kind`
    (transport/5xx → halt; 409 → recover; Ok-resting → recover); added `is_definite_not_filled_classifies...`
    (exhaustive boundary table), `outcome_409_fok_reject_recovers_while_transport_err_still_halts` (end-to-end
    `apply_outcome` routing), `pmus_first_abort_clean_on_definite_reject_halts_on_ambiguous` (abort path, sentinel
    on either slot).

- **Change 2 — surgical `--flatten` one-shot CLI** (`flatten.rs` + `main.rs`):
  - `cross-arb-bot --flatten <pmus|kalshi> <market> <yes|no> <qty>`: connects ONLY the target venue's WS (same
    `VenueCreds`/`*_stream` auth path the live loop uses), reads ONLY that market's first book frame (10s
    timeout), prices ONE marketable SELL via the recovery pricer `flatten_exit_cents`, fires via `backend.submit`
    (dry-run prints + sends nothing; live sends), logs the ack + fill, exits. NO discovery, NO loop, NO other orders.
  - Wired into `main.rs` after the prod-consent hard gate (inherits the same `CROSSARB_I_UNDERSTAND_PROD` guard);
    a malformed request exits(2) loud.
  - Tests: `parse_flatten...` (well-formed + garbage), `build_flatten_sell...` (exactly one correctly-priced SELL,
    one-sided book → none), `dry_run_flatten...` (one simulated SELL, sends nothing).

## Why (non-obvious only)

- The live incident's reject lands on the SECOND-fired (Kalshi) leg under pmus-first, so the 409 routes through
  `recover_naked_leg` (the unfilled "other" leg), not the pmus-first abort arm — that recover path was the primary
  fix site. The abort-arm refinement covers the symmetric case (pmus hedge itself definitively rejected).
- `--flatten` reads the pmus book via a brief single-market WS subscribe (pmus has no REST book in this codebase),
  reusing the tested stream + auth instead of writing a new signed REST client.

## Verification

- `cargo test`: 175 passed, 0 failed (was 169; +6 new test fns).
- `cargo clippy --all-targets`: clean, 0 warnings.
- Did NOT commit (per instruction). Did NOT update the README test-count line (stale already at 147/169; out of scope).
