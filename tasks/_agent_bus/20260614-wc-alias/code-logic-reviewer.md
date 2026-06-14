---
from: coding-agent (self-review pass)
run_id: 20260614-wc-alias
timestamp: 2026-06-14T06:31:00Z
scope_reviewed: [bot/colisted_map.py:50-227, bot/colisted_map.py:515-548, bot/colisted_map.py:655-728, bot-rs/src/discovery.rs:128-180, bot-rs/src/discovery.rs:561-606, bot-rs/src/discovery.rs:760-895, bot-rs/src/main.rs:981-986]
critical_count: 0
warn_count: 0
info_count: 2
launch_recommendation: PROCEED
self_review: true
cross_references: []
---

## Goal Understanding
Make World Cup country-code binding robust to ALL venue code-mismatches (current + future knockout rounds) by adding a normalized-full-name fallback behind the exact-3-letter-code primary, removing the brittle hardcoded 3-entry alias table, with EXACT normalized-name equality (no fuzzy/substring) so two distinct countries can never bind, and logging any code+name double-miss as `[wc-unbound]` so a miss is never silent. Same behavior in the Python research path and the Rust live bot.

## Scope Reviewed
- bot/colisted_map.py — `_norm_country`, `pm_team_name`, `_wc_resolve_codes`, `soccer3_emit` (now returns `(records, unbound)` + takes `knames`), WC discovery loop (parallel name map + `[wc-unbound]` stderr), report field, self-test.
- bot-rs/src/discovery.rs — `norm_country`+`fold_accent`, `pm_wc_team_name`, `wc_resolve_codes`, `Discovery.soccer_unbound`, WC assemble loop (parallel `by_name`/`kbydate_names`), tests.
- bot-rs/src/main.rs — `report_coverage` `[wc-unbound]` line.
- IN-SCOPE confirmed: discovery-only; the 2-leg core and per-outcome binary emission were not touched (verified by smoke test + the untouched signal/risk/exec tests still green).

## Findings

### CRITICAL (must fix before launch)
- None.

### WARN (fix or justify)
- None.

### INFO (optional improvements / simplifications)
- Python `_wc_resolve_codes` rebuilds the `sufs`/`name2suf` maps once per GAME rather than once per date (O(games×events) vs O(events)). Negligible at WC scale (~59 games); per-game locality keeps it readable. Not changed.
- The accent fold in Rust (`fold_accent`) is a hand-rolled Latin-1 table rather than a unicode-normalization crate. Justified: the project deliberately keeps a minimal dependency surface (Cargo.toml note), the table covers the entire realistic country-name universe, and it was proven byte-equal to Python's NFKD on all 54 real names. Adding a crate for an accent case that today binds by exact code would be over-engineering.

## Checks Passed
- L1 NO FALSE JOIN — the core invariant. Empirically verified on the real captured board: all 48 live WC countries normalize to 48 DISTINCT keys on EACH venue (0 collisions, incl. accent-folded). The match is exact normalized equality; no substring/prefix/fuzzy. The feared "South/North Korea" over-collapse is explicitly tested (`Korea Republic` ≠ `Korea DPR`, `South Korea` ≠ `North Korea`) and there is only one Korea in this tournament anyway.
- PRIMARY-PATH PRESERVED — proved that all 9 games whose name STRINGS differ across venues (bih/civ/cpv/tur/usa…) bind via the exact-code path; their names are never consulted. A name-only matcher would have falsely failed them. Two-tier (code-first) is the correct and necessary design.
- FUTURE-PROOFING — a game that binds by NEITHER exact code NOR exact name is left UNBOUND (emits nothing) and logged `[wc-unbound]` with the slug + unmatched names; reported in `report["soccer3_UNBOUND"]` (Py) / `Discovery.soccer_unbound` (Rust). Better a logged miss than a false join.
- FIELD PATHS verified against actual payloads, not assumed: pmus `marketSides[*].team.name`, Kalshi `yes_sub_title`; `-draw` pmus markets carry `team: null` → empty name → never name-matched (draw binds structurally via the `tie` suffix).
- L23 honored — discovery only maps slug↔ticker; it reads NO prices. The pmus YES read stays the live WS book's per-slug YES orientation (unchanged). No `outcomePrices[0]` index read introduced.
- EDGE CASES — `_norm_country(None)`→`""` and `_norm_country("  ")`→`""` (non-matchable; caught by self-test, fixed). Incomplete game (2/3 siblings) → skip, NOT reported unbound. Kalshi event missing TIE → no bind → reported unbound. `name2suf` first-wins (`setdefault`) is safe: a country plays at most once/day so no within-date name collision.
- PYTHON↔RUST EQUIVALENCE — `norm_country` == `_norm_country` on all 54 real names (incl. `Côte d'Ivoire`→`cotedivoire`, `Türkiye`→`turkiye`); both implementations bind all 59 games / 177 per-outcome pairs / 0 unbound on the real captured catalog (throwaway tests, since removed).
- DETERMINISM — Rust `kbydate` and `kbydate_names` are pushed in the same sorted-event-ticker order so they stay index-aligned for the `used`-set; verified the `by_event.remove`/`by_name.remove` always succeed (both populated together).
- BINARY emission unchanged — every WC pair is `kalshi_b=None`, `cat=Sports`, `soccer=true`; no 3-leg/basket logic reintroduced (asserted in tests + smoke).
- BUILD/LINT/TESTS — Python 20/20 selftest GREEN; Rust 136 tests pass; clippy --all-targets clean; smoke fires the WC arb dry-run.

## Launch Recommendation
PROCEED. The change is discovery-only, L1-safe by construction and by empirical verification on the live WC board, behavior-equivalent across both implementations, and strictly more robust than the alias table it replaces.

## Self-review caveat
This is my own diff; authorship bias applies. The L1 conclusion rests on the captured 2026-06-13/14 WC board — if knockout-round countries introduce a name that normalizes into another's key (none do today), the `[wc-unbound]` log is the safety net, not a silent false join. The change does not feed a signed preregistration (it is live-bot discovery), so a follow-up independent review is optional, but the owner may want one before scaling WC size per the staged-rollout protocol.
