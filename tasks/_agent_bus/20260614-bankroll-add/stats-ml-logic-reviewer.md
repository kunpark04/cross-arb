---
from: stats-ml-logic-reviewer
run_id: 20260614-bankroll-add
timestamp: 2026-06-14T09:05:00Z
scope_reviewed: scripts/bankroll_add_backtest.py (committed 3d38e7f) — bankroll-netted marginal value of scale-in/re-entry adds under the $250/$250 separated two-pool model; verified by recomputation + bit-identity check + bankroll/offset sweeps.
verdict: partially sound
critical_flags: 0
warn_flags: 3
info_flags: 2
cross_references: [tasks/_agent_bus/20260614-bankroll-add/coding-agent.md, tasks/_agent_bus/20260614-bankroll-add/code-logic-reviewer.md, scripts/venue_split_backtest.py, scripts/flip_add_backtest.py, scripts/capital_sim.py, tasks/lessons.md]
---

## Verdict

The **mechanics are sound and the headline numbers reproduce exactly** — I re-ran the backtest and recomputed
the all-verified case position-by-position. The four flagged bug-classes from prior backtests are all clean:
the base-only walk is **bit-identical** to `venue_split.two_pool_walk` (0 mismatches on per-position
profit/settle/capital across both cohorts), the leg→pool split routes real two-letter sports dirs
asymmetrically (no [L28] trap), causal ordering holds (0 add-before-base violations), the phantom lens is
genuinely active (8 raw adds → 4 lensed on the cohort), and there is no double-count (base+add are separate
held positions). The coding agent's self-caught CRITICAL is **correct**: the realized-only +$8.22 is a pure
window-boundary reclassification (the funded add settles 06-14 04:00 in-window and displaces 3 base pairs
that settle 06-15 out-of-window; dTOTAL = add 8.22 − displaced 8.56 = **−0.34**, reconciles exactly), so
dTOTAL is the right marginal quantity and the mark is path-independent.

**But the headline conclusion as written — "bankroll-netted, adds ≈ $0 because the base saturates both $250
pools; capital, not opportunity, is the binding constraint" — is QUOTABLE only with two qualifications the
writeup omits, and is NOT unconditional.** (1) On all-verified, dTOTAL(both) is **not stable** to the
known-uncertain settle-offset proxy: it swings from **+$9.24 (12h) → −$0.34 (28h headline) → $0.00 (48h)**;
the headline sits at the one offset where crowd-out cancels the add edge, and 80% of that cohort is sports
whose *realistic* settle is game-end (hours → the 12h row → dTOTAL ≈ **+$9**, near the unconstrained UB), not
28h. (2) The mechanism "capital is binding" is **literally true** and I verified it (relaxing the bankroll to
$2k lifts dTOTAL monotonically to the +$10.88 UB) — which means all-verified is an "**at $250 the base spends
the capital first**" result, NOT an "adds have no edge" result; those are economically opposite and the
distinction is load-bearing for the build decision. The genuinely robust, bankroll/offset-invariant finding
is different and stronger: **the owner's actual feature (TRUE scale-in) barely exists in this data — 1 true
add in 5 days worth ~$0.13 even at infinite bankroll — and both "funded adds" that drive every headline
number are RE-ENTRIES that the live one-position-per-slug guard (`bot-rs main.rs:497`) already blocks.** Quote
*that*, not the proxy-fragile −$0.34.

## Methodological Audit

- **Choice of model (one causal two-pool walk, base+adds sharing $250/$250):** correct and well-motivated.
  An add deploys a YES+NO leg across both venues exactly like a base arb, so netting it against residual pool
  room (rather than flip_add's free-capital UB) is the decision-relevant construction. Reuses the proven
  harness verbatim (`leg_split`, `settle_t`, `void_haircut`, `DEPTH_BOUNDARY_NET`, trapezoid book-avg edge,
  fat-edge haircut) per [L28] discipline — no re-derived "clean" cohort.

- **dTOTAL vs dReal (the central methodology call): SOUND.** dReal (realized-only) is the wrong quantity
  because a high-edge add that settles *inside* the 5-day window reshuffles which positions land inside the
  window — exactly the [L18] "favorable-number-is-the-wrong-quantity" shape. I reconstructed it: the +$8.22
  is add-realized-in-window (8.2212) minus crowded-out-realized-in-window (0.0000) = +8.2212; dTOTAL is add
  (8.2212) minus the 3 displaced base pairs' *total* profit (8.5575) = **−0.3363**. The displaced base edge
  (8.56) and the add edge (8.22) are near-equal in magnitude — the add buys almost nothing, it just relabels
  which day's edge counts as "realized." Leading with dTOTAL is the right correction.

- **Mark consistency (the subtle risk in Q1): VERIFIED clean.** A position's profit is `size*edge_per` set
  ONCE at entry and never re-marked by scenario — 15 base markets present in both A and C have identical
  profit (0 re-marks). The add's `edge_per` uses the identical trapezoid+void formula as base (independently
  recomputed 0.04420000 = code 0.04420000); it differs from base only in using `net_add` vs `open_net`, which
  is correct. No mark-to-edge asymmetry manufactures or hides the delta. Total reconciles to the cent:
  add 8.2212 + baseC 32.6075 − baseA 41.1651 = −0.3364 = dTOTAL.

