# Adversarial Readiness Audit — Dimension D: CAPITAL VELOCITY & REALISTIC RETURNS

**Date:** 2026-06-11 · **Scope:** what a real bankroll actually EARNS net of lockup, on ALL data, applying
the FROZEN 0014 H1 rule (NOT re-tuned). **Phase:** read-only, paper. **Verdict in one line:** the realistic
realized return is **tiny and lumpy** — gross **+1.26% over the whole 1.98-day window at $500** (≈+0.85% under
friction), the bankroll **never binds** (peak ~30% deployed, falling to ~9.5% at $10k), the tradeable universe
is **15 positions total** (6 after the L20 phantom lens), weather — the only fast category — caps at
**~$177/day of deployable capital**, and arrivals come in **lumps (top-1 day = 62% of booked edge, max 6.8 h
inter-arrival gap)**. This is a method demo on <2 days; magnitudes are not deployable.

## Method (reuse, no re-tune)

- Imported verbatim: `analyze_persistence.load/build_episodes`, `capital_sim.capturable/one_per_market/settle_t/
  void_haircut/peak_and_avg/DEPTH_BOUNDARY_NET`, `alloc_policy_experiment._profit_per/_cost_per`. The frozen
  H1 rule needs **per-category caps** (weather 20 / sports 10 / econ 5 %) that the committed scripts do not yet
  implement (prereg §1b: that machinery lands before the *confirmatory* run). This is a **descriptive** audit,
  so I mirrored `run_policy`'s reservation walk and added only the category-cap gate, changing no economics.
  Driver + JSON dumps in `scripts/_data/auditD_h1_driver.py` / `auditD_h1_results.json` (gitignored).
- Frozen H1 constants applied exactly: τ=2.0¢ booked/contract; caps 20/10/5%; `capturable(edge_min=0,
  window_min=0, liq_floor=1, max_age=off, drop_restart=True, drop_flat=False)`; void_haircut ON (void_mult=1);
  one_per_market; settle proxy = event-date + 28 h. $500 = verdict; $2k/$10k = descriptive robustness.
