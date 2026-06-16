# CLAUDE.md — cross-arb

Index + working agreement for this project. Read this first every session. It points to every
managerial doc; it does **not** duplicate their content.

## What this is

Research into a **cross-venue arbitrage** between two US-legal, CFTC-regulated prediction markets:
**Kalshi** and **polymarket.us** (QCX). The bet: when the *same real-world event* is listed on both
venues and **both grade off the identical deterministic public number** (e.g. NWS CLI high temp), a
price gap is a structurally clean, US-legal arb — buy YES on the cheap venue + NO on the dear venue,
hold to settlement, collect the gap net of fees.

**Phase: READ-ONLY → live-capable (owner override 2026-06-11, [0015](decisions/0015-owner-override-live-trading-phase.md)).**
The research/monitor work is read-only; on 2026-06-11 the owner **overrode** the read-only governance to
build the **live trading bot** (`bot-rs/`, Rust). It is **SAFE BY DEFAULT** — dry-run / demo-sandbox /
1-contract caps / kill-switch — and the same-day readiness audit's **NOT-READY** verdict plus a
**staged-rollout protest-of-record** stand (dry-run → demo → 1-contract prod → scale only after 0014
validates). **Live order submission runs in the owner's environment, not here** (the sandbox blocks it;
the read-write key stays external, never in repo). All market data on both venues
is public (no auth needed for books). The read-only **persistence monitor** (`bot/monitor.py`, todo #10)
is **DEPLOYED + LIVE** since 2026-06-09 — running 24/7 on a DigitalOcean droplet as a confined `cross-arb`
user ([0006](decisions/0006-deploy-on-digitalocean-consult-first.md)), collecting the event-date-partitioned
persistence dataset pulled daily to `Kalshi/data/cross-arb/` ([0009](decisions/0009-event-date-partition-copy-keep-pull.md)).
See [deploy/README.md](deploy/README.md).

## Core findings (as of 2026-06-11)

- **Econ (CPI/FOMC/GDP/NFP/U-3) IS live and US-legal on polymarket.us** — 36 live macro markets, each settling
  on the *same* government print Kalshi uses (BLS/BEA/Fed); per [research/us-legal-overlap-audit.md](research/us-legal-overlap-audit.md)
  this is the *structurally cleanest* (identical-deterministic-number) subset, though episodic/consensus-priced
  so the spread is event-driven. **Only crypto is genuinely US-blocked** (absent from polymarket.us). So the
  US-legal overlap is **econ + weather + sports** (+ politics). *(Corrected 2026-06-09 reviewer audit C7.)*
  **Econ pairing CORRECTED 2026-06-10 ([0013](decisions/0013-econ-grid-step-twin-and-measurement-integrity.md),
  [research/econ-settlement-identity-2026-06-10.md](research/econ-settlement-identity-2026-06-10.md)):** the
  original join paired pmus `≥T` (inclusive, "at least") with Kalshi "Above T" (**strict**, `strike_type:
  greater`) — **off by one print-grid bucket**; an exact-on-T print settles the venues oppositely, and that
  outcome is the *modal region* for an ATM threshold, so the pair's cross-venue gap is the market-priced
  P(print==T) — the observed persistent **12.2¢ U-3 "edge" was this phantom**, not an arb. The matcher now joins
  on the settlement-identical twin `floor = T − grid_step` (0.1 for U-3/CPI/GDP, 1000 for NFP; Fed categorical
  unchanged): live result **14 identical pairs** (+13 honest no-twin skips, was 24 false-ish pairs); remapped
  pairs verified live = no phantom edges. Pre-remap threshold-econ records are **quarantined** in
  `analyze_persistence.load()`. So the monitor/analyses cover the full US-legal universe (weather + sports +
  econ-identical), with econ data restarting clean at the next gated redeploy.
- **Settlement identity for weather: source + station + bucket boundaries VERIFIED**
  (`scripts/verify_settlement.py`, 2026-06-09): all 5 mapped cities grade off the **same** NWS Climatological
  Report (Daily), at the **same station** (incl. NYC = Central Park). Bucket boundaries verified on a low-tail
  AND a **middle 2° bucket** (SFO `66-67°` ↔ pmus `gte66lt67f`, both inclusive `[66,67]`, same source/station);
  `colisted_map.py` now **enforces** boundary-number equality (`pm_bounds`/`kbounds`) and refuses+flags any
  mismatch. Refutes the old NWS-vs-Wunderground fear (`miami-temp-arb.html`). **Settlement timing RESOLVED**:
  both sides now **primary-source confirmed** — Kalshi waits past 8 AM for the *final* (MIA expiry 10 AM EDT),
  and the pmus weather **FAQ** specifies **8 AM**, delaying to **11 AM only for a CLI-vs-METAR inconsistency**
  (not a downward CLI correction). So the one residual is a *downward* 8–11 AM CLI correction on a boundary day
  (asymmetry **narrowed**, not eliminated); its **rate** is logged live (`monitor.py` `cli_stream` → `cli.jsonl`
  → `scripts/cli_revisions.py`). See [research/settlement-verification.md](research/settlement-verification.md).
- **Settlement identity for SPORTS: clean only for games that COMPLETE on schedule** (new 2026-06-09,
  `scripts/verify_sports_settlement.py`, 10 leagues read): normal completed games agree, but the **postpone/
  void tail diverges — materially for MLB, the depth-and-edge proof case.** Kalshi waits for a replay only if
  rescheduled **≤2 days** (else voids to *"a fair price"*); pmus waits **≤2 weeks** (else *last-traded*). So a
  rain-postponed game replayed in that **2-day–2-week gap settles to the real winner on pmus but a fair-price
  void on Kalshi** → the YES/NO legs stop offsetting → **both-legs loss** (and even a symmetric void doesn't
  net: Kalshi *"fair price"* ≠ pmus *"last-traded"*). Esports/WNBA non-completion: pmus → last-price, Kalshi
  silent. **Mitigation PROBED (2026-06-11): the actionable window is MINUTES, not 2 days** — Kalshi *closed*
  voided markets **47–90 min after scheduled start** (n=3 live cases), so the rule is "unwind on
  postponement-detection": a 5-min MLB-statsapi poll surfaces `Postponed` + reschedule date immediately (5/5 in
  a 30-day scan; 1.2%/game matches 0010's prior), pre-game unwind books are 1¢-spread deep, and unwind ≈
  **+12–13¢/contract vs holding** through the void EV — rule spec in
  [research/probe-program-2026-06-11.md](research/probe-program-2026-06-11.md) §7; void EV term modeled (0010).
  See [research/sports-settlement-verification.md](research/sports-settlement-verification.md).
- **Edge-location and scale-capacity are DIFFERENT axes.** *Edge* (the gap) lives in **inefficient corners**
  (thin, intermittent — line lag, settlement quirks); *capacity* (deployable size before you walk the book
  past the edge) lives in **depth**, and the efficient deep books (tennis/UFC/ITF) are ~$0 cross-venue
  precisely *because* they're arbitraged. The scalable money is their **intersection** — a deep book
  *transiently* dislocated: the **one observed instance** of depth-and-edge co-occurring is **MLB new-venue
  line-lag** (`lad-pit`, n=1: c2 grew ~4.8k→16k on a fresh pre-game book over one 8h overnight window) — a
  **hypothesis to confirm across more games, not yet a class property** (the 06-10 MLB markets did *not*
  replicate it). So scaling = **breadth of depth-AND-edge events** sized to each book's depth, not bigger
  clips in one corner ([tasks/lessons.md](tasks/lessons.md) L16).
  **Magnitudes are PRELIMINARY**: the live monitor has now accumulated **5.3 days (2026-06-09→14, 197
  capturable ≥1¢/≥30s episodes)** — superseding the early "single ~8h morning window ×3-extrapolated" basis —
  but **one day (06-10) supplies ~52%**, so it stays *one-day-dominated*, not a settled distribution.
  **WHERE (5.3d):** Sports ~66% / Weather ~30% / Econ ~4% (econ under-sampled, release-gated); the deep-AND-edge
  corner is **MLB**, while the high tennis/ITF *count* is one-day + phantom-inflated ([L20]/[0012](decisions/0012-clip-allocation-edge-floor-and-phantom-filter.md)).
  **WHEN:** an afternoon/evening-ET concentration (14–16 + 20–21 ET sports game windows; weather afternoon
  high-lock; econ 8:30 AM print) — direction credible, magnitude one-day-dominated. **Most edges are sub-second**
  (median 1 s; only ~15% last ≥30 s) → mostly not raceable (matches the ~29%-die-<250 ms leg-fill finding). Two
  adversarial reviews
  ([independent-review](tasks/independent-review-2026-06-09.md), [reviewer-audit](tasks/reviewer-audit-2026-06-09.md))
  found the earlier "$/day" prose unsupported and a capital double-count (~6–7× — now fixed in `capital_sim.py`:
  corrected peak ≈ $13.6k / ~6%/day, still preliminary). Treat edge size as unproven until the live monitor +
  `scripts/capital_sim.py` accumulate. The bot decides what to trade.
  **LIVE-CORROBORATED 2026-06-15 (94 fires, [0025](decisions/0025-dynamic-fire-order.md), cont. 14):** pmus is the
  binding thin leg across **ALL** categories (fails ~9× Kalshi); SPORTS has the deepest *displayed* book (median
  `depth_c2` 57 vs weather 2) **but aborts MORE** (80% vs 60%) and the displayed depth **evaporates at fire** (aborted
  fires logged HIGHER `depth_c2` than locked) — so the deep Kalshi sports books don't translate to fillable
  cross-venue size. The bot now fires the **THINNER leg first** ([0025], superseding 0020's fixed pmus-first), and the
  scaling lever reframes from "which category" to **breadth + the maker study + transient depth windows, not depth
  per pair** (the MLB line-lag window stays n=1 — instrument it, don't bet it). [tasks/lessons.md](tasks/lessons.md) L37–L40.
  **MEASURED 2026-06-16 → the displayed book is PHANTOM ([0027](decisions/0027-depth-gate-refuted-pmus-book-phantom.md), cont.16):**
  a 178-fire depth-at-fire replay (`scripts/depth_at_fire.py`, after a 25.3 h run: 21 locks / 11.8% conversion, ~85%
  died on the pmus hedge non-fill) shows DISPLAYED `depth_c2`-at-fire does **NOT** predict the fill — per-category
  AUC ≤0.57 (sports 0.42, weather 0.57), stratified-perm p=0.64, best depth-gate lift ≤3.3 pts, and conversion is
  **monotone-DECREASING** in displayed depth (11.9% → 0% @≥5000; MLB, the deepest book, converts WORST). A Monte-Carlo
  power check (89-100% vs a business-relevant gate) makes "p=0.64" **evidence of absence, not under-power**, so the
  **depth-gate / breadth / bigger-clip scale levers are REFUTED** — the cross-venue taker arb is capacity-capped by
  phantom pmus liquidity, NOT by the cap or the floor. The locks that DO complete are profitable (net edge +2.49¢
  mean) → a *capacity* problem, not a losing strategy. Bot stays a 1-contract rig; one gap pre-registered before the
  FINAL "uncapturable" verdict — log the **pmus-side depth ladder** at fire (`depth_c2` is paired-min; a pmus-only
  signal is untested). `stats-ml-logic-reviewer` reproduced the result SOUND/0-CRITICAL.
- **Hardened post-review (three passes):** per-order fee + crossed-book + entry-guard fixes (`bot/ledger.py`,
  `bot/monitor.py`) plus per-transition depth + staleness instrumentation. `age` is a coarse staleness hint
  (a resting-but-tradeable quote and a wedged stream both accrue large `age`); **depth** does the real
  fillability work. Second audit (2026-06-09) added: WS reconnect (no more silent half-dead collector),
  invariant-#2 guards in the live matcher (exact-date game binding, bucket boundary-equality, orientation
  price-guard), and the econ-legality correction. **Third full review (2026-06-10, [0013](decisions/0013-econ-grid-step-twin-and-measurement-integrity.md))
  fixed measurement integrity end-to-end:** debounced CLOSEs now stamped at **detection** time (the old
  flush-time stamps inflated every duration ~1.0–1.5s — corrected shadow-fill leg-fail @1s: 39%→**55.5%**, and
  the sub-second regime was structurally unmeasurable); `ws_reconnect` markers per venue (reconnect-rebuild
  phantoms now censorable, like restarts/resyncs); supervised heartbeat + **degraded-discovery prune skip** (an
  API outage can't masquerade as mass settlement); **single-subscription Kalshi invariant** (seq gap / new
  tickers cycle the connection — a second subscribe's semantics were never probed); doubleheader/duplicate-ticker
  binding guards; maker-fee rounding per the venue audit; `scan_all` now imports the bot's matchers + marginal
  fees (its private copies had drifted, incl. an L15 violation). **Follow-ups closed 2026-06-10 (same day):**
  (a) multi-subscription semantics PROBE-VERIFIED (`probe_kalshi_ws.py --multisub`: one sid/channel, control
  acks consume seq slots, add/delete are no-gap) → the invariant is **retired**: the monitor now does in-place
  `update_subscription` adds with snapshot-confirm + cycle fallback and delete-on-prune (offline integration
  test `scripts/test_monitor_nogap.py`; ends the censored ~650-ticker rebuild every cycle-on-add caused);
  (b) **fees re-pinned from primary sources** ([research/fee-pin-2026-06-10.md](research/fee-pin-2026-06-10.md)):
  all 4 coefficients confirmed; real finding — Kalshi **maker fees don't exist on 12/23 tracked series (all
  weather)** + pmus rebates makers −0.0125, so a weather maker-maker round-trip is fee-*negative* (strengthens
  the queued maker study); (c) the **allocation rule is PRE-REGISTERED** ([0014](decisions/0014-preregistered-allocation-rule.md),
  [research/allocation-prereg-2026-06-10.md](research/allocation-prereg-2026-06-10.md)): τ=2¢ + category caps
  20/10/5% frozen with a confirmatory protocol BEFORE the multi-week data exists ([L19] discipline).
  Open items in [tasks/todo.md](tasks/todo.md).
- **Execution feasibility — read-only tests run (2026-06-09, [research/execution-feasibility-2026-06-09.md](research/execution-feasibility-2026-06-09.md)).**
  **Latency MEASURED** (~86–261 ms RTT, network-bound → compute language is noise; Rust deferred — see
  [research/latency-playbook.md](research/latency-playbook.md)). **Leg-fill is the gating risk**: shadow-fill shows
  hit-rate collapses with latency — **corrected per [0013](decisions/0013-econ-grid-step-twin-and-measurement-integrity.md): 55.5% naked at 1 s, 62.7% at 2 s, and ~27% of
  capturable ≥1¢ edges die ~instantly** (the published 39%/67% carried the debouncer's flush-stamp lag).
  **First post-0013 sub-second read (16 h, 2026-06-11, [probe brief](research/probe-program-2026-06-11.md) §2):
  naked-leg 17.0% @100 ms / 23.3% @150 ms / 29.4% @250 ms** (n=575 capturable ≥1¢ episodes) — at the measured
  RTT, naked taker execution is **not rejected** (breakeven naked-unwind ≈4–6¢ vs ~1–3¢ plausible cost); ~29%
  of edges die <250 ms (never raceable, simply forgone). Preliminary: one sports-heavy day. **Settlement
  identity empirically CONFIRMED for weather (2026-06-11): 360/360 settled co-listed buckets graded identically**
  (three-way vs the independent NWS CLI, incl. one real revision day; 0 divergence). The prior "pmus interim
  outcomes unreliable — one verified wrong" claim is **retracted: it was our own parse bug** — pmus `outcomes[]`/
  `outcomePrices[]` are **not index-aligned**; `marketSides` is the authoritative winner encoding ([L23]; under
  the fix, sports interim reads are 56/56 == Kalshi, though pre-`endDate` sports reads stay interim-by-policy).
  **Econ source-identity empirically reconciled on PAST recurring releases (2026-06-11): CPI Apr/May
  print-identity + FOMC Apr categorical = 5 rows, 0 diverge — both venues settle off the identical
  government number (`settle_recon.py --econ-only`, [L24]); the U-3/NFP cumulative-twin instrument (the
  live arb mechanism) settles first 07-02.** Sports finalized recon pending (endDates ~06-23/25). **None of this
  needs capital — it needs the next gated deploy + weeks of data.** Reinforces: this is a *measurement rig*,
  not yet a go.
- **Capital velocity — MEASURED (2026-06-09, `scripts/exit_liquidity.py` + `capital_velocity.py`).** Velocity,
  not edge, separates the categories. pmus **freezes the order book at resolution** (10/10 resolved markets had
  empty books at `closed=true`) → **no early-exit** → capital locked to the far-future `endDate` (~15d). Weather
  is the OPPOSITE: it closes **late** (`endDate` 1 AM local — corrected 2026-06-11, was "~2 AM ET" — after the ~6 PM high-lock), so it HAS an ~8h evening
  exit window — measured: the winning bucket bids ~0.98-0.99 with thousands of contracts of depth. So **weather
  is doubly capital-efficient** (fast ~1.2d natural settlement + a liquid early-exit), while **sports (~15d, book
  frozen) and econ (weeks-mo, outcome known only at the far release) are capital-locked** — the sports early-exit
  "rescue" is refuted. So weather = clean+fast+thin; sports = deep but capital-slow + void tail; econ = cleanest
  but slowest. **No category wins {clean settlement, fast capital, real depth}; weather comes closest on capital.**
  **CORRECTION 2026-06-11 (owner, direct account observation):** the "sports/econ capital LOCKED ~15d to
  endDate" claim was an **inference, not a measurement** — it conflated *can't-sell-early* (the book IS
  frozen at resolution, measured) with *cash-locked* (assumed). The owner reports pmus credits the payout
  to **cash at GRADE**, not at the +14d endDate (which is just the administrative expiration window for
  contingencies). pmus exposes no balance endpoint to verify independently, and the one observed position
  was a loss (clears at grade regardless), so the win-case isn't fully confirmed — but the "15-day capital
  lock" should **not** be asserted: sports/econ appear to settle to cash ~at grade (fast), like weather.
  This makes **sports a prime category once reconciled (June 23): deep books AND fast capital.** The
  separable, smaller cost is the *pre-game* freeze (mitigated by the new game-proximity entry gate,
  `bot-rs` `SPORTS_MAX_DAYS_TO_GAME`) + the void/postpone tail (the unwind rule).
- **Clip-stage allocation — tested OOS + two phantoms fixed (2026-06-10, [0012](decisions/0012-clip-allocation-edge-floor-and-phantom-filter.md) corrected by [0013](decisions/0013-econ-grid-step-twin-and-measurement-integrity.md), [research/allocation-policy-2026-06-10.md](research/allocation-policy-2026-06-10.md)).**
  `account_sim`'s FIFO-by-arrival is the worst rule when the bankroll binds (one deep clip eats the whole $500 →
  funds 1/217). The owner's "wait 1 s + sort the batch" captures only **~4%** of the FIFO→optimal gap (competing
  arbs span *days*, not seconds) and *adds* a ~55%-naked-leg-at-1s latency tax. The lever is a **global 2¢ edge
  floor** (skip thin arbs) **+ a per-pair cap sized to fully deploy without over-concentrating** (~10–20%):
  **~+9% to +141% over FIFO out-of-sample** (corrected — the published +64–269% contained an econ
  settlement-phantom worth ~20% of design PnL; 10 diversified weather+sports pairs). **Clip-cap ALONE is
  risk-control, not PnL** (−14% OOS corrected — it just diversifies into thin arbs). Ordering stays
  **second-order to capital velocity** ($500 funds ~10 then locks). **Magnitudes PRELIMINARY** (<1 d / one
  cluster / paper-gross — method demo, not validated). The eye-popping in-sample numbers are oracle artifacts
  ([L19]); a **book-init phantom** (37.7¢ ITF-tennis, `censored=restart`) was 75% of the old in-sample headline
  until `capturable()` dropped restart-censored ([L20]), and the **econ off-by-one phantom** (12.2¢ "U-3 edge" =
  market-priced P(print==T), [L21]) sat in the OOS tables until 0013 quarantined pre-remap econ records.
