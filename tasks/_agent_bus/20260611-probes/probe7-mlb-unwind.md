# Probe 7 — MLB unwind rule + postponement detection (2026-06-10/11)

**Probe:** `scripts/probe_mlb_postpone.py` (new; read-only, public keyless APIs). Raw evidence in
`scripts/_data/`: `mlb_postpone_samples.json`, `mlb_feed_823543.json`, `mlb_kxmlbgame_rules.json`,
`mlb_postmortem.json` (+ ad-hoc `mlb_postponed_pm_markets.json`, `mlb_postponed_kalshi*.json`),
`mlb_unwind_books.json` (in-play), `mlb_pregame_books_0611.json` (pre-game). All numbers below were
produced by code run 2026-06-10 ~21:00–21:45 ET; n stated per cell.

## Verdict

- **Detection: FEASIBLE, minutes-scale** — every postponement surfaces in the public statsapi schedule
  with the makeup date attached (5/5 live examples); a 5-min batched poll detects ≤ ~5 min after the API
  flips. **But the comfortable "2-day window" is REFUTED**: Kalshi closed trading **+47 / +82 / +90 min
  after the original scheduled start** (n=3 fair-price voids) — the rule must act immediately, not within
  2 days.
- **Unwind liquidity: REAL pre-game** — 1¢ spreads both venues, bid-side depth 1.1k–82k at touch and
  ~99k–205k contracts within 3¢ per leg (n=3 pairs × 3 books, live; archive med spreads 1¢, n=1652
  px-records). Degraded in-play on the trailing side (1 @ touch, 36¢ Kalshi spread observed once).
- **EV on trigger: POSITIVE** — unwind cost ≈ g + ~5¢ ≈ **7–8¢/contract** vs conditional hold-loss
  **20¢/contract** (0010: p_gap 0.4 × loss 0.5) → **net ≈ +12–13¢/contract**, ~2.5× margin to breakeven.

## 1. How a postponement surfaces (statsapi, live-verified)

