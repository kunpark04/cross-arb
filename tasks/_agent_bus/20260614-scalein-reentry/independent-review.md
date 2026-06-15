---
from: independent-reviewer (adversarial, did NOT author the code)
run_id: 20260614-scalein-reentry
timestamp: 2026-06-14T09:52:00Z
commit_reviewed: 8164710 (feat: SCALE-IN + RE-ENTRY, multi-position-per-slug)
scope: bot-rs exposure-desync / silent-naked-leg axis (R1–R7) before arming past dry-run
tests: 146 passed / 0 failed; cargo clippy --all-targets clean; smoke silent at defaults, loud-banner when armed
adversarial_probes_run: 3 (R4-A halffill-add-while-unwind, R4-B unwind-while-recovery, R1 interleaved unwind+refill) — written, run, then REVERTED
critical_count: 0
warn_count: 2
info_count: 2
verdict: SAFE TO ARM — with ONE pre-arm fix STRONGLY RECOMMENDED (W-1) before max_positions_per_slug >= 2
self_review_cross_ref: tasks/_agent_bus/20260614-scalein-reentry/code-logic-reviewer.md
---

## Bottom line

**SAFE TO ARM past dry-run — NO BLOCKER on the R1 exposure-desync axis (the stated focus).** The whole-bucket
`per_pair.remove` is gone, the unified `subtract_exposure` is the exact per-position inverse of
`reserve_exposure`, and every close path (unwind both-filled, entry non-fill release, recovery) subtracts
exactly one leg's `cost_per*size` using the leg's OWN stored value. I tried hard to corrupt a stacked slug's
exposure by interleaving unwind + re-fill with distinct cost_pers and could not — it stays exact to zero.

**One real defect found (W-1, the flagged R4): an armed scale-in/re-entry can leave a SILENT NAKED LEG** under a
specific outcome ordering. It is NOT a blocker for the SHIPPED config (both flags off + cap=1 ⇒ no add ever
spawns ⇒ the path is unreachable, byte-identical to today), but it IS reachable the moment the owner sets
`ENABLE_SCALE_IN`/`ENABLE_REENTRY` with `MAX_POSITIONS_PER_SLUG >= 2` — which is exactly what this review gates.
**Fix W-1 before arming a flag with cap >= 2.** Arming with cap kept at 1 is pointless (no add admitted), so in
practice W-1 should be fixed before any meaningful arming.

---

## What I verified (and how)

### R1 — exact exposure release: CONFIRMED EXACT (no blocker)
- The whole-bucket remove is GONE: `decrement_exposure` deleted; `git grep per_pair.remove` → 0 hits in src. The
  Unwind both-filled arm (`main.rs:836-849`) now pops `legs.remove(0)` and calls
  `subtract_exposure(exposure, &removed.pos, removed.cost_per)`.
- `subtract_exposure` (`main.rs:677-695`) is the EXACT inverse of `reserve_exposure` (`main.rs:668-675`):
  `notional = cost_per * pos.size`, saturating-subtracted off per_pair/per_cluster/total, `open_positions -= 1`.
  Both release paths (entry non-fill `main.rs:817`; unwind `main.rs:840`) call the SAME fn with the SAME
  `(pos, cost_per)` the reservation used → cannot drift. This is exactly the unify-inverse-paths fix.
- Stacked 2-position trace (reserve A+B → unwind A → unwind B), DISTINCT cost_pers 0.90/0.95: after A the
  buckets equal B *exactly* (not 0 — the old desync; not A — a swap), slug kept, open=1; after B ~0, slug key
  dropped, open=0. The committed `test1_*` proves this; I additionally ran an interleaved
  unwind-one-then-refill-append probe with three distinct costs (0.90/0.93/0.97) and exposure stayed exact at
  every step and to zero at the end. **I could not construct a stacked-unwind that corrupts the survivor.**
- `track_position` (`main.rs:704-738`) APPENDS a `HeldLeg`, stores `cost_per/entry_net/entry_dir`, sets game
  metadata only on the Vacant (first) insert, and never touches `prev` on append → C6 preservation is now
  structural. Verified `postpone.rs:172-189` (`SlugPositions`/`HeldLeg`).

### R4 — recovery ↔ unwind interleave on one slug: ONE REAL HOLE (W-1 below)
- Architecture is correct and is the load-bearing safety: the whole `tokio::select!` (`main.rs:388-404`) is a
  SINGLE task. `spawn_unwind`, `apply_outcome` (recovery launch + unwind pop), and the entry/add path all run on
  the loop's turn, mutating `flattening`/`positions`/`exposure` single-threaded; spawned tasks do ONLY I/O +
  send the outcome back. So there is no data race on the accounting state.
