---
from: coding-agent (self-review pass)
run_id: 20260611-unwind-trigger
timestamp: 2026-06-11T12:45:51Z
scope_reviewed: [bot-rs/src/postpone.rs:1-end, bot-rs/src/main.rs:209-265, bot-rs/src/main.rs:316-470, bot-rs/src/main.rs:632-760, bot-rs/src/config.rs:44-47, bot-rs/src/discovery.rs:119-124]
critical_count: 1
warn_count: 2
info_count: 2
launch_recommendation: PROCEED WITH FIXES
self_review: true
cross_references: [tasks/lessons.md, scripts/probe_mlb_postpone.py]
---

## Goal Understanding
Arm the live MLB postponement-unwind trigger: track held cross-arb positions, port `probe_mlb_postpone.py`'s
`unwind_trigger` faithfully (incl. the L3 event_date-not-officialDate gap), poll statsapi for postponements,
and FIRE the SELL-both-legs unwind that already exists + is tested in `unwind.rs`. Compile + unit-test only;
no live HTTP on a test path; reduce-only + dry-run-safe + disengageable.

## Scope Reviewed
- `postpone.rs` — detector (`snap`, `detect_postponement`, `days_between`) + poll (I/O). The parity gate.
- `main.rs` — positions map, `track_position`/exposure bump, `tokio::select!` rework, `handle_unwind`, exit
  pricing helpers, `decrement_exposure`, the smoke live-path demo, + 3 new tests.
- `config.rs`/`exec.rs`/`risk.rs` — `postpone_poll_s` + `auto_unwind` (struct + all Config literals).
- `discovery.rs` — `iso_date`/`pm_league` `pub(crate)`.

## Findings

### CRITICAL (must fix before launch)
- Busy-loop when `auto_unwind = false`
  - Location: bot-rs/src/main.rs (the `tokio::select!` loop + the `drop(unwind_tx)` I originally wrote)
  - Issue: when `auto_unwind=false` the poll task is never spawned, so no clone of `unwind_tx` exists. Dropping
    our `unwind_tx` (as I first did) closes `unwind_rx`; its `select!` arm then resolves to `None` every poll
    iteration and the `continue` spins the loop hot (100% CPU), starving the venue-event arm.
  - Why it matters: the bot would peg a core and effectively stop trading whenever the unwind trigger is
    disengaged — a silent degradation of the whole live loop, not just the unwind path.
  - Suggested fix: hold `unwind_tx` alive for the loop's lifetime so `recv()` PARKS when idle instead of
    returning `None`. The loop still exits on the venue `rx` closing.
  - Status: self-resolved within run (`let _unwind_tx_keepalive = unwind_tx;`, with an explanatory comment).

### WARN (fix or justify)
- Re-entry on the same slug double-accumulates per_pair notional vs a single `decrement_exposure`
  - Location: bot-rs/src/main.rs `track_position` / `decrement_exposure`
  - Issue: a second both-filled fill on the SAME slug (without an intervening unwind) accumulates into
    `per_pair[slug]` and `open_positions` (+1 each) but overwrites the single `HeldPosition`; a later
    `decrement_exposure` removes the FULL accumulated per_pair[slug] and decrements `open_positions` by only 1.
  - Justification (not fixed — bounded by an existing gate): `evaluate` applies `max_notional_per_pair` BEFORE
    a second fill; once `per_pair[slug]` reaches the cap (default $1 in staged rollout) the next attempt is
    `Reject::PairCap`. So re-entry on one slug is gated out by the very cap `track_position` makes bind.
    Per-position notional tracking is out of this task's scope ("BUMP exposure so caps bind"). Documented here.
- `decrement_exposure` reconstructs notional from the per_pair bucket, not from cost_per×size
  - Location: bot-rs/src/main.rs `decrement_exposure`
  - Issue: it removes `per_pair[market]` wholesale and subtracts that from total + the shared cluster bucket.
  - Why acceptable: there is exactly one `HeldPosition` per pmus slug (HashMap keyed by slug), and
    `track_position` added the identical value to per_pair, per_cluster, and total — so removing the per_pair
    value is exact for all three (cluster clamped at 0 to be safe with multi-position clusters). Tested by
    `track_and_decrement_exposure_round_trips` (round-trips to zero). No fix needed; noted for the reader.

