---
from: coding-agent (self-review pass)
run_id: 20260611-0940
timestamp: 2026-06-11T09:56:00Z
scope_reviewed: [bot-rs/src/venue.rs (whole), bot-rs/src/exec.rs:155-330 (LiveBackend transport), bot-rs/src/main.rs:108-285 (run_live + leg pricing), bot-rs/src/auth.rs:18-27 (now_ms_for_sign)]
critical_count: 1
warn_count: 2
info_count: 3
launch_recommendation: PROCEED WITH FIXES
self_review: true
cross_references: [tasks/_agent_bus/20260611-rust-review/code-latency-review.md, tasks/_agent_bus/20260611-rust-review/trading-strategy-review.md, tasks/_agent_bus/20260611-0925/code-logic-reviewer.md, tasks/lessons.md]
---

## Goal Understanding
Build the stage-2 network layer that completes the Rust bot: two venue WebSocket clients (Kalshi RSA-PSS + pmus Ed25519) with frame parsing and supervised reconnect, an async connect-and-trade loop that runs the match→signal→risk→exec pipeline, and the real concurrent signed-POST live transport. Compile + unit-test only — no live venue I/O, no order submission, no safety gate weakened.

## Scope Reviewed
- `venue.rs` — WS clients + pure parsers (snapshot/delta merge, pmus marketData, SeqTracker, subscribe envelopes). All in scope.
- `exec.rs` `LiveBackend` — the real signed POST transport (`post_leg`/`run_pair`/`submit_pair`, payload builders). In scope.
- `main.rs` `run_live` + `leg_prices`/`cents`/`fire_pair` — the async loop and per-leg pricing. In scope.
- `auth.rs` — one helper added. In scope.
- No adjacent refactors; no renamed unrelated symbols; deps were already in Cargo.toml (untouched).

## Findings

