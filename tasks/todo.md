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
- [x] **Idle-market pruning** — `run_live` frees settled markets (gone from discovery for
      `PRUNE_THRESHOLD=2` heartbeats; symmetric teardown). Verified flat heap ~7 MB over 30 sim-days vs
      ~216 MB unpruned (`scripts/probe_monitor_footprint.py`) → droplet = **1 vCPU·1 GB·NYC1·Ubuntu 24.04**.
- [x] **DEPLOYED + LIVE (2026-06-09)** — monitor runs 24/7 on the DO droplet (`cross-arb-droplet`,
      `198.199.67.245`) as a confined **`cross-arb`** user (owns only `/opt/cross-arb`, `0700`;
      `ProtectSystem=strict`). `deploy/deploy.sh` ships only the runtime cone via scp; secrets out-of-band.
      Both streams up, logging real MLB + weather transitions. RSS ~76 MB.
- [x] **Foolproof data pipeline** ([0009](../decisions/0009-event-date-partition-copy-keep-pull.md)) —
      monitor partitions by **event-date** (`transitions-<date>.jsonl`, lifecycle never split at midnight) +
      `sessions.jsonl` restart marker. `deploy/pull-data.ps1` = copy-keep + sha256-verified + idempotent
      mirror to `Kalshi/data/cross-arb/`, gzips finalized days; scheduled daily (`PullCrossArbData`, 8:30am).
- [ ] **Let it run + pull** — accumulate ≥days of `Kalshi/data/cross-arb/` data; spot-check the daily pull.
- [x] **Persistence-analysis harness** (`scripts/analyze_persistence.py`) — reconstructs edge episodes
      (OPEN→CLOSE per market, restart-aware via `sessions.jsonl`) → edge-magnitude / persistence (fill
      window) / capturable-rate / scalability proxy. Self-tested; validated on preliminary data (0.8 h:
      median edge ~0.7c, median duration ~2 s with a thin persistent tail — the MLB line-lag). Re-run as
      the dataset grows.
- [x] **Depth-logging + capital/throughput simulator** — monitor logs per-transition fillable depth
      (`depth:{c2,c1,c0}` = contracts at gross marginal edge ≥2c/1c/0c, both legs); `scripts/capital_sim.py`
      models hold-to-settlement concurrency (Little's Law) → required-capital ↔ daily-return frontier,
      W-sensitivity + intraday arrival profile. Self-tested; validated end-to-end (data still too sparse for
      a read). MLB edges show **thousands of contracts of depth** (e.g. `lad-pit` c2≈4800).
- [x] **Independent review + hardening (2026-06-09)** — fresh-eyes adversarial review
      ([tasks/independent-review-2026-06-09.md](independent-review-2026-06-09.md)) → fixed per-order fee +
      float-ceil (C1), entry guard (C2), crossed-book rejection (C3), seq-resync marker (C6); added
      per-transition staleness (`age`) + clean-fillable filter (L2); **verified settlement identity**
      (`scripts/verify_settlement.py` → same NWS CLI Daily + station + boundary, 5 cities;
      [research/settlement-verification.md](../research/settlement-verification.md)).
- [ ] **All-in edge filter + cost model** ([0010](../decisions/0010-all-in-edge-filtering-and-cost-model.md)) —
      today we filter only the QUOTED taker-fee-net edge (fees + spread in; **slippage / latency / leg-fill
      risk OUT**). Build the all-in filter: size-aware slippage (from `depth`, + net-marginal depth curve),
      a measured **latency** haircut (order-ack study), and a **leg-fill-failure** EV term
      (`P(both)·quoted − P(one)·naked-loss`). [`make_px`/`signal`/`game_edge` per-direction pricing for
      one-sided books — **done 2026-06-09**.]
- [ ] **Open items from the review** — settlement **timing/revision** (pmus rulebook) + middle-bucket
      boundaries; **cost-of-carry** in `capital_sim`; live **mid-divergence** guard (L1 in the monitor);
      WS snapshot-vs-delta confirmation.
- [ ] **Size the bankroll + intraday strategy** — as data accumulates, re-run the harness + simulator to
      set the initial capital (peak concurrent), per-arb clip (depth-capped), and intraday allocation
      (verify/refute the evening-cluster hypothesis). Per the owner this is sizing/tuning, **not** a hard
      go/no-go gate (confident the arb works).
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
