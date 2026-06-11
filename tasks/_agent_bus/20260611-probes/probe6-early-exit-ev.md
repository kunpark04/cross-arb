# Probe #6 — Weather early-exit EV: hold a won pair vs exit in the evening window

**Verdict: REJECT exit-all (−EV at every plausible flip probability); boundary-days-only survives
only as a Kalshi MAKER-exit and only if P(flip) > ~2% or the bankroll genuinely binds — neither is
shown by current data → default HOLD-ALL, revisit with more station-days.** Computed, not assumed:
`scripts/early_exit_ev.py` (new, `--selftest` clean); outputs `scripts/_data/early_exit_ev.{txt,json}`.

## The loss structure (derived, then encoded + self-tested)

pmus locks at 8 AM on the first CLI; Kalshi takes the final (≤11 AM). A downward 8–11 AM correction
crossing a bucket boundary grades the venues into different buckets — and **the diverging leg is
always the Kalshi leg** (pm's settlement = the evening-known high, flip or not). Per held pair on
bucket X vs the evening-known high m:

| held bucket | dir P (YES@pm+NO@K) | dir K (YES@K+NO@pm) |
|---|---|---|
| X = winner bucket | flip → pays **2** (FAVORABLE) | flip → pays **0** (EXPOSED) |
| X below winner | flip-in → **0** (EXPOSED) | flip-in → **2** (FAVORABLE) |
| X above winner / low-tail winner | 1 (SAFE) | 1 (SAFE) |

Consequences the EV math uses: (a) an EXPOSED pair's winning (~$1) leg is **always on Kalshi**, so
selling that one leg removes the whole exposure (the pm leg is flip-invariant); (b) a FAVORABLE
pair's winner-leg exit is on pm and **keeps the Kalshi flip-lottery for free** (the ~$0 K leg pays 1
on a flip) — so the tail never argues for exiting favorable pairs; (c) "boundary pair" = needs only
a −1°F correction (`d_req=1`): print on its bucket floor, or held bucket adjacent-below the print.

## Measured inputs (all from code run now; n per cell)

