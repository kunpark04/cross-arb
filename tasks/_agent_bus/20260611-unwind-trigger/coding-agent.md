---
from: coding-agent
run_id: 20260611-unwind-trigger
timestamp: 2026-06-11T12:45:51Z
task_summary: Arm the live MLB postponement-unwind trigger in bot-rs (held-position tracking + statsapi detection + SELL-both-legs firing), reusing the tested unwind.rs decision/orders.
files_changed: [bot-rs/src/main.rs, bot-rs/src/config.rs, bot-rs/src/discovery.rs, bot-rs/src/exec.rs, bot-rs/src/risk.rs]
files_created: [bot-rs/src/postpone.rs]
context_loaded: [tasks/lessons.md, scripts/probe_mlb_postpone.py, bot-rs/src/unwind.rs, bot-rs/src/types.rs, bot-rs/src/main.rs, bot-rs/src/config.rs, bot-rs/src/exec.rs, bot-rs/src/discovery.rs, bot-rs/src/venue.rs, bot-rs/src/book.rs, bot-rs/src/risk.rs, bot-rs/Cargo.toml]
libraries_consulted: []
self_review_artifact: tasks/_agent_bus/20260611-unwind-trigger/code-logic-reviewer.md
---

## What I changed
- New `bot-rs/src/postpone.rs`: the pure DETECTOR (`GameStatus`, `snap`, `detect_postponement`, `days_between`) — a faithful port of `probe_mlb_postpone.py`'s `snap`/`unwind_trigger`, SPLIT so the UNWIND/WATCH cutoff is delegated to the existing `unwind::should_unwind`; plus the I/O poll `poll_mlb_postponements` (statsapi teams+schedule, owner-droplet, NOT on any test path).
- Ported ALL Python `_selftest` vectors as Rust parity tests, incl. the L3 trap (gap from the BOUND event_date, never officialDate): the 5d-out UNWIND and the live officialDate-already-moved-104d UNWIND both assert `reschedule_in_days` explicitly (`postpone.rs` tests).
- `config.rs`: added `postpone_poll_s` (env `POSTPONE_POLL_S`, default 60) + `auto_unwind` (default true; `CROSSARB_NO_AUTO_UNWIND=1` disables); wired into `from_env`, `test_default`, and the inline test Config literals in `exec.rs`/`risk.rs`.
- `main.rs`: `HeldPosition` map keyed by pmus slug; on a both-filled entry builds a `Position` from the two `OrderIntent`s and tracks it + BUMPS exposure (per_pair/cluster/total/open_positions) so caps bind across the session (`track_position`); spawns `poll_mlb_postponements` gated on `cfg.auto_unwind`; converted the event loop to `tokio::select!` over the venue `rx` AND a new `unwind_rx`.
- `main.rs` `handle_unwind`: prices each leg's exit from the live books (SELL YES -> `yes_bid`; SELL NO -> `1 - yes_ask`), fires the two SELLs via `unwind::unwind_orders` -> `submit_pair`, removes the position + decrements exposure on both-filled; one-sided book -> WARN + hold (poll re-emits; idempotent `unwind-…` coids). REDUCE-ONLY: fires even under the kill-switch (logs the note), dry-run-safe.
- `main.rs` `smoke` (5): now demonstrates the LIVE composition offline on a synthetic schedule game (`snap -> detect_postponement -> should_unwind -> unwind_orders`) — prints the two SELL legs, no network.
- `discovery.rs`: `iso_date`/`pm_league` made `pub(crate)` (reused by `track_position` to derive league/date/abbrevs for the poll).

## Why (non-obvious only)
- `unwind_tx` is held alive for the loop's lifetime (`_unwind_tx_keepalive`) instead of dropped: with `auto_unwind=false` no poll task holds a sender, so dropping ours would close `unwind_rx`, and its `select!` arm would return `None` every iteration -> busy-loop. Holding the sender parks `recv()` when idle. (Self-review CRITICAL caught + fixed within run.)
- `detect_postponement` returns `Some` for BOTH the UNWIND and WATCH cases (the `reschedule_in_days` value carries the distinction via `should_unwind`), and `None` only for the truly-normal life-cycle — this is what reproduces `unwind_trigger`'s 3-valued action under the split.

## Verification
- `cargo test`: 98 passed (80 baseline + 18 new), 0 failed. `cargo build`: 0 warnings. `cargo clippy`: `postpone.rs` clean; the 5 remaining lints are all pre-existing (matcher.rs/unwind.rs untouched; main.rs:6-7 + risk.rs:88 pre-existing).
- `cargo run -- --smoke` prints the detected postponement (`reschedule_in_days=Some(5.0)` -> UNWIND) and the two SELL unwind orders (YES@Pmus + YES@Kalshi-B), confirming the L3 event_date gap and the full offline composition.
