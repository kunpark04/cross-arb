---
from: stats-ml-logic-reviewer
run_id: 20260615-2207
timestamp: 2026-06-15T22:07:00Z
scope_reviewed: p_hedge_measure.py +EV maker-mode go/no-go vs the NOT_VIABLE a-priori skeptic
verdict: unsound
critical_flags: 1
warn_flags: 3
info_flags: 2
cross_references: []
---

## Verdict

**UNSOUND. The +EV is a measurement artifact; the skeptic is right.** The script reports p_hedge =
51.6%/64.1% and EV = +0.50c/+0.83c, but this rests on a **two-clock timestamp bug** in the hedge-at-fill
lookup. `detect_fill` returns the trade's **venue fill time `vt`**, while the ladders are stamped on the
**monitor receive clock `t`**. On the Kalshi trade feed these differ by a **structured ~150 s median
offset** (`t − vt`: p50 154 s, p99 309 s, always positive — trades arrive batched, minutes after they
printed). `ladder_at(obs, vt, 300)` therefore prices the pmus hedge against the book **~150 s BEFORE the
fill propagated** — i.e., before the informed flow that filled the Kalshi maker had reached and emptied
pmus. That is precisely the adverse-selection look-ahead the skeptic described, smuggled in via a clock
mismatch. Re-pricing the hedge on a **clock-consistent** basis (match the ladder to the trade's
monitor-receive `t`, the same clock the ladders use) collapses the result to **p_hedge = 33.8%/43.5%,
EV = −1.06c/−0.50c** — inside the skeptic's predicted 25–40% / wrong-sign band. The sign is negative
across every maker floor (0–2c) and both fill modes. **Do not advance to a live maker build on this
number.** The measurement *rig* is sound and salvageable; the *result* is not.

## Methodological Audit

- **Choice of instrument (measure p_hedge from logs, not assume it): correct and exactly right.** Converting
  the a-priori dispute into a measurement off WAVE-2 trades+ladders is the right move. Reusing the verified
  `maker_feasibility.hedge_eval`/fee math (single source of truth, L15) is good discipline. The placement→
  fill→hedge-at-fill decomposition mirrors EV/fill = p_hedge·E[net|hedged] + (1−p_hedge)·E[cost|naked]
  faithfully. Selftest passes; fee math validated independently.
- **CRITICAL assumption violated — single time axis.** The whole hedge-at-fill step assumes `vt` (venue clock,
  from trades) and ladder `t` (monitor-receive clock) are interchangeable for a nearest-neighbour lookup.
  They are not. Ladder records carry ONLY `t` (verified: keys = `t,market,k,pb,pa,kb,ka`; no `vt`). The
  trade `vt` lags its own receipt by ~150 s (Kalshi batches the trade WS). Direction of bias is
  deterministic and optimistic: the matched book is always the *earlier*, *pre-adverse-selection* state.
- **Alternatives not considered.** (a) Match the hedge ladder to the trade's monitor-receive `t` (same clock)
  — this is the clock-consistent fix and is what flips the sign. (b) Tighten `MAX_GAP_S` — done below; under
  the BUGGY clock it spuriously *raises* p_hedge (the artifact intensifies as you zoom into the stale book),
  which is itself a tell that the matching is look-ahead-contaminated.

## Data Audit

- **Descriptive reality.** 192 weather ladder markets / 140 trade markets; 1886 placement anchors; ~5–6 days
  (2026-06-10→16), weather-only, ATM-dominated — as stated. Median time-to-fill 732 s, p90 ~16,000 s (4.5 h),
  max 26 h. A maker resting on a weather book for *hours* is the modal case, not the exception.
- **Leakage / contamination — the load-bearing one.** Temporal look-ahead via the `vt`/`t` clock skew (above).
  Quantified flip asymmetry (join, gap≤300 s, 1720 fills evaluated both clocks): **hedged@vt → NAKED@recv =
  19.9%**, naked@vt → hedged@recv = 2.2%, same = 77.9%. Net −17.7 pts of p_hedge is pure look-ahead inflation.
  The asymmetry (≈9:1) is the fingerprint of a directional leak, not symmetric timing noise.
