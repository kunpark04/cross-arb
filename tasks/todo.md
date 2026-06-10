# TODO — Kalshi × polymarket.us cross-venue arbitrage

**Goal:** measure whether a structurally clean, US-legal cross-venue edge (Kalshi × polymarket.us) is
**persistent and large enough to justify a live trading bot**. Phase: **READ-ONLY** (no orders).
This file is the live plan; the step-by-step history is in [sessions](../docs/sessions.md).

> **Project docs:** [CLAUDE.md](../CLAUDE.md) (index) · [decisions/](../decisions/README.md) · [lessons.md](lessons.md) · [sessions](../docs/sessions.md)

## Reviewer audit (2026-06-09) — fixes ([reviewer-audit-2026-06-09.md](reviewer-audit-2026-06-09.md))

Second adversarial review (92-agent find→verify pass + manual cross-read). 8 CRITICAL root causes + cheap
WARNs. **All landed + self-tests green this session (2026-06-09):**

- [x] **C1 — `ledger.enter()` bypasses crossed/no-arb guard** → now refuses crossed/stale/no-arb + unpriceable
      dirs; S6 regression added. *(+ WARN: mtm/unwind None-guards for one-sided books)*
- [x] **C2 — sports date-join binds the WRONG game** → now uses the pm slug ET date + exact-match (`pick_game`),
      unique-±1 fallback only when slug undated. Mirrored `colisted_map.py` + `scan_all.py`. (173 live pairs.)
- [x] **C3 — sports leg ORIENTATION unverified** → `game_edge` >40c orientation/identity price-guard (live path);
      `verify_sports_settlement.py` written.
- [x] **C4 — weather bucket pairing was a blind positional zip** → pair only on canonical inclusive `[lo,hi]`
      boundary equality (`pm_bounds`/`kbounds`, live-verified convention); loud MISALIGNED report. All 3 matchers.
      (60 live pairs, 0 false misalignments.)
- [x] **C5 — sports settlement-identity** → `scripts/verify_sports_settlement.py` (source + void/postpone diff
      per league); CLAUDE.md tempered (sports cleanliness UNVERIFIED). *Owner: run it live per league.*
- [x] **C6 — monitor WS streams now have supervised reconnect-with-backoff** (clean-close half-dead hole closed);
      `return_exceptions=True`; per-venue `rx_age` in the health beacon.
- [x] **C7 — econ-legality corrected** across CLAUDE.md + README + research/README + decisions/0001 + catalog brief.
- [x] **C8 — capital/profit de-double-counted** (`one_per_market`) + book-average (trapezoid) profit; self-test.
      Corrected live headline: peak ≈ **$13.6k** (was $100k), **~6%/day** (was 8.2%). *(+ persistence headline now
      depth/age-gated + excludes restart-censored; `load()` per-date dedup.)*
- [x] **WARN/INFO sweep** — `smatch` ≤1-char prefix guard + `colisted_map._selftest`; `weather_arb_scan` date
      de-hardcoded (5 cities); pull-data TOCTOU re-hash; security/deploy egress hardening + RO-key warning +
      `.env.example`; settlement-VERIFIED / `age` / brief-count wording.
- [x] **Settlement residuals closed (2026-06-09, same session):**
      - [x] **Weather-FAQ timing contradiction RESOLVED** (WebFetch) — pmus FAQ *does* specify 8 AM / 11 AM-if-
            CLI≠METAR (catalog brief was right; verification brief corrected). Asymmetry narrowed, not eliminated.
      - [x] **Middle 2° bucket boundary VERIFIED** — SFO `66-67°` ↔ pmus `gte66lt67f` both `[66,67]`, same
            source/station; enforced by the `colisted_map.py` guard.
      - [x] **Sports settlement verifier RUN** → finding: clean for completed games, **void/abandonment tail
            diverges** (esports → pmus "last fair price" vs Kalshi silent; tennis >2wk reschedule → $0.50).
            New brief [research/sports-settlement-verification.md](../research/sports-settlement-verification.md).
- [x] **Per-league sports void read DONE (2026-06-09, 10 leagues)** — material finding: **MLB** Kalshi
      reschedule window is **2 days** vs pmus **2 weeks**, so a game replayed in that gap settles real-winner on
      pmus but fair-price-void on Kalshi → both-legs loss on the proof case. Esports/WNBA: pmus last-price vs
      Kalshi silent. In [research/sports-settlement-verification.md](../research/sports-settlement-verification.md).