- **Assumptions that HOLD:** separated-wallet two-pool model; capital frees at each position's own
  settlement (causal); base sizing = `min(open_c2, kcash//k_unit, pcash//p_unit)` matches venue_split; fat-edge
  ×0.5 haircut above 6c knee applied consistently to base AND add (the 7842→3921 halving on the deepest book
  is this shared haircut, present identically in venue_split — not a bug).

- **Assumptions that are UNTESTED / proxy-dependent:** (a) settlement timing is a slug-date+28h proxy; the
  realized/unrealized split — and therefore how much crowd-out vs recycling occurs — inherits its
  uncertainty. The writeup calls dTOTAL "window-invariant," which is true in the sense that it counts realized
  AND unrealized, but dTOTAL is NOT *offset-invariant* (see Result Interpretation). (b) None-px adds would
  fall back to 50/50 leg-split, but all 4 cohort adds carry real px, so this didn't bite here.

- **Alternatives not considered:** the upstream `flip_add_backtest` §3a runs a hold-window SWEEP precisely
  because the add count is hold-conditional and explicitly says "quote the RANGE across plausible holds, not
  one value." `bankroll_add` collapses to the single 28h point and does not sweep — the one place its
  methodology is *weaker* than the harness it builds on.

## Data Audit

- **Descriptive reality (recomputed, not taken on faith):** span 5.08 d (06-09..06-14), 99,198 transitions,
  36 econ off-by-one records quarantined ([L21], confirmed firing at runtime). All-verified cohort = 33
  markets (25 sports / 8 weather), 18 base positions entered, effective event-days 5. Weather-only cohort = 8.

