---
from: engine-review (adversarial, async-loop/lifecycle/concurrency lane)
run_id: 20260611-engine-review
scope: bot-rs/src/main.rs (run_live loop + refresh_loop + handle_unwind + track_position), bot-rs/src/unwind.rs, bot-rs/src/postpone.rs
cross_checked_against: bot-rs/src/venue.rs (streams + clear-on-reconnect), bot-rs/src/exec.rs (submit_pair/run_pair block_in_place), bot-rs/src/risk.rs (Exposure/stream_paused), bot-rs/src/book.rs (touch/last_update)
prior_reviews_honored: 20260611-1015 (discovery+staleness), 20260611-unwind-trigger, 20260611-unwind-parity
clippy: 5 warnings, all pre-existing/cosmetic (doc-indent main.rs:7, too_many_args matcher.rs:125, 3x map_or→is_*_and/or in risk.rs:88 / unwind.rs:29); NO concurrency lints, no errors
counts: CRITICAL 4, WARN 5, INFO 3
---

## Summary

The single-module reviews were right within their modules, but the **cross-module integration** has real
concurrency bugs none of them could see. The two highest-severity are: (1) the Kalshi stream `clear()`s the
shared `kalshi_books` on *every* reconnect while the event loop clears its `k_rebuild` pause on the *first*
post-reconnect frame — so a half-rebuilt Kalshi book trades against a never-cleared pmus book; and (2) the
event loop calls `submit_pair`, which does `block_in_place(handle.block_on(...))` — a synchronous network
join that **stalls the entire single-task event loop**, including `unwind_rx`, for the full two-leg RTT (and
under `LiveBackend` timeouts, seconds), directly delaying a void-flatten the system is built to fire fast.

---

### [CRITICAL] main.rs:274-303 + venue.rs:305 — reconnect clears all Kalshi books but the loop resumes trading on the FIRST rebuilt ticker, trading a half-rebuilt book

**Problem:** On every Kalshi (re)connect the stream wipes the *shared* book map:
```rust
// venue.rs:305 (inside kalshi_stream, on each connect)
books.lock().unwrap().clear();
```
then streams snapshots back one ticker at a time, emitting `VenueEvent::Kalshi{ticker}` per ticker as it
rebuilds. The event loop pauses on the `Reconnect` event (`k_rebuild = true`, main.rs:289-294) but then
**un-pauses on the very first Kalshi book frame**:
```rust
// main.rs:273-276
venue::VenueEvent::Kalshi { ticker } => {
    k_rebuild = false; // a Kalshi book frame -> its rebuild is flowing again
    pairs.lock().unwrap().by_ticker.get(ticker).cloned()
}
...
exposure.stream_paused = k_rebuild || pm_rebuild; // main.rs:303 -> now false
```
The comment claims "its rebuild is flowing again", but one snapshot frame rebuilds **one** ticker; the other
N−1 Kalshi books in the just-cleared map are still empty. The loop now has `stream_paused=false` and proceeds
to evaluate the pair touched by *this* frame. For a **sports** pair that is doubly broken: if the frame is for
Kalshi-A, the gate reads `kb.get(tb)` for Kalshi-B (main.rs:326) which is still empty → `continue` (safe by
luck). But for a **1:1 weather/econ** pair, the single Kalshi book *is* the one that just rebuilt, the pmus
book was **never cleared** (it is a local map, untouched by the Kalshi reconnect) and still holds its last
pre-reconnect snapshot, and `pmus_books.get(&slug)` returns it → a fully-formed `Quote` is built and can fire
**on the first frame of a mass rebuild**, defeating the entire "never trade a half-rebuilt book" guard (0013).
Staleness won't save it: `apply_snapshot` restamps `last_update = Instant::now()` (book.rs:60), so the
freshly-rebuilt Kalshi leg looks 0 s old, and the pmus leg keeps streaming independently so it is rarely
stale either.

**Fix:** Don't clear the pause on the first frame. Resume only when the rebuild is *demonstrably complete* —
the cleanest signal available is the resubscribe ack: have `kalshi_stream` emit a `Resynced{venue}` event
after it has sent the subscribe (or after the first snapshot-per-tracked-ticker count is reached) and clear
`k_rebuild` only on that, not on an `orderbook_snapshot` frame. Minimal interim: track a per-venue
"rebuild-in-progress until we've seen a snapshot for every currently-tracked ticker" counter, or simply
debounce — keep `k_rebuild=true` until the frame's ticker count since reconnect ≥ the tracked-set size at
reconnect time. (pmus is *less* exposed because its books are never bulk-cleared, but apply the same
ack-based resume there for symmetry.)

