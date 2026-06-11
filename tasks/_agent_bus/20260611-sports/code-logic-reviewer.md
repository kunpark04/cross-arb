---
from: coding-agent (self-review pass)
run_id: 20260611-sports
timestamp: 2026-06-11T10:59:00Z
scope_reviewed: [signal.rs:game_signal+GameSignal, book.rs:game_depth_at_edge, types.rs:Quote.k_b+PositionLeg+Position, risk.rs:evaluate k_b gates, discovery.rs:pick_game+sports assemble+ymd_to_epoch_days+sports_abbrevs, main.rs:plan_legs/build_legs+PairState+event-loop sports branch, unwind.rs:unwind_orders]
critical_count: 0
warn_count: 0
info_count: 4
launch_recommendation: PROCEED
self_review: true
cross_references: [tasks/_agent_bus/20260611-parity-review/independent-review.md, tasks/_agent_bus/20260611-rust-review/trading-strategy-review.md, agent-memory/coding-agent/MEMORY.md]
---

## Goal Understanding
Make SPORTS tradeable in the live Rust loop. Sports is genuinely 2-outcome: a pmus game (YES=team A)
hedges against the OTHER team's SEPARATE Kalshi market, so two cross-venue configs (PK = A@pmus + B@Kalshi;
KP = A@Kalshi + B@pmus). This touches the LIVE ORDER PATH — the economics (`game_edge`) and the binding
(`pick_game`) had to be ported EXACTLY, plus a latent leg-market bug fixed. Compile + unit-test only.

## Scope Reviewed
- signal.rs:97-150 — `game_signal` + `GameSignal` (port of game_edge 157-179).
- book.rs:275-289 — `game_depth_at_edge` (port of GameTracker._depth 210-216).
- types.rs:93-160 — `Quote.k_b`, `PositionLeg`, generalized `Position`.
- risk.rs:93-115 — `q.k_b` crossed/stale gates.
- discovery.rs:38-46,360-560 — `Pair.kalshi_b`, `assemble(today_epoch_days)`, sports emission, `pick_game`/`resolve_two`/`dnear`/`ymd_to_epoch_days`, `sports_abbrevs` long-team-first.
- main.rs:103-165,470-635 — `LivePair.kalshi_b`+`kalshi_tickers`, `PairState` dual-ticker index, `pair_tickers`, event-loop sports branch, `plan_legs`/`build_legs` (the leg-market fix), refresh dual-ticker add/prune.
- unwind.rs:33-57 — `unwind_orders` sells both explicit legs.

## Findings

### CRITICAL (must fix before launch)
- None.

### WARN (fix or justify)
- None.

### INFO (optional improvements / simplifications)
- **C3-reject reason is collapsed to NonPositiveEdge in the live loop.** When `game_signal`'s C3 guard trips
  (a >40c same-team gap), it returns `net=0.0` → `evaluate` rejects as `NonPositiveEdge`, not a distinct
  "orientation/flip" reason. The trade is correctly BLOCKED either way; only the *telemetry reason* is lost.
  Stage-2 transition logging (out of scope here) could surface `GameSignal.crossed` as its own reject reason.
