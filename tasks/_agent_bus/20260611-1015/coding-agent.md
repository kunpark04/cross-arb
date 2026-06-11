---
from: coding-agent
run_id: 20260611-1015
timestamp: 2026-06-11T10:15:11Z
task_summary: Close the Rust bot's last two functional gaps — co-listed-pair DISCOVERY (fills the empty live-loop `pairs`) and the per-venue staleness `age` feed (makes `Reject::StaleBook` real).
files_changed: [bot-rs/src/book.rs, bot-rs/src/main.rs, bot-rs/src/venue.rs, bot-rs/src/config.rs, bot-rs/src/risk.rs, bot-rs/src/exec.rs, tasks/todo.md]
files_created: [bot-rs/src/discovery.rs]
context_loaded: [CLAUDE.md, tasks/lessons.md, bot/colisted_map.py, bot/monitor.py, bot-rs/src/matcher.rs, bot-rs/src/main.rs, bot-rs/src/book.rs, bot-rs/src/venue.rs, bot-rs/src/types.rs, bot-rs/src/config.rs, bot-rs/src/risk.rs, bot-rs/src/signal.rs, bot-rs/src/exec.rs, bot-rs/src/unwind.rs, bot-rs/src/auth.rs, bot-rs/Cargo.toml, scripts/kalshi_calendar.py, scripts/coverage_map.py, tasks/todo.md]
libraries_consulted: [reqwest]
self_review_artifact: tasks/_agent_bus/20260611-1015/code-logic-reviewer.md
---

## What I changed
- New `bot-rs/src/discovery.rs`: pure slug/field parsers (`weather_city`, `econ_parse`, sports abbrev/date, Kalshi ticker→date/bounds/event/period) + `assemble()` that reuses `matcher.rs` joins verbatim → `Vec<Pair>`; plus an async I/O layer (`discover`) that paginates BOTH public/no-auth catalogs (pmus offset / Kalshi cursor) and is NOT on any test path. Weather+econ emit subscribable 1:1 `Pair`s; sports is matched+counted only (the 1:1 loop can't price a two-ticker market — same boundary `main::LivePair` already documents).
- `book.rs` staleness: `KalshiBook`/`PmusBook` carry `last_update: Instant`, restamped on every applied snapshot/delta; `touch()` now derives real `age_s` from it (was a hard-coded `0.0`), `touch_at(age)` kept for synthetic/test paths, `age_s()` exposed. Manual `Default` (Instant has none). `main.rs` loop builds Quotes with `touch()`, so a wedged stream ages its leg out and `risk::evaluate` fires `Reject::StaleBook` (the per-leg checks already enforce "worst of the two legs").
- `main.rs` loop wiring: initial `discovery::discover()` → seed shared `tracked` sets + a `PairState` map; spawn both streams with the shared `tracked` + a per-venue `SubUpdate` control channel; spawn `refresh_loop` (interval `DISCOVERY_REFRESH_S`, default 300 per monitor.py) that re-discovers, diffs (`diff_targets`), prunes on a 2-miss debounce (`prune_step`), and dispatches in-place add/delete. pmus/Kalshi books for pruned markets are freed (memory stays flat — L20). Dry-run default + every gate intact (smoke verified).
- `venue.rs`: both streams take the `tracked` set (re-subscribed on reconnect) + a `SubUpdate` receiver, and run a FAIR `select!` (frames vs sub-updates). Kalshi applies no-gap `update_subscription` add/delete on the captured sid (`kalshi_update_subscription`, port of monitor.py `update_sub_cmd`); pmus adds a subscribe shard, delete is a local no-op (no documented pmus unsubscribe).
- `config.rs`: added `discovery_refresh_s` (default 300) + a `#[cfg(test)] test_default()` so cross-module tests don't re-spell the 25-field literal. `risk.rs`/`exec.rs` test literals updated for the new field.

## Why (non-obvious only)
- Sports is intentionally NOT emitted as a subscribable pair: the live loop's `Quote`/book path is strictly 1:1 (one Kalshi ticker per pmus slug); wiring sports' two tickers into the subscribe set without a 2-ticker Quote path would subscribe books that are never evaluated. It is still matched + COUNTED so a live league is never silently missed (L7).
- The streams own the subscribe-set re-subscribe (reconnect rebuilds from the shared `tracked`), so the refresh task mutates `tracked` BEFORE dispatching the wire update — a reconnect racing a refresh stays consistent, and an overlapping add is a probe-verified harmless no-gap merge.

## Docs cited
- reqwest → async GET + rustls Client + JSON body → https://github.com/seanmonstar/reqwest (confirmed `client.get(url).send().await?` then read `.text()` and `serde_json::from_str`, matching exec.rs's existing pattern; PUBLIC endpoints, no auth headers).

## Test + build status
- `cargo test`: **66 passed, 0 failed** (53 pre-existing + 13 new: 6 discovery, 2 book-staleness, 3 venue sub-update, 2 main diff/prune helpers — net of one renamed). `cargo build` + `cargo clippy` warning-free for all new code (5 remaining clippy warnings are all PRE-EXISTING — matcher.rs `match_econ` 8-arg, risk.rs/unwind.rs `map_or`, main.rs header doc — out of this run's scope). `cargo run -- --smoke` shows all gates firing + dry-run intact.
- Live-verify pending (owner's droplet): the actual catalog HTTP pull + WS subscribe/sid-capture/`update_subscription` acceptance — none exercised here (sandbox egress blocked; no live venue connection per the task constraint).
