# Design — SCALE-IN + RE-ENTRY (multi-position-per-slug) for bot-rs

**Status:** design of record (owner-directed build, 2026-06-14). Riskiest money-path change since the
Dutch book — it removes the one-position-per-slug anti-desync guard. **Requires an independent code
review of R1–R4 before arming past dry-run.** Safe-by-default: both feature flags OFF by default, dry-run
unchanged, and the pmus leg still cannot fire without `PMUS_POST_SIGNING_VERIFIED=yes`.

## 0. What this change is

The one-position-per-slug guard (`main.rs:502-506`, the `lock(&positions).contains_key(&slug)` clause) is a
**bookkeeping safety**, not an exposure limit. Today slug→one `HeldPosition` and slug→one per-pair exposure
bucket are 1:1, so `decrement_exposure` can blindly `remove(&pos.market)` the whole bucket on unwind. Relaxing
the guard makes positions **N:1** per slug, so every release must track its OWN contribution exactly.

Definitions (from `scripts/flip_add_backtest.py`):
- **TRUE SCALE-IN** — a bigger SAME-direction arb (`add_net − base_net ≥ tau_gain`) while the base **edge is
  still live**.
- **RE-ENTRY** — a bigger same-direction arb while the base **position is held but its edge already closed**.
Both = "a qualifying add on a held slug." The live loop can't replay episodes; it distinguishes via a cheap
per-frame proxy (§3).

## 1. Position tracking — the core restructure

`positions: HashMap<String, HeldPosition>` → `HashMap<String, SlugPositions>` where (new, in `postpone.rs`):
```
struct SlugPositions { league,date,team_a,team_b: String,   // SHARED game metadata (one per slug)
                       prev: Option<GameStatus>,             // poll status — ONE per slug, never per-position
                       legs: Vec<HeldLeg> }
struct HeldLeg { pos: Position, cost_per: f64, entry_net: f64, entry_dir: Dir }  // cost_per/net/dir STORED per add
```
Rationale: poll metadata + `prev` are game properties (all adds on a slug share them) → poll still does ONE
statsapi match + ONE `prev` update per slug; the C6 "preserve prev" concern becomes structural (prev isn't
touched when appending a leg). A composite `(slug,id)` key was rejected (scatters a slug's positions, splits
`prev` ownership). **Storing `cost_per` per add is what makes exact release possible** — it closes the
`main.rs:960-961` "cost_per not available post-fill" gap.

### Every keyed-on-slug site and its change
| Site | Today | Change |
|---|---|---|
| `main.rs:280` decl | `HashMap<.,HeldPosition>` | `HashMap<.,SlugPositions>` |
| `main.rs:502-506` **entry guard** | `contains_key` blocks all re-entry | qualifying-add logic (§2) |
| `reserve_exposure` 610-616 | `+=` buckets, `open_positions+=1` | **UNCHANGED** — already additive; stacking was always safe |
| `release_exposure` 623-633 | subtract exact `cost_per*size` | **UNCHANGED** — already exact per-attempt inverse |
| `track_position` 641-676 | insert/replace ONE, preserve prev | append `HeldLeg` to `legs`; metadata on first insert; **new param `cost_per`** |
| apply_outcome Entry/filled 702 | `track_position(..,pos)` | `track_position(..,pos,out.cost_per)` — append |
| apply_outcome Unwind/filled 729-732 | `remove(slug)`; `decrement(removed.pos)` | remove the **specific** position; decrement ITS cost_per (§1.3) |
| `spawn_unwind` 940 | `get(slug)` → one pos | unwind WHICH position(s) (§1.5) |
| **`decrement_exposure` 962-970** | `per_pair.remove(market)` whole bucket | **subtract this position's `cost_per*size`** (§1.3) — THE load-bearing line (R1) |
| `refresh_loop` prune 1096 | `positions.keys()` = held? | unchanged: slug held iff `legs.len()≥1` |
| `postpone.rs` poll 190-268 | values()=HeldPosition; prev per slug | values()=SlugPositions; prev read/written once per slug (simpler) |
| ~12 tests | construct old shape | adapt to new shape (part of green baseline) |