---

### [CRITICAL] main.rs:361 + main.rs:441 → exec.rs:269-285 — `submit_pair` blocks the whole event loop (and `unwind_rx`) for the full two-leg network RTT

**Problem:** The live loop is a single async task. Inside it, an approved entry fires synchronously:
```rust
// main.rs:361
let ack = backend.submit_pair(&legs[0], &legs[1]);
```
and `LiveBackend::submit_pair` → `run_pair` drives the two signed POSTs by **blocking the current thread**:
```rust
// exec.rs:269-285
fn run_pair(&self, a, b) -> PairAck {
    let fut = async { let (ra, rb) = tokio::join!(self.post_leg(a), self.post_leg(b)); ... };
    match tokio::runtime::Handle::try_current() {
        Ok(handle) => tokio::task::block_in_place(|| handle.block_on(fut)), // <-- blocks here
        ...
```
`block_in_place` keeps *this worker thread* parked until both HTTP round-trips finish. Because `run_live` is
**one** task doing all of `select!` → match → evaluate → submit, while it is parked in `block_on` it is **not
polling `unwind_rx`**. The whole design goal — "a void detection must not wait behind a quiet book stream, so
the unwind arm is co-equal" (main.rs:257-258) — is undone the moment an *entry* is in flight: a postponement
`UnwindRequest` that arrives during an entry's POST waits the full entry RTT (measured ~86–261 ms, but on a
`RateLimited`/timeout leg it is the reqwest timeout — seconds). It also stalls every book update for that
window, aging all other books. `handle_unwind` (main.rs:441) blocks the same way, so two voids serialize.

