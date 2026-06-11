# Adversarial Readiness Audit — Dimension E: COST-MODEL COMPLETENESS

**Question:** the edge filter nets only quoted taker fees + the spread it crosses. Enumerate every other cost/friction, size each from data, and compute how much of the apparent ~0.39¢ median edge survives an honest all-in accounting.
**Verdict:** **The median arb is structurally underwater all-in; only the ≥1¢ capturable cohort survives, and only at fast latency — and even that survivor is fragile because the single biggest unmodeled friction (leg-fill / naked-unwind) is the same term auditB flags as unmeasurable from these logs.** After every *modelable* friction, the all-in net edge is **weather +0.36¢ to +0.96¢ (n=152)** and **sports −0.41¢ to +0.22¢ (n=647)** on the ≥1¢ cohort; on the typical (median) arb it is **−0.69¢ weather / −1.18¢ sports** — i.e. negative. Largest missing friction: **leg-fill EV** (0.5–1.3¢, dwarfs slippage/carry/void). Sub-tick illusion: **85% of all apparent edges are <1¢** and uncapturable at a 1¢ tick (median 0.39¢ sits below one tick of price resolution).

Basis: full archive (pre+post-0013), span **1.98 d**, 60,363 transitions, **5,804 measured episodes** (clean+eod; 281 restart phantoms excluded; 36 econ off-by-one records quarantined). Capturable ≥1¢ cohort n=799 (152 weather / 647 sports / 0–3 econ). Scripts reused unmodified via import: `analyze_persistence`, `capital_sim` (slippage ladder + `void_haircut`), `shadow_fill` (leg-fill survival), `bot/ledger` (fee curves). Driver + raw numbers: `scripts/_data/auditE_costs.py`, dump `scripts/_data/auditE_costs_dump.json`.

---

## TASK 1 — Full friction inventory (in-model vs MISSING)

