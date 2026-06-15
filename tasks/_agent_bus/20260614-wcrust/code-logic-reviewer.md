---
from: coding-agent (self-review pass)
run_id: 20260614-wcrust
timestamp: 2026-06-14T04:09:00Z
scope_reviewed: [bot-rs/src/discovery.rs:39-69 (Pair/Discovery soccer fields), bot-rs/src/discovery.rs:113-191 (SOCCER3/alias/soc_parts/pick_wc_game), bot-rs/src/discovery.rs:748-833 (soccer3 assemble branch), bot-rs/src/main.rs:131-161 (LivePair+From), bot-rs/src/main.rs:642-657 (track_position WC skip), bot-rs/src/main.rs (WC smoke + routing test), bot-rs/src/postpone.rs:218]
critical_count: 0
warn_count: 0
info_count: 2
launch_recommendation: PROCEED
self_review: true
cross_references: [tasks/_agent_bus/20260613-wcpython, bot/colisted_map.py, tasks/lessons.md]
---

## Goal Understanding
Make the FIFA World Cup tradeable on the bot-rs LIVE path. A WC game's 3 outcomes (teamA / draw / teamB) are each their OWN binary YES/NO market on BOTH venues, so each outcome is a clean 1:1 co-listed pair — exactly the weather/econ model. The change is DISCOVERY (group + bind the 3 siblings, alias the country codes) + a routing MARKER, reusing the 2-leg `signal`/`build_legs`/`risk`/`exec`/`unwind` core UNCHANGED (no 3-leg basket).

## Scope Reviewed
- `discovery.rs` soccer fields + SOCCER3/alias maps + `soc_parts`/`pick_wc_game` + the `soccer3` assemble branch — the identity-critical JOIN (where a phantom pair would be born).
- `main.rs` `LivePair`/`From`/`track_position` + the WC smoke + the binary-routing test.
- `postpone.rs` empty-league warning skip.
- ADVERSARIAL POSTURE ENGAGED: from here I am a skeptical reviewer trying to find the mis-hedge / leak / false-join before a launch does — not the author defending the diff.

## Findings

### CRITICAL (must fix before launch)
- None.

### WARN (fix or justify)
- None.

### INFO (optional improvements / simplifications)
- Removed a dead `pm_yes_price` port DURING this review (self-resolved). It mirrored the Python but is never called on the bot's money path: discovery only maps slug<->ticker; the live pmus YES book comes from the per-SLUG YES-oriented WS frame (`venue::parse_pmus_market_data`). Keeping it would have been misleading dead code (implying discovery prices WC outcomes). Status: self-resolved within run; `-1` test accordingly.
- The `cluster` key for WC is `fwc-<a>-<b>-<date>` (per-game), so the 3 outcomes of one game share a correlated-exposure cluster — correct (they are correlated: at most one resolves YES). No change; noted because it differs slightly from the moneyline `<league>-<date>` cluster shape (intentional: WC needs per-GAME, not per-date, correlation).

