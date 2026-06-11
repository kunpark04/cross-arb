---
from: coding-agent (self-review pass)
run_id: 20260611-1015
timestamp: 2026-06-11T10:15:11Z
scope_reviewed: [bot-rs/src/discovery.rs (new, full), bot-rs/src/book.rs (staleness), bot-rs/src/venue.rs (sub-update channel + streams), bot-rs/src/main.rs (run_live + refresh_loop + helpers), bot-rs/src/config.rs (discovery_refresh_s + test_default)]
critical_count: 1
warn_count: 3
info_count: 4
launch_recommendation: PROCEED WITH FIXES
self_review: true
cross_references: [tasks/_agent_bus/20260611-1015/coding-agent.md, tasks/lessons.md, bot/colisted_map.py, bot/monitor.py]
---

## Goal Understanding
Complete the live loop by (1) porting `colisted_map.py`'s catalog-pull + join application into Rust so the loop's `pairs` list is filled from the live universe (reusing `matcher.rs` joins, no identity logic re-implemented), and (2) feeding each book's real staleness `age` so the inert `Reject::StaleBook` gate actually fires. Compile + unit-test only; no live venue connection (owner's droplet does the real pull).

## Scope Reviewed
- `discovery.rs` — catalog pull (pmus offset / Kalshi cursor pagination), pure slug/field parsers, `assemble()` reusing matcher joins, coverage report.
- `book.rs` — `last_update: Instant` on both books; `touch()` derives real age; `touch_at`/`age_s`/`backdate` helpers.
- `venue.rs` — `SubUpdate` channel + `kalshi_update_subscription` envelope; both streams `select!` frames vs sub-updates; reconnect re-subscribes the shared `tracked` set.
- `main.rs` — initial discovery → seed; spawn streams + `refresh_loop`; periodic re-discover/diff/prune; book freeing on prune; the event loop now reads the shared `PairState` and builds Quotes with real `touch()`.
- `config.rs` — `discovery_refresh_s`; `test_default()`.

## Findings

### CRITICAL (must fix before launch)
- GDP econ pairs would silently never join (Kalshi period parser dropped the day)
  - Location: bot-rs/src/discovery.rs `k_econ_period`
  - Issue: `econ_parse` produces a FULL-DATE period for GDP (`26JUL30`, matching colisted_map.py), but the Kalshi-side period extractor originally truncated to `YYMMM` (`26JUL`). colisted_map.py's Kalshi regex is `-(\d{2}[A-Z]{3}\d{0,2})-` — it KEEPS the 0-2 trailing day digits. So pmus `26JUL30` could never equal Kalshi `26JUL` → every GDP pair dropped (a whole econ family silently missing from the live universe).
  - Why it matters: silent coverage loss for an entire settlement-identical econ family — exactly the "hardcoded enumeration silently misses" failure class (L7), and it would never surface as an error (just zero GDP pairs).
  - Suggested fix: consume 0-2 trailing day digits then require the closing `-` (faithful to the `\d{0,2}` + `-` anchor).
  - Status: **self-resolved within run** — fixed `k_econ_period` + added a regression test (`econ_gdp_full_date_period_joins`) asserting `k_econ_period("KXGDP-26JUL30-T1.9") == "26JUL30"`, `KXU3-26JUN == "26JUN"`, and a full GDP assemble join.

### WARN (fix or justify)
- Kalshi books for pruned tickers lingered until the next reconnect (slow memory growth)
  - Location: bot-rs/src/main.rs `refresh_loop`
  - Issue: `delete_markets` stops the stream but the book stayed in the shared `kalshi_books` map until `books.clear()` at the next reconnect — a slow leak over a multi-week run vs monitor.py's explicit `books.pop` on teardown (L20: memory must stay flat).
  - Status: **self-resolved within run** — `refresh_loop` now pops pruned tickers from `kalshi_books`; pmus books are freed in the event loop when an untracked frame arrives.
- `biased` select could starve discovery sub-updates under a busy frame stream
  - Location: bot-rs/src/venue.rs (both streams)
  - Issue: the original `tokio::select! { biased; ... }` always polled WS frames first, so a continuously-ready frame stream could delay applying an add/delete indefinitely.
  - Status: **self-resolved within run** — removed `biased`; the fair (randomized) select gives sub-updates a turn each poll. Adds tolerate a small delay anyway (a just-discovered market is not time-critical).