### CRITICAL (must fix before launch)
- **Live-loop fired the WRONG per-leg limit prices (would systematically leg out the hedge).**
  - Location: `main.rs` (the `evaluate Ok` arm, ~line 213) + `fire_pair`.
  - Issue: the YES-leg limit was set to `(1 - edge.net) * 100` ≈ the PAIR cost (~97c) and the NO-leg limit to `100 - that` ≈ the edge (~3c). The pair cost is `YES_ask + NO_ask`, not either leg's price. So the NO leg's limit (~3c) sat far below its real NO ask (~90c) and could never fill → a filled YES leg + an unfilled NO leg = a naked directional position, exactly the both-legs-don't-offset failure the whole strategy guards against. It cannot fire in THIS build (dry-run / no keys), but it would lose money the instant the bot is armed.
  - Why it matters: a naked leg on an unvalidated edge with an unmeasured unwind cost (the 0015 / deployment-readiness top risk) is the fastest way to lose capital.
  - Suggested fix: price each leg from the BOOKS — YES leg = cheap venue's YES ask; NO leg = `1 - dear venue's YES bid`. Skip the fire if either side isn't two-sided.
  - Status: **self-resolved within run.** Added `leg_prices(q, dir)` + `cents()` (book-derived per-leg prices, tick-validated), rewired `fire_pair` to take both leg prices, and added a regression test (`leg_prices_come_from_books_not_edge`) that pins YES=7c / NO=90c for the smoke's weather quote. Verified live in the dry-run smoke (was 97c/3c → now 7c/90c; sports 55c/42c).

### WARN (fix or justify)
- **Staleness gate (`max_book_age_s`) is DEAD in the live path — books are fed `age_s = 0.0`.**
  - Location: `main.rs` run_live — `pm.touch(0.0)` and `kbook.touch(0.0)`.
  - Issue: every book looks perfectly fresh, so `risk::evaluate`'s `Reject::StaleBook` can never fire live. L13 ("persistent edge ≠ fillable edge; instrument staleness") is explicit that this gate matters. The Python monitor tracks per-venue last-update wall-clock; the Rust loop does not yet.
  - Justification for shipping as-is: out of the network-layer scope and the README/types already mark `age` as a stage-2 item; the `VenueEvent` already carries enough to add per-venue last-rx timestamps. But it MUST be wired before real-money use — flagged loudly here so it isn't forgotten when the gate looks "present" but is inert.
- **Reconnect rebuild-pause cleared too eagerly (cross-venue).**
  - Location: `main.rs` run_live event loop.
  - Issue (original): a single `paused_until_rebuild` bool was cleared on the FIRST book frame from EITHER venue after a reconnect — so a pmus frame would re-enable entries while the Kalshi book map was still rebuilding from snapshots (or vice-versa).
  - Status: **self-resolved within run.** Split into per-venue `k_rebuild`/`pm_rebuild`; `stream_paused = k_rebuild || pm_rebuild`; each clears only on a frame from its own venue. (A seq-gap pauses Kalshi only.)

### INFO (optional improvements / simplifications)
- **`led_by` is a best-effort heuristic, not the monitor's exact definition.** I use "whichever venue's mid moved more since the prior complete snapshot." The Python monitor records per-venue YES touches and derives the mover in post-hoc adverse-selection analysis. The H1 toxicity-direction gate is documented as dormant-until-stage-2-supplies-`led_by`; this is a defensible stage-2 fill, but a reviewer wiring the real adverse-selection signal may prefer the monitor's touch-level definition. Returns `None` on first sighting (gate dormant) — correct.
- **The live-loop BODY is compile-checked only, not unit-tested.** `pairs` is an empty `Vec` (the discovery seam), so the event→book→Quote→evaluate→fire wiring never runs in tests. Mitigation: the pricing logic that held the CRITICAL bug was extracted into pure functions (`leg_prices`/`cents`) and IS unit-tested. The async loop itself would need a refactor into a per-event handler + injected channel to test end-to-end — deferred (proportionate to scope).
- **Discovery (colisted-map port) is the stated-separate stage-2 matcher task**, so the network layer connects + idles with no pairs and falls back to the smoke. Not a defect — a documented seam (one line: fill `pairs`).

## Checks Passed
- **Concurrency / latency invariant:** `run_pair` uses `tokio::join!` (both legs fired concurrently, not serial) — satisfies the latency review's CRITICAL-1. Verified `join!` (not `try_join!`) so both legs' acks are always returned (naked-leg detection intact).
- **Kalshi signing path == request path** for both demo (`demo-api.kalshi.co/trade-api/v2`) and prod (`api.elections.kalshi.com/trade-api/v2`) bases — signed `/trade-api/v2/portfolio/orders` matches the URL path component (no silent 401).
- **No safety gate weakened:** kill-switch (`risk::evaluate` step 0) is hit for every live quote; the live+prod consent gate is checked in `main` before the loop; `REQUIRE_SETTLE_CLEAN` / event-proximity / toxicity-direction / mid-divergence / crossed / non-positive gates all unchanged and still in the path (confirmed by the dry-run smoke: econ REJECTED SettlementUnverified, dear-led weather REJECTED ToxicDirection, 5d-out sports REJECTED TooEarly).
- **Never sends in the sandbox:** `LiveBackend::submit_pair` returns `KeysUnavailable` per-leg when keys aren't loaded (empty `KALSHI_RW_KEY_PATH` / no env) — tests + smoke confirm no network call.
- **Mutex never held across `.await`:** in `kalshi_stream`, `drop(bk)` precedes every `tx.send`; the only `.await` points (`ws.next()`, `sleep`) hold no lock. In `run_live`, the `kalshi_books` lock scope contains no `.await`.
- **Frame parsers correct vs the verified Python:** snapshot→book best (0.67/0.69), delta merge add+empty→0.67, NO-bid→YES-ask, missing side→empty (not error), pmus money-object→(bids,asks)→PmusBook touch, SeqTracker gap detection — all ported from `kalshi_book.py::_selftest` + the probe frame shapes and unit-tested against embedded JSON.
- **Edge cases:** malformed delta (missing field) → book unchanged (returns false); one-sided book → NO-leg price `None` → loop skips (no naked fire); `cents` rejects out-of-tick / non-finite prices; pmus eof-less framing handled (each marketData frame replaces the book, per the brief).
- **Known-failure cross-reference:** L1 (mid-divergence gate intact), L5 (waits for BOTH books before a Quote), L12 (crossed reject intact), L13 (staleness — flagged WARN above), `pattern_rest_envelope_probe` (snapshot/delta/control/seq/eof envelope all honored).
- **rustls only:** reqwest built with `use_rustls_tls()`; tungstenite uses the `rustls-tls-webpki-roots` feature — no OpenSSL.

## Launch Recommendation
PROCEED WITH FIXES — the CRITICAL leg-pricing bug and the eager-pause WARN are fixed in-run and regression-tested; 53 tests green; the staleness-gate WARN is a documented stage-2 wiring item that must be closed (with discovery + per-venue age + a demo-sandbox session) before any real-money arm. The live transport + WS clients compile and unit-test; the live connect/orders themselves are verify-pending on the owner's droplet.

## Self-review caveat
This is the author reviewing their own diff — authorship bias is real, and the live-loop body has only compile coverage, so a focused independent review of `run_live` (esp. the per-venue rebuild-pause state machine and the `led_by` definition) is warranted before the bot is armed against real money. The change feeds decision 0015's live-trading path on the owner's read-write key; per the README, do NOT trade real money until (a) the fee/signal parity test is green [done — prior run], (b) a demo-sandbox session is clean [verify-pending, owner droplet], and (c) the 0014 data validates the edge. The two highest-risk live-verify items remain pmus POST-body signing (brief-flagged unconfirmed; loud TODO + typed-error in code) and the live fill/leg-out behavior under real latency.
