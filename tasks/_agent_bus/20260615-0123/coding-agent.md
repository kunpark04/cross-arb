---
from: coding-agent
run_id: 20260615-0123
timestamp: 2026-06-15T01:23:30Z
task_summary: Port the surname-based co-listing matcher into bot-rs discovery so tennis (ATP/WTA/ITF) + UFC bind to their Kalshi twins, closing a coverage-parity gap.
files_changed: [bot-rs/src/matcher.rs, bot-rs/src/discovery.rs]
files_created: []
context_loaded: [CLAUDE.md, "C:/Users/kunpa/.claude/CLAUDE.md", bot/colisted_map.py, bot-rs/src/discovery.rs, bot-rs/src/matcher.rs, bot-rs/src/types.rs, bot-rs/src/pair.rs, bot-rs/src/refresh.rs, tasks/lessons.md, "C:/Users/kunpa/.claude/projects/.../memory/MEMORY.md", "C:/Users/kunpa/.claude/agent-memory/coding-agent/pattern_regex_port_quantifier_truncation.md"]
libraries_consulted: []
self_review_artifact: tasks/_agent_bus/20260615-0123/code-logic-reviewer.md
---

## What I changed
- matcher.rs: added `surname(name)` (faithful port of `colisted_map.py::surname` — NFKD/Latin-1 accent-fold via the reused `discovery::fold_accent`, drop non-ASCII, lowercase, non-`[a-z space hyphen]`→space, last whitespace token) at matcher.rs:234.
- matcher.rs: added `smatch(a, b)` (port of `colisted_map.py::smatch`: exact OR ≤1-char prefix with the `len<4` floor + `|len-len|<=1` guard that rejects distinct prefix-sharing players — the L1 invariant) at matcher.rs:253; quoted in the review artifact.
- matcher.rs: added `match_sports_surname(...)` — the matcher-level surname primitive paralleling the existing `match_sports_abbrev` (2 distinct tickers, `Cat::Sports`, `settle_clean=false`) at matcher.rs:269.
- discovery.rs: added `LEAGUES_SURNAME` (atp/wta/itfm/itfw/ufc → KX*MATCH/KXUFCFIGHT) + a `Join{Abbrev,Surname}` enum; kept `LEAGUES_ABBREV` byte-identical (doc-comment only) at discovery.rs:~115.
- discovery.rs: threaded `join: Join` through `pick_game` + `resolve_two` (abbrev arm unchanged exact-lookup; surname arm binds via `smatch`, lexicographic-min on the pathological multi-match for cross-pass determinism); `pick_wc_game` now passes `Join::Abbrev`.
- discovery.rs: the sports `assemble` branch iterates BOTH league tables (chained, tagged with `Join`); the Kalshi event dict is keyed on the abbrev suffix (abbrev) or `surname(yes_sub_title)` (surname), and `sports_abbrevs` is generalized to `sports_keys(pm, join)` (team.abbreviation vs `surname(team.name)`), same long-named-A/B ordering.
- discovery.rs: added the 5 surname series to the `discover()` prefetch `needed` vec (else the surname branch sees an empty cache and binds 0) at discovery.rs:~1000.
- Tests: +3 in matcher.rs (`surname_folds_and_takes_last_token`, `smatch_accepts_close_rejects_distinct_prefix_l1`, `sports_surname_binds_two_distinct_tickers`), +2 in discovery.rs (`assemble_binds_surname_tennis_league`, `assemble_surname_l1_prefix_does_not_bind_wrong_player`). The 3 existing discovery `pick_game` test call sites updated with `Join::Abbrev`.

## Why (non-obvious only)
- `fold_accent` made `pub(crate)` (was private) so the surname fold reuses the same Latin-1 table the WC name-fold uses — no second accent table (task-suggested).
- `resolve_two` surname arm uses `min_by_key` not `find`: a Rust HashMap has random iteration order, so on the pathological "two near-duplicate players in one event both smatch one pmus key" case, `find` would bind a different ticker per re-discovery pass and could swap a held position's tickers (the same determinism hazard the existing doubleheader `event_ticker` sort guards). Lex-min is stable; Python's insertion order isn't stable across API responses either. Documented as a deliberate robustness deviation from the reference.

## Gate results
- `cargo test`: 154 passed, 0 failed (baseline 149 + 5 new).
- `cargo clippy --all-targets`: clean (no warnings/errors).
- Bot NOT run live; no orders placed. Verification is offline (`assemble` + matcher unit tests).
