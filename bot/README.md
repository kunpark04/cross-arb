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
  shard pattern), new **Kalshi** tickers CYCLE the Kalshi connection (single-subscription invariant: a
  second subscribe's seq semantics were never probed — a seq gap cycles it too). `FlipDebouncer` coalesces
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
- **Extended run = DigitalOcean droplet deploy** — gated on owner sign-off ([0006](../decisions/0006-deploy-on-digitalocean-consult-first.md)).
- **Deploy is gated** → DigitalOcean droplet, consult the owner first
  ([decisions/0006](../decisions/0006-deploy-on-digitalocean-consult-first.md)).

## Roadmap (not built — read-only phase)

`tasks/todo.md` item: wire `ledger.py` to live `scan_all.py` signals — trade selection, capital
allocation, per-market layer/rotate decisions, and leg-risk fill management. Gated on the persistence
study (`tasks/todo.md` #10) and an explicit move out of the read-only phase.
