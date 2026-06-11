# Probes #8 + #9 — recycle-time reconsideration arm / city-date cluster exposure cap

**EXPLORATORY — outside the 0014 frozen protocol; method demo on ~1.81 d of data (post-epoch slice
~0.66 d); not a result until re-run on multi-week data.** Nothing here amends H1/H2, their
constants, or their gates; no frozen script (`account_sim`, `alloc_policy_experiment`,
`clip_threshold_test`, `capital_sim`, `analyze_persistence`, `bot/*`) was modified. All machinery
lives in the new `scripts/recycle_arm_experiment.py` (selftested), which **imports** the shared
economics (`capturable`/`one_per_market`/`settle_t`/`void_haircut`, `_cost_per`/`_profit_per`) and
reproduces `run_policy("fifo")` / `("reservation", tau)` exactly when its levers are off
(equivalence asserted in `--selftest`, including the in-window settle→recycle-cash clock).

Data: `Kalshi/data/cross-arb/` pulled this hour; loader-corrected (econ-quarantined, lag-corrected,
restart/reconnect/resync censored). Full span 2026-06-09 05:27 → 2026-06-11 00:59 UTC (1.81 d,
279 capturable one-per-market candidates); post-0013-epoch slice t ≥ 1781082189 (0.66 d, 257).
Frozen-H1 reference constants replayed as-is: τ=2¢ booked, caps w/s/e = 20/10/5%, $500, clip 1000,
offset 28 h, void_mult 1, `capturable(edge_min=0, window_min=0, liq_floor=1, max_age=off,
drop_restart=True, drop_flat=False)`.

## Verdict

- **#8 recycle arm: UNTESTABLE on this window — the reconsideration set is empty (n=0), and the
  structure says it stays ≈empty even with more data.** ΔPnL arm-vs-baseline = **$0.00 in every
  cell** (FIFO and H1, full and post-epoch). Not "recycle adds nothing" — "recycle had literally
  nothing to reconsider," for two independent reasons measured below. Deprioritize; re-check is
  free (one command) when the multi-week prereg dataset lands.
- **#9 cluster exposure: concentration under frozen H1 is LOW today; a cluster cap at or above the
  weather pair cap (≥20%) would have bound 0/13 fills.** Max same-cluster share = **20.0%** of
  bankroll (a *single* pair at its own 20% weather cap — not stacking); the only true stack was
  2 positions = **17.4%** in `tc-temp-sfohigh-2026-06-10`. The 10% cap row costs paper PnL
  (−13.6%) for tighter concentration; 20%/30% cost zero because they never bind. **Risk-control,
  not PnL — the curve confirms the L19 expectation.**

## #8 — recycle-time reconsideration arm (n per cell)

| run (full 1.81 d) | funded | settled in-window | recycle events | events w/ ≥1 still-open skipped cand | recycle-funded | ΔPnL vs baseline |
|---|---|---|---|---|---|---|
| FIFO (τ=0, no caps) | 1 | 0 | **0** | 0 | 0 ($0.00) | $0.00 |
| H1 (τ=2¢ + cat caps) | 13 | 1 | **1** | **0** | 0 ($0.00) | $0.00 |
| post-epoch FIFO (0.66 d) | 12 | 1 | 1 | 0 | 0 | $0.00 |
| post-epoch H1 | 9 | 1 | 1 | 0 | 0 | $0.00 |

Free-capture size at recycle moments: **$0.00, 0 candidates** — at the single in-window settlement
timestamp no previously-skipped candidate's episode was still open. (`--recycle-edge open|twa`
basis was therefore never exercised; outputs identical.)

**Why the set is empty — two independent structural reasons, both measured:**

1. **Under H1 the bankroll never binds.** 266/279 candidates die at the τ=2¢ floor;
   **capital-skips = 0** (peak deployed ≈ $430 of $500, cash floor ≈ $120). The recycle arm only
   has work when affordability, not τ, is the binding constraint — today it never is at $500.
   (Under FIFO capital binds hard — 278 skips — but FIFO's first deep clip locks past the window
   end: 0 settlements to recycle from. The two preconditions anti-co-occur.)
2. **Skipped episodes don't live long enough to be re-fundable.** The FIFO capital-skipped pool
   (n=278, the maximal recycle pool): episode duration median **1 s**, p90 66 s, p99 610 s, max
   **2,205 s (~37 min)** — **0% reach 1 h**, while skip→settlement gaps are hours-to-days
   (settlements cluster at event-date+28 h). A same-episode re-entry must bridge that gap; on
   these durations the expected overlap is ≈0 *even with multi-week data*. What does survive
   until a settlement is a market **re-opening later** — and a re-opened arb re-enters the
   arrival stream anyway (it is a new OPEN the rule already considers; `one_per_market` collapses
   it in this backtest, conservatively).