- **Probe program — all 10 ranked next-steps probed in one read-only pass (2026-06-11,
  [research/probe-program-2026-06-11.md](research/probe-program-2026-06-11.md);** first ~16 h of post-0013
  data + live API reads; per-item notes in `tasks/_agent_bus/20260611-probes/`). Beyond the leg-fill,
  settlement-recon and MLB headlines folded into the bullets above: **(a) the maker study narrows to ONE
  config** — rest on **Kalshi** (weather, $0 maker fee) + taker-hedge on pmus, bounded **+0.14–0.44¢/attempt**;
  rest-on-pmus is structurally toxic (15–16¢ hedge slippage) and full maker-maker carries 32% one-leg-naked —
  weather **trade-print + ladder logging is built** (spec'd from a measured 2-orders-of-magnitude sampling gap:
  ~23.8k Kalshi weather prints/day vs 220 visible crossings). **MEASURED 2026-06-15 → NOT_VIABLE ([0026](decisions/0026-maker-mode-not-viable-prereg-threshold.md), cont.15):**
  `scripts/p_hedge_measure.py` on those WAVE-2 logs gives clock-corrected `p_hedge` 36–46% < ~53% breakeven, EV
  negative — the +0.14–0.44¢ bound priced only the hedge SLIPPAGE, never the hedge-MISS, and the maker
  structurally INVERTS [0025]'s fire-the-thinner-leg-first safety; shelved (re-measurable free, forward
  threshold pre-registered; a stats-review caught a clock-skew look-ahead that had flipped the sign, [L44]);
  **(b)** the adverse-selection **skip filter is null** overall, but a weather-only
  **leg-sequencing** signature (cheap-side-made opens 18% toxic vs dear-side-made 79%, z=4.6, small cells) is
  hypothesis-grade — pre-register before believing; **(c) early-exit = hold-all** (exit-all ≈ −1.5¢/pair;
  boundary-day maker-exit breakeven needs P(flip)>2%, measured 0/14 station-days — and the diverging leg is
  always the Kalshi leg); **(d) recycle-time reconsideration = measured $0** (structurally starved — skipped
  arbs die in ~1 s median; deprioritized); city-date cluster exposure ≤20% today (opt-in knob built,
  exploratory); `/series/fee_changes` tripwire implemented (laptop half active now, droplet half rides the
  redeploy). **No-gap build verified ready** (18/18 + integration green; the old build censors ~310
  episodes/day, ~152/day avoidable) — **REDEPLOYED 2026-06-11 02:55 UTC (owner-greenlit per 0006)**:
  droplet on build `f8f261298097` (sha byte-verified), no-gap adds + trade/ladder logging + fee tripwire
  all live; flagged fixes landed same session (pull-data late-append overwrite hazard,
  `weather_spread_snapshot` marketSides read, daily ~12:30Z settle-recon step in the pull).

