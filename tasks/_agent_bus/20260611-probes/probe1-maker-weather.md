# Probe 1 — maker-side weather execution: fees, fill BOUNDS, adverse-selection BOUNDS

**Date:** 2026-06-11 (data pulled 2026-06-10 ~20:00 UTC). **Code:** `scripts/maker_feasibility.py`
(new; offline `--selftest`; reuses `ledger.kfee/pfee` for taker formulas — no drifted copy, L15).
**Data:** `Kalshi/data/cross-arb/` archive — 4,934 px-bearing weather observations (419 pre-0013 /
4,515 post), 46 `tc-temp-*` markets, 15.4 observed market-days, 98 censor events (pm 41 / k 57).
**Dump:** `scripts/_data/maker_feasibility_summary.json`.

**Binding limitation (stated up front):** the monitor logs touches only AT edge-state transitions
(OPEN/CLOSE/WIDEN/NARROW ≥0.2¢), not per tick. Everything below about quote dynamics is a **bound
from sparse, activity-biased sampling**, not a measurement. ~1.8 days, one cluster — method demo
grade ([L19]).

## Verdict

**Fees say go, bounds say "plausibly +EV on exactly ONE configuration, unproven."** The weather fee
wall inverts for makers (maker-maker ≈ **−0.06 to −0.31¢**, a net rebate, vs ~0.7–3.0¢ taker-taker)
— but adverse selection decides everything:

- **Rest-on-Kalshi + taker-hedge-on-pmus is the only survivor.** Hedge slippage med 2¢; with a
  cancel-after-1h cap, visible-fill lower bound 26–45% of attempts, net|fill med +0.8 to +2.2¢,
  **per-attempt EV +0.14 to +0.44¢/contract** (unfilled = 0). That is ~1–2× the all-taker median
  open net (0.26¢ this archive, n=1,117 OPENs) **with no leg-race at entry** — you hedge *after* a
  confirmed fill instead of racing the 55.5%-naked-at-1s taker problem.
- **Rest-on-pmus is toxic.** A pmus passive fill arrives only on a big informed move (the bucket
  dying/spiking): hedge slippage med **15–16¢**, per-attempt **−0.7 to −5.0¢**. The −0.0125 rebate
  is irrelevant at that slippage. Do not rest the pmus leg unhedged.
- **Full maker-maker is unpriceable from this data:** 33% joint-fill (locks med 8.2¢ — positive *by
  construction*, so that number carries no information), but **32% exactly-one-leg-naked** with a
  med **2.2 h** unhedged window between fills. The naked directional tail is the whole question and
  this archive cannot price it.

**Single most valuable measurement the ladder spec unlocks: trade prints on the 5 Kalshi weather
series** (+ top-N ladder for queue-ahead) — they convert the fill *bound* into a queue-aware fill
*measurement* and stamp the **true fill time**, so slippage is measured at fill instead of at next
visibility. The gap they close is measured, not hypothetical: Kalshi printed **~23,800 weather
trades in the last 24 h** (live public-REST count, 2026-06-11, ~400 prints/market-day, with
`taker_side` aggressor on every print) vs the **220** definite crossings the transition-sampled
series can see in 15.4 market-days — the visible bound misses ~2 orders of magnitude of trade
activity. The REST endpoint (`GET /trade-api/v2/markets/trades`) is public, cursor-paged, and
needs no new WS wiring. See `ladder-logging-spec.md`.

## 1. Exact round-trip fees (¢ per contract-pair, marginal; legs YES@p pmus + NO@(0.98−p) Kalshi)

Kalshi weather series (all 5: KXHIGHTSFO/LAX/NY/MIA/CHI) are `fee_type=quadratic` → **resting orders
pay $0**; pmus maker = **−0.0125·p(1−p) rebate** (research/fee-pin-2026-06-10.md, primary-pinned).
`ledger.kfee(taker=False)` is series-blind by design; the per-series model lives in
`maker_feasibility.py`.

| yes_p | taker-taker | mkr@pmus+tkr@K | tkr@pmus+mkr@K | maker-maker | MM on MLB-type series |
|--:|--:|--:|--:|--:|--:|
| 0.05 | 0.693 | 0.396 | 0.237 | **−0.059** | +0.054 |
| 0.10 | 1.189 | 0.627 | 0.450 | **−0.113** | +0.072 |
| 0.30 | 2.573 | 1.261 | 1.050 | **−0.263** | +0.118 |
| 0.50 | 2.997 | 1.435 | 1.250 | **−0.312** | +0.124 |

The ~3¢ taker wall at mid prices is exact (2.997¢ at 0.50/0.48). One Kalshi maker leg cuts it ~58%;
maker-maker is fee-*negative* on weather only (the MLB/econ contrast column stays positive — the
maker edge is category-shaped, exactly the fee-pin finding). Booking adds Kalshi's per-order cent
ceil: ≤1¢/order ≈ 0.01¢/contract at 100-lot (detection stays marginal, L10/L15).