`GET statsapi.mlb.com/api/v1/schedule?sportId=1&date=YYYY-MM-DD` (public, keyless). 30-day scan
(2026-05-11..06-10): **415 games, 5 postponements** (1.2%/game — matches 0010's 1.3% prior), all
`detailedState='Postponed'`, `codedGameState='D'`, `statusCode='DR'`, `reason='Rain'`.

| Game (original) | makeup (`rescheduleDate`) | gap | trigger |
|---|---|---|---|
| STL@CIN 05-22 | 05-23 | 1d | WATCH |
| TB@NYY 05-23 | 09-22 | **122d** | UNWIND |
| DET@BAL 05-23 | 05-24 | 1d | WATCH |
| STL@CIN 05-24 | 08-17 | **85d** | UNWIND |
| BOS@NYY 06-06 | 08-29 | **84d** | UNWIND |

Field semantics (the L3 trap, baked into `unwind_trigger`'s selftest):

- `rescheduleDate` appears **immediately with the status flip** (5/5) — no "makeup unknown" limbo observed.
- **`officialDate` MOVES to the makeup date; `gameDate` keeps the original datetime.** The gap must be
  measured from the pair's bound event date (pm slug date = Kalshi ticker date); diffing against
  `officialDate` computes makeup-minus-makeup = 0d and misses every UNWIND (my first scan version did).
- The **same gamePk** appears on original + makeup dates (`rescheduleDate` forward / `rescheduledFrom`
  back pointer; verified `schedule?gamePks=824518` → 2 entries) → **one batched
  `schedule?sportId=1&gamePks=<all tracked>` request polls every tracked game**.
- **No status-change timestamp exists anywhere**: schedule entries carry none; the live feed
  (`/api/v1.1/game/{pk}/feed/live`) of a postponed game has *moved on* to the makeup
  (`/timestamps` = [], even `originalDate` = makeup) — so **detection latency = our polling cadence**
  (+ MLB's own announce→API lag, unmeasurable retroactively; MLB's apps run on statsapi, assume ~minutes).
  No push/WS endpoint exists; `feed/live/diffPatch` shrinks payloads, not latency.

## 2. Detector spec

- Poll `schedule?sportId=1&gamePks={pks of all tracked MLB pairs}` every **N = 5 min** (one request;
  288 req/day — trivial). Escalate to **1 min** when any tracked game shows `detailedState` containing
  `Delayed`/`Suspended` or is inside scheduled-start ± 3h. Justification: the binding deadline is
  Kalshi's observed close at start+47–90 min, so 5 min ≪ 47 min with ~9× margin; 1-min escalation when
  the weather drama is already visible buys back most of the residual.
- Trigger = `unwind_trigger(prev, cur, event_date=pair.date)` (in `probe_mlb_postpone.py`, offline
  selftest green): **UNWIND** on {Postponed/Cancelled/Suspended + makeup/resume > 2d from the bound
  event date OR unknown} or an officialDate move > 2d; **WATCH** on a ≤2d makeup; require 2 consecutive
  confirming polls only for WATCH→hold decisions, never to delay an UNWIND (act on first sight; +5 min
  of confirmation is not worth the window).

## 3. The deadline — what "2 days" actually means (primary text + measured behavior)

Kalshi rules verbatim (`KXMLBGAME-26JUN131910HOUKC-KC`, pulled live, `_data/mlb_kxmlbgame_rules.json`):

> "The following market refers to the Houston vs Kansas City professional baseball game **originally
> scheduled for** Jun 13, 2026 at 7:10 PM EDT. If this game is postponed or delayed, the market will
> remain open and close after the rescheduled game has finished (**within two days**). If the game is
> cancelled or **rescheduled to over two days away**, the market will resolve to **a fair price** in
> accordance with the rules."

So "2 days" anchors to the **originally scheduled date**, and the void condition is decidable the moment
a >2d makeup is announced — Kalshi need not (and does not) wait 48h. Measured (n=3 scalar voids,
`_data/mlb_postmortem.json`): trading closed **+47 min** (TB@NYY), **+90 min** (STL@CIN), **+82 min**
(BOS@NYY) after the original scheduled start. **Deadline rule: unwind immediately on trigger; assume the
Kalshi book can shut ~45 min after the scheduled start.** The 2-day outer bound applies only while no
makeup is announced (not observed: 5/5 carried `rescheduleDate` at once).

## 4. What each venue actually did (first empirical read of the void path, n=5)

| Game | makeup | Kalshi | pmus |
|---|---|---|---|
| STL@CIN 05-22 | +1d | settled **yes/no $1.00/$0.00 on the replay** (closed after makeup ended) | closed, EXPIRED, book empty, outcome **still null** |
| DET@BAL 05-23 | +1d | settled **yes/no on the replay**; `gameStartTime` updated in place on pmus | same |
| TB@NYY 05-23 | +122d | **`result='scalar'` settle $0.44/$0.56** (last 0.44/0.50 — pair normalized to $1.00), closed start+47min | closed, EXPIRED, `endDate` = start+**14d** exactly, outcome null (19d later) |
| STL@CIN 05-24 | +85d | scalar $0.48/$0.52 (last 0.47/0.50), closed start+90min | same (endDate = start+14d) |
| BOS@NYY 06-06 | +84d | scalar $0.49/$0.51 (last 0.50/0.50), closed start+82min | same |

Reads: (a) Kalshi's "fair price" = **scalar settlement ≈ pre-close market price, normalized so the pair
sums to $1.00** — the void recovers ≈ market value on the Kalshi leg (single-leg adjustments up to 6¢
observed: NYY last 0.50 → settled 0.56). (b) The ≤2d WATCH path is settlement-identical end-to-end
(2/2 settled the replay; pmus tracks the makeup in the same market). (c) pmus `endDate` = original start
+ exactly 14 days = the 2-week rule operationalized; **outcome posting lags even past endDate** (05-22
still null at +19d — consistent with settle_recon). (d) Empirical makeup distribution: 2/5 next-day,
**3/5 months-out, 0/5 in the 3–14d real-winner-vs-void gap** — MLB either replays immediately or banks
the makeup for the next series visit. n=5: do not over-update 0010 yet, but the *catastrophic* mode
(pmus pays real winner while Kalshi voids) looks rarer than p_gap=0.4, while the trigger itself fires
*more* often (3/5) in a milder months-out mode (asymmetric-void basis + ~2wk pm-side lock).

## 5. Exit-depth reality (the unwind sells into BIDS)

Live pre-game (06-11 pairs, 21:30 ET 06-10, n=3 pairs, `_data/mlb_pregame_books_0611.json`):

| Book | spread | bid @ touch | cum bids ≤3¢ |
|---|---|---|---|
| pmus az-mia / min-det / stl-nym | 1¢ / 1¢ / 1¢ | 78k / 82k / 1.3k | 154k / 163k / 99k |
| Kalshi 6 team tickers | 1¢ all | 1.1k–63k | 138k–205k |

Live in-play (06-10, `_data/mlb_unwind_books.json`): liquid case (min-det) 1–2¢ spreads, 9.3k/0.4M at
touch; trailing-side case (lad-pit, LAD ~0.14) pm 6¢ spread 1@touch (142 ≤3¢), Kalshi trailing leg
**36¢ spread** — in-play unwinds on a lopsided game cost multiples of pre-game. Archive context
(transitions 06-10, px-epoch, n=1652 in-play / 6 pre-game records — snapshots AT arb transitions, not a
continuous sample): pmus spread med **1¢** (p75 2¢), Kalshi overround med **1¢** (p75 2¢). Every
measured pre-game leg absorbs a 0012-scale clip (≤~2k pairs) at touch or one level down.

## 6. The unwind rule (spec for the future trade layer)

- **Trigger:** detector (§2) returns UNWIND for a held MLB pair = postponement/cancellation/suspension
  detected AND makeup outside Kalshi's ≤2-day window (anchored to the originally scheduled date) or
  unknown. WATCH (≤2d makeup) = hold; re-verify game binding (doubleheader guard already in
  `colisted_map.pick_game`) and re-poll.
- **Action:** unwind **both legs taker, immediately** — sell the pm YES into pm bids; sell the held
  Kalshi YES into that ticker's yes-bids (Kalshi nets the position). Speed dominates fees during a
  liquidity-decaying event; rest a maker order only for the pmus leg (maker rebate −0.0125·p(1−p)) and
  only if the book is visibly stable — Kalshi MLB makers pay 0.0175·p(1−p) (fee-pin: MLB is
  `quadratic_with_maker_fees`), so there is no fee-free Kalshi resting exit. If Kalshi has already
  closed (observed possible from start+47 min): unwind the pm leg alone ASAP and book the Kalshi
  scalar receivable at ≈ fair price.
- **Deadline:** act within minutes of trigger; hard assumption = Kalshi book shuts at original start
  + ~45 min once a >2d makeup is announced. The "2 days" text is the outer bound only while no makeup
  exists (not yet observed).
- **Expected cost (measured):** re-cross 2 × 1¢ spreads + taker fees ≈ kfee(0.5)=1.75¢ + pfee(0.5)=1.25¢
  → **~5¢/contract** + forfeited locked gross g (τ=2¢ floor per 0014, typical 2–3¢) ≈ **7–8¢ all-in**
  pre-game; multiples of that in-play on a lopsided game (cap in-play exposure accordingly).
- **EV (cite 0010):** unconditional prior stays `void_haircut` ≈ 1.3% × 0.4 × 0.5 ≈ **0.26¢/contract**
  (live re-measure of P(postpone): 5/415 = 1.2%). Conditional on trigger — the number the rule acts
  on — hold-EV ≈ −[p_gap·L − (1−p_gap)·g] ≈ −(20¢ − 0.6g) ≈ **−18¢** vs unwind-EV ≈ −(g + 5¢) ≈
  **−7–8¢** → **unwind wins ≈ +12–13¢/contract**. Breakeven at p_gap·L ≈ 5¢ + 1.6g ≈ 8–10¢, i.e. 0010
  would have to be ~2.5× too pessimistic before holding wins. Empirical severity mix (n=5, §4) is
  milder than 0010's flat 0.4×0.5 but unwind still dominates in both observed modes (months-out:
  saves basis noise + ~2-week pm capital lock for ~the same price recovery; 3–14d gap: removes a
  ±50¢ coin flip for 7–8¢).
- **Residual risks:** (1) in-play suspension → trailing-leg exit cost ≫ 5¢ (measured 36¢ spread once);
  suspended games usually resume next-day (→ WATCH), so true Suspended-UNWINDs should be rare;
  (2) Kalshi closes before we act → pm-leg-only unwind + scalar receivable (basis ≤ ~6¢ observed
  single-leg normalization); (3) doubleheader rebinding on ≤2d makeups — monitor's `used`-set +
  exact-date binding + duplicate-ticker guard cover it; verified the venues kept the ORIGINAL
  market/tickers through the det-bal next-day makeup; (4) **pmus post-postponement book-close timing is
  UNMEASURED** (no `closedTime`; books observed only days later as EXPIRED/empty) — if pmus freezes at
  the originally scheduled start, the pm-leg window ≈ pre-start only, which still works for
  announced-before-start rainouts (the common case) but makes detection-before-start the real
  constraint; first live-detected postponement must record both books minute-by-minute; (5) the pmus
  "last-traded" void basis is rules-text-only until a postponed market finalizes — **watch
  `aec-mlb-tb-nyy-2026-05-23` for the first empirical print** (endDate passed 06-06, outcome still null).

## Caveats / data honesty (L18/L19)

Postponement sample n=5 (one 30-day window, all rain); Kalshi close-time n=3; pre-game book sample
n=3 pairs at one timestamp; archive px-records are transition-conditioned snapshots from a single day
(06-10). The 0010 p_gap/loss split should be refit once live-detected postponements accumulate; nothing
here validates edge size — it validates that the unwind rule is implementable, cheap relative to the
modeled tail, and that the real deadline is ~start+45 min, not 2 days.
