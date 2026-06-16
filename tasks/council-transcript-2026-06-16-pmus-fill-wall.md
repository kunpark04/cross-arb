# LLM Council transcript — 2026-06-16 — "pmus fill wall: scale, instrument, or shelve?"

## Original question
After a full 25.3h live run the bot locked only 21 hedged positions at a 1-contract cap
(~$0.78 gross). Owner: "4 [held positions] is crazy for the whole day. Barely no edge or scale."
Is there an issue, and what's the highest-value next move?

## Framed question (given to all 5 advisors)
Cross-venue arb bot, Kalshi × polymarket.us (pmus), real money, safe-by-default. 25.3h run, 178
positive-edge fires (~7/hr, avg 3.7¢ net, past a 1.5¢ floor) → only **21 locks (11.8% conversion)**;
~85% of fires died because the **pmus hedge leg would not fill**. Maker-mode workaround already
measured NOT_VIABLE; bigger clips fill even worse; bot already fires the thinner (pmus) leg first so
misses abort before touching Kalshi (0 naked exposure, 0 realized losses this run). Separate
$250/$250 wallets. Settlement recon unlocks more categories (sports ~6/23, econ ~7/2). Fork:
(a) accept measurement rig / wait for recon; (b) push breadth; (c) raise the cap; (d) smarter
depth-gating; (e) conclude real-but-uncapturable, research not business.

## Advisor responses (de-anonymized)

