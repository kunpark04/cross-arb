# cross-arb Rust live bot — principal latency / systems review (2026-06-11)

Scope: `bot-rs/src/{types,config,risk,exec,ledger,main}.rs` (stage-1 std-only dry-run spine, 845 LOC)
plus the stage-2 architecture it implies, reviewed against the Python reference
(`bot/kalshi_book.py`, `bot/colisted_map.py`, `bot/monitor.py`, `bot/ledger.py`) and the measured
network floor (`research/latency-playbook.md`, `research/execution-feasibility-2026-06-09.md`).

Read-only review. No code was edited. Cargo is unavailable in the sandbox (a `target/` exists, so it
builds locally); findings are by inspection, not by running `cargo build/clippy`.

---

## 1. Latency verdict — "is the engine optimized for the lowest possible latency?"

**Three-layer honest answer:**

**(a) Is compute provably negligible vs the wire? YES — by ~5 orders of magnitude, and it isn't close.**
The entire decision hot path that exists today — `signal`/`game_edge` (a handful of f64 add/mul + a
`max` over ≤2 options) → `risk::evaluate` (≤13 branches + 3 `HashMap::get` + a few `f64` floors)
→ build `OrderIntent` → serialize the Kalshi JSON body — is **sub-microsecond to low-single-digit-µs**
work. Even the stage-2 per-delta book merge (`apply_delta` = one hash lookup + add) is tens of ns. The
measured network floor is **86–261 ms RTT, p50 148 ms serial two-leg** (execution-feasibility §1). So
compute is **~1e-6 of the budget**. There is no amount of hot-path micro-optimization in this code that
moves realized latency by a number you could measure against the wire. The README and decision 0015
already say this; the code is consistent with it. **Anyone claiming "Rust bought us latency" here is
wrong** — at this layer Python was already adequate (the playbook says so at Tier 4).

**(b) The REAL levers, ranked by impact (none of them live in the files under review yet):**
1. **Region/colocation** — host in the venue's cloud region (playbook Tier 1). Collapses RTT from
   ~30–100 ms to ~1–10 ms. This single deploy choice beats *everything* in code combined. **Not a
   `bot-rs` concern, but it is THE number.** Note 0015's caveat: these retail venues don't offer
   matching-engine colo, so the ceiling is "fast-follower," not HFT.
2. **Warm, pre-authenticated ORDER connection** — never pay TLS handshake + TCP slow-start in the send
   path (one or more extra RTTs). Stage 2 must hold a persistent HTTP/2 (or WS-order) connection per
   venue, separate from the data feed. **Currently `exec::LiveBackend::submit` is a stub** (exec.rs:95)
   — the entire transport, connection-pooling, and keep-alive story is unbuilt, and *this* is where the
   real latency design must land. Today it cannot reuse a connection because there is no connection.
3. **Concurrent two-leg fire** — `max(rtt_A, rtt_B)` not `rtt_A+rtt_B`; halves the floor (161→86 ms
   p50, execution-feasibility §1). The current `ExecutionBackend::submit(&mut self, one_intent)` trait
   (exec.rs:29) is **inherently serial and single-leg** — see §5 CRITICAL-1. This is the single
   highest-leverage thing *in the code's control*.