- `legs[0]` stability between spawn and outcome: appends push to the BACK (`main.rs:736`), the entry/add path
  refuses while `!flattening.contains` (`main.rs:539`), and a 2nd `spawn_unwind` is refused by the same
  `flattening` guard (`main.rs:1055`). So the front leg the unwind targets is unchanged when its outcome lands —
  the pop is the just-flattened leg, subtracted by its OWN cost_per. CONFIRMED.
- R4-B (unwind request arrives while a recovery holds the slot): `spawn_unwind` returns early on
  `flattening.contains` → it does NOT pop a held leg out from under the recovery, does NOT fire a competing
  flatten. Probe-confirmed.
- **R4-A — THE HOLE (W-1).** When a flag is armed AND cap >= 2, an ADD on a held slug can be in flight
  (`pending_entries`) when a postpone fires. `spawn_unwind` does NOT check `pending_entries` (only `flattening`),
  so it spawns an unwind of the BASE leg (`legs[0]`) and marks `flattening`. If the ADD's outcome then lands
  HALF-FILLED *before* the unwind's outcome (`apply_outcome` Entry/else, `main.rs:813-825`):
  `recover_naked_leg` sees `flattening.contains(slug)==true` and returns `true` at `main.rs:925-927`
  ("recovery is already underway; treat as launched") — so it fires NO flatten for the add's filled leg AND the
  `naked_leg_failclose` backstop does NOT run (it's only on the `false` branch). But the in-flight flatten is
  the UNWIND of the BASE leg — it does NOT cover the add's just-naked leg. The unwind outcome later pops
  `legs[0]` (the base), leaving **the add's filled leg as a live, unhedged directional position with no
  scheduled flatten and no halt.** There is no later naked-leg sweep (`grep` confirms recovery is the only
  reconciliation). My R4-A probe reproduced the abandon-state: `spawned_recovery=false, halted=false,
  flat_still=true` with the add's filled leg unrecovered.
  - Reachability: UNREACHABLE at shipped defaults (cap=1 ⇒ `qualifying_add` returns None ⇒ no add spawns).
    NEWLY introduced by removing the one-position guard (previously a held slug could never have an entry in
    flight, so an unwind could never race an add). Reachable only with a flag ON and cap >= 2.

### R2 — postpone flattens ALL positions: CONFIRMED
- `poll_mlb_postponements` sends ONE `UnwindRequest` per slug (`postpone.rs:287-289`); `spawn_unwind` flattens
  the FRONT leg one-at-a-time and the per-cycle re-emit picks up the next; the Unwind outcome arm removes the
  SPECIFIC front position and decrements ITS cost_per, dropping the slug key only when `legs` empties.
  `test8_*` drives three stacked legs to zero over three re-emit cycles; my interleaved probe corroborated the
  exposure stays exact. Caveat carried from the self-review (W-2): a HALF-filled unwind re-fires the SAME front
  leg's two SELLs (idempotent Kalshi coid dedups; pmus relies on `halt`). Pre-existing; larger surface now.

### Safe-by-default: CONFIRMED both directions
- At defaults (`enable_scale_in=false, enable_reentry=false, max_positions_per_slug=1`):
  `qualifying_add` (`main.rs:751-776`) fails the count-cap clause (`held.len() 1 >= cap 1`) AND the flag clause,
  so it ALWAYS returns None for a held slug ⇒ the loop `continue`s — byte-identical to the old `contains_key`
  block. `test10_*` + the 146-green baseline confirm. The cap comparison is `>=` (`main.rs:291`) so cap=1 blocks
  the first add (named-as-passing, not borderline — the >= framing is correct, matches the tau gate which admits
  on `>=`, `test4_*`).
- `cargo run -- --smoke` at defaults prints NO `ADD-TO-HELD ARMED` banner; setting `ENABLE_SCALE_IN=true
  MAX_POSITIONS_PER_SLUG=2` prints the loud banner. The arming is impossible to miss.

### Other axes checked (clean)
- **Cap bypass across stacked positions: NONE.** `evaluate` computes `pair_room = max_notional_per_pair −
  per_pair[market]` (`risk.rs:231`); the held leg's reservation stays in `per_pair[market]`, so the cap bounds
  the SUM, not each add. `max_concurrent_positions` (`risk.rs:219`) binds on `open_positions`, which counts every
  leg. `test6_*` → PairCap/ClusterCap; verified the arithmetic independently.
- **Opposite-direction re-entry: BLOCKED.** `qualifying_add` clause 1 requires `all(entry_dir == edge.dir)`;
  `same_dir_live` also requires it. `test4_*` pins an opposite-dir 0.20 arb → None.
- **Leaked/never-released reservation: NONE found.** Every reserve has exactly one subtract (both-filled keep;
  non-fill release; unwind pop). The failed-add path releases ONLY the add's reservation, base intact
  (`test7_*` + my R4-A probe both confirm base per_pair/total untouched).
- **R5 prune pinning a closed slug: CORRECT.** Unwind arm drops the slug key when `legs` empties
  (`main.rs:842-846`); refresh prune treats `positions.keys()` as the held set (`main.rs:~1207`). `test1_/test8_`
  assert the key vanishes only after the last leg. (Note: W-1's abandoned naked leg, once the base leg's slug
  key drops, could let that leg's book be pruned too — a compounding symptom, not the root.)

---

## Findings

### CRITICAL (must fix before arming)
- None on the R1 axis. (W-1 is CRITICAL-severity behavior but is gated unreachable at the shipped config, so it
  is not a blocker to *ship/keep dry-run*; it IS a must-fix before arming a flag with cap >= 2.)

### WARN
- **W-1 (R4) — armed scale-in/re-entry can leave a silent naked leg under one outcome ordering.**
  - Location: `main.rs:925-927` (`recover_naked_leg` returns `true` on `flattening.contains`) vs
    `main.rs:1055-1059` (`spawn_unwind` ignores `pending_entries`) and `main.rs:813-825` (Entry/else recovery).
  - Issue: an UNWIND in flight (which targets only `legs[0]`) makes a concurrently-arriving half-filled ADD
    skip recovery AND skip the halt backstop, abandoning the add's filled leg.
  - Why it matters: a live unhedged directional position with no flatten and no halt — exactly the silent-naked
    class this whole subsystem exists to prevent. Money risk once armed.
  - Suggested fix (pick one): (a) in `recover_naked_leg`, only treat `flattening` as "already covered" if the
    in-flight flatten is a RECOVERY for THIS leg; if it is an UNWIND (or any flatten not covering this filled
    leg), DO NOT return `true` — fall through to fail-close `halt` so the naked add leg surfaces. (b) Make
    `spawn_unwind` refuse (or defer) while `pending_entries.contains(slug)`, so an unwind never races an
    in-flight add on the same slug. (c) Distinguish the `flattening` slot per-purpose (recovery vs unwind) so the
    recovery path knows its leg is uncovered. Option (a) is the smallest and most defensive; add a regression
    test = my R4-A probe asserting `halt==true` (not the current abandon).
  - Status: REPORTED (not fixed — review-only; the probe that exposed it was reverted).
- **W-2 (R2, pre-existing) — half-filled unwind re-fires the same front leg's SELLs; surface grows with stacked
  legs.** Location: `main.rs:850-855`. Carried from the self-review; idempotent Kalshi coids + `halt` bound it.
  The design's "multi-fire with an in-flight count" follow-up would address it. Not introduced here.

### INFO
- The `add_tag` log path (`main.rs:573-578`) and the loud arming banner (`main.rs:121-127`) are good
  auditability; keep them.
- R3 proxy frame-lag (self-review WARN): I concur it only selects WHICH flag gates an add, never a money/
  exposure desync, and is inert at defaults. Acceptable; no action needed for the R1 axis.

---

## Checks Passed (explicit)
- R1 exact per-position release, including DISTINCT-magnitude stacked unwind and an interleaved unwind+refill —
  exact to zero, no cross-contamination.
- Whole-bucket `per_pair.remove` deleted (grep-confirmed) and replaced by the unified saturating subtract.
- `legs[0]` front-pop stability between spawn and outcome (back-append + `flattening` serialization + single
  loop task).
- Safe-by-default: zero adds admitted at defaults (cap clause + flag clause), smoke byte-silent, loud banner
  when armed; 146 tests green; clippy clean.
- Caps bind across stacked positions (per-pair/cluster/total room is the remaining amount; concurrency counts
  every leg). Opposite-direction add blocked. Failed-add releases only its own reservation. R5 slug-key drop
  exact.

## Adversarial probes (written, run, REVERTED — tree is clean, 146 tests restored)
1. `probe_r1_unwind_one_then_refill_append_exposure_exact` — PASS (exposure exact through interleave).
2. `probe_r4b_unwind_request_while_recovery_in_flight` — PASS (unwind refused; held leg not popped).
3. `probe_r4a_halffilled_add_outcome_while_unwind_in_flight` — exposed W-1 (add's filled leg abandoned:
   spawned_recovery=false, halted=false). This is the recommended regression test once W-1 is fixed (flip the
   assertion to `halt==true`).

## Authorship note
I did not author this code; this is an independent adversarial pass. The R1 desync axis is genuinely closed and
I am confident shipping/keeping dry-run is safe. W-1 is the one residual money-path hole and is precisely the
risk the implementing agent flagged as R4 — it is real, reproduced, and should be fixed before arming a flag
with `MAX_POSITIONS_PER_SLUG >= 2`.
