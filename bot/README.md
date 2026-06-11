# bot/ — accounting core + (future) live bot

## `ledger.py` — cross-venue position ledger + PnL simulator

The bot's accounting core for **one** binary market with **identical settlement on both venues**
(P = polymarket.us, K = Kalshi). A cross-venue arb entry buys YES on one venue + NO on the other (a
complementary pair); if the pair costs < $1, the difference is locked. It tracks holdings per venue,
cash, and cost basis, and models the layer / rotate / hold / leg-risk decisions.

Run it — it's a **self-verifying harness** (no network):

```bash
python bot/ledger.py
```

It walks 5 scenarios (S1–S5) and asserts the invariants hold.

### Invariants it demonstrates + asserts

1. **Additive** — settlement PnL of a set of held arb pairs == sum of each entry's net edge.
2. **Outcome-independent** — for a balanced book, settlement PnL is identical whether YES or NO wins.
3. **Hold, don't panic-unwind** — a locked arb can be mark-to-market *negative* mid-life yet settle
   *positive*. Never unwind at an MTM loss; hold + layer. A genuine **flip** (proceeds > $1 > cost) is
   the only case where early rotation beats holding.
4. **Leg risk is the real downside** — if only one leg fills, the position is directional, not a
   locked arb, and can settle negative. A later opportunity is used to *repair* the naked leg, not to stack.

### Decision rule (encoded in the scenarios)

> **Layer by default. Rotate only if** the first tranche's MTM exceeds its locked edge plus round-trip
> cost **and** exit depth exists. Never unwind at an MTM loss. A single unhedged leg is the only real
> loss path.

Rationale recorded in [decisions/0004](../decisions/0004-ledger-layer-by-default.md).

## Fee models

- **Kalshi** taker: `ceil(0.07 · N · P·(1−P))` rounded up to the next cent **once per ORDER** (not per
  contract); maker: `ceil(0.0175 · N · P·(1−P))` — the ceil applies to each side's own formula
  ([research/kalshi-venue-audit.md](../research/kalshi-venue-audit.md) §2.1).
- **polymarket.us** taker: `0.05 · N · P·(1−P)` linear (no ceil); maker rebate −0.0125 modeled as 0
  (conservative; [research/us-legal-overlap-audit.md](../research/us-legal-overlap-audit.md)).
- Edge **detection** uses the marginal (no-ceil) rate so nothing +EV-at-size is dropped ([L10]/[L15]);
  the exact per-order ceil fee applies at **booking**. Re-pin both schedules from primary sources before
  any sizing decision (tracked in todo).

## `monitor.py` — dual-stream edge monitor (persistence layer; decisions 0003 / 0005)

The persistence logger: streams both venues' order books, recomputes the cross-venue edge on every
book delta, and logs edge **state-transitions** (open / close / flip / widen / narrow) to JSONL — the
input to the layer-vs-rotate rule above. **READ-ONLY**; places no orders.

```bash
python bot/monitor.py          # OFFLINE self-test (weather + sports trackers; no network)
python bot/monitor.py --live N # bounded ~N-second READ-ONLY dual-stream run (no orders; deploy gated)
```

- **Transition core** (`classify` / `MarketTracker`) is pure + self-verifying, like `ledger.py`.
- **polymarket.us stream** is wired with the protocol verified 2026-06-08
  (`scripts/probe_pmus_ws_auth.py`): `wss://api.polymarket.us/v1/ws/markets`, Ed25519 handshake signing
  `{ts}GET/v1/ws/markets`, slug-keyed `SUBSCRIPTION_TYPE_MARKET_DATA`, frames = REST-book shape.
- **Kalshi stream** (`kalshi_book.py`) — RSA-PSS handshake + snapshot/delta merge (`KalshiBook`),
  VALIDATED offline + live (28 real deltas, no seq gaps). `python bot/kalshi_book.py [--live]`.
- **Co-listed map** (`colisted_map.py`) — `build_colisted_map()` does FULL discovery (pmus catalog +
  Kalshi series, dynamic date/event grouping) → pairs, plus a COVERAGE AUDIT that flags any pmus
  city/league we don't map AND a `fetch_errors` list (non-empty = DEGRADED pass; the monitor skips
  pruning on it). Weather joins on **identical canonical bounds** (dict join, never index-zip); sports
  binds one Kalshi event to at most one pm game (doubleheader guard); econ joins pmus `≥T` to its
  settlement-identical Kalshi twin `floor = T − grid_step` ([0013](../decisions/0013-econ-grid-step-twin-and-measurement-integrity.md)).
  `python bot/colisted_map.py`. Refreshed on the heartbeat ([0008](../decisions/0008-colisted-map-discovery-and-coverage-audit.md)).
- **Two tracker types:** WEATHER → `MarketTracker` (1:1 binary); SPORTS → `GameTracker` (2-outcome: a pm
  game market + two Kalshi team tickers, cheapest-venue-per-side). `run_live` dispatches each book delta
  to the right one. **Live-verified** 2026-06-08 (`--live 75`: 60 weather + 124 sports tracked; logged
  real MLB edges nyy-cle PK +3.75¢, phi-tor KP +6.81¢).
