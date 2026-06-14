---
from: coding-agent (self-review pass)
run_id: 20260614-flip-add
timestamp: 2026-06-14T08:24:12Z
scope_reviewed: [scripts/flip_add_backtest.py — ADD path refinements per stats-ml-logic-reviewer audit (hold sweep, phantom lens, true-add/re-entry split, INFO fixes)]
critical_count: 0
warn_count: 0
info_count: 1
launch_recommendation: PROCEED
self_review: true
cross_references: [tasks/_agent_bus/20260614-flip-add/stats-ml-logic-reviewer.md, tasks/_agent_bus/20260614-flip-add/coding-agent.md (prior-run), tasks/lessons.md (L19/L20/L28)]
---

## Goal Understanding
Refine the already-committed read-only ADD/FLIP backtest so the $135 ADD figure is no longer quotable
bare. The audit (WARN-1/2/3 + INFO-1/2/3) wanted three disclosures wired into the report: a hold-window
sweep, the [L20] flat-ladder phantom lens applied to the add's driving WIDEN, and a TRUE-ADD-vs-RE-ENTRY
split classified against the REAL episode open/close (not a proxy). Reuse the proven harness; no fresh
cohort. This feeds a BUILD decision, not a live launch or signed prereg.

## Scope Reviewed
- scripts/flip_add_backtest.py:186-243 — `add_events` refactor (now returns `(out, dropped_flat)`; adds
  `drop_flat_widen` param + `_widen_is_flat` helper; explicit sort key; per-add `flat_widen`/`is_true_add`).
- scripts/flip_add_backtest.py:330-420 — report sections (3a) hold sweep, (3b) phantom lens, (3c)
  true-add/re-entry + both-lenses line; caveats reworded (INFO-1/INFO-2 + hold-conditional-range note).
- scripts/flip_add_backtest.py:440-470 — selftest: true-add vs re-entry on a synthetic case + phantom-lens
  drop on a flat-driving widen + non-flat survives.

## Findings

### CRITICAL — none.

### WARN — none. (All three audit WARNs are now disclosed in-report; the audit had 0 CRITICAL.)

### INFO (optional)
- The (3c) true-add/re-entry split is computed on the UNLENSED 348-add cohort (matching the audit's
  301/408 reference frame), while (3b) reports the lensed total. To avoid a reader cross-wiring the two,
  I added a "BOTH lenses" line (61 adds / $22.69) — the genuine scale-in that ALSO survives the phantom
  lens. This is the single most-honest number for the actual feature; kept the per-cohort views too so
  each audit point maps 1:1 to its section. No further action needed.

## Adversarial checks I actually ran (independent recompute, not trusting my own code)
- **Phantom lens fired correctly:** dropped markets == flat-driving markets EXACTLY (93 == 93); 0 clean
  adds wrongly dropped; 0 flat adds wrongly kept in the lensed cohort. BEFORE $135.31 → AFTER $98.69,
  delta $36.62 (27.1%) — reproduces the audit's $36.62/27.1% to the cent.
- **True-add classification correct against the REAL interval:** re-derived true/re-entry straight from
  raw OPEN/WIDEN records + `build_episodes` intervals (not via `add_events`) → 92 true / 256 re-entry,
  MATCHES the code. The base-episode match keys on `abs(open_t − ep.open_t) < 1e-6` against the actual
  episode close_t, not the settlement proxy.
- **TRUE-ADD is invariant across the hold sweep (92 at every offset 4h..48h)** while RE-ENTRY grows
  134→259. This is a stronger statement than the audit had: the hold-window LEVER the audit flagged moves
  ONLY re-entry; a genuine scale-in never depends on the settlement assumption. Surfaced as a sweep column.
- **Effective-n honestly tiny:** lensed add PnL spans 7 event-dates, ~4 (06-10/11/12/13) carry ~99%;
  top-1 market 9.0%, top-5 24.3% — not single-bet-dominated (consistent with the audit's not-an-oracle
  finding), but ~5 independent date-units, stated in the caveats.
- **INFO-3 (latent sort TypeError) is real:** reproducing `add_events` with a 5-tuple raised
  `TypeError: '<' not supported between instances of 'dict' and 'dict'` immediately when two widens shared
  a `t`. Fixed with explicit `key=lambda w: w[0]`.
- **[L28] field shapes re-verified against real records BEFORE coding:** WIDEN dir = sports KP/PK,
  weather/econ single-letter K/P; 33/39594 WIDENs have `depth=None` → `(r.get("depth") or {})` handles it;
  8848 WIDENs carry a flat `c2==c1==c0>0` ladder (the phantom population the lens targets).

## Checks Passed
- No oracle ([L19]): the lens uses the FIRST causal widen and DROPS it if it's a phantom — it does NOT
  substitute a later clean widen (that would be look-ahead picking the favourable event). The sweep is
  straight application across a fixed offset list, not in-sample parameter tuning.
- Phantom discipline matches the base cohort: `_widen_is_flat` uses the identical `c2==c1==c0 & c2>0`
  predicate that `build_episodes` sets `open_flat` from and `capturable(drop_flat)` gates on.
- 2026-07-02 econ event-date is NOT a leak: econ slugs carry the future RELEASE date (the U-3 twin settles
  07-02 per CLAUDE.md); its widens fall inside the held window legitimately and it contributes $0.59.
- Reused harness only — no re-derived cohort, no re-implemented economics ([L28]); the econ [L21]
  quarantine still fires (36 records), 0 restart-censored leak into `filled`.
- Selftest covers the new logic: true-add (base open at widen) AND re-entry (base closed at t5 < widen
  t101 on a re-opened bucket still "held"); flat-driving widen dropped by the lens, non-flat survives;
  `_widen_is_flat(None)`/zero-depth = not flat; all three report sections render.
- All four selftests pass (flip_add + analyze_persistence + capital_sim + ledger run) — import-only, no
  upstream edits.
- Headline FLIP numbers unchanged (the refinement touched only the ADD path): weather 0/101, sports 10/604,
  econ 0/4; FLIP PnL still 0/10 priceable.

## Launch Recommendation
PROCEED. The three audit disclosures are wired in and reproduce the audit's numbers to the cent
($104→$135 sweep; $36.62/27.1% flat-WIDEN; 92 true / 256 re-entry). The decision-quotable figures are now
a hold-conditional, phantom-lensed RANGE ($99 lensed @28h .. $135 unlensed) with the genuine-scale-in
feature honestly sized at 61 adds / $22.69 (both lenses). No CRITICAL/WARN remain.

## Self-review caveat
Authorship bias applies — I wrote this code. I countered it by re-deriving the two load-bearing claims
(lens drop, true-add split) from raw records independently of `add_events` and confirming the match. This
still feeds a BUILD decision, not a signed prereg or live launch, so the bar is "is the measurement honest
and the cohort clean," which it is. An independent reviewer should probe the ONE remaining judgment call:
the true-add/re-entry split is reported on the unlensed cohort (audit's frame) with a both-lenses summary
line — if the project wants the canonical feature number to be the both-lenses one (61/$22.69), that is a
framing choice for the owner, not a correctness issue.
