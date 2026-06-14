---
from: coding-agent (self-review pass)
run_id: 20260614-flip-add
timestamp: 2026-06-14T07:46:00Z
scope_reviewed: [scripts/flip_add_backtest.py:1-440]
critical_count: 2
warn_count: 3
info_count: 3
launch_recommendation: PROCEED
self_review: true
cross_references: []
---

## Goal Understanding
A READ-ONLY backtest answering a BUILD decision: would the FLIP (close+reverse) and ADD (scale-in)
extensions add profit on historical persistence data, given the live bot takes one position/market and
holds to settlement. Not tuning a live system. The honest gating output is the per-category flip base
rate; a "too rare to matter" answer is a valid (good) outcome, not a failure.

## Scope Reviewed
- scripts/flip_add_backtest.py — the only created file; reuses the proven harness, no upstream edits.

## Findings

### CRITICAL (must fix before launch) — both caught and self-resolved within this run
- Flip window scoped to the first edge episode, not the held position's lifetime
  - Location: flip_events / add_events (the `<= ep["close_t"]` bound, pre-fix)
  - Issue: under hold-to-settlement, the pair is held from the first capturable open to SETTLEMENT; a
    logged CLOSE only means the EDGE STATE left the book, not that the locked pair was sold. Scoping flips
    to `[open_t, episode close_t]` dropped 65 of 68 logged flips — they land in LATER edge episodes of the
    same still-held market. The pre-fix run reported a FALSE 0.0% flip rate across all categories.
  - Why it matters: a 0% gating number would have wrongly killed the FLIP idea outright; the corrected
    number is small but non-zero (sports 1.7%) and qualitatively different.
  - Fix: window = `[open_t, settle_t(market, open_t, 28h)]`, the same settlement proxy capital_sim uses.
  - Status: self-resolved within run.
- No opposite-direction guard on the flip trigger
  - Location: flip_events trigger condition
  - Issue: a logged FLIP record is a reversal vs the THEN-CURRENT edge state, but over a multi-flip held
    position the book can swing back to the ENTRY direction. Without `rdir != dir0`, 4 of 14 detected
    "flips" were same-direction bigger arbs (entry KP, recomputed reverse KP) — that is the ADD path, not
    a close-and-reverse cross-over. It inflated the flip count and would have double-counted those events'
    benefit as a flip.
  - Why it matters: goal misalignment — a flip's economics (close the held leg, re-enter reversed) only
    apply when the bigger arb is OPPOSITE the held direction.
  - Fix: require `rdir != dir0` in the trigger; selftest now asserts a same-dir reverse is excluded.
    Flip count corrected 14 → 10.
  - Status: self-resolved within run.

### WARN (fix or justify)
- Sports FLIP close mark is structurally unpriceable from the logged fields — DISCLOSED, not worked around
  - Location: close_pnl_game (returns None by construction)
  - Issue: the transition `px` for sports logs only the two Kalshi team YES *asks* (ka/kb), never the
    Kalshi YES *bid* needed to sell a Kalshi-backed leg. Since every cross-venue arb has exactly one Kalshi
    leg, the sports close P&L cannot be computed without fabricating a Kalshi bid ([L18]/[L28] forbid the
    convenient proxy). All 10 real flips are sports, so the "FLIP vs HOLD" headline the task asked for has
    ZERO fully-priced rows.
  - Justification/mitigation: I report the computable PARTIAL — the reverse-arb leg vs HOLD (+5.36c/arb),
    explicitly labeled a LOWER bound (the close gain is an unknown positive term, since you only close into
    a favourable basis). The caveat block states this prominently. Closing this fully needs a new logged
    field (Kalshi YES bid) on sports transitions — a monitor change, out of scope for a read-only backtest.
- n=1 ceil-fee asymmetry between HOLD and the close mark
  - Location: close_pnl_weather (Ledger.enter/unwind_all use the per-ORDER ceil fee at size 1)
  - Issue: HOLD's `net0`/`net1` come from the marginal-fee `signal()`/`game_edge`, while the close mark
    pays the n=1 ceil fee (~2c per Kalshi leg, [L10]). At unit size the close is therefore a CONSERVATIVE
    floor, not a like-for-like fee basis. Documented in the selftest comment and the report is per-contract
    at unit size. Moot for the real data (0 priceable flips), but if weather ever flips this would slightly
    understate the flip's PnL. Acceptable as a conservative floor; flagged so a future reader re-prices at
    real clip size if it ever matters.
