---
from: coding-agent
run_id: settlement-identity-tail-20260613
timestamp: 2026-06-14T02:30:00Z
task_summary: Add a 4th TAIL verdict (quantified ¢/contract cost) to settlement_identity.py, reserve DIVERGENT for structural conflicts, make sports result-timing sport-appropriate, and discover outcome-count from market structure.
files_changed: [scripts/settlement_identity.py]
files_created: []
context_loaded: [CLAUDE.md, memory/MEMORY.md, agent-memory/coding-agent/MEMORY.md, agent-memory/coding-agent/pattern_complement_needs_regime_sign.md, scripts/settlement_identity.py, scripts/capital_sim.py, scripts/cli_revisions.py, scripts/selftest_all.py, bot/colisted_map.py, scripts/verify_settlement.py, tasks/lessons.md (L21/L23), decisions/0010-all-in-edge-filtering-and-cost-model.md]
libraries_consulted: []
self_review_artifact: tasks/_agent_bus/settlement-identity-tail-20260613/code-logic-reviewer.md
---

## What I changed
- New status `TAIL` + `tail_cost_cents` (float ¢/contract) in every verdict dict; precedence now `DIVERGENT > NEEDS_MANUAL > TAIL > IDENTICAL` (`_combine` rewritten; statuses entries may be `(TAIL, cost)` tuples) — `settlement_identity.py:508`.
- Sports void/postpone WINDOW-or-FALLBACK difference: was DIVERGENT, now **TAIL** with `tail_cost_cents = 100 * capital_sim.void_haircut(pm_slug, 1.0)` (0010's model; 0.26¢ MLB / 0.10¢ other `aec-`). If the model returns 0 for the slug (e.g. soccer `atc-`, not yet modelled) the tail is un-priced -> stays NEEDS_MANUAL, never a degenerate 0¢ TAIL — `settlement_identity.py:282`.
- Sports result-timing made SPORT-APPROPRIATE: scored only for draw-capable leagues (`_DRAW_LEAGUES={fwc,fifa,mls}` via `pmlg`, or a draw discovered from structure). Definite-result sports (mlb/nba/nhl/wnba/...) get `result_timing.applicable=False` and append NO status — fixes the spurious NEEDS_MANUAL the owner caught — `settlement_identity.py:265`.
- Outcome-count DISCOVERED FROM STRUCTURE (replaces the prose regex): pmus = distinct settleable sides in `marketSides` (L23-authoritative; fall back to `outcomes` length); Kalshi = distinct bound outcome-markets (a Tie/Draw `yes_sub_title` in the set => 3-way). Provable 2-vs-3-way mismatch -> DIVERGENT (structural) — `_pm_settleable_sides`/`_kalshi_settleable_sides`.
- Weather CLI-vs-METAR: split the old `_NONCLI_WX` into `_METAR_ASOS` (same-station raw obs -> **TAIL**, cost from `cli_revisions.downward_rate*100` floored at a 0.05¢ prior) and `_NONNWS_WX` (Wunderground/non-NWS -> DIVERGENT) — `settlement_identity.py:155` + weather dim (b).
- `--audit` prints the 4-category breakdown (IDENTICAL/TAIL/DIVERGENT/NEEDS_MANUAL) plus a TAIL-pairs list with each pair's ¢/contract cost.
- `--selftest` updated: void-window MLB -> TAIL@0.26¢, CLI-vs-METAR -> TAIL, CLI-vs-nonNWS -> DIVERGENT, 2-vs-3-way discovered -> DIVERGENT, MLB timing N/A (no spurious NEEDS_MANUAL), precedence TAIL+NEEDS_MANUAL -> NEEDS_MANUAL. Kept the no-false-IDENTICAL + ESPN-vs-FIFA-IDENTICAL cases.

## Why (non-obvious only)
- `void_haircut` parses the pmus slug and returns a $-FRACTION only for `aec-`-prefixed markets; soccer uses `atc-` and returns 0.0. The `vcost > 0` guard is therefore load-bearing: it prevents a soccer void-difference from emitting a "tradeable iff edge > 0¢" (always-tradeable) TAIL and instead abstains (NEEDS_MANUAL).
- The CLI-revision tail is FLOORED at the prior: an empirically-observed 0% downward-rate is an upper-bound-not-yet-violated (cli_revisions is explicit a low rate is "reassuring", not zero-risk), so a 0¢ TAIL would defeat the flag's purpose.

## Verification
- `python scripts/settlement_identity.py --selftest` -> OK
- `python scripts/settlement_identity.py --audit` -> weather 24 IDENTICAL/36 NEEDS_MANUAL; sports 52 TAIL (21 MLB@0.26¢ + 31 ATP/WTA/UFC@0.10¢)/53 NEEDS_MANUAL; econ 13 IDENTICAL; **0 DIVERGENT** (none structural in the live universe).
- `python scripts/selftest_all.py` -> 20/20 ALL GREEN.

## Docs cited
- decision 0010 (all-in-cost / void EV) → MLB ~0.26¢, other sports ~0.10¢ → confirms the `×100` ¢ units.
- lessons L23 → marketSides is the authoritative settleable-side encoding (used as pmus outcome-count primary).
