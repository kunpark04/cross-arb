# Trading-strategy + risk review — cross-arb Rust live bot (`bot-rs`)

**Reviewer hat:** quant trader + risk manager. **Date:** 2026-06-11.
**Scope:** `bot-rs/src/{risk,config,exec,ledger,main,types}.rs` (stage-1 spine, dry-run) audited
against the project's own measured findings (deployment-readiness + backtest 2026-06-11, probe-program
2026-06-11, settlement briefs, 0014 prereg). **Read-only review — no code edited.**

**Framing.** The bot is *correctly built* as a safe-by-default spine: dry-run default, sandbox default,
1-contract/tiny-notional caps, hard prod consent gate, kill switch, external RW key. The
**accounting/fee core is sound and self-verifying** (outcome-independence assert, per-order ceil fee,
marginal-fee detection). My critique is **not** "the spine is wrong" — it is that the **risk gate
encodes the project's *structural* invariants (settlement identity, crossed/stale/mid-divergence,
edge≥floor, caps) but encodes *none* of the project's *hard-won behavioral* findings** — and those
findings are the entire reason the readiness audit said NOT READY. The gate would happily approve the
exact trades the audit identified as money-losers. Below, every claim is tagged to a finding.

The single most important context: **the edge is not validated** (effective n≈1 day, 71% of apparent
edge phantom, 85% sub-tick, *median arb friction-negative*, fattest edges most toxic & fastest-dying,
capital velocity binding). The bot's job right now is to **lose as little as possible while measuring**,
not to harvest. Judge every design choice by "does it protect the staged rollout / produce a clean
measurement," not "does it maximize PnL."

---

## 1. Does the RISK design capture what the project learned?

**Partly. The structural invariants are in; the behavioral edge-shape findings are absent.** Audit of
`risk.rs::evaluate` gate-by-gate:

### What is correctly captured (credit where due)
- **Settlement identity (gate 1)** — present and *correctly conservative*: `require_settle_clean &&
  !settle_clean && cat != Weather → SettlementUnverified`. This matches the briefs exactly: weather is
  the only empirically-verified category (settlement-verification.md §4: 360/360, 0 divergence); sports
  is rules-text-only until ~06-23/25; econ source-identity reconciled on 3 past events but the
  cumulative-twin instrument settles 07-02. **Hard-coding the weather carve-out is right** but see the
  bug in §1-gaps below — `cat==Weather` *bypasses* the flag instead of requiring `settle_clean=true` for
  weather too, which is a footgun if the discovery layer ever emits a weather pair with a bad bucket join.
- **Crossed/stale book (gate 2)** — L12/L13, correct, mirrors `signal()`'s crossed rejection in
  `ledger.py`.
- **Mid-divergence (gate 3, L1)** — right *tool*, wrong *coverage* (see below). A 40¢ guard catches a
  gross bad-join/stale quote. Good.
- **Non-positive edge + opt-in floor (gate 4, L11/L15)** — correct; `edge_floor_cents=2.0` is the 0014
  pre-registered τ. The L15 discipline ("a thin book is *small* size, not no-trade") is correctly
  honored — `NoFillableSize` only fires when size collapses to literally 0, and the floor is *reported*,
  never silent.
- **Caps + concurrency (gates 5–6)** — pair/cluster/total notional + concurrency, with cluster keyed on
  city-date / game (the correlated-risk axis the probe-program §9 measured). Sizing is
  `min(depth.c2, pair_cap, affordable, notional_caps)` — depth-limited, which is the right backbone.

### The gaps (ranked by how directly they contradict a measured finding)