### INFO (optional improvements / simplifications)
- Simplified the `orig` fallback chain (removed a convoluted `.map(Some).unwrap_or(None)` no-op) and dropped a
  redundant `Some(p)` binding (`let _ = p;`) in the officialDate-move branch — both verified still Python-faithful
  (prev-None and prev-without-officialDate both fall through identically). Applied (trivially safe, in-scope).
- Fixed one clippy `doc_list_items` overindentation in `postpone.rs`; left the 5 pre-existing lints
  (matcher.rs too-many-args, unwind.rs/risk.rs `map_or`, main.rs:6-7 doc) untouched to avoid scope creep.

## Checks Passed
- PARITY: every Python `_selftest` vector ported and asserted — postponed-5d UNWIND, live officialDate-moved-104d
  UNWIND (the L3 trap), makeup-next-day WATCH, no-makeup-date UNWIND, suspended-resumes-next-day WATCH, cancelled
  UNWIND, the 5 normal-lifecycle None states, first-sight-already-postponed with/without event_date, officialDate
  slid-1day WATCH vs slid-a-week UNWIND. The composed `detect_postponement -> should_unwind` reproduces
  `unwind_trigger`'s UNWIND/WATCH/None on each.
- L3 invariant: gap measured from `event_date` (bound pm slug date), never officialDate — asserted by the two
  vectors that pin `reschedule_in_days` (5.0 and 104.0) where officialDate had already moved to the makeup date.
- POSTPONE_STATES substring semantics ("Cancelled: Rain" matches), Cancelled->unwind (no target), no-target->unwind,
  unparseable-gap->None(unwind), first-sight prev=None — all ported and tested.
- `days_between` trims a datetime suffix to 10 chars (the non-trivial port case — pattern_regex_port_quantifier_truncation
  cross-ref) and is tested on `...T17:10:00Z` inputs + a signed/negative case + malformed -> None.
- Exit pricing direction: SELL YES -> `yes_bid`, SELL NO -> `1 - yes_ask`; one-sided book -> declines to price the
  pair (hold). Tested both legs + the missing-side cases.
- Reduce-only policy: `handle_unwind` fires under the kill-switch (logs `[UNWIND] kill-switch engaged but flattening
  (reduce-only)`); entries still gated by `evaluate`'s kill-switch reject. Dry-run backend only LOGS.
- No-network-on-test: the poll (`poll_mlb_postponements`, `get_json`) is never referenced by any test; all parity +
  match tests run on embedded JSON / pure functions. `cargo test` = 98 passed.
- No safety gate weakened: `unwind.rs` untouched (reused `Postponement`/`should_unwind`/`unwind_orders`); rustls-only
  (no new deps; statsapi reuses the existing reqwest rustls client). Idempotent `unwind-…` coids prevent double-flatten.
- Smoke offline composition confirmed via `cargo run -- --smoke` (prints reschedule_in_days=5.0 -> UNWIND + 2 SELLs).

## Launch Recommendation
PROCEED WITH FIXES
The one CRITICAL (busy-loop) is self-resolved within this run; the two WARNs are documented and bounded by existing
caps. The detector matches the Python `_selftest` parity gate including the L3 event_date trap, and the live path is
reduce-only + dry-run-safe + disengageable.

## Self-review caveat
This is authorship self-review and carries authorship bias. The change arms a LIVE order-firing path (the SELL-both
unwind); although it is dry-run-by-default and the owner runs live submission in their own environment, an independent
review of the `handle_unwind` exit-pricing + exposure bookkeeping before the first PROD-armed run is warranted, and the
poll's live statsapi field semantics (away/home id resolution, schedule envelope shape) should be confirmed on a real
postponed game on the droplet (they are ported from the verified probe but never exercised against live JSON here).
