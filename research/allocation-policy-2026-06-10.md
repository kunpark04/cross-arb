# Clip-stage allocation policy — FIFO vs edge-floor + cap (tested out-of-sample)

**Date:** 2026-06-10 · **Phase:** READ-ONLY (paper/gross; no orders) · **Status:** method demo, not validated
**Scripts:** `scripts/alloc_policy_experiment.py`, `scripts/clip_threshold_test.py` (both `--selftest`)
**Audit:** `tasks/_agent_bus/20260610-0522/stats-ml-logic-reviewer.md` (numbers reproduced, OOS split checked for leakage)
**Data:** `Kalshi/data/cross-arb/` mirror, **0.81 d** of candidate-arb arrivals (one event cluster) — PRELIMINARY.

> ## ⚠ CORRECTION (2026-06-10 full review, [0013](../decisions/0013-econ-grid-step-twin-and-measurement-integrity.md))
>
> The OOS tables below contain **one econ position (`urc-…-atl4pt4`) that was a settlement-identity
> phantom** — the pre-0013 matcher paired pmus `≥4.4` with Kalshi `>4.4` (off by one print-grid bucket),
> so its "12.2¢ edge" was the market-priced P(print==4.4), not an arb
> ([econ-settlement-identity brief](econ-settlement-identity-2026-06-10.md)). The stats audit verified
> arithmetic + split hygiene but not the settlement identity of the inputs. Econ-quarantined +
> close-lag-corrected reruns (same scripts, fixed pipeline):
>
> | policy (test half, $500) | PnL | vs FIFO | published (wrong) |
> |---|---|---|---|
> | FIFO | $6.21 (funds 9) | — | $5.15 |
> | **FIXED 2¢ + 5% cap (26)** | $6.78 · 10 pairs · top-1 19% | **+9%** | +64% |
> | FIXED 2¢ + 10% cap (51) | $9.79 · 10 pairs · top-1 21% | **+58%** | +153% |
> | **FIXED 2¢ + 20% cap (102)** | $14.97 · 10 pairs · top-1 27% | **+141%** | +269% |
> | FIXED 2¢ + 40% cap (204) | $17.06 · 8 pairs · top-1 48% | +175% (re-concentrates) | +232% |
>
> The phantom was ~20% of design PnL in the published rows *and* depressed the FIFO denominator.
> **Findings 1–3 and 5–7 stand directionally** (FIFO loses when the bankroll binds; the lever is the
> edge floor; batch-sort is the wrong instrument; clip-cap-alone earns nothing: −14% corrected). The
> **deployable claim is now ~+9% to +141%**, on 10 diversified weather+sports pairs, still a
> <1-day method demo. Tables below are kept as published for the audit trail — read them with this block.

## Question

The not-yet-built trade-selection ("clip") stage must allocate a fixed bankroll across arbs that arrive over
time and **lock to settlement** (pmus freezes its book at resolution — no early exit). `account_sim.py`
allocates **strictly FIFO by arrival** (`sorted(key=open_t)`). Is that the right policy, or does prioritising
bigger arbs help? Owner's specific proposal: *wait ~1 s, sort that batch largest-first.*

## Method

Every policy reuses `account_sim`'s **exact** per-contract economics (cost, book-average edge, void haircut,
size cap, settlement recycling); only the **consideration order + a τ gate + the clip cap** change, so the
comparison is apples-to-apples (verified: each policy's FIFO PnL reconciles to `account_sim.run_account` to
<1e-9). Two knobs:
- **τ (tau) = edge floor** — minimum *booked per-contract* edge to fund a pair (booked ≈ ½ the displayed/touch
  edge). A reservation price: skip thin arbs, keep powder for fat ones.
- **clip cap** — max contracts per pair, expressed as % of bankroll (5% of $500 ≈ 26 contracts).

`clip_threshold_test.py` adds a **causal out-of-sample split**: fit (τ\*, clip\*) on the early-arrival half,
apply it *fixed* to the unseen late half with a fresh $500, and compare to FIFO and to **non-oracle** fixed
rules. An in-sample τ sweep is an **oracle** ([L19]); only the held-out, non-tuned number is trustworthy.