## Checks Passed
- **(a) WC NEVER routes to `game_signal`.** The live loop routes on `pair.kalshi_b.is_some()` (`main.rs:462`). A WC pair emits `kalshi_b = None` -> the binary `signal` arm, structurally. Proven two ways: the `world_cup_pair_routes_through_binary_signal_not_game_signal` test asserts `kalshi_b.is_none()` AND drives `build_legs` to the 1:1 shape (YES@pmus + NO@Kalshi on the SAME outcome ticker — only the `(None, dir)` plan_legs arms emit this); and the `--smoke` output shows `Kalshi No ...-GER` (binary) vs the moneyline `Kalshi Yes ...-PIT` (team-B). A 3-outcome market routed to the 2-team `game_signal` would mis-hedge (draw loses both legs) — prevented.
- **(b) Per-outcome legs LOCK.** For "Germany wins": pmus YES book (slug `-ger`) + Kalshi YES book (ticker `-GER`) grade off the SAME match result on regulation. PK = YES@pmus + NO@Kalshi pays $1 whether THIS outcome resolves YES or NO — identical to a weather bucket. The draw and the-other-team outcomes are independent binaries, each locked the same way. Settlement identity holds because both venues' per-outcome markets settle on the same regulation result.
- **(c) The join has NO false positives.** Bind is exact-DATE (`pick_wc_game` uses `kbydate.get(date)`); country codes are matched via an EXPLICIT 3-entry alias table (`soccer_cc_alias`), never fuzzy 3-letter matching (L1); a `used`-set guards against two pmus games binding the same Kalshi event (doubleheader guard — WC has none, but free correctness); requires all 3 sibling outcomes AND a Kalshi TIE, all three tickers pairwise-DISTINCT (`resolve_two` ensures `ta != tb`; `pick_wc_game` adds `ta != tie && tb != tie`). Differential-verified vs the Python `soccer3_emit`: unknown-code -> 0, partial-game -> 0, alias irn->iri binds IRI, byte-identical record set.
- **(d) `build_legs` / `exec` / `unwind` / `Position` UNCHANGED (still 2-leg).** `git diff` touches none of those bodies. WC reuses the binary `plan_legs` `(None, dir)` arms verbatim. No `kalshi_d`, no 3-leg exec, no `Position` struct change.
- **(e) Moneyline sports + weather/econ untouched.** `SOCCER3`/`SOCCER_CC_ALIAS` are SEPARATE consts from `LEAGUES_ABBREV`; the soccer branch is additive (runs AFTER the moneyline branch, reads the same `pm_markets` but filters `marketType=="drawable_outcome"` — disjoint from `"moneyline"`). The `assemble_joins_the_right_pairs_and_no_false_joins` test still asserts exactly 1 moneyline pair + 0 soccer + 0 econ-phantom; all prior tests pass.
- **`Cat::Sports` reuse is correct, not a shortcut.** `lock_days(Cat::Sports)` uses the dynamic `days_to_event` (correct for near-dated WC); the settlement gate passes via `settle_clean=true`; the toxicity gate is `Cat::Weather`-only (WC exempt); the MLB postpone poll filters `league=="mlb"` so a WC (empty-league) position is never unwound by it.
- **`settle_clean=true` is the right verdict.** Mirrors the Python TAIL: both venues settle on regulation; the small priceable void tail is far below a tradeable edge. This passes the risk gate's invariant-#1 clause WITHOUT needing `assume_sports_settled` (which is for moneyline recon, still open). Full void-tail cost-vs-edge integration is a noted follow-on (the plan defers it).
- **No leakage / lookahead / NaN issues.** Discovery is pure slug-mapping; `days_to_event = game_date - today` reuses the proven `ymd_to_epoch_days` (None when a date is unmissing -> proximity gate dormant, fail-safe). No future info enters the join.
- **Determinism.** The soccer branch sorts game keys (`pm_soc` is a HashMap) and sorts Kalshi events by event_ticker before the `used`-set bind — so a re-discovery pass binds identically (mirrors the moneyline determinism fix). Verified by the structure matching the tested moneyline path.
- **Coverage audit (L7).** A pmus drawable-outcome slug for an UNMAPPED soccer league (e.g. `fifa`) is reported in `soccer_leagues_unmapped` + logged in `report_coverage` — never silently missed. Tested.
- **Tests + clippy.** 135 pass; clippy `--all-targets` clean (0 warnings, matching the README baseline).

## Launch Recommendation
PROCEED. The change is additive, the join is differential-verified byte-identical to the established Python reference, and the money-path invariants (binary routing, per-outcome lock, no false join, untouched 2-leg core) all hold.

## Self-review caveat
Authorship bias acknowledged: I wrote this diff, so "I already reasoned about it" can mask a real flaw. The strongest external check here is the DIFFERENTIAL parity vs `bot/colisted_map.py::soccer3_emit` (an independently-reviewed reference that landed in 17ab0ea) — the join is byte-identical, which is harder to fool than self-inspection. This change feeds the LIVE order path; the bot is dry-run-by-default and the live submission stays the owner's gated step (0006/0015), but before arming WC past dry-run an independent reviewer (or the `llm-council`) pass on the routing + the `Cat::Sports`-reuse decision is warranted, and the void-tail cost-vs-edge integration (noted follow-on) should land first.
