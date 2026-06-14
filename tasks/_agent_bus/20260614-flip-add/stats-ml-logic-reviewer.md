---
from: stats-ml-logic-reviewer
run_id: 20260614-flip-add
timestamp: 2026-06-14T08:40:00Z
scope_reviewed: scripts/flip_add_backtest.py (commit 70c9867) — FLIP/ADD feasibility backtest; verified against raw transitions-*.jsonl (06-08..07-02) + the reused harness (analyze_persistence, capital_sim, ledger, monitor)
verdict: partially sound
critical_flags: 0
warn_flags: 3
info_flags: 3
cross_references: [tasks/_agent_bus/20260614-flip-add/coding-agent.md, tasks/_agent_bus/20260614-flip-add/code-logic-reviewer.md]
---

## Verdict

The headline qualitative conclusion is **SOUND and well-evidenced**: flipping is not worth building yet
(structurally absent in weather/econ, rare in sports at a Wilson-95%-CI of [0.90%, 3.02%], and its profit
is genuinely unmeasurable because the close-mark field is missing from the logs), and adding is marginal/
unvalidated on 5 days of data. I independently reproduced every reported number and traced each of the three
load-bearing claims to raw records or a recomputation. The two CRITICALs the self-review claims to have
caught (flip-window scoping, opposite-direction guard) **are genuinely fixed in the committed code** (lines
141/203, 166), and the code does NOT repeat the L20/L28 base-cohort phantom failures — `capturable()` is the
real chokepoint, 0 restart-censored episodes leaked, the econ L21 quarantine fired (36 records).

**The $135 ADD figure is NOT quotable as a point estimate.** Not because it is a single-bet or oracle
artifact — I checked and it is neither (top-1 share 6.5%, 323 distinct settlement clusters, cap monotone not
tuned). It is unquotable because it carries **two undisclosed sensitivities** that move it by tens of percent:
(1) 27.1% of it ($36.62) rides on WIDENs with a flat `c2==c1==c0` ladder — the L20 phantom fingerprint — on
a code path that bypasses the phantom filter; and (2) the held-to-settlement window is hardcoded at 28h and is
strongly load-bearing (add PnL $104→$135 over a 4h→28h hold; 301/408 add opportunities fire AFTER the base
edge episode already closed, so they are separate later arbs counted as "adds" only under the hold
assumption). The defensible statement is a **range, ~$99–$135 paper-gross**, explicitly conditioned on the
hold horizon and the flat-WIDEN treatment, labelled preliminary. The single biggest methodological risk is
the **undisclosed hold-window lever** (WARN-1).

## Methodological Audit

- **Choice of harness:** Correct and disciplined. Reuses `load`/`build_episodes`/`category` +
  `capturable`/`one_per_market`/`settle_t`/`void_haircut`/`DEPTH_BOUNDARY_NET` + `signal`/`game_edge`/`Ledger`.
  No re-derived cohort, no re-implemented economics — directly addresses the L28 failure mode. The close-mark
  reuses `Ledger.enter`+`unwind_all` so fees/signs are pinned to the bot's accounting core (selftest asserts
  `close_pnl_weather == Ledger.cash` to 1e-12). This is the right way to build a derivative backtest here.
- **FLIP detector correctness (Question 1) — VERIFIED as a real structural finding, not a detection bug.**
  Three independent confirmations that a genuine weather basis-cross WOULD fire a FLIP:
  (a) the monitor classifier emits FLIP on any `new["dir"] != prev["dir"]` (monitor.py:58-59), and `signal()`
  returns the single-letter `P`/`K` dir for weather (ledger.py:63,66) — the L28 trap is handled at source;
  (b) the monitor selftest constructs an explicit weather basis-cross and asserts the FLIP:
  `P0.72/0.70 K0.62/0.60 -> FLIP` (ran it live, passes);
  (c) the backtest's own selftest builds a weather FLIP and asserts detection + correct close (lines 417-421).
  The raw data has **0 weather FLIP transitions across 20,310 weather records** (and 0/810 econ) — the absence
  is in the data, not in the backtest's reading of it. `reverse_net_edge` correctly routes weather px through
  `signal()` not `game_edge` (px-shape dispatch verified on both real shapes). The 0/101 is a true 1:1-bucket
  basis-convergence property; rule-of-three 95% upper bound ~3.0%, consistent with structural zero.
- **Assumptions that hold:** single-letter vs two-letter dir dispatch (verified against real records: weather/
  econ `{p_yb,p_ya,k_yb,k_ya}` P/K; sports `{pm_b,pm_a,ka,kb}` PK/KP); None-touch handling; phantom filters at
  the base cohort; first-causal (non-oracle) widen selection.
