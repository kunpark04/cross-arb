# Ladder + trade logging SPEC — make the maker study a measurement (wave-2 implementer doc)

**Why:** probe 1 (`probe1-maker-weather.md`) could only BOUND maker fill rates and adverse selection,
because `bot/monitor.py` logs touches only at edge-state transitions. The bound is severe: the
transition-sampled series shows **220 definite level-crossings over 15.4 observed market-days**,
while Kalshi alone printed **~23,800 weather trades in the last 24 h** (live REST count, 2026-06-11,
5 series × 12 open markets ≈ ~400 prints/market-day). Three additions close the gap. All are
**additive**: new files, new record types — `transitions-*.jsonl` shape is untouched.

**Scope: weather only** (`tc-temp-*` / the 5 `KXHIGHT*`/`KXHIGH*` series). The maker fee edge is
weather-only (maker-fee series MLB/econ stay fee-positive — probe 1 fee table), and weather is the
volume-cheap subset (~6.8k transition recs/day vs sports ~40k).

---

## A. `trades-<event-date>.jsonl` — trade prints (the fill ground truth) — PRIORITY 1

### Kalshi: REST cursor-poll (live-VERIFIED 2026-06-11, zero new WS risk)

`GET /trade-api/v2/markets/trades?ticker=<tk>&min_ts=<last_poll>&limit=200[&cursor=..]` is **public
(no auth), cursor-paged, and returns the aggressor side per print** — verified live this session
incl. weather:

```json
{"count_fp": "25.00", "created_time": "2026-06-11T00:40:09.714292Z", "is_block_trade": false,
 "no_price_dollars": "0.9200", "taker_book_side": "bid", "taker_outcome_side": "yes",
 "taker_side": "yes", "ticker": "KXHIGHNY-26JUN11-T95", "trade_id": "8bfb...a86d",
 "yes_price_dollars": "0.0800"}
```

- **When:** poll on the existing 300 s `rest_heartbeat` for every tracked weather ticker
  (≤~60 tickers → ≤0.2 req/s staggered, with `min_ts` = last poll, dedupe on `trade_id`).
  ~300 s print latency is fine for the *study* (offline analysis), not for live execution.
- **Do NOT wire the Kalshi WS `trade` channel yet:** it is vendor-documented but unprobed in-repo,
  and the 0013 probe verified single-sid/seq semantics for `orderbook_delta` ONLY. A second channel
  on one connection = unknown sid/seq interleaving → same class of risk the multisub probe existed
  to kill. If wanted later: extend `scripts/probe_kalshi_ws.py` with a `--trade` phase (Q: 2-channel
  subscribe → one sid or two? do seqs interleave contiguously? frame shape?).

### pmus: WS `SUBSCRIPTION_TYPE_TRADE` (documented, needs a probe first)

