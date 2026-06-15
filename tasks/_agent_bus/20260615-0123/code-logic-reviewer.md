---
from: coding-agent (self-review pass)
run_id: 20260615-0123
timestamp: 2026-06-15T01:24:00Z
scope_reviewed: ["bot-rs/src/matcher.rs:226-345 (surname/smatch/match_sports_surname + tests)", "bot-rs/src/discovery.rs:113-140 (LEAGUES_SURNAME + Join enum)", "bot-rs/src/discovery.rs:498-560 (pick_game/resolve_two join dispatch)", "bot-rs/src/discovery.rs:769-840 (sports assemble surname arm)", "bot-rs/src/discovery.rs:~1000 (discover prefetch)", "bot-rs/src/discovery.rs:933-965 (sports_keys)", "bot-rs/src/discovery.rs surname assemble tests"]
critical_count: 0
warn_count: 1
info_count: 3
launch_recommendation: PROCEED
self_review: true
cross_references: ["tasks/lessons.md (L1, L17, L23, L31)", "C:/Users/kunpa/.claude/agent-memory/coding-agent/pattern_regex_port_quantifier_truncation.md"]
---

## Goal Understanding
Faithfully port the Python `colisted_map.py` SURNAME co-listing join (tennis ATP/WTA/ITF + UFC) into the Rust bot's discovery so those 5 individual-sport leagues bind to their Kalshi twins instead of logging UNMAPPED — closing a coverage-parity gap. The load-bearing constraint is the L1 "no false positives" invariant: `smatch`'s ≤1-char-prefix guard must reject distinct players sharing a prefix (`martin`~`martinez`), and the abbrev path must stay byte-identical.

## Scope Reviewed
- matcher.rs:234 `surname` — port of `colisted_map.py::surname`.
- matcher.rs:253 `smatch` — port of `colisted_map.py::smatch` (the L1 guard).
- matcher.rs:269 `match_sports_surname` — surname analogue of `match_sports_abbrev`.
- discovery.rs:~115 `LEAGUES_SURNAME` + `Join` enum; `LEAGUES_ABBREV` unchanged.
- discovery.rs:498 `pick_game` / `resolve_two` — `join`-dispatched binding.
- discovery.rs:769 sports `assemble` branch — both league tables iterated; key extraction branched.
- discovery.rs:933 `sports_keys` (was `sports_abbrevs`).
- discovery.rs:~1000 `discover` prefetch `needed` vec.
- Tests: matcher.rs (3) + discovery.rs (2); 3 existing `pick_game` call sites updated.

## Findings

### CRITICAL (must fix before launch)
None.

### WARN (fix or justify)
- HashMap iteration non-determinism on multi-smatch — SELF-RESOLVED within run.
  - Location: discovery.rs `resolve_two` (`Join::Surname` arm).
  - Issue: Python's `_match_game` takes the insertion-FIRST `smatch`ing Kalshi key; a Rust `HashMap` iterates in random order. On the pathological case where two near-duplicate players in ONE Kalshi event both `smatch` one pmus surname (e.g. event has `muller` AND `mulle`), a plain `find` would bind a different ticker per re-discovery pass, which under a live held position could swap that position's Kalshi tickers (the exact hazard the existing doubleheader `event_ticker` sort guards against — L31 "removing/relying-on an ordering invariant").
  - Why it matters: cross-pass ticker swap on a held position is a money-path correctness bug, not just cosmetic.
  - Resolution: changed the surname `find` to `min_by_key(key)` — a STABLE lexicographic choice across passes. Python's insertion order isn't stable across Kalshi API responses either, so lex-min is strictly safer than mirroring an unstable reference order. The normal (unique-match) case is unaffected.
  - Status: self-resolved within run.

