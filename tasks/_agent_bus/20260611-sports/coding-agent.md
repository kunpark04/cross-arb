---
from: coding-agent
run_id: 20260611-sports
timestamp: 2026-06-11T10:58:13Z
task_summary: Make SPORTS a tradeable 2-outcome category in the Rust bot (game_edge + pick_game ported, leg-market fix), compile + unit-test only.
files_changed: [bot-rs/src/types.rs, bot-rs/src/signal.rs, bot-rs/src/book.rs, bot-rs/src/risk.rs, bot-rs/src/discovery.rs, bot-rs/src/main.rs, bot-rs/src/unwind.rs]
files_created: []
context_loaded: [CLAUDE.md, tasks/lessons.md (L1-L24), tasks/todo.md, bot/monitor.py (game_edge 157-235, GameTracker._depth 210-216, FlipDebouncer), bot/colisted_map.py (pick_game/_match_game 133-158, build_colisted_map sports 304-340, econ 185-258), bot-rs/src/{types,signal,book,risk,discovery,main,venue,matcher,config,ledger,exec}.rs, agent-memory/coding-agent/MEMORY.md (all feedback_/pattern_ entries), tasks/_agent_bus/20260611-parity-review/independent-review.md, tasks/_agent_bus/20260611-rust-review/trading-strategy-review.md]
libraries_consulted: []
self_review_artifact: tasks/_agent_bus/20260611-sports/code-logic-reviewer.md
---

## What I changed
- `signal.rs:game_signal(pm_bid,pm_ask,ka_ask,kb_ask) -> GameSignal{edge,no_arb,crossed}` — faithful port of `monitor.py::game_edge` (157-179): strictly-crossed-pm reject, the C3 orientation guard (`|guard_pm-ka_ask|>0.40` with `guard_pm = pm_ask.or(pm_bid)`), PK/KP opts with marginal `pfee`/`kfee`, `round4` on `pm_backB` AND each net, `best=max(opts)` PK-stable. Reuses the existing `pmus_marginal_fee`/`kalshi_marginal_fee` (no redefinition).
- `book.rs:game_depth_at_edge(pm,ka,kb,dir) -> Depth` — port of `GameTracker._depth` (210-216): PK = (pm yes-asks, kb yes-asks); KP = (ka yes-asks, `no_ask_ladder(pm yes-bids)`). Reuses `depth_curve`.
- `types.rs` — `Quote` gains `k_b: Option<Book>` (away-team Kalshi book; None for wx/econ). New `PositionLeg{venue,market,side}`; `Position` generalized to `{market(=pmus slug), cat, legs:[PositionLeg;2], size, cluster}` (replaces yes_venue/no_venue, which couldn't represent a 2-YES sports hedge).
- `risk.rs:evaluate` — after the k/pm crossed+stale checks, the away-team book `q.k_b` (when Some) gets the same `CrossedBook(Kalshi)` / `StaleBook(Kalshi)` gates. All other gates unchanged (settlement/proximity/edge-floor/toxicity/caps already work for sports via q.k=Kalshi-A).
- `discovery.rs` — `Pair` gains `kalshi_b: Option<String>`; `assemble` gains `today_epoch_days: Option<i64>` and now EMITS sports as real 2-ticker Pairs via a ported `pick_game` (exact-slug-ET-date bind + doubleheader `used`-set keyed by (date,index); ±1-day fallback only when slug undated AND globally unique). `sports_abbrevs` now picks the LONG-named side as team A (matches `lo=next(s.long)`). `days_to_event = slug_date - today` via dep-free civil-days `ymd_to_epoch_days` (no chrono). Live `discover` computes today from `SystemTime`. Weather/econ joins untouched (only added `kalshi_b: None`).
- `main.rs` — **LEG-MARKET FIX**: replaced `fire_pair`/`leg_prices` with a unified `plan_legs`/`build_legs` that emits two `OrderIntent`s with VENUE-NATIVE market ids (Kalshi legs carry the Kalshi TICKER, pmus legs the slug) + per-leg LIMIT prices from the BOOKS (never the edge). `LivePair`/`PairState` register BOTH sports tickers in `by_ticker`→slug; `kalshi_tickers()`/`pair_tickers()` drive k_tracked + refresh add/prune + book-freeing for both tickers. Event loop branches: sports reads pm+Kalshi-A+Kalshi-B (all 3 required) → `game_signal`+`game_depth_at_edge` → Quote{k=kA, k_b=Some(kB)}. Smoke updated with a 2-ticker sports example + the new Position legs shape.
- `unwind.rs:unwind_orders(pos, [u8;2])` — now SELLs each of the position's two `legs` with its exact (venue, venue-native market, side); correct for a sports hedge whose 2 legs are both YES on two different Kalshi tickers.

## Why (non-obvious only)
- The leg-market fix closes a LATENT live bug: `build_kalshi_payload` uses `intent.market` as the Kalshi `ticker`, but the old `fire_pair` passed `q.market` (the pmus SLUG) → a live Kalshi order with the wrong ticker (404/reject). Smoke now prints `Kalshi No @ 90c market=KXHIGHNY-26JUN11-T95` — the Kalshi leg carries the TICKER.
- `pick_game` was ported inline (not via `matcher::match_sports_abbrev`) because the doubleheader `used`-set + date-bucketed binding needs cross-event state the single-event matcher lacks. This directly closes the `independent-review.md` Claim-4 WARN ("port pick_game's exact-date + doubleheader guard BEFORE sports is emitted").

## Docs cited
(none — pure in-repo port; no external library APIs introduced.)