- **Effective-n is TINY and CONCENTRATED — disclosed but under-emphasized for all-verified.** At $250 the base
  is not a diversified book: the largest single position (`mlb-sea-bal`, 310 ctr) is **$303.77 = 61% of $500
  capital**, pool-bound; the top 3 pool-bound clips (mlb 310, valorant 253, joagui 185) drive most of the
  saturation. This is the same concentration `venue_split_backtest` flags ("the largest is ~X% of capital —
  concentration, not a portfolio"); `bankroll_add` inherits it silently. The pool "saturation" is therefore
  a *mix* of genuine breadth and a few large deep-book clips, not pure breadth.

- **Sports-settlement exposure: 80% of all-verified base profit is sports** ($32.85 sports vs $8.32 weather),
  which the project itself marks UNVERIFIED until recon ~June 23. The all-verified number is a post-recon
  *preview*, correctly labeled — but the dTOTAL-offset fragility is *driven* by sports (game-end settle ≪ 28h).

- **Leakage / contamination:** none found. Phantom lens ([L20] flat-ladder drop) active on both base
  (`_live_cohort` → `open_flat` drop) and adds (`drop_flat_widen=True`, drops 4 flat widens incl. an MLB and a
  UFC on the real run). Econ off-by-one quarantined. Causal ordering enforced (0 violations). No combo-ID or
  look-ahead leakage applicable to this construction.

- **Representativeness:** 5 days, World Cup nearly absent, effective-n ~5 event-dates. A method demo, correctly
  labeled PAPER/GROSS (fees+spread in net_edge; latency, ~17–55% naked-leg, slippage, adverse-selection,
  sports-void NOT netted). Not a validated return — the writeup says this clearly.

## Result Interpretation

- **Reproduction (exact):** all-verified base $41.17 total (+8.23%); dTOTAL true +0.00 / re-entry −0.34 /
  both −0.34; dReal +8.22; 1 funded / 3 skipped; both pools 100%. Weather-only base $12.85; adds +0.04.
  All match the writeup to the cent.

- **Statistical/practical significance:** with effective-n ~5 event-dates and the deltas at the cent-to-dollar
  scale on $500 paper-gross, **no delta here is statistically distinguishable from zero** and none carries a CI
  — correctly treated as a method demo, not an estimate. The honest read is qualitative: "the genuine
  scale-in opportunity is near-absent in this window," not "the marginal is −$0.34 ± nothing."

- **dTOTAL offset-sensitivity (the key result-interpretation finding):** all-verified dTOTAL(both) by settle
  offset: 12h **+9.24** | 24h +7.20 | 28h **−0.34 (headline)** | 36h +0.04 | 48h 0.00 | 72h −0.71. The headline
  is one point in a [−0.71, +9.24] range. Because 80% of the cohort is sports settling at game-end (hours), the
  sports-appropriate offset is the SHORT end where dTOTAL ≈ +$9 — the opposite sign from the quoted −$0.34.

- **Bankroll-sensitivity (confirms the writeup's stated mechanism):** all-verified dTOTAL(both) by pool/side:
  $250 −0.34 (1 funded, 3 skipped, pools 100%) | $500 +5.85 (2 funded) | $1000 +9.11 (3 funded) | $2000+
  **+10.88 (all 4 funded, pools no longer saturated)**. The UB is reached once bankroll ≥$2k. So "capital is
  the binding constraint" is **verified true** — and it means the all-verified result is bankroll-conditional,
  not an opportunity verdict.

- **The robust finding (what the conclusion SHOULD lead with):** the owner's feature is TRUE scale-in. There
  is exactly **1 true scale-in in 5 days** (all-verified `mikars-sebgor`, inc $0.135 unconstrained); it does
  NOT fund at $250 (true dTOTAL +0.00) and is worth only +$0.135 even at infinite bankroll. Weather has **0**
  true scale-ins. Both "funded adds" driving every headline are **RE-ENTRIES** the live per-slug guard
  (`bot-rs main.rs:497`) already blocks. This finding is **bankroll-invariant AND offset-invariant** — far more
  defensible than the −$0.34.

- **What the result licenses you to conclude:** "On 5 days of paper-gross data, building a scale-in/re-entry
  add feature shows no material marginal value: the genuine scale-in opportunity is near-absent (1 true add,
  ~$0.13 unconstrained), and the rest are re-entries the live bot already blocks." What it does **NOT** license:
  quoting "−$0.34" or "adds ≈ $0 because capital is binding" as *the* unconditional marginal — that figure is
  proxy-fragile (flips to +$9 at a sports-realistic hold) and bankroll-conditional (→ +$10.88 at $2k).

## Impact on Project Scope

- **If the (corrected) conclusion stands:** the add feature is **not worth building now** — but for the right
  reason: *the opportunity barely exists in this window and the live guard already captures the only funded
  cases.* This is a "no-build / revisit on multi-week post-recon data" signal, not a "capital wall" signal.
  Decision-safe either way (both point to not building), but the *rationale* must be the robust one or a future
  reader will wrongly infer "just add more bankroll and adds pay off."

- **If the result is fragile (it is, on the all-verified `both` column):** any decision quoting the −$0.34 (or
  +8.22) as the marginal is premature. The bankroll sweep shows the all-verified add value is **entirely a
  function of how much capital the base leaves behind**, which in turn depends on the unverified sports
  settlement timing. Do not let −$0.34 enter a decision brief as an unconditional number.

- **Consistency with prior findings:** CONSISTENT and reinforcing. Confirms `venue_split`'s concentration
  caveat (largest pos 61% of capital), the [L18] window-artifact pattern, the [L20]/[L21]/[L28] phantom
  discipline, and flip_add's own "$0.04 weather / true scale-in is the real feature" framing. Complements
  flip_add by adding the bankroll constraint flip_add lacked — but should adopt flip_add's hold-window-RANGE
  discipline, which it dropped.

## Recommended Alternatives (ranked by information value)

1. **Re-headline on TRUE scale-in, bankroll/offset-invariant — the cleanest decision number.**
   *Answers:* "is the owner's actual feature (scale-in, not re-entry) worth building?" *Why better:* the −$0.34
   is the re-entry/both column, which is proxy-fragile AND describes events the live guard blocks; the true-add
   column is +0.00 (weather) / +0.00 at $250 / +$0.135 unbounded (all-verified) and does not move with offset
   or bankroll. *Decision rule:* if TRUE-scale-in dTOTAL stays < ~1 friction-unit across the planned multi-week
   data, do not build. No code change needed — the script already computes scenario B; just promote B over the
   "both" column in the bottom line and demote the −$0.34 to a footnote.

2. **Add a hold-window SWEEP to bankroll_add, mirroring flip_add §3a.**
   *Answers:* "how does the bankroll-netted add value depend on the (uncertain) settle proxy?" *Why better:* I
   showed dTOTAL(both) spans [−0.71, +9.24] over offsets 12–72h and the sports-realistic end gives +$9 — the
   single 28h point is misleading for an 80%-sports cohort. *Implementation:* loop `SETTLE_OFFSET_H` over
   `flip_add.ADD_SWEEP_OFFSETS_H = (4,8,12,24,28,48)` and print dTOTAL/dReal per offset with the
   "weather→24-48h, sports→4-12h" mapping flip_add already prints. *Decision rule:* quote the RANGE, never the
   28h point; if the range straddles zero, the result is "indeterminate at this data volume."

3. **Add a bankroll sweep (or at minimum a one-line disclosure) so "capital is binding" is shown, not asserted.**
   *Answers:* "is 'adds ≈ $0' an opportunity verdict or a capital-rationing artifact?" *Why better:* I verified
   it is capital-rationing on all-verified (dTOTAL → +$10.88 at $2k) but opportunity-limited on weather (+$0.04
   at any bankroll) — these are different stories and the writeup conflates them under one bottom line.
   *Implementation:* loop `--kalshi/--pmus` over {250, 1000, 5000} and print dTOTAL(both); state explicitly that
   the all-verified figure is conditional on $250/$250.

4. **Restate the all-verified concentration caveat inline (largest pos 61% of $500).**
   *Answers:* "is the base a portfolio or a few big bets?" *Why better:* the pool saturation that produces
   "adds can't fund" is partly driven by 2–3 large pool-bound clips, not pure breadth; a reader should know the
   base itself is concentration-flagged before reading "the base already saturates the pools." *Implementation:*
   carry `venue_split`'s `big = max(capital)` line into the bankroll_add per-section output.

5. **(Low priority) Fix the `--add-cap-frac` desync (self-review INFO-2).** The walk hardcodes module
   `ADD_CAP_FRAC` while `add_events` (count/UB) takes the CLI value; identical at the 0.20 default (headline
   unaffected) but silently inconsistent if the flag is exercised. Pass `add_cap_frac` into
   `two_pool_walk_with_adds` instead of reading the constant.

## Severity Flags

- **CRITICAL:** none. The mechanics are correct, the base is bit-identical to the proven reference, the
  bug-classes are clean, and the dTOTAL methodology call is right. A genuine, honest null result.

- **WARN-1 — Headline marginal (−$0.34) is offset-proxy-fragile and the writeup presents it as the number.**
  dTOTAL(both) flips sign across the plausible hold range (+$9.24 at 12h → −$0.34 at 28h → 0.00 at 48h); the
  cohort is 80% sports settling at game-end (the short end → +$9). The single 28h point is not representative.
  *Fix:* recommendation 2 (sweep) + lead with the offset-invariant TRUE-scale-in number (rec 1).

- **WARN-2 — "Capital is the binding constraint" is true but makes the conclusion CONDITIONAL on $250/$250,
  not unconditional.** Verified: relaxing bankroll to $2k lifts all-verified dTOTAL to the +$10.88 UB. So
  "adds ≈ $0" on all-verified is "the base spends the capital first at this bankroll," NOT "adds have no edge."
  These must not be conflated in a decision brief. *Fix:* recommendation 3 + state the conditionality.

- **WARN-3 — The funded headline adds are RE-ENTRIES the live guard already blocks; the buildable feature
  (TRUE scale-in) is near-absent (1 in 5 days, ~$0.13 unconstrained).** This is the genuinely robust result and
  it is buried under the proxy-fragile "both" column. *Fix:* recommendation 1 (re-headline on scenario B).

- **INFO-1 — All-verified base is concentration-flagged (largest position 61% of $500), inherited silently
  from venue_split.** Not a bug; restate inline (rec 4).

- **INFO-2 — `--add-cap-frac` walk/UB desync** (confirmed; headline unaffected at default 0.20). Latent
  footgun if the flag is exercised (rec 5).

---
*Verification basis: re-ran `bankroll_add_backtest.py` (numbers reproduce to the cent); independent
bit-identity check vs `venue_split.two_pool_walk` (0/8 weather, 0/18 all-verified per-position mismatches);
position-by-position reconstruction of the −$0.34/+8.22 case (reconciles exactly); bankroll sweep
{250..100000}; settle-offset sweep {12..72h}; real-data leg-split/causal/phantom-lens checks. All scratch
scripts were run from /tmp outside the repo — no project file modified.*