- **Data:** `…\data\cross-arb\` — 60,363 transitions, **span 1.976 d** (2026-06-09 05:27 → 06-11 04:52 UTC),
  33,568 post-epoch. 36 pre-remap threshold-econ records quarantined (0013). 6,085 episodes (5,804 clean / 281
  restart). 4,061 pass the frozen capturable gate → **291 one-per-market candidates** → **15 clear τ=2¢**.

---

## 1. Frozen H1 applied to ALL capturable data — funding, deployment, realized/locked, ending statement

**The τ=2¢ floor admits only 15 candidates across the entire 2 days** (10 sports / 5 weather / **0 econ** — no
release in window). That is the binding fact; everything below follows from it.

| Bankroll | Funded (cat) | Deployed $ | Peak locked $ (% bankroll) | **Total PnL (gross)** | **Realized PnL** | Friction PnL¹ | Skips (τ / bankroll / cap) |
|---|---|---|---|---|---|---|---|
| **$500** | 9 (3 wx, 6 sp) | $168 | $149 (**29.8%**) | **+$6.30 (+1.26%)** | +$6.20 (+1.24%) | +$4.23 (**+0.85%**) | 276 / **0** / 6 |
| **$2,000** | 13 (5 wx, 8 sp) | $719 | $550 (27.5%) | +$22.47 (+1.12%) | +$17.68 (+0.88%) | +$13.72 (+0.69%) | 276 / 0 / 2 |
| **$10,000** | 15 (5 wx, 10 sp) | $1,443 | $948 (**9.5%**) | +$42.90 (+0.43%) | +$30.11 (+0.30%) | +$25.37 (+0.25%) | 276 / 0 / 0 |

¹ Friction = booked-edge-gated τ then subtract a 0.0115 haircut (= ~23% naked-leg @150 ms × 5¢ E_loss, from
the post-epoch shadow_fill curve + frozen E_loss) from funded-position profit, per prereg §3.2. A more
conservative haircut-then-gate variant gives +1.06%/+0.27%/+0.05% (fewer positions survive the τ re-check).
Both show **friction roughly halves the gross return.**

**Realized vs locked.** The realized/unrealized split is **an artifact of the settle proxy, not a measurement**
— at settle@+6h, $500 funds 13 and realizes 9; at +28h it funds 9 (slower recycle) and realizes 8; at +48h it
realizes 1. So I report **total booked PnL** (realized+unrealized) as the honest figure — which is also exactly
what prereg §4 defines as the verdict quantity ("captures more **booked edge** … positions cannot lose by
construction; settlement-realization is a separate open item, `settle_recon`").

**% capital sitting locked at any time** (time-weighted, settle@+28h): **$500 → 14.4% avg / 29.8% peak ;
$2k → 10.8% / 27.5% ; $10k → 3.5% / 9.5%.** The bankroll is **mostly idle** — there simply aren't enough
τ-clearing arbs to deploy it, and what does fund mostly settles fast (weather 1.2 d). **0 bankroll-skips at
every size** — capital is never the constraint; **opportunity arrival × depth × caps** is.

**Ending statement ($500, settle@+28h, gross):** start $500.00 → cash-free $502.39 → locked $3.81 →
realized PnL +$6.20 → unrealized +$0.10 → **equity ≈ $506.30 (+1.26%)** for the 1.98-day window. Naively
×15 ≈ +9–10 %/mo, but see §3/§4 — that extrapolation is not supported.

---

## 2. Velocity per category — edge-per-$-day

Median capturable edge: **weather 1.97¢ / sports 1.80¢ / econ no data.** Velocity (booked edge ÷ expected
lock-days, frozen priors) is where they diverge — **not the edge:**

| Category | edge | lock-days (reality) | **edge per $-day** | RoC/month | of $500, productively cycling? |
|---|---|---|---|---|---|
| **weather** | 1.97¢ | **1.2 d** (natural settle) | **1.64¢/$-day** | ~50%/mo | YES — recycles ~25×/mo |
| **sports** | 1.80¢ | **~7.5–15 d** (pmus endDate = start+14 d) | **0.12¢/$-day** (15 d) / 0.24¢ (7.5 d) | ~3.7%/mo | NO — frozen the whole window |
| **econ** | (none) | 21 d+ (release; book frozen) | ~0.1¢/$-day | ~lowest | NO — capital-dead |

**Weather has ~7–14× the capital velocity of sports.** The bankroll productively cycling is **almost entirely
weather**; every sports position locks past the data window (lock 7.5–15 d ≫ 1.98 d span), so in this dataset
sports capital is **100% frozen** — it books edge but never frees to redeploy. This is the L16 axis: edge lives
in thin/slow corners; the one fast corner (weather) is shallow.

---

## 3. CONSISTENCY — the owner's actual question: STEADY or LUMPY?

**Lumpy.** The fill-arrival process over the 15 τ-clearing arbs:

- **~3 fundable arbs/day** (3 on 06-09, 9 on 06-10, 3 on 06-11). Not a smooth Poisson trickle — a daily handful.
- **Inter-arrival gaps: median 2.2 h, max 6.8 h.** Hours of idle, then a cluster.
- **Concentration: top-1 DAY = 62% of booked edge; top-1 POSITION = 33%; top-3 = 64%.** A single day (06-10,
  driven by the fat ITF-tennis prints) carries most of the realized edge. Remove that day and the return roughly
  halves.
- **Top-1 position share of deployed capital** under H1: 0.40 ($500) / 0.27 ($2k) / 0.34 ($10k) — one weather
  pair eats ~40% of the (small) deployed capital at $500.

**Could a $500–$10k bot deploy steadily?** No. It would **sit ~70–96% idle**, fund a handful of small positions
in bursts, and earn a return dominated by 1–3 lucky fat-edge events. The H2 velocity-ordered arm (+1.73% vs
+1.26% at $500) does not change this — capital never binds at $500, so ordering is moot (prereg flags H2
"uninformative at this horizon"), and the gain is just which positions the caps happen to admit.

**Data-quality asterisk (L20):** **9 of the 15 τ-clearing candidates carry the flat-ladder (c2==c1==c0)
book-init phantom fingerprint** — including the two fattest (23.5¢ and 12.6¢ ITF-tennis). Under `--drop-flat`
the tradeable universe **collapses from 15 → 6** (2 wx / 4 sp). H1's frozen rule keeps them (drop_flat=False per
L15), but a real bot's realized return could be **well below** even these tiny numbers if those are phantoms.
Adjudication is the prereg §4 W5d checklist's job on real data.

---

## 4. The depth ceiling — weather's scaling wall (L16)

Weather is the only fast category, but it is shallow:

- **5 weather τ-clearing arbs over 1.98 d (~2.5/day)**, displayed depth median 49 / max 200 contracts
  (consistent with the ~157 crossable figure; recall only 3 weather fills in 16 h).
- **$-deployable per weather arb: $20, $24, $47, $67, $192 → ~$177/day total deployable**, capturing
  **~$4.26/day** of booked edge at unit fill (≈$128/mo gross on a fully-recycling weather-only book ≈ 26%/mo
  on $500 — IF every fill lands and recycles, which §3 says it won't steadily).

**The wall:** weather absorbs only **~$177/day** of capital. A $500 book already only puts ~$100 (its 20% cap)
into weather at peak; a $2k–$10k book **cannot find enough weather** and is forced into either (a) thin fat-edge
ITF prints ($2–9 deployable each — negligible) or (b) **deep sports (MLB/ATP, $254–495 deployable) that lock
7.5–15 days** — i.e. you buy size by buying lock-up, killing velocity. This is precisely why $10k deploys only
$1,443 (14% of bankroll) and earns +0.43% while $500 earns +1.26%: **scaling past the weather ceiling trades
fast-but-shallow for deep-but-frozen, and returns-on-bankroll fall.**

---

## 5. BLINDSPOTS (what would change with more data)

- **Arrival process measured over <2 days, one sports-heavy weekday (06-10).** No weekday/weekend, seasonal, or
  event-calendar variation. The 62%-top-day concentration may be a small-sample fluke OR the steady state — we
  cannot tell. A multi-week dataset is the only fix (prereg trigger: ≥21 post-epoch event-days, ~07-01).
- **0 econ in window** — the econ velocity/return is purely priors. FOMC settles 06-17, U-3/NFP 07-02; those are
  the first real econ datapoints. Econ is the cleanest-settling but most capital-dead; its absence here flatters
  velocity (no weeks-locked positions yet).
- **Realized/locked split is settle-proxy-driven, not observed.** pmus exposes no settlement timestamp; sports
  finalize ~D+14. Real recycle dynamics (does freed capital actually find a new arb same-day?) are untested —
  the recycle arm measured **$0** on this data (probe #8: skipped pool dies in ~1 s, 0 still-open at settlement).
- **Phantom load (9/15 flat-ladder)** could be deflating OR the real books; if mostly phantom, realistic return
  is closer to the 6-position `--drop-flat` world, materially smaller.
- **Friction is one global scalar.** Per-category / px-based adverse selection (probe #5: weather dear-side-made
  opens 79% toxic) would lower weather's realized edge specifically — the one category the return relies on.

---

## Bottom line for the owner

A live $500 bot, on the best 2 days of data we have, applying the frozen rule, earns **~+1.3% gross / ~+0.85%
after a friction haircut over the whole window**, sits **~70–85% idle**, and gets **most of that from 1–3 fat
ITF-tennis prints on a single day** — 9 of which carry a phantom fingerprint. Returns **fall** as you add capital
(**+1.26% → +0.43% from $500 → $10k**) because the only fast category (weather) walls out at **~$177/day** and
scaling forces you into 7–15-day-locked sports. **This is not a steady, scalable yield at these sizes on this
data** — it is a thin, lumpy, weather-velocity-bound edge that the multi-week confirmatory run (0014) must
either validate or retire. Capital velocity, not edge size, is the binding constraint, and weather is the only
place it is favorable.

**Artifact + reproducer:** this file; `scripts/_data/auditD_h1_driver.py` (+ `auditD_h1_results.json`).
Run: `python scripts/_data/auditD_h1_driver.py`. All four reused scripts' selftests pass.