## 2. Maker fill-rate BOUNDS (level-crossings in transition-sampled touches)

Logic: a book can never rest crossed, so if a venue's opposing touch at a later observation is
**strictly through** a hypothetical resting level placed at an earlier observation, that order was
provably filled in between → **lower bound**. An `==` touch is queue-ambiguous (counted separately).
What sampling hides: dip-through-and-back between observations is invisible → true rate ≥ bound;
the upper bound is unobservable in this data.

Consecutive-pair (pegged join-the-touch) definite crossings, censor-clean (n = 4,563 Kalshi /
4,732 pmus obs-pairs):

| rest | definite | touch-only | per observed market-day |
|---|--:|--:|--:|
| Kalshi bid | 89 | 89 | 5.8 |
| Kalshi ask | 58 | 69 | 3.8 |
| pmus ask | 43 | 28 | 2.8 |
| pmus bid | 30 | 26 | 1.9 |
| **total** | **220** | 212 | **14.3** |

25/46 weather markets (54%) show ≥1 definite crossing. Denominator caveat: "observed market-day"
spans only transition-active periods (the series is blind between episodes) — it is the only honest
denominator this data supports.

Rest-at-OPEN (anchor = each hygienic weather OPEN; hygiene skips counted: 51 post-censor-window,
39 mid-divergence>20¢, 37 incomplete touches, 28 stale-book>600s): visible-fill lower bounds
**40–77%** for Kalshi-resting legs (n=491–499 anchors each), 29–55% for pmus-resting; strict
(censor-stopped) variants 4–32%. Median visible time-to-crossing 14 min (K-ask improve) to 2.8 h
(pmus) — dt **upper-bounds** true time-to-fill for the fills we can see.

## 3. Adverse-selection bound → net EV per contract (hedge priced taker at the same dual-venue snapshot where the crossing first becomes visible)

| config (join; n anchors) | fill LB | slip med | net\|fill med | per-attempt | with cancel-after-1h: per-attempt |
|---|--:|--:|--:|--:|--:|
| rest **K bid**, hedge NO@P (n=499) | 76% | 2¢ | +0.82¢ | **+0.68¢** | +0.23¢ (44% fill) |
| rest **K ask**, hedge YES@P (n=491) | 40% | 2¢ | +0.82¢ | −0.21¢ | **+0.34¢** (26% fill) |
| rest **P ask**, hedge YES@K (n=482) | 53% | 15¢ | −9.40¢ | −5.04¢ | −1.09¢ |
| rest **P bid**, hedge NO@K (n=480) | 29% | 16¢ | −12.42¢ | −4.06¢ | −0.74¢ |

(`improve` = 1 tick inside differs by <0.2¢ throughout — sampling can't resolve 1-tick queue
priority.) The uncapped K-ask mean is dragged negative by a few multi-hour rests filling near
bucket-death; the 1h cancel cap removes them — but that cap was chosen **after seeing the tails**:
a policy illustration, not a pre-registered rule ([L19]; the ladder data is where it gets
confirmed or killed).

**Why the asymmetry is structural:** Kalshi weather books are the active side — crossings there
happen on small oscillations while the sticky, wide pmus hedge barely moves (slip 2¢). pmus books
are thin and sticky — a passive fill there means the world moved through a wide spread (bucket
collapsing), and Kalshi has already repriced 15¢ before the fill is even visible.

## Caveats (all binding)

1. **Bounds, not measurements**: fill rates are lower bounds; slippage is sampled at first
   visibility (true fill was earlier in the gap; direction of that bias unknown, adverse-inclusive
   by construction).
2. **No queue model**: definite fills require the level fully crossed (conservative); `touch` fills
   excluded; rest-size capacity (how many contracts fill at the level) is completely unmeasured.
3. **Activity-biased sampling** + ~1.8 days + one weather cluster: magnitudes are method-demo grade.
4. Pre-0013 stamps add ≤1.5s noise to dt (92% of obs are post-0013 anyway; price levels unaffected).
5. The prompt-supplied 0.39¢ median taker-net comparator differs from this archive's 0.26¢ OPEN
   median (n=1,117) — different cohort definitions; both are well below the 1h-capped K-rest
   net|fill medians (+0.8 to +2.2¢).

## Artifacts

- `scripts/maker_feasibility.py` (new; `--selftest` offline; `--post-only` / `--data-dir` flags)
- `scripts/_data/maker_feasibility_summary.json` (numeric dump, this run)
- `tasks/_agent_bus/20260611-probes/ladder-logging-spec.md` (what makes this a measurement)
