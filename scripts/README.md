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
- `verify_econ_settlement.py` — **settlement-identity / co-listing check (econ)**: joins pmus↔Kalshi CPI/U-3/NFP/GDP/Fed with the SAME matcher the monitor uses (`econ_parse`/`econ_twin`): pmus `≥T` ↔ its identical Kalshi twin `floor = T−grid_step` ([0013]; the original equal-number join was off by one bucket — the "12.2¢ U-3 edge" was the market-priced P(print==T)). Flags ≤-tails (opposite orientation), point-buckets, and `≥T` markets with no listed twin. Live: **14 identical pairs**. `--family`
- `cli_revisions.py` — **settlement residual-risk gauge**: from the monitor's `_data/cli.jsonl`, reports the NWS CLI daily-max **revision rate** (and the **downward** rate + drop magnitude — the settlement-relevant direction) per station-day; upper-bounds the "downward 8–10 AM correction splits the venues" loss rate. `--selftest`

**Weather**
- `nyc_align_check.py` — resolve the NYC 74–75 bucket alignment between venues (the boundary-equality logic now in `colisted_map.py`)
- `weather_spread_snapshot.py` — live cross-venue weather spread snapshot + fee-netted lock *(superseded display probe; pmus read fixed to marketSides-primary ([L23]) 2026-06-11)*
- `weather_depth.py` — book-depth measurement across all 5 co-listed cities × buckets. `--selftest`. **Finding:** pmus lists only **5** weather cities (the cap; Kalshi has ~22 but a cross-arb needs both); the resting books are DEEP (~244k/~72k contracts) but **CROSSABLE depth is ~tiny** (~157 at edge≥0, ~1 at 2c) — weather is deep-resting but EFFICIENT, lockable size appears intermittently.
- `weather_arb_scan.py` — depth-aware weather scan; date now defaults to TODAY + covers 5 cities, but reads a **cached** pmus snapshot — for the live 5-city universe use `scan_all.py` (the canonical scan)

**Sports**
- `kalshi_sports_discover.py` — discover how Kalshi structures sports game-winner markets
- `overlap_check.py` — check whether both venues actually co-list the same games right now
- `sports_match.py` — first-pass moneyline matcher *(superseded — produced a false positive; see lessons)*
- `sports_match_v2.py` — robust matcher via (league, date, team-abbreviation) join ⭐
- `sports_name_match.py` — name/surname matcher for individual sports (tennis/UFC)
- `coverage_map.py` — complete co-listed coverage audit across all sports + weather
- `probe_mlb_postpone.py` — **MLB postponement-detection probe** (public statsapi, keyless): status/reschedule fields, 30-day postponement scan (5/5 detected w/ `rescheduleDate`; 1.2%/game), Kalshi void-case postmortems, pre-game book depth. Modes: scan/feed/rules/archive/books/postmortem; `--selftest`. **Finding: Kalshi closed voided markets 47–90 min post-start → the unwind rule must act in MINUTES; a 5-min poll suffices.** Spec in the [probe brief](../research/probe-program-2026-06-11.md) §7

