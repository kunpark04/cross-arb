---
from: coding-agent (self-review pass)
run_id: 20260615-fill-race
timestamp: 2026-06-15T06:02:00Z
scope_reviewed: [src/exec.rs:806-885, src/config.rs:51-161, src/risk.rs:348, src/live.rs:86-93+238-239+383+454+460-466+503, src/bookkeeping.rs:130-260+346-372+1382-1455, src/exec_log.rs:30-58+131-205]
critical_count: 0
warn_count: 0
info_count: 3
launch_recommendation: PROCEED
self_review: true
cross_references: []
---

## Goal Understanding
Close the 2026-06-15 fill-detection-race incident (one UFC fight fired 4x in ~45s → untracked naked positions). Root cause: both legs rested as GTC limits so "not filled" was a snapshot, not terminal — a resting order fills seconds after the bot reads it unfilled. The 4 fixes pin the already-shipped FOK behavior in tests, add a per-slug entry cooldown to stop churn, route dry-run exec records to a separate log so paper runs don't pollute the live audit trail, and fail closed when the unfilled leg ERR'd (fate unknown — may have filled, so flattening could un-hedge a lock).

## Scope Reviewed
- exec.rs — FOK assertions added to the two payload tests (BUY=FOK, SELL=GTC, both venues); 3 Config literals gained `entry_cooldown_s: 0`.
- config.rs — new `entry_cooldown_s` field; `from_env` default 30 (prod ON); `test_default` literal 0.
- risk.rs — `cfg()` literal gained `entry_cooldown_s: 0`.
- live.rs — `cooldown` map; pre-gate clause; fire-site stamp; outcome-arm re-stamp via `apply_outcome`'s new return; heartbeat prune; `book_snapshot` `live` arg; `live` derived from `backend.label()`.
- bookkeeping.rs — `apply_outcome` → `Option<String>` (Some(slug) for any Entry, None for Unwind/Recovery); `fire_outcome` `live` args; `recover_naked_leg` ambiguous-Err early fail-close; W-1 test acks updated to Ok-resting; new Fix-4 test.
- exec_log.rs — `record(entry, live)`; pure `log_path(live)` resolver; `book_snapshot`/`fire_outcome` `live` params; new path-resolver test.

## Findings

### CRITICAL (must fix before launch)
- None.

### WARN (fix or justify)
- None.

### INFO (optional improvements / simplifications)
- Cooldown inserts (fire-site + outcome-arm) fire even when `entry_cooldown_s==0`; the pre-gate clause then ignores them, and the heartbeat prune keeps the map bounded (<60s). Harmless; spec dictated unconditional inserts. No change.
- Fix 4 is sequenced ahead of the W-1 recovery-in-flight short-circuit (per spec), so a recovery-already-in-flight slug receiving a SECOND naked outcome with an Err leg now halts rather than no-ops. Conservative (cannot leave a naked leg untracked) and bounded by loop-level `pending_entries`/`flattening` dedup. Acceptable.
- exec-log liveness is derived two ways (`backend.label()` for snapshot/outcome; `mode=="live"` for submit) — both keyed on `cfg.mode` (main.rs backend selection) so they agree. Could be unified to one source, but the current derivation is provably consistent.

## Checks Passed
- Safe-by-default preserved: cooldown=0 in every test literal → byte-identical test behavior; from_env default 30 only *blocks* entries (never enables risk); dry-run only changes which LOG FILE it writes, never the trade path.
- Live audit trail unchanged: `log_path(true)` == `CROSSARB_EXEC_LOG` (default `executions.jsonl`); `order_cancel` and a live `order_submit` both route to the live file.
- FOK-killed 404 cancel left BENIGN: zero changes to `cancel`/`cancel_one`/`spawn_cancel`; no halt-on-404 added anywhere.
- No naked leg left untracked: Fix 4 Err path returns false → caller `naked_leg_failclose` engages the kill-switch + logs CRITICAL, keeping the filled leg visible for manual flatten; the reservation is still released by the caller.
- Fix 4 fires ONLY on `Err` (a clean FOK `Ok`-not-filled miss still recovers) — proven by the new contrast test on a priceable book.
- `apply_outcome` returns `Some(slug)` for every Entry resolution path (lock / abort_clean / abort_ambiguous / recover / naked_halt) and `None` for Unwind/Recovery; the return is `Option` (not `#[must_use]`) so the test-harness discard is clean (clippy green).
- Math/units: cooldown compares `as_secs()` u64 vs `entry_cooldown_s` u64 (seconds == seconds); `saturating_mul(2)` avoids overflow.
- `log_path` extension/dotted-directory edges (no-ext append, dotted-dir not split, env override, live-ignores-override) covered by the unit test and green.
- 162/162 tests pass; clippy --all-targets clean.

## Launch Recommendation
PROCEED — both gates green, safe-by-default preserved, all four fixes verified by tests; the only INFO items are conservative-by-design and spec-mandated.

## Self-review caveat
This is the author's own review of a money-path change; authorship bias means I am the least likely to see what a guard silently protected ([L31]). The changes touch the naked-leg recovery path on the live order spine — an INDEPENDENT review (e.g. the llm-council reviewer pass, which globs `*-reviewer.md`) before arming past dry-run is cheap insurance ([L14]/[L31]).
