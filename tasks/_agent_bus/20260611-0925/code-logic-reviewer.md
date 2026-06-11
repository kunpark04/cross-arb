---
from: coding-agent (self-review pass)
run_id: 20260611-0925
timestamp: 2026-06-11T09:25:19Z
scope_reviewed: [bot-rs/src/signal.rs:1-220, bot-rs/src/book.rs:1-300, bot-rs/src/matcher.rs:1-380, bot-rs/src/main.rs:10-19]
critical_count: 0
warn_count: 0
info_count: 2
launch_recommendation: PROCEED
self_review: true
cross_references: [tasks/lessons.md, ~/.claude/agent-memory/coding-agent/feedback_threshold_ge_vs_gt.md]
---

## Goal Understanding
Port three financial-correctness-critical pieces of the bot from Python to Rust — the order-book merge,
the signal/fee model, and the co-listed matcher — such that the Rust reproduces the Python EXACTLY, and
prove it with unit tests (no live venue). A wrong fee or an inverted direction silently turns +EV into a
loss, so fidelity to ledger.py/monitor.py/colisted_map.py is the whole point.

## Scope Reviewed
- `signal.rs` — `signal()` + marginal fee wrappers + 6 parity tests. In scope (deliverable 2).
- `book.rs` — `KalshiBook`/`PmusBook` merge + `depth_curve`/`depth_at_edge` + 6 tests. In scope (deliverable 1).
- `matcher.rs` — weather/econ/sports joins + slug parser + 7 tests. In scope (deliverable 3).
- `main.rs` — three `mod` lines only. In scope (required wiring). No other module's logic touched.

## Findings

### CRITICAL (must fix before launch)
- None.

### WARN (fix or justify)
- None. (One candidate was investigated and resolved within the run — see Checks Passed item 6.)

### INFO (optional improvements / simplifications)
- `book.rs` keeps both `PmusBook` and `KalshiBook` even though stage-2 may only need one path per
  direction; both are required by `depth_at_edge` (cross-venue), so this is not removable. No action.
- `matcher.rs` takes already-parsed Kalshi inputs (`k_buckets`, `k_floors`, `k_labels`) rather than
  doing the live catalog pull — deliberate (the prompt allowed "port the JOIN LOGIC OR a static map"
  and the sandbox blocks live venue I/O). The discovery/HTTP layer is a separate stage-2 task. No action.

## Checks Passed
1. **Fee parity (the gate)**: Kalshi taker 100@.5 -> 175c (ceiled once, L10 not 176), pmus 100@.5 ->
   125c, marginal Kalshi 1.75c / pmus 1.25c (no ceil, for detection, L10/L15). Reuses `ledger.rs`
   constants — no second source of truth to drift.
2. **Direction mapping (highest-risk invariant)**: Python `dir "P"`==`Dir::PK`, `"K"`==`Dir::KP`. Pinned
   by three tests AND cross-checked against live `python bot/ledger.py` output: s1 book -> dir P/+0.0326,
   flip -> dir K/+0.053, s2 (Kalshi no-bid) -> dir K/-0.1368/no_arb. Rust matches all three exactly.
3. **Edge sign**: a flat (venues-agree) book -> `no_arb`; a real gap -> positive net in the right dir;
   `net <= 0` sets `no_arb` (mirrors ledger.py's `net_edge > 0` test).
4. **Crossed/stale rejection (L12)**: a venue with both touches and `bid > ask` is rejected on every
   direction touching it (pmus-crossed and the Kalshi stale-high-bid phantom both -> `crossed && no_arb`).
5. **One-sided books (per-direction pricing)**: a missing touch only kills the direction that needs it;
   the other direction still prices (verified both pmus-no-bid and Kalshi-no-bid against Python).
6. **Self-caught test bug**: my first `one_sided` test asserted PK where Python returns KP (I had the
   "which missing touch kills which direction" backwards in the comment). The implementation was correct;
   the test expectation was wrong. Corrected the assertion to match live ledger.py (KP, no_arb, net<0).
   Net: the parity discipline caught my own authoring error — exactly its purpose.
7. **Depth walk fidelity (L18 crossable-not-resting)**: `depth_curve` two-pointer reproduces monitor.py
   on three vectors — (30,30,30) generic, (50,50,50) weather PK, (8,8,8) thin-leg-caps — confirmed by
   running monitor.py side-by-side. `depth_at_edge` Dir->ladder map matches `MarketTracker._depth`
   (PK = pmus-ask + Kalshi-NO-ask; KP = Kalshi-ask + pmus-NO-ask); always cross-venue (never self-paired).
8. **YES-ask-from-NO-bid (Kalshi merge)**: `yes_ask = 1 - best_no_bid`, ladders ordered (bids desc,
   asks asc); snapshot+delta+empty-level-drop match kalshi_book.py's selftest (best (.67,.69) etc).
9. **Weather bounds equality (L17)**: both venues canonicalized to inclusive `[lo,hi]` BEFORE comparison
   (pmus tails exclusive, Kalshi tails `floor+1`/`cap-1`, middles inclusive); matched on identical bounds,
   never sorted-index; offset listing still finds the twin; a missing twin is flagged, not paired.
   Adversarial digit widths (gte9lt10f, gte100lt101f) cross-checked vs colisted_map.py.
10. **Econ twin off-by-one (L21, feedback_threshold_ge_vs_gt)**: `econ_twin(T,step)=T-step`, so pmus
    `>=4.4` binds Kalshi floor 4.3, NEVER floor 4.4 (the phantom P(print==T) pair). Test asserts it binds
    the .3 twin even when both .3 and .4 floors are listed; `<=`/`==` orientations are skipped, not paired.
11. **No-false-positive (L1)**: sports binds only when BOTH abbrevs resolve to DISTINCT tickers; a
    non-resolving team -> `None`.
12. **dtype/overflow**: depth accumulates as f64, rounds to u32 only at the end (matches Python's
    `round()` to int); no negative or NaN path (prices guarded to `0<p<1` for fees; degenerate -> 0 fee).
13. **No scope creep / no new deps**: only added 3 modules + 3 mod lines; reused `types` (Book/Depth/
    Edge/Dir/Cat/Side/Venue) and `ledger` fees; `Cargo.toml` untouched; 22 prior tests still green.
14. **Read-only invariant**: no order-placement code, no network I/O, no secrets touched.

## Launch Recommendation
PROCEED. 42/42 tests green (was 22), zero warnings; the fee + direction + twin invariants are pinned and
cross-checked against the authoritative Python.

## Self-review caveat
This is a self-review of code I just authored, so authorship bias applies. The financial-correctness
claims rest on parity tests I also wrote — I mitigated by cross-checking the three highest-risk pieces
(fee values, direction mapping, depth walk) against LIVE `python` output rather than only against my own
expected values, and one of my own test assertions was caught wrong by that discipline. Before this Rust
path ever trades real money, an independent reviewer (or a `python bot/ledger.py --selftest` +
`bot/monitor.py` selftest diff in CI) should confirm parity, per the project's "verify the fee path is
green before live" note in `ledger.rs` and the L14 fresh-eyes-review discipline.
