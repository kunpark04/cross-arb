---
from: independent-loop-refactor-review (adversarial, no authorship stake)
run_id: 20260611-loop-refactor-review
reviewed_commit: 72ac4185cd4e065b78258f889f3d9f0ac5dbd3e5 (HEAD)
scope: bot-rs/src/main.rs (run_live + spawn/outcome helpers), exec.rs (ExecutionBackend &self+Send+Sync);
       cross-checked risk.rs, venue.rs, book.rs, postpone.rs, unwind.rs, types.rs, Cargo.toml
build: cargo test = 112 passed / 0 failed; cargo clippy = 3 warnings (2 doc-list-indent main.rs:6-7,
       1 map_or unwind.rs:30) — all pre-existing/cosmetic, none in the refactored path; smoke clean
verdict: SOUND with caveats (2 WARN robustness defects, 1 disclosed-WARN latent-at-caps, INFO notes).
         The new loop does NOT leak exposure, double-count, stick a slug, or trade a frozen book at the
         staged-rollout caps + 5s staleness backstop. Two robustness gaps below should land before caps
         are raised or before relying on the halt-and-exit alarm.
counts: CRITICAL 0, WARN 3, INFO 3
---

## Verdict: SOUND (no CRITICAL). Exposure balance, in-flight de-dup, lock order, and the safety
## invariants are correct. Two real robustness defects (WARN) + the author's disclosed re-entry WARN.

### 1. EXPOSURE BALANCE — BALANCED. (confirm)
- Reserve-on-spawn (`reserve_exposure` main.rs:541-547) bumps per_pair/per_cluster/total += `cost_per*size`,
  open_positions += 1. Entry both-filled KEEPS it + records (apply_outcome main.rs:623-626). Entry failed
  RELEASES the exact inverse (`release_exposure` main.rs:554-564 — subtracts the same `cost_per*size` from
  the same three buckets, `open_positions.saturating_sub(1)`). Verified exact-inverse; tested
  (`outcome_entry_failed_releases_reservation`, round-trips to 0). No leak, no double-release.
- UNWIND both-filled decrements via `decrement_exposure` (main.rs:638-639 → 710-718) which removes the WHOLE
  per_pair bucket. This equals exactly the kept reservation **iff one position per slug** — the documented
  invariant; correct for the entry→record→unwind path. ENTRY release uses the exact-inverse, UNWIND uses
  remove-bucket — correct asymmetry (the unwind has no surviving `cost_per` to invert, and one-per-slug makes
  the bucket == that position). W14 naked-leg does NOT record a position and DOES release (main.rs:627-633) —
  no bogus full position, no stranded reservation.
- RATCHET (pre-existing, NOT introduced, NOT fixed): a weather/econ position that SETTLES NORMALLY is never
  removed from `positions` and its reservation is never released — there is no settlement reaper (`positions`
  is removed ONLY at main.rs:638, a both-filled unwind; refresh prunes the PAIR, never the POSITION). Over a
  long run, total/per_pair/concurrency ratchet toward the caps. Identical pre- and post-refactor (the original
  E-review INFO flagged it). At 1-contract/$20-total caps + short weather settlement this is slow, not a
  same-session hazard, but stage-2 needs the reaper before any multi-day live run. [WARN, see #3 list]

### 2. STUCK IN-FLIGHT STATE — safe today, but the submit task is UNSUPERVISED with no timeout. (WARN)
- `pending_entries`/`flattening` inserted before spawn (main.rs:473 / 702), removed in `apply_outcome`
  (main.rs:622 / 636) for EVERY outcome kind (both-filled or not), so the normal path always clears.
- **GAP:** the spawned submit task (main.rs:529-535) is detached — its JoinHandle is dropped, NOT in the
  supervisor `select!`. If `submit_pair` ever PANICS, `outcome_tx.send` (main.rs:534) is never reached → the
  slug is stuck in `pending_entries`/`flattening` FOREVER (never re-tradeable / a postponed position never
  re-flattenable), and the panic is silent (no JoinError observed). `outcome_tx.send`'s result is discarded
  (`let _ =`) — fine for a dropped-receiver, but there is no recovery if the task dies before sending.
  NOT reachable in either current backend (DryRunBackend only `println!`s; LiveBackend catches transport
  errors as `Err` and `block_in_place` is sound on the `#[tokio::main]` multi-thread runtime — exec.rs:310-311),
  so it is LATENT. But the de-dup sets have no timeout and the submit task is the one spawned task with no
  supervision — a future backend panic strands a slug invisibly. FIX: supervise the submit JoinHandle (or wrap
  the body so a panic still sends a synthetic failed outcome), and/or add a stale-pending timeout that releases
  the reservation + clears the marker.
- SHUTDOWN drop: outcomes are processed only in a `select!` arm; on `break` (rx closed / supervised death,
  main.rs:335/351-354) any queued `SubmitOutcome` is dropped — a completed fill goes unrecorded. The bot is
  halting (no further trading), so this is a reporting gap, not a safety hole. [INFO]

### 3. SUPERVISION — FIRES on an in-window panic, but an `is_finished()` guard can MASK an out-of-window
###    task death so the halt never fires. (WARN — bounded by the 5s staleness backstop)
- A panicked stream/refresh/poll resolves its `&mut JoinHandle` to `Err(JoinError)`; the arm
  (main.rs:351-354) runs `supervise_fatal` → `halt.store(true)` + `break` (main.rs:505-511). So a panic
  DETECTED while the loop is parked in `select!` correctly HALTS (this is the C1 happy path, and it covers a
  panic mid-poll). Confirmed `supervise_fatal` treats BOTH Ok(()) and Err(JoinError) as fatal.
- **GAP:** every supervisor arm carries `, if !k_handle.is_finished()`. If a collector finishes (panic OR
  clean return) while the loop is executing its BODY (between two `select!` evals — the body has no `.await`,
  but it is non-zero CPU), the NEXT `select!` evaluates the precondition as `false`, DISABLES that arm, and the
  dead task is never consumed/reported. Because pmus's `tx` clone keeps `rx` open (the loop dropped its own at
  main.rs:316), `rx.recv()` never returns `None`, so the loop does NOT break that way either → the dead-task
  arm stays disabled on EVERY subsequent iteration (the task stays finished) → `halt` never fires and the bot
  never exits/alerts. The `is_finished()` guard is also UNNECESSARY: each arm `break`s on fire, so the handle
  is polled at most once — the "poll after completion panics" case the guard defends against cannot occur once
  the arm fires. So the guard only adds the masking race. FIX: drop the `if !*.is_finished()` preconditions.