- **Representativeness.** Weather-only, one-day-dominated cadence (per CLAUDE.md, 06-10 ≈ 52% of episodes
  historically). Deployment maker mode would also face the pmus-thin regime that dominates live taker fires
  (72/94, ~77%) — which the corrected p_hedge (~34%) is consistent with and the headline (~52%) is not.

## Result Interpretation

- **Headline vs corrected (clock-consistent) — the decision number flips sign:**
  | matching | JOIN p_hedge | JOIN EV | IMPROVE p_hedge | IMPROVE EV |
  |---|---|---|---|---|
  | as-written (`vt` vs ladder `t`) | 51.6% | **+0.50c** | 64.1% | **+0.83c** |
  | clock-consistent (recv-`t`) | **33.8%** | **−1.06c** | **43.5%** | **−0.50c** |
- **MAX_GAP_S sweep under the BUGGY clock** (the user's #1 worry — and the result is diagnostic): tightening
  the gap *increases* p_hedge/EV (300 s→5 s: 51.6%→75.2%, +0.50c→+2.63c). Naively this looks like "tighter =
  more trustworthy = still +EV," but it is the opposite: with a ~150 s clock offset, the tightest-gap matches
  are the ones landing deepest inside the stale pre-fill book, so the look-ahead is *strongest* there. The
  monotone rise as gap→0 is evidence FOR the leak, not against it. Under the corrected clock the gap sweep is
  ~flat at p_hedge≈32–34% (no cadence sensitivity) — confirming cadence coarseness was never the real issue;
  the clock was.
- **Corrected EV is robustly negative**: across floors {0,0.5,1,2}c and both modes, EV ∈ [−1.07c, −0.24c],
  p_hedge ∈ [23%, 44%]. Raising the floor lowers p_hedge (thicker-edge opens correlate with the adverse
  moving-book regime) — so a "skip thin opens" rule does NOT rescue it.
- **Tail toxicity (item 3): confirmed.** Slow fills (>1 h, the bucket-death tail, 448/1726 ≈ 26%) hedge at
  24.6% vs 37.1% for fast (≤1 h) fills. The long tail is genuinely toxic and pulls p_hedge down. Long-tail
  fills DO dominate the drag.
- **Recovery under-priced (item 4): confirmed.** Corrected naked branch median −2.20c, mean −2.88c, p10
  −6.71c, **min −67.84c** — matching the live −2c+ flattens and the one −51c event. The headline's −1.44c
  median was itself optimistic (the same early clock prices the Kalshi re-cross against the pre-move book).
- **No CI reported.** Point estimates only. Even the corrected −0.5 to −1.1c should carry a city/day-clustered
  bootstrap CI before being treated as decisively negative (it almost certainly is, but quantify it).
- **What it licenses:** with the clock fixed, the data REJECTS +EV for weather maker mode at this horizon and
  CONFIRMS the NOT_VIABLE skeptic. It does NOT license the original +EV ship-advance.

## Impact on Project Scope

- **If the as-written +EV had stood:** it would have greenlit building a live maker mode (real engineering +
  live-capital risk) on a phantom — the project's documented recurring failure mode (phantom edges:
  37.7c ITF book-init, 12.2c U-3 off-by-one). A false +EV here is exactly the expensive mistake the owner
  flagged.
- **Given the corrected −EV:** the maker-mode build should NOT advance to shadow/paper on this evidence.
  The taker-mode path and the existing maker_feasibility bounds (+0.14–0.44c, which priced only the hedged
  branch and explicitly never priced the miss) remain the state of knowledge — and that bound is now
  understood as an upper sliver, not the EV.
- **Consistency with prior findings: CONFIRMS** the 2026-06-15 risk verdict (NOT_VIABLE) and the live
  observation that pmus is the binding thin leg failing ~9× Kalshi, with displayed sports/weather depth
  evaporating at fire ([0025]). The skeptic's mechanism (Kalshi maker fills on the same informed flow that
  empties pmus) is corroborated by the 19.9%/2.2% flip asymmetry.

## Recommended Alternatives (ranked by information value)