**The Contrarian (was Response B):** 88% hedge-rejection isn't an execution defect — it's adverse
selection. The 12% that fill are disproportionately where pmus was slow to update (pennies in front
of someone's information). "0 losses over 21 fills" is too small a sample to have met the one fill
that grades against you. Breadth = more 12% lottery tickets across thinner books; raising the cap is
worst (fills even less, grows the unseen adverse fill). Depth-gating is the only honest lever, but if
firing only on real resting depth keeps conversion ~12%, the displayed depth is fake and the business
is dead. Run (d) as a 2-week kill-test with a pre-registered ~50% conversion threshold; below it,
conclude (e) and stop feeding it engineering time.

**The First Principles Thinker (was Response D):** pmus isn't a thin leg to defeat — it's the market
telling you the gap isn't real. 85% non-fill at the best displayed price IS the verdict; the depth is
a quote, not a commitment, and it vanishes when an informed taker (you) arrives. A "clean arb" that
fills 12% of the time was never 88%-clean. Real question: is 3.7¢ even positive after the misses cost
you? $0.78 gross/day is the CEILING (cap=1, no slippage, no losses), not the floor. Breadth and bigger
caps are strictly worse. This is a measurement rig that has returned its measurement — a real,
publishable (e). Before accepting, run (d) inverted: log whether fills correlate with pmus quote AGE
and SIZE; if deep-and-stale quotes also refuse you, adverse selection, game over. Two days decides.

**The Expansionist (was Response A):** Push BREADTH. The detection layer is the asset — 178 fires/25h
is a working alpha radar that scales with the UNIVERSE you point it at, not capital. Recon unlocks
full sports 6/23, econ 7/2; World Cup alone is dozens of co-listed outcomes — each new city/league/
series a free draw at ZERO marginal capital (two $250 wallets barely touched). The 12% conversion
isn't failure, it's a FILTER you didn't build on purpose: the pmus wall auto-selects the 21 arbs where
pmus had real depth (already doing depth-gating for free). Real metric = locks-per-DAY, scales
linearly with breadth. The validated mispricing feed also has a second life as a SIGNAL/dataset.
Explode breadth now, log every fire's pmus resting-depth; cap stays 1 until locks/day saturates.

**The Outsider (was Response E):** Question the "friction to engineer around" frame. 88% miss means
the quote was never real liquidity, or something faster pulls it exactly when your signal says "edge"
— textbook adverse selection (you fill when you shouldn't, miss when you'd profit). Maker mode died of
the SAME disease: two levers, one root cause. Distrust breadth and bigger cap. Unexamined assumption:
is the 3.7¢ edge even real, or an artifact of your stale view of pmus vs a quote already gone?
Detection says edge; 88% non-fill says edge and fillability are ANTICORRELATED — the whole ballgame.
Do (d) reframed: gate on whether quotes you fire at actually fill, then measure realized edge ON FILLS
ONLY. If fills cluster on the worst 12%, research not business (e).

**The Executor (was Response C):** You've proven you can't tunnel through the pmus wall (maker dead,
bigger clips dead). Monday: stop guessing WHY it won't fill, measure WHICH fires would have filled.
You have 178 fires with outcomes. Run (d) as pure INSTRUMENTATION first: log pmus resting depth at the
hedge price at the instant of fire, bucket the 21 locks vs ~157 misses by that depth. Two answers by
Tuesday: (1) a clean depth threshold separates fills from misses → add one gate ("don't fire unless
pmus shows ≥X resting at hedge"), conversion jumps toward 60–80%, every surviving fire is real — the
scale unlock for one conditional; (2) no threshold, identical depth → the book is a mirage, it's a
measurement rig, write it up. Skip breadth and cap-raise. First step: write the depth-at-fire logger,
replay against today's 178 fires if book snapshots are saved, else run live one more session.

## Peer reviews (5, anonymized A–E at review time)
- **Unanimous (5/5): Response C strongest** — the only one that converts the fork into a falsifiable,
  dated experiment using data already in hand (replay the 178 fires), with two pre-committed branches
  mapped to actions. B/D/E reach the same adverse-selection diagnosis but stop short of C's
  instrumentation; C makes (d) the *measurement that adjudicates between (b) and (e)*.
- **Unanimous (5/5): Response A biggest blind spot** — treats 12% conversion as a free depth-filter and
  assumes locks scale linearly with breadth, ignoring that the surviving 12% may be the *adversely-
  selected* fills. "Triple universe, triple locks" assumes fill-rate is breadth-independent; thinner
  new books likely convert worse. Scaling would multiply a negatively-selected book.
- **What ALL FIVE missed (convergent):**
  1. **Realized-PnL accounting on the 21 actual locks** — nobody computed net edge after *both venues'
     fees + slippage*; the 3.7¢ gross may already be underwater.
  2. **The 21 locks are UNSETTLED** — "0 losses" is statistically empty pre-grade; the adverse-selection
     / settlement-tail risk only resolves when positions grade (ties to the 6/23 sports, 7/2 econ recon).
  3. **Diagnosis ambiguity inside "adverse selection":** is pmus racing a *faster taker* (latency —
     possibly beatable) or posting *fake liquidity* (structural — not)? Different fixes.
  4. **One-day-dominance** — 25.3h is a single sample; a known hazard in this project.
  5. **Leg-sequencing** as an untested lever (confirm-hedge-first).

## Chairman synthesis
See the inline verdict delivered to the owner (Recommendation: run depth-at-fire instrumentation as
the adjudicating experiment before any breadth/cap move; pair with net-edge-after-fees on the 21 locks
and let the sports locks reach 6/23 recon). One Thing: write the depth-at-fire replay over the 178
fires and read whether a separating pmus-resting-depth threshold exists.

## RESULT — the experiment ran (2026-06-16, `scripts/depth_at_fire.py`)
**WORLD 2 — the displayed cross-venue book is PHANTOM.** Depth-at-fire does NOT predict the fill:
- Per-category AUC(lock depth > miss depth): sports **0.420** (14 locks/128), weather **0.566** (7/44),
  worldcup 0 locks/6. Pooled 0.445; literal "did pmus fill?" recoding **0.392** (holds stronger).
- Stratified label-shuffle permutation (within category): depth_c2 **p=0.64**, pmus touch-spread **p=0.57**
  — neither separates. Best depth-gate conversion lift +0.9 / +1.0 / +3.3 pts (pooled/sports/weather) — negligible.
- Conversion is **monotone-DECREASING** in displayed depth (11.9% → 7.7% @≥100 → 0% @≥5000); MLB (deepest
  displayed, median 317) converts WORST. World-1 is affirmatively **refuted**, not merely unsupported.
- Monte-Carlo power: **89-100%** vs a business-relevant gate (AUC≈0.65) ⇒ "p=0.64" is **evidence of absence,
  not under-power**. The "inconclusive, need more data" reading does NOT apply at the paired-depth level.
- The locks that DO complete are profitable (5 with the metric: net +2.49¢ mean, all positive) → a **capacity**
  problem, not a losing strategy.

The Expansionist's dissent (breadth as the move) is thereby **rejected by the data** — at 12% phantom-gated
conversion, breadth multiplies adversely-selected tickets. Decision recorded as
[0027](../decisions/0027-depth-gate-refuted-pmus-book-phantom.md). Independent audit (the panel's "verify before
acting" discipline): `stats-ml-logic-reviewer` reproduced it bit-for-bit → **SOUND, 0 CRITICAL**
(`_agent_bus/20260616-0438/stats-ml-logic-reviewer.md`). One gap pre-registered before the FINAL "uncapturable"
verdict: instrument the **pmus-side depth ladder** (`depth_c2` is paired-min; a pmus-only signal is untested).
