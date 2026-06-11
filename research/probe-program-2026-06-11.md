# Probe program — 2026-06-11 (10 ranked next-steps, probed in parallel)

**What this is:** the owner directed "probe these" on the 10-item ranked next-step list (2026-06-10
session close). All 10 were probed read-only in one parallel pass. Data basis: fresh droplet pull at
2026-06-11 01:00 UTC — **~31,297 post-0013-epoch transition records (~15.94 h**, detection-time ms
stamps + `px` + `depth`; t ≥ 1781082189) plus the lag-corrected pre-epoch archive (~1.8 d total) and
live public-API reads. Per-item working notes: `tasks/_agent_bus/20260611-probes/`.

**Everything here is preliminary at ≤1.8 days of data** — directions are solid, magnitudes are not.
Each section names its deflator and its upgrade trigger.

## Executive synthesis

The "items 1–4 are one program" framing survives contact with the data, with three updates:

1. **Taker execution is NOT dead at measured latency.** The 55.5%-naked-@1s number was the wrong
   regime: at the measured 86–261 ms RTT, naked-leg risk is **17–29%**, survivors keep ~1.9¢ median,
   and breakeven naked-unwind cost (~4–6¢) is above plausible actual unwind cost (~1–3¢). Sub-second
   data confirms ~29% of edges die <250 ms (you simply never race those).
