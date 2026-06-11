# Backtest — the bot's CURRENT strategy (its live gate stack)

**Date:** 2026-06-11 · **Posture:** READ-ONLY paper backtest, zero orders. **Method-demo, NOT validation.**

**What this is.** The prior "full backtest" (`research/backtest-2026-06-11.md`) ran the *raw* pipeline —
every positive-edge capturable arb, all categories, no new gates. This run replays the **same episode
data through the bot's ACTUAL pre-trade gate stack** as of now: `bot-rs/src/risk.rs::evaluate` with
`bot-rs/src/config.rs` `Config::from_env` defaults. It answers "what does the strategy the bot would run
*today* do, and how does it change the headline numbers?"

**Full untruncated report:** `scripts/_data/backtest_current_strategy_20260611.txt` (the main agent cats it).
**Script:** `scripts/backtest_current_strategy.py` (`--selftest` green; reuses `analyze_persistence`,
`capital_sim`, `account_sim`, `adverse_selection`, `capital_velocity` — never re-implements, [L20]).

**Data:** 48.4 h (2.02 d), 60,492 transitions, 86 censors. **Effective-n ≈ 1 event-day** (~91% of
candidate arbs carry 2026-06-10). Every figure PAPER/GROSS (fees+spread netted; latency / leg-fill /
slippage / adverse-selection / void NOT) — realized would be lower.

---

## Headline: the gate stack collapses 292 candidates → 9, all WEATHER

The single biggest fact is `require_settle_clean=true`: **only weather is empirically settlement-verified
today** (sports recon ~06-23, econ ~07-02), so the **current strategy is weather-only**. That plus the 2c
floor takes the cohort from 292 to 9.

### The gate funnel (risk.rs order, weather-only)

| gate | survivors | dropped | by category |
|---|---|---|---|
| 0 candidates (all +edge capturable, 1/mkt) | **292** | — | econ=2 sports=249 weather=41 |
| 1 settlement-identity | 41 | −251 | weather=41 |
| 3 mid-divergence (econ 15c / else 40c) | 41 | 0 | weather=41 |
| 4 edge-floor 2c (0014) | **9** | −32 | weather=9 |
| 4b dear-led-weather skip (H1) | 9 | 0 | weather=9 |
| 6 fat-edge sizing (≥6c → ×0.5) | 9 | 0 | weather=9 |
| 6 caps ($ walk) | **5** | −4 | weather=5 |

- **settlement** removes every sports/econ arb (−251). **edge-floor** is the dominant *weather* cut
  (41→9; median weather edge < 1c).
- **The H1 directional gate is DORMANT on this data**: of the 41 weather candidates the at-open class mix
  is `{unclassified: 40, cheap_made: 1}` — **0 dear-led**. ~All weather opens are unclassified (no prior
  px / censor-gapped → `led_by` unknown → KEPT by design). The gate needs the sub-2c + multi-day weather
  population to bite; at the ≥2c candidate level it has nothing to cut. (H1 itself is real where
  measurable — strategy-idea-tests Fisher p=2.7e-6, n=59 sub-2c weather opens — this is a *coverage* limit.)
- **fat-edge** is also inert: no ≥6c survivors in this cohort.

### Baseline vs Current (side by side)

| metric | BASELINE (all-cat, no new gates, clip 1000) | CURRENT (full gate stack, weather-only) |
|---|---|---|
| candidate arbs (1/mkt) | **292** | **9** |
| by category | econ=2 sports=249 weather=41 | weather=9 |
| median / mean edge | 0.51c / 1.05c | **3.51c / 3.34c** (floor lifts the median) |
| $500 entered (capital-bound) | 3 (sports=3) | 5 (weather=5) |
| REALIZED PnL | **+$1.97 (+0.39%)** | **+$0.10 (+0.02%)** |
| UNREALIZED PnL | +$0.39 (+0.08%) | +$0.00 |
| capital locked | $54.39 | $0.00 |
| **cap-wt lock-days (velocity)** | **15.0 d** | **1.2 d** |
| close-toxicity share | 47% (n=266) | 25% (n=4) |

The **$500 REALIZED at the bot's real caps is tiny by design** — `max_total_notional=$20`,
`max_contracts_per_pair=1` (staged-rollout floor) bind far below $500. The honest scale comparison is the
**unconstrained variant** below. The **one structural win is velocity: 15.0 d → 1.2 d** (weather settles
~1.2 d vs sports ~15 d) — capital recycles ~12× faster. The toxicity drop is the *weather focus*, not the
gate (which skipped 0); n=4 is too small to read.

### How it changes vs `research/backtest-2026-06-11.md`

That backtest: **292 candidates, ~85% sports, $500 → +$1.97 realized (+0.39%) / +0.86% mark-to-edge, ~15 d
sports-locked, ~67% capital frozen, toxicity unaddressed.** The current strategy:

- **Candidates 292 → 9 (−97%)**; mix flips ~85% sports → **100% weather**.
- **$500 realized +0.39% → +0.02%** at real caps (both noise at effective-n≈1; not the comparable scale).
- **Velocity 15.0 d → 1.2 d** (the clean win); **locked capital $54 → $0** (weather settles in-window-ish).
- **Toxicity 47% → 25%** — but that's the category shift, not the (dormant) directional gate.

### Variants (in the full report)

- **5a — if sports+econ were verified:** tradeable cohort 9 → **30** (sports=21 weather=9); cap-wt
  lock-days back up to **6.7 d** (sports/econ drag velocity). The directional gate still only touches
  weather; the econ-twin 15c bound now acts on the larger cohort. This is the shape once recon clears.
- **5b — unconstrained notional** (lift $-caps, keep floor+directional+fat-edge+clip): weather-only
  **8 entered, +1.96% unrealized**, but one deep clip eats ~$500 (the FIFO-binds problem persists).
  All-verified: 4 entered, +1.18%. Shape, not size — still paper/gross, effective-n≈1.
- **5c — directional gate on/off:** **no difference** (gate dormant; see above).

---

## Honest read

The gate stack is **structurally sound and does exactly what it should**: strips the strategy to the one
empirically-clean, capital-efficient universe (weather), applies the 0014 2c floor (the dominant cut), and
sizes tiny. The **fat-edge haircut + H1 directional gate are present but INERT** on this 2-day cohort. The
cost is **cohort size** — weather-only on ~1 effective event-day is **9 arbs**. This is a **measurement
rig** producing a small, directionally-sound, **NOT-yet-validated** signal; re-run on the multi-week
post-0013 data the 0014 protocol requires. **Not a go; not a magnitude to quote.**

**Artifacts:** `scripts/backtest_current_strategy.py` · `scripts/_data/backtest_current_strategy_20260611.txt`