- [x] **Sports-void EV term BUILT + gate added (2026-06-09):** `capital_sim.void_haircut()` charges the MLB
      postpone divergence (`P(postpone)1.3% · P(2d–2wk gap)0.4 · loss0.5` ≈ 0.26c/contract MLB, 0.10c other
      sports; `--void-mult` knob) — sports profit drops ~7% ($825→$767/day). `colisted_map` tags every sports
      pair `void_clean=False`. Decision [0010](../decisions/0010-all-in-edge-filtering-and-cost-model.md) item 3b.
      Postpone rate grounded in mlbschedulegrid.com (29/31 per ~2430 games, 2024/2023).
- [x] **Read-only execution-feasibility tests built + run (2026-06-09):** `latency_probe` (RTT ~86–261ms,
      network-bound → Rust deferred, Tier 4), `shadow_fill` (leg-fill the gating risk; hit-rate collapses with
      latency), `settle_recon` (invariant #1 empirically open — pmus finalization lag), `adverse_selection`
      (instrumented). Acted: `monitor.py` now logs **ms timestamps** + per-venue `px`. See
      [execution-feasibility brief](../research/execution-feasibility-2026-06-09.md) + [latency-playbook](../research/latency-playbook.md).
- [x] **GATED redeploy DONE (2026-06-10 UTC, owner-greenlit [0006](../decisions/0006-deploy-on-digitalocean-consult-first.md)):**
      droplet brought from a pre-`dabc106` build (integer-second `t`, no `px`, **no ECON**) to current HEAD
      (`monitor.py` sha verified byte-identical to local). Verified live on disk: ms-precision `t`
      (`…082.402`), per-venue `px` touches, **ECON now tracked** (U-3 `urc-…-atl4pt4` OPEN net **0.1222**,
      depth c2=423 — first econ edge ever captured), event-date partitioned (`transitions-2026-07-02.jsonl`).
      The multi-week accumulation clock effectively **restarts now** on the correct schema.
- [x] **Clip-stage allocation tested + phantom fix (2026-06-10, [0012](../decisions/0012-clip-allocation-edge-floor-and-phantom-filter.md), [brief](../research/allocation-policy-2026-06-10.md)):**
      FIFO-by-arrival loses to a **2¢ edge floor + a deploy-to-full per-pair cap** (~+64% to +269% over FIFO
      out-of-sample, 11 diversified pairs); the "wait 1 s + sort" idea captures only 3.6% of the gap; clip-cap
      alone is **risk-control, not PnL** (−3% OOS). Found + fixed a **book-init phantom** (37.7¢ ITF tennis,
      depth 690, captured 1.5 s post-resubscribe in a restart storm, `censored=restart`) that was **75% of the
      old in-sample headline** — `capital_sim.capturable()` now drops restart-censored ([L20]); candidates
      226→219, in-sample +7127%→+1744%, **OOS unchanged**. New `alloc_policy_experiment.py` +
      `clip_threshold_test.py`. **Method demo on 0.81 d** — re-run on the multi-week data.
## Full review 2026-06-10 — ALL FIXES LANDED ([0013](../decisions/0013-econ-grid-step-twin-and-measurement-integrity.md), [econ brief](../research/econ-settlement-identity-2026-06-10.md), lessons [L21]/[L22])

Third adversarial pass (full code+data+docs, live-API verification). **17/17 self-tests green; full
backtest pipeline re-run on the corrected pipeline.**

- [x] **CRITICAL — econ pairing off by one bucket**: pmus `≥T` (inclusive) was joined to Kalshi "Above T"
      (STRICT, `strike_type: greater` — verified live). The persistent **12.2¢ U-3 "edge" was the
      market-priced P(print==T)** (proven: pmus ≥4.4 mid 0.275 vs twin T4.3 mid 0.33 vs old partner T4.4
      mid 0.105). Fixed: `econ_twin` joins `floor = T − grid_step` (24 pairs → **14 identical** + 13 honest
      skips; remapped pairs verified live = no phantom edges); pre-remap econ records **quarantined** in
      `load()`; **headline correction: allocation OOS +64%/+269% → +9%/+141%** (econ-free, 10 pairs).
- [x] **CRITICAL — debouncer stamped CLOSEs at flush time** (+1.0–1.5s on EVERY duration; sub-second regime
      structurally unmeasurable). Fixed: detection-time stamps; pre-fix data lag-corrected −1.25s in
      `build_episodes` (clamped). **Corrected shadow-fill: leg-fail 55.5% @1s / 62.7% @2s** (was 39%/67%);
      ~27% of capturable ≥1¢ episodes die ~instantly.
- [x] **HIGH — WS reconnects now write `ws_reconnect` markers** (both venues, both drop+clean paths);
      `load()` censors them like restarts/resyncs (reconnect-rebuild phantoms were invisible).
- [x] **HIGH — heartbeat supervised** (was: one send-race exception killed discovery/prune/beacon forever);
      **degraded discovery (fetch_errors) skips pruning** (an API outage looked like mass settlement →
      uncensored re-OPEN phantoms); `get()` retries transient errors.
- [x] **HIGH — single-subscription Kalshi invariant**: seq gap / new tickers CYCLE the connection (the old
      in-place second `subscribe` + gap-resubscribe ran on never-probed semantics — silent no-data or a
      seq-counter storm).
- [x] **MEDIUM sweep**: weather pairing = bounds-dict join (offset listings no longer zero out a date);
      econ Dec/Jan year-boundary fix; doubleheader guard (`pick_game` used-set) + duplicate-ticker
      registration guard; `game_edge` ±40¢ orientation guard now covers one-sided pm books;
      `analyze_persistence` headline stats restricted to measured episodes; `shadow_fill` counts FLIP-
      before-fill as leg-fail; `scan_all` imports the bot's matchers + MARGINAL detection fees (private
      copies had drifted, an [L15] violation) + same host; `exit_liquidity` TRADEABLE requires bids;
      `adverse_selection` uses the shared loader + censor-aware pairing; `cod` league mapped (KXCODGAME,
      live-verified).
