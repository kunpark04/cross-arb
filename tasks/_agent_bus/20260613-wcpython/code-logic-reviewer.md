---
from: coding-agent (self-review pass)
run_id: 20260613-wcpython
timestamp: 2026-06-14T03:43:14Z
scope_reviewed: [bot/colisted_map.py:39-56, bot/colisted_map.py:104-184, bot/colisted_map.py:412-431, bot/colisted_map.py:_selftest soccer3, scripts/capital_sim.py:106-135, scripts/settlement_identity.py:460-475 (_kalshi_three_way), scripts/settlement_identity.py:426-450 (_pm_outcomes_list/_pm_settleable_sides), scripts/settlement_identity.py:_selftest WC fixtures, scripts/settlement_identity.py:_audit soccer3]
critical_count: 2
warn_count: 1
info_count: 2
launch_recommendation: PROCEED
self_review: true
cross_references: []
---

## Goal Understanding
Make the FIFA World Cup tradeable on the Python (research + settlement-gate) side by modeling each WC outcome
(teamA / teamB / draw) as its OWN binary co-listed pair — exactly the weather/econ 1:1 model — NOT a 3-leg
basket and NOT the 2-team complementary game model (which would leave the draw unhedged). Discovery emits
3 per-outcome binary records/game; capital_sim prices the WC void tail; the settlement gate must read each
per-outcome pair as 2-way-vs-2-way and reach TAIL (not false-DIVERGENT, not false-IDENTICAL, not NEEDS_MANUAL).

## Scope Reviewed
- `bot/colisted_map.py` — SOCCER3 map + 3-entry alias; `pm_yes_price` (L23); `soc_parts`; `soccer3_emit` (pure bind); discovery branch + report/return; offline selftest. All additive; moneyline `LEAGUES` path untouched.
- `scripts/capital_sim.py` — `void_haircut` `atc-fwc` branch (0.08¢); selftest.
- `scripts/settlement_identity.py` — WC `--selftest` fixtures; `_audit` soccer3 iteration; `_kalshi_three_way` single-market guard; `_pm_outcomes_list` JSON-string parse.

## Findings

### CRITICAL (must fix before launch)
- Lone Kalshi TIE market mis-read as 3-way → false DIVERGENT on every WC draw leg (self-resolved within run)
  - Location: `scripts/settlement_identity.py` `_kalshi_three_way` (was :460-471)
  - Issue: the outcome-count heuristic returned 3-way if ANY bound market's `yes_sub_title` matched `\b(tie|draw)\b`. The per-outcome WC model passes a SINGLE Kalshi ticker; when that ticker IS the TIE leg, the lone "Tie" sub-title tripped the 3rd-side inference → Kalshi read 3-way while pmus read 2-way → false DIVERGENT.
  - Why it matters: a false DIVERGENT forgoes a real, settlement-clean arb (1/3 of all WC legs — the draw on every game). The draw is a genuine tradeable outcome; killing it silently removes a third of WC capacity.
  - Fix: the tie-regex 3rd-side inference now requires `len(markets) >= 2` (a Tie market bound ALONGSIDE ≥1 other leg). A single bound market is one binary outcome by construction. The multi-market 2-vs-3-way DIVERGENT discipline is preserved (verified: the existing `pm_soc`/`k_2way_only` fixtures still pass).
  - Status: self-resolved within run.