- **Evening winner bids** (post-0013 `px` touches, NYC 06-10 19:00–19:52 ET, n=10 records, 1
  bucket-evening + exit_liquidity's 2 pm probes): **pm bid 0.99; Kalshi bid 0.96–0.97, ask 0.98**.
  The venue asymmetry is load-bearing: exposed pairs exit on the *worse* (Kalshi) side.
- **Capital rate**: $209/day gross on $3,980 avg concurrent capital = **5.3%/day** (capital_sim
  cohort ≥1¢/≥30s/clip1000, n=98 arbs, 1.81 d span — PRELIMINARY); **30%** of capturable edge
  arrives in the freed window (8 PM–9 AM ET) → **ctv ∈ [0, 1.55¢]/pair** (upper bound assumes the
  bankroll binds AND marginal=average; at today's $0 deployed, ctv = 0).
- **Downward CLI revisions**: **0/14 station-days** (1 upward: MDW 06-09 87→88) → Jeffreys 95%
  ceiling **12.6%/station-day** on the any-downward rate (superset of the 8–11 AM boundary-crossing
  event). 14 station-days cannot resolve P1 against a ~2% breakeven — grid P1 ∈ {0.1,0.5,1,2}%.
- **Boundary-day rate**: **1/4** joinable station-days (SFO 06-10: print 79 ON the [79,∞) floor;
  MDW 91, MIA 88, NYC 82 all landed on bucket caps → need −2°F). A-priori ~50% for 2°F buckets; n tiny.
- **Held-pair mix** (capturable weather pairs one-per-market, n=39 held; 16 classified vs a print):
  **exposed-boundary 1** (SFO [77,78] dir P vs print 79), exposed d=2 1, exposed unreachable (d≥3)
  2, **favorable 8**, safe 4. So ~6% of pair-evenings are the tail case the policy targets.
- Settlement fees: Kalshi **none** (CFTC-filed, verbatim); pmus none mentioned → hold pays 1.0 flat.
- Priors (labeled, sensitivity-swept): P(corr ≥2°F | corr) = 0.3; loser-leg bids pm 0.01 / K NO 0.02.

## EV table (¢ per contract pair, exit − hold; fees = ledger.py marginal)

| pair class / variant | ctv=0 | ctv=0.78¢ | ctv=1.55¢ (max) |
|---|---|---|---|
| SAFE/FAVORABLE, taker exit pm leg @0.99 | **−1.05** | −0.27 | +0.50 |
| SAFE, taker exit Kalshi leg @0.965 | −3.74 | −2.96 | −2.18 |
| EXPOSED boundary, taker K @0.965 (P1=1%) | −2.74 | −1.96 | −1.18 |
| EXPOSED boundary, **maker** K @0.98 $0-fee (P1=1%) | −1.00 | −0.22 | +0.55 |
| EXPOSED boundary, maker (P1=2%) | **0.00** | +0.78 | +1.55 |
| EXIT-ALL policy, whole 16-pair population (P1=1%) | | −24.6¢ total ≈ −1.5¢/pair | |

**Breakeven flip probability P\*** (exposed boundary pair): **taker 3.74%** / **maker 2.00%** at
ctv=0; with max-ctv 2.18% / 0.45%. Measured 0/14 downward (Jeffreys ceiling 12.6%) — the data is
*consistent with* P1 both above and below P\*; it cannot yet license the exit.

## Recommendation

**Reject all-days early exit** — it pays 1–2.1¢+fees per pair to dodge a tail that 8/16 of pairs are
on the *favorable* side of, and the capital argument only rescues it under the most optimistic
(binding-bankroll, marginal=average) reading of 1.81 days of data; at the current $0 deployed,
ctv=0 and exit-all is −1.05¢ (pm leg) to −3.74¢ (K leg) per pair, every day. **Boundary-days-only,
exposed-pairs-only, as a resting Kalshi maker order ($0 weather maker fee, rest at the 0.98 ask)**
is the only variant with a plausible positive region (P\*=2.0% → 0.45% if capital binds), affects
~6% of pair-evenings, and even then a **cheaper instrument likely dominates: buy Kalshi YES on the
flip-destination bucket** at its ~1–2¢ ask (pays 1 exactly when the exposed pair pays 0; no 2–3.5¢
bid-discount; costs capital instead of freeing it — destination-bucket evening ask is UNMEASURED).
**Needs-X-more-data**: (a) O(100) station-days of `cli.jsonl` to bound P1 below/above 2% (at 0
observed downward, the Jeffreys ceiling computes to 3.1% at 60 station-days, 1.9% at 100, 1.3% at
150 — i.e. ~3 weeks of 5-station logging crosses the maker breakeven); (b) one
measured evening ask on a flip-destination bucket; (c) a real bankroll-binding measurement for ctv.
Until then: **hold to settlement; on a boundary day prefer entering the tail-favorable direction**
(dir P on the winner bucket / dir K below it) — it converts the same tail into a free lottery, at
zero cost, which no exit policy can beat.

## Caveats

- Evening bids: n=10 px records on ONE bucket-evening (+2 probe reads) — b sensitivity is in the
  script flags (`--b-pm`, `--b-k`); ±1¢ on b moves every Δ by 1¢ and P\* one-for-one.
- The K-leg 0.965 bid was measured on the *winner* bucket; loser-bucket NO exits (= 1 − loser ask)
  are likely ~0.98, so the SAFE-K row and EXIT-ALL total are ~1–1.5¢/pair *pessimistic* — the
  exit-all verdict survives the correction with margin.
- "High locked ~6 PM" is the standing project assumption; a late-evening high *rise* before the 8 AM
  CLI is a separate (unmodeled, both-venues-symmetric) risk to the exit decision itself.
- frac2 (share of corrections ≥2°F) = 0.3 prior, unmeasured — only touches the d_req=2 rows.
- All capital figures inherit capital_sim's preliminary status (single ~2-day window, walk-the-book
  model, displayed-depth upper bounds).

Artifacts: `scripts/early_exit_ev.py` (selftest: beta/Jeffreys, taxonomy, EV hand-cases, breakevens,
boundary join) · `scripts/_data/early_exit_ev.txt` (full report) · `scripts/_data/early_exit_ev.json`
(machine-readable summary incl. per-pair classifications).