`risk.rs:231-233` reads the aggregate exposure buckets, NOT the positions map → no change; it auto-sees the
summed contribution (§3).

### 1.3 Exact release — the desync the guard prevented (R1)
Invariant: **every reserved position is released exactly once for exactly `cost_per*size`.** Reserve (additive)
and per-attempt release (exact inverse) already satisfy this. The ONE change: `decrement_exposure` must stop
removing the whole bucket and instead subtract the specific position's `cost_per*size` (saturating), decrement
`open_positions`. It becomes identical to `release_exposure` — **unify into one `subtract_exposure(exposure,
pos, cost_per)`** so the two paths can't drift. The per-pair bucket reaches ~0 only when the LAST position on
the slug closes; when `legs` empties, drop the slug key (so prune/W16 can fire). **Deleting the whole-bucket
`remove` is the single most important line in the change.**

### 1.4 Naked-leg recovery — unchanged
A half-filled add is recovered from its own `SubmitOutcome` BEFORE it enters `positions` (the Entry/else arm);
existing tracked positions are untouched; the failed add's reservation already released exactly. No change to
`recover_naked_leg`. The per-slug `flattening` serialization between a recovery and an unwind is handled by the
existing `contains` guards (but see R4).

### 1.5 Postpone-unwind must flatten ALL positions on the slug
A postponement voids the GAME → breaks the hedge for every position on the slug. Keep `UnwindRequest` keyed by
slug (poll shouldn't know about adds); `spawn_unwind` flattens every position in `legs`. **v1: one-at-a-time** —
`spawn_unwind` fires the first un-flattened position, the poll's per-cycle re-emit picks up the next (reuses the
proven retry path, zero new state; one poll-cycle latency per extra position, acceptable since postpone is
rare). The Unwind outcome arm removes the SPECIFIC position (by index/id on `SubmitOutcome`) and decrements its
`cost_per` — never the bucket. (Multi-fire with an in-flight count is a noted follow-up.)