- [x] **Phantom hardening (no monitor-schema change needed)**: `build_episodes` keeps BOTH venue ages
      (`open_age_p/k`) + an `open_flat` (c2==c1==c0) flag; `capturable(drop_flat=True)` opt-in lens ([L15]).
- [x] **LOW sweep**: maker fee = `ceil(0.0175·N·P(1−P))` per venue-audit §2.1 (was 0.25× the ceiled taker);
      ledger S4 print; `pm_catalog` cap warning; `cli_stream` seeds dedup state from cli.jsonl (no more
      8× restart re-logs); `session_start` logs `build` hash + argv (deploy-vs-crash forensics);
      `selftest_all.py` one-command test gate; doc drift (bot/README fees per-ORDER, etc.).
- [x] **GATED REDEPLOY DONE (2026-06-10 09:03 UTC, owner-greenlit, [0006](../decisions/0006-deploy-on-digitalocean-consult-first.md)):** droplet on the 0013
      build — sha **byte-identical** to local (`bfa9e2fccf15` monitor / `4bc9c241…` colisted_map),
      `session_start` now self-identifies (`build` + argv), tracking **30 weather + 307 sports + 14 econ**
      (remapped twins; old build's last session said 24). First new-schema records verified on-disk
      (detection-time ms stamps + px + depth). `ECON_REMAP_DEPLOY_TS = DEBOUNCE_STAMP_FIXED_TS =
      1781082189` set in `analyze_persistence.py`; pre-epoch econ stays quarantined, post-epoch is clean.
      **The multi-week accumulation clock restarts here on the corrected schema.**
- [x] **Live-WS smoke test DONE (2026-06-10, owner-greenlit):** bounded `--live 75` ran clean — 351 pmus /
      658 Kalshi subscribed, remapped econ pairs priced, OPEN/CLOSE/WIDEN/NARROW logged at detection-time
      ms precision, CLI dedup-seeding worked, clean shutdown. (A logged 13¢ U-3 dir-P record was inspected:
      a REAL wide-book dislocation — pmus 0.47/0.69 vs Kalshi 0.84/0.87 on the now-identical ≥4.2 bucket —
      with flat-ladder + age instrumentation attached for fillability analysis; not a settlement phantom.)
- [ ] **Probe Kalshi multi-subscription semantics** (extend `probe_kalshi_ws.py`: 2nd subscribe on the same
      channel; `update_subscription` add_markets; seq behavior with 2 sids) → would replace cycle-on-add
      with a no-gap add.
- [ ] **Re-pin both venues' fee schedules from primary sources** (Kalshi fee PDF 429'd, pmus fees page is
      JS-only this session); coefficients currently pinned to the venue audits ([kalshi-venue-audit](../research/kalshi-venue-audit.md) §2.1,
      [us-legal-overlap-audit](../research/us-legal-overlap-audit.md)). Also check per-series `fee_multiplier`.