- **Assumptions that are UNTESTED / load-bearing (the real exposure):**
  - **Hold horizon = slug-date + 28h, hardcoded, no sweep.** `capital_sim` itself treats settlement timing as
    uncertain enough to run a W-sensitivity sweep; this derivative backtest runs none. Both the flip count and
    the add PnL inherit a hidden sensitivity to this proxy (quantified in Result Interpretation).
  - **Flat-ladder WIDENs admitted on the add path.** The add reads the WIDEN's raw `depth.c2`/`net_edge`; this
    record never passes `capturable()`, so neither `drop_flat` nor any age/staleness gate touches it.
- **Alternatives not considered:** (1) a hold-window sweep (cheap, the W-sweep already exists in capital_sim);
  (2) reporting the add headline with `drop_flat`/age applied to the WIDEN record; (3) a cluster/day bootstrap
  CI on the add PnL (effective-n is ~5 event-dates); (4) for flips, the report could note that selecting on
  net0→net1 crossings is itself a max-order-statistic over a noisy basis — the reverse net1 values are biased
  high by selection (you only see the crossings that grew), so even the lower-bound "reverse leg" benefit is
  optimistic. None of these are fatal omissions for a go/no-go-on-BUILDING artifact, but (1) and (2) are the
  difference between a quotable number and an unquotable one.

## Data Audit

- **Descriptive reality (independently recomputed):** 99,198 transitions over span 5.08 d; 36 econ records
  quarantined (L21 fired). Filled cohort 709 markets (weather 101 / sports 604 / econ 4). FLIP transitions in
  raw data: weather 0, econ 0, sports 105. Detected flips after the opposite-direction + tau_gain gate: 10
  sports. Adds: 348 (sports 302 / weather 43 / econ 3).
- **Leakage / contamination — CLEAN on the channels that bit this project before:** 0 restart-censored leaked
  into `filled`; 0 c2<1; the econ off-by-one (L21) quarantine fired. The base cohort is not re-derived. No
  combo-ID / train-test channel applies (this is a feasibility count, not a fitted model).
- **The phantom channel that is NOT clean (WARN-2):** 93/348 add opportunities (27.1% of $135 = $36.62) are
  driven by a flat `c2==c1==c0` WIDEN ladder. This is the L20 fingerprint, on a path the chokepoint doesn't
  cover. Partially defensible — a mid-life WIDEN flat ladder is more plausibly a small real book than a
  book-init flat ladder captured 1.5s after a resubscribe — but it is the same SHAPE as the L28 failure
  (there 57%, here 27%) and is undisclosed. Only 9 markets ($0.88) are "truly only-flat" at the base-cohort
  level, so the base admission itself is robust; the exposure is entirely on the add leg.
- **Representativeness:** 5.08 days, 9 event-dates, ~5 of which carry essentially all the add PnL
  (06-10 alone = 35.6%). Span < 4d; the report prints the PRELIMINARY banner. Deployment distribution
  (multi-week, all sessions) is not yet sampled — correctly flagged.

## Result Interpretation

- **FLIP base rate — statistically robust as a "rare" finding.** Sports 10/604 = 1.66%, Wilson 95% CI
  [0.90%, 3.02%]. Weather 0/101, rule-of-three upper bound ~3.0%, and the upstream 0-transitions confirmation
  makes a true structural zero the right read. The gating conclusion ("too rare to build") survives the CI.
- **FLIP composition — one report caveat is slightly mis-stated (INFO).** The report says the sports flips are
  "mostly sub-cent ITF-tennis noise." Actually only 4/10 are ITF, and only 1/10 has a reverse net1 < 2c — the
  reverse arbs are mostly substantial (14.3/10.3/9.1/6.5c). It is the ORIGINAL net0 that is sub-cent. The
  phrasing understates the reverse magnitudes, i.e. it errs CONSERVATIVE (makes flips look less attractive);
  not a number error, but the prose should read "the original legs are sub-cent; the reverse arbs that follow
  are larger but their close-mark is unpriceable."
- **FLIP PnL — correctly reported as unmeasurable (Question 3 VERIFIED).** Exhaustive key enumeration: the
  sports `px` contains ONLY `{pm_b,pm_a,ka,kb}`; `ka`/`kb` are the two Kalshi team YES *asks*. Closing a
  Kalshi-backed leg needs the Kalshi YES *bid*, which is absent from every sports transition record. The
  weather-only `ladders-*.jsonl` stream does not cover sports. So `close_pnl_game`→None is correct, not a
  failure to read an available field. The "lower bound" framing (reverse leg only, close = unknown positive)
  is honest, with the selection-bias caveat above.