**GAP-1 — FAT-EDGE-IS-TOXIC is not encoded; the bot treats bigger edge as strictly better. (Severity:
HIGH — this is the headline finding of the whole audit.)** `evaluate` rejects `edge ≤ floor` and then
**sizes monotonically up with depth** — there is no upper scrutiny on edge fatness. But Audit G is
unambiguous: **≥8¢ gaps are 66% toxic** (informed cheap side) vs 47% for the body, and they **die in
~0.5 s vs ~1.6 s**; fat edges are **62% naked @1 s**. The readiness audit's #1 of "three findings that
answer consistent-profits": *"The bigger the arb, the worse it is… the fat tail that drives the EV mean
is exactly the part you most often fail to capture, and when you do you get the wrong side."* The bot's
sizing curve points **exactly the wrong way** — it leans hardest into the fattest, most toxic, fastest-
dying edges. This is the single most important risk-design defect. **Fix:** size and/or scrutiny should
be **non-monotone (inverse) in edge fatness** above a toxicity knee (~5–8¢), OR a `FatEdgeToxic` gate
that *down-weights/quarantines* gaps ≥ a configurable `toxic_edge_cents` unless a leadership/sequencing
condition clears them (see §2). At minimum: cap clip size on fat edges to 1 contract and require the
benign-sequencing condition.