`research/polymarketus-api-auth.md` documents `SUBSCRIPTION_TYPE_TRADE` ("real-time trade prints,
separate channel from book") on the same auth'd WS. **Frame shape unverified** → extend
`scripts/probe_pmus_ws_auth.py` with one TRADE subscription on a live weather slug before wiring
(pattern is identical to the existing sharded MARKET_DATA subscribes). Until probed, ship Kalshi-only
— the resting venue that matters is Kalshi (probe 1: the only +EV configs rest on Kalshi).

### Record (one line per print)

```json
{"t": 1781139999.123, "venue": "k", "market": "tc-temp-nychigh-2026-06-11-gte95f",
 "ktk": "KXHIGHNY-26JUN11-T95", "vt": "2026-06-11T00:40:09.714292Z",
 "yes_c": 8, "qty": 25.0, "taker": "yes", "id": "8bfb...a86d"}
```

`market` = the monitor's pm-slug key (join key with transitions; map via the existing `slug_k`);
`vt` = venue timestamp verbatim (the TRUE fill time — L22: never re-stamp with poll time; `t` is
rx/poll wall-clock, kept separately). ~150 B/print.

**Bytes/day:** ~23.8k Kalshi weather prints/day × ~150 B ≈ **3.6 MB/day** (pmus later: unknown,
expect ≪ Kalshi; budget +0.5).

**Unlocks:** TRUE maker fill events — (i) fill rate at a level = prints at-or-through the level with
the right aggressor side, vs resting time; (ii) **true time-to-fill** (probe 1's `dt` upper bounds
collapse to measurements); (iii) adverse selection measured AT the fill timestamp, not at next
visibility; (iv) fill-size distribution (`count_fp`) = rest-size capacity; (v) with B, queue
throughput per level.

---

## B. `ladders-<event-date>.jsonl` — top-5 ladder both sides both venues — PRIORITY 2

The monitor already holds both full ladders in memory at every weather evaluation
(`MarketTracker.books["P"]` = pm bids/offers; `books["K"]` = the merged `KalshiBook` views) —
**zero new subscriptions; purely a logging change.**

### Record

```json
{"t": 1781139584.572, "market": "tc-temp-nychigh-2026-06-11-gte95f", "k": "tr",
 "pb": [[9,120.0],[8,300.0],[7,55.0],[5,1000.0],[4,12.0]], "pa": [[11,40.0], ...],
 "kb": [[...]], "ka": [[...]]}
```

Prices integer **cents**, qty rounded 1 dp, best-first, ≤5 levels/side. ~320 B/record.

### When emitted

1. **`k:"tr"`** — on every weather transition write (same `t` as the transition record = detection
   time, L22). ~6.8k/day → **~2.2 MB/day**.
2. **`k:"hb"`** — every heartbeat (300 s) per tracked weather market, **delta-suppressed** (skip if
   all 4 top-5 ladders unchanged since the last hb — weather books idle overnight; expect 30–60%
   suppression). ≤40 live markets × 288/day × 320 B ≈ **≤3.7 MB/day pre-suppression**.

**Unlocks:** (i) **queue-ahead size at placement** (the missing input for a queue-position fill
model, joined with A's throughput); (ii) uniform-clock book series → unbiased crossing/fill
denominators (kills probe 1's "observed market-day" activity bias) and covers the between-episode
regime where today there are ZERO records; (iii) depth-shape phantom tells (L20 flat-ladder) on
every snapshot; (iv) full two-sided `depth_curve` reconstruction at any moment (today only the
signalled-direction `{c2,c1,c0}` summary survives).

### What orderbook_delta can and cannot infer (why A exists)

A Kalshi qty-decrease delta at a price is **trade-or-cancel — indistinguishable**, and the aggressor
is unknown. Joining A's prints onto B's ladder states resolves both. Don't build delta-inference:
the trade feed is directly available (REST verified).

---

## C. Explicitly NOT proposed

- Per-tick/per-frame ladder logging (~50–100× bytes; nothing in the maker study needs it).
- Sports/econ ladders or trades (maker fee edge is weather-only; sports is the 40k-rec/day hog).
- Any change to existing `transitions-*` record fields (downstream loaders untouched).

---

## Budget (current total ~11.7 MB/day raw, measured this session)

| item | recs/day | bytes/day | running total |
|---|--:|--:|--:|
| current monitor (all categories) | ~47k | 11.7 MB | 11.7 |
| A trades (Kalshi weather, REST poll) | ~24k | +3.6 MB | 15.3 |
| B-tr transition ladders (weather) | ~6.8k | +2.2 MB | 17.5 |
| B-hb heartbeat ladders (weather, pre-suppression) | ~11.5k | +3.7 MB | 21.2 |

Worst case **~21 MB/day raw** (~2.4 MB/day gzipped at the measured ~8.7× ratio); lean variant
(top-3 levels, hb 600 s, suppression on) ≈ **+6 MB/day**. Droplet: 25 GB disk + daily
pull-then-delete → either fits trivially; pull bandwidth +~10 MB/day.

## Compatibility checklist (verified against current source)

1. `analyze_persistence.load()` globs `transitions-*.jsonl[.gz]` only → new prefixes are
   **invisible** to every existing loader. No shape change anywhere.
2. **`deploy/pull-data.ps1` MUST be extended** — it lists/pulls all `*.jsonl` (new files get
   mirrored ✓) but its gzip-finalize + remote-delete passes regex
   `^transitions-(\d{4}-\d{2}-\d{2})\.jsonl$` only → without extending to
   `^(transitions|ladders|trades)-(\d{4}-\d{2}-\d{2})\.jsonl$`, ladders/trades **accumulate on the
   droplet unbounded**.
3. `TransitionLogger` gains `ladder(rec)` / `trade(rec)` writers reusing `event_partition()`.
4. `run_live` needs a **slug → tracker registry**: trackers currently live only inside the
   `register()` closures, so `rest_heartbeat` cannot reach pm ladders for `k:"hb"` snapshots.
   (Kalshi ladders are already reachable via the shared `books` dict.)
5. Trade-poll dedupe state (`last min_ts`/`trade_id` per ticker) must survive a restart the same
   way `cli_stream` seeds from its own log (read the tail of today's `trades-*.jsonl` on boot) —
   else every restart re-logs a day of prints.
6. Probes BEFORE any WS wiring: `probe_kalshi_ws.py --trade` (2-channel sid/seq + frame shape),
   `probe_pmus_ws_auth.py` TRADE-type frame shape. The Kalshi REST path needs no probe (verified).

## Which maker-study quantity each field unlocks (summary)

| quantity (probe-1 status) | needs |
|---|---|
| true fill rate + time-to-fill at a level (bounded) | A (`vt`, `yes_c`, `taker`) |
| adverse selection at fill time (bounded at visibility) | A + existing transitions `px` |
| rest-size capacity / fill sizes (unmeasured) | A (`qty`) |
| queue-position fill model (unmeasured) | A + B (queue-ahead at placement) |
| unbiased crossing denominators (activity-biased) | B-hb |
| between-episode book regime (invisible today) | B-hb |
| flat-ladder phantom tell on demand (OPEN-only today) | B |
| cancel-vs-trade attribution in deltas (ambiguous) | A joined on B |

**Priority order: A (Kalshi REST trades) → B-tr → B-hb.** A alone converts the headline bound
(probe 1 verdict) into a measurement; B-tr adds the queue model; B-hb cleans the denominators.
