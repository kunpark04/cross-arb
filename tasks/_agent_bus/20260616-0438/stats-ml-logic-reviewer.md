---
from: stats-ml-logic-reviewer
run_id: 20260616-0438
timestamp: 2026-06-16T04:38:00Z
scope_reviewed: scripts/depth_at_fire.py World-1/World-2 adjudication (does displayed cross-venue depth_c2 at fire predict the pmus hedge fill?) over bot-rs/executions.jsonl (178 fires)
verdict: sound
critical_flags: 0
warn_flags: 2
info_flags: 4
cross_references: [tasks/_agent_bus/20260615-2207/stats-ml-logic-reviewer.md]
---

## Verdict

**SOUND — the World-2 conclusion is SUPPORTED, and it survives every robustness attack I could mount.**
I tried to break it five ways (correct the outcome label to the literal "did the pmus leg fill" question,
strip the within-sports league confound, scan the full depth tail for a high-threshold business case, run
a Monte-Carlo power analysis, and re-price with the categorization bug fixed) and the verdict held or got
*stronger* every time. The single most important finding: **conversion does not merely fail to rise with
displayed depth — it monotonically FALLS** (11.9% baseline -> 7.7% at depth_c2>=100 -> 6.2% at >=1000 ->
0% at >=5000). That is the [0025] inversion confirmed across the entire range, not a small-cell artifact.
Combined with a power analysis showing the test would detect a business-relevant depth gate (pooled
AUC~=0.65) **89% of the time**, "p=0.61, not significant" is correctly read as *evidence of absence*, not
*absence of evidence*. The displayed book is a mirage; a depth gate is not the scale unlock. The two WARN
items (an imprecise outcome label and an un-instrumented pmus-only depth) qualify the *framing* and bound
*future* work — neither flips the decision. The look-ahead trap that sank the sibling p_hedge analysis
(20260615-2207, clock-skew join) is **NOT present here**: depth_c2 is logged synchronously on one clock
before the submit, and I verified the event ordering in the raw log.

## Methodological Audit

- **Choice of instrument (AUC / Mann-Whitney + stratified label-shuffle permutation): correct and well-matched.**
  Rank-based AUC is the right separation metric for a small-n, heavy-tailed predictor (depth_c2 spans
  0..28410); it is distribution-free and tie-corrected (verified the tie handling in `auc()`,
  depth_at_fire.py:131-150, against the selftest). Stratifying the permutation *within category* is exactly
  the right defense against the documented depth<->category confound ([0025]); a pooled test would be
  Simpson-prone and the script says so and quarantines the pooled number to "exposure only" (line 251-260).
- **Assumptions checked and met.** (1) Append-only log == time order: VERIFIED — all 797 records are
  monotonic non-decreasing in ts_ms. (2) Pre-fire snapshot (no look-ahead): VERIFIED in code AND data —
  `exec_log::book_snapshot("entry",...)` at live.rs:477 is called synchronously BEFORE `spawn_submit`
  (live.rs:489); traced 9 fires of `aec-wta-talgib-frajon` and every `book` precedes its `submit` precedes
  its `fire_outcome`. (3) Independence across fires for the permutation: adequate — labels shuffled within
  stratum; repeated fires of one market are distinct opportunities (the same market re-fires minutes apart
  against a refreshed book). One mild dependence (same market re-fired) is not corrected, but it cannot
  manufacture the *observed* near-0.5/-below-0.5 AUC.
- **Alternatives not considered (all immaterial to the verdict, but worth noting).** No per-venue depth
  ladder was logged, so a pmus-only depth proxy could not be built (see WARN-2). The script uses the
  permutation two-sided; I re-ran one-sided toward World-1 under the corrected label and still got p=0.65.

## Data Audit

- **Descriptive reality (independently recomputed, matches the script exactly).** 178 fires, all 178 carry
  a pre-fire depth_c2 (0 censored). Outcomes: abort_clean=128, recover=24, lock=21, abort_ambiguous=3,
  naked_halt=2. Conversion 21/178=11.8%. Pooled AUC=0.445. Per-cat: sports 122/13 AUC=0.370, weather 44/7
  AUC=0.566, worldcup 6/0, other 6/1 AUC=1.000. Stratified perm p(depth)=0.6088, p(spread)=0.5754. All
  reproduced bit-for-bit.
