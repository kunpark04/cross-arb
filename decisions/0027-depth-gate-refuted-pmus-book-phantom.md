# 0027 — depth-gate scale lever REFUTED; the displayed cross-venue book is phantom at retail size

- **Date:** 2026-06-16
- **Status:** Accepted (do NOT scale on a depth gate / breadth / bigger clips; bot stays a 1-contract rig; one instrumentation gap pre-registered before the FINAL "uncapturable" verdict)
- **Deciders:** owner ("4 [locks] is crazy for the whole day — barely no edge or scale"); LLM council (5-advisor, 4-of-5 converged on instrument-first); Claude (built `scripts/depth_at_fire.py`); stats-ml-logic-reviewer (independently reproduced; SOUND, 0 CRITICAL)

## Context

A full 25.3 h live run (1-contract cap, breadth armed) **locked only 21 of 178 fires (11.8%)**; ~85% died
because the pmus hedge leg would not fill. Detection was NOT the problem (178 positive-edge fires, ~7/hr, avg
3.7¢ net, past the 1.5¢ floor) — **completion** was. With the two obvious scale levers already closed (bigger
clips fill worse; maker-rest NOT_VIABLE per [0026]), the owner's "barely no scale" forced the question: is the
edge *capturable at scale at all*? An LLM council (`tasks/council-transcript-2026-06-16-pmus-fill-wall.md`)
framed two worlds and 4-of-5 advisors converged on a falsifiable test over data already in hand:

- **World 1 (real-but-thin book):** within a category, displayed depth-at-fire predicts the fill → a gate
  ("fire only when depth_c2 ≥ X") raises conversion → the scale unlock (one conditional), THEN breadth multiplies.
- **World 2 (phantom/adversarial book):** displayed depth does NOT predict the fill → the surviving ~12% are
  adversely selected → breadth/cap-raise would scale a negatively-selected book → research, not a business.

`scripts/depth_at_fire.py` reads which world we are in off the bot's own `executions.jsonl`: every fire logs a
`book` entry snapshot with `depth_c2` (cross-venue fillable PAIRS at ≥2¢ gross edge, `book.rs::depth_curve`, a
two-pointer merge **capped by the thinner leg → already pmus-constrained**), stamped **synchronously at fire,
before the submit** (`live.rs:477`) → pre-fire book, **no look-ahead** (contrast the [0026] clock-skew trap, [L44]).

## Decision

**WORLD 2. Do NOT scale.** The displayed cross-venue book is phantom at retail size; depth-at-fire does not
predict the fill, and conversion if anything *falls* as displayed depth rises. The bot stays a **1-contract
measurement rig**; breadth and cap-raise are NOT scale levers on this venue pair.

Measured (`scripts/_data` via `depth_at_fire.py`, all 178 fires carry a snapshot):

| stratum | fires | locks | conv | AUC(lock depth > miss depth) | read |
|---|---|---|---|---|---|
| sports | 128 | 14 | 11% | **0.420** | no separation |
| weather | 44 | 7 | 16% | 0.566 | no separation |
| worldcup | 6 | 0 | 0% | n/a | never locked |
| pooled | 178 | 21 | 11.8% | 0.445 | (Simpson-exposed) |
| literal "did pmus fill?" recoding | 178 | — | — | **0.392** | inverse — holds stronger |

Stratified label-shuffle permutation (labels shuffled WITHIN category): depth_c2 **p=0.64**, pmus touch-spread
**p=0.57** — neither separates. Best depth gate lifts conversion **+0.9 / +1.0 / +3.3 pts** (pooled/sports/
weather) — nowhere near the 12%→60-80% World-1 needs. The independent review's two reinforcements are
load-bearing: (1) conversion is **monotone DECREASING** in displayed depth (11.9% → 7.7% @≥100 → 0% @≥5000) and
MLB — the deepest displayed book (median 317) — converts *worst*, so World-1 is **affirmatively refuted**, not
merely unsupported; (2) a Monte-Carlo power check gives **89-100% power** to detect a business-relevant gate
(AUC≈0.65), so "p=0.64" is **evidence of absence, not under-power** — the "inconclusive, need more data" reading
does NOT apply at the paired-depth level. Full audit: `tasks/_agent_bus/20260616-0438/stats-ml-logic-reviewer.md`.