## Managerial docs index

Every doc that governs *how this project is worked on*. Keep this list current — adding a managerial
doc without linking it here leaves the index incomplete.

| Doc | Purpose |
|---|---|
| [README.md](README.md) | Human-facing project overview, thesis, how to run |
| [tasks/todo.md](tasks/todo.md) | Live plan + checklist; the current work item lives here |
| [tasks/lessons.md](tasks/lessons.md) | Mistakes made → rules to not repeat them (self-improvement loop) |
| [tasks/independent-review-2026-06-09.md](tasks/independent-review-2026-06-09.md) · [tasks/reviewer-audit-2026-06-09.md](tasks/reviewer-audit-2026-06-09.md) | The two adversarial reviews + their fix logs |
| [docs/sessions.md](docs/sessions.md) | Session log / process changelog |
| [decisions/README.md](decisions/README.md) | Decision-log convention + index of entries |
| [decisions/template.md](decisions/template.md) | Skeleton for a new decision entry |
| [research/README.md](research/README.md) | Index of the research briefs + the latency playbook (the evidence base) |
| [scripts/README.md](scripts/README.md) | Index of probe/scan scripts + `_data/` outputs |
| [bot/README.md](bot/README.md) | Accounting core (`ledger.py`) + the dual-stream monitor (`monitor.py`, `kalshi_book.py`, `colisted_map.py`) |
| [bot-rs/README.md](bot-rs/README.md) | **LIVE trading bot (Rust, [0015](decisions/0015-owner-override-live-trading-phase.md))** — safe-by-default model, build/run, staged rollout, stage-1 spine vs stage-2 venue I/O |
| [docs/architecture.md](docs/architecture.md) | Data-flow diagram (venues → discovery → monitor → transitions → analysis/bot); marks where the clip / position-size lever sits |
| [deploy/README.md](deploy/README.md) | Droplet deploy artifacts (systemd unit, provision/deploy/pull-logs scripts) + droplet sizing — GATED ([0006](decisions/0006-deploy-on-digitalocean-consult-first.md)) |
| [deploy/bot-rs/README.md](deploy/bot-rs/README.md) | **LIVE-bot droplet deploy** (WSL Linux build + systemd unit + out-of-band secrets + log pull) — owner override of 0007 ([0028](decisions/0028-live-bot-on-droplet-override-0007.md)) |
| [tasks/scale-in-reentry-design.md](tasks/scale-in-reentry-design.md) | Design-of-record for the scale-in/re-entry money-path capability (multi-position-per-slug; exact per-position exposure release) — [0019](decisions/0019-scale-in-reentry-multi-position.md) |