2. **The maker study collapses to ONE config:** rest on **Kalshi** (weather, $0 maker fee) + taker-hedge
   on pmus = bounded **+0.14 to +0.44¢/attempt**. Rest-on-pmus is structurally toxic (15–16¢ hedge
   slippage — the rebate is irrelevant) and full maker-maker carries a 32% one-leg-naked rate with
   ~2.2 h unhedged windows. The trade-print/ladder logging (spec'd + implemented this session) turns
   the bounds into measurements.
3. **The catastrophic tails closed faster than expected:** settlement identity for weather is now
   **empirically confirmed (n=360, 0 divergence)** — and the prior "pmus settled data can be wrong"
   scare was **our own parse bug** (see §4, lesson L23). The MLB void tail is real but the actionable
   window is **minutes, not 2 days** (Kalshi closed voided markets 47–90 min after scheduled start)
   — and a 5-min schedule poll detects postponements comfortably inside it.

Cheap option-buying items resolved as expected: early-exit = **hold-all** (reject), recycle arm =
**measured $0** (deprioritize), cluster cap = risk-control knob built (not binding today), fee
tripwire + ladder logging = implemented, riding the next gated redeploy with the no-gap build.

## Verdicts at a glance

| # | Item | Verdict (n basis) |
|---|---|---|
| 1 | Maker weather study | **Plausibly +EV in exactly one config** — rest-Kalshi + taker-hedge-pmus, +0.14–0.44¢/attempt bounded; rest-pmus toxic. Measurement gap → ladder/trades logging (built, awaits redeploy) |
| 2 | Sub-second leg-fill | **No red light for taker at real RTT**: naked 17–23% @100–150 ms; ~29% of edges die <250 ms (never raceable); 1s/2s claims soften ~8 pp (n=575 episodes) |
| 3 | No-gap redeploy | **Ready** (18/18 + integration green; droplet sha verified). Old build censors ~310 episodes/day; no-gap eliminates ~152/day (49%). **Awaiting 0006 greenlight** |
| 4 | Settlement recon (weather) | **CONFIRMED n=360/360, 0 divergence** (3-way vs NWS CLI; incl. the one real revision day). Prior "pmus interim WRONG" = our parse bug (fixed; L23) |
| 5 | Adverse-selection gate | **Skip-filter: not worth building** (overall null, z=−0.68). **Weather leg-sequencing signal is hypothesis-grade** (18% vs 79% toxic, z=4.58, small cells) |
| 6 | Weather early-exit | **REJECT exit-all** (−1.5¢/pair); boundary-day maker-exit breakeven needs P(flip)>2% (unmeasured, 0/14 station-days). **Default hold-all**; better idea: hedge flip-destination bucket |
| 7 | MLB unwind rule | **Implementable + EV +12–13¢/contract on trigger** — but must act in MINUTES (2-day comfort refuted); 5-min statsapi poll suffices (5/5 postponements detected w/ reschedule date) |
| 8 | Recycle-time arm | **$0.00 in every cell** (n=1 recycle event; 0 still-open skipped candidates; skipped pool dies in ~1 s median). Deprioritize |
| 9 | City-date cluster cap | Max same-(city,date) exposure **20%** = one pair at its own cap; 20/30% caps bind nothing; 10% costs −13.6% PnL. **Risk-control knob built (opt-in, exploratory)** |
| 10 | fee_changes tripwire | **Implemented** (wave-2): monitor heartbeat + laptop healthcheck poll + alert; rides the next redeploy (laptop side active immediately) |

---

## 1. Maker-side weather execution study

- **Fees (exact, from pinned primary coefficients, `scripts/maker_feasibility.py --selftest`):**
  taker-taker 0.69–3.00¢/contract by price → weather maker-maker **−0.06 to −0.31¢ (net rebate)**.
  One Kalshi maker leg alone cuts the fee wall ~58%. Contrast series with Kalshi maker fees
  (MLB-type) stay fee-positive — **the maker edge is weather-only**, as the fee re-pin predicted.
- **Fill bounds (n=4,934 px observations / 46 weather markets / 15.4 observed market-days):**
  220 definite level-crossings (14.3/market-day; 54% of markets ≥1). Resting at OPEN: visible-fill
  lower bounds 40–77% (Kalshi-rest) vs 29–55% (pmus-rest); visible time-to-fill median 14 min–2.8 h
  (upper bounds — transition-sampled).
- **Adverse selection:** Kalshi-rest hedge slippage median ~2¢ → **net per filled attempt median
  +0.8¢** (+2.2¢ with a 1 h cancel cap → per-attempt +0.14 to +0.44¢). pmus-rest slippage median
  **15–16¢** — structural: the sticky wide pmus quote fills only on bucket-death moves Kalshi has
  already repriced. **Full maker-maker: 32% one-leg-naked, median unhedged window 2.2 h** — not
  priceable from this data.
- **Sampling bound severity, measured:** Kalshi printed **~23,800 weather trades/24 h** (public REST,
  per-print aggressor side) vs our 220 visible crossings — the transition-sampled view misses ~2
  orders of magnitude of fill events. Hence the spec.
- **Spec → implemented** (`tasks/_agent_bus/20260611-probes/ladder-logging-spec.md`, wave-2 code):
  (A) `trades-<date>.jsonl` Kalshi REST cursor-poll (~+3.6 MB/day) — true fill time/rate/size +
  aggressor; (B) `ladders-<date>.jsonl` top-5 both venues on weather transitions (+2.2 MB/day) +
  delta-suppressed 300 s heartbeat snapshots (≤+3.7 MB/day) — queue-ahead + unbiased denominators.
  All additive; existing loaders untouched; `pull-data.ps1` extended (else new files accumulate on
  the droplet). Kalshi WS `trade` channel deliberately NOT wired (2-channel sid/seq semantics
  unprobed — same risk class the multisub probe existed to kill).
- **Deflators:** activity-biased sampling (books observed only at transitions), no queue model, 1.8 d.
  **Upgrade:** trade prints + ladders accumulating after the next redeploy → re-run as a measurement.

## 2. Sub-second leg-fill (taker viability)

Post-epoch only (15.94 h, n=575 capturable ≥1¢ episodes: 476 sports / 99 weather / 0 econ —
no release in window). `scripts/shadow_fill.py` extended: sub-second grid + `--post-epoch`.

- **Naked-leg % by entry latency:** 9.4 @50 ms · 17.0 @100 ms · 23.3 @150 ms · 29.4 @250 ms ·
  47.3 @1 s · 55.0 @2 s. Survivors keep ~1.9¢ median realized edge.
- **Breakeven naked-unwind cost ≈ 6.2¢ @150 ms / 4.3¢ @261 ms** vs ~1–3¢ plausible actual unwind →
  taker execution at the measured RTT is **not** rejected by this data.
- **Durations:** 29.2% of capturable ≥1¢ episodes die <250 ms (**confirms** the "~27% die instantly"
  claim — those are simply never raceable); 47.3% <1 s / 55.0% <2 s (**softens** the corrected
  pre-epoch 55.5%@1s / 62.7%@2s by ~8 pp).
- **Depth lens (L18):** restricting to c2≥100 buys only ~3 pp survival — depth does not rescue the
  race; the dur≥30s+deep subset (n=51) survives 100% but that's outcome-conditioned (selection, not
  a strategy).
