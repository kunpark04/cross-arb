---
from: coding-agent
run_id: 20260611-0925
timestamp: 2026-06-11T09:25:19Z
task_summary: Built the testable core of stage-2 for the Rust bot — order book, signal/fee port with a Python-parity test, and the co-listed matcher.
files_changed: [bot-rs/src/main.rs]
files_created: [bot-rs/src/book.rs, bot-rs/src/signal.rs, bot-rs/src/matcher.rs]
context_loaded: [CLAUDE.md, tasks/lessons.md, bot/ledger.py, bot/kalshi_book.py, bot/colisted_map.py, bot/monitor.py, bot-rs/src/types.rs, bot-rs/src/ledger.rs, bot-rs/src/main.rs, ~/.claude/agent-memory/coding-agent/feedback_threshold_ge_vs_gt.md, ~/.claude/projects/.../memory/MEMORY.md]
libraries_consulted: []
self_review_artifact: tasks/_agent_bus/20260611-0925/code-logic-reviewer.md
---

## What I changed
- `bot-rs/src/signal.rs` — port of `ledger.py::signal` (two `Book`s -> `Edge{net,dir}` net of marginal fees), reusing `ledger::{marginal_taker_fee, order_taker_fee_cents, *_COEF}` so fee constants stay single-source. MANDATORY parity test reproduces ledger.py's S6 fee vectors (Kalshi 100@.5=175c, pmus=125c, marginal=1.75c/1.25c) and S1/S2 edge sign+dir; all cross-checked against live `python bot/ledger.py` output.
- `bot-rs/src/book.rs` — `KalshiBook` (BTreeMap-on-tick, O(1)-best snapshot+delta merge, port of kalshi_book.py), `PmusBook` (snapshot ladders), and `depth_at_edge(k, pm, dir) -> Depth{c2,c1,c0}` two-pointer walk (port of monitor.py `depth_curve` + `MarketTracker._depth`). Depth vectors match monitor.py exactly: (30,30,30), (50,50,50), (8,8,8).
- `bot-rs/src/matcher.rs` — pure join logic: weather bounds-equality (`pm_bounds`/`k_bounds` canonicalized to inclusive `[lo,hi]`, L17), econ grid-step twin (`econ_twin` floor=T-step, L21), sports abbrev binding to two distinct tickers. Returns `ColistedMatch{cat,kalshi,kalshi_b,cluster,settle_clean,days_to_event}`. Hand-rolled slug parser (no regex dep) pinned against colisted_map.py on adversarial digit-width cases.
- `bot-rs/src/main.rs` — added `mod book; mod matcher; mod signal;` (kept alphabetical).

## Why (non-obvious only)
- DIRECTION MAPPING: Python `dir "P"` (YES@pmus+NO@Kalshi) == Rust `Dir::PK`; Python `"K"` == `Dir::KP`. A flip here silently inverts every trade, so it is pinned by three parity tests against live ledger.py output (s1->PK net +0.0326, flip->KP, s2->KP no_arb).
- `settle_clean`: weather=true (empirically verified); sports/econ=false (recon still open) — mirrors the project's empirical settlement-identity status, not a guess.
- I extended `ledger.rs`'s deferred parity test (its header explicitly listed it as a stage-2 TODO) rather than duplicating fee code.

## Test result
`cargo test` = 42 passed (was 22; +20 new across signal/book/matcher). Zero compiler warnings.