1. **Fix the clock, re-run — the single highest-value change.** In `ladder_at`/`measure`, match the hedge
   (and recovery) ladder to the **trade's monitor-receive `t`**, not `vt`. Concretely: have `detect_fill`
   return the trade's `t` (or a `(vt, t_recv)` pair) and pass `t_recv` to `ladder_at`. Keep `vt` only for
   reporting true time-to-fill. Expected outcome: p_hedge ≈ 34%/44%, EV ≈ −1.06c/−0.50c. Decision rule: if
   corrected EV ≤ 0 with a clustered-bootstrap upper CI < +0.2c, do NOT build maker mode. (Already computed
   here; the implementer should reproduce it inside the script as the headline.)
2. **Cluster-bootstrap CI on corrected EV, resampled by (city,date).** Answers: is the negative sign
   significant or could it be ~0? n_eff is days×cities, not 1726 fills (heavy within-day/within-market
   correlation; the 1886 anchors are ≤104 markets over ~6 days). Decision rule: report EV with a 90% CI; a
   CI straddling 0 means "not viable AND not yet powered," not "viable."
3. **Pre-register a maker floor + a forward p_hedge gate BEFORE more data.** The 48.7%-of-anchors-within-1c
   marginal opens should be excluded by a real floor; freeze τ and a p_hedge threshold now (per the [0014]
   prereg discipline) so the multi-week read is confirmatory, not fitted. Decision rule: advance to paper
   only if corrected forward p_hedge clears the pre-registered threshold on held-out weeks.
4. **Add a pmus-book-staleness / liveness guard to the hedge-availability test.** Even clock-corrected, the
   nearest ladder can be a stale pmus quote; require a pmus ladder update within N s of the corrected fill
   time AND `hedge_vol ≥ clip` (the latter is present; the freshness gate is not). Answers: how much of the
   residual ~34% p_hedge survives a freshness requirement.
5. **Separate the bucket-death tail explicitly.** Report p_hedge/EV for a cancel-after-T policy (e.g. T=1 h),
   since a real maker cancels before bucket death. This is the only branch with a plausible path to neutral
   EV and should be measured on its own, clock-corrected.

## Severity Flags

- **CRITICAL:** Hedge-at-fill (and recovery) ladder is matched on `vt` (venue clock) against ladders stamped
  on monitor-receive `t` (`p_hedge_measure.py:218–228` `ladder_at`, called at `:270` with `vt` from
  `detect_fill` `:266`). The ~150 s structured offset injects directional look-ahead; correcting it flips the
  decision number from +0.50/+0.83c to −1.06/−0.50c and p_hedge from 52%/64% to 34%/44%. This invalidates the
  +EV conclusion.
- **WARN:** Recovery cost in the naked branch is optimistic for the same clock reason (median −1.44c headline
  vs −2.20c corrected; tail to −67.84c) — `evaluate_fill` `:240–248` prices the Kalshi re-cross at the stale
  `vt`-matched ladder.
- **WARN:** No uncertainty quantification; point estimates over a ~6-day, one-day-dominated, weather-only,
  ATM sample with strong within-market/day correlation. Even the corrected −EV needs a clustered CI.
- **WARN:** Default `floor_c=0` counts 48.7% of anchors as marginal (0,1c] opens a real maker would skip;
  pads the denominator (does not change the sign, but inflates n and dilutes per-attempt interpretation).
- **INFO:** `place_net_c` max 63.88c echoes the project's phantom-magnitude pattern (book-init / off-by-one);
  worth a spot check on the top tail, not load-bearing for this verdict.
- **INFO:** `MAX_GAP_S` sweep is non-diagnostic UNDER the bug (tighter gap raises the artifact); becomes flat
  and informative only AFTER the clock fix. Keep the sweep in the corrected script as a leak tripwire.

### Reconciliation — who is right and why
The **skeptic is right on the sign**; the measurement is right on *method* but wrong on *execution*. The
skeptic's "~25–40% p_hedge" was NOT a conflation of the 23% taker leg-fill rate — the clock-corrected
measurement independently lands at 34%/44% (join/improve), validating the skeptic's adverse-selection
mechanism directly from the logs. The +EV was the artifact: a ~150 s venue-vs-monitor clock skew let the
hedge be priced against the pmus book *before* the informed flow emptied it. Fix the clock and the
measurement and the a-priori argument agree. The +EV sign is **not robust enough to advance**; it is
**negative once the look-ahead is removed.**
