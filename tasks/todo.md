# TODO — Cross-venue sports matcher (Kalshi × polymarket.us)

**Goal:** a *trustworthy* cross-venue moneyline matcher — zero false positives — that measures
real cross-venue edges across the deep sports books, to decide if the strategy is scalable
(vs. weather's verified-but-tiny ~$20/day).

**Why:** weather edge is verified real but lunch-money. Sports has huge capacity (deep books:
50k–99k contracts at 1¢ on some games). If even a 1–2¢ cross-venue gap persists there, it scales.
First-pass matcher (`sports_match.py`) failed: global text-match → ambiguous city names ("Los
Angeles"/"Seattle") produced a false-positive "edge". Need league+date bucketing + real team identity.
(That false positive became lesson [L1](lessons.md).)

> **Project docs:** [CLAUDE.md](../CLAUDE.md) (index) · [decisions/](../decisions/README.md) · [lessons.md](lessons.md) · [sessions](../docs/sessions.md)

## Plan
- [x] **1. Discover Kalshi sports game structure** — Kalshi has 237 per-game series; teams keyed by
      CITY name + ABBREVIATION (BOS), polymarket.us by full name + same abbrev. (`kalshi_sports_discover.py`, `overlap_check.py`)
- [x] **2. Build matcher** — join on (league, date, abbreviation-pair). Robust to naming. (`sports_match_v2.py`)
- [x] **3. Cross-venue 2-outcome arb** — min ask A + min ask B < $1, net of fees, with depth/$ value.
- [x] **4. Validate — no false positives** — price-sanity guard (>40¢ = bad join) auto-rejects
      stale/in-play markets. MLB 23/23 joined; spot-checked clean.
- [x] **5. Measure (team sports)** — MLB ~$23 right now (4 edges, depth 18–321), WNBA efficient.

## Directive (2026-06-08): COMPREHENSIVE COVERAGE, NO PRUNING — [decision 0002](../decisions/0002-comprehensive-coverage-no-pruning.md)
Do NOT drop any market for "not enough edge" — that's the live bot's job later. Keep ALL co-listed
markets in scope and compute the same metrics (net edge, fillable size, $) for every one.

- [x] **6. Verify COMPLETE weather coverage** — polymarket.us = HIGH temp only, 5 cities
      (SFO/LAX/NYC/MIA/MDW); no low/rain. All map to Kalshi. Nothing missed. (`coverage_map.py`)
- [x] **7. Verify COMPLETE sports coverage** — full map done: PM.us 4176 sports mkts, ~22 leagues,
      6 market types (moneyline 284 + futures/props/totals/spreads/drawable). Co-listed moneyline:
      tennis(atp/wta/itf), ufc, mlb, wnba, nba, nhl, cs2/lol/valorant. Kalshi series for each found.
- [x] **8. Name matcher TENNIS/ITF/UFC** (`sports_name_match.py`) — surname join, validated correct
      (prices agree 1-3c). 104 matched; ALL efficient ($0 edge) — kept in scope, not pruned.
- [x] **9. Unified scanner (`scan_all.py`)** — DONE. 202 co-listed markets kept (weather 5 cities +
      12 sports leagues), uniform metrics, nothing pruned -> `_data/scan_all.json`. Snapshot: MLB
      ~$15 (3 edges); weather $0 *this instant* (intermittent — ~$20 mid-day, settles by EOD); all
      sharp sports efficient but retained.
- [x] **10. Time-series / persistence layer** — capture how edges appear / persist / flip / fade per
      market over the day (the input to the bot's layer/rotate logic). **Design decided 2026-06-08:
      NOT blind fixed-cadence snapshot polling** — fixed cadence aliases the transient edges + flips the
      arb actually hunts, burns REST limits across the ~200 co-listed markets, and is stale by one
      interval. **Use event-driven detection instead:** subscribe to each venue's order-book stream,
      recompute the cross-venue edge on every book change, and log edge **state-transitions** (open /
      size-change / flip / close), with periodic full REST sweeps as a resync + coverage heartbeat.
      Flips are transient, so only event-driven catches them in time to drive the ledger's
      rotate-vs-layer call. **Venue reality:** Kalshi has a documented WebSocket (orderbook-delta);
      polymarket.us retail WS is unverified (the gateway we use is REST) → pragmatic **hybrid** = stream
      Kalshi + fast-poll polymarket.us's *active* (at/near-edge) subset + a slow full-universe REST sweep
      for discovery. Cadence by category: weather slow (~60–120s is fine), MLB/sports fast (event-driven
      or ≤15–30s). **Next:** confirm whether polymarket.us exposes a retail WS, then build the hybrid
      monitor (transition log + heartbeat snapshot). (Design rationale: [decision 0003](../decisions/0003-event-driven-persistence.md).)
      **→ UPDATE 2026-06-08 (step 1 done):** polymarket.us retail WS **CONFIRMED** —
      `wss://api.polymarket.us/v1/ws/markets`, slug-keyed, channels MARKET_DATA/LITE/TRADE, ≤100/sub,
      Ed25519 auth on handshake, frames identical to REST book (`scripts/probe_pmus_ws.py`; docs verified).
      Monitor is now **dual-stream**, not hybrid ([decision 0005](../decisions/0005-dual-stream-persistence-monitor.md)).
      **→ UPDATE 2026-06-08 (step 2):** WS auth VERIFIED — upgrade signs `{ts}GET/v1/ws/markets`,
      camelCase subscribe, REST-book frames (`scripts/probe_pmus_ws_auth.py`). Monitor scaffold built
      with a self-verifying transition core (`bot/monitor.py`: OPEN/CLOSE/FLIP/WIDEN/NARROW).
      **Remaining for live:** co-listed `{slug:ticker}` map (`scan_all.py`) + Kalshi `orderbook_delta`
      auth/merge + FLIP debounce + REST heartbeat; deploy GATED → DigitalOcean droplet, consult owner
      ([decision 0006](../decisions/0006-deploy-on-digitalocean-consult-first.md)).
      **→ UPDATE 2026-06-08 (Kalshi half):** Kalshi WS authed `orderbook_delta` **VERIFIED** with the
      **read-only** key — RSA-PSS handshake → `101`, `subscribed` + `orderbook_snapshot`
      (`scripts/probe_kalshi_ws.py`, `scripts/kalshi_readonly.pem`; legacy `trading-api` dead). **Both
      venues' WS now proven.** Remaining: Kalshi snapshot/delta merge into `monitor.py` + co-listed
      `{slug:ticker}` map (`scan_all.py`) + FLIP debounce → live dual-stream run (deploy gated, 0006).
      **→ UPDATE 2026-06-08 (Kalshi merge built):** `bot/kalshi_book.py` (`KalshiBook` + `SeqTracker`)
      reconstructs the Kalshi book from snapshot+deltas — VALIDATED offline + live (28 real deltas, 0 seq
      gaps) — and is wired into `monitor.py`'s `kalshi_stream`. **Both streams now implemented.** Remaining:
      co-listed `{slug:ticker}` map (`scan_all.py`) + FLIP debounce + REST heartbeat → live dual-stream run.
      **→ UPDATE 2026-06-08 (co-listed map):** `bot/colisted_map.py` `build_colisted_map()` does FULL
      discovery (60 weather + 123 sports pairs now) + a COVERAGE AUDIT (caught unmapped league `twc`
      soccer — 1 niche mkt, no Kalshi co-listing, left unmapped). Wired into `monitor.py` run_live
      (weather 1:1) + heartbeat re-discovery ([decision 0008](../decisions/0008-colisted-map-discovery-and-coverage-audit.md)).
      Remaining: SPORTS 2-outcome tracker (game ↔ 2 Kalshi tickers) + dynamic re-subscribe on churn +
      FLIP debounce → short live read-only dual-stream run.
      **→ UPDATE 2026-06-08 (sports 2-outcome tracker + FIRST LIVE RUN):** `GameTracker` (pm game YES=A +
      two Kalshi team tickers; cheapest-venue-per-side `game_edge`) built in `monitor.py`, self-test
      passing, wired into run_live's unified dispatch. **LIVE-VERIFIED** (`--live 75`): tracked 60 weather
      + 124 sports (184 pmus slugs / 292 Kalshi tickers) and logged real MLB OPEN edges (nyy-cle dir PK
      net 0.0375; phi-tor dir KP net 0.0681). Remaining: dynamic re-subscribe on churn + per-frame FLIP
      debounce.
      **→ UPDATE 2026-06-08 (refinements A done):** dynamic re-subscribe (heartbeat adds new weather
      days / games via `register()` + live subscribe) + FLIP debounce (`FlipDebouncer`: CLOSE + opposite
      OPEN within ~1s = one FLIP; flicker suppressed) built, self-tested, and **LIVE-VERIFIED**
      (`--live 120 30`: captured a full LAX weather edge OPEN→WIDEN→NARROW→CLOSE + live MLB edges; heartbeat
      re-discovery ran clean). **Monitor (item #10) BUILD COMPLETE** as a read-only logger; an extended
      data-collection run = DigitalOcean deploy (gated, [0006](../decisions/0006-deploy-on-digitalocean-consult-first.md)).
- [x] **Bot accounting core** (`bot/ledger.py`) — per-market position ledger + PnL simulator;
      self-verifies additive-PnL + outcome-independence; models layer/rotate/hold/leg-risk. Decision
      rule: layer by default; rotate only if first's MTM > locked edge + round-trip cost AND exit
      depth exists; never unwind at MTM loss; a single unhedged leg is the only real downside.
      ([decision 0004](../decisions/0004-ledger-layer-by-default.md))
- [ ] (later, per user) Live-bot TRADE-SELECTION logic — wire `ledger.py` to live `scan_all.py`
      signals; capital allocation + per-market layer/rotate decisions + leg-risk fill management.

## Key finding (2026-06-08)
Edge lives in INEFFICIENT corners, not deep books. Tennis/UFC/ITF (deepest liquidity) = $0 cross-venue
(sharp/arbitraged). Real edge: MLB (~$23, new-venue line lag) + weather (~$20/day). Keep ALL in scope;
the bot decides when/what to trade.

## Status / context
- Read-only phase (no trading). polymarket.us creds verified (Ed25519 signed read = 200).
- Weather: NYC alignment confirmed real; ~$20/day gross across Miami/Chicago/NYC.
- Research + findings in `research/`; scripts + raw data in `scripts/` + `scripts/_data/`.
