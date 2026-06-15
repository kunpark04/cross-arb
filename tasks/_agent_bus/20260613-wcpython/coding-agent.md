---
from: coding-agent
run_id: 20260613-wcpython
timestamp: 2026-06-14T03:43:14Z
task_summary: Implement the Python side of "World Cup tradeable" — per-outcome binary WC arbs (NOT a 3-leg basket) across discovery + capital + settlement-gate.
files_changed: [bot/colisted_map.py, scripts/capital_sim.py, scripts/settlement_identity.py]
files_created: []
context_loaded: [.claude/plans/deep-waddling-barto.md, .claude/plans/deep-waddling-barto-agent-a442741179787ce0f.md, CLAUDE.md, memory/MEMORY.md, bot/colisted_map.py, scripts/settlement_identity.py, scripts/capital_sim.py, scripts/selftest_all.py, scripts/verify_sports_settlement.py, scripts/verify_settlement.py (kalshi_detail/fuzzy_rule_fields), scripts/scan_all.py, tasks/lessons.md (L1/L2/L23), scripts/_data/wc_pmus_fwc.json, scripts/_data/wc_kalshi.json]
libraries_consulted: []
self_review_artifact: tasks/_agent_bus/20260613-wcpython/code-logic-reviewer.md
---

## What I changed
- `bot/colisted_map.py`: new SOCCER3 map (`fwc`→`("KXWCGAME","abbrev")`) + 3-entry country-code alias (`irn→iri,alg→dza,hai→hti`), both kept SEPARATE from `LEAGUES` so the moneyline path is untouched (`bot/colisted_map.py:39-56`).
- `bot/colisted_map.py`: `pm_yes_price()` (L23 YES-read: marketSides `description=="Yes"`, never `outcomePrices[0]`) + `soc_parts()` slug splitter (`:104-119`).
- `bot/colisted_map.py`: `soccer3_emit()` — pure/offline-testable bind: groups the 3 sibling pmus slugs per `(a,b,date)`, reuses `pick_game`/`_match_game` (+alias) for the GAME bind, emits ONE PER-OUTCOME BINARY record per outcome (team A / team B / draw↔TIE), each with a SINGLE Kalshi ticker, `cat:"soccer3"`, `void_clean:False`, `settle_basis:"regulation"` (`:163-184`).
- `bot/colisted_map.py`: discovery branch wires `drawable_outcome`+`atc-fwc-` markets → `soccer3_emit`; added to the returned dict, report counts, coverage report, `--live` printout (`:412-431`, return/report edits).
- `bot/colisted_map.py`: offline `_selftest` block proving 3 siblings→3 binary records, draw↔TIE, alias join (irn→iri), L23 YES-read (No-first array), no false join, incomplete-game skip.
- `scripts/capital_sim.py`: `void_haircut` extended to `atc-fwc-` (WC) at `WC_POSTPONE_RATE=0.004` → **0.08¢/contract** (firm tournament schedule, below MLB's 0.26¢, non-zero so the gate reaches TAIL); `aec-` behavior unchanged; selftest updated.
- `scripts/settlement_identity.py`: WC TAIL fixtures (team + draw outcomes) added to `--selftest`; `_audit` now iterates the `soccer3` category and routes each per-outcome record through the single-market `_sports` path (2-way-vs-2-way → TAIL).
- `scripts/settlement_identity.py` (TWO bug fixes, see review artifact): (1) `_kalshi_three_way` no longer reads a LONE bound market as 3-way (the tie-regex 3rd-side inference now requires ≥2 markets — a single TIE ticker in the per-outcome model is just that outcome's binary); (2) `_pm_outcomes_list` parses the LIVE pmus `outcomes` field which is a JSON-encoded STRING, not a list (char-iteration was mis-reading every draw leg as 9-way → false DIVERGENT).

## Why (non-obvious only)
- The alias table is only 3 entries, not 8: 51/59 live games bind exactly; the other 8 are games where ONE team is Iran/Algeria/Haiti — `irn/alg/hai` remap, while their partners (`bel/egy/jor/mar`) are EXACT on Kalshi (self-maps would be dead code). Verified 51 exact + 8 alias = 59/59, 0 miss against the saved live pulls.
- WC routes through `cat:"soccer3"` but is gated via category `"sports"` with a SINGLE Kalshi market (the binary `_sports` path) — this is what makes the gate read it 2-way-vs-2-way instead of the 3-way game comparison.

## Verify (all green)
- `python scripts/settlement_identity.py --selftest` — PASS (incl. WC TAIL team+draw).
- `python scripts/selftest_all.py` — 20/20 GREEN (incl. colisted_map WC selftest + capital_sim WC void).
- `python scripts/settlement_identity.py --audit` (live) — soccer3: **174 TAIL, 0 DIVERGENT, 0 NEEDS_MANUAL** (58 games × 3 outcomes, 0.08¢ each). Live discovery binds 58 games (the saved snapshot had 59; one game's match-winner closed since 2026-06-13).
