---
from: coding-agent (self-review pass)
run_id: 20260614-wc-monitor
timestamp: 2026-06-14T04:24:00Z
scope_reviewed: [bot/monitor.py:914-941 (register 1:1 loop + soccer3), bot/monitor.py:951-962 (startup print + _counts), bot/monitor.py:1093-1098 (heartbeat current/prune set), bot/monitor.py:1110-1112 (health beacon), bot/monitor.py:455-481 (_selftest soccer3 case), bot/monitor.py:880-883 (run_live docstring)]
critical_count: 0
warn_count: 1
info_count: 2
launch_recommendation: PROCEED
self_review: true
cross_references: [tasks/_agent_bus/20260613-wcpython/code-logic-reviewer.md]
---

## Goal Understanding
Register each FIFA World Cup `soccer3` per-outcome record (teamA / draw / teamB — each a BINARY 1:1 co-listed
pmus-slug↔single-Kalshi-ticker, committed 17ab0ea) into the read-only persistence monitor exactly like a
weather/econ bucket: a 1:1 `MarketTracker` (NOT the 2-team `GameTracker`), with the 3 outcomes of a game
groupable as one correlated-exposure cluster via the shared `game` field. Read-only; closes the explicit
deferred WARN from run 20260613-wcpython ("wire register() — a MarketTracker per outcome, grouped by game").

## Scope Reviewed
- `bot/monitor.py` `register()` 1:1 loop — extended the `enumerate(weather + econ)` iterable to append `colisted.get("soccer3", [])`; one branch, same `MarketTracker` + `_collide` + `wx_trackers`-exclusion path.
- `bot/monitor.py` prune `current` set (heartbeat) — `soccer3` slugs added so WC isn't torn down each heartbeat.
- `bot/monitor.py` startup print, `_counts`, `health` beacon — `soccer3` count added (reporting parity).
- `bot/monitor.py::_selftest` — new offline soccer3 registration assertion.
- `run_live` docstring — SOCCER3(WC) noted on the 1:1 path.

## Findings

### CRITICAL (must fix before launch)
(none)

### WARN (fix or justify)
- soccer3 slugs were missing from the heartbeat prune `current` set — would have pruned WC every heartbeat (self-resolved within run)
  - Location: `bot/monitor.py` heartbeat `current = weather | sports | econ` (was missing soccer3)
  - Issue: `prune_decision(set(pm_targets), current, absent)` tears down any registered slug absent from `current` for 2 heartbeats. Registering WC into `pm_targets` WITHOUT adding its slugs to `current` would mark every WC pair "settled" within ~2 refresh cycles → register/teardown thrash (and a censored re-OPEN phantom on each re-add, the [L20] class). This exactly mirrors the inline comment "incl. econ or it'd be pruned each heartbeat."
  - Fix: added `| {e["slug"] for e in fresh.get("soccer3", [])}` to `current`. Without this the feature would have silently not persisted WC data despite registering it.
  - Status: self-resolved within run. (Surfaced by the Scope/Logical-Flaw sweep, not by a failing test — the offline selftests don't exercise the live heartbeat prune timing.)

### INFO (optional improvements / simplifications)
- The cluster requirement is met implicitly (the `game` lives in each slug's `<a>-<b>-<date>` substring, just like weather's city-date), matching the monitor's existing convention where NO record carries a `cluster` field and downstream grouping is by slug-regex. Surfaced as a deliberate design choice, not a gap — a record-schema `cluster` field would diverge from the weather model and be consumed by nothing on the Python side. No action.
- The `_selftest` soccer3 block asserts the registration INVARIANT (binary MarketTracker, one ticker, game-grouped, wx-excluded, collide-guarded) by replaying register()'s 1:1 loop shape on a fake map, rather than calling the `run_live`-nested `register` closure directly. This avoids refactoring the live closure (scope creep / capture-rewrite risk) at the cost of not exercising the literal `register` symbol. Acceptable per the task ("an offline assertion in _selftest suffices"); the live `register` path is additionally covered end-to-end by `scripts/test_monitor_nogap.py` (GREEN). No action.

## Checks Passed
- **(a) BINARY 1:1, NOT GameTracker** — soccer3 records flow through the `weather + econ + soccer3` `MarketTracker` loop; selftest asserts `isinstance(trk, MarketTracker) and not isinstance(trk, GameTracker)` and `"kalshi_b" not in e`. The 2-team `GameTracker` loop reads `e["kalshi_a"]/["kalshi_b"]`, which soccer3 records do not have, so they can never enter it (confirmed against the 20260613-wcpython reviewer's live finding: 174 records, all single-`kalshi`, no double-bind).
- **(b) game cluster** — selftest asserts `{e["game"] for e in soc} == {"ger-cuw-2026-06-14"}`; the shared `game` substring is present in all 3 pm slugs, so the 3 outcomes group identically to a weather city-date downstream (slug-regex), and identically to the live bot's per-game cluster `fwc-ger-cuw-2026-06-14`.
- **(c) `_collide` covers the WC ticker** — soccer3 runs the same `if e["slug"] in pm_targets or _collide(e["kalshi"]): continue` guard; selftest asserts no ticker is bound twice. A duplicate WC ticker is SKIPPED with the no-false-positive warning, never silently overwritten (L1).
- **(d) weather/sports/econ untouched** — weather still gated into `wx_trackers` by `i < wx_n` (soccer3 appended AFTER weather+econ, so `i >= wx_n` → excluded; selftest asserts `wxr == {}`); the `colisted["sports"]` GameTracker loop is byte-unchanged; econ still in the 1:1 loop; full `_selftest` weather/sports/FlipDebouncer/prune/logger blocks all still GREEN.
- **draw↔TIE** — selftest asserts the draw outcome binds `KXWCGAME-26JUN14GERCUW-TIE` (the event tie ticker), consistent with `soccer3_emit`'s `pl["tie"]` mapping.
- **No record-schema change** — `TransitionLogger.write` is untouched; WC transitions log OPEN/CLOSE/WIDEN/NARROW/FLIP with the identical `{t, market, transition, dir, net_edge, depth?, age?, px?}` shape as weather, so every existing loader (`analyze_persistence.py`, `capital_sim.py`) runs unmodified.
- **`.get` safety** — `colisted.get("soccer3", [])` tolerates a map without the key (e.g. `test_monitor_nogap.py`'s fake returns `{weather, sports, econ}` only); GREEN.
- **teardown parity** — a pruned soccer3 slug frees via the same single-ticker `slug_k[slug]=[kalshi]` path as weather (one `k_targets`/`books` entry, delete_markets-unsubscribed), not the 2-ticker game path.
- **Suite** — `python bot/monitor.py` GREEN incl. the new soccer3 case; `python scripts/selftest_all.py` = **20/20 ALL GREEN**.

## Launch Recommendation
PROCEED. (Code change only — the monitor runs on the gated DigitalOcean droplet; the actual redeploy is the owner's step per decision 0006. This change is read-only, additive, and does not touch the live trading bot `bot-rs`.)

## Self-review caveat
This is an authorship self-review and carries authorship bias. The change is small, read-only, and on the
research-persistence path (not a signed-prereg launch), so independent review is not strictly required.
The one finding worth a human glance is upstream of this task: the WARN/INFO from 20260613-wcpython noted
`WC_POSTPONE_RATE=0.004` is an unvalidated prior — unrelated to this wiring but relevant before WC is traded
with real size. The deferred-WARN this run closes was itself flagged by the prior self-review, which is a
reminder that the cross-run handoff (not a single self-review) is what actually caught the gap.
