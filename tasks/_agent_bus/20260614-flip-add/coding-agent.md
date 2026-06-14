---
from: coding-agent
run_id: 20260614-flip-add
timestamp: 2026-06-14T08:24:12Z
task_summary: Refine the read-only ADD/FLIP backtest (scripts/flip_add_backtest.py) per the stats-ml-logic-reviewer audit — wire in a hold-window sweep, the [L20] phantom lens on the add's driving WIDEN, and a TRUE-ADD vs RE-ENTRY split classified against real episode open/close — so the $135 ADD figure is never quoted bare.
files_changed: [scripts/flip_add_backtest.py]
files_created: []
context_loaded: [CLAUDE.md, tasks/lessons.md, tasks/_agent_bus/20260614-flip-add/stats-ml-logic-reviewer.md, tasks/_agent_bus/20260614-flip-add/coding-agent.md (prior run), tasks/_agent_bus/20260614-flip-add/code-logic-reviewer.md (prior run), scripts/flip_add_backtest.py, scripts/capital_sim.py, scripts/analyze_persistence.py, bot/ledger.py, "agent-memory feedback_threshold_ge_vs_gt + pattern_same_bar_fill_optimism + pattern_complement_needs_regime_sign", "data/cross-arb real transition records (06-08..06-14)"]
libraries_consulted: []
self_review_artifact: tasks/_agent_bus/20260614-flip-add/code-logic-reviewer.md
---

## What I changed
- `add_events` refactor (scripts/flip_add_backtest.py:186) — now returns `(out, dropped_flat)`; takes a
  `drop_flat_widen` flag; classifies each add `is_true_add` against the real `build_episodes`
  OPEN/CLOSE interval; carries a `flat_widen` flag. New `_widen_is_flat` helper reuses the exact
  `c2==c1==c0 & c2>0` [L20] predicate `build_episodes`/`capturable(drop_flat)` use.
- Fixed audit INFO-3: the WIDEN candidate sort now keys explicitly on `w[0]` (`key=lambda w: w[0]`) —
  the bare `sorted()` raised `TypeError` the instant two widens shared a `t` (I hit it live reproducing).
- Report (3a) HOLD-WINDOW SWEEP — add count + PnL across `settle_offset_h ∈ {4,8,12,24,28,48}`h, per
  category, with a TRUE-ADD/RE-ENTRY column and a realistic per-category settle annotation (weather ~1.2d,
  sports at game-end, econ at release). WARN-1.
- Report (3b) PHANTOM LENS — add PnL before/after the flat-ladder drop on the driving WIDEN, with the
  dropped count proving the lens fired, per category. WARN-2.
- Report (3c) TRUE-ADD vs RE-ENTRY split + a "BOTH lenses" line (genuine scale-in AND non-phantom widen).
- Caveats: reworded INFO-1 ("original legs are sub-cent; the reverse arbs that follow are larger"), added
  INFO-2 (reverse-leg selection-bias lower bound), and a hold-conditional-range note.
- `--selftest` extended: true-add (base open at widen) vs re-entry (base closed before a later re-opened
  bucket's widen); phantom-lens drops a flat-driving widen, a sloped-ladder widen survives.

## Clean numbers (PAPER/GROSS, span 5.08 d, span<4d → PRELIMINARY; reproduce the audit to the cent)
- ADD-PnL hold sweep: $104.31(4h) $100.60(8h) $107.50(12h) $129.94(24h) **$135.31(28h)** $135.65(48h).
  weather/sports/econ at 28h = $10.10 / $124.28 / $0.93.
- Phantom lens: BEFORE n=348 $135.31 → AFTER n=255 **$98.69** (dropped **93** adds = **$36.62, 27.1%**).
- TRUE-ADD **92 / $38.06** vs RE-ENTRY **256 / $97.24** (28h). TRUE-ADD is INVARIANT across the whole hold
  sweep; RE-ENTRY grows 134→259 — the hold lever moves ONLY re-entry. Genuine scale-in surviving BOTH
  lenses = **61 / $22.69** (31 of the 92 scale-ins ride a flat phantom widen).
- FLIP unchanged (ADD-only refinement): weather 0/101, sports 10/604 (1.7%), econ 0/4; FLIP PnL 0/10
  priceable (sports Kalshi YES bid not logged).

## Why (non-obvious only)
- The phantom lens is CAUSAL: it drops an add when its FIRST qualifying widen is a flat phantom — it does
  NOT fall through to a later clean widen. Falling through would be [L19] oracle look-ahead (picking the
  favourable event); dropping matches `capturable(drop_flat)`'s "drop the opportunity, don't substitute."
  This is what reproduces the audit's $36.62 (a fall-through lens only drops $12.83).
- true-add/re-entry is classified on the REAL episode interval (`build_episodes` close_t), NOT the
  settlement proxy — a logged CLOSE means the EDGE left the book = the scale-in window ended, even though
  the locked pair is still held. That is exactly why TRUE-ADD is hold-invariant and RE-ENTRY is not.

## Verification
- flip_add_backtest --selftest passes; upstream analyze_persistence/capital_sim/ledger selftests still
  pass (import-only, no upstream edits).
- Adversarially re-derived the lens drop (93==93 flat-driving markets, 0 mis-drops) and the true/re split
  (92/256) from raw records independent of `add_events` — both match. See the self-review artifact.

## Single biggest remaining caveat
5.08 d / ~5 event-dates, paper-gross. The decision-quotable ADD figure is a hold-conditional, phantom-
lensed RANGE ($99 lensed @28h .. $135 unlensed), and the actual owner-described feature (genuine scale-in,
both lenses) is only 61 markets / $22.69 — most "adds" are re-entry the live one-position-per-slug guard
already blocks. Not a build trigger on its own; needs multi-week data.