- **Deflators:** shadow proxy is an upper bound (transition-grain, no queue/partial fills); split-half
  regime swing 16 pp @150 ms; single sports-heavy weekday. **Upgrade trigger:** ~1 week post-0013
  data + ≥1 econ release; and measure actual naked-unwind cost (the real go/no-go term).

## 3. No-gap build — ready; cost of every day NOT redeployed

- **Gates:** `selftest_all.py` **18/18 green**; `test_monitor_nogap.py` green standalone (no-gap add
  + snapshot-confirm + delete-on-prune on one connection; fallback-cycle path with `ws_reconnect`
  marker). Droplet check (read-only ssh): running sha = repo HEAD = `session_start.build`; local
  ships +80/−18 lines in `bot/monitor.py` (only behavioral delta) — plus the wave-2 additions.
- **Old-build cost, recounted over 15.94 h:** 32 Kalshi clean reconnects, **32/32 on a 309.1 s
  integer grid** (300 s heartbeat + ~9 s discovery) = cycle-on-add ≈ **48/day** (prior "4 in 1 h" was
  a burst; steady state ~2/h). Episode-level: 206/4,964 post-epoch episodes restart-censored (4.1%,
  ~310/day); attribution: 76% k-clean cycles. **Counterfactual with no-gap: −101 censored (−49%),
  +100 measured episodes/day; "mattering" censored (≥1¢ or c2≥100) 87→39; capturable-grade 575→592.**
  Not eliminated: pmus reconnects, real restarts, true seq gaps (0 in window).
- **Owner ask (0006):** one `systemctl restart` of downtime; the wave-2 ladder/trades/tripwire code
  rides the same redeploy. Readiness note: `tasks/_agent_bus/20260611-probes/probe3-nogap-redeploy.md`.

## 4. Settlement reconciliation, weather-first — CONFIRMED + a critical parse correction

- **Invariant #1 (weather): empirically confirmed.** n=**360** settled co-listed buckets (60
  city-days, 2026-05-29→06-09, all 5 cities): **360/360 same side**, three-way anchored — Kalshi vs
  independent NWS CLI 330/330, pmus vs CLI 330/330, Kalshi's recorded `expiration_value` vs the
  monitor's independently-logged CLI 55/55. The one real CLI revision day in window (MDW 06-09,
  87→88 overnight) settled to 88 on **both** venues. 47 buckets pending (06-10/11). 0 divergent.
- **The critical catch — a documented-convention bug, now fixed (L23):** pmus `outcomes[]` and
  `outcomePrices[]` are **not index-aligned**; prices follow **`marketSides`** order (each side
  self-labels + carries settled `price` 1/0). The old label-pairing read produced 22 phantom weather
  "divergences" + 44/70 internally-impossible multi-YES days, and **explains away** the prior
  findings "pmus interim settled data verified WRONG (ATP case)" and "MIA 06-08 4-YES day". Bulk
  validation: marketSides-paired winner == Kalshi **286/286**; label-pairing wrong on exactly the
  141 No-first objects (and coincidentally right on Yes-first ones — why 06-09 spot-checks "verified"
  the wrong convention; L17-class). `settle_recon.pm_winner()` is now marketSides-primary with
  regression selftests. Under the fix, sports interim reads **56/56 == Kalshi**.
- **What survives of the old caution:** sports `closed` ≠ finalized is still structurally true (pmus
  sports `endDate` ≈ D+14; treat pre-endDate reads as interim) — but the "known wrongness" evidence
  is gone. pmus exposes **no settlement timestamp**; weather finalization is bracketed: final +
  correct by **T+13.5 h** post-Kalshi-settlement (earliest probed read; 0–13.5 h unobserved), stable
  out to T+11.6 d. Kalshi settles weather ~8:02 AM ET D+1; pmus weather close = **1 AM local** D+1
  (corrects the "~2 AM ET" in CLAUDE.md).
- **Unlock calendar:** sports finalized recon ~**06-23/25** (post-endDate); econ: FOMC settles
  **06-17** → run 06-18 (cleanest first econ datapoint); U-3/NFP print **07-02** → run 07-03.

## 5. Adverse-selection direction gate

- **Close toxicity (n=4,746 closes):** 54% cheap-side-led / 46% dear-side-receded; weather 65/35
  (n=862), sports 52/48 (n=3,880), econ n=4.
- **At-open classification is computable** from logged `px` for 78% of opens (missing: a px snapshot
  at subscribe for first-sightings).