- **`kb_book.touch()` is constructed twice** in the sports event-loop branch (once for `game_signal`'s
  `kb_ask`, once for the Quote's `k_b`). Both reads are under the same `kalshi_books` lock with no mutation
  between, so they're identical; binding `let kbt = kb_book.touch();` once would be marginally cleaner.
- **±1-day fallback iterates a HashMap in nondeterministic order.** Correctness is unaffected — the fallback
  requires `hits.len() == 1` (globally unique), so order can't change the result — but a deterministic sort
  would make any future debugging reproducible.
- **`matcher::match_sports_abbrev` is now unused outside its own tests** (discovery ports pick_game inline for
  the doubleheader state). It's still a faithful, tested helper; leaving it costs nothing, but it could be
  removed if sports binding is never needed in two places.

## Checks Passed
- **game_edge economics bit-for-bit:** PK net `round4((1-(pa+kb)) - pfee(pa) - kfee(kb,marginal))`, KP net
  `round4((1-(ka+pm_backB)) - kfee(ka,marginal) - pfee(pm_backB))` with `pm_backB=round4(1-pm_bid)` — both
  `round4`s present; tests recompute the Python expression and compare to <1e-12.
- **Fee parity:** `pmus_marginal_fee`/`kalshi_marginal_fee` = `coef·p·(1-p)` with the `0<p<1`→0 guard,
  identical to `ledger.py::pfee`/`kfee(marginal=True)` (which monitor.py imports — verified the import line).
  No fee redefinition.
- **C3 guard verbatim:** crossed-pm reject; `guard_pm = pm_ask.or(pm_bid)` (one-sided falls back to bid, still
  covering KP); `|guard_pm-ka_ask|>0.40` → reject. Matches monitor.py 163-169.
- **Dir mapping:** Python "PK"/"KP" → Dir::PK/Dir::KP; `max(opts)` PK-stable on ties (strict `>` fold from opts[0]).
- **Depth direction matches signal direction:** event loop passes `sig.edge.dir` to BOTH `game_depth_at_edge`
  and `build_legs` — leg/depth/signal cannot diverge.
- **pick_game faithful:** exact-date arm runs FIRST unconditionally; ±1 fallback only when `!slug_dated` AND
  globally unique; doubleheader `used`-set keyed by (date,index) ≡ Python `id(pl)` (each event dict appears
  once). Tests cover exact-not-adjacent, doubleheader-distinct-events, undated-unique-vs-ambiguous, dated-no-fallback.
- **sports_abbrevs A/B ordering:** long-named side = A (`is_truthy(s.long)`), other = B; sample lists PIT first
  but LAD `long:true` → A=LAD/B=PIT; smoke confirms PK leg2 = PIT (away) ticker. Matches `lo=next(s.long)`.
- **LEG-MARKET FIX (the regression invariant `leg_prices_come_from_books_not_edge`):** every leg's `market`
  is venue-native (Kalshi→ticker, pmus→slug); every limit price is from the BOOKS (YES=cheap-venue YES ask;
  NO@pmus=1-pm_bid; NO@Kalshi=1-k_bid; sports PK leg2=Kalshi-B YES ask) — NEVER `(1-edge)`. Smoke prints the
  weather Kalshi leg as `market=KXHIGHNY-26JUN11-T95` (the ticker), proving the latent bug is closed.
- **Position/unwind:** `unwind_orders` sells each of the 2 explicit `legs` with its exact venue/market/side;
  sports legs are both YES on two different tickers — the old yes/no-venue model couldn't represent this.
- **Quote.k_b crossed/stale gates** fire `CrossedBook(Kalshi)`/`StaleBook(Kalshi)` for the away book; None for
  weather/econ leaves those paths unchanged (tested).
- **ymd_to_epoch_days (dep-free civil-days):** anchors 1970-01-01→0, 2000-01-01→10957, leap-day deltas correct;
  no chrono dependency added.
- **days_to_event sign:** `game-today` → future game positive; smoke: 5d→TooEarly, 1d→approved.
- **Known-failure-mode cross-reference:** L21 econ off-by-one — untouched (only added `kalshi_b:None`);
  L1 no-false-positive — pick_game requires two DISTINCT tickers; the parity-review Claim-4 WARN is now CLOSED
  (exact-date + doubleheader guard ported before sports made subscribable). Same-bar/lookahead/leakage patterns
  N/A (no time-series labels here). Regex-port truncation (pattern memory) — reused the already-verified
  `iso_date`/`ktok_date`; GDP full-date period path unchanged.
- **No leg can be derived from the edge** (the prior self-review CRITICAL): `plan_legs` reads only book
  touches; `build_legs` returns None if any leg lacks a book price → the loop skips (no naked fire), tested.
- **66 baseline tests still green; +14 new = 80 passed, 0 failed.** `cargo build` + `cargo clippy --all-targets`
  produce ZERO warnings attributable to new code (5 residual clippy warnings are all in pre-existing,
  unmodified lines — verified absent from the diff hunks).

## Launch Recommendation
PROCEED. The order-path economics (game_edge) and binding (pick_game) are faithful ports verified bit-for-bit
against the live Python; the leg-market latent bug is fixed and pinned by a regression test; no safety gate was
weakened (the away-team book gained the same crossed/stale gates); no live venue I/O was added.

## Self-review caveat
This is authorship self-review of a LIVE-ORDER-PATH change, so an independent parity pass (like the
`independent-review.md` that flagged the very Claim-4 WARN this run closes) of `game_signal` + `pick_game` +
the leg mapping vs the Python is warranted before the first armed (EXECUTION_MODE=live) sports run — the
project's todo already queues exactly that follow-up review.