- [ ] **Now: let it run ≥ weeks + re-pull**, then re-run on the new-schema data: `shadow_fill`
      (sub-second leg-fill at ~150ms — measurable only on post-0013 data), `adverse_selection` (toxic-close
      share from `px`), `settle_recon` (after pmus markets pass `endDate`), `analyze_persistence`/`capital_sim`
      (multi-day edge), and a first **ECON** persistence/depth read on the 14 identical pairs.
- [ ] **Still needs the trade layer or in-season data:** (a) the **bot unwind rule** (close MLB before Kalshi's
      2-day window; reads `void_clean`); (b) latency-haircut from a real order-ack study + leg-fill EV (0010 items
      2/3); (c) `p_gap`/`loss_frac` refinement; (d) NBA/NHL settlement read in season; (e) CLI-revision rate.

## Next-session explorations — policy + strategy upgrades (queued 2026-06-10)

Owner-reviewed suggestions from the post-0013 policy read (OOS tables in the corrected pipeline).
Ordered by expected value; none are decisions yet — each is an experiment or spec item.

- [ ] **Pre-register the clip rule BEFORE the multi-week data arrives** (so the next test is
      confirmatory, not another tuning pass): hard 2¢ *booked*-edge floor (≈3.5¢ touch; also the
      friction buffer) + per-pair cap ~10–20% **differentiated by category tail** — weather ~20%
      (identity verified + 1.2d capital + exit window), sports ~10% until the MLB unwind rule exists
      (a void at 20% clip = −10–20% of bankroll), econ smallest (clean but capital-dead). Validation
      plan: K-fold over disjoint multi-day windows, per-position bootstrap CIs, friction inside the
      OOS arm. Rationale: only the *shape* (floor=return, cap=risk, FIFO/batch1s dead — batch1s now
      ties FIFO exactly) is supported on 0.86 d; the 20%-flat row's marginal PnL is just more notional
      in the same ~10 arbs, scaling exactly the tails the paper PnL excludes.
- [ ] **Edge-RATE ranking (the one real policy upgrade):** reservation on
      `booked_edge / expected_lock_days` instead of edge-level — capital velocity is the binding
      constraint and a flat τ gets categories backwards (13¢ U-3 locking ~22 d = **0.6¢/$-day** vs a
      3¢ weather arb locking 1.2 d = **2.5¢/$-day**). Lockups exist in `capital_velocity.py`,
      settlement proxies in `settle_t`; ~small change to the `_profit_per`-based ranking; test with
      the existing OOS harness on the multi-week data.
- [ ] **Maker-side execution study (attacks the gating risk + the fee wall at once):** rest the cheap
      leg as MAKER on the wide/sleepy venue (pmus weather quotes 20¢+ spreads), take the Kalshi side
      only AFTER the maker fill (conditional hedge at the measured ~86–261 ms). Fee asymmetry pays for
      it: taker round-trip ≈3.5¢ vs maker ≈0.9¢ → widens the +EV universe below the 2¢ taker floor
      AND shrinks the 55%-naked-@1s tail. Largely simulatable READ-ONLY from book data (quote-presence
      sim) before any capital.
- [ ] **Fill-contingency rule in the policy spec:** if leg B unfilled within X ms of leg A → exit leg A
      at market immediately (known small insurance premium vs unbounded naked coin-flip); price it into
      the all-in edge filter as 0010's leg-fill EV term. Extend `shadow_fill` to simulate it from `px`.
- [ ] **Adverse-selection gate on direction** (needs accumulated `px`): prefer arbs whose DEAR side
      moved away (benign line-lag) over ones whose CHEAP side led (informed quote — the U-3-style wide
      sleepy book where the tighter venue is righter). Use `adverse_selection.py` toxic-close share.
- [ ] **Correlated-exposure cap per event cluster** (city-date / game), alongside the per-pair cap — a
      single CLI-revision day hits every same-city weather pair at once; per-pair caps don't bound it.
- [ ] **Recycle-time re-evaluation in the sims:** when settlement frees capital, re-score all still-open
      arbs (arrival-or-never skips them today); the live monitor gives this for free — the backtest
      should model what the bot will actually do.