- **As a skip filter: null.** At-open class predicts neither toxicity (48% vs 51%, z=−0.68,
  n=217/217) nor duration (1.44 vs 1.84 s) nor survival (sign-flips across lenses). **Do not build.**
- **Exception worth chasing:** weather-only, cheap-side-made opens run **18% toxic (n=33)** vs
  dear-side-made **79% (n=39 obs window, n=24 cell)**, z=4.58, robust across 24 markets / 4–5
  cities — a **leg-sequencing** hypothesis (take the cheap leg first when the dear side made the
  edge), not a skip rule. Subgroup-after-a-null ([L19]) → needs pre-registered confirmation at
  ~1 week of data (cells n≈150–250).

## 6. Weather early-exit option — quantified: hold

`scripts/early_exit_ev.py` (+ selftest). Structural finding first: **the diverging leg on a
boundary-revision day is always the Kalshi leg** (Kalshi waits for the final CLI; pmus locks at
8 AM), so "exposed" pairs must exit on the *worse* venue — measured evening bids K 0.965 vs pm 0.99
(n=10 px records) — while "favorable" pairs exit on pm and keep the flip-windfall for free.

- **EV (¢/contract-pair, exit−hold, capital-value=0):** safe-pair pm exit −1.05; exposed-boundary
  taker-exit on K −2.74 @P(flip)=1%; exposed-boundary **maker**-exit on K ($0 weather maker fee)
  −1.00 @1% / **0.00 @2%**; exit-all portfolio ≈ **−1.5**.
- **Breakeven P(flip):** taker 3.74% / maker **2.00%** (drops to 2.18%/0.45% only at the max
  capital-value bound ≈1.55¢ = 5.3%/day × 30% overnight share — preliminary upper bound).
- **Measured P(flip): 0 downward revisions in 14 station-days** (Jeffreys 95% ceiling 12.6% — cannot
  resolve vs 2% yet; ~3 more weeks of `cli.jsonl` crosses the breakeven). Boundary evenings ≈ 6% of
  pair-evenings (1/4 joinable station-days had a boundary print).
- **Verdict: default hold-all.** Revisit only the exposed-boundary maker-exit if P(flip) measures
  >2%. **Cheaper dominant alternative flagged:** buy the flip-destination Kalshi bucket at its
  ~1–2¢ ask (a hedge, not an exit — ask depth unmeasured); zero-cost improvement: on boundary days
  prefer the tail-favorable entry direction.

## 7. MLB unwind rule — implementable, but the window is MINUTES

- **The "2-day window" comfort is refuted:** Kalshi **closed voided markets 47–90 min after
  scheduled start** (n=3 live postponement cases) — the venue void determination is what bites, not
  the 2-day reschedule rule. An unwind rule must fire within ~minutes-to-an-hour of postponement.