## Headline results (post-[L20] phantom fix, $500 bankroll)

Each "pair" = one arb = a YES leg on one venue + the offsetting leg on the other (sports pairs use a pmus game
market + 2 Kalshi single-team tickers). All positions are **unrealized** (locked to settlement; nothing
recycles in <1 day).

### TEST / out-of-sample (98 arbs, the honest half)
| policy | τ | clip cap | pairs | deploy | top-1 | cat mix | PnL | vs FIFO |
|---|---|---|---|---|---|---|---|---|
| FIFO | 0¢ | none | 7 | 158% | 43% | spo5,wea2 | $5.15 | — |
| clip-cap only | 0¢ | 26 (~5%) | 29 | 105% | 26% | spo21,wea8 | $4.99 | −3% |
| FITTED (tuned) | 5¢ | none | 3 | 77% | **93%** | eco1,spo1,wea1 | $28.93 | +462% |
| FIXED 2¢ + 5% cap | 2¢ | 26 | 11 | 42% | 20% | eco1,spo5,wea5 | $8.43 | **+64%** |
| **FIXED 2¢ + 20% cap** | 2¢ | 102 | 11 | 100% | 22% | eco1,spo5,wea5 | $18.98 | **+269%** |

### OOS cap sweep (τ=2¢) — FIFO baseline $5.15
| cap | clip | pairs | deploy | top-1 | PnL | vs FIFO |
|---|---|---|---|---|---|---|
| 5% | 26 | 11 | 42% | 20% | $8.43 | +64% |
| 10% | 51 | 11 | 65% | 25% | $13.03 | +153% |
| **20%** | 102 | 11 | 100% | 22% | $18.98 | **+269%** |
| 40% | 204 | 8 | 100% | 48% | $17.06 | +232% |
| none | — | 4 | 100% | 89% | $22.73 | +342% |

### FULL / TRAIN in-sample (oracle — do NOT quote as results)
| slice | policy | τ | cap | pairs | deploy | top-1 | PnL | vs FIFO |
|---|---|---|---|---|---|---|---|---|
| FULL (219) | FIFO | 0¢ | none | 1 | 100% | 100% | $1.97 | — |
| FULL | FITTED | 5¢ | none | 3 | 87% | 74% | $36.25 | +1744% |
| FULL | FIXED 2¢+20% | 2¢ | 102 | 11 | 100% | 39% | $22.31 | +1035% |
| TRAIN (139) | FIFO | 0¢ | none | 1 | 100% | 100% | $1.97 | — |
| TRAIN | FITTED | 5¢ | none | 1 | 11% | 100% | $8.63 | +339% |
| TRAIN | FIXED 2¢+20% | 2¢ | 102 | 9 | 94% | 41% | $21.09 | +973% |

## Findings

1. **FIFO is the worst rule when the bankroll binds.** At $500 the first arb to cross is a deep clip that eats
   ~$500, so FIFO funds **1 of 219** candidates. Prioritising bigger arbs is directionally right.
2. **The owner's "wait 1 s + sort" (batch1s) is the wrong instrument.** It captures only **3.6%** of the
   FIFO→optimal gap (arbs almost never collide within one second — they're spread across the multi-*day* lock
   horizon) and ties FIFO at $2 k, while *adding* an entry-latency tax (`shadow_fill`: ~39% naked-leg at 1 s).
   Allocation priority is a bankroll decision over days; it must **not** be an execution delay.
3. **The lever is a global edge floor (τ) — the clip cap alone earns nothing.** Clip-cap-only is **−3% OOS**
   (capping in FIFO order just diversifies into *thin* arbs). The cap's value is **risk-control** — bounding
   per-pair exposure against a settlement-void / leg-fail — not return; the τ filter does the return work.
