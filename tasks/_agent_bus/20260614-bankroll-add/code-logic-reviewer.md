---
from: coding-agent (self-review pass)
run_id: 20260614-bankroll-add
timestamp: 2026-06-14T08:54:01Z
scope_reviewed: [scripts/bankroll_add_backtest.py:1-417, scripts/flip_add_backtest.py:236-272]
critical_count: 1
warn_count: 2
info_count: 2
launch_recommendation: PROCEED
self_review: true
cross_references: [tasks/lessons.md (L18,L20,L21,L28,L30), memory/venue-wallets-separated.md, tasks/_agent_bus/20260611-probes]
---

## Goal Understanding
Give the owner the TRUE combined 5-day PnL of the base single-entry strategy PLUS adds under the separated
$250/$250 two-pool model, so the add contribution is netted against real capital (an add needs room in BOTH
pools) — reported as the DELTA vs base, NOT the unconstrained per-market upper bound flip_add reports.

## Scope Reviewed
- `scripts/bankroll_add_backtest.py` (new) — the unified causal two-pool walk, 4 scenarios x 2 cohorts, report, 6 selftests.
- `scripts/flip_add_backtest.py:236-272` (additive) — WIDEN px captured + `widen_t`/`widen_px` surfaced on `add_events` output.

## Findings

### CRITICAL (must fix before launch)
- Realized-only delta is the WRONG quantity (window-boundary reclassification artifact) [L18]
  - Location: bankroll_add_backtest.py report (original `_scenario`/section/bottom-line)
  - Issue: my first draft quoted dRealized as "the marginal $ from adds." On all-verified, the one funded
    re-entry add (ITF tennis `joagui-leoros`, 06-13, 186 ctr) shows dReal +$8.22 — but it settles IN-window
    and crowds out 3 base pairs that settle on 06-14 (OUT-of-window). Those base pairs flip from realized to
    forgone; net dTOTAL = -$0.34. The +$8.22 is NOT new edge, it is a 5-day-window settlement-timing artifact
    (exactly the L18 "favorable number is the wrong quantity" shape — a high-edge in-window add reshuffles
    which positions land inside the window).
  - Why it matters: quoting +$8.22 would tell the owner "re-entry adds add ~$8" when bankroll-netted they add
    ~$0 (and slightly negative). A launch/sizing decision off that would be wrong by ~24x.
  - Suggested fix: lead the report with dTOTAL (realized+unrealized mark-to-edge), which is window-invariant;
    show dReal alongside with an explicit NB when |dReal - dTOTAL| > $0.10.
  - Status: self-resolved within run. Report now leads with dTOTAL; selftest case (vi) proves the
    dReal>dTOTAL crowd-out property so the framing can't silently regress.

### WARN (fix or justify)
- Add's leg->pool split uses the WIDEN px, which is frequently None in real records
  - Location: bankroll_add_backtest.py:_item_economics (add branch passes `it["widen_px"]`)
  - Issue: real WIDEN records often carry `px: null` (verified in transitions-2026-06-1x). leg_split returns
    0.5 (50/50) on None. So a None-px add is split evenly across pools rather than by its true venue leg cost.
  - Justification (not fixed): this is the SAME 0.5 fallback venue_split's base walk uses for a None-px base
    arb — consistent with the proven harness, and documented. A None-px add can't be leg-split any other way
    without fabricating touches (an L28/L18 violation). Disclosed in the docstring + the leg_split contract.
- Add settlement timing leans on settle_t's slug-date proxy, same as base
  - Location: bankroll_add_backtest.py:154 (add settle_t = settle_t(market, widen_t, OFFSET))
  - Issue: the add settles on the bucket's slug date (+28h), floored at widen_t+1h. For a same-day widen this
    equals the base settle; for a widen days after the slug date it floors to widen+1h. This is the standard
    capital_sim proxy (the whole project uses it) but it is a proxy, so the realized/unrealized SPLIT inherits
    its uncertainty — which is exactly why dTOTAL (split-invariant) is the headline. Justified, disclosed.

### INFO (optional improvements / simplifications)
- The unified `two_pool_walk_with_adds` duplicates venue_split's base-sizing block rather than calling
  `venue_split.two_pool_walk`. I verified bit-for-bit equivalence (realized/unreal/entered/both peaks all match
  on both cohorts), so this is faithful composition — but a future refactor could factor venue_split's
  per-item base step into a shared primitive both walks call, eliminating the duplication. Not done now
  (would touch the shipped venue_split walk; out of scope, and the equivalence test guards drift).
- `--add-cap-frac` is plumbed to `build_report`/`add_events` but the walk hardcodes `ADD_CAP_FRAC` in the add
  sizing. They agree at the default (0.20); a non-default `--add-cap-frac` would make the add COUNT/UB use the
  CLI value while the walk's funded-size cap uses the constant. Low-impact (the default is the only sane value
  here) but worth aligning if the flag is ever exercised. Flagged, not fixed (keeps the diff minimal).

## Checks Passed
- BASE-only walk == venue_split.two_pool_walk bit-for-bit on weather-only AND all-verified (realized,
  unrealized, entered count, peak Kalshi, peak pmus) — the delta is against the TRUE reference.
- leg->pool mapping [L28]: single-letter binary dir P/K routed through the binary branch (selftest v proves
  a lopsided book puts ~0.94 on Kalshi vs ~0.03 on pmus for dir P — NOT an accidental 50/50). Sports PK/KP
  inherited from venue_split.leg_split unchanged.
- both-pools-have-room check: an add funds iff BOTH `kcash//k_unit >=1` AND `pcash//p_unit >=1`; selftest (i)
  proves SKIP when EITHER pool is full (incl. the asymmetric Kalshi-full / pmus-idle case).
- capital-free-at-settlement: per-pool free at each position's own settle_t; selftest (iv) proves a
  post-settlement add reuses freed pool room while a concurrent one is skipped.
- no double-count: the base pair and its add are two SEPARATE held positions competing for the pools (the add
  is a 2nd deployment, not a resize of the base); each frees independently. The unified stream processes a
  market's base open before its own add (add_events guarantees widen_t >= open_t).
- phantom lens applied: base cohort via venue_split._live_cohort (capturable drops restart-censored +
  open_flat per [L20]); adds via add_events(drop_flat_widen=True) ([L20] lens on the driving widen). [L21]
  econ off-by-one quarantined by load() (36 records, logged at runtime).
- the DELTA IS the real marginal contribution: A is the exact venue_split base; B/C/D add only the
  interleaved add attempts that consume residual pool room; dTOTAL isolates the bankroll-netted economic
  marginal value, window-invariant.
- all upstream selftests still green (flip_add, venue_split, capital_sim, backtest_current) — the additive
  flip_add change regressed nothing.

## Launch Recommendation
PROCEED — the script is a READ-ONLY paper measurement rig; its base equals the proven venue_split reference,
the add netting + both-pools rule + capital recycling are selftest-proven, and the one CRITICAL framing flaw
(realized-only delta) is fixed with dTOTAL as the headline + a regression-guarding selftest.

## Self-review caveat
I authored this code, so authorship bias applies. The dTOTAL-vs-dReal call is a methodology judgment a
stats-ml-logic-reviewer should independently sanity-check before any number is quoted into a research brief
or decision log — the figure is decision-useful but rests on the L18 window-artifact reasoning. The result
is PAPER/GROSS on effective-n ~5 event-dates; it is a method demo, not a validated return.
