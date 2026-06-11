---
from: coding-agent
run_id: 20260611-engine-fix-B
timestamp: 2026-06-11T14:29:08Z
task_summary: Concurrency-core (spawn/ack submit path) + gate fixes on the bot-rs live order path from the adversarial engine review.
files_changed: [bot-rs/src/main.rs, bot-rs/src/risk.rs, bot-rs/src/exec.rs]
files_created: []
context_loaded: [tasks/_agent_bus/20260611-engine-review/B-risk-config.md, tasks/_agent_bus/20260611-engine-review/E-loop-concurrency.md, bot-rs/src/main.rs, bot-rs/src/risk.rs, bot-rs/src/config.rs, bot-rs/src/exec.rs, bot-rs/src/types.rs, bot-rs/src/venue.rs, bot-rs/src/postpone.rs, bot-rs/src/unwind.rs, bot-rs/src/book.rs, bot-rs/src/signal.rs, bot-rs/src/ledger.rs, bot-rs/src/discovery.rs, bot-rs/Cargo.toml, CLAUDE.md, tasks/todo.md, ~/.claude/agent-memory/coding-agent/MEMORY.md]
libraries_consulted: []
self_review_artifact: tasks/_agent_bus/20260611-engine-fix-B/code-logic-reviewer.md
---

## What I changed

Scope held to 3 of the 5 allowed files; `config.rs` + `types.rs` needed no edits (C8 reads env in
`main.rs` mirroring the existing prod gate; the new loop types live in `main.rs`). The 9 sibling-pass
files were NOT touched.

**Core refactor (C4+C5+W14+W16) — `exec.rs` + `main.rs`:**
- `ExecutionBackend` trait: `submit_pair`/`cancel` now take `&self` (impls never needed `&mut`); trait is
  `Send + Sync`; `main.rs` holds it as `Arc<dyn ExecutionBackend>` (`exec.rs:50-60`, `main.rs:~49-72`).
- All submissions are SPAWNED + ACKED: `run_live` `tokio::spawn`s `submit_pair` off a cloned `Arc<backend>`
  (`spawn_submit`, `main.rs:~520`) and a third `select!` arm on `outcome_rx` does all bookkeeping
  (`apply_outcome`, `main.rs:~594`). The loop NEVER calls `submit_pair` inline → the network RTT can't
  block the unwind/outcome arms.
- In-flight de-dup: `pending_entries` + `flattening` HashSets gate re-fires (`main.rs:~452-456`, spawn sites).
- Exposure RESERVED on spawn (`reserve_exposure`); on outcome kept (record) or released via the exact-inverse
  `release_exposure` (`main.rs:~559`) — NOT the remove-whole-bucket `decrement_exposure`, so a release can't
  wipe a co-resident position's reservation.
- W14 naked-leg fail-close (`naked_leg_failclose`, `main.rs:~637`): a non-both-filled outcome with one LIVE
  (non-simulated) fill engages a runtime `AtomicBool` halt that blocks all new entries; dry-run (simulated)
  never trips it.

**Other loop fixes — `main.rs`:**
- C3 per-pair `k_fresh` freshness gate (cleared on Kalshi Reconnect/SeqGap, set per Kalshi frame; pair trades
  only when every ticker it uses is fresh) — backstops the coarse `stream_paused` which still clears on first frame.
- C6 `track_position` preserves an existing `HeldPosition.prev` (match `Occupied`/`Vacant`) instead of clobbering.
- W16 `refresh_loop` does not prune (nor free the Kalshi book of) a slug with an open `HeldPosition`
  (added `positions` param).
- W17 `pmus_books.get(&slug).unwrap()` → `let Some(pmb) … else { continue }`.
- C1 supervisor: spawned stream/refresh/poll `JoinHandle`s are awaited in `select!` arms; any termination
  ⇒ runtime halt + break (`supervise_fatal`). Loop-path locks use a poison-tolerant `lock()` helper.

**Gate fixes — `risk.rs` + `main.rs`:**
- C7 (`risk.rs` step 3b): sports `k_b` away-team divergence — reject if `|k_b.mid() − (1 − pm.mid())|·100 >
  mid_divergence_reject_cents`. New test `rejects_divergent_uncrossed_away_team_book`.
- C8 (`main.rs` startup + banner): live+prod with `!require_settle_clean` REFUSES to start unless
  `CROSSARB_I_UNDERSTAND_NO_SETTLE_GATE=yes`; banner shouts `*** SETTLE-CLEAN GATE DISABLED ***`. Verified both
  refuse (exit 2) and consent-unlock (exit 0) live.
- C9 (`refresh_loop`): a `fresh.truncated` catalog skips the prune step (still applies adds) + logs.
- W4 (`affordable`): subtracts `exposure.total` from the total-notional cap.
- W5 (`risk.rs` proximity): `if let Some(d) … { if !d.is_finite() || d > max { TooEarly } }` — NaN fails CLOSED;
  also clears the `map_or` clippy lint. New test `nan_days_to_event_fails_closed`.
- W6 (`realized_edge_clears_floor`, `main.rs`): recompute realized net edge from the ROUNDED leg prices (same
  marginal-fee model as `signal`/`game_signal`); skip the fire if under the floor or cost ≥ 100c. New test.

## Why (non-obvious only)
- `block_in_place` inside `tokio::spawn` is sound (multithread runtime from `features=["full"]`) and parks only
  the spawned worker, not the loop — that is the whole point of the spawn.
- C8 needed no `config.rs` field: it is an env-consent gate exactly like the existing `CROSSARB_I_UNDERSTAND_PROD`
  check, read in `main()`; adding a config field would have been speculative surface.
- `release_exposure` (exact inverse) vs `decrement_exposure` (remove-bucket): the spec says "RELEASE the
  reservation" — releasing the exact reserved `cost_per·size` is correct under re-entry stacking; remove-bucket
  only coincidentally equals it for a single position.

## Verification
- `cargo test`: **112 passed / 0 failed** (baseline 103; +9 regression tests across C7, W5, W6, C6-retrack,
  W4-affordable, and 4 `apply_outcome` core tests).
- `cargo clippy`: 3 warnings, ALL pre-existing + out of scope (`main.rs:6-7` module-doc-header indent;
  `unwind.rs:29` `map_or`). Zero NEW warnings; cleared the `risk.rs:88` lint the review flagged.
- `cargo run -- --smoke`: all gate paths correct (settlement reject, weather approve+fire, toxicity reject,
  proximity reject, sports approve+fire, postponement unwind). C8 refuse/unlock both verified with env toggles.