**GAP-2 — No capital-velocity / edge-RATE notion (H2). (Severity: HIGH — capital velocity is *the*
binding constraint per Audits C/D.)** `Quote`/`Edge`/`evaluate` have **no concept of expected-lock-days**.
The 0014 prereg's H2 explicitly ranks by `booked_edge / expected_lock_days` with frozen priors (weather
1.2 d / sports 15 d / econ days-to-release). Audit D: weather is the only fast category (~1.2 d natural
settle + liquid evening exit), sports is +14 d **book-frozen** (pmus freezes the book at resolution → no
early exit), econ is weeks-to-release **capital-locked**. The bot will cheerfully sink its tiny notional
into a 15-day sports lock with the *same* priority as a 1.2-day weather arb of equal cent-edge — which is
**strictly capital-inefficient** and, at the $500 reality (account_sim: 3 enter, 1 realized, 2 locked,
~67% frozen), is how you wall out at ~$177/day deployable. **Fix:** add `expected_lock_days` to the
quote (frozen 0014 priors), rank/prioritize and ideally *cap* by **edge-rate** not edge-level, and add a
**per-category capital budget** (weather should get the lion's share). This is also the cleanest way to
"avoid over-committing to the 14-day sports lock" (the prompt's Q3 ask): a sports lock must clear a
*much higher* cent-edge bar to justify tying up capital 12× longer.

**GAP-3 — Econ twin-mid-divergence guard missing; L1 guard does not fire on econ. (Severity: MEDIUM —
flagged as an actionable code gap by the readiness audit itself.)** The readiness audit's "one new code
gap": *"the >40¢ price-sanity guard (L1) does not fire on econ, and nothing flags a mid-divergence
between two settlement-identical econ twins."* The only live econ "edge" in the universe (U-3 ≥4.2,
+9.2¢, n=1) is an **18¢ mid disagreement between provably-identical twins** on thin books 3 weeks
pre-release — a textbook stale/informed artifact, not an arb. In `risk.rs` the mid-divergence gate at 40¢
*would* fire at 18¢… **no it wouldn't** — 18¢ < 40¢, so it passes. And econ is already gated out by
`settle_clean=false` today, so this is latent — but the moment econ flips to `settle_clean=true` (07-02),
**a much tighter twin-mid guard (~a few ¢) must exist for econ** because two *settlement-identical* twins
should price within ~tick, not 18¢. **Fix:** category-specific `mid_divergence_reject_cents` — econ
twins get a tight bound (≈3–5¢), weather/sports keep the loose 40¢. The smoke test in `main.rs` even
hard-codes this exact U-3 case as the "must refuse" example — encode *why* (twin divergence), not just
the settle-clean flag.

**GAP-4 — Naked-leg / unwind cost `u` is the go/no-go term and the gate has no hook for it. (Severity:
HIGH for live, but correctly deferred to stage-2 `legs.rs`.)** The readiness audit: `u` is *"the single
term the go/no-go pivots on, unmeasurable from transition logs."* Probe §2: naked 17–29% @100–250 ms,
breakeven `u` ≈ 4–6¢ vs ~1–3¢ plausible. This lives in `legs.rs` (not yet built — confirmed: only 6
.rs files, no `legs.rs`), and `risk.rs` correctly says so in its header. **But** `leg_fill_timeout_ms`
(500 ms) is a config knob with **no consumer** in the spine, and there is **no `expected_unwind_cost`
term in the EV at gate time**. A 2¢ edge floor that ignores a 1–3¢ expected unwind tax can approve a
*net-negative-after-unwind* trade. **Fix (stage-2, but design it now):** the gate's effective edge floor
should be `τ + E[naked_rate × unwind_cost]`, i.e. the floor must *rise* with measured naked-leg
probability at the prevailing latency. Until `u` is measured, the conservative default is a higher floor
on fast-dying (fat) edges — which dovetails with GAP-1.

**GAP-5 — MLB postponement kill is documented but unbuilt; no `void_clean` plumbing into the gate.
(Severity: MEDIUM — a both-legs-loss tail.)** `colisted_map.py` tags **every** sports pair
`void_clean=False`, and the sports brief is explicit: a 3–14-day-replayed postponed MLB game **pays the
real winner on pmus but voids to fair-price on Kalshi → both-legs loss**. Probe §7: the actionable window
is **minutes** (Kalshi voids 47–90 min post-scheduled-start), unwind is **+12–13¢/contract on trigger**,
a 5-min statsapi poll detects it. The Rust `Quote` has no `void_clean` field and the gate has no
postponement awareness. This is fine *while sports is gated by `settle_clean=false`*, but it must be
plumbed before sports goes live: `void_clean` into the quote, and the stage-2 leg-manager must own the
5-min poll + minutes-fast unwind. **Today's risk:** none (sports blocked); **latent:** high the day
sports is enabled.

**GAP-6 — Stream-paused / WS-reconnect is a single global flag, not per-venue. (Severity: LOW.)**
`Exposure.stream_paused` is one bool for "EITHER venue reconnecting." The monitor side (0013) tracks
`ws_reconnect` *per venue*. A single global flag is *safe* (it halts all trading on any reconnect) but
coarse — it will pause weather trading because a sports-only pmus book is rebuilding. Acceptable for
stage-1; refine to per-venue/per-market when the matcher is ported.

**GAP-7 — No depth-vs-toxicity interaction in sizing. (Severity: MEDIUM.)** Sizing uses `depth.c2`
linearly. But Probe §2 (depth lens, L18): *"restricting to c2≥100 buys only ~3 pp survival — depth does
not rescue the race."* Deep + fat is *still* toxic. So "big depth" must **not** license a big clip on a
fat edge — the c2 sizing should be gated by the toxicity/sequencing condition, not taken at face value.
Ties to GAP-1.

---

## 2. Entry timing — should it fire on a complete dual-venue snapshot?

**The complete-snapshot trigger is correct (L5 — classify on the complete state). But "fire" should be
conditioned on *leg-sequencing*, which the design has no hook for.**

- **Leg-sequencing (the z=−13.1 / z=4.58 weather signal) is NOT in the design.** `legs.rs` doesn't
  exist. Probe §5 + readiness Audit G: weather **cheap-side-made opens run 18% toxic vs dear-side-made
  79%** (z=4.58), and the close-level signal is z=−13.1 — *you can avoid the toxic fill by taking the leg
  that moved first*. This is the project's **one usable execution edge** and the bot captures none of it.
  The current `report()` in `main.rs` just submits the YES leg on pmus unconditionally. **Design ask:**
  the stage-2 leg-sequencer must (a) know **which venue led the quote** (the move-first signal), and (b)
  **take the leg on the side that made the edge first**, hedging the laggard. This is *not* a skip filter
  (Probe §5 says the skip-filter is null, z=−0.68 — do **not** build that); it is a **sequencing /
  leg-ordering** rule. Encode it as: `Quote` carries a `led_by: Option<Venue>` (which venue's quote moved
  to create the gap), and the leg-manager sequences accordingly.
- **Should entry be conditional on which venue led (toxicity direction)?** **Yes, for weather, as a
  sequencing input — not a hard skip.** The benign case (cheap-side-made, 18% toxic) you trade; the toxic
  case (dear-side-made, 79%) you *still* trade but lead with the cheap leg to dodge adverse selection.
  Combined with GAP-1: a **fat** edge in the **toxic-direction** is the worst quadrant — that is where a
  size cap / scrutiny gate should bite hardest.
- **Fire-on-WIDEN layering — NO, refuted.** Watch for L20: book-init/WIDEN spikes are the phantom
  signature (37.7¢ ITF, restart-censored; 5 ITF/valorant with open_net 0–1¢ but 36–37¢ "peak").
  "Fire on widen" would chase exactly these. The bot must fire on a *stable* gap, not a widening one;
  if anything, a freshly-widened gap should *raise* scrutiny (it is disproportionately a book-init/restart
  phantom or a toxic informed move). Do not build fire-on-widen.

---

## 3. Exit timing + capital velocity

**Hold-to-settlement is correct (early-exit measured −EV, Probe §6: −1.5¢/pair → reject exit-all). But
capital velocity is the binding constraint and the design does not recycle / re-score.**

- **Hold-all is right.** Probe §6 + §8: early-exit = hold-all; recycle arm measured **$0.00** (n=1
  recycle event, skipped pool dies in ~1 s median). So *recycling on settlement is correctly NOT a
  priority* — but the bot also has **no settlement-driven re-scoring loop at all**, which is fine for
  stage-1 but means it can't reallocate freed capital when a fast weather pair settles while slow sports
  capital is still locked. Low priority to build (measured $0 today) but flag for the multi-week recheck.
- **Sizing should be edge-RATE, not edge-LEVEL — this is GAP-2 restated and it is the highest-value
  strategic change.** The 0014 H2 (`booked_edge / expected_lock_days`) is the right object. Concretely:
  a 3¢ weather edge (1.2 d → 2.5¢/day) should out-prioritize a 4¢ sports edge (15 d → 0.27¢/day) by ~9×.
  The bot today would rank the sports edge *higher* (bigger cents). **Add `expected_lock_days` (frozen
  priors), rank by rate, budget capital by category.**
- **Avoiding over-commit to the 14-day sports lock:** beyond edge-rate ranking, add a **per-category
  notional budget** (the 0014 caps are 20/10/5% weather/sports/econ — these are *per-pair* caps, not
  *category* budgets; a category-level cap is missing). With a $500 bankroll that locks after ~10
  positions, a hard "≤X% of capital in >7-day-lock categories at once" budget prevents the slow-lock
  starvation Audit D measured.

---

## 4. Profit optimization — taker-only vs maker mode

**The bot is taker-only; the maker study found the *only* +EV non-trivial configuration is rest-the-
cheap-leg-on-Kalshi (fee-negative weather) + taker-hedge-pmus. The bot should support a conditional-maker
mode — but as a *measured stage-2 experiment*, not a day-1 default.**

- **The maker edge is real but narrow and one-directional.** Probe §1: rest on **Kalshi** (weather, $0
  maker fee — confirmed in `ledger.rs` `KALSHI_MAKER_COEF` comment + fee-pin: 12/23 series have no maker
  fee, all weather) + taker-hedge pmus = **+0.14 to +0.44¢/attempt**. **Rest-on-pmus is structurally
  toxic** (15–16¢ hedge slippage — the rebate is irrelevant). Full maker-maker carries 32% one-leg-naked
  + ~2.2 h unhedged windows — **not** priceable yet. So the *only* config to build is **Kalshi-rest +
  pmus-taker-hedge, weather-only**.
- **`ledger.rs` is taker-only today** (`order_taker_fee_cents`, `marginal_taker_fee`; `KALSHI_MAKER_COEF`
  exists but is unused and the comment says "stage-2"). A maker mode needs: (a) `fee_type` awareness per
  series (the $0-maker-fee weather series vs the fee-positive MLB-type — `ledger.py` already models this
  distinction; the Rust must port it), (b) a resting-order lifecycle (place, queue-wait, cancel-on-timeout,
  hedge-on-fill), (c) the naked-window risk accounting. This is a **real new subsystem**, justified only
  once trade/ladder logging (Probe §1, now logging post-redeploy) **measures** the +0.14–0.44¢ bound into
  a number. **Recommendation:** keep stage-1/2 taker-only; add maker as a *flagged, weather-only,
  Kalshi-rest* mode for the paper-trading phase, gated behind the measured fill data.
- **Is 2¢ floor + caps the right economics?** **The floor is right by pre-registration (0014, τ=2¢) —
  do not re-tune it (L19).** But it is *incomplete*: Audit E shows the **median 0.39¢ arb is friction-
  negative** and only the **≥1¢ weather, fast-fill, single-digit-contract** cohort stays positive. A flat
  2¢ floor applied across categories lets through sub-marginal *sports* arbs (Audit E: sports +0.22¢ →
  **−0.41¢** all-in). So the floor should arguably be **category-differentiated** (higher for the
  friction-heavier, slower categories) — but **this is a tuning change and must be pre-registered, not
  slipped in** (the 0014 freeze + L19 are explicit). For *now*: ship τ=2¢ frozen; the category-floor idea
  is a HYPOTHESIS for the next prereg (see §5 H7).
- **Sizing vs depth/toxicity:** covered in GAP-1/GAP-7 — size must be **inverse** to toxicity (fat edge)
  and **not** licensed by depth alone.

---

## 5. NEW IDEAS TO TEST — ranked (each a HYPOTHESIS with its test + deflator)

> Grounded in the findings; none re-proposes a refuted idea (batch-1s ordering = +0.0% / early-exit −EV /
> bigger-clips-in-thin-corners = the toxicity trap are all **excluded**).

**H1 — Toxicity-direction / leg-sequencing entry gate (weather). [RANK 1 — highest value, already has
the strongest live signal.]**
*Hypothesis:* taking the leg on the side that **made the edge first** cuts naked-leg toxicity from the
79% (dear-side-made) regime toward the 18% (cheap-side-made) regime; and gating *fat* edges on the benign
direction recovers most of the EV the fat-edge toxicity destroys.
*Test:* the **pre-registered confirmation** Probe §5 already specifies — weather sequencing cells at
~1 week post-0013 data (cells n≈150–250), z-test toxicity by at-open class, and a shadow-fill A/B of
sequenced vs simultaneous entry on the naked-leg rate.
*Deflates if:* the z=4.58 signal is a small-cell artifact that washes out at n≈200 (it's a
subgroup-after-a-null, L19), or sequencing latency (taking one leg first widens the unhedged window)
costs more in naked-leg risk than the toxicity it dodges.

**H2 — Edge-RATE capital allocation (`booked_edge / expected_lock_days`) with per-category budget.
[RANK 2 — directly attacks the binding constraint.]**
*Hypothesis:* ranking + budgeting by edge-rate (not edge-level) materially raises realized return at a
fixed bankroll vs the level-ranked FIFO/reservation baseline, because it stops slow sports/econ locks
from starving fast weather turns.
*Test:* this **is** the 0014 H2 arm — run it as the pre-registered confirmatory comparison (H1 level vs
H2 rate) on ≥14 event-days. No new prereg needed; it's already frozen.
*Deflates if:* weather volume is so capacity-walled (~$177/day, Audit D) that even perfect velocity
ranking can't deploy more capital — i.e. the constraint is weather *supply*, not *ordering* (Audit D
hints this: bankroll never binds, 70–96% idle).