- **Dynamic re-subscribe + FLIP debounce** — the heartbeat (supervised; one bad cycle never kills it)
  re-discovers and registers new markets; new **pmus** slugs subscribe on the live socket (the verified
  shard pattern), new **Kalshi** tickers are added IN-PLACE via `update_subscription add_markets` — a
  **NO-GAP add** (probe-verified 2026-06-10, `probe_kalshi_ws.py --multisub`: one sid per channel, control
  acks consume seq slots, only added tickers snapshot; offline integration test
  `scripts/test_monitor_nogap.py`). An add whose snapshot never arrives within `ADD_CONFIRM_SECS` falls
  back to the old reliable cycle; pruned tickers are `delete_markets`-unsubscribed (hygiene); a genuine
  **seq gap still cycles** (missed deltas have no replay). `FlipDebouncer` coalesces
  a CLOSE + opposite-direction OPEN within ~1s into one FLIP (same-dir reopen = suppressed flicker) and
  emits flushed CLOSEs stamped at **DETECTION time** — flush-time stamps silently added ~1.0–1.5s to every
  episode duration pre-[0013](../decisions/0013-econ-grid-step-twin-and-measurement-integrity.md).
- **Per-transition fill inputs** — every arb transition also logs `depth` (`{c2,c1,c0}` = **displayed**
  contracts at gross marginal edge ≥2¢/1¢/0¢ on both legs — an *upper bound* on takeable size, never pinged)
  + `age` (per-venue book *staleness* — a coarse hint, not a per-venue stream-liveness certificate) — the raw
  inputs to the ALL-IN cost model ([0010](../decisions/0010-all-in-edge-filtering-and-cost-model.md)).
- **Stream resilience** (2nd review C6, hardened by 0013) — each venue WS runs in a supervised
  reconnect-with-backoff loop, so a clean or abnormal close re-subscribes the current targets instead of
  silently half-killing the collector; **every reconnect writes a `ws_reconnect` marker** to
  sessions.jsonl (analysis censors episodes across it — reconnect-rebuild re-OPENs are the same phantom
  class as restarts); `session_start` logs a `build` hash + argv so deploys vs crashes are
  distinguishable; the health beacon carries per-venue `rx_age` so a wedged-but-connected stream is
  observable off-box.
- **NWS CLI revision stream** (`cli_stream`) — polls the NWS Climatological Report for 5 stations
  (NYC/LAX/MDW/MIA/SFO) every 30 min and logs each distinct `(station, report_date, daily-max)` to
  `_data/cli.jsonl`; feeds `scripts/cli_revisions.py`, the settlement **revision-rate** gauge that
  quantifies the one open settlement-timing risk (a downward 8–10 AM CLI correction splitting the venues).
  READ-ONLY public NWS endpoint.
- **Wave-2 ladder + trade logging, WEATHER only** (`weather_poll`; spec
  [tasks/_agent_bus/20260611-probes/ladder-logging-spec.md](../tasks/_agent_bus/20260611-probes/ladder-logging-spec.md))
  — all ADDITIVE: new file prefixes, the `transitions-*` record shape is untouched and every existing
  loader (`analyze_persistence.py` etc.) runs unmodified.
  - **`trades-<event-date>.jsonl`** — Kalshi weather **trade prints** via the public REST cursor-poll
    (`/markets/trades`; envelope `{cursor, trades}` + `min_ts` filter verified live 2026-06-11 — the WS
    `trade` channel stays OUT until its 2-channel sid/seq semantics are probed). Per print: `vt` = venue
    fill time **verbatim**, `t` = local poll receipt, kept separately ([L22] — nothing re-stamps the
    clock); deduped on `trade_id` with a 1 s `min_ts` overlap; a restart seeds from the log tail (like
    `cli_stream`) so nothing is re-logged. ~+3.6 MB/day.
  - **`ladders-<event-date>.jsonl`** — top-5 dual-venue ladder snapshots (`pb/pa/kb/ka`, integer cents,
    qty 1 dp): `k:"tr"` rides every weather transition with the ladders captured **at detection time**
    and the same `t` as the transition record (the join key; a debounce-held CLOSE flushes
    detection-time content, not flush-time — [L22]); `k:"hb"` every `WX_POLL_SEC` (300 s) per tracked
    weather market, **delta-suppressed** when all four ladders are unchanged. ~+2.2 +≤3.7 MB/day.
  - **`fee_changes.jsonl` + a `fee_changes` beacon field** — the `/series/fee_changes` tripwire (envelope
    key `series_fee_change_arr`, verified live): the array turning non-empty / changing logs LOUDLY —
    a scheduled per-series fee change can invalidate `ledger.py`'s pinned coefficients
    ([research/fee-pin-2026-06-10.md](../research/fee-pin-2026-06-10.md)). `deploy/healthcheck.ps1`
    polls the same endpoint laptop-side every 30 min and raises its alert path on non-empty.
  - Cadence is `WX_POLL_SEC` (300 s), deliberately decoupled from `--forever`'s refresh interval;
    offline coverage: `scripts/test_monitor_trades_ladders.py` + the extended `bot/monitor.py` self-test;
    `deploy/pull-data.ps1` finalizes/deletes the new dated files exactly like transitions.
- **Extended run = DigitalOcean droplet deploy** — gated on owner sign-off ([0006](../decisions/0006-deploy-on-digitalocean-consult-first.md)).
- **Deploy is gated** → DigitalOcean droplet, consult the owner first
  ([decisions/0006](../decisions/0006-deploy-on-digitalocean-consult-first.md)).

## Roadmap (not built — read-only phase)

`tasks/todo.md` item: wire `ledger.py` to live `scan_all.py` signals — trade selection, capital
allocation, per-market layer/rotate decisions, and leg-risk fill management. Gated on the persistence
study (`tasks/todo.md` #10) and an explicit move out of the read-only phase.