### INFO (optional improvements / simplifications)
- `surname` accent coverage is Latin-1 only (reuses `fold_accent`). A non-Latin-1 letter Python would ASCII-fold-to-nothing (e.g. Turkish `İ` U+0130 → `i` under NFKD) is instead DROPPED by my `is_ascii()` filter, so a name like `İ...` could fold to a slightly different surname than Python. Consequence is at worst a MISS (no bind), NEVER a false bind (a shorter/different surname can't manufacture an L1 false positive) — same failure mode as the league being unmapped (the prior status quo), and identical to the documented `norm_country` limitation. Not worth a unicode-crate dependency for the rare case; flagged for awareness.
- `match_sports_surname` (matcher.rs, Vec-backed, insertion-first tie-break — exactly Python's order) and `resolve_two` (discovery.rs, HashMap, lex-min tie-break) differ in their multi-match tie-break. They are never on the same code path (assemble binds via `resolve_two`; `match_sports_surname` is the standalone tested primitive, mirroring how `match_sports_abbrev` already relates to `resolve_two`), and both are deterministic. Harmless; noted for the next reader.
- A Kalshi event with two players who fold to the SAME surname (e.g. two `martinez`) last-wins-overwrites the event-dict key in BOTH Python (`dict[k]=tk`) and Rust (`HashMap.insert`) — an inherent surname-join ambiguity, not introduced here. Faithful to the reference.

## Checks Passed
- L1 no-false-positive guard PINNED by tests: `smatch` rejects `martin`/`martinez`, `williams`/`williamson`, `mann`/`mannarino`, `koval`/`kovalenko` (both directions); the full-path `assemble_surname_l1_prefix_does_not_bind_wrong_player` proves a pmus `martin` does NOT bind a Kalshi `martinez` event (0 pairs, not a phantom).
- `smatch` argument order matches Python (`smatch(kalshi_key, pmus_key)`) — and `smatch` is symmetric anyway, verified no asymmetry bug.
- `resolve_two`'s `ta != tb` (ticker distinctness) is equivalent to Python's `mA != mB` (key distinctness): a single Kalshi key matching both pmus keys yields the same ticker on both sides → rejected by both; traced the degenerate cases.
- Abbrev path byte-identical: `LEAGUES_ABBREV` table unchanged; `resolve_two` `Join::Abbrev` = old `ev.get`; `sports_keys` `Join::Abbrev` = old `sports_abbrevs`; Kalshi abbrev-suffix key extraction unchanged. All 4 existing abbrev sports tests + the doubleheader-determinism test still green.
- `settle_clean=false` for surname leagues (void/postpone tail) — matches the Python `void_clean=False` and the task requirement.
- `discover()` prefetch includes all 5 surname series — without it the surname branch's `kalshi_series(kser)` is empty → 0 binds (the SOCCER3/WC bug the code comments warn about). Pacing loop handles the extra series count.
- `surname` empty-fold → `sports_keys` returns None → game skipped; equivalent to Python (empty key → `smatch` len<4 → no match → `pick_game` None). No false negative relative to Python.
- regex-port-quantifier-truncation pattern (memory) does NOT apply: `surname` ports a char-class substitution + split + last-token, no bounded quantifier to truncate; `smatch`'s `len<4` / `|len-len|<=1` are exact numeric ports.
- Coverage report: `sports_leagues_unmapped` now excludes both tables, so the 5 leagues stop logging UNMAPPED; refresh.rs coverage log unchanged.
- Gates: `cargo test` 154/0; `cargo clippy --all-targets` clean. Bot not run live; no orders.

## Launch Recommendation
PROCEED. The port is faithful, the L1 guard is pinned by reject-case tests on both the matcher primitive and the full assemble path, the abbrev path is byte-identical, and the one latent non-determinism was caught and resolved with a stable tie-break.

## Self-review caveat
This is authorship self-review (bias toward "I already considered that"). The change is discovery-only (read-side; binds market pairs, places no orders), the L1 reject cases are tested both in isolation and end-to-end, and it does not feed a signed preregistration — so the self-review is an adequate first line here. A live discovery run on the owner's environment (the only place the surname leagues' real Kalshi events exist) is still the L17 end-to-end confirmation that the live `yes_sub_title` surname strings actually `smatch` their pmus `team.name` twins; the offline tests use representative-but-synthetic strings. Recommend the owner eyeball the first live `[refresh]` pair counts for atp/wta/itfm/itfw/ufc before sizing into any individual-sport pair.