- **Detection: yes, minutes.** MLB statsapi (public, keyless): `detailedState=Postponed`
  (codedGameState `D`, statusCode `DR`) with `rescheduleDate` attached immediately — 5/5
  postponements in a 30-day scan (5/415 games = 1.2%/game; matches 0010's 1.3% prior). No push/
  timestamp → latency = poll cadence; spec: **one batched `schedule?gamePks=` poll every 5 min**
  (escalate 1 min near start / on Delayed). L3 trap found: `officialDate` *moves* to the makeup date
  — measure the gap from the bound event date.
- **Unwind liquidity (pre-game): real.** 1¢ spreads both venues; bids 1.1k–82k at touch, ~99k–205k
  within 3¢ per leg (n=3 live pairs; archive median 1¢/1¢, n=1,652). In-play trailing side degrades
  badly (36¢ spread observed).
- **EV on trigger:** hold ≈ −18¢/contract expected (0010 conditional: 0.4 gap-prob × 50¢ loss −
  0.6 × edge kept) vs unwind ≈ −7–8¢ (spread+fees) → **unwind +12–13¢/contract conditional on
  postponement**. Caveat: makeup empirics ran 0/5 in the 3–14 d coin-flip gap (2/5 next-day, 3/5
  months-out) — 0010's gap-prob 0.4 may be ~2.5× pessimistic; even then unwind stays positive.
  Bonus: Kalshi "fair price" void = scalar settle ≈ last price (n=5 observed). Watch
  `aec-mlb-tb-nyy-2026-05-23` for the first pmus void print (endDate +14 d).
- Spec: `tasks/_agent_bus/20260611-probes/probe7-mlb-unwind.md` (trade-layer item; detector is
  buildable read-only any time).

## 8. Recycle-time reconsideration arm — measured $0, deprioritize

`scripts/recycle_arm_experiment.py` (EXPLORATORY, outside the 0014 freeze; imports the frozen
economics, modifies nothing). On 1.81 d: recycle events **n=0 (FIFO) / 1 (H1)**; at the lone
settlement, **0 previously-skipped candidates were still open** → free capture **$0.00**. Two
measured structural reasons it stays ≈0: (a) under H1 capital never binds (0 capital-skips; ~$120
cash floor), (b) the skipped pool dies fast (median lifetime 1 s, max 37 min, 0% ≥1 h) vs
hours-to-days skip→settle gaps. One-command recheck on the multi-week pull; do not build into the bot.

## 9. City-date correlated-exposure cap — low concentration today; knob built

Same script, H1 replay: max same-(city,date) bankroll share **20.0%** — a *single pair* at its own
20% weather category cap (true 2-position stack 17.4%, `sfohigh-06-10`; sports/game max 10%).
Cluster caps 20%/30% bind **0/13 fills** (ΔPnL $0); 10% binds 3 at **−13.6% PnL** → pure
risk-control, no PnL case (as expected, L19). Opt-in `--cluster-cap` lives in the exploratory script
only. **Data-quality flag fed to the prereg W5d checklist:** 8/13 H1 fills ($235/$430) carry the L20
flat-ladder fingerprint (incl. the max-cluster position); under `--drop-flat` both verdicts hold.

## 10. `/series/fee_changes` tripwire — implemented (wave-2)

Monitor heartbeat polls the (currently empty) endpoint and writes a loud record + health-beacon
field on any change; laptop-side `healthcheck.ps1` polls on its 30-min schedule and raises the
existing alert path (balloon + ALERT.txt) — active immediately, no redeploy needed for the laptop
half. Droplet half rides the next gated redeploy with #3 + #1's logging.

---

## Doc corrections landed this session (traceable to §4 + §7)

- `CLAUDE.md`: execution-feasibility bullet (settlement-identity status → weather empirically
  confirmed; "one verified wrong" retracted as parse bug), sports bullet (2-day → minutes framing),
  pmus weather close time (1 AM local).
- `research/settlement-verification.md` + `research/execution-feasibility-2026-06-09.md`: dated
  correction notes at the affected claims (originals preserved).
- `tasks/lessons.md`: **L23** (sibling-array alignment; validate conventions on diverging cases).
- `scripts/README.md`: 4 new probe scripts + 3 extended ones indexed; `weather_spread_snapshot.py`
  flagged (label-pairing bug, superseded — fix-or-retire before reuse).

## Owner asks — ALL RESOLVED same session (owner: "redeploy, fix anything flagged, commit and push")

1. ~~Redeploy greenlight (0006)~~ — **DONE 2026-06-11 02:55 UTC**: build `f8f261298097` sha-verified
   on the droplet (no-gap + ladder/trade logging + fee tripwire live); rollback ref HEAD `bfa9e2fccf15`.
2. ~~Commit checkpoint~~ — **DONE**: focused commits pushed to `origin/main`.
3. ~~0–13.5 h finality window~~ — **DONE**: `pull-data.ps1` now runs a daily `settle_recon` read at the
   scheduled ~12:30 Z pull (inside the window); appends to `settle_recon_daily.log`, raises ALERT.txt
   on any DIVERGE. Also fixed same session: the pull's recreate-after-delete overwrite hazard (late-append
   day kept raw beside its canonical `.gz`) and `weather_spread_snapshot.py`'s pre-L23 pmus read.

## Re-run calendar (data-gated triggers)

| When | What |
|---|---|
| post-redeploy +~1 wk | shadow_fill sub-second re-read (econ included); weather sequencing-gate confirmation cells (n≈150–250); maker study as *measurement* (trades+ladders) |
| 2026-06-17 → 06-18 | FOMC settles → first econ settlement recon (`settle_recon.py`) |
| ~2026-06-23/25 | sports pmus endDates pass → first finalized sports recon |
| 2026-07-02 → 07-03 | U-3/NFP print → econ recon batch 2 |
| ~+3 wks of `cli.jsonl` | P(downward flip) resolves vs the 2% maker-exit breakeven (#6) |
| multi-week pull | recycle-arm one-command recheck (#8); cluster-cap re-measure (#9); prereg H1/H2 confirmatory run per 0014 gates |
