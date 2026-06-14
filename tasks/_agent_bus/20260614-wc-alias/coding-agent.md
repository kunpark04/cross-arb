---
from: coding-agent
run_id: 20260614-wc-alias
timestamp: 2026-06-14T06:30:38Z
task_summary: Replace the hardcoded 3-entry WC country-code alias table with an exact-code primary + normalized-full-name fallback join (L1-safe, future-proof), in both bot/colisted_map.py and bot-rs/src/discovery.rs.
files_changed: [bot/colisted_map.py, bot-rs/src/discovery.rs, bot-rs/src/main.rs]
files_created: []
context_loaded: [CLAUDE.md, tasks/lessons.md, bot/colisted_map.py, bot-rs/src/discovery.rs, bot-rs/src/matcher.rs, bot-rs/Cargo.toml, scripts/sports_name_match.py, scripts/_data/wc_pmus_fwc.json, scripts/_data/wc_kalshi.json, C:/Users/kunpa/.claude/agent-memory/coding-agent/MEMORY.md]
libraries_consulted: []
self_review_artifact: tasks/_agent_bus/20260614-wc-alias/code-logic-reviewer.md
---

## What I changed
- Python `bot/colisted_map.py`: removed `SOCCER_CC_ALIAS`; added `_norm_country` (NFKD-fold + lowercase + strip non-alphanumeric), `pm_team_name`, `_wc_resolve_codes` (exact-code primary, exact-normalized-name fallback); `soccer3_emit` now takes `knames=` and returns `(records, unbound)`; the WC discovery loop builds the parallel Kalshi `{suffix: yes_sub_title}` name map and prints `[wc-unbound]` to stderr for code+name double-misses; `report["soccer3_UNBOUND"]` added.
- Rust `bot-rs/src/discovery.rs`: removed `SOCCER_CC_ALIAS`/`soccer_cc_alias`; added `norm_country` (+ `fold_accent`, dep-free Latin-1 fold matching Python NFKD), `pm_wc_team_name`, `wc_resolve_codes`; WC loop builds the parallel `by_name`/`kbydate_names` and records `Discovery.soccer_unbound: Vec<(slug, why)>`.
- Rust `bot-rs/src/main.rs`: `report_coverage` prints `[wc-unbound]` for each `soccer_unbound` entry.
- 2-leg trading core (signal/build_legs/risk/exec/unwind) untouched; per-outcome BINARY emission unchanged (3 binary Pairs/game, `kalshi_b=None`, `cat=Sports`, `soccer=true`).

## Why (non-obvious only)
- Exact-abbrev MUST stay primary: 9 live games (bih/civ/cpv/tur/usa…) have IDENTICAL codes but DIFFERENT name strings across venues ("Bosnia-Herzegovina" vs "Bosnia and Herzegovina", "Côte d'Ivoire" vs "Ivory Coast", "United States" vs "USA"). They bind by code; their names never get consulted. A name-only matcher would falsely fail them. The name fallback fires only for the 8 genuine code-mismatch games, where names agree.
- `_norm_country(None)` must be `""` not `"none"` (a missing name must be a non-matchable key) — caught by the self-test.

## Field paths verified (against real captured payloads, not assumed)
- pmus full country name: `marketSides[*].team.name` ("Germany", "IR Iran"). For a `-<code>` market BOTH Yes and No sides carry the SAME team; `-draw` markets carry `team: null`.
- Kalshi full country name: `yes_sub_title` ("Germany", "IR Iran", "Tie"). (`subtitle` also exists but `yes_sub_title` is the field the codebase already uses everywhere and is present on all markets.)
- All 48 live WC countries normalize to 48 DISTINCT keys on each venue (0 collisions, incl. accent-fold) → exact-normalized equality is L1-safe with no fuzzy.

## Verification
- Python: `bot/colisted_map.py` self-test OK; `scripts/selftest_all.py` 20/20 GREEN.
- Rust: `cargo test` 136 passed (was 125); `cargo clippy --all-targets` clean; `cargo run -- --smoke` fires the WC per-outcome arb (Pmus Yes @42c + Kalshi No @55c on atc-fwc-ger-cuw-2026-06-14-ger), dry-run.
- Equivalence on REAL captured WC board (throwaway checks, since removed): both Python and Rust bind all 59 games / 177 per-outcome pairs / 0 unbound; the 8 code-mismatch games bind by name; Rust `norm_country` == Python `_norm_country` on all 54 real names incl. accented.
