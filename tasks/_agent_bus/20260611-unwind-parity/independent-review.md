---
from: independent-adversarial-reviewer (no authorship stake)
run_id: 20260611-unwind-parity
commit: 45a6115
timestamp: 2026-06-11
scope: bot-rs/src/postpone.rs, bot-rs/src/unwind.rs, bot-rs/src/main.rs (handle_unwind / track_position / position_from_intents / exit pricing), bot-rs/src/config.rs, bot-rs/src/exec.rs, bot-rs/src/risk.rs vs scripts/probe_mlb_postpone.py
verdict: FAITHFUL + SAFE (2 INFO notes, no CRITICAL, no WARN that blocks)
---

## Verdict: FAITHFUL + SAFE — no issue can make the trigger miss a real unwind or fire a harmful one.

Empirical gate: Python `--selftest` PASS; `cargo test` = **98 passed / 0 failed**; postpone module 15/15 incl. the L3
regression. The composed `detect_postponement -> should_unwind` reproduces `unwind_trigger`'s UNWIND/WATCH/None on
every `_selftest` vector. The exit-firing path is reduce-only, dry-run-safe, idempotent, and strictly gated on a
detected POSTPONE_STATE / officialDate-move.

---

## 1. DETECTOR PARITY — CONFIRMED FAITHFUL (incl. the L3 trap)

Each Python decision branch has an exact Rust counterpart and a passing test:

| Vector | Python (`unwind_trigger`, py:85-103) | Rust (`detect_postponement`, postpone.rs:107-156) + `should_unwind` | Test |
|---|---|---|---|
| POSTPONE_STATES | `("Postponed","Suspended","Cancelled")` py:28, substring `any(s in cs)` py:88 | `["Postponed","Suspended","Cancelled"]` :28, `is_postpone` substring :84-86 | normal_lifecycle_is_none, cancelled_unwinds |
| Cancelled → None gap | `if "Cancelled" in cs: return "UNWIND"` py:95-96 (no target) | `if cs.contains("Cancelled") { reschedule_in_days: None }` :143-145 | cancelled_unwinds (asserts `None`) |
| Suspended→resume / Postponed→reschedule | `cur.resumeDate if "Suspended" else cur.rescheduleDate` py:97 | `if cs.contains("Suspended") { resume_date } else { reschedule_date }` :148 | suspended_resumes_next_day_watches |
| no target → unwind | `if not target: return "UNWIND"` py:98-99 | `let Some(target)=target else { …None }` :149-151 | postponed_no_makeup_date_unwinds |
| officialDate moved, no status flip | py:88-93 (gap `orig or prev.officialDate` → cur.officialDate) | :125-138 (same; requires BOTH dates present) | officialdate_slid_one_day_watches / _a_week_unwinds |
| first-sight prev=None | `(prev or {})` py:85-86 | `prev.and_then(...)` is None-safe :122,:129 | first_sight_postponed_with/without_event_date |
| UNWIND vs WATCH cutoff | `d is None or d > window` py:101 | `map_or(true, |d| d > window)` unwind.rs:28-31 | every WATCH/UNWIND vector |

**L3 (the load-bearing invariant) — CORRECT.** The gap is measured from `orig` (= `event_date` when present, else
`prev.game_date[:10]`, else `cur.game_date[:10]`), NEVER from `officialDate`:
- Python py:86-87: `orig = (event_date or str(prev.gameDate)[:10] or str(cur.gameDate)[:10]) or None`; gap `_days(orig, target)` py:100.
- Rust postpone.rs:119-123 builds `orig` identically (empty `event_date` treated as falsy, matching the Python `or`
  chain); gap `orig.as_deref().and_then(|o| days_between(o, target))` :154.

**Hand-built `TB@NYY officialDate-already-moved` vector → UNWIND (NOT WATCH), confirmed two independent ways:**
- Rust test `live_officialdate_already_moved_104d_unwinds` (postpone.rs:390-395): `cur = Postponed, officialDate=2026-09-22,
  rescheduleDate=2026-09-22T17:05:00Z`, event_date falls to gameDate `2026-06-10` → asserts `reschedule_in_days == Some(104.0)`
  AND `action == "UNWIND"`, with the message "L3: 06-10 -> 09-22 = 104d, NOT officialDate-based 0d".
- Independent Python triangulation I ran by hand (event_date=`2026-06-12`, officialDate moved to `2026-09-22`):
  `unwind_trigger` → `UNWIND | makeup 2026-09-22 is 102d after original`; a regression to officialDate−officialDate =
  `_days('2026-09-22','2026-09-22')` = **0 days → WATCH** (the trap). Rust on the same shape: `orig="2026-06-12"`,
  target=`2026-09-22T17:05:00Z`, `days_between` trims to 10 chars → 102 → `should_unwind(102 > 2)` = UNWIND. ✔