**Fix:** Do not perform blocking network I/O on the event-loop task. Make the entry/unwind fire `spawn` a
detached task (or a small bounded worker) that owns the `submit_pair` call and reports the `PairAck` back over
a channel; the loop then records the position / decrements exposure when the ack message arrives, keeping the
`select!` hot. Minimal version: `tokio::spawn` the submit and handle `track_position`/exposure in a third
`select!` arm fed by an `ack_rx`. (This also fixes the position-record-after-prune race in WARN below, since
the record step moves onto the loop's own turn.)

---

### [CRITICAL] postpone.rs:246-264 + main.rs:264-269 / 427 — the poll re-emits `UnwindRequest` every cycle and can DOUBLE-FIRE a flatten before the position is removed

**Problem:** The poll, for every still-held postponed position, sends an `UnwindRequest` on **every poll
cycle** as long as the status stays postponed:
```rust
// postpone.rs:254-258
if let Some(p) = detect_postponement(prev.as_ref(), &cur, &h.date, &h.pos.market) {
    if crate::unwind::should_unwind(&p, kalshi_void_window_days) {
        let _ = unwind_tx.send(UnwindRequest { slug: h.pos.market.clone() });
    }
}
```
The position is only removed inside `handle_unwind` **after** `submit_pair` returns `both_filled()`
(main.rs:442-446). With `LiveBackend`, that flatten POST takes an RTT; with `poll_s` defaulting to 60 s the
windows rarely overlap, but they are not mutually exclusive: a poll that fires while a *previous* flatten of
the same slug is mid-POST (or that is configured with a small `poll_s`, or a status that re-detects across two
fast cycles) enqueues a second `UnwindRequest`. The two requests are processed sequentially by the loop, but
the **idempotency argument is weaker than the prior review states**: `handle_unwind` re-reads the book and
re-prices the exit each time (main.rs:432-435), so the second flatten can carry a *different* `price_cents`
than the first → a **different `client_order_id` is NOT produced** (coid is `unwind-{slug}-{i}`, price-free,
unwind.rs:48) so the venue dedupes the *resend*, **but only if the first actually reached the venue**. If the
first flatten's leg returned `RateLimited`/`Rejected` (not `both_filled`), the position is *not* removed
(main.rs:447-449), the poll re-emits, and now you have repeated SELL attempts — which is the intended
retry — but there is no cap and no backoff, so a venue that 429s the SELL gets hammered once per poll
indefinitely, and a *partial* first fill (leg A sold, leg B 429'd) leaves a **naked re-exposed leg** that the
re-emit re-sells leg A on every cycle (coid-deduped, so leg A is a no-op, but leg B keeps retrying) — there is
no state that says "leg A already flat, only re-try leg B".

**Fix:** Make the unwind path stateful and idempotent at the *intent* level, not just the coid: mark the
position `unwinding=true` (or move it to a `flattening` set) the moment the first `UnwindRequest` is dequeued,
so re-emits for a slug already in-flight are dropped until the prior attempt resolves; on a partial fill,
record which leg filled and re-emit only the unfilled leg. At minimum, dedupe `UnwindRequest` in the poll
(don't re-send for a slug whose `prev` already triggered an unwind this session unless the book/leg state
changed).

---

### [CRITICAL] main.rs:406-409 vs 261-263 — `track_position` overwrites the poll's `prev` snapshot, and a re-entry resets postponement detection (missed unwind on first-sight-postponed re-entry)

**Problem:** `track_position` inserts a fresh `HeldPosition` with `prev: None`:
```rust
// main.rs:406-409
positions.lock().unwrap().insert(
    slug,
    postpone::HeldPosition { pos, league, date, team_a, team_b, prev: None },
);
```
The poll maintains `prev` across cycles by mutating the *same* map entry (postpone.rs:261-263). These two
writers race on the **same `positions` map** with no coordination beyond the mutex. If an entry fires for a
slug that already has a held position whose `prev` the poll has been tracking (the documented re-entry case,
or a same-slug re-fill after a partial unwind), the `insert` **clobbers `prev` back to `None`**, throwing away
the poll's accumulated status. The next poll cycle then treats it as first-sight. That is *usually* safe
(first-sight with an `event_date` still detects a postpone), but it silently defeats the **officialDate-moved-
without-status-flip** branch (postpone.rs:129-137), which *requires* `prev.official_date` to be present:
after a clobber, `prev=None` → `prev.and_then(|p| p.official_date)` is `None` → the whole officialDate-slide
detection is skipped for a full cycle (until `prev` repopulates). For a game that slid out of the window via
an officialDate move (no status flip — the exact case `officialdate_slid_a_week_unwinds` covers), a
mistimed re-entry can **miss the unwind**. The two writers also have no notion of "this is the same position"
vs "a new one" — `insert` is unconditional.

**Fix:** In `track_position`, do not blindly `insert`. If an entry for the slug already exists, **preserve its
`prev`** (and decide whether re-entry is even allowed — see the PairCap discussion): `match
map.entry(slug) { Occupied(e) => { /* keep e.prev, update pos/size */ } Vacant(e) => e.insert(... prev:None) }`.
This makes the entry path and the poll path agree on the lifecycle of a single held position.

---

### [WARN] main.rs:355-368 — an entry can fire, then the refresh task prunes the pair, and the position is recorded for a market no longer tracked / book freed

**Problem:** The loop clones the pair out and releases the `pairs` lock (main.rs:307) before building the
quote and firing. Between `submit_pair` (main.rs:361) and `track_position` (main.rs:367), the `refresh_loop`
runs on another task and can prune that slug: it removes it from `pairs`/`pm_tracked`/`k_tracked` and
**frees the Kalshi book** (`kb.remove(tk)`, main.rs:606-611). The fill still happened (real position), and
`track_position` then records a `HeldPosition` whose Kalshi leg's book has just been freed. The postponement
poll can still see it (it keys off the slug, not the book), so it is not *orphaned* — but `handle_unwind`
later tries to price the exit from `kalshi_books` (main.rs:433) and finds **no book** → `unwind_exit_cents`
returns `None` → "one-sided book — holding" forever, because the pruned ticker is no longer subscribed and
will never restream. The position is held but **un-flattenable**.

**Fix:** This is fixed for free by the CRITICAL exec change (record on the loop's own turn, after re-checking
the pair is still tracked). Absent that, re-verify membership under the `pairs` lock immediately before
`track_position`, and on a prune of a slug that has an open `HeldPosition`, do **not** free its Kalshi book
(or keep it subscribed until the position is flattened).

---

### [WARN] main.rs:307 + 313-337 / refresh_loop:582-611 — TOCTOU: the loop reads `pm_tracked`/`pmus_books`/`by_ticker`/`kalshi_books` across SEPARATE lock acquisitions, so it can act on a half-applied refresh

**Problem:** Each shared structure is locked, read, and unlocked independently:
- `by_ticker` lookup (main.rs:275) — lock #1
- `pm_tracked.contains` (main.rs:279) — lock #2
- `by_slug.get(&slug).cloned()` (main.rs:307) — lock #3
- `kalshi_books.lock()` (main.rs:320) — lock #4

Meanwhile `refresh_loop` mutates `pairs` + `k_tracked` + `pm_tracked` under one combined lock block
(main.rs:581-603) but frees `kalshi_books` in a **separate** block afterward (main.rs:606-611). So the event
loop can observe states that never existed atomically: e.g. it resolves a ticker→slug via `by_ticker` (lock
#1) for a pair the refresh is *mid-prune*; by the time it does `by_slug.get` (lock #3) the slug is gone →
`continue` (benign). The dangerous direction is the **add**: refresh inserts into `by_slug`/`by_ticker`/
`pm_tracked` (so a frame now resolves) but the new pair's books are not yet populated — handled by the
`continue` on missing book (safe). The genuinely unguarded interleaving is prune-of-`kalshi_books`: the loop
can pass the `by_slug.get` check (lock #3, pair still present) and *then* the refresh's second block frees the
Kalshi book before the loop's lock #4 → `kb.get(&pair.kalshi)` returns `None` → `continue`. All paths
currently degrade to `continue`, so today this is **latent, not exploited**, but it depends on every reader
tolerating a vanished entry; the moment any path assumes "pair present ⇒ book present" it becomes a panic/
mis-fire. The `.unwrap()` at main.rs:323 (`pmus_books.get(&slug).unwrap()`) is exactly such an assumption —
see next finding.

**Fix:** Either (a) snapshot the needed state under a single lock acquisition (clone pair + its book handles
together), or (b) fold the `kalshi_books` free into the *same* locked block as the `pairs`/`tracked` mutation
in `refresh_loop` so a pruned pair and its freed book are never observably out of sync. Long-term, a single
`Arc<Mutex<World>>` holding pairs+tracked+books removes the multi-lock interleaving entirely.

---

### [WARN] main.rs:323 — `pmus_books.get(&slug).unwrap()` can panic on a settled-then-restreaming slug

**Problem:**
```rust
// main.rs:313-316  (first read, Option-guarded)
let pm = match pmus_books.get(&slug) { Some(b) => b.touch(), None => continue };
...
// main.rs:323  (second read, UNWRAPPED)
let pmb = pmus_books.get(&slug).unwrap();
```
The two reads are on the same `pmus_books` map and `pmus_books` is loop-local (only this task mutates it), so
in the *current* control flow nothing removes the entry between line 313 and 323 — it is safe **today**. But
it is a booby-trap: the only thing keeping it sound is that no `.await` or `pmus_books.remove` sits between the
two reads. The unwrap encodes "the guarded get above guarantees this one", which is true only by adjacency.
Any future edit that touches `pmus_books` mid-block (e.g. handling a second event type, or moving the
book-free here) turns it into a panic — and a panic on the event-loop task silently kills the whole bot (no
supervisor; the task just ends and `rx`/streams keep running into a dead consumer).

**Fix:** Reuse the already-borrowed `pm`/book instead of re-getting, or make it `let Some(pmb) =
pmus_books.get(&slug) else { continue };`. No reason to `unwrap` when a `continue` is the established idiom two
lines up.

---

### [WARN] main.rs:296-301 — a Kalshi `SeqGap` sets `k_rebuild=true` but NO path guarantees the cleared book is rebuilt before resume; resume still keys off "first frame"

**Problem:** On `SeqGap` the loop pauses Kalshi (`k_rebuild=true`, `stream_paused=true`, main.rs:298-299). The
stream, on a seq gap, breaks its read loop and reconnects (venue.rs:349-354), which `clear()`s the books and
re-subscribes. Same shape as the reconnect CRITICAL: resume is gated on the *first* rebuilt frame, not a
complete rebuild. Additionally, the seq-gap event is emitted with `clean=false` semantics but there is no
corresponding `Reconnect` suppression — the stream will *also* emit a `Reconnect{clean:false}` when it tears
down (venue.rs:388), so a single gap produces **two** pause-setting events (SeqGap then Reconnect) and then a
frame clears it; harmless for correctness but means `k_rebuild` toggling is driven by event *ordering* that
isn't pinned anywhere.

**Fix:** Same as the reconnect CRITICAL — resume on an explicit resync/ack signal. Consider collapsing SeqGap
into "the stream will reconnect and emit Reconnect" so there is one pause source.

---

### [WARN] postpone.rs:215-227 + 246-264 — the poll holds NO lock across its awaits (correct) but reads `positions` THREE times per cycle with mutation in between; a flatten landing mid-cycle is acted on with stale `held`

**Problem:** The cycle snapshots `held` (lock, clone, unlock, main.rs/postpone.rs:215-227), then for each
`h` re-reads `prev` from the map (postpone.rs:250-253), sends the unwind (postpone.rs:257), then re-reads to
store `prev` (postpone.rs:261-263). If `handle_unwind` removes the position between the `held` snapshot and
the `prev`-store, the store is guarded (`if let Some(hp) = ...get_mut`) so it no-ops (good). But the
`UnwindRequest` at :257 was already computed from the *snapshot* `h`, so a position flattened earlier in the
same cycle (by a request from a *prior* cycle that the loop processed late) still gets a fresh
`UnwindRequest` here — feeding the double-fire in the CRITICAL above. The poll has no view of "already
requested / already flattening".

**Fix:** Gate the send on a re-check that the position is still held *and not already flattening* at send
time (re-lock and test a `flattening` flag), tying into the unwind-state fix in the double-fire CRITICAL.

---

### [INFO] main.rs:340 + 662 — `led_by_from_prior` mutates `prior_mid` for EVERY frame even when the pair is later skipped by a gate, so `led_by` can be computed off a snapshot that never traded

**Observation:** `led_by_from_prior` (main.rs:638-664) inserts the current mids into `prior_mid` on every call,
and it is called (main.rs:340) before `evaluate`. If the quote is rejected (stale, crossed, capped), the
prior snapshot is still advanced. The *next* time the pair is seen, `led_by` is computed relative to a mid the
bot saw but never acted on — which is arguably correct (it tracks market movement, not trade movement), but it
means the toxicity-direction signal (`led_by`) is sensitive to frames the gate discarded, including frames
during a rebuild. Not a bug; flagging because the H1 toxicity gate is the one live behavioral signal and its
input is advanced unconditionally.

**Fix:** None required; if H1 precision matters, only advance `prior_mid` on frames that passed the structural
gates (or stamp it with a "was tradeable" flag).

---

### [INFO] main.rs:213 + 365-367 — in DRY-RUN every entry is `both_filled` so `positions`/exposure grow unbounded by the concurrency/notional caps only; intended, but note the dry-run position set never shrinks except via a (also-simulated) unwind

**Observation:** `DryRunBackend::submit_pair` always returns two `Ok` acks → `both_filled()` is always true →
`track_position` always records. Over a long dry-run, `positions` grows until `max_concurrent_positions`
(default 5) blocks new entries via `ConcurrencyCap` (risk.rs:153). So it *is* bounded — by the concurrency
cap, not by anything else. The only removal is a simulated unwind (weather/econ never unwind → those slots
stay filled for the run). This is the intended "caps bind in dry-run" behavior, but it means a dry-run with
`max_concurrent_positions` set high accumulates `HeldPosition`s for the whole session with no settlement-based
cleanup (there is no "position settled at expiry → free the slot" path anywhere — settlement only prunes the
*pair*, never the *position*).

**Fix:** Out of this lane's scope, but stage-2 needs a position-settlement/expiry reaper: a pruned/settled pair
with an open `HeldPosition` is currently never cleaned (the WARN above is the unwind-side symptom; the general
case is "held position whose market settled normally"). Today nothing decrements exposure on normal
settlement, so the caps ratchet down over a long run until everything is `ConcurrencyCap`/`TotalCap`.

---

## Lock-ordering audit (deadlock check) — CLEAN, with one caveat

No A→B / B→A inversion exists. The locks {`pairs`, `k_tracked`, `pm_tracked`, `kalshi_books`, `positions`}
are acquired as: seed/refresh take `pairs → k_tracked → pm_tracked` (same order both sites, main.rs:190-192,
582-584) then `kalshi_books` **separately** (never nested with the trio); the event loop takes them one at a
time, never two nested; the streams take only `tracked` and only `books`, never both; the poll takes only
`positions`. `block_in_place` in `submit_pair` runs with **no lock held** (the loop drops every lock before
firing — verified main.rs:307/337 scope ends before :361, and `handle_unwind` re-locks `positions` only
*after* the book locks are dropped at :435). So no deadlock. **Caveat:** `block_in_place` requires the
multi-thread runtime; `#[tokio::main]` provides it (`features=["full"]`), but if anyone ever switches to a
current-thread runtime, `block_in_place` panics — worth an assertion/comment at the call site.