## Repo layout

```
cross-arb/
├─ CLAUDE.md            ← you are here (index + working agreement)
├─ README.md           ← human overview
├─ miami-temp-arb.html ← the seed idea (the ORIGINAL fat-edge artifact; now superseded — see findings)
├─ miami-screenshot.png
├─ docs/sessions.md    ← session log
├─ decisions/          ← decision log (README convention + numbered entries)
├─ tasks/              ← todo.md (plan) + lessons.md (corrections)
├─ research/           ← 9 settlement / legality / venue / edge briefs
├─ scripts/            ← Python probes + scanners (read-only); _data/ outputs are gitignored
├─ bot/                ← ledger.py (PnL core) + monitor.py (dual-stream logger) + kalshi_book.py + colisted_map.py
├─ bot-rs/             ← LIVE trading bot (Rust, 0015) — safe-by-default (dry-run/demo/caps/kill-switch); stage-1 spine
├─ deploy/             ← droplet deploy: systemd unit + provision/deploy/pull-logs scripts + runbook (GATED, 0006)
└─ requirements.txt    ← pinned runtime deps for the monitor (websockets, cryptography)
```

## Working agreement (project-specific)

- **Read-only until told otherwise** — **REVERSED 2026-06-11 by the owner ([0015](decisions/0015-owner-override-live-trading-phase.md)):**
  the live bot `bot-rs/` is authorized. The least-privilege spirit holds (read-write key stays
  external, never in repo/history), the bot is **dry-run by default**, and **live submission runs in the
  owner's environment** (this sandbox blocks it). The Python research/monitor side stays read-only.