**Implication (exploratory):** the as-specced arm (re-score still-open episodes at settle time) is
structurally starved; the only version with potential mass is "treat later re-openings as fresh
candidates when cash is free," which is the *existing* arrival semantics plus capital — i.e., the
real lever remains capital velocity, consistent with the 0012/velocity findings. Would revive #8:
multi-hour still-open episodes co-occurring with a capital-bound book (check via this script on
the multi-week pull; runs in ~30 s).

## #9 — event-cluster concentration under frozen H1 + opt-in cap

Cluster key = slug truncated after its event date → weather (city, date); sports = the game
(doubleheaders collapse — intended: same-day same-teams = correlated postpone risk); econ = the
release/meeting. Implemented only in this script.

**Measurement (H1 replay, NO cap, full):** 13 positions across 12 clusters. Per-cluster peak
concurrent share of $500: median 5.6%, p75 9.9%, **max 20.0%** (`tc-temp-laxhigh-2026-06-11`,
npos=1 — a single pair at its own 20% pair cap). Only multi-position cluster:
`tc-temp-sfohigh-2026-06-10` (2 buckets, $86.91 = **17.4%**). Clusters ≥10%: 2; ≥20%: 0 (the 20.0%
is the boundary single pair); ≥30%: 0. By category: weather max 20.0%, sports max 10.0% (each = its
pair cap). Post-epoch slice: 9 positions / 9 clusters, max 20.0%, no stacking. So **today the
correlated-exposure problem H1 leaves open is bounded by ~2× the pair cap in the one stacking
instance observed (17.4% actual)** — category caps already do most of the clustering work because
same-cluster bucket arbs rarely co-qualify at τ=2¢.

**Cap sweep (curve — no single row is "the result"; rejects/trims are positive-edge trades
refused, so per L15 this stays opt-in, default OFF):**

| cluster cap | binds (trims+rejects)/considered | max cluster share | total PnL | ΔPnL vs no-cap |
|---|---|---|---|---|
| off | — | 20.0% | $15.25 | — |
| 10% | 3/13 (2 trims, 1 reject) | 10.0% | $13.18 | **−$2.07 (−13.6%)** |
| 20% | 0/13 | 20.0% | $15.25 | $0.00 |
| 30% | 0/13 | 20.0% | $15.25 | $0.00 |

(Post-epoch: 10% binds 1/9, −$1.18 (−8.4%); 20%/30% bind 0/9, ΔPnL $0.00.) Reading per L19: the
curve shows **zero PnL effect at non-binding levels and strictly negative when binding** — a
cluster cap is insurance against a not-yet-observed stacking day, not a return lever. A cap below
the pair cap (10%) mostly re-implements a smaller pair cap (it trims singleton clusters).

## Data-quality flag (feeds the prereg §4 W5d phantom checklist; NOT a gate change)

**8/13 of the frozen-gate H1 fills ($235 of $430 deployed) carry the [L20] flat-ladder fingerprint
(`open_flat`, c2==c1==c0) at open** — including the 20.0% max-cluster position
(`tc-temp-laxhigh-2026-06-11-gte74lt75f`, dur 0.4 s) and the **only in-window settlement**
(`aec-atp-ottvir-kammaj-2026-06-08`: a 2-day-old ATP match still flickering sub-second 1–6¢
"arbs" with flat 522-deep ladders on 06-10; its $1.42 "realized PnL" reaches the books via the
settle-proxy open+1h floor). Under the opt-in `--drop-flat` lens: H1 funds **5** (not 13), max
cluster share **13.3%**, in-window settlements **0** — i.e., the dataset's entire realized PnL and
its single recycle event are flat-fingerprinted. Frozen gates keep `drop_flat=False` (correctly,
per prereg/L15); reported here as the alongside-diagnostic the prereg requires. Both probe verdicts
are **unchanged or strengthened** under the lens (recycle n: 1→0; concentration: lower).

## Artifacts / repro

- Script (new, only file touched outside this dir + `_data`): `scripts/recycle_arm_experiment.py`
  — `--selftest` (cluster keys; run_policy equivalence incl. recycle clock; arm funds still-open
  skip / excludes closed; τ-re-gate under twa basis; cat caps 20/10/5; cluster cap reject/trim/
  clear + PnL monotonicity) **passed**; frozen scripts' selftests re-run green, `git status` clean
  on all frozen files.
- Numeric outputs (gitignored `_data`): `scripts/_data/recycle_cluster_probe-20260611.json`,
  `...-dropflat.json`, full console reports `...-report.txt`, `...-dropflat-report.txt`.
- Repro: `python scripts/recycle_arm_experiment.py` (add `--drop-flat` for the lens,
  `--cluster-cap 0.2` for the single-knob run, `--recycle-edge twa` for the decay-aware basis).
