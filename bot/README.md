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

- **Kalshi** taker: `ceil(0.07 · N · P·(1−P))` per contract (maker = 0.25×).
- **polymarket.us** taker: `0.05 · P·(1−P)` per contract (maker = 0).

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
  city/league we don't map. `python bot/colisted_map.py`. Refreshed on the heartbeat ([0008](../decisions/0008-colisted-map-discovery-and-coverage-audit.md)).
- **Two tracker types:** WEATHER → `MarketTracker` (1:1 binary); SPORTS → `GameTracker` (2-outcome: a pm
  game market + two Kalshi team tickers, cheapest-venue-per-side). `run_live` dispatches each book delta
  to the right one. **Live-verified** 2026-06-08 (`--live 75`: 60 weather + 124 sports tracked; logged
  real MLB edges nyy-cle PK +3.75¢, phi-tor KP +6.81¢).
- **Dynamic re-subscribe + FLIP debounce** — the heartbeat re-discovers and subscribes new weather days /
  games (`register`); `FlipDebouncer` coalesces a CLOSE + opposite-direction OPEN within ~1s into one FLIP
  (same-dir reopen = suppressed flicker). Both live-verified 2026-06-08 (captured a full LAX weather edge
  OPEN→WIDEN→NARROW→CLOSE + live MLB edges).
- **Extended run = DigitalOcean droplet deploy** — gated on owner sign-off ([0006](../decisions/0006-deploy-on-digitalocean-consult-first.md)).
- **Deploy is gated** → DigitalOcean droplet, consult the owner first
  ([decisions/0006](../decisions/0006-deploy-on-digitalocean-consult-first.md)).

## Roadmap (not built — read-only phase)

`tasks/todo.md` item: wire `ledger.py` to live `scan_all.py` signals — trade selection, capital
allocation, per-market layer/rotate decisions, and leg-risk fill management. Gated on the persistence
study (`tasks/todo.md` #10) and an explicit move out of the read-only phase.