4. **Pre-built / pre-signed order + unwind templates** — when an edge is forming, pre-build both signed
   payloads so the trigger only does the network send (playbook Tier 3 #9/#10). The signing key must be
   loaded into memory once at startup (the Python re-reads the PEM from disk every call — fine for read,
   fatal for the order path; playbook Tier 2 #5). Nothing in `bot-rs` addresses pre-signing yet.
5. **Drive off the in-memory WS book, never a REST fetch in the hot path** (playbook #11). The Python
   monitor already does this; the Rust port must preserve it.

**(c) Where Rust genuinely helps vs where it's irrelevant:**
- **Genuinely helps:** (i) *reliability* of a 24/7 money-touching daemon — no GC pauses, so no
   tail-latency spike from a stop-the-world collection mid-fire (the one place a *language* tail could
   bleed into the wire-bound budget); (ii) strong types over a hand-rolled JSON/float pipeline that
   silently mis-prices (the fee-parity risk is real money — ledger.rs header); (iii) single-binary
   deploy. This is exactly decision 0015's stated rationale ("forward bet on a co-located future," not
   present-day latency). That framing is correct and the code honors it.
- **Irrelevant for latency:** the f64 arithmetic, the enum dispatch, `Box<dyn>` in `main`, String vs
   interned ids — at 148 ms wire floor none of it is observable. Optimizing these *for latency* would be
   the exact mistake the prompt warns against. (They still matter for **correctness and tail-GC-free
   determinism**, which is why some appear in §3/§4 — but not as a latency win.)

**Bottom line on the headline:** The spine is *latency-appropriate* (it does no dumb work) but it is
**not yet "optimized for lowest latency," because the part that determines latency — the warm
concurrent pre-signed transport — does not exist yet.** Stage 1 can't be optimized for a number that
stage 2 owns. The right verdict to give the owner: *"compute is already a non-issue; your latency is
100% a stage-2 transport + deploy-region problem, and the current single-leg synchronous
`ExecutionBackend` trait will actively prevent the concurrent-fire that is your biggest in-code lever
unless it's redesigned now."*

---

## 2. Time-complexity audit of the hot path

**Current code — all O(1) per evaluation, confirmed:**
- `risk::evaluate` (risk.rs:53) — every gate is O(1): `crossed()`/`mid()` are constant, the three
  notional caps are `HashMap::get` (O(1) average), sizing is a fixed sequence of `.min()`/`.floor()`.
  **No accidental O(n), no re-scan.** Good.
- `Book::mid`/`crossed` (types.rs:44–55) — constant. Good.
- `ledger` fee + `book_pair` — constant. Good.

**Stage-2 design implied by the Python — the parts that will actually run per-message, and the right
structures:**

- **Order-book representation (the hot one).** The Python `KalshiBook` holds `{price: qty}` dicts and
  computes best via `max(self.yes)` / `sorted(...)` on **every** access (kalshi_book.py:56–64,
  `best()` is O(n levels), the ladders are O(n log n)). In Python at ~47k transitions/day that's fine.
  **In the Rust port this is the one structure to get right.** The merge pattern is: apply a signed
  delta to one price level, then read **best bid/ask** and walk a few top levels for depth.
  - **Recommendation:** represent each side as a **`BTreeMap<u16_price_cents, u32_qty>`** (price in
    integer ticks 1..=99 — the venue tick is 1¢, so the key space is *tiny and bounded*). Delta merge =
    O(log n) ≈ O(log 99) ≈ 7 comparisons; **best bid = `.iter().next_back()`, best ask side =
    `.iter().next()` — both O(1) amortized** (BTreeMap end-access is cheap). Depth-walk is a bounded
    in-order iteration. This beats the Python's recompute-max-on-every-read by construction.
  - **Even better (and idiomatic for a 1¢-tick book):** a **fixed `[u32; 100]` price-indexed array**
    (index = price in cents) plus a cached `best_bid: u8` / `best_ask: u8` cursor. Update is **O(1)**
    (write the cell; if it emptied the best level, step the cursor — amortized O(1)); best is a field
    read. Zero allocation, cache-line friendly, no tree-node chasing. For a book that is *definitionally*
    99 price points this is the HFT-correct answer and it's simpler than it sounds. Use the array if you
    want the tightest tail; use BTreeMap if you want less bookkeeping. **Do NOT port the Python
    `HashMap<f64, f64>` + sort-on-read** — f64 keys are a hashing and equality hazard (see §4) and the
    sort is needless work per delta.
- **Dispatch from a book update to the affected market(s).** The Python does `k_targets.get(ticker)`
  → closure (monitor.py:1006). The Rust equivalent must be **one `HashMap<Ticker, MarketId>` lookup →
  index into a `Vec<MarketState>`**. Keep market state in a **`Vec` indexed by a dense `u32` MarketId**,
  not a `HashMap<String, MarketState>`, so the per-delta path is hash-once-then-index, never
  hash-the-whole-state-key. This is O(1) and allocation-free. Flag: a Kalshi ticker can map to **two**
  markets only in the pathological collide case the Python guards (`_collide`, monitor.py:891) — keep it
  1:1 and the dispatch stays a single lookup.
- **Per-tick match→signal→risk→size.** All O(1) as above. The only thing that scales is the **depth
  walk** (`depth_curve`, monitor.py:81) — it's a two-pointer merge over both ask ladders, O(levels), and
  levels is small and bounded. Fine. Just cap the walk at the top-N you actually size against (you never
  size past displayed `c2` depth) so a pathological 99-level book can't make the walk the long pole.

**No structure in the current or implied design scales with #markets per message** (each delta touches
one market). The only #markets-wide passes are the **discovery heartbeat** (every `refresh_sec`=300 s)
and prune — correctly *off* the hot path. Good separation; preserve it in the port.

---

## 3. Allocation / hot-path hygiene

Realistic rate: ~47k transitions/day ≈ **0.5/s average**, bursty to maybe low-hundreds/s on a
mass event-open. At that rate **none of the allocations below are a latency problem** — but several are
worth fixing for *determinism, GC-free tail, and correctness*, and a couple are genuinely sloppy.

- **`OrderIntent.client_order_id: String` + `market: String` (types.rs:91–97), and the
  `format!("smoke-{}", q.market)` / `client_order_id` build in the send path (main.rs:120).** A
  `format!` + `String` alloc on every order. **Verdict: MED — fix for the order path, not for latency.**
  At the order send you are about to block ~150 ms on the wire; a 100 ns alloc is invisible. BUT
  generating the idempotency key with `format!` is fragile and re-allocates; pre-build the coid into a
  **stack `arrayvec`/`[u8; N]` or reuse a per-market preallocated `String`** as part of the pre-staged
  template (§1 lever 4). The win is "pre-built template," not "alloc avoidance."
- **`risk::Exposure: HashMap<String, f64>` keyed by owned `String` (risk.rs:30–31), hashed twice per
  evaluate (`per_pair.get(&q.market)`, `per_cluster.get(&q.cluster)`, risk.rs:118–120).** Hashing two
  Strings per gate call. **Verdict: LOW-MED.** At 0.5–100/s it's noise vs the wire, but it's also
  trivially better as **`HashMap<MarketId(u32), f64>` / `HashMap<ClusterId(u32), f64>`** once you intern
  markets/clusters to dense ids (which you want anyway for the `Vec`-indexed dispatch in §2). Intern
  once at discovery, hash a `u32` (or just index a `Vec`) thereafter. Do it because it *also* fixes the
  dispatch structure, not as a standalone latency play.
- **`Quote` clones `market: String` + `cluster: String` (types.rs:69–79) and `q.market.clone()` at
  intent build (main.rs:117).** **Verdict: LOW.** `Quote` is constructed per-evaluation in the smoke;
  in stage 2 the live `MarketState` should *own* its market string once and the evaluator should borrow
  `&str`, never clone. Design the stage-2 `evaluate` to take `&MarketState` and return a sized decision
  that carries a `MarketId`, resolving the string only at the actual send.
- **`Box<dyn ExecutionBackend>` in `main` (main.rs:33).** One vtable indirection per submit. **Verdict:
  LOW / non-issue** — it's one dynamic call before a network round-trip. Keep it; the polymorphism
  (dry-run vs live) is worth more than the nanosecond. *Not* a place to spend effort.
- **The `f64` cast / `.floor()` chain in sizing (risk.rs:111–136).** `(room.max(0.0) / cost_per).floor()
  as u32` ×3. **Verdict: LOW for speed, but see §4 — there's a correctness wrinkle in the `as u32` cast.**
- **Logging in the would-be hot path:** `println!` on every dry-run submit (exec.rs:39) and every
  transition (`_write`, monitor.py:882). **Verdict: MED for the *live* path** — `println!` takes a locked
  stdout handle and formats synchronously; on a burst this is a real, *bounded-but-jittery* stall and the
  one std-lib thing that can add measurable tail. In live mode the order path must **not** `println!`
  inline — push a record to a bounded channel and let a logging thread drain it. Fine in dry-run.

**Premature to worry about now:** SmallVec for ladders, custom allocators, `#[repr]` packing of `Quote`.
The message rate doesn't justify any of it; the wire dwarfs it. Revisit only if a profile on the
co-located future ever shows a compute pole (it won't, per the playbook).

---

## 4. Correctness + Rust idiom review (the existing code)

This is where the real findings are — a dry-run spine's job is to be *correct*, and most of it is.

**Right / good:**
- **Fee math `order_taker_fee_cents` (ledger.rs:30–33)** — verified against `bot/ledger.py:kfee`.
  `ceil(coef·n·p·(1−p)·100 − 1e-9)` reproduces the Python `math.ceil(rate*n*p*(1-p)*100 - 1e-9)`
  exactly, and the tests pin it (100@0.5 → 175¢ not 176, the L10 over-charge). The `CEIL_EPS=1e-9` float
  guard is correct and matches Python. **`marginal_taker_fee` (no ceil) for detection** mirrors
  `kfee(marginal=True)`. This is the one money-critical numeric and it's faithful. ✔ (The README's
  "fee parity TODO" is the *cross-check harness*, not a bug — the formula itself checks out here.)
- **`book_pair` outcome-independence `debug_assert` (ledger.rs:60–62)** — correct invariant. One nit:
  `win_yes` and `win_no` are computed as `1 - cost_yes - cost_no` and `1 - cost_no - cost_yes`, which are
  **algebraically identical by commutativity**, so the assert can *never* fire — it's a tautology, not a
  guard. The *real* hedge-imbalance check the Python implies is "did you buy equal size on both legs at
  prices that sum < 1"; this function takes a single `size` and two costs, so imbalance is structurally
  impossible here anyway. **Verdict: harmless but misleading** — either delete the assert or make it
  assert the thing that matters (`cost_yes + cost_no < 1.0`, i.e. the pair is actually +edge). MED.
- **Risk-gate ordering (risk.rs:60–140)** — the "cheap/structural rejects first, sizing last" ordering
  is correct and well-reasoned: kill-switch → stream-paused → settlement-identity → crossed → stale →
  mid-divergence → edge-sign → floor → concurrency → sizing. This matches the invariant priority
  (settlement identity is the catastrophic axis, checked before any sizing work). ✔
- **One-sided book handling in `Book::mid` (types.rs:48–55)** — returns the single side as the "mid"
  when only one quote exists. Matches the Python `signal`'s philosophy (price the direction you can).
  Reasonable, though see the mid-divergence note below.

**Wrong / subtly off / un-idiomatic:**
- **`price_cents: u8` typed 1..=99 but never validated (types.rs:94).** Nothing enforces the range; a
  caller passing 0 or 100+ compiles. **Verdict: MED.** Use a constructor `OrderIntent::new(...) ->
  Result<_, _>` or a `newtype PriceCents(u8)` with a checked ctor. At real-money submit, an out-of-range
  price is a venue reject at best, a mispriced fill at worst. The Python doesn't have this type so the
  port is *adding* a safety surface — finish it by validating.
- **`f64 → u32 as` saturating casts in sizing (risk.rs:123–125) and `price_cents` paths.** `(x).floor()
  as u32` in Rust **saturates** (negative → 0, huge → u32::MAX, NaN → 0). Here inputs are guarded
  `.max(0.0)` so negatives are fine, and the values are tiny, so it's *currently* safe — but relying on
  `as` saturation is a latent footgun if any upstream value ever goes NaN (e.g. `cost_per` from a bad
  edge). **Verdict: LOW now, but prefer an explicit `f64::round`→bounds-check or
  `.clamp(0.0, u32::MAX as f64) as u32`** and assert non-NaN at the boundary. A NaN `cost_per` would make
  every cap `as u32 == 0` → silent `Reject::PairCap`, masking a real bug as a benign reject.
- **`cost_per = (1.0 - edge.net).max(0.1)` (risk.rs:111).** This is an *approximation* of the real
  cash-per-pair (it derives cost from the net edge, not from the actual `yes_ask + no_ask` legs the
  Python `signal`/`enter` use). For sizing-against-notional it's a defensible upper-ish bound, but it is
  **not** the true deployed cost, and the `.max(0.1)` floor is a magic number. The Python ledger uses the
  real `ay + an`. **Verdict: MED** — when stage 2 has the real leg prices, size against
  `yes_ask + no_ask`, not a reconstruction from `edge.net`; otherwise per-pair/cluster/total notional
  accounting will drift from reality (and notional caps are a safety control). Document or replace the
  0.1 floor.
- **Mid-divergence gate uses `mid()` which collapses a one-sided book to a single price (risk.rs:90).**
  If one venue is one-sided (say only a bid) and the other two-sided, the "mids" compared are a bid vs a
  midpoint — a structural ~half-spread skew that could trip or duck the 40¢ guard spuriously. The Python
  guards per-direction inside `signal` with the actual touches. **Verdict: LOW-MED** — fine at a 40¢
  threshold (very loose), but when you tighten it, compare like-for-like (both bids or both asks, or skip
  the gate when either side is one-sided).
- **`ExecError`/`Reject` are fine as enums; `Rejected(String)` and `MidDivergence(f64)` allocate/format
  only on the cold reject path** — idiomatic, no concern.
- **`#![allow(dead_code)]` (main.rs:8)** — pragmatic for a stage-1 spine with stage-2 fields wired but
  unused. Acceptable *with the comment*; just make sure it's removed once stage 2 lands so real dead code
  re-surfaces.
- **`ExecutionBackend::submit(&mut self, ...)` takes `&mut self` (exec.rs:29)** — fine for a stateful
  connection later, but combined with single-intent it blocks the concurrency design (§5).

**No memory-safety, no panics-in-normal-flow** (the only `debug_assert`/`panic` doc is the tautological
one above; `book_pair`'s "Panics if…" doc overstates what the code does). Error handling is `Result`-based
and clean. Idiom is generally good Rust.

---

## 5. Refactors that matter (prioritized)

### DO NOW — cheap, real, and they shape stage 2 correctly

- **CRITICAL-1 — Redesign the `ExecutionBackend` trait for CONCURRENT, MULTI-LEG submission *before*
  stage 2 is built on top of it (exec.rs:28–32).** The current
  `fn submit(&mut self, intent: &OrderIntent) -> Result<Ack, ExecError>` is **synchronous and
  single-leg** — it bakes in serial legging, which the project's own data says **doubles your effective
  latency** (148 ms serial vs 86 ms concurrent, execution-feasibility §1) and leaves you naked-legged
  more often. This is the single highest-leverage change *in the code's control*. Make it
  **`async fn submit_pair(&self, leg_a: &OrderIntent, leg_b: &OrderIntent) -> (Result<Ack,_>,
  Result<Ack,_>)`** that fires both legs with `tokio::join!` on two **pre-warmed, per-venue
  connections**, and add a separate `async fn flatten(&self, naked: &OrderIntent)` for the pre-staged
  unwind. `&self` (not `&mut self`) + interior-mutable connection pool so both legs can fire truly
  concurrently. *Why now:* every other stage-2 component (sequencer, unwind) will be written against this
  signature; fixing it after they exist is a rewrite. **This is the one architectural call I'd block on.**
- **HIGH — Specify the order-book type now as a 1¢-tick structure, not a port of the Python
  `HashMap<f64, _>` (kalshi_book.py:30–64 → stage-2 Rust).** Use `[u32; 100]` price-indexed array (or
  `BTreeMap<u16, u32>`) with cached best-bid/ask cursors (§2). O(1) update, O(1) best, zero alloc per
  delta, no f64-key hazard. Decide this before writing the merge so the dispatch and depth-walk are built
  against it. **Why:** it's the only per-message structure, and the Python's design is the one thing you
  should *not* copy verbatim.
- **HIGH — Load the signing key into memory once at startup and pre-stage signed templates; never read
  the PEM/.env from disk in the send path.** The Python re-reads on every call
  (`kalshi_ws_headers`/`_pmus_auth_headers`, kalshi_book.py:109 / monitor.py:575) — the playbook flags
  this as "must be fixed for the order path" (Tier 2 #5). Bake key-load into `LiveBackend::new` and
  pre-compute everything in the signature except `{ts}`/nonce. Wire this when `submit` is implemented.
- **MED — Intern markets/clusters to dense `u32` ids; store `MarketState` in a `Vec`, exposures in
  `HashMap<u32,_>` (risk.rs:30, types.rs:69).** Fixes dispatch (§2) and double-String-hashing (§3) in
  one move. Cheap, and it's the natural shape once you have a discovery pass that assigns ids.
- **MED — Fix the tautological `book_pair` assert (ledger.rs:60–62)** → assert `cost_yes + cost_no <
  1.0` (the actual +edge property) or delete it. And soften the doc-comment's "Panics" claim.
- **MED — Validate `price_cents` (1..=99) at construction (types.rs:94)** via a checked ctor / newtype.
- **MED — Size against real leg prices `yes_ask + no_ask`, replace the `cost_per` reconstruction +
  magic `0.1` floor (risk.rs:111).** Do it when stage-2 quotes carry the real touches.

### STAGE-2 ARCHITECTURE GUIDANCE (build it this way)

- **Separate sockets for data vs orders** (playbook #7) — a market-data burst must never head-of-line-
  block an order; a data rate-limit must never throttle the order quota. Two connections, two budgets.
- **Bounded-channel logging off the live path** — replace inline `println!`/file-append with an MPSC to
  a writer thread (§3). Keeps the GC-free tail actually GC-free of stdout-lock stalls.
- **Pre-staged unwind** — the moment a pair is approved, build the naked-leg flatten order for *each*
  leg so a one-leg fill cleans up in a single RTT (risk control *and* latency, playbook #10;
  execution-feasibility §2 shows naked-leg breakeven ≈4–6¢ vs 1–3¢ plausible, so unwind speed is the
  margin).
- **Port the FlipDebouncer detection-time stamping faithfully (monitor.py:238–272, decision 0013)** —
  the whole sub-second leg-fill measurement depends on CLOSE carrying *detection* time; a naive port that
  stamps flush time silently re-introduces the +1.0–1.5 s bias that 0013 just fixed.
- **Preserve the supervised-reconnect + seq-gap → `StreamPaused` wiring** — `risk::Reject::StreamPaused`
  (risk.rs:13) already exists; stage 2 must set `Exposure.stream_paused` from the WS layer on every
  reconnect/seq-gap/resubscribe so the gate refuses a rebuilding book (the Python does this via the
  resync/reconnect markers). The gate is ready; the producer isn't built.
- **Fee-parity harness before live** (README ⚠) — a property test asserting the Rust `ledger` reproduces
  `bot/ledger.py`'s selftest vectors. The formula matches by inspection (§4); prove it mechanically.

### LOW / SKIP (do not spend latency effort here)
`Box<dyn>` dispatch, String allocs on the cold path, SmallVec/custom-allocator micro-opts. All invisible
behind 148 ms. Touch only if a profile on a co-located deployment ever proves otherwise (the playbook
says it won't).

---

## 6. Bottom line for the owner

This is a **sound, honest, safe-by-default spine** — the risk gates are correctly ordered and O(1), the
fee math is faithful to the Python source (the money-critical part), and the code's own docs are
refreshingly clear that Rust here is a reliability/forward bet, not a latency win. **But it is not yet
"optimized for lowest latency," and it *can't* be, because the part that owns latency — a warm,
pre-authenticated, pre-signed, CONCURRENT two-leg transport — is a stub (`exec.rs:95`).** The compute is
already negligible (~5 orders of magnitude under the 86–261 ms wire floor), so do **not** spend a minute
micro-optimizing arithmetic, `Box<dyn>`, or String allocs — that's polishing the noise floor. **The
single highest-leverage change is to redesign the `ExecutionBackend` trait *now*, before stage 2 is
built on it, from synchronous single-leg `submit` to async concurrent `submit_pair` over two warm
connections** — because serial legging doubles your effective latency (148→86 ms) and is the one
latency lever actually in the code's hands; everything bigger than that (region/colo) is a deploy choice,
not code. On time-complexity: the design is clean; the only thing to get right is the order book — use a
1¢-tick price-indexed `[u32;100]` (or `BTreeMap<u16,u32>`) with cached best cursors, **not** a port of
the Python `HashMap<f64,_>`-with-sort-on-read. Lowest possible latency / lowest time complexity: **the
complexity is already right; the latency is 90% deploy-region + warm-concurrent-transport and 0% the code
you've written so far.**
