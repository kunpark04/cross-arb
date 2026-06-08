# TODO — Kalshi × polymarket.us cross-venue arbitrage

**Goal:** measure whether a structurally clean, US-legal cross-venue edge (Kalshi × polymarket.us) is
**persistent and large enough to justify a live trading bot**. Phase: **READ-ONLY** (no orders).
This file is the live plan; the step-by-step history is in [sessions](../docs/sessions.md).

> **Project docs:** [CLAUDE.md](../CLAUDE.md) (index) · [decisions/](../decisions/README.md) · [lessons.md](lessons.md) · [sessions](../docs/sessions.md)

## Done (discovery → matcher → scanner → monitor)
- [x] **1–5. Sports matcher** — Kalshi game structure discovered; robust `(league, date, abbrev)` join
      (`sports_match_v2.py`); 2-outcome arb metric; **no false positives** (price-sanity guard, [L1](lessons.md)); MLB ~$23.
- [x] **6–8. Complete coverage** — weather = HIGH temp, 5 cities (all map to Kalshi); 12 co-listed
      moneyline leagues; tennis/UFC/ITF surname matcher (`sports_name_match.py`). No pruning
      ([0002](../decisions/0002-comprehensive-coverage-no-pruning.md)).
- [x] **9. Unified scanner** (`scan_all.py`) — entire co-listed universe, uniform metrics, nothing
      pruned → `_data/scan_all.json`.
- [x] **10. Persistence monitor** (`bot/monitor.py`) — **BUILD COMPLETE, live-verified.** Event-driven
      dual-stream logger ([0003](../decisions/0003-event-driven-persistence.md) · [0005](../decisions/0005-dual-stream-persistence-monitor.md)):
      polymarket.us WS (Ed25519) + Kalshi `orderbook_delta` WS (RSA-PSS; `kalshi_book.py` snapshot/delta
      merge), self-discovering + coverage-audited map (`colisted_map.py`, [0008](../decisions/0008-colisted-map-discovery-and-coverage-audit.md)),
      weather `MarketTracker` + sports `GameTracker`, FLIP debounce, dynamic re-subscribe. Logs
      OPEN/CLOSE/FLIP/WIDEN/NARROW → `_data/transitions.jsonl`.
- [x] **Accounting core** (`bot/ledger.py`) — self-verifying PnL; layer-by-default rotate rule
      ([0004](../decisions/0004-ledger-layer-by-default.md)).

## Next
- [ ] **Collect persistence data** — run `bot/monitor.py` continuously for ≥1 day to capture how edges
      persist / flip / fade. This = the **DigitalOcean droplet deploy**, GATED on owner sign-off
      ([0006](../decisions/0006-deploy-on-digitalocean-consult-first.md)). On-host: secrets off-git,
      process supervision (systemd), durable `transitions.jsonl`, log pull-back.
- [ ] **Go/no-go gate** — from the collected data, decide whether the edge is persistent/scalable enough
      to build the live trading bot. File the verdict as a decision when reached.
- [ ] **(then, per user) Live-bot trade-selection** — wire `ledger.py` to live monitor signals: capital
      allocation, per-market layer/rotate, leg-risk fill management. Exits the read-only phase.

## Open coverage note
`colisted_map.py`'s audit flags unmapped polymarket.us categories every run. Currently unmapped: `twc`
(influencer soccer, 1 mkt, no Kalshi co-listing) — intentionally not mapped. If a *real* new co-listed
city/league appears, add it to `WX`/`LEAGUES` (in `colisted_map.py` **and** `scan_all.py`).

## Key finding (2026-06-08)
Edge lives in INEFFICIENT corners, not deep books. Tennis/UFC/ITF (deepest liquidity) = $0 cross-venue
(sharp). Real edge: **MLB** (~$23, new-venue line lag) + **weather** (~$20/day, intermittent) — the live
monitor has logged real MLB + weather transitions. Keep ALL in scope; the bot decides when/what to trade.

## Status / context
- **Read-only phase** (no orders). Creds verified: polymarket.us (Ed25519) + Kalshi **read-only** key
  (RSA-PSS, `scripts/kalshi_readonly.pem`; read-write key intentionally out of repo, [0007](../decisions/0007-readonly-kalshi-key-least-privilege.md)).
- Monitor build-complete; the next concrete step is the gated day-long data-collection run on a droplet.
- Private GitHub repo `kunpark04/cross-arb` (`origin/main`). Research in `research/`; raw data in
  `scripts/_data/` (gitignored).
