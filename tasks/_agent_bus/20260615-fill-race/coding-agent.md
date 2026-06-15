---
from: coding-agent
run_id: 20260615-fill-race
timestamp: 2026-06-15T06:01:00Z
task_summary: Implement 4 verified fixes for the 2026-06-15 fill-detection-race incident in bot-rs (FOK payload tests, per-slug entry cooldown, separate dry-run exec log, ambiguous-Err leg fail-close).
files_changed: [src/exec.rs, src/config.rs, src/risk.rs, src/live.rs, src/bookkeeping.rs, src/exec_log.rs]
files_created: []
context_loaded: [CLAUDE.md, tasks/lessons.md, bot-rs/README.md, src/exec.rs, src/config.rs, src/exec_log.rs, src/bookkeeping.rs, src/live.rs, src/risk.rs, src/pair.rs, src/test_support.rs, src/main.rs]
libraries_consulted: []
self_review_artifact: tasks/_agent_bus/20260615-fill-race/code-logic-reviewer.md
---

## What I changed
- (1) FOK payload tests — exec.rs: `live_payload_is_well_formed_*` now asserts the entry BUY Kalshi body has `time_in_force:fill_or_kill` and a SELL has none; `pmus_payload_is_well_formed` asserts `tif==TIME_IN_FORCE_FILL_OR_KILL` on BUY (YES+NO) and `TIME_IN_FORCE_GOOD_TILL_CANCEL` on SELL (LONG+SHORT). exec.rs:806-812, 869-885.
- (2) Per-slug entry cooldown — config.rs: new `entry_cooldown_s: u64`, `env_u64("ENTRY_COOLDOWN_S", 30)`, `0` in all 5 test literals (config test_default, risk cfg, exec.rs ×3). live.rs: `cooldown: HashMap<String, Instant>` loop state, a pre-gate clause, a fire-site stamp, an outcome-arm re-stamp, and a heartbeat prune. bookkeeping.rs: `apply_outcome` now returns `Option<String>` (`Some(slug)` for any Entry resolution, `None` for Unwind/Recovery); live.rs applies the returned slug to the cooldown.
- (3) Separate dry-run exec log — exec_log.rs: `record(entry, live)` + a pure `log_path(live)` resolver (LIVE unchanged → `CROSSARB_EXEC_LOG`; DRY-RUN → `CROSSARB_EXEC_LOG_DRYRUN` or `.dryrun` inserted before the extension / appended when no extension). `book_snapshot`/`fire_outcome` gained a `live` param; callers in live.rs (`backend.label()!="dry-run"`) and bookkeeping.rs (same) pass it; `order_submit` derives `mode=="live"`; `order_cancel` passes `true`.
- (4) Ambiguous-leg fail-close — bookkeeping.rs `recover_naked_leg`: after `resting_ack`, before the W-1 flatten match, `if resting_ack.is_err()` logs CRITICAL + `return false` (caller halts), because under FOK an Err (not a clean `Ok` miss) means the order's fate is unknown and it may have FILLED → flattening could un-hedge a real lock.
- Tests: added `exec_log::log_path_separates_dry_run` and `bookkeeping::errd_unfilled_leg_fails_closed_even_with_priceable_book`; updated the W-1 regression's acks from `Err(RateLimited)` to a clean `Ok`-resting leg so it still exercises the W-1 unwind/recovery-slot distinction (an Err now short-circuits via Fix 4 first — the realistic FOK-miss is `Ok`-not-filled).

## Why (non-obvious only)
- Fix 4 is sequenced BEFORE the W-1 flattening match (per spec), so an Err unfilled leg now fail-closes ahead of the recovery-in-flight short-circuit. That made the W-1 test's `Err` acks no longer reach the W-1 branch; I switched them to `Ok`-but-resting (the genuine clean-FOK-miss shape) to keep the W-1 regression meaningful. This is a test-realism update driven by the new (correct) semantics, not a workaround.
- `live` for the exec-log split is derived once from `backend.label()` (DryRunBackend → "dry-run"), which is keyed on `cfg.mode` in main.rs — so it tracks the execution mode exactly and is consistent across `order_submit`/`book_snapshot`/`fire_outcome`.

## Verification
- `cargo test --bin cross-arb-bot` → 162 passed, 0 failed.
- `cargo clippy --all-targets` → clean (0 warnings).