- [ ] **Weather-first scaling note:** the only category with {identity ✓, fast capital ✓, exit ✓};
      constraint is crossable depth (~157 contracts) → growth = breadth (cities × buckets × days) +
      layering on WIDEN, never bigger clips in one corner ([L16]).

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
      OPEN/CLOSE/FLIP/WIDEN/NARROW → `_data/transitions-<event-date>.jsonl` (event-date partitioned, 0009).
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
- [ ] **Open items from the review** — settlement timing **researched + live-object-read** (both ~8 AM ET /
      same morning CLI; residual = downward-correction asymmetry. Kalshi side now **primary-source confirmed**
      via `verify_settlement.py` — rules say *"final value"*, MIA expiry 10 AM EDT; pmus's 8 AM-lock is **still
      third-party** (live object has no timing language) → owner: confirm via QCX support / one observed
      correction day). **Revision-rate logger BUILT + LIVE** (`monitor.py` `cli_stream` → `_data/cli.jsonl`;
      `scripts/cli_revisions.py` reports revised / **downward** / drop-magnitude; day-1 = 0/5 station-days,
      accrues over weeks). Remaining: middle-bucket boundaries; **cost-of-carry** in `capital_sim`; live
      **mid-divergence** guard (L1 in the monitor); WS snapshot-vs-delta confirmation.
- [ ] **Size the bankroll + intraday strategy** — as data accumulates, re-run the harness + simulator to
      set the initial capital (peak concurrent), per-arb clip (depth-capped), and intraday allocation
      (verify/refute the evening-cluster hypothesis). **The scalable lever is breadth of depth-AND-edge
      events** (MLB line-lag — edge *with* depth), each sized to its own book, **not** larger clips in a thin
      corner; capital is sized to peak concurrent *deployable depth*, not opportunity count ([L16](lessons.md)).
      Per the owner this is sizing/tuning, **not** a hard go/no-go gate (confident the arb works).
- [ ] **(then, per user) Live-bot trade-selection** — wire `ledger.py` to live monitor signals: capital
      allocation, per-market layer/rotate, leg-risk fill management. Exits the read-only phase.

## Coverage (all US-legal series)
- [x] **ECON mapped (2026-06-09; pairing corrected 2026-06-10, 0013)** — `colisted_map.ECON` covers
      CPI/U-3/NFP/GDP/Fed via the grid-step twin join (`≥T` ↔ `>T−step`): **14 settlement-identical pairs**
      (+13 no-twin skips; the original 24 equal-number pairs included off-by-one phantoms). Settlement
      identity via `scripts/verify_econ_settlement.py`. Monitor + analyses cover the full US-legal universe.
- [ ] **Politics** (103 pmus markets, US-legal, long-dated) — not yet mapped; needs a per-race rule audit +
      accepts months-long capital lockup. Lower priority. `colisted_map` audit flags it.
- `colisted_map.py`'s audit flags unmapped polymarket.us categories every run. `cod` (KXCODGAME) was
  flagged, verified co-listed, and **mapped 2026-06-10**; `twc` (influencer soccer) has no Kalshi
  co-listing. `scan_all.py` now imports `LEAGUES`/`WX`/`ECON` from `colisted_map` — one config to extend.

## Key finding (2026-06-08)
Edge lives in INEFFICIENT corners, not deep books. Tennis/UFC/ITF (deepest liquidity) = $0 cross-venue
(sharp). Real edge: **MLB** (new-venue line lag) + **weather** (intermittent) — the live monitor has logged
real MLB + weather transitions. Keep ALL in scope; the bot decides when/what to trade.
> **Magnitudes PRELIMINARY** — the old "~$23 MLB / ~$20/day weather" were a single ~8h window under a
> since-fixed fee model + a ~6–7× capital double-count (corrected: peak ≈ $13.6k / ~6%/day). See
> [reviewer-audit C8](reviewer-audit-2026-06-09.md). MLB depth-and-edge is n=1, not yet a class property.

## Status / context
- **Read-only phase** (no orders). Creds verified: polymarket.us (Ed25519) + Kalshi **read-only** key
  (RSA-PSS, `scripts/kalshi_readonly.pem`; read-write key intentionally out of repo, [0007](../decisions/0007-readonly-kalshi-key-least-privilege.md)).
- Monitor build-complete; the next concrete step is the gated day-long data-collection run on a droplet.
- Private GitHub repo `kunpark04/cross-arb` (`origin/main`). Research in `research/`; raw data in
  `scripts/_data/` (gitignored).