- **Deployment is gated.** Live loggers (`bot/monitor.py`) + bot run on a DigitalOcean droplet —
  **consult the owner before any deploy** ([decisions/0006](decisions/0006-deploy-on-digitalocean-consult-first.md)).
  Dev stays local + read-only; `bot/monitor.py --live` is gated. Never push secrets to a remote host.
- **Two load-bearing invariants** for any "arb" claim:
  1. **Settlement identity** — both venues must grade off the *same* named deterministic number, else
     a "locked" pair can lose *both* legs. See [decisions/0001](decisions/0001-us-legal-only-venue-pair.md).
  2. **No false positives** — a join must be team/event-identity-correct, not text-similar. The
     first sports matcher produced a fake edge from ambiguous city names; see [tasks/lessons.md](tasks/lessons.md).
- **Comprehensive coverage, no pruning** — keep every co-listed market in scope and compute the same
  metrics for all; "not enough edge" is the live bot's call, not the scanner's.
  See [decisions/0002](decisions/0002-comprehensive-coverage-no-pruning.md).
- **Edge persistence is event-driven, not fixed-cadence polled.**
  See [decisions/0003](decisions/0003-event-driven-persistence.md).
- **Secrets:** never commit `scripts/.env`, `*.pem`, `*.key`, or `scripts/_data/` (all gitignored).
  Market data is public; only order placement needs keys.
- **Version control:** private GitHub repo `kunpark04/cross-arb` (`origin/main`). Commit in focused units
  when the user asks; verify nothing sensitive is staged before any push (gitignore covers `.env`/`*.pem`/`_data/`).
- **Scripts are throwaway-grade probes**, named for what they answer. Raw pulls land in
  `scripts/_data/` (gitignored). Conclusions get promoted into a `research/` brief.

## When you do substantive work here

- Capture load-bearing decisions as a numbered entry under `decisions/` (convention in its README).
- Append a `docs/sessions.md` entry for any session that ships code, makes a decision, or changes scope.
- After any user correction, add a `tasks/lessons.md` rule. (The `/update-managerial-docs` skill
  automates this refresh at session end.)
- Keep this index in sync when you add a managerial doc.