**H3 — Time-of-day arrival exploitation (concentrate capital + readiness in the 9 am–4 pm ET window).
[RANK 3 — cheap, data-grounded, no model risk.]**
*Hypothesis:* arbs arrive 13–20 Z (≈9 am–4 pm ET) with **no evening cluster** (backtest §2 explicitly
refuted the evening-cluster hypothesis). So holding maximum dry powder / lowest concurrency-cap pressure
during that window, and *not* reserving capital for a (non-existent) evening wave, improves fill of the
real arrivals.
*Test:* split realized capture by arrival-hour on the multi-week data; compare a flat-allocation policy
vs an intraday-weighted one (more aggressive sizing 13–20 Z).
*Deflates if:* the 9–4 peak is an artifact of the single 06-10 day (effective n≈1 — the whole dataset is
basically one day, so *any* intraday pattern is suspect until multi-day data confirms it persists).

**H4 — MLB postponement-unwind optionality as a *positive* EV term (not just a risk patch). [RANK 4 —
measured +12–13¢/contract on trigger.]**
*Hypothesis:* a 5-min statsapi postponement poll + minutes-fast unwind is not merely a both-legs-loss
*hedge* — it is a small **+EV optionality** the bot can lean into (size MLB *slightly* less conservatively
because the tail is now actively managed, vs voiding blind).
*Test:* build the detector read-only now (Probe §7 says it's buildable anytime); paper-trade the
unwind-on-trigger vs hold-through on the postponement events that occur; confirm the +12–13¢ and the
minutes-window detection (5/5 in the 30-day scan).
*Deflates if:* makeup-scheduling empirics (0/5 in the 3–14 d gap in the probe window) mean the *material*
real-winner-vs-void divergence almost never fires → the option is worth ~$0 in practice (the EV leans on
0010's gap-prob 0.4 which the probe flagged as maybe 2.5× pessimistic).

**H5 — Econ twin-pair mid-divergence as a *settlement-quality monitor*, not a tradeable edge. [RANK 5 —
defensive, prevents a phantom from being traded.]**
*Hypothesis:* two settlement-identical econ twins diverging >~3–5¢ in mid is **always** a stale/informed-
book artifact (never a real arb), so a tight econ-twin mid guard prevents the bot from ever booking the
U-3-type phantom when econ flips to `settle_clean=true` (07-02).
*Test:* on the post-07-02 econ recon data, measure the distribution of twin mid-divergence and confirm
that divergence does **not** predict a real settlement split (it shouldn't — they settle identically by
construction). Build the guard before econ is enabled.
*Deflates if:* (it shouldn't) — but if real twin divergences ever *do* predict a settlement split, the
settlement-identity assumption itself is broken and econ must go back to OPEN.

**H6 — Maker-rest on the sleepy venue (Kalshi weather), measured. [RANK 6 — real but unproven +EV, needs
the new fill data.]**
*Hypothesis:* resting the cheap leg on Kalshi (weather, $0 maker fee) + taker-hedge pmus realizes the
+0.14–0.44¢/attempt the study bounded, beating the taker-taker fee wall.
*Test:* the trade-print + ladder logging (Probe §1, now live post-redeploy) converts the bound into a
measured per-filled-attempt number with a real queue model; paper-trade Kalshi-rest weather.
*Deflates if:* realized queue position / cancel-timing means the visible-fill lower bound (40–77%) is
optimistic and actual fills are adversely selected (the resting leg fills *only* when it's about to be
wrong) — i.e. the 2¢ Kalshi-rest slippage measured small turns out larger live.

**H7 — Category-differentiated edge floor τ(category). [RANK 7 — must be pre-registered; do not slip in.]**
*Hypothesis:* a flat 2¢ floor lets through friction-negative sports arbs (Audit E: sports +0.22¢ →
−0.41¢ all-in); a higher sports/econ floor (the friction-heavier categories) and the existing 2¢ for
weather improves all-in realized PnL.
*Test:* a **fresh prereg** (the 0014 freeze forbids re-tuning τ on the same data) + the multi-week data,
sweeping per-category floors out-of-sample.
*Deflates if:* category sample sizes are too small to estimate a per-category floor (sports already only
28 τ-clearing candidates total), so the differentiation overfits.

**H8 — Hedge-the-flip-destination-bucket on weather boundary days (instead of early-exit). [RANK 8 —
Probe §6's flagged "cheaper dominant alternative."]**
*Hypothesis:* on a boundary-revision day, buying the flip-destination Kalshi bucket at its ~1–2¢ ask is a
cheaper hedge than exiting, capturing the downward-CLI-correction tail without paying the exit spread.
*Test:* measure flip-destination ask depth (unmeasured in Probe §6) and P(downward flip) (needs ~3 more
weeks of `cli.jsonl`; currently 0/14 station-days, Jeffreys ceiling 12.6%); compare hedge-cost vs
hold-and-eat-the-flip.
*Deflates if:* P(downward flip) measures <~2% (then the hedge premium isn't worth it — same breakeven the
maker-exit faces), or the flip-destination bucket has no ask depth to hedge into.

**Explicitly NOT proposed (refuted — listed so the exclusion is on record):** batch-1s sort ordering
(+0.0%, backtest §4), early-exit/exit-all (−1.5¢/pair), bigger clips in thin corners (the toxicity trap),
fire-on-widen (L20 phantom signature), recycle-on-settlement as a priority ($0.00 measured), full
maker-maker (32% naked / 2.2 h windows / unpriceable), rest-on-pmus (15–16¢ toxic).

---

## 6. Bottom line

**Is the strategy/risk design sound for a LIVE deployment given the not-validated edge?** **As a
*safe-by-default spine* — yes, and impressively disciplined** (dry-run/sandbox/consent/caps/kill-switch
are exactly right for an admittedly-unvalidated edge under 0015). **As a *strategy* — no, not yet, and it
shouldn't be turned to `live`+`prod` on real money** until (a) the multi-week 0014 data validates the
edge, (b) `u` (naked-unwind cost) is measured, (c) sports/econ settlement reconciles, and (d) the three
behavioral findings below are encoded. The gate currently captures the project's **structural** invariants
but **none of its behavioral edge-shape findings** — so it would approve precisely the trades the audit
called money-losers (fat/toxic edges at full size, slow-lock sports at weather priority). The good news:
the spine is built so that adding these is additive, and the defaults already force the staged rollout.

**Top-3 highest-value design changes:**
1. **Encode fat-edge toxicity (GAP-1):** make size/scrutiny **inverse** to edge fatness above a ~5–8¢
   toxicity knee (and require benign sequencing on fat edges). The bot's monotone-up sizing is the most
   wrong thing in the design relative to the #1 measured finding.
2. **Add edge-RATE ranking + per-category capital budget (GAP-2 / 0014-H2):** `booked_edge ÷
   expected_lock_days` with frozen priors; prioritize weather, hard-cap capital in >7-day-lock categories.
   Attacks the binding constraint (capital velocity).
3. **Wire leg-sequencing into the (unbuilt) leg-manager (§2/H1):** take the leg that moved first; this is
   the project's one usable execution edge (z=4.58 weather), and it's the mitigation that makes fat edges
   tradeable at all.

**The single most important thing to TEST first:** **measure the naked-leg unwind cost `u`** (the only
term the entire go/no-go pivots on, per the readiness audit — unmeasurable read-only, breakeven ≈4–6¢ vs
~1–3¢ plausible). It requires the demo-sandbox / paper-trading phase the staged rollout already prescribes
(shadow/IOC probe orders post-greenlight), and **it gates whether *any* of the taker strategy is viable.**
Everything else (sequencing, edge-rate, maker) is a refinement on top of a strategy that does not clear
its costs if `u` lands on the wrong side of breakeven. Test `u` first; do it on the sandbox, weather-only,
≥1¢-only, single-digit-contract, before a dollar of production capital.

---

*Artifact: `tasks/_agent_bus/20260611-rust-review/trading-strategy-review.md`. Read-only review; no code
changed. Anchored to deployment-readiness-2026-06-11, backtest-2026-06-11, probe-program-2026-06-11,
settlement-verification.md, sports-settlement-verification.md, decisions 0013/0014/0015,
`bot/colisted_map.py`, `bot/ledger.py`.*
