---
from: coding-agent (self-review pass)
run_id: 20260611-engine-fix-A
timestamp: 2026-06-11T14:06:02Z
scope_reviewed: [exec.rs:163-260, exec.rs:309-400+(tests), auth.rs:18-29, venue.rs:243-251, venue.rs:299-330, venue.rs:356-372, venue.rs:405-440, ledger.rs:13-45, ledger.rs:64-95, matcher.rs:120-170, matcher.rs:237-260, book.rs:13-25, discovery.rs:312-360, discovery.rs:577-595, discovery.rs:713-745]
critical_count: 0
warn_count: 0
info_count: 3
launch_recommendation: PROCEED
self_review: true
cross_references: [tasks/_agent_bus/20260611-engine-review/A-pricing-math.md, tasks/_agent_bus/20260611-engine-review/C-discovery.md, tasks/_agent_bus/20260611-engine-review/D-transport-auth.md, ~/.claude/agent-memory/coding-agent/pattern_regex_port_quantifier_truncation.md]
---

## Goal Understanding
Implement 11 reviewed engine bug-fixes in bot-rs verbatim from the three engine-review briefs, one regression test per fix, all tests green, plus the one in-scope clippy clear (matcher arg-count). The fixes harden the live order body (JSON injection), the signing clock, the pmus-leg gate, the WS sub-update path, the pmus fee parity, two slug parsers, the Kalshi pagination bound, and doubleheader determinism — none change a join SEMANTIC; they fix latent wrong-output/hang/silent-drop paths.

## Scope Reviewed
- exec.rs — payload builders → serde_json; pmus signing-gate env; +2 tests. In scope.
- auth.rs / venue.rs `now_ms*` — panic on pre-epoch. In scope.
- venue.rs `apply_kalshi_sub_update` + sid handling — reconnect signal + WARN. In scope.
- ledger.rs — pmus linear fee + book_pair f64 widening + test fix. In scope.
- matcher.rs — `re_two` retry + EconQuery struct (clippy). In scope.
- book.rs — doc-only. In scope.
- discovery.rs — pagination cap, release/cpi period parsers, DH sort; +2 tests. In scope.
- NOT touched: main.rs, risk.rs, config.rs (owned by another pass). Verified by grep — no edits leaked.

## Findings

### CRITICAL (must fix before launch)
None.

### WARN (fix or justify)
None. (The self-review specifically hunted for: a missed `book_pair`/`match_econ` caller breaking on the signature changes — grepped, only the in-file tests call them; a `clean`-uninitialized path after the new `break 'read` — `clean` is set `true` before the read loop, so the `clean=false` before break is definitely-assigned and the build confirms it; a pmus wire-shape regression from `json!` — price stays a 2dp string, qty a bare number, re-parse test confirms.)

### INFO (optional improvements / simplifications)
- **`parse_release_period` first-segment edge (not reachable).** Python's `-(month)[a-z]*-` requires a leading dash, so a month appearing as the slug's FIRST segment (index 0, no preceding dash) would not match in Python but WOULD in my `split('-')` scan. Not reachable: this fn is only called for `urc`/`nfpc` families, whose slugs always start with the family prefix (`urc`/`nfpc`), never a month. I implemented exactly the review's specified "pure-alpha" fix and did not add a `.skip(1)` (out of the specified scope); flagging the residual for the record.
- **`parse_cpi_period` `maybe→may` shared limitation.** A slug WORD containing a month substring (e.g. `maybe2026yoy`) is read as `may` by BOTH the Rust and the Python (`re.search` over the whole string with no word boundary). This is a pre-existing shared limitation the review explicitly flagged as "not a port infidelity"; my leftmost-match rewrite preserves it exactly (does not worsen or fix it).
- **Fix 8 (pagination cap) has no end-to-end async test.** The live cursor loop can't be unit-tested without an HTTP mock (no such dependency in the project, and adding one violates the no-new-deps rule). I extracted the termination decision into the pure `cursor_loop_done` and tested THAT (stuck-cursor, empty, page-cap, row-cap, forward-progress). The async wiring around it is a 3-line loop that is self-evidently correct given the predicate.

## Checks Passed
- **Port fidelity vs Python source-of-truth (the L17/quantifier-truncation failure mode):** re-read `bot/ledger.py::pfee` (linear, `0.05·n·p(1-p)`, `0<p<1` guard) → `order_pmus_fee_cents` matches incl. the guard; differential values 1.25c/9.375c/6.02c asserted. Re-read `colisted_map.py:202` CPI regex `(month)[a-z]*?(\d{4})yoy` and `:211` release regex `-(month)[a-z]*-` → both ports verified leftmost + pure-alpha + the `[2:]`/`[:3]` slicing. GDP full-date period (the known `\d{0,2}` truncation case from my memory) was NOT in scope this run and is unchanged (still passes `econ_gdp_full_date_period_joins`).
- **No join semantics changed:** `EconQuery` fields map 1:1 to the old positional args; `cluster` format string identical; `re_two` returns the SAME `(x,y)` for every single-`gte` slug (the only change is retrying past a decoy) — the existing weather vectors still pass unchanged.
- **Math/units:** pmus fee in cents (×100) consistent with the Kalshi cents path; `book_pair` fees `/100.0` unchanged in meaning (sum of fractional cents → dollars). No sign error, no unit mix.
- **Edge cases:** `order_pmus_fee_cents` p∉(0,1)→0 (tested); `cursor_loop_done` empty/stuck/cap (tested); `re_two` exhausts occurrences then returns None; `parse_cpi_period` >4-digit-year + no-yoy positions reject correctly (reasoned).
- **Determinism:** DH test runs `assemble` 51× with fresh per-HashMap random seeds; all bind identically after the event_ticker sort.
- **CRITICAL C2 injection:** adversarial slug `x","count":9999,"x":"\` re-parses to the LITERAL string on both venue builders; injected `count` does not take effect (tested).
- **Clippy/build:** 0 warnings in the 7 target modules; `cargo build` clean; the 4 residual warnings are all in out-of-scope files (main/risk/unwind), pre-existing.
- **Leakage checklist (memory `feedback_combo_id_leak_checklist`):** N/A — no model/calibrator/feature-pipeline code in scope (transport/pricing/discovery only); confirmed no `adaptive_rr`/`calibrator`/`predict_pwin`/`combo_id` touchpoints.

## Launch Recommendation
PROCEED. All 11 fixes land faithfully, 103/103 tests green, in-scope clippy clear, build clean; the 3 INFO items are non-reachable-today or explicitly-shared-with-Python limitations the review already accepted, not regressions.

## Self-review caveat
I authored these diffs, so this pass carries authorship bias — I verified ports against the Python by re-reading the exact regexes/fee fns rather than trusting my own translation, but an independent reviewer should still confirm the two parser rewrites (`parse_cpi_period` leftmost-scan, `parse_release_period` pure-alpha) before the next gated redeploy, since both feed the econ twin-join whose off-by-one history (L21) is the project's most expensive past bug. None of these fixes touch a signed-prereg path (0014 is the allocation rule; this is engine plumbing), so a same-run land is appropriate, but the redeploy gate (0006, owner-greenlit) is the right checkpoint for an independent look.