- **ADD practical significance — in domain units, small and conditional.** $135 paper-gross over 5 days ≈
  $27/day incremental, on a base whose own daily figures are themselves preliminary ×3-extrapolations. Net of
  the disclosed gross-ness (n=1 ceil fee, the add's own spread, the 17–55% naked-leg risk on 2 extra fills,
  latency) the realized number is lower. Per-market edges are modal ~2c net at ~200 contracts.
- **Multiple-testing / oracle exposure — NOT an oracle (Question 2c VERIFIED).** Swept `add_cap_frac`:
  inc_pnl is MONOTONE in the cap ($36@5% → $135@20% → $564@100%) — there is no in-sample peak being selected;
  20% is the pre-registered risk-control cap (0014: 20/10/5%), not a value fit to the test data. This is the
  opposite of the L19 oracle failure. tau_gain=1.0c is likewise not a tuned peak.
- **Concentration / effective-n (Question 2a VERIFIED — clean of the prior failure):** top-1 share 6.5%,
  top-5 20%, top-15 38%; 323 distinct (date, matchup) clusters across 348 markets. This is NOT the prior
  backtest's single-contract domination (L19: 93%-one-econ-contract). The correlated-settlement stacking the
  report warns about is largely theoretical here (most clusters = 1 market). BUT effective-n is still ~5
  independent event-dates and the deepest 17 adds (size capped at 200 = the max-clip channel) carry 36% — a
  cluster/day bootstrap CI would be the honest uncertainty statement and is absent.
- **The dominant lever (Question 2e + the biggest risk):** the held-to-settlement window. Add PnL
  $104(4h)→$135(28h)→$136(48h); flips 3→10→11. 301/408 add opportunities fire AFTER the base episode's edge
  CLOSED — they are separate later arbs, counted as "adds to a still-held pair" only because the model assumes
  the original pair is held to slug-date+28h. Defensible under the stated base case (and the self-review's
  CRITICAL-1 fix deliberately chose this window), but undisclosed and not swept.
- **What the result licenses you to conclude:** "FLIP is too rare (weather/econ structurally zero; sports
  ~1–3%) AND its profitability is unmeasurable from current logs → do not build FLIP yet; the prerequisite is
  logging the Kalshi YES bid on sports transitions." And "ADD shows a positive but small, hold-horizon- and
  phantom-sensitive paper-gross signal (~$99–$135 over 5 preliminary days) → not validated; not a build
  trigger on its own." It does NOT license quoting "$135" as a figure, nor "+5.36c/arb" as a flip benefit
  without the unknown-close and selection caveats.

## Impact on Project Scope

- **If the result stands:** the FLIP extension is correctly deprioritized; the actionable next step it implies
  is a *monitor* change (log the Kalshi YES bid on sports transitions) before any flip PnL can be measured —
  not more backtesting of the current logs. ADD stays an open, unvalidated hypothesis pending more data and a
  hold-window-honest re-run.
- **If the $135 is quoted as-is (the fragility):** any downstream prose or decision that treats "$135
  incremental from adding" as established would be premature — it is one hold-horizon assumption and one
  phantom-treatment choice away from $99 or lower, on 5 days. This is exactly the L14/L19/L28 prose-outruns-
  data hazard the project has been bitten by; the figure belongs in a backtest artifact with its range and
  caveats, not in CLAUDE.md/README as a magnitude.
- **Consistency with prior findings:** CONFIRMS the weather 1:1-basis-doesn't-cross property (prior 0/14
  station-days; here 0 transitions across 20,310 records). CONFIRMS L30's "locked pair is basis-bearing,
  flippable in principle" while showing the population is empirically ~absent outside sports. COMPLICATES the
  add story relative to the pre-registered allocation work (0014): the 20% cap is honored, but the add's
  realized value here is dominated by a hold-window assumption the allocation prereg did not pin.

## Recommended Alternatives (ranked by information value)

1. **Hold-window sweep on both headlines (highest value, ~zero cost).** Report flips and add-PnL across
   `settle_offset_h ∈ {4,8,12,24,48}h` (the W-sweep capital_sim already implements). Decision rule: if the
   add PnL or flip count changes by >~25% across the plausible hold range, quote the RANGE, never the 28h
   point. (Measured: it does — $104→$135. So the range is mandatory.) Also report the fraction of adds whose
   widen fires after the base edge closed (measured 301/408) so the reader knows most "adds" are later
   independent arbs under the hold assumption.
