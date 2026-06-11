---
from: coding-agent
run_id: 20260611-engine-fix-A
timestamp: 2026-06-11T14:06:02Z
task_summary: Implement 11 reviewed engine bug-fixes in bot-rs (pricing/discovery/transport-auth) + a regression test per fix + in-scope clippy clear.
files_changed: [bot-rs/src/exec.rs, bot-rs/src/auth.rs, bot-rs/src/ledger.rs, bot-rs/src/matcher.rs, bot-rs/src/book.rs, bot-rs/src/discovery.rs, bot-rs/src/venue.rs]
files_created: []
context_loaded: [tasks/_agent_bus/20260611-engine-review/A-pricing-math.md, tasks/_agent_bus/20260611-engine-review/C-discovery.md, tasks/_agent_bus/20260611-engine-review/D-transport-auth.md, CLAUDE.md, bot/ledger.py, bot/colisted_map.py, bot-rs/src/exec.rs, bot-rs/src/auth.rs, bot-rs/src/ledger.rs, bot-rs/src/matcher.rs, bot-rs/src/book.rs, bot-rs/src/discovery.rs, bot-rs/src/venue.rs, bot-rs/src/signal.rs, ~/.claude/agent-memory/coding-agent/pattern_regex_port_quantifier_truncation.md, ~/.claude/agent-memory/coding-agent/feedback_combo_id_leak_checklist.md]
libraries_consulted: []
self_review_artifact: tasks/_agent_bus/20260611-engine-fix-A/code-logic-reviewer.md
---

## What I changed
- [CRIT C2] exec.rs `build_kalshi_payload`/`build_pmus_payload` — `serde_json::json!{}` instead of `format!` string-splice; a `"`/`\` in a venue slug/coid is now escaped, not injected. pmus price kept as a 2dp string, qty bare number (wire shape unchanged for clean input). New test `payload_escapes_quotes_in_untrusted_fields` (exec.rs).
- [WARN D] auth.rs `now_ms_for_sign` + venue.rs `now_ms` — `panic!("system clock before UNIX epoch — cannot sign")` instead of `unwrap_or(0)` (a stale signing clock 401s forever; fail loud).
- [WARN D] exec.rs pmus live leg gated behind `PMUS_POST_SIGNING_VERIFIED` env (default OFF) → typed `ExecError::Rejected(...)` before signing; self-contained env read, no config.rs field. New test `pmus_live_leg_gated_behind_signing_env`.
- [WARN D] venue.rs `apply_kalshi_sub_update` now returns `bool`: forces reconnect when sid unknown OR a control `ws.send` errs (both logged); plus a one-shot WARN when a `subscribed`/`ok` ack carries no parseable sid (no-gap adds disabled this connection). No-gap path unchanged when sid is known.
- [WARN A] ledger.rs new `order_pmus_fee_cents(n,p)` — LINEAR, no ceil (matches `bot/ledger.py::pfee`); `book_pair` leg fees widened `u32→f64` so the pmus fractional cent survives (zero non-test callers). Fixed the test that baked the ceiled 10c (now 9.375c). New test `pmus_fee_is_linear_no_ceil_matches_pfee`.
- [WARN A] matcher.rs `re_two` retries from each subsequent `gte` (loop like `re_one`) so a decoy `…gte5high…gte64lt65f` reads `(64,65)`, not `(5,None)`. Decoy vector added to `weather_bounds_canonicalize_both_venues`.
- [INFO A] book.rs `to_key` doc corrected: quantizes to 4dp (1/100c), not "1c venue tick" (no behavior change).
- [WARN C] discovery.rs `pull_kalshi_series` bounded by a page-cap + stuck-cursor break + row-cap (pure predicate `cursor_loop_done`, unit-tested `cursor_loop_terminates_on_stuck_or_caps`); mirrors `PM_CATALOG_CAP`.
- [WARN C] discovery.rs `parse_release_period` requires a PURELY-ALPHABETIC month segment (Python `-(month)[a-z]*-`): `jun2`/`may1adj` decoys skip to the real `-july-`. Vectors added.
- [WARN C] discovery.rs `parse_cpi_period` picks the LEFTMOST `monthYYYYyoy` in the string (Python `re.search`), not calendar-earliest; scans all occurrences. Two-`yoy`-token vector added.
- [WARN C] discovery.rs doubleheader: Kalshi events sorted by `event_ticker` before indexing → deterministic `(date,i)` `used`-key across re-discovery. New test `sports_doubleheader_binding_is_deterministic_across_passes` (50 passes).
- [clippy] matcher.rs `match_econ` 8 positional args → `&EconQuery` struct (3 args). Join semantics byte-identical; cleared the only in-scope `too_many_arguments`.

Result: cargo test 103 passed / 0 failed (98 baseline + 5 new). cargo clippy: 0 warnings in the 7 target modules (4 remain in main.rs/risk.rs/unwind.rs — out of scope, untouched). cargo build clean.

## Why (non-obvious only)
- `book_pair` u32→f64: the Kalshi leg ceils to whole cents (exact in f64); only the pmus leg is fractional. Widening is the minimal faithful way to book `pfee` without the per-order ceil. Zero production callers today (the live exec path computes no fees yet), so no ripple.
- Fix 8 has no end-to-end async test by design: the project has no HTTP-mock dependency and the I/O layer is intentionally test-free. I extracted the termination decision into the pure `cursor_loop_done` and tested that, rather than add a network-mock dependency.

## Docs cited
(none — no unfamiliar libraries; serde_json/std only, both already in use)