- BLAST RADIUS BOUNDED: even when the halt is masked, the per-leg staleness gate is a hard backstop — a frozen
  Kalshi/pmus book's `age_s` (book.rs:92/`touch()`) grows and `risk::evaluate` returns `Reject::StaleBook`
  after `max_book_age_s` (5s, risk.rs:111-119). So the loop will NOT trade the frozen book beyond ~5s; what is
  defeated is the halt-and-exit + the CRITICAL alert, not a money-loss path. Hence WARN, not CRITICAL.

### 4. C3 k_fresh GATE — CORRECT. (confirm)
- A pair trades only when EVERY ticker it uses is fresh: `pair.kalshi_tickers().iter().all(|t|
  k_fresh.contains(t))` (main.rs:402-404) — sports needs BOTH A and B (`kalshi_tickers` returns both,
  main.rs:137-143). `k_fresh` is set per Kalshi frame (main.rs:360) and CLEARED on BOTH `Reconnect{Kalshi}`
  (main.rs:378) AND `SeqGap` (main.rs:388). The original C3 (pmus never cleared, traded against a half-rebuilt
  Kalshi book on the first frame) is genuinely fixed: the just-cleared Kalshi book is now blocked until its own
  post-reconnect snapshot restamps `k_fresh`, regardless of the never-cleared pmus book. A stale pmus book is
  independently caught by the 5s staleness gate. RESOLVED, not relocated.

### 5. NEW DEADLOCK / LOCK ORDER — CLEAN. (confirm)
- No lock is held across an `.await` on the loop path. The spawned submit closure (main.rs:529-535) holds NO
  lock — `legs`/`position`/`pair` are owned moves; the `kalshi_books` guard from the quote-build is dropped at
  the block close (main.rs:434) BEFORE `reserve_exposure`/spawn (main.rs:472-474). `exposure`/`pending_entries`/
  `flattening`/`prior_mid`/`pmus_books` are loop-LOCAL (not shared, no lock). `block_in_place` runs on the
  spawned worker with no lock held. New `positions` + in-flight-set touches: in-flight sets are loop-local;
  `positions` is locked one-at-a-time via `lock()` (track_position main.rs:590, apply_outcome main.rs:638,
  spawn_unwind main.rs:688) — never nested with `pairs`/`kalshi_books`. The E-review's clean ordering (pairs →
  k_tracked → pm_tracked, then kalshi_books separately; streams take only tracked|books; poll only positions)
  is preserved. `kalshi_stream` still holds the std book Mutex only across in-memory merges, never `.await`
  (venue.rs:278 allow + drop-before-send at 384/393). No A→B/B→A inversion. `lock()` is poison-tolerant
  (main.rs:488-490) so one panicked task can't cascade-poison the loop.

### 6. REGRESSIONS — none broke. (confirm)
- Dry-run NEVER sends: backend Arc is `DryRunBackend` in DryRun (main.rs:58-61); its `submit_pair` only
  `println!`s (exec.rs:84-91); the spawn path uses the same Arc. Smoke shows only `[DRY-RUN] would submit`.