4. **Trustworthy deployable claim: τ=2¢ floor + a deploy-to-full cap (~10–20%) beats FIFO ~+64% to +269% OOS**,
   across 11 diversified positions (top-1 ≤22%). The cap should be sized so the bankroll **fully deploys**
   without over-concentrating: 5% under-deploys to ~42% (idle cash), ~20% deploys 100% at top-1 22%, ≥40%
   re-concentrates (top-1 climbs, PnL falls), "none" is a 90%-one-bet fragility.
5. **The oracle numbers are not validation.** The fitted "+462% OOS" is **93% one econ contract**
   (`urc-us-seasonadj…2026-07-02`); drop it and the fitted policy falls **below** FIFO (−61%), while the
   diversified fixed rule still wins (+29%). The in-sample "+1744%" is a swept-τ ceiling.
6. **Dynamic edge-proportional cap (cap% = k·edge):** sound instinct (conviction sizing), but a *pure*
   proportional cap never excludes dust — it dilutes into 77 micro-positions (+48% OOS). With a 2¢ floor it
   recovers (+207%) but only **ties** the simple static rule; the two coherent corners are *high-τ + big clips*
   (concentrated) or *low-τ + diversified cap* (robust). Choice is a prior, not settled by this data.
7. **With a 2¢ floor the allocation problem mostly dissolves** — only **~19 arbs clear 2¢ in 0.81 d**
   (34 on touch edge), and they nearly all fit in $500. The binding constraint reverts to **capital velocity**:
   one continuous $500 funds ~10 of them then locks till settlement. Ordering is second-order to velocity.

## The phantom that corrupted the in-sample numbers ([L20])

The fattest "arb" found — `aec-itfm-fravaz-fedval-2026-06-09`, **37.7¢ touch / 19¢ booked, depth 690** — was a
**book-initialization phantom**: a single OPEN captured **1.5 s after a fresh resubscribe** (`age k=0.0`) during
a ~10-restarts-in-90-min storm, with a **flat `c2==c1==c0=690` ladder** (one resting level, not crossable depth),
`censored="restart"` (no natural close), on the lowest-liquidity sport via a surname join (L1 risk). It passed
every filter and was **75% of the old in-sample headline** (FITTED $142 → $36 once removed).

**Root cause:** `analyze_persistence.summarize()` already excluded restart-censored from its CAPTURABLE metric
(line 192), but the shared `capital_sim.capturable()` — which `account_sim`/`alloc_policy`/`clip_threshold`
actually call — **never honoured that rule** (grep: zero references to `censored`). Instrumentation ≠ gating.
**Fix:** `capturable(..., drop_restart=True)` now excludes restart-censored by default (selftest added). Effect:
candidates 226→219, in-sample +7127%→+1744%, **OOS conclusions unchanged** (the phantom lived in the in-sample
half). Still open (needs a monitor-schema change): the `k=0` fresh-subscribe tell is discarded (`build_episodes`
keeps only `max(p,k)`), and there is no flat-depth (`c2==c1==c0`) guard — both deferred.

## Caveats

- **0.81 d / one event cluster**, OOS halves ~0.4 d each, ~11 funded pairs. A **method demo of the design's
  *shape*** (floor earns, cap diversifies, fixed rule stable across halves), **not a validated magnitude**.
- Paper/gross: fees+spread netted; latency/leg-fill/slippage NOT (except where the friction test adds a haircut).
- τ here is *booked* per-contract edge (≈ ½ touch). The 2¢ floor ≈ 3.5¢ touch.
- Real validation needs the weeks of multi-date data now accumulating: K-fold over disjoint windows, friction
  inside the OOS arm, per-position bootstrap CIs.

## Sources

- `scripts/alloc_policy_experiment.py` — fifo / batch1s / offline_knapsack / reservation on the live mirror.
- `scripts/clip_threshold_test.py` — τ×clip surface, causal OOS split, friction, dynamic cap.
- `scripts/capital_sim.py` `capturable()` — the shared gate (now drops restart-censored, [L20]).
- Audit: `tasks/_agent_bus/20260610-0522/stats-ml-logic-reviewer.md`.
- Decision [0012](../decisions/0012-clip-allocation-edge-floor-and-phantom-filter.md) · lessons [L19], [L20].