| # | Cost / friction | In model? | Where | Size (¢/contract round-trip) |
|---|---|---|---|---|
| 1 | **Taker fees** (both venues) | ✅ IN | `ledger.kfee`/`pfee`, primary-pinned | **0.57¢ (deep tails) → 3.0¢ (ATM)**; this is already the LARGEST cost |
| 2 | **Spread crossed** | ✅ IN | `signal` lifts both asks, not mids | embedded in `net_edge` (it's the ask-to-ask gap) |
| 3 | **Size slippage / walk-the-book** | ⚠ PARTIAL | `depth` `{c2,c1,c0}` logged; NOT in the edge filter | **0.35¢ (sports) / 0.42¢ (weather)** decay at clip=c2; see Task 2 — net-marginal walk far worse |
| 4 | **Latency haircut** | ❌ MISSING | `age` logged; `--haircut` defaults 0 | folds into leg-fill (the edge that erodes in-flight either narrows or dies) |
| 5 | **Leg-fill-failure EV** P(one)·naked-loss | ❌ MISSING | "in no number yet" (0010 B1, the dominant cost) | **0.53¢ @0.25 s → 1.24¢ @1 s** (this audit; auditB models it rigorously as `u`) |
| 6 | **Settlement-void EV** (sports) | ⚠ PARTIAL | `capital_sim.void_haircut`, NOT in `signal` | **MLB 0.26¢ / other sports 0.10¢**; blended sports **0.13¢**; weather 0 |
| 7 | **Min-tick / sub-cent rounding** | ❌ MISSING | 1¢ price tick pins an edge FLOOR | **kills 85% of apparent edges** (median 0.39¢ < 1 tick) — Task 4 |
| 8 | **Cost-of-carry** on locked capital | ❌ MISSING | capital locked fill→settle | weather **0.03–0.07¢** (~1.2 d); sports **0.31–0.77¢** (14 d frozen) |
| 9 | Settlement fees | ✅ IN (=0) | Kalshi "no settlement fee" (CFTC-filed); pmus none | 0 |
| 10 | Maker fees/rebates | n/a taker model | weather maker = $0 Kalshi + −0.0125 pmus rebate | only relevant if strategy flips to maker (would HELP weather) |
| 11 | Deposit/withdraw frictions | ❌ note-only | ACH free; wire ~$25/bank | amortized ~0 at scale; one-time, not per-contract |
| 12 | Taxes | ❌ note-only | short-term gains on every settled pair | post-hoc on net PnL; not a per-trade gate input |

---

## TASK 2 — Sizing the missing frictions from data

### Slippage — the `{c2,c1,c0}` ladder IS the decay curve (project's own primitive)
`c2`/`c1`/`c0` = cumulative fillable PAIRS while the **gross** marginal pair edge stays ≥2¢/1¢/0¢. Walking the book past `c2` dilutes the average. On the ≥1¢ cohort, median ladder = **c2=136, c1=152, c0=209** (sports c2=150; weather c2=25 — weather is thin). Book-average gross edge vs clip size (median over cohort):

| clip (contracts) | avg gross edge realized | decay vs touch |
|--:|--:|--:|
| touch | ~2.70¢ (sports) / 2.84¢ (weather) | — |
| **c2 (≈136)** | 2.35¢ / 2.42¢ | **−0.35¢ / −0.42¢** |
| 200 | 2.25¢ / 2.06¢ | −0.45¢ / −0.78¢ |
| 1000 | 2.11¢ / 1.97¢ | −0.59¢ / −0.87¢ |

So at the project's conservative deployable size (c2), slippage is only ~0.35–0.42¢. **BUT `c2` is GROSS-≥2¢ *displayed* depth — an upper bound the monitor caveats as "never pinged."** The honest measure is the crossable NET-marginal walk:

### Weather NET-marginal walk (from the top-5 ladders — the honest crossable curve)
65/111 detection-time weather ladders had a positive-net touch. Median **touch net = 0.32¢**, and the **median size while net>0 is just 6 contracts**; averaging over 50 contracts the net goes to **−3.78¢**, over 200 to **−6.58¢**. This reproduces L18's two-pointer finding (~157 crossable @edge≥0, ~1 @gross-2¢): **weather's deep-resting books are efficient — true lockable size at positive net is single-digit contracts, and slippage turns sharply negative within tens of contracts.** The `c2` summary (0.42¢ decay) is optimistic; the real crossable depth is ~6 contracts.

### Leg-fill EV (the dominant missing term)
From `shadow_fill` survival at the measured 86–261 ms RTT band (mid L=0.25 s) and a sluggish 1 s decision-loop. Charge = leg-fail-rate × 0.5 (≈ fraction that filled exactly one leg) × naked-loss (conservative 5¢ = half-tick adverse move + the lost edge). *auditB models this same term more rigorously as the per-contract unwind cost `u`; the conclusions agree.*

| latency | leg-fail % (all) | leg-fill cost ¢ | weather cost ¢ | sports cost ¢ |
|--:|--:|--:|--:|--:|
| 0.15 s | 23.2% | 0.58 | — | — |
| **0.25 s** | 29.8% | **0.74** | **0.53** | **0.80** |
| **1.0 s** | 49.7% | **1.24** | **1.12** | **1.27** |

This is **larger than slippage + carry + void combined** at every latency. It is the friction that decides go/no-go, and it is the one *not* in any filter.

### Cost-of-carry on locked capital (~$1 notional/pair)
| capital cost (annual) | weather (1.2 d) | sports (14 d, book frozen) |
|--:|--:|--:|
| 8% | 0.03¢ | 0.31¢ |
| 12% | 0.04¢ | 0.46¢ |
| 20% | 0.07¢ | 0.77¢ |

Carry is negligible for weather (fast natural settle + evening early-exit) but **material for sports** — a 14-day frozen-book lock at 20% costs 0.77¢, ~half a typical ≥1¢ edge. This compounds the velocity finding: sports is capital-slow *and* the slow capital itself is a cost.

---

## TASK 3 — Honest ALL-IN net edge (weather vs sports)

Start from the quoted (taker-net) median, subtract each modelable friction. **Optimistic** = L=0.25 s leg-fill + 8% carry; **pessimistic** = L=1 s leg-fill + 12% carry. Sports also pays blended void 0.13¢.

| | WEATHER (n=152) | SPORTS (n=647) |
|---|--:|--:|
| Quoted net, ≥1¢ cohort median | **1.94¢** | **1.80¢** |
| − slippage (clip=c2) | −0.42¢ | −0.35¢ |
| − leg-fill EV (opt / pess) | −0.53¢ / −1.12¢ | −0.80¢ / −1.27¢ |
| − carry (opt / pess) | −0.03¢ / −0.04¢ | −0.31¢ / −0.46¢ |
| − void | 0 | −0.13¢ |
| **= ALL-IN (optimistic)** | **+0.96¢** | **+0.22¢** |
| **= ALL-IN (pessimistic)** | **+0.36¢** | **−0.41¢** |
| **ALL-IN on the MEDIAN arb (0.28¢/0.40¢ quoted)** | **−0.69¢** | **−1.18¢** |

**Read:** on the *cherry-picked* ≥1¢ cohort, weather survives all-in (+0.36 to +0.96¢) and sports is a coin-flip around zero (−0.41 to +0.22¢). On the *typical* arb (the 0.39¢ median the prompt names), **both categories are firmly negative all-in** — the median arb does not clear its own frictions. And the ≥1¢ "survivor" rests on the optimistic-latency leg-fill number; push to a 1 s decision loop and sports goes negative, weather thins to +0.36¢. The honest deployable edge lives ONLY in the ≥1¢-AND-fast-fill corner, on weather, at single-digit-contract size (Task 2 crossable depth).

---

## TASK 4 — The sub-tick illusion

With a 1¢ price tick, an edge below 1¢ cannot be improved into existence (you cannot post or lift at a finer price), so a sub-1¢ net edge is uncapturable at execution. Share of the apparent (taker-net) edge distribution that is sub-tick:

| cohort | median | **share <1¢** | share <0.5¢ | share ≥1¢ | share ≥2¢ |
|---|--:|--:|--:|--:|--:|
| all (n=5804) | 0.39¢ | **85.2%** | 59.1% | 14.8% | 6.4% |
| weather (n=1235) | 0.28¢ | 86.6% | 64.6% | 13.4% | 6.1% |
| sports (n=4565) | 0.40¢ | 84.8% | 57.5% | 15.2% | 6.5% |

**85% of every "positive-edge arb" the monitor logs is sub-1¢ and therefore illusory at a 1¢ tick** — the median arb (0.39¢) is below a single tick of price resolution. The apparent opportunity rate (2,055 capturable/day, "1,872¢/day of edge per $1") is dominated by this sub-tick dust: only ~14.8% (≈434/day extrapolated) clears 1¢, and that subset is what Task 3 shows barely survives all-in. This is the single largest haircut on the apparent edge — bigger than any per-contract friction — because it removes most of the *count*.

---

## TASK 5 — Blindspots (cannot size from current data)

1. **Naked-unwind cost `u` (the decisive one)** — I bound leg-fill with a conservative 5¢ naked-loss; the TRUE per-contract cost of flattening a naked leg (re-cross spread + adverse move + taker fee) needs live order data. auditB shows the go/no-go sits *inside* this unmeasured band; this audit's leg-fill term inherits that uncertainty. **Needs live fills.**
2. **Real fill rate vs shadow-fill** — shadow-fill is the monitor's own caveat-flagged OPTIMISTIC upper bound (transitions fire only on >epsilon moves; real books tick between logged transitions). True leg-fail is *worse* than the 23–50% here. **Needs live quote-stream + order acks.**
3. **Adverse selection on the filled leg** — when only one leg fills, it's because the *other* side moved away, so the leg you got is systematically the worse one. I charged a flat 0.5×5¢; the real conditional loss is almost certainly larger and asymmetric. **Needs live fills.**
4. **`px` coverage** — the per-record touch-price field was only added in the late-06-11 wave-2 monitor, so I could not reconstruct per-arb GROSS gaps across the bulk (06-10) archive; touch-gross used a fee-curve estimate. Doesn't move the decay (ladder-shape-driven) but blocks an exact per-arb fee attribution. **Needs post-wave-2 data accumulation.**
5. **Slippage realism** — the `{c2,c1,c0}` summary is gross-≥2¢ displayed depth (over-counts takeable size); only weather has the top-5 ladders for the honest net walk. Sports slippage is therefore *understated* here (I used the optimistic c2 summary). **Needs per-venue depth ladders for sports** (currently weather-only).
6. **pmus order-book freeze at resolution** — already known (no early exit → carry to far endDate), but the *interim* mark you'd actually exit at is unmeasured. **Needs live pmus settlement data** (settle_recon still open).
7. **Funding/borrow if levered, FX, withdrawal timing** — out of scope at this capital scale; one-time, amortizes to ~0 per contract.

---

### Bottom line for the owner
The cost model is **materially incomplete in the bad direction**, exactly as L10/L15/L18 predicted. The quoted edge is a ceiling; the all-in floor is: **median arb negative, ≥1¢ weather marginally positive (+0.4 to +1.0¢), ≥1¢ sports a zero-crossing coin-flip.** The two costs that hurt most are *not* per-contract sizing details — they're (a) the **1¢ tick erasing 85% of the opportunity count** and (b) the **leg-fill/naked term (0.5–1.3¢) that the logs cannot measure**. Real-capital deployment cannot be gated on these read-only numbers: the deciding friction needs live order data (concurs with auditB). If deployed at all, the data says **weather-only, ≥1¢-only, fast-fill-only, single-digit-contract clips** — the one corner where the all-in stays positive.