- Kill-switch blocks entries: `Reject::KillSwitch` (risk.rs:63) + runtime `halt` gate (main.rs:454). Unwinds
  fire under kill-switch by design (reduce-only, spawn_unwind main.rs:689-691).
- Caps bind: evaluate steps 5-6 unchanged; `affordable` now subtracts open exposure (W4, main.rs:909-913,
  tested). Smoke shows 1-ctr cap → size=1.
- `leg_prices_come_from_books_not_edge`: `build_legs`/`plan_legs` unchanged (main.rs:999-1064); test green.
- W14: a non-both-filled outcome with one LIVE (`!simulated`) fill engages halt + does NOT record a position
  (apply_outcome main.rs:627-633, naked_leg_failclose main.rs:654-666); a SIMULATED partial never trips it
  (tested `outcome_naked_live_leg_engages_halt`). dry-run both legs simulated → both_filled true → never trips.
- C6 prev preservation: track_position Occupied keeps `hp.prev` (main.rs:592-599); tested
  (`track_position_preserves_prev_on_retrack`).
- cargo test = 112 / 0; clippy 3 pre-existing; smoke exercises every gate path correctly.

### 7. ORIGINAL E FINDINGS — all RESOLVED, not relocated. (confirm)
- C1 supervision: spawned tasks supervised + halt (main.rs:289-354) — RESOLVED, with the #3 WARN caveat
  (is_finished mask) that the staleness gate backstops.
- C2 (blocking submit stalls unwind arm): submits are SPAWNED off an Arc backend with an outcome channel +
  3rd select! arm (main.rs:343-348, 529-535) — the loop never calls `submit_pair` inline. RESOLVED.
- C3 (poll double-fire / re-expose / partial-naked re-sell): `flattening` set gates re-emits (spawn_unwind
  main.rs:685-687); the bookkeeping moved to the outcome arm. RESOLVED at the intent level for the double-fire;
  the partial-fill leg-state retry is still coarse (re-emit re-sends both legs, coid-deduped) — acceptable for
  reduce-only, noted INFO.
- C4 (track_position clobbers prev): Occupied-keeps-prev — RESOLVED.
- W16 (record-after-prune orphan): refresh won't prune/free a slug with an open HeldPosition (refresh_loop
  `held` filter main.rs:837-845, book-free skips held); AND record now happens on the loop turn. RESOLVED.
- W17 (`pmus_books.get(&slug).unwrap()` panic): now `let Some(pmb) … else { continue }` (main.rs:420).
  RESOLVED.
- C7 sports k_b divergence (risk.rs:145-152), C8 settle-gate consent (main.rs:48-56, banner), C9 truncated-no-
  prune (refresh_loop main.rs:812-815, 838-845), W4 affordable, W5 NaN-fails-closed (risk.rs:90-96), W6
  realized-edge re-check (main.rs:922-941) — all present + tested.

## Disclosed WARN re-confirmed (the author's own, severity correct = WARN, latent at caps)
- RE-ENTRY EXPOSURE STACKING (apply_outcome unwind uses remove-bucket vs additive reserve): a SEQUENTIAL
  re-entry on a slug that already holds a recorded position (the in-flight gate at main.rs:454 only blocks a
  CONCURRENT one) stacks per_pair[slug] = R1+R2 via two reserves, but track_position is keyed by slug so pos2
  OVERWRITES pos1 in `positions` (Occupied branch, main.rs:592-599) → 2 reservations, 1 tracked position;
  the first unwind's `decrement_exposure` then zeroes the whole per_pair bucket (R1+R2) while open_positions
  drops by only 1 → exposure/position accounting desyncs. BLOCKED at the staged-rollout cap (per_pair=$1 ⇒
  `evaluate` pair_cap=0 ⇒ Reject::PairCap on the re-entry), so LATENT. Becomes live only if
  MAX_NOTIONAL_PER_PAIR is raised to admit ≥2 contracts. The real fix is per-position exposure tracking (a
  Vec<position> per slug) — larger than this task; must land before caps are raised past 1 contract/pair.

## Bottom line
The rewritten loop is sound at the staged-rollout caps: exposure reserve/release is exactly balanced (no leak,
no double-count), the in-flight de-dup prevents the double-fire/double-reserve, lock ordering is clean with no
await-under-lock, and every safety invariant (dry-run-never-sends, kill-switch, caps, leg-prices-from-books,
W14 fail-close, C6 prev) survives. It will NOT leak exposure, stick a slug, trade a frozen book (5s staleness
backstop), or miss an IN-WINDOW task death. Two robustness defects to fix before scaling: (a) drop the
`is_finished()` supervisor preconditions — they can mask an out-of-window collector death so the halt never
fires (bounded today by staleness, but it defeats the halt-and-exit alarm); (b) supervise the spawned submit
task / add a stale-pending timeout so a (currently-impossible) backend panic can't strand a slug forever. The
author's disclosed re-entry exposure-stacking WARN is correctly latent at 1-contract caps and must be fixed
before caps rise.
