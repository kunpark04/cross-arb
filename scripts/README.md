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

**Weather**
- `nyc_align_check.py` — resolve the NYC 74–75 bucket alignment between venues
- `weather_spread_snapshot.py` — live cross-venue weather spread snapshot + fee-netted lock
- `weather_arb_scan.py` — clean, depth-aware weather arb scan with fillable size ⭐

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

> The next work item (`../tasks/todo.md` #10) replaces `persistence_scan.py`'s fixed-cadence approach
> with an **event-driven, dual-stream WebSocket** monitor (Kalshi + polymarket.us). polymarket.us's
> retail WS is confirmed (`probe_pmus_ws.py`) — see [decisions/0003](../decisions/0003-event-driven-persistence.md)
> + [0005](../decisions/0005-dual-stream-persistence-monitor.md).