## 2. Entry path — relax the guard
`main.rs:502-506`: keep `halt || pending_entries.contains || flattening.contains` EXACTLY (in-flight de-dup,
C5). Only the 4th clause changes:
1. not in `positions` → fresh entry → `evaluate` as today (zero change to the common case).
2. in `positions` (held) → candidate ADD: gate it —
   - the edge `evaluate` would approve must be **same-direction** as the held position(s) AND `edge.net ≥
     max(held entry_net) + add_tau_gain` (parity with `add_events`);
   - classify SCALE-IN vs RE-ENTRY (§3 proxy), require the matching flag ON;
   - require `legs.len() < max_positions_per_slug`;
   - all pass → `evaluate` (notional caps then bound the add); any fail → `continue` (≡ today's block).
Log `[live] ADD(scale-in|re-entry) {slug} base_net=.. add_net=..` so an armed add is auditable.

## 3. SCALE-IN vs RE-ENTRY proxy + caps
Proxy: maintain `edge_live: HashSet<slug>` updated each frame (insert when a same-dir qualifying arb is present,
remove otherwise). At add time: `edge_live.contains(slug)` → SCALE-IN, else RE-ENTRY. Conservative, cheap, maps
to the backtest's "base episode still OPEN." Gate each by its flag; **both off (default) ⇒ step-2 always
continues ⇒ exact current behavior**.

Caps bind UNCHANGED across positions (confirmed in `risk.rs`): `open_positions` counts all positions;
`pair_room = max_notional_per_pair − per_pair[slug]` is the REMAINING room, so `max_notional_per_pair` bounds
the SUM on a slug (the real concentration bound — the guard never did this); cluster/total likewise. NEW
explicit count cap `max_positions_per_slug` (default **1**) checked before `evaluate`. Three independent knobs:
notional/slug, count/slug, notional/cluster.

## 4. Config (`config.rs` struct + `from_env` + `test_default` + the ~6 inline test literals in risk.rs/exec.rs)
| Field | Env | Default |
|---|---|---|
| `enable_scale_in` | `ENABLE_SCALE_IN` | **false** |
| `enable_reentry` | `ENABLE_REENTRY` | **false** |
| `add_tau_gain` | `ADD_TAU_GAIN` | `0.01` |
| `max_positions_per_slug` | `MAX_POSITIONS_PER_SLUG` | **1** |
Reuse existing size caps. Dry-run stays default. Banner prints the new flags when non-default (mirror the
settle-clean loud banner) so an armed config is impossible to miss.

## 5. Safety invariants
1. Exposure never desyncs — exact per-position `cost_per*size` release (R1); unify release/decrement.
2. Recovery correct — half-filled add recovered from its own outcome; tracked positions untouched.
3. Postpone unwinds ALL positions (§1.5), each removed/decremented individually.
4. Single-position case behavior-equivalent — guard step-1 untouched; default flags never admit an add; Vec
   len 1 behaves like today; the existing ~134-test baseline stays green after mechanical test adaptation.
5. Dry-run honored — no execution-path change.
6. Correlated-settlement risk bounded by max_notional_per_pair (dollars/slug) + max_positions_per_slug
   (count/slug) + max_notional_per_cluster (dollars/cluster).

## 6. Test plan (beside the existing position/exposure tests)
1. **Multi-position exact reserve/release** (THE desync regression): reserve A+B on one slug → buckets=A+B,
   open=2; decrement A → buckets=B exactly, open=1, slug kept; decrement B → ~0, slug key gone, open=0.
2. `track_position` appends + preserves shared `prev`.
3. SCALE-IN vs RE-ENTRY proxy classification, each flag independently admits/blocks.
4. `add_tau_gain` gate + same-direction requirement; opposite-direction bigger arb is NOT an add (blocked).
5. `max_positions_per_slug` binds (cap=1 ⇒ default behavior; cap=2 ⇒ 3rd add blocked).
6. Notional caps bind across positions (pos#1 uses full per-pair ⇒ add `Reject::PairCap`); cluster/total too.
7. Recovery picks the right (own) leg with a position already held; existing position's exposure intact.
8. Postpone unwinds ALL positions (over re-emit cycles), each removed+decremented, slug gone only after both.
9. Dry-run honored for an armed-flags add.
10. Single-position regression + full existing baseline green.

## Risks (where desync could reappear)
- **R1 (highest):** the whole-bucket `decrement_exposure` remove. If left, unwinding one of two positions wipes
  BOTH per-pair contributions → surviving position untracked → wrong cap room + double-subtract. Fix: per-position
  `cost_per` subtraction + Test 1. **Focus of the independent review.**
- **R2:** `flattening` is per-slug but a postpone makes multiple unwinds/slug. v1 one-at-a-time + re-emit avoids
  premature re-arm; a position left un-flattened on a void is a live directional bet.
- **R3:** the SCALE-IN/RE-ENTRY proxy isn't episode-replay; can misclassify which FLAG gates an add (not a money
  desync). Conservative proxy + the ADD log tag + Test 3.
- **R4:** recovery↔unwind interleave on one slug serialize through one `flattening` slot; verify a recovery
  whose slot is held by someone else's UNWIND retries rather than treating itself as launched (else a naked add
  leg). Needs explicit review.
- **R5:** prune/W16 — drop the slug key when `legs` empties, else a closed slug stays pinned forever.
- **R6:** an opposite-direction re-entry — the same-direction gate forbids opening one; assert it in Test 4.
- **R7:** the 4 new Config fields must be added to ~6 inline test-config literals or the crate won't compile.
