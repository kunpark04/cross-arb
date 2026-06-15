---
from: coding-agent
run_id: 20260614-wc-dutchbook
timestamp: 2026-06-14T05:00:17Z
task_summary: Build the cross-venue 3-leg DUTCH-BOOK arb in bot-rs (full WC game) alongside the existing per-outcome binary arbs, as a parallel path leaving the 2-leg core byte-unchanged.
files_changed: [bot-rs/src/types.rs, bot-rs/src/signal.rs, bot-rs/src/exec.rs, bot-rs/src/risk.rs, bot-rs/src/unwind.rs, bot-rs/src/discovery.rs, bot-rs/src/main.rs]
files_created: []
context_loaded: [bot-rs/README.md, bot-rs/src/*.rs (all 15), tasks/lessons.md (L1-L29), CLAUDE.md, memory/MEMORY.md, tasks/_agent_bus/20260614-wcrust/code-logic-reviewer.md, C:/Users/kunpa/.claude/agent-memory/coding-agent/MEMORY.md]
libraries_consulted: []
self_review_artifact: tasks/_agent_bus/20260614-wc-dutchbook/code-logic-reviewer.md
---

## What I changed
- `types.rs:121+` — additive 3-leg types: `OutcomeQuote`, `OutcomeTag` (A/D/B), `SoccerTriple` (live quote: 3 outcomes' dual-venue books + basket depth), `TripleOutcome`, `TriplePosition` (`[PositionLeg;3]`). No 2-leg type touched.
- `signal.rs:151+` — `dutch_book(&[OutcomeQuote;3], void_tail) -> DutchSignal`: per-outcome cheapest YES ask across venues, `basket_cost`=sum, `net=(1-basket)-Σfees-void_tail`, `no_arb` on net≤0/unpriceable/crossed. Reuses `pmus_marginal_fee`/`kalshi_marginal_fee`/`round4`.
- `exec.rs` — `TripleAck{legs:[Result<Ack>;3]}` + `all_filled()`; new `submit_triple(a,b,d)` trait method on both backends (DryRun logs 3; Live fires 3 CONCURRENTLY via `tokio::join!` in `run_triple`, keys-absent→`KeysUnavailable`). `submit_pair`/`PairAck` UNCHANGED.
- `risk.rs:271+` — `evaluate_triple(cfg,&SoccerTriple,&DutchSignal,exp,affordable) -> ApprovedTriple`: mirrors `evaluate` gates (kill/pause/settle/proximity/edge-sign+floor+rate/caps/concurrency), `cost_per=basket_cost`, `size=min-of-3 depth`, per-outcome crossed+stale book sanity. `evaluate` UNCHANGED.
- `unwind.rs:53+` — `triple_unwind_orders(&TriplePosition,[u8;3])` SELLs each of the 3 held YES legs (`unwind3-<game>-{0,1,2}` coids). `unwind_orders` UNCHANGED.
- `discovery.rs` — `TripleSpec`/`TripleOutcomeSpec` + `Discovery.triples`; soccer3 branch ALSO emits one `TripleSpec` per WC game (A/draw/B → slug+ticker), coexisting with the 3 per-outcome `Pair`s. Skips the triple if any sibling is missing (no partial basket).
- `main.rs` — `TripleMeta`/`TripleLeg` (static routing) + `TripleState` (game-index over all 6 per-outcome keys; books are the SAME ones the per-outcome pairs already subscribe — no extra subscription); `build_quote` (live 6-book quote + correct `basket_depth` 3-pointer walk that stops at the lock boundary); `build_triple` ([OrderIntent;3], each outcome's YES on its cheapest venue, `xarb3-<game>-<tag>`); `evaluate_triple_frame` (per-frame route→price→gate→W6 re-check→reserve→spawn); `spawn_submit_triple`/`apply_triple_outcome`; **`recover_naked_basket`** (flatten EVERY filled leg, fail-close on any unpriceable filled leg); `triple_naked_failclose`; reserve/release exposure. Wired into `run_live` (shared `triple_state` rebuilt by `refresh_loop`, `triple_rx` select arm) + a smoke case (`report_triple`).

## Why (non-obvious only)
- **A `SoccerTriple` needs no new WS subscription**: its 3 pmus slugs + 3 Kalshi tickers are already tracked as the 3 per-outcome binary `Pair`s (e50ecbb), so the basket only adds a game-routing index + reads the live books. This is the smallest design that leaves the 2-leg path untouched.
- **`basket_depth` is a 3-pointer ladder walk**, not a per-leg sum: a naive sum over-states depth (deep levels push the marginal basket past $1). The walk mirrors `book::depth_curve` and stops when `1 - Σ(3 levels) < 0`.
- **`recover_naked_basket` decomposes into N independent single-leg flattens** reusing the proven 2-leg `spawn_flatten`/`spawn_cancel`/`flatten_exit_cents` (each routes back as a `SubmitKind::Recovery` that halts if its SELL fails). A filled leg already flattening for a DIFFERENT position fails-closed (size-mismatch can't be trusted).

## Verify
- `cargo test --manifest-path bot-rs/Cargo.toml` → **161 passed; 0 failed** (135 baseline UNCHANGED + 26 new).
- `cargo clippy --all-targets` → **0 warnings** (matches README baseline).
- `cargo run -- --smoke` Dutch-book case: `basket_cost=0.92 (<$1), net=4.1c, legs A@Pmus 0.42, D@Kalshi 0.20, B@Pmus 0.30`; dry-run fires 3 YES legs (`xarb3-…-A/D/B`). The WC per-outcome BINARY (`xarb-…`) still fires too — both arbs coexist.