**Unified + persistence**
- `selftest_all.py` — **run EVERY offline self-test in the repo** (4 bot cores + 15 script tests) in one command with a PASS/FAIL table; non-zero exit on any failure. The pre-commit / pre-deploy gate. ⭐
- `test_monitor_nogap.py` — **offline integration test** of the no-gap Kalshi subscription path: drives the real `run_live` against a localhost fake Kalshi WS (probe-verified protocol) — asserts in-place `add_markets` (no reconnect), snapshot-confirm, prune `delete_markets`, and the un-snapshotted-add fallback cycle with its `ws_reconnect` censor marker. In `selftest_all`. ✅
- `test_monitor_trades_ladders.py` — **offline test** of the wave-2 logging additions: trade cursor-poll writer (trade_id dedupe + restart seed-from-log-tail), `k:"tr"`/`k:"hb"` ladder records (detection-time stamps [L22], delta suppression), fee-tripwire record, and a transitions-record-shape-unchanged assertion. In `selftest_all`. ✅
- `scan_all.py` — unified scanner over the entire co-listed universe, uniform metrics ⭐ (imports the bot's matchers + MARGINAL detection fees from `colisted_map`/`ledger` — its old private copies drifted, [0013])
- `persistence_scan.py` — repeated weather + sports snapshots over time (early persistence probe)
- `probe_pmus_ws.py` — confirm the polymarket.us retail WebSocket exists (step-1 probe; live, auth-gated 401) ✅
- `probe_pmus_ws_auth.py` — authenticated WS handshake + live snapshot; verifies signed-string + subscribe envelope ✅
- `probe_kalshi_ws.py` — validate the Kalshi `orderbook_delta` WS (RSA-PSS handshake + snapshot VERIFIED with read-only key) ✅; `--multisub` mode (2026-06-10) live-verified the multi-subscription semantics: ONE sid per channel (a 2nd subscribe MERGES into it), control acks consume seq slots, `update_subscription` add/delete is NO-GAP (raw frames in `_data/kalshi_multisub_probe.jsonl`) ✅
- `analyze_persistence.py` — **analysis harness**: reconstructs cross-venue edge EPISODES (OPEN->CLOSE per market, restart-aware) from the pulled `../../data/cross-arb/transitions-*.jsonl[.gz]` + `sessions.jsonl`; reports edge magnitude / persistence (fill window) / capturable rate / scalability. `--selftest` ⭐
- `capital_sim.py` — **capital / throughput model**: from the same data (incl. the monitor's `depth` field) models hold-to-settlement concurrency (Little's Law) -> required-capital <-> daily-return frontier, W-sensitivity, and the intraday arrival profile; answers "how much initial capital / how to maximize the day". `--selftest` ⭐
- `account_sim.py` — **fixed-bankroll account sim**: walks a `--capital` ($500 default) bankroll forward through the capturable arbs in arrival order, recycling capital as positions settle, and splits the book into **REALIZED** (exited at settlement) vs **UNREALIZED** (still locked) + an ending account statement. Answers "with $X, how much actually books vs sits locked" — exit = settlement (pmus freezes the book; no early-exit). `--capital` `--max-clip` `--sweep` `--selftest`. **Finding (0.86 d):** ~0 realized / ~100% locked; PnL = deployed-capital × avg-edge-per-contract, so the clip sweep peaks at moderate diversification.
- `alloc_policy_experiment.py` — **allocation-policy experiment** for the clip stage: account_sim allocates strictly FIFO-by-arrival, which pins the whole bankroll on whatever crosses first. This pits **fifo** vs **batch1s** (wait 1 s, sort that bucket largest-first) vs **offline_knapsack** (clairvoyant profit-density ceiling) vs **reservation** (global edge threshold τ — skip thin arbs, keep powder for fat ones), reusing account_sim's *exact* per-contract economics (only the consideration ORDER differs). `--capital` `--max-clip` `--haircut` `--selftest`. **Finding (0.81 d, $500):** FIFO funds 1/219 (one deep clip eats ~$500); the **1 s batch captures only 3.6%** of the fifo→optimal gap and ties FIFO at $2 k; the lever is **a global edge threshold + a clip cap, NOT a batch window** (in-sample threshold-only +1744% — an oracle ceiling, not a deployable constant; the OOS number is in `clip_threshold_test.py`). See [docs/architecture.md](../docs/architecture.md) "Allocation policy".
- `clip_threshold_test.py` — **TESTS the proposed clip-stage design** (edge threshold τ + clip cap), with an **out-of-sample** check (the in-sample τ sweep is an oracle): (A) τ×clip PnL surface + lever isolation; (B) causal temporal split — fit (τ\*,clip\*) on early arrivals, apply *fixed* to the unseen late half, vs FIFO and vs non-oracle rules; (C) friction sensitivity. `--capital` `--selftest`. **Finding (<1 d, audited; post-[L20] phantom fix; magnitudes corrected by [0013] — econ-quarantined + lag-corrected):** the trustworthy claim is a **non-oracle fixed rule (2¢ floor + a deploy-to-full cap ~10–20%/pair) = +9% to +141% over FIFO out-of-sample** (published +64–269% contained an econ settlement-phantom); the +1744% (in-sample) / +462% (fitted OOS) are oracle/single-obs artifacts (the +462% was 93% one econ contract = the phantom). **Clip-cap alone is risk-control, not PnL (−14% OOS)**; the threshold does the return work. FIFO → $0 under ≥0.5¢ friction. Method demo on <1 d — real OOS needs weeks. Audit: `tasks/_agent_bus/20260610-0522/stats-ml-logic-reviewer.md`.
- `recycle_arm_experiment.py` — **EXPLORATORY (outside the 0014 prereg freeze)**: recycle-time reconsideration arm (re-score still-open skipped candidates when settlement frees capital) + city-date cluster-exposure measurement + opt-in `--cluster-cap`. `--selftest`. **1.8 d read: recycle free-capture $0.00 (structurally starved — skipped arbs die ~1 s median, capital doesn't bind under H1); max same-(city,date) exposure 20%; 20/30% caps bind nothing (10% costs −13.6%)** ([brief](../research/probe-program-2026-06-11.md) §8–9)

**Execution feasibility (read-only empirical tests, 2026-06-09 — see [research/execution-feasibility-2026-06-09.md](../research/execution-feasibility-2026-06-09.md) + [probe-program-2026-06-11.md](../research/probe-program-2026-06-11.md))**
- `latency_probe.py` — read-path RTT to both venues (lower bound on order latency) + the serial-vs-concurrent two-leg floor. `--selftest`. Verdict: ~86–261ms, network-bound (compute/language is noise).
- `shadow_fill.py` — shadow leg-fill simulator: replays the edge trajectory to measure fill-survival % / realized edge / naked-leg rate vs assumed entry latency L; **sub-second grid + `--post-epoch`** (post-0013 detection-time data only). `--selftest`. **First 16 h read (2026-06-11): naked 17% @100 ms / 23% @150 ms / 29% @250 ms (n=575); ~29% of ≥1¢ episodes die <250 ms — taker NOT rejected at measured RTT** ([brief](../research/probe-program-2026-06-11.md) §2)
- `settle_recon.py` — empirical invariant-#1 test: **three-way recon** (Kalshi result vs pmus outcome vs NWS CLI), marketSides-primary pmus winner read ([L23]). `--selftest`. **Weather CLOSED 2026-06-11: 360/360 identical** (the old "unreliable interim" finding was our parse bug); re-run sports ~06-23/25 (post-`endDate`), econ 06-18 + 07-03
- `adverse_selection.py` — "why is the cheap side cheap?": attributes each edge close to cheap-rose (benign) vs dear-fell (toxic), + an **at-open direction classifier** (computable for ~78% of opens). `--selftest`. **16 h read: NULL as a skip gate overall; weather-only leg-sequencing signature (cheap-made 18% vs dear-made 79% toxic, z=4.6) = hypothesis to pre-register** ([brief](../research/probe-program-2026-06-11.md) §5)
- `exit_liquidity.py` — can you EARLY-EXIT a pmus position once the outcome is known? `--selftest`. **MEASURED: NO** — pmus freezes the book at resolution (10/10 resolved markets empty), so capital is locked to `endDate` (~15d sports); no same-day exit.
- `capital_velocity.py` — capital-velocity / early-exit lens: per-arb edge × how fast capital recycles, per category. `--selftest`. Shows velocity (not edge) separates categories; weather fast (~1.2d), sports/econ slow (~15d+), early-exit measured-unavailable.
- `maker_feasibility.py` — **maker-side weather study (read-only BOUNDS)**: exact per-series fee math (maker/taker × venue, `fee_type`-aware), level-crossing fill-rate bounds from transition-sampled touches, hedge-slippage bounds. `--selftest`. **Verdict: only rest-on-KALSHI + taker-hedge-pmus is plausibly +EV (+0.14–0.44¢/attempt); rest-on-pmus is structurally toxic (15–16¢ slip).** Becomes a measurement once the ladder/trade logging deploys ([spec](../tasks/_agent_bus/20260611-probes/ladder-logging-spec.md))
- `early_exit_ev.py` — **weather early-exit EV**: hold-to-settlement vs evening exit (taker/maker variants, boundary-day split, capital-value + P(flip) sensitivity, breakeven frontier). `--selftest`. **Verdict: HOLD-ALL** (exit-all ≈ −1.5¢/pair; boundary maker-exit breakeven P(flip)=2% vs 0 downward flips in 14 station-days measured)
- `ev_at_latency.py` — **profitability translation of the leg-fill measurements**: per-episode EV(L,u) on the post-0013 capturable ≥1¢ cohort (both-fill → realized edge; one-leg → −unwind u), dollarized at c1/c2 depth + the $500 H1 replay. `--selftest`. **16 h read: EV +1.5–2.3¢/contract at measured RTT (u≤3¢); latency tax ≈31% of paper flow @150 ms; weather goes EV-negative only at 1 s**
- `probe_account_readonly.py` — **account audit (read-only key)**: signed GETs of portfolio balance / fills / orders / positions / settlements; distinguishes app/UI fills (empty `client_order_id`, fractional `count_fp`) from API fills. Used 2026-06-11 to show a "−$18.31" was the owner's own app activity, not project trading. Raw → `_data/` (gitignored)

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