- Live pmus `outcomes` is a JSON STRING, not a list → char-iteration mis-counts draw leg as 9-way → false DIVERGENT (self-resolved within run)
  - Location: `scripts/settlement_identity.py` `_pm_settleable_sides` (was :435), now via new `_pm_outcomes_list`
  - Issue: `_pm_settleable_sides` fell back to `len(set(outcomes))` when marketSides carry no labels (the WC draw market has `team:null`, no side title). But the LIVE pmus API returns `outcomes` as a JSON-encoded STRING `'["Yes","No"]'`; iterating the raw string yields individual CHARACTERS → 9 distinct → pmus read as 3-way (≥3) → false DIVERGENT on every draw leg even after the `_kalshi_three_way` fix. This is a pre-existing latent bug; the WC draw market is the FIRST co-listed pair that reaches the `outcomes` fallback (weather/econ don't use outcome-count; old sports moneyline always had `team.name` in marketSides). L23 family.
  - Why it matters: same consequence — false DIVERGENT silently forgoes every WC draw arb. Caught only because the live `--audit` showed 58 DIVERGENT all on `-draw` slugs (not by the offline selftest, whose fixture had used a parsed list — unrealistic).
  - Fix: `_pm_outcomes_list` tolerates both the JSON-string and parsed-list forms; the selftest draw fixture now uses the STRING form to lock the regression.
  - Status: self-resolved within run.

### WARN (fix or justify)
- soccer3 records are discovered + gated but NOT yet consumed by the monitor (`bot/monitor.py register()`)
  - Location: `bot/monitor.py:889-917` (register iterates weather/econ/sports, not soccer3)
  - Issue: `build_colisted_map()` now returns a `soccer3` key, but `register()` does not iterate it, so WC markets are not yet tracked by the persistence monitor.
  - Justification (deferred, in-scope): the approved plan §4 explicitly defers monitor registration to the gated droplet redeploy ("secondary to tradeability"). This task's scope is discovery + capital + the settlement gate. SAFETY VERIFIED: soccer3 records carry a single `kalshi` key (no `kalshi_a`/`kalshi_b`), so they CANNOT be accidentally consumed by the 2-team `GameTracker` sports loop — they are a distinct category the loop never reads. No crash, no mis-route. The only effect is "not tracked yet," which is the intended deferral. Flagged so the next session wires `register()` (a `MarketTracker` per outcome, grouped by `game`) before relying on WC persistence data.

### INFO (optional improvements / simplifications)
- `soccer3_emit` is intentionally extracted as a pure function (mirrors `pick_game`/`_match_game`) so the discovery bind is offline-testable AND the live branch has a single source of truth (avoids the `scan_all` "private copy drifted" hazard CLAUDE.md calls out). No action.
- The alias table is 3 entries (`irn/alg/hai`), not the 8 the design doc's prose implied — the 8 was a count of GAMES needing an alias; only 3 country CODES actually differ (partners `bel/egy/jor/mar` are exact on Kalshi). Verified 51 exact + 8 alias = 59/59 against the saved pulls. Adding self-map entries for the 4 exact partners would be dead code. No action.

## Checks Passed
- **No false positives in the join (L1)**: alias table is an explicit 3-entry map, NO fuzzy 3-letter matching; `soccer3_emit` reuses `pick_game` (exact-date + `used` doubleheader guard) and refuses an unknown/unaliased code (selftest asserts `[]`). Live: 0 Kalshi tickers double-bound, every game maps to exactly 3 distinct outcome tickers (58 games).
- **Per-outcome BINARY emission (not 3-way game)**: every record carries a SINGLE `kalshi` ticker, no `kalshi_b`, `cat:"soccer3"`; verified on all 174 live records. Routed through the binary single-market `_sports` gate path, NOT the 2-team `GameTracker`.
- **draw↔TIE mapping**: pmus `-draw` → Kalshi event `tie` ticker (`pl["tie"]`); selftest + live confirm (58 draw records, all bind the `-TIE` ticker, `team:None`).
- **L23 YES-read**: `pm_yes_price` reads the marketSides `description=="Yes"` side, never `outcomePrices[0]`; proven on the live No-first `-ger` market (`outcomes=["No","Yes"]`) where the array index read would take the wrong leg.
- **Gate reaches TAIL, never false-IDENTICAL**: live `--audit` soccer3 = 174 TAIL / 0 DIVERGENT / 0 NEEDS_MANUAL / 0 IDENTICAL. IDENTICAL is correctly NOT reached because the void FALLBACK differs (Kalshi "fair price" vs pmus "last-traded") — the no-false-IDENTICAL discipline (L1) holds; the difference is priced as the 0.08¢ tail, not asserted clean.
- **void_haircut units**: `void_haircut("atc-fwc-…")` returns a $-fraction 0.0008; `settlement_identity` multiplies by 100 → 0.08¢/contract (selftest asserts 0.05–0.10¢ band). Non-zero so the gate reaches TAIL (a 0¢ haircut would degenerate to "always tradeable"). Below MLB's 0.26¢ (firm tournament schedule), asserted.
- **Existing discipline preserved**: full `--selftest` still green (weather IDENTICAL/TAIL/DIVERGENT; sports ESPN-vs-FIFA IDENTICAL, MLB void TAIL 0.26¢, ET-timing & 2-vs-3-way DIVERGENT, MLB timing N/A, precedence). The `_kalshi_three_way` ≥2-market guard did NOT weaken the multi-market 2-vs-3-way DIVERGENT check.
- **Moneyline path untouched**: `LEAGUES` unchanged; SOCCER3/alias are separate maps; WC `drawable_outcome` markets never enter the moneyline `pmg` dict; `scan_all.py` imports unchanged and runs clean; coverage report's only gap is the pre-existing `boxing` (unrelated).
- **Suite**: `python scripts/selftest_all.py` = 20/20 GREEN.

## Launch Recommendation
PROCEED.
Both CRITICAL findings were caught (the second only via the live `--audit`, not the offline selftest) and self-resolved with regression fixtures locking the live data shapes; the gate now reaches TAIL on all 174 WC per-outcome pairs with zero false DIVERGENT/IDENTICAL.

## Self-review caveat
This is an authorship self-review; the two CRITICAL bugs slipped past my own offline fixtures and were only exposed by running the live `--audit` (the offline fixture had assumed a parsed-list `outcomes`, the unrealistic shape). That is a direct reminder that for this project the live audit is the real test of the gate, not the synthetic selftest. The change is research/gate-path only (not a signed-prereg launch), so independent review is not strictly required — but the deferred monitor `register()` wiring (WARN) and the `WC_POSTPONE_RATE=0.004` estimate (an unvalidated prior, like the other void rates) warrant a look before WC is traded with real size.