- **Grouping state machine is correct (Challenge 6).** Replayed `group_fires` independently: 179 entry-books
  vs 178 fire_outcomes reconciles to exactly 1 orphan book at EOF (`tc-temp-laxhigh-...gte75f`, run ended
  before its outcome). **0 overwrites** (no case where a second entry-book replaced an un-closed pending book
  for the same market) -> no cross-fire depth mis-association, even on the 9x-fired WTA market. Every one of
  the 178 grouped fires has a real, correctly-attached pre-fire book.
- **Leakage / look-ahead: NONE found (Challenge 2).** This is the headline given the sibling's clock-skew
  bug. depth_c2 is the displayed book the bot fired against, stamped before the order left. Single clock.
  Verified in both code path and raw event ordering.
- **Representativeness / selection (Challenge 7).** The depth distribution is conditional on passing the
  upstream edge/floor gate (only fires that cleared `realized_edge_clears_floor` reach the log). This
  conditioning is *correct*, not a bias: the council's question is explicitly "given we fired, does
  displayed depth predict the fill." Within the fired set there is no missing-outcome bias (178/178 have a
  book).

## Result Interpretation

- **Statistical significance.** No separation. Corrected-label stratified stat = -0.021, two-sided p=0.709,
  one-sided-toward-World-1 p=0.647. The point estimate is on the *wrong side* of 0.5 (aborts run slightly
  deeper).
- **Practical significance (the decision metric).** A depth gate REDUCES conversion monotonically: at
  depth_c2>=100 conversion is 7.7%, at >=1000 it is 6.2% (1/16 locks), at >=5000 it is 0/3. The deepest
  displayed books are the *worst* converters. World-1's central claim ("fire only when depth>=X raises
  conversion") is affirmatively REFUTED, not merely unsupported.
- **Power (Challenge 4 — the crux of the over-claim question).** Monte-Carlo holding the observed depth
  distribution and per-category lock counts fixed, injecting a true depth->lock effect: null effect ->3%
  false-positive (calibrated); weak effect (pooled AUC~=0.65) ->**89% power**; moderate (AUC~=0.78) ->100%.
  The design is NOT uninformative. A depth gate strong enough to be the "scale unlock" would have been
  detected with high probability. So "p=0.61" is correctly read as evidence of absence. (Honest caveat: power
  is lower against an effect confined to the extreme tail >5000 where n is tiny — but the tail scan shows
  that region converts at 0%, so there is no hidden tail signal to miss.)
- **What it licenses.** You MAY conclude: displayed depth_c2 does not predict pmus-hedge fill within the
  fired population; a depth gate is not a conversion lever; scaling on displayed depth would scale an
  adversely-selected book (World 2). You may NOT conclude: that the *true* pmus-side depth (un-instrumented)
  is also uninformative, nor that pmus fills are *random* (they may depend on a signal not in this log).

## Impact on Project Scope

- **If the result stands (it does):** the "depth gate -> scale unlock" path (World 1) is closed. Scaling
  reframes to breadth + transient-depth-window instrumentation, exactly as [0025] already concluded — this
  analysis CONFIRMS and hardens 0025 rather than opening a new lever. Do not raise caps or add a depth gate.
- **If fragile (it is not, on this data):** the only premature downstream decision would be declaring pmus
  fills *fundamentally* random and abandoning fill prediction entirely. Don't — the un-logged pmus-side
  depth (WARN-2) is the obvious next predictor to test before that conclusion.
- **Consistency with prior findings:** CONFIRMS [0025] ("displayed sports depth evaporates at fire";
  aborted fires logged higher depth than locked) with a quantified rank statistic and a power floor.
  COMPLEMENTS the sibling p_hedge review (20260615-2207): both land on "the pmus book is structurally
  hostile to the hedge," reached by independent instruments.

## Recommended Alternatives (ranked by information value)