**The locks that DO complete are profitable** (the 5 with the metric: net edge sum 12.5¢, mean 2.49¢, all
positive) — so this is a **capacity** problem, not a losing strategy. There are structurally too few locks and
they are not foreseeable from the displayed book.

**One instrumentation gap pre-registered before the FINAL "fundamentally uncapturable" verdict** (review Rec 1):
`depth_c2` is the PAIRED (min-of-both-legs) depth; **no pmus-side-only depth ladder is logged**, so a
pmus-specific fill predictor the paired metric could mask is untested. Add a pmus bid/ask SIZE ladder to the
entry `book_snapshot` (logging-only, no behavior change); if a pmus-only signal ALSO fails to separate on the
next run, World 2 is fully nailed and the cross-venue taker arb is **real but uncapturable at scale on this
venue pair** — a finding, not a business.

## Alternatives considered

- **World 1 — add a `depth_c2 ≥ X` fire gate, then scale breadth.** REFUTED by the data (every per-category
  AUC ≤ 0.57; gate lift ≤ 3.3 pts; conversion monotone-decreasing in depth). A high-depth gate would select
  TOWARD the toxic, deep-but-phantom sports fires.
- **Push breadth NOW (the Expansionist's call; more pairs/cities/leagues/WC outcomes).** REJECTED — all 5
  reviewers flagged it as the biggest blind spot: at 12% phantom-gated conversion, breadth multiplies
  adversely-selected lottery tickets, not edge. Breadth is the multiplier AFTER a fill problem is solved, and it
  isn't. (Breadth still has independent value for the *measurement* dataset and the pending settlement recon.)
- **Raise the contract cap to test $-scale.** REJECTED — bigger clips fill even less against a thinner-than-
  displayed pmus book ([0025]); it risks the clean 0-loss record to learn nothing new.
- **Declare it research/dead now (council option e), skip the pmus-ladder step.** DEFERRED — the paired-depth
  result is World 2, but honesty requires testing the one pmus-specific signal not yet logged before the final
  "fundamentally uncapturable" claim. Cheap, logging-only.

## Consequences

- **Scaling is reframed from "which lever" to "the venue pair caps capacity."** The route to more $ is NOT clip
  size, NOT a depth gate, NOT breadth-at-12%; it is (a) the pending **settlement recon** (sports ~6/23, econ
  ~7/2) unlocking cleaner categories, (b) the rare **transient deep-AND-real-book** windows (MLB line-lag, n=1 —
  instrument, don't bet), and (c) accepting this may top out as a research rig. [0025]'s "breadth not clips" is
  narrowed: even breadth doesn't help while fills are phantom.
- **Instrumentation BUILT this session** (logging-only, no order-path change, build + 187 tests + clippy green):
  `book::pmus_hedge_depth(pm, dir) -> HedgeDepth{touch, within_2c}` computes the pmus HEDGE-side single-leg
  resting qty (dir-specific: PK→pmus YES-ask, KP→pmus NO-ask), and `exec_log::book_snapshot` now logs it as
  `pm_depth0` (touch) + `pm_depth2` (within 2¢) at every entry fire (`live.rs:477`, recomputed from the
  unchanged-this-iteration pmus book). `scripts/depth_at_fire.py` reads the new fields and runs a pmus-only
  per-category AUC (graceful "not yet instrumented" on pre-2026-06-16 logs). **Captures on the next live run**
  (owner-gated, [0006]); until then the verdict is held at "World 2 on paired depth, pmus-specific signal awaiting
  data." If the pmus-only depth ALSO fails to separate next run, the "uncapturable at scale" verdict is final.
- **Does not change safety or current operation:** the bot remains 1-contract, dynamic-fire-order, 0-loss; the
  21 open/closed locks are clean hedged positions. No live behavior changed by this decision.
- Artifacts: `scripts/depth_at_fire.py` (+ `--selftest`), the council transcript, and the stats audit are the
  record. Supersedes nothing; complements [0025] (thin-leg/fire-order) and [0026] (maker NOT_VIABLE) as the
  third leg of the "pmus is the binding constraint" finding — now measured at the fill level.
