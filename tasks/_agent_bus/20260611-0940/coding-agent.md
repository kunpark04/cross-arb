---
from: coding-agent
run_id: 20260611-0940
timestamp: 2026-06-11T09:55:00Z
task_summary: Built the stage-2 NETWORK LAYER of the Rust cross-arb bot — both venue WS clients, the async connect-and-trade loop, and the live signed-POST transport (compile + unit-test only; live I/O is the owner's droplet step).
files_changed: [bot-rs/src/exec.rs, bot-rs/src/main.rs, bot-rs/src/auth.rs, tasks/todo.md]
files_created: [bot-rs/src/venue.rs]
context_loaded: [CLAUDE.md, tasks/lessons.md, bot-rs/src/{types,config,exec,risk,signal,book,matcher,unwind,ledger,auth,main}.rs, bot-rs/Cargo.toml, bot-rs/README.md, bot-rs/.env.example, bot/monitor.py, bot/kalshi_book.py, scripts/probe_kalshi_ws.py, scripts/probe_pmus_ws_auth.py, research/polymarketus-api-auth.md, research/kalshi-venue-audit.md, decisions/0015-owner-override-live-trading-phase.md, tasks/_agent_bus/20260611-rust-review/code-latency-review.md, tasks/_agent_bus/20260611-0925/coding-agent.md, ~/.claude/agent-memory/coding-agent/MEMORY.md]
libraries_consulted: [tokio-tungstenite, tungstenite-rs, reqwest]
self_review_artifact: tasks/_agent_bus/20260611-0940/code-logic-reviewer.md
---

## What I changed
- `bot-rs/src/venue.rs` (NEW) — both venue WS clients + pure frame parsers. Kalshi: RSA-PSS handshake to `wss://api.elections.kalshi.com/trade-api/ws/v2`, `orderbook_delta` subscribe, snapshot+delta merge into `book::KalshiBook`, single-sid `SeqTracker` (gap → cycle connection). pmus: Ed25519 handshake to `wss://api.polymarket.us/v1/ws/markets`, `SUBSCRIPTION_TYPE_MARKET_DATA` subscribe (≤100-slug shards), `marketData` frame → `(bids, asks)`. Both run a supervised reconnect loop with 1→30 s backoff (ports `monitor.py`); each pushes a `VenueEvent` on an mpsc channel. (venue.rs:207 kalshi_stream, :276 pmus_stream)
- `bot-rs/src/exec.rs` — `LiveBackend::submit_pair` now fires the REAL signed POSTs CONCURRENTLY (`tokio::join!` inside `run_pair`, exec.rs:269): Kalshi `POST /portfolio/orders` (RSA-PSS over `{ts}POST/trade-api/v2/portfolio/orders` + `build_kalshi_payload`) and a best-effort pmus order (Ed25519 + `build_pmus_payload`, with a loud `// TODO verify pmus POST signing live` — body-signing is brief-flagged unconfirmed). Keys load from env + the external key path; absent → `KeysUnavailable` (never sends in the sandbox). Trait stays dyn-compatible (sync `submit_pair`; the async join runs off the ambient runtime via `block_in_place`/a tiny current-thread rt).
- `bot-rs/src/main.rs` — restructured to `#[tokio::main]`; dry-run `smoke` preserved (runs on `--smoke` OR when venue creds are absent). Added the live loop (main.rs:108 run_live): spawns both streams, maintains a book per venue, builds a `Quote` on each complete dual-venue update (L5), runs `risk::evaluate`, fires via `submit_pair`. Populates `led_by` (mid-move diff vs the prior snapshot) and per-leg limit prices from the books.
- `bot-rs/src/auth.rs` — added `now_ms_for_sign()` (single millis-since-epoch source for handshake + POST signing).

## Why (non-obvious only)
- **dyn-compat of the async transport:** `ExecutionBackend` is `Box<dyn>` and `submit_pair` is sync; an async-fn-in-trait isn't dyn-compatible without boxing futures (would ripple through every caller + the smoke). So `LiveBackend` owns its reqwest `Client` and drives `tokio::join!` of the two POSTs off the ambient runtime. Keeps the trait object-safe and the dry-run path byte-for-byte unchanged. (Caveat: `block_in_place` needs a multi-thread runtime — the `#[tokio::main]` default; documented in exec.rs.)
- **`tokio::join!` not `try_join!`:** a hedge needs BOTH legs' outcomes (the `PairAck{a,b}` carries the naked-leg signal). `try_join!` would short-circuit on one failure and discard the other leg's ack.
- **Kalshi signed-path vs request-path agree:** signed path `/trade-api/v2/portfolio/orders` == the path component of `{kalshi_base}/portfolio/orders` for BOTH demo and prod bases (a mismatch 401s silently).
- **Discovery is the SEPARATE stage-2 matcher task** (the `colisted_map.py` port), so the live loop consumes a `Vec<LivePair>` it's given; here that list is empty (one-line seam) and the loop reports + falls back to the smoke rather than inventing a universe to trade.

## Docs cited
- tokio-tungstenite → `connect_async` accepts any `IntoClientRequest` → https://docs.rs/tokio-tungstenite (via context7 /snapview/tokio-tungstenite)
- tungstenite → `ClientRequestBuilder::new(uri).with_header(k,v)` for signed handshake headers → https://docs.rs/tungstenite (via context7 /snapview/tungstenite-rs)
- reqwest → async `Client.post(url).header().body(String).send().await` + `resp.status()`/`resp.text()`, `use_rustls_tls()` → https://docs.rs/reqwest (via context7 /seanmonstar/reqwest)

## Test result
`cargo test` = 53 passed (was 42; +11: 7 venue frame-parsers, 1 pmus payload, 3 leg-pricing/cents regression guards). Zero clippy warnings in the new code. Dry-run smoke runs end-to-end with all gates behaving.