2. **Re-run the ADD headline with the phantom lens applied to the WIDEN leg (high value, low cost).** Apply
   `drop_flat` + an age/staleness check to the WIDEN record that drives each add, not just the base episode.
   Report the headline with and without. Decision rule: the quotable add figure is the one that survives the
   same phantom discipline the base cohort gets — disclose the delta (measured ~$36.62/27% sits on flat-ladder
   widens). This directly closes the L20/L28 exposure on the add path.
3. **Cluster/day bootstrap CI on the add PnL (medium value).** Resample by event-date (≈5 independent units)
   or by (date,matchup) cluster; report a 95% CI on the $ figure. With effective-n ~5 days the point estimate
   is fragile; a CI converts "$135" into an honest interval and will likely straddle a wide band.
4. **State the flip reverse-leg selection bias (low cost, correctness).** The reverse net1 values are a
   max-order-statistic over a noisy basis (you only observe crossings that grew past tau_gain); even the
   "lower bound" +5.36c/arb is optimistic. Add one sentence; it is the same caveat family as the project's
   seed-city / best-order-statistic lessons.
5. **(Out of scope for this backtest, but the real unlock for FLIP):** log the Kalshi YES bid on sports
   transitions in the monitor. Until then the FLIP PnL is structurally unmeasurable and no amount of
   re-backtesting the current logs changes that — the self-review states this correctly.

## Severity Flags

- **CRITICAL:** none. The conclusion is not invalidated; the two prior-failure channels (base-cohort phantoms,
  re-implemented economics/dir-trap) are genuinely avoided, and the qualitative FLIP verdict is robust.
- **WARN-1 (biggest risk): undisclosed hold-window lever.** `settle_offset_h=28h` hardcoded, no sweep; add PnL
  $104→$135 over 4h→28h; 301/408 adds fire after the base edge closed. The $135 must be quoted as a
  hold-conditional range, not a point. Fix: Recommendation 1.
- **WARN-2: flat-ladder WIDENs on the add path bypass the phantom chokepoint.** 27.1% ($36.62) of the add
  headline rides on `c2==c1==c0` widens; `capturable()` does not see the WIDEN record. Same shape as L28 (was
  57%). Partially defensible (mid-life, not book-init) but undisclosed. Fix: Recommendation 2.
- **WARN-3: no uncertainty interval on the add $ figure** despite effective-n ~5 event-dates and a top-17
  (max-clip channel) carrying 36%. Fix: Recommendation 3.
- **INFO-1:** the "mostly sub-cent ITF noise" flip caveat is mis-stated (4/10 ITF; reverse arbs mostly >2c;
  it's the original leg that's sub-cent) — errs conservative, but reword.
- **INFO-2:** reverse net1 is a selection-biased max-order-statistic; the lower-bound flip benefit is
  optimistic. Add the caveat (Recommendation 4).
- **INFO-3:** `add_events` sorts WIDEN tuples that contain a dict in the last position; it works only because
  `t` (first element) breaks all ties before the dict is reached. A latent fragility — if two widens ever
  share an identical `t`, the sort raises `TypeError: '<' not supported between instances of 'dict'`. Harmless
  today (timestamps are sub-ms-unique) but a one-line risk: key the sort explicitly on `w[0]`
  (`cand = sorted(..., key=lambda w: w[0])`). I hit this exact error reproducing the logic.

## Verification log (what I actually ran)

- `flip_add_backtest.py --selftest` and `bot/monitor.py --selftest` — both pass; the monitor selftest emits a
  weather FLIP on a constructed basis-cross.
- `flip_add_backtest.py` full run — reproduced FLIP 0/101, 10/604, 0/4 and ADD $135.31 exactly.
- Raw-record enumeration over transitions-*.jsonl (06-08..07-02): FLIP-by-category (weather 0, econ 0, sports
  105), dir/px shapes per category, exhaustive sports-px key set ({pm_b,pm_a,ka,kb} only).
- Decomposition of the $135: top-k shares, by-event-date, by-(date,matchup) cluster; flat-ladder WIDEN share
  ($36.62/27.1%); size_add distribution; `add_cap_frac` and `tau_gain` and `settle_offset_h` sweeps;
  post-base-close widen fraction (301/408).
- Binomial Wald + Wilson CI on 10/604; rule-of-three on 0/101.
- Confirmed both CRITICAL fixes present in committed source (lines 141/166/203) and the commit message honest.
