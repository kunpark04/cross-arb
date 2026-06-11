# Probe #2 (sub-second leg-fill / taker viability) + #5 (adverse-selection direction gate)

2026-06-11 ~01:30 UTC. Post-0013-epoch data only (`t >= 1781082189`, detection-time CLOSE stamps): the
**first window in which the sub-second regime is measurable at all** ([L22]). All numbers from code run
this session; all selftests green (`selftest_all.py` 18/18 after edits).

**Data**: 31,297 post-epoch transitions over **15.94 h (0.66 d)** — single weekday window, sports-dominated.
4,964 episodes → **575 capturable ≥1¢** (shared gate `capital_sim.capturable`: open_net ≥ 1¢, open_c2 ≥ 1,
restart-censored dropped): sports 476, weather 99, **econ 0** (23 econ episodes, 19 restart-censored incl. a
13¢ U-3 open at a reconnect — correctly quarantined [L20]; 4 clean ones fail the edge/depth gate). 49
post-epoch censor events (reconnect/resync churn) → 206 restart-censored episodes excluded.

**Code shipped** (only my two files): `scripts/shadow_fill.py` — sub-second grid
{0.05, 0.1, 0.15, 0.25, 0.5, 1, 2}s (+0/5/10 kept), `--post-epoch`, opt-in lenses `--window-min/--liq-floor`
through the **shared** `capital_sim.capturable` (replaced the private gate copy, [L20]), `--by-category`,
duration distribution, `--json-out`; selftest extended (sub-second points, post-epoch filter, duration dist,
lens passthrough). `scripts/adverse_selection.py` — sports PK/KP close-attribution via implied A-space
(`PK: cheap=pm_a, dear=1−kb; KP: cheap=ka, dear=pm_b` — semantics verified line-by-line in `bot/monitor.py`
GameTracker, [L3]), per-category breakdown, `--post-epoch`, and the **at-open direction-gate classifier**
(`open_classes` + `gate_report`: OPEN px vs the market's previous px-bearing record, censor-gap-aware);
selftest extended (PK/KP, open attribution, gate join end-to-end).

## #2 — Shadow-fill, post-epoch (n=575 capturable ≥1¢; OPTIMISTIC upper bound — transitions are coarse)

| L (s) | survival % | naked-leg % | realized med / mean (¢, survivors) |
|--:|--:|--:|--:|
| 0.05 | 90.6 | 9.4 | 2.01 / 3.09 |
| 0.10 | 83.0 | 17.0 | 1.99 / 3.05 |
| 0.15 | 76.7 | 23.3 | 1.85 / 2.97 |
| 0.25 | 70.6 | 29.4 | 1.77 / 2.96 |
| 0.50 | 59.7 | 40.3 | 1.90 / 3.09 |
| 1.0 | 52.7 | 47.3 | 1.90 / 2.95 |
| 2.0 | 45.0 | 55.0 | 1.95 / 3.08 |

- **At the measured 86–261 ms RTT (interpolated): ~85% → ~70% both-leg survival, i.e. 15–30% naked-leg
  rate.** Survivors keep ~1.9¢ median regardless of L — latency kills the hit *rate*, not kept-hit size
  (confirms the prior shape).
- **Subsets** ([L15] baseline above; [L18] decision lens): ex-ante observable deep lens **c2 ≥ 100**
  (n=310): 80.0% @0.15 s / 57.7% @1 s — depth buys only ~3pp; **depth does not rescue the race.**
  Outcome-conditioned `dur ≥ 30 s AND c2 ≥ 100` (n=51): 100% survival at every L (trivially — selected on
  duration, the L18 "persistent+deep never has a leg-fill problem" cohort; not ex-ante tradeable info).
- **Category** (n): weather 99 — 85.9% @0.15 s but realized median decays 2.02→1.18¢ by 1 s (weather edges
  NARROW before dying); sports 476 — 74.8% @0.15 s, realized holds ~2¢; econ 0 — **16 h is insufficient;
  econ capturability is release-calendar-gated, not span-gated.**
- **Stability deflator**: split-half @0.15 s = **69.7% vs 85.7%** (n=323/252) — a 16pp regime swing inside
  one 16 h window. The aggregate carries regime error far beyond its binomial SE (~1.8pp).

## #3 — Duration distribution, capturable ≥1¢ (n=575)

`<0.25 s: 29.2%  <0.5 s: 40.3%  <1 s: 47.3%  <2 s: 55.0%` (<5 s 62.6%, <30 s 85.6%).

vs the corrected pre-epoch claims: **"~27% die ~instantly" CONFIRMED and now resolved — 29.2% die inside
250 ms** (9.4% inside 50 ms). The 1 s/2 s leg-fail claims **SOFTEN ~8pp**: 55.5%→47.3% @1 s, 62.7%→55.0%
@2 s. Net read: the sub-second curve is steep but not cliff-like — at real RTT you lose 15–30%, far better
than the 55.5%@1 s number implied, **but** the proxy is optimistic (no queue competition, books move
between logged transitions) and the cohorts differ (0.86 d pre vs 0.66 d post, different market mix).

## #4/#5 — Adverse selection + direction gate (post-epoch)

**Close attribution** (n=4,746 of 4,749 OPEN→CLOSE pairs; px coverage 100%): cheap_rose 54% / dear_fell
(toxic) 46% overall. By category: **weather 65/35 benign-leaning** (n=862), **sports 52/48 coin-flip**
(n=3,880), econ n=4 (no signal possible). Median |move| both sides = 1¢.

**At-open classification IS possible from logged fields**: px is logged on every transition, so OPEN px vs
the market's previous record classifies **3,980/5,076 opens (78%)**. The 22% gap: 316 first-sightings
(no prior record — would need a px snapshot at subscribe/heartbeat, the one missing field-source), 778
censor-gap (prior spans a reconnect), 2 missing legs.

**Gate predictive power on the capturable cohort** (cheap_made n=217 / dear_made n=217 / unclassified 141):
- (i) duration: median 1.44 s vs 1.84 s — no useful separation.
- (ii) close toxicity: 48% vs 51%, **z = −0.68 — null overall**. Fresh-gap (≤60 s) and strong-attribution
  (|Δ|≥1¢) lenses: still null (z −1.06, −0.60).
- (iii) shadow-fill: @0.15 s 81.1% vs 76.0% (z=1.29), @1 s 53.5% vs 57.1% (sign flips) — null.
- **EXCEPT WEATHER — a strong REVERT signature**: cheap_made → **18% toxic** (n=33) vs dear_made → **79%
  toxic** (n=24), **z = 4.58**. Whichever side moved to create the gap is the side that closes it: the
  weather "edge" is largely a one-venue transient quote that mean-reverts. Cluster-robust: 11/13 vs 8/10
  distinct markets by market-majority tag, 4–5 cities, both event dates. Sports show the opposite,
  insignificant tilt (z=1.10) — consistent with sports gaps being real repricings, weather gaps flickers.
  Caveat: subgroup finding after an overall null (multiple-comparisons risk), n=57 episodes / 24 markets /
  2 event-dates — **hypothesis-grade, not a result** ([L19]).
- Note the weather signal predicts *which leg you'd miss*, **not** survival (88.9% vs 88.0% @0.15 s) — its
  use is **leg-sequencing** (fire the just-moved, perishable side first; treat dear_made weather opens as
  "the bid you must hit is about to revert"), not a skip filter.

## #5 verdict

**Naked taker execution plausibly survives at the measured RTT, conditional on an unmeasured quantity.** At
86–261 ms the both-leg survival is ~85→70% and survivors keep ~1.9¢ median, so per attempted $1-pair
EV ≈ surv×1.9¢ − (1−surv)×(naked-unwind cost): breakeven unwind cost ≈ **6.2¢ @150 ms / 4.3¢ @261 ms**.
A naked unwind should cost ~1–3¢ (re-cross the spread + fees + the ~1¢ median adverse move) — comfortably
inside breakeven — **but unwind cost is not yet measured and the shadow proxy bounds from above**, so this
is "no red light", not a green one. **The direction gate as designed (skip filter) is NOT worth building** —
overall null with n=217/217 and sign-flipping survival deltas. **The weather revert signature IS worth a
pre-registered confirmation** (61pp toxicity spread, z=4.58, cluster-robust) as a *leg-sequencing* rule.
**Upgrade triggers**: (a) ~1 week of post-0013 data (≥5 weekdays + a weekend) → weather gate cells n≈150–250
each / ≥50 markets per class, and per-day split-halves to bound the 16pp regime swing; (b) ≥1 econ release
cycle for any econ statement (span alone won't do it); (c) to close the 22% unclassifiable opens, log a px
snapshot at subscribe (monitor change — owner-gated); (d) the real go/no-go needs the **naked-unwind cost**
measured (next probe: reconstruct post-CLOSE px to price the losing-leg exit).

## What would deflate these numbers

1. The shadow proxy is an **upper bound**: transitions fire only on >ε moves; intra-transition book motion,
   queue position, and partial fills all cut true survival.
2. **Regime instability**: 16pp split-half swing @0.15 s; one weekday, sports-heavy mix.
3. Weather-gate cells are small subgroup cells found after an overall null.
4. Sports realized edge ignores the postpone/void tail (research/sports-settlement-verification.md).
5. eod-censored episodes (9/4,964) are duration lower bounds — negligible here.

## Artifacts

- `scripts/shadow_fill.py`, `scripts/adverse_selection.py` (edited; selftests green; `selftest_all.py` 18/18)
- `scripts/_data/shadow_fill_postepoch_20260611.json` — all grids (baseline, lenses, categories)
- `scripts/_data/adverse_selection_gate_postepoch_20260611.json` — close attribution + gate stats
- `scripts/_data/gate_lenses_postepoch_20260611.json` — fresh/strong/per-category gate lenses
- Repro: `python scripts/shadow_fill.py --post-epoch --window-min 30 --liq-floor 100 --by-category`;
  `python scripts/adverse_selection.py --post-epoch`