`snap` alias-folding (`rescheduleDate || rescheduleGameDate`, `resumeDate || resumeGameDate`) py:66-68 ↔ postpone.rs:51-52,
tested (snap_reads_status_and_folds_aliases). `days_between` 10-char trim (py `[:10]` fromisoformat ↔ `&d[..d.len().min(10)]`
:60) tested on `...T17:10:00Z` + signed + malformed→None (days_between_whole_days).

## 2. FIRING SAFETY — CONFIRMED, no inverted/one-legged/double-fire path

- **Exit pricing NOT inverted.** `exit_price` main.rs:794-799: `Side::Yes => book.yes_bid` (SELL YES lifts the best
  YES bid), `Side::No => book.yes_ask.map(|a| 1.0 - a)` (SELL NO nets `1 − yes_ask`). Matches the spec exactly; neither
  is flipped to `yes_ask` / `1 − yes_bid`. Tested (exit_pricing_yes_takes_bid_no_takes_one_minus_ask, main.rs:1147-1170).
- **One-sided book SKIPs both legs (never legs one).** `unwind_exit_cents` main.rs:804-814 returns `None` if EITHER
  leg can't price (`book_of(leg)?` / `cents(...)?`); `handle_unwind` :436-439 then logs `WARN one-sided book … holding
  (poll re-emits)` and `return`s without firing — so a single leg is never sent. Tested (one leg missing side → `None`,
  main.rs:1169).
- **Idempotent coids prevent double-flatten.** `unwind_orders` unwind.rs:38-51 stamps `format!("unwind-{}-{}", pos.market, i)`
  — deterministic per (slug, leg). A poll re-emitting the same `UnwindRequest` produces byte-identical coids, which the
  venue dedupes. Note: the unwind path does NOT use `backend.cancel` (which exec.rs:302-308 refuses pending stage-2 order-id
  tracking); it fires fresh limit SELLs via `submit_pair`, so the unimplemented `cancel` is not on this path.

## 3. REDUCE-ONLY POLICY — CONFIRMED deliberate, logged, dry-run-safe, disableable

- Fires under the kill-switch by design: `handle_unwind` main.rs:428-430 logs `[UNWIND] kill-switch engaged but flattening
  (reduce-only) {slug}` and proceeds. ENTRIES remain blocked: `evaluate` returns `Reject::KillSwitch` first (risk.rs:63-65),
  so the kill-switch only stops new risk, never the flatten.
- Dry-run still only LOGS: `submit_pair` resolves to `DryRunBackend` (main.rs:43-46 default); `DryRunBackend::log_leg`
  exec.rs:66-76 prints `[DRY-RUN] would submit …` and returns a simulated ack — no network. `both_filled()` is true in
  dry-run, so the position is removed + exposure decremented (correct simulation), still no order sent.
- `CROSSARB_NO_AUTO_UNWIND=1` disables: config.rs:87 `auto_unwind: !env_bool("CROSSARB_NO_AUTO_UNWIND", false)`; main.rs:235
  only spawns `poll_mlb_postponements` when `cfg.auto_unwind`. With it off, no poll → no `UnwindRequest` is ever produced.

## 4. POSITION TRACKING — CONFIRMED correct; one INFO on the abbrev join (safe-direction)

- `Position` is built from the two entry `OrderIntent`s, recorded only on `both_filled()`: main.rs:365-368
  (`if ack.both_filled() { … track_position(...) }`); `position_from_intents` :776-788 copies each leg's exact
  venue/market/side and `market` = the pmus slug. Tested (position_from_intents_records_exact_legs, round-trips a sports
  PK fill of two YES legs).
- Game meta for the statsapi match: `track_position` main.rs:394-404 sets `league = pm_league(slug)` (2nd dash-segment,
  discovery.rs:119-121), `date = iso_date(slug)` (first YYYY-MM-DD, discovery.rs:124-144), `team_a = last_seg(pair.kalshi)`,
  `team_b = last_seg(pair.kalshi_b)` (last dash-segment, lowercased :395). For `KXMLBGAME-26JUN16-PIT` → `pit`. Tested
  (track_..._round_trips asserts `("mlb","2026-06-16","lad","pit")`, main.rs:1199).
- Wrong-game match guard: `find_game` postpone.rs:287-310 requires the statsapi game's `{away,home}` abbrev set to EQUAL
  `{team_a, team_b}` (orientation-free `HashSet` ==), AND the schedule is pulled for THIS held position's exact `date`
  (poll groups by `h.date`, :233-247). So a different game on the same date, or the same teams on a different date, cannot
  match. The poll also stores `prev` per the position's own slug (:252,:261-263), so cross-game state can't leak.

INFO (safe-direction, worth a droplet check, NOT a fire risk): the join is **Kalshi-ticker-suffix == statsapi `/teams`
`abbreviation`** (both lowercased; parse_teams :269-282). If those two ever disagree for a club (e.g. an Athletics/`ATH`
vs `OAK`, or a relocation), `find_game` returns `None` → the postponement is simply **not detected** (a MISSED unwind, not
a wrong-game unwind). This is the safe failure direction (no harmful fire) and is exactly the "confirm field semantics on a
real postponed game on the droplet" caveat the self-review already raised — but it means an abbrev mismatch silently forgoes
the protection, so it should be smoke-checked against live `/teams` before the first PROD-armed run. Today's MLB tickers
match statsapi abbrevs, so no live divergence is asserted.

## 5. THE TWO SELF-REVIEW WARNs + THE SELF-CAUGHT CRITICAL — re-verified

- **WARN "re-entry double-accumulates per_pair vs one decrement" — BENIGN as documented.** A second both-filled fill on
  the same slug would add to `per_pair[slug]`/`open_positions` while overwriting the single `HeldPosition`. But `evaluate`
  gates it: once `per_pair[slug] >= max_notional_per_pair` the sizing block returns `Reject::PairCap` (risk.rs:165,170,174-176),
  and `track_position` is what makes that cap bind. With the staged-rollout default `max_notional_per_pair = 1.0` (config.rs:70),
  a single contract (~$0.9–1.0) saturates the pair bucket, so a 2nd entry on the same slug is rejected before it can fill.
  Not exploitable under shipped caps; per-position notional tracking is legitimately out of this task's scope.
- **WARN "`decrement_exposure` reconstructs from the per_pair bucket" — CORRECT.** decrement_exposure main.rs:456-464 removes
  `per_pair[market]` wholesale and subtracts it from `total` + the cluster bucket (clamped ≥0). Because `track_position`
  added the identical value to all three (:388-391) and there is exactly one `HeldPosition` per slug, the removal is exact.
  Tested to round-trip to zero (track_and_decrement_exposure_round_trips, main.rs:1201-1203). The cluster `.max(0.0)` clamp
  correctly protects a multi-position cluster from going negative.
- **Self-caught CRITICAL "dropped `unwind_tx` busy-loop" — GENUINELY RESOLVED.** With `auto_unwind=false` no poll task holds
  a sender; if the loop's own `unwind_tx` were dropped, `unwind_rx.recv()` would resolve `None` every iteration and the
  `continue` (main.rs:264-269) would spin hot. The fix `let _unwind_tx_keepalive = unwind_tx;` (main.rs:249) keeps a live
  sender so `recv()` PARKS when idle; the loop still exits on the venue `rx` closing (:261-263). Confirmed in source; the
  comment at :245-249 documents the rationale.

## 6. CAN THE POLL AUTO-SELL WITH NO REAL POSTPONEMENT? — NO.

A fire is strictly gated on a detected POSTPONE_STATE or an officialDate move. `detect_postponement` returns `None`
(→ `handle_unwind` never invoked) on the entire normal life-cycle: it only returns `Some(Postponement)` when `is_postpone(cs)`
is true (postpone.rs:125) OR `prev.official_date != cur.official_date` with both present (:129-137). "Settlement merely
owner-assumed" (`assume_sports_settled`) only relaxes the *entry* gate (risk.rs:74-80); it has no path into the poll. The poll
reads live statsapi state — it cannot fabricate a postponement from an owner assumption. Tested: all 5 normal states
(Scheduled/Pre-Game/Warmup/In Progress/Final) → `detect_postponement(...).is_none()` (normal_lifecycle_is_none, postpone.rs:431-437).

## Bottom line
The trigger CANNOT miss a real postponement under the tested vectors (the L3 event-date gap is correctly ported and the
officialDate-already-moved case fires UNWIND), and it CANNOT fire a harmful unwind: exits are priced on the correct book side,
a one-sided book skips rather than legs out, coids are idempotent, the only failure mode (an abbrev/ticker mismatch) fails
SAFE by simply not firing. Reduce-only-under-kill-switch is deliberate and logged; dry-run only logs; `CROSSARB_NO_AUTO_UNWIND`
fully disengages. Independent recommendation: PROCEED — with a one-time droplet smoke of the `/teams` abbrev vs Kalshi-ticker
join on a live postponed game before the first PROD-armed run (the only unexercised-against-live-JSON surface).