- Effective-n is tiny and the span is < 4 days
  - Location: whole report
  - Issue: sports 10 flips / econ 4 filled / weather 0 flips over 5.08 d. Any PnL magnitude is a method
    demo, not validation. The report prints the `< 4 days` PRELIMINARY banner, the per-category effective-n,
    and the `effective-n TINY` note for n<20. Honest, but the reader must not over-read the $135 add figure.

### INFO (optional improvements / simplifications)
- The 4 same-direction events excluded from the flip count are genuine "bigger same-direction arb while
  held" opportunities, but they are logged as FLIP transitions (not WIDEN), so the ADD path (which keys on
  WIDEN) does not pick them up. Minor undercount of adds; left as-is to keep the ADD path cleanly keyed on
  the WIDEN kind per the task's definition.
- close_pnl_game's three params are unused (it always returns None). Kept for call-signature symmetry with
  close_pnl_weather and so the docstring documents WHY at the call site. Could be simplified to a constant,
  but the symmetry aids the reader.
- `--tau-gain` defaults to 1.0c, a fixed (non-oracle) trigger. A sweep would be informative but must be
  reported as straight/OOS application, not in-sample best ([L19]) — deliberately not swept here.

## Checks Passed
- Phantom filters APPLY at the shared chokepoint: 0 restart-censored episodes leaked into `filled`,
  0 episodes with c2<1, the [L21] econ off-by-one quarantine fired (36 records dropped by `load()`). The
  default-on filters removed 121 markets (830 unfiltered → 709 filled). A fresh harness re-admitting these
  inflated the last backtest 57% ([L28]) — not repeated here (reused `capturable()`).
- Close mark comes from REAL flip-time bids, not assumed: `close_pnl_weather` == a fresh `Ledger`'s cash
  after enter+unwind_all on the same px (asserted to 1e-12); returns None if a sell-leg touch is missing.
- Same-direction WIDEN is correctly EXCLUDED from the flip count and INCLUDED in adds (selftest);
  opposite-direction WIDEN is excluded from adds (selftest).
- Single-letter dir trap ([L28]) handled: weather/econ `P`/`K` routed via `signal`; sports `PK`/`KP` via
  `game_edge`; px-shape dispatch (`px_is_game`) verified on both real shapes.
- None-touch handling: `k_yb:null` and other missing touches return None (close unpriceable) rather than
  crashing or fabricating.
- Entry direction taken from the OPEN record, not the post-flip-mutated `ep["dir"]` (would mark the close
  on the wrong leg).
- Upstream selftests (analyze_persistence / capital_sim / ledger) still pass — import-only, no edits.
- READ-ONLY: no order code, no network, no writes outside the new script + the bus artifacts.

## Launch Recommendation
PROCEED. The two CRITICAL logic bugs were caught by the self-review's own diagnostics and fixed before
delivery; the result is now internally consistent and conservative. The headline is a HONEST NEGATIVE-ish
finding (flips are rare — sports-only at ~1.7%, and their close mark isn't even priceable from current
logs), which is a valid build-decision input, not a failure.

## Self-review caveat
This is a self-review of code I just authored — authorship bias applies. The result feeds a BUILD decision,
not a signed preregistration or a live launch, so the bar is "is the measurement honest and the cohort
clean," which it is. The single biggest reservation an independent reviewer should probe: the FLIP idea's
actual profitability is UNMEASURED (sports close mark unpriceable from logged fields), so the only honest
read is "flips are too rare AND too unmeasurable on current data to justify building yet" — if the owner
wants a real flip PnL, the prerequisite is logging the Kalshi YES bid on sports transitions, not this
backtest. An independent `stats-ml-logic-reviewer` pass on the tiny-n add figure would also be warranted
before any prose quotes the $135.