1. **Log the pmus-side depth ladder (and Kalshi-side) separately at fire, not just the min-paired depth_c2.**
   Answers: is there a *pmus-specific* fill signal the min-pairing masks? Why better: depth_c2 = min(pmus,
   kalshi) by construction (book.rs:233), so when pmus binds it already ~= pmus depth, but you cannot verify
   that or isolate a pmus-only effect from the current log (entry-book carries only depth_c2 + 4 touch
   prices). Decision rule: if pmus-only depth AUC > 0.60 with perm p<0.05 in the next run, World-1 reopens on
   the *correct* proxy; else World-2 is settled. This is a one-field instrumentation add, highest value/effort.
2. **Relabel the outcome to "did the pmus leg fill" (submit-derived), not "did it lock," and report both.**
   Answers the council's literal question without conflating a pmus fill that lost the Kalshi race with a
   pmus miss. Why better: 13 of the 157 "miss"-coded fires actually had the pmus leg fill (11 recover/Pmus +
   2 naked_halt/Pmus). I already recomputed it: pooled AUC 0.426, sports 0.406, weather 0.554 — verdict
   unchanged. Cheap; tightens the writeup's precision. Decision rule: none changes; this is a correctness/
   clarity fix, not a re-adjudication.
3. **Fold UFC + Valorant into the sports stratum (categorize bug).** `aec-ufc-` (4) and `aec-valorant-` (2)
   fall through to `other`, producing the spurious `other` AUC=1.000 (n=1 lock). Fixing it moves sports
   0.370->0.420 and deletes the only World-1-looking cell. Why better: removes a misleadingly pro-World-1
   number. Decision rule: none changes.
4. **(Optional) Cluster the permutation by market to absorb repeated-fire dependence.** Low value — the
   observed AUC is already <=0.5, so any dependence correction can only widen the null, never create a
   missed signal. Skip unless a future run shows a borderline-positive AUC.

## Severity Flags

- CRITICAL: none. No issue found that would flip or invalidate World 2. I looked specifically for a
  clock-skew/look-ahead join (the sibling's failure mode), a grouping mis-association, a within-sports
  Simpson rescue, a high-depth tail business case, and an under-power "inconclusive" reading — none survived
  scrutiny.
- WARN-1 (qualifies framing, does not flip): **outcome label imprecision.** {recover, naked_halt} are coded
  as hedge-miss=0, but 13 of them had the pmus leg FILL (the Kalshi leg missed). The council's question is
  literally "does the pmus hedge fill," and under the corrected label the verdict is unchanged (pooled AUC
  0.426). Fix the label for precision; the conclusion is robust to it. (exec_log.rs:169 + bookkeeping.rs:247
  confirm `detail`=the FILLED leg's venue.)
- WARN-2 (bounds future work): **no pmus-only depth was instrumented.** Only the min-paired depth_c2 is
  logged, so a pmus-specific fillability signal cannot be tested from this data. This is the one honest gap
  between "depth_c2 doesn't separate" and "the pmus book is unpredictable." Recommendation 1 closes it.
- INFO-1: categorize() mis-buckets `aec-ufc-` and `aec-valorant-` into `other` (Rec 3) — immaterial, and the
  fix removes a pro-World-1 artifact.
- INFO-2: the `other` per-category AUC=1.000 is a single lock (n=1, depth=3845); the script already
  discounts it verbally, and Rec 3 deletes it.
- INFO-3: worldcup (6 fires, 0 locks) contributes no AUC and is correctly reported as n/a.
- INFO-4: net edge on locks reproduces exactly (5 of 21 carry edge_net_c, sum=12.5c, mean=2.49c, min 1.55,
  max 4.61, all positive). This is a *profitability-of-the-survivors* anchor, not part of the World-1/2
  separation test, and n=5 is far too small to claim the lock cohort is reliably +EV — treat as diagnostic.

VERDICT (one line): **World-2 conclusion is SUPPORTED**, because displayed depth_c2 does not separate
locks from misses within category (corrected-label perm p=0.65, AUC<=0.5), conversion monotonically
*falls* with depth (11.9%->6.2% at >=1000), the test has ~89% power against a business-relevant gate so
the null is evidence-of-absence not under-power, and the snapshot is verified pre-fire single-clock with a
correct grouping state machine (0 mis-associations, 0 censored).
