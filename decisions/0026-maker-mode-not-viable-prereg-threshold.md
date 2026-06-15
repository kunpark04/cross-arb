# 0026 — maker mode NOT_VIABLE on current data; pre-registered forward go-threshold

- **Date:** 2026-06-15
- **Status:** Accepted (do NOT build the live maker mode now; the rig + threshold are pre-registered for re-measurement as data accumulates)
- **Deciders:** owner (chose measure-first over build); Claude (scoped the design + built `scripts/p_hedge_measure.py`); stats-ml-logic-reviewer (caught the clock-skew LOOK-AHEAD bug that had flipped the sign)

## Context

The owner asked to build a MAKER execution mode — rest a Kalshi-weather GTC limit (the only +EV maker config:
$0 `quadratic` maker fee) and taker-hedge on pmus when it fills — the largest queued PnL lever. An adversarial
scoping (workflow `wt2hasp04`) returned **NOT_VIABLE a-priori**: resting on Kalshi forces the Kalshi leg to fill
FIRST, then reach across for the pmus taker hedge; but pmus is the thin binding leg (no resting volume in 72/94
live taker fires, ~77%), and the maker fills on the SAME informed flow that empties pmus → the hedge misses →
naked Kalshi leg → recovery loss. This structurally INVERTS the bot's one safety property ([0025] dynamic
fire-order: fire the thinner leg first so a pmus-miss is a clean $0 abort). The advertised +0.14–0.44¢/attempt
priced only the hedge SLIPPAGE on fills that DID hedge — it never priced the hedge-MISS branch. The decision
turns entirely on `p_hedge = P(a profitable+available pmus hedge exists | a Kalshi-weather maker just FILLED)`,
which was unmeasured for the maker direction. The owner chose to MEASURE it from the already-running WAVE-2 logs
(`trades-`/`ladders-`, live since 2026-06-11) before committing real money — no live orders placed.

## Decision

**Do NOT build the live maker mode now.** `scripts/p_hedge_measure.py` (read-only, ~5 days weather, trades+
ladders) measures, clock-corrected:

| mode | p_hedge (CI) | breakeven | EV/fill (CI) | P(EV>0) |
|---|---|---|---|---|
| join | 36.1% [33, 39] | 54.4% | −0.82¢ [−1.17, −0.45] | 0% |
| improve | 46.4% [43, 50] | 53.0% | −0.25¢ [−0.57, +0.08] | 11% |

Both modes' clustered-bootstrap p_hedge CI sit **BELOW breakeven**, EV negative — empirically confirming the
a-priori NOT_VIABLE. The maker captures the fee edge but loses more on the ~54–64% of fills that go naked.

**Pre-registered forward go-threshold (0014-style, frozen NOW, before the multi-week data):** the live maker
build is authorized ONLY if, on ≥ 6 independent (city,date) clusters of fresh data, the **clustered-bootstrap
p_hedge CI LOWER bound clears the sample's breakeven p_hedge** (currently ~53%) in the conservative `join` mode
at the 1-contract clip, AND mean EV/fill > +0.5¢. Re-run `p_hedge_measure.py` as logs accumulate; **do not tune
the rig to clear the gate** ([L19]). The design is fully scoped and cheap (~1 day: the resting-GTC flip is ~1
line in `build_kalshi_payload`; cancel/recovery/exposure reuse exists — workflow `wt2hasp04`) if it ever clears.

## Alternatives considered

- **Build the live maker at 1-contract (the owner's initial "do it").** REJECTED after measure-first: the
  corrected measurement is −EV across both modes and every floor 0–2¢; building would bet an edge the data
  falsifies — the exact failure [0015]'s protest-of-record warns against.
- **Trust the FIRST (buggy) measurement (+EV, p_hedge 52–64%).** REJECTED: stats-ml-logic-reviewer found a
  clock-skew LOOK-AHEAD — the hedge ladder was matched to the venue fill time `vt`, but ladders are stamped on
  the monitor-receive clock `t` (a structured ~150 s skew), so the hedge was priced against the PRE-fill pmus
  book. Re-pricing on the consistent clock (`t`) dropped p_hedge ~15 pts and flipped EV negative. [L44]
- **Full maker-maker (rest both legs).** REJECTED earlier (probe): 32% one-leg-naked, ~2.2 h unhedged windows.

## Consequences

- The largest queued maker PnL lever is **shelved, not killed** — re-measurable for FREE as the logs accumulate;
  no capital, no deploy, no code in the live bot.
- The clock-consistency fix + the (city,date)-clustered bootstrap are now in the rig; the numeric record is
  `scripts/_data/p_hedge_measure.json`; the full audit is `tasks/_agent_bus/20260615-2207/stats-ml-logic-reviewer.md`.
- Scaling stays where the live data points: **breadth + the taker model** (which keeps the dynamic-fire-order
  safety the maker structurally inverts), per [0025] and `research/probe-program-2026-06-11.md` §1.
