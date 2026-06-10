# scripts/ — read-only probes + scanners

Throwaway-grade Python that answers one question each (named for the question). **All read-only** —
nothing here places an order. Raw API pulls and snapshots land in `_data/` (gitignored); durable
conclusions get promoted into a `../research/` brief.

## Setup

```bash
cp .env.example .env   # ONLY needed for auth'd checks (verify_pmus_auth.py). Market data is public.
python <script>.py
```

Market **data** on both venues is public (no key). Keys (`.env`) are only for *verifying* order-API
read access, never to trade in this phase.

## Scripts by phase

**Venue / API discovery**
- `probe_apis.py` — probe both venues' APIs for reachability + JSON shape
- `enumerate_catalogs.py` — dump both venues' live market catalogs for overlap analysis
- `pmus_dig.py` — inspect polymarket.us markets by flags/families/server-side filters
- `pmus_open.py` — pull full polymarket.us open (non-sports) catalog with settlement detail
- `probe_books.py` — establish live order-book access + market structure on both venues
- `find_pmus_book.py` — brute-force discover polymarket.us depth endpoint
- `inspect_pmus_book.py` — inspect polymarket.us public order-book structure + depth
- `test_pmus_pubbook.py` — verify the public depth endpoint on live markets
- `verify_pmus_auth.py` — one-shot polymarket.us Ed25519 auth credential check

**Matching (settlement + identity)**
- `match_kalshi.py` — match Kalshi series ↔ polymarket.us families w/ fees + settlement sources
- `settlement_timing.py` — measure settlement hold duration + capital lockup per family
- `kalshi_calendar.py` — forward calendar per matched series; soonest settle dates
- `verify_settlement.py` — **settlement-identity check (weather)**: pulls BOTH venues' live rules per co-listed weather city, diffs source/station/boundary/timing + flags mismatches (resolves the #1 thesis risk) ⭐
- `verify_sports_settlement.py` — **settlement-identity check (sports, review C5)**: per league, diffs the named result SOURCE + the void/postponement/forfeit handling on both venues (a contested game can settle the two legs opposite). `--league`
- `verify_econ_settlement.py` — **settlement-identity / co-listing check (econ)**: joins pmus↔Kalshi CPI/U-3/NFP/GDP/Fed on family+period+threshold, flags the ≥-vs-> boundary, the ≤-tail opposite-orientation, and point-buckets. Established the 24 clean econ pairs now in `colisted_map.ECON`. `--family`
- `cli_revisions.py` — **settlement residual-risk gauge**: from the monitor's `_data/cli.jsonl`, reports the NWS CLI daily-max **revision rate** (and the **downward** rate + drop magnitude — the settlement-relevant direction) per station-day; upper-bounds the "downward 8–10 AM correction splits the venues" loss rate. `--selftest`

**Weather**
- `nyc_align_check.py` — resolve the NYC 74–75 bucket alignment between venues (the boundary-equality logic now in `colisted_map.py`)
- `weather_spread_snapshot.py` — live cross-venue weather spread snapshot + fee-netted lock
- `weather_depth.py` — book-depth measurement across all 5 co-listed cities × buckets. `--selftest`. **Finding:** pmus lists only **5** weather cities (the cap; Kalshi has ~22 but a cross-arb needs both); the resting books are DEEP (~244k/~72k contracts) but **CROSSABLE depth is ~tiny** (~157 at edge≥0, ~1 at 2c) — weather is deep-resting but EFFICIENT, lockable size appears intermittently.
- `weather_arb_scan.py` — depth-aware weather scan; date now defaults to TODAY + covers 5 cities, but reads a **cached** pmus snapshot — for the live 5-city universe use `scan_all.py` (the canonical scan)

**Sports**
- `kalshi_sports_discover.py` — discover how Kalshi structures sports game-winner markets
- `overlap_check.py` — check whether both venues actually co-list the same games right now
- `sports_match.py` — first-pass moneyline matcher *(superseded — produced a false positive; see lessons)*
- `sports_match_v2.py` — robust matcher via (league, date, team-abbreviation) join ⭐
- `sports_name_match.py` — name/surname matcher for individual sports (tennis/UFC)
- `coverage_map.py` — complete co-listed coverage audit across all sports + weather