- Sports is matched but NOT subscribable in the 1:1 loop (coverage vs capability gap)
  - Location: bot-rs/src/discovery.rs (sports arm), main.rs loop
  - Issue: discovery counts sports pairs but emits none as a `Pair`, so the bot never trades sports even when reconciled — the live loop's `Quote`/book path is strictly 1:1 (one Kalshi ticker), and sports needs two.
  - Why it matters: a deliberate scope boundary (mirrors the existing `LivePair` doc), not a bug — but it means "sports is a prime category once reconciled" (CLAUDE.md) is still blocked on a 2-ticker Quote path. Justified + surfaced, not silently dropped: discovery COUNTS sports so a live league is never missed (L7).
  - Status: reported (out of this run's 1:1 network-layer scope; the 2-ticker GameTracker port is a separate task).

### INFO (optional improvements / simplifications)
- Sports exact-date binding is approximated in discovery's COUNT path: `assemble` tries every Kalshi event for a league (`by_event.values().any(...)`) rather than pinning the exact slug date the way `pick_game` does in Python. Acceptable because the matcher still refuses unless BOTH abbrevs resolve to two DISTINCT tickers (L1), and this is a count-only path (not subscribed). If sports ever go 1:1, port the exact-date + doubleheader `used`-set binding too.
- `apply_kalshi_sub_update` drops an add if the `sid` ack hasn't been seen yet; the shared `tracked` set means the next reconnect re-subscribes it, so it's not lost in practice (the ack always precedes the first 300s refresh). Documented inline.
- Kalshi books for pruned markets are now freed in `refresh_loop`, but a ticker that settles WITHOUT appearing in a prune cycle (e.g. between refreshes) is still cleared only on reconnect — bounded and self-healing.
- `month-name first-3-chars` matching in `parse_release_period` is faithful to Python's `-(jan|...)[a-z]*-` (which would also match `-mayfield-`); econ slugs don't contain surnames so no false month match in practice.

## Checks Passed
- **Settlement-identity joins reuse matcher.rs verbatim** — no identity logic re-implemented in discovery; the L21 econ off-by-one (twin = floor T−step, never T) and L17 weather bounds-canonicalization are exercised through `match_econ`/`match_weather` and pinned by the assemble test (U-3 >=4.4 binds K-floor-4.3, NOT 4.4; gte64lt65f→B64 despite an offset listing; gte80lt81f flagged misaligned, not paired).
- **No false joins**: `<=` tails + `==` point buckets skipped (counted in `econ_skipped`); a `>=T` with no listed twin skipped; an unmapped climate city (phl) flagged; sports requires two distinct tickers.
- **Staleness is real**: `touch()` derives age from `last_update`, restamped on every snapshot AND delta; a backdated (9s) pmus book → `Reject::StaleBook(Pmus)` end-to-end through the real `touch()` path (deterministic, no sleep). The per-leg gate enforces worst-of-two-legs.
- **No deadlock / lock-ordering cycle**: the event loop never nests two of {pairs, k_tracked, pm_tracked, kalshi_books}; the refresh + seed blocks acquire pairs→k_tracked→pm_tracked in the same order; `kalshi_books` is never nested with the others.
- **Concurrency safety**: the event loop clones the `LivePair` out before releasing the `pairs` lock, so it never holds the lock across the Quote build / book locks while the refresh task mutates the map.
- **Degraded discovery never prunes** (H4): a failed pull `continue`s without touching the tracked set; the 2-miss debounce (`prune_step`) stops a transient blip from tearing down a live market (parity with monitor.py `prune_decision`).
- **Safety gates intact**: dry-run default, kill-switch, prod-consent, settlement/toxicity/proximity/edge-floor all unchanged; `--smoke` shows SettlementUnverified / ToxicDirection / TooEarly all firing and no orders sent. No auth on the catalog pull; rustls only; no order submission; no live venue connection in test/dev.
- **All 53 pre-existing tests still green** (66 total).

## Launch Recommendation
PROCEED WITH FIXES — the one CRITICAL (GDP join) and two memory/fairness WARNs were self-resolved within the run and pinned with tests; what remains is live-verification on the owner's droplet (catalog HTTP, WS sid-capture, `update_subscription` acceptance), which the sandbox cannot exercise.

## Self-review caveat
This is authorship self-review: I wrote the code under audit, so authorship bias is a real risk (the GDP CRITICAL was caught only by cross-checking the Python regex's `\d{0,2}` against my truncating port — a reminder that "I already thought about it" is not proof). The discovery join feeds live-capable trading; before the owner arms anything beyond dry-run, an independent review of `discovery.rs`'s parsers against the LIVE catalog shapes (esp. the econ period/ineq decoding per family, which is the L21/L24 phantom-edge surface) is warranted — and the live pull + `update_subscription` acceptance must be confirmed on the droplet, since no parser here was run against a real venue response.
