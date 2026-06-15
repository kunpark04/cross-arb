---
from: coding-agent (self-review pass)
run_id: settlement-identity-tail-20260613
timestamp: 2026-06-14T02:35:00Z
scope_reviewed: [scripts/settlement_identity.py:155-310, scripts/settlement_identity.py:411-526 (_combine), scripts/settlement_identity.py:466-492 (_can_draw/_cli_metar_tail_cents), scripts/settlement_identity.py:560-707 (_selftest), scripts/settlement_identity.py:713-767 (_audit)]
critical_count: 0
warn_count: 2
info_count: 3
launch_recommendation: PROCEED
self_review: true
cross_references: [tasks/lessons.md (L21/L23), agent-memory/coding-agent/MEMORY.md, decisions/0010-all-in-edge-filtering-and-cost-model.md, decisions/0013-econ-grid-step-twin-and-measurement-integrity.md]
---

## Goal Understanding
Refine the load-bearing settlement-identity gate so a small priceable tail (the sports void/reschedule-window
difference; weather CLI-vs-METAR-same-station) returns a new TAIL verdict carrying a quantified ¢/contract cost,
while DIVERGENT is reserved for structural/un-priceable conflicts. Also fix two real bugs the owner caught:
(1) result-timing fired a spurious NEEDS_MANUAL on definite-result sports (mlb/nba/...); (2) outcome-count
must be discovered from market structure, not a prose regex.

## Scope Reviewed
- scripts/settlement_identity.py — the only file edited (verified `git status`; README.md + sessions.md were
  already modified before this run and were NOT touched here).

## Findings

### CRITICAL (must fix before launch)
- None. The four named focus checks pass (see Checks Passed).

### WARN (fix or justify)
- Kalshi outcome-count is incomplete for 3-way sports in --audit because colisted_map binds only the two TEAM
  legs (kalshi_a/kalshi_b), never the Tie/Draw market.
  - Location: scripts/settlement_identity.py:255 (`_kalshi_three_way(kms)`) + bot/colisted_map.py:340 (sports bind).
  - Issue: once soccer leagues (fwc/fifa/mls) are added to `LEAGUES`, every soccer pair will read Kalshi=2-way
    (only 2 team legs bound) vs pmus=3-way -> a DIVERGENT on outcome-count for EVERY soccer pair.
  - Why it matters: it would block real soccer arbs. It is CONSERVATIVE-SAFE (a false DIVERGENT blocks, never a
    false IDENTICAL that green-lights a bad pair), and it is currently LATENT (soccer is not in `LEAGUES` yet, so
    0 soccer pairs in the live audit). It is also spec-compliant ("Kalshi count = distinct outcome markets BOUND")
    — the selftest's `k_soc` deliberately includes a Tie leg to represent the bound-set case.
  - Suggested fix (deferred, out of this task's scope): when soccer is enabled, have colisted_map also bind the
    Tie/Draw Kalshi market for the event so the gate sees the real 3rd side.
  - Status: reported (not fixed — colisted_map binding is outside the gate's scope and outside this task).

- capital_sim.void_haircut returns 0.0 for soccer (`atc-` prefix) — 0010's void model only recognizes `aec-`.
  - Location: scripts/capital_sim.py:110 (`if not m.startswith("aec-")`) consumed at settlement_identity.py:291.
  - Issue: a soccer void-window difference cannot be priced by 0010 today, so my code routes it to NEEDS_MANUAL
    ("0010's void model returns 0c for this slug") rather than a degenerate 0¢ TAIL.
  - Why it matters: correct + conservative TODAY (verified: `void_haircut('atc-fwc-x',1.0)*100 == 0.0`), but when
    soccer is enabled the void tail should become priceable, not abstain. The handling is honest (it names the
    reason in the verdict) but `void_haircut` should be extended to model `atc-` soccer postpone/void.
  - Suggested fix (deferred): extend `capital_sim.void_haircut` to recognize the soccer prefix + a soccer
    postpone/void rate, mirroring the MLB term.
  - Status: reported; the gate behaves correctly with the current model (abstains rather than mis-prices).

### INFO (optional improvements / simplifications)
- Void fallback overlap is treated as a match (partial-overlap `k_fb & p_fb` truthy -> IDENTICAL). Pre-existing
  behavior, not introduced here; only relevant if a Kalshi clause matches two fallback patterns at once.
- scripts/README.md (pre-existing uncommitted edit) still describes the OLD 3-status signature and "sports 51
  DIVERGENT". It now contradicts the code. Out of this task's scope (tests + verify, not docs) — flag for the
  owner / `/update-managerial-docs`, do not silently entangle with its unrelated pending edits.
- `_cli_metar_tail_cents` reads live `_data/cli.jsonl` at call time. Pure read, cheap, and wrapped in
  try/except -> falls back to the prior on any error. Fine; noting the live-data dependency for transparency.

## Checks Passed
- (a) NO false IDENTICAL: IDENTICAL requires EVERY scored dim provably IDENTICAL with no TAIL/DIVERGENT/
  NEEDS_MANUAL (`_combine` final `else`); the ESPN-vs-FIFA IDENTICAL case and the no-station-NEEDS_MANUAL case
  both still pass; the new structural-mismatch directions all push AWAY from IDENTICAL (toward DIVERGENT/
  NEEDS_MANUAL), so the change cannot manufacture a false IDENTICAL.
- (b) PRECEDENCE: `_combine` checks DIVERGENT first (structural conflict decisive, never downgraded to TAIL),
  then NEEDS_MANUAL (unknown could be structural; outranks TAIL), then TAIL, then IDENTICAL. Proven by the
  precedence selftest (TAIL+NEEDS_MANUAL -> NEEDS_MANUAL) and the ET-timing/2v3-way DIVERGENT cases.
- (c) void_haircut UNITS = ¢: `100 * void_haircut(slug,1.0)` -> 0.26¢ MLB / 0.10¢ ATP verified at runtime and
  in the live --audit; cross-checked against decision 0010's stated "~0.26c MLB / ~0.10c other".
- (d) outcome-count DISCOVERED from structure: pmus reads `marketSides`/`outcomes` (no prose regex for the
  count); Kalshi counts distinct bound `yes_sub_title`s. The only regex is Tie/Draw detection on a bound
  market's own title (structure, not free rules prose). The baseball "tie->50-50" clause correctly reads 2-way.
- Edge cases: empty marketSides+outcomes -> None -> NEEDS_MANUAL; empty fallback set -> NEEDS_MANUAL; `differs`
  evaluates to a bool in all branches (traced); soccer atc- 0¢ tail -> guarded to NEEDS_MANUAL.
- No import cycle (settlement_identity -> capital_sim -> analyze_persistence; none import back). selftest_all 20/20.
- Econ path behaviorally unchanged (only added `tail_cost_cents` to its early return); audit econ 13 IDENTICAL
  matches prior. L21 inequality semantics + L23 marketSides authority both respected.

## Launch Recommendation
PROCEED. No CRITICAL findings; the two WARNs are latent (soccer not yet enabled in `LEAGUES`), conservative-safe
(they block, never green-light), and out of this task's scope; the gate behaves correctly under the current
matcher + cost model.

## Self-review caveat
This is a self-review of code I just authored — authorship bias applies. The change feeds invariant #1, the
gate the live bot consults before sizing a "locked" pair; before soccer leagues are enabled in colisted_map (the
trigger for both WARNs), an independent review of the Kalshi-3-way binding + a `void_haircut` extension for `atc-`
is warranted. For the categories live TODAY (weather/mlb/tennis/ufc/econ) the behavior is verified end-to-end.