**Unified + persistence**
- `scan_all.py` — unified scanner over the entire co-listed universe, uniform metrics ⭐
- `persistence_scan.py` — repeated weather + sports snapshots over time (early persistence probe)
- `probe_pmus_ws.py` — confirm the polymarket.us retail WebSocket exists (step-1 probe; live, auth-gated 401) ✅
- `probe_pmus_ws_auth.py` — authenticated WS handshake + live snapshot; verifies signed-string + subscribe envelope ✅
- `probe_kalshi_ws.py` — validate the Kalshi `orderbook_delta` WS (RSA-PSS handshake + snapshot VERIFIED with read-only key) ✅
- `analyze_persistence.py` — **analysis harness**: reconstructs cross-venue edge EPISODES (OPEN->CLOSE per market, restart-aware) from the pulled `../../data/cross-arb/transitions-*.jsonl[.gz]` + `sessions.jsonl`; reports edge magnitude / persistence (fill window) / capturable rate / scalability. `--selftest` ⭐
- `capital_sim.py` — **capital / throughput model**: from the same data (incl. the monitor's `depth` field) models hold-to-settlement concurrency (Little's Law) -> required-capital <-> daily-return frontier, W-sensitivity, and the intraday arrival profile; answers "how much initial capital / how to maximize the day". `--selftest` ⭐
- `account_sim.py` — **fixed-bankroll account sim**: walks a `--capital` ($500 default) bankroll forward through the capturable arbs in arrival order, recycling capital as positions settle, and splits the book into **REALIZED** (exited at settlement) vs **UNREALIZED** (still locked) + an ending account statement. Answers "with $X, how much actually books vs sits locked" — exit = settlement (pmus freezes the book; no early-exit). `--capital` `--max-clip` `--sweep` `--selftest`. **Finding (0.86 d):** ~0 realized / ~100% locked; PnL = deployed-capital × avg-edge-per-contract, so the clip sweep peaks at moderate diversification.
- `alloc_policy_experiment.py` — **allocation-policy experiment** for the clip stage: account_sim allocates strictly FIFO-by-arrival, which pins the whole bankroll on whatever crosses first. This pits **fifo** vs **batch1s** (wait 1 s, sort that bucket largest-first) vs **offline_knapsack** (clairvoyant profit-density ceiling) vs **reservation** (global edge threshold τ — skip thin arbs, keep powder for fat ones), reusing account_sim's *exact* per-contract economics (only the consideration ORDER differs). `--capital` `--max-clip` `--haircut` `--selftest`. **Finding (0.86 d, $500):** FIFO funds 1/226 (one deep clip eats $499.97); the **1 s batch captures only 3.6%** of the fifo→optimal gap and ties FIFO at $2 k; the lever is **reservation (+7127%, ties the clairvoyant ceiling) — a global edge threshold + a clip cap, NOT a batch window**. τ is swept in-sample (oracle ceiling, not a deployable constant). See [docs/architecture.md](../docs/architecture.md) "Allocation policy".

**Execution feasibility (read-only empirical tests, 2026-06-09 — see [research/execution-feasibility-2026-06-09.md](../research/execution-feasibility-2026-06-09.md))**
- `latency_probe.py` — read-path RTT to both venues (lower bound on order latency) + the serial-vs-concurrent two-leg floor. `--selftest`. Verdict: ~86–261ms, network-bound (compute/language is noise).
- `shadow_fill.py` — shadow leg-fill simulator: replays the edge trajectory to measure fill-survival % / realized edge / naked-leg rate vs assumed entry latency L. `--selftest`. (sub-second regime needs the new ms-timestamp data.)
- `settle_recon.py` — empirical invariant-#1 test: compares both venues' *settled* outcomes on co-listed markets. `--selftest`. (Surfaced: pmus `closed`≠finalized, ~2wk lag, unreliable interim data — re-run after `endDate`.)
- `adverse_selection.py` — "why is the cheap side cheap?": attributes each edge close to cheap-rose (benign) vs dear-fell (toxic). `--selftest`. (Needs the monitor's new `px` field; accrues after redeploy.)
- `exit_liquidity.py` — can you EARLY-EXIT a pmus position once the outcome is known? `--selftest`. **MEASURED: NO** — pmus freezes the book at resolution (10/10 resolved markets empty), so capital is locked to `endDate` (~15d sports); no same-day exit.
- `capital_velocity.py` — capital-velocity / early-exit lens: per-arb edge × how fast capital recycles, per category. `--selftest`. Shows velocity (not edge) separates categories; weather fast (~1.2d), sports/econ slow (~15d+), early-exit measured-unavailable.

⭐ = the current canonical script for that job.

## `_data/` outputs (gitignored)

| File | Contents |
|---|---|
| `pmus_markets.json` | Full polymarket.us market catalog dump |
| `kalshi_series.json` | Kalshi series reference (tickers, titles, settlement sources) |
| `pmus_open_markets.json` | Filtered polymarket.us open markets |
| `name_match.json` | Tennis/UFC cross-venue matches by player name |
| `scan_all.json` | Unified scanner output (all weather + sports markets + edges) |
| `persistence_log.jsonl` | Per-snapshot persistence-probe rows |
| `persistence_summary.txt` | Aggregated persistence summary |
| `transitions.jsonl` | **Live monitor output** — edge state-transitions (OPEN/CLOSE/FLIP/WIDEN/NARROW) from `bot/monitor.py` |
| `cli.jsonl` | **Live monitor output** — distinct NWS CLI daily-max issuances per `(station, report_date)` (settlement-revision tracking; read by `cli_revisions.py`) |

> `persistence_scan.py` (fixed-cadence) is **superseded** by the build-complete **event-driven
> dual-stream monitor** `bot/monitor.py` (writes `transitions.jsonl`) — see [decisions/0003](../decisions/0003-event-driven-persistence.md)
> + [0005](../decisions/0005-dual-stream-persistence-monitor.md) and `bot/README.md`.
