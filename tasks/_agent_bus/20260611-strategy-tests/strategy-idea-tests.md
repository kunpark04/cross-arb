# Strategy-idea tests — the 3 rust-review ideas, tested on the cross-arb data

**Date:** 2026-06-11 · **Posture:** READ-ONLY · hypothesis-generation, **NOT validation**.

**What this is.** The rust trading-strategy review (`tasks/_agent_bus/20260611-rust-review/
trading-strategy-review.md`) raised three design ideas (GAP-1/2 + §2 leg-sequencing, the time-of-day
window). This tests all three on the live archive, rigorously and honestly. Watch for **data-dredging**:
3 ideas on ~1 day of data is a spurious-signal hazard, so every result reports **n AND effective-n**
(distinct independent event-days, not episode count) and names its deflator.

**Data basis.** Post-0013 epoch only (`t ≥ 1781082189`, detection-time stamps): **33,697 transitions /
0.87 d** (2026-06-10 09:03 → 2026-06-11 05:53 UTC). The dominant fact, reconfirmed here: the
capturable (≥1¢) candidate arbs span **4 event-dates but 82–88% carry event-date 2026-06-10** — the one
fully-covered day. 06-11 is a ~10 h overnight stub; 06-08/09 are a handful of stale-dated stragglers
logged late. **Effective-n ≈ 1 event-day** for any edge-existence claim; for the weather-specific H1
signal, effective-n ≈ 2 (the only days with joinable weather boundary dynamics).

**Method / reuse ([L20]).** New script `scripts/strategy_idea_tests.py` (offline `--selftest` green) —
it *imports* and never re-implements the proven loaders/economics: `analyze_persistence.load/
build_episodes`, `capital_sim.capturable/one_per_market/settle_t` + the per-contract economics,
`adverse_selection.open_classes/attribute`, `shadow_fill`, `capital_velocity.LOCKUP_PASSIVE`. It does
**not** edit any frozen 0014 script. Numeric dump: `scripts/_data/strategy_idea_tests_postepoch_20260611.json`.

---

## H1 — toxicity-direction / leg-sequencing gate → **SIGNAL (weather-only), with a premise correction**

**Verdict: real, replicated, weather-only — but the prompt's gate DIRECTION is inverted.** The prompt
said "skip **cheap**-led (toxic) arbs." On the data, **cheap-led is the *benign* class; dear-led is the
toxic one** — exactly as the rust-review (§2: "cheap-side-made 18% toxic vs dear-side-made 79%") and
probe §5 stated. My capturable-cohort numbers independently reproduce that split.

**The weather signal (capturable cohort, ≥1¢, post-epoch):**

| at-open class | n | close-toxic (dear_fell) | how it closes | dur median |
|---|---|---|---|---|
| **cheap_made** (buy leg made the edge) | 38 (35 attrib.) | **17%** | cheap_rose 29/35 — laggard mean-reverts (benign) | 1.05 s |
| **dear_made** (sell leg moved away) | 25 (24 attrib.) | **79%** | dear_fell 19/24 — the cheap quote was *right*; you'd miss the dear leg | 1.28 s |

- **Strength, robust to small cells:** two-proportion **z = −4.74**; **Fisher exact two-sided
  p = 2.7e-6**, odds-ratio **0.05** (cheap_made ~20× less likely to close toxic). Survives Bonferroni×6
  (p = 1.6e-5). So this is **not** a small-cell artifact.
- **Weather-only.** Sports: cheap_made 51% vs dear_made 50% toxic, **z = +0.37** (null, n=228/230).
  Overall (all categories pooled) z = −1.27 — the weather signal is diluted to nothing by sports volume,
  which is *why* a blanket skip-filter is null (probe §5 z = −0.68) and must not be built.
- **Duration is NOT the discriminator** (1.05 vs 1.28 s weather; the toxicity is the signal). Fill-
  survival barely differs either (84% vs 88% @150 ms) — meaning the gate's value is **avoiding adverse
  selection on the leg you DO fill**, not improving fill-survival.

**EV impact of gating (weather, close-toxicity = the realized-wrong-leg proxy):**

| weather policy | cohort | close-toxic | realized_med @150 ms |
|---|---|---|---|
| take-all | 105 | 43% | 1.66¢ |
| **skip cheap_made** (prompt-literal) | 67 | **56% ← WORSE** | 1.57¢ |
| **skip dear_made** (corrected) | 80 | **31% ← −12 pp** | 1.62¢ |

So the prompt-literal gate (drop cheap-led) *raises* toxicity to 56% — it throws away the benign class.
The **corrected** gate (drop dear-led) cuts toxicity 43%→31% at ~0¢ realized-edge cost, discarding ~24%
of weather candidates. But per the rust-review this is better expressed as **sequencing, not skipping**:
on a dear-made open you still trade, you just **take the cheap leg first** to dodge the adverse-selection
fill — capturing the toxicity avoidance without forgoing the (still-positive) edge.

- **Effect size:** ΔP(toxic) ≈ **−46 pp** between classes (17% vs 79%) in weather; a skip-dear gate
  realizes ≈ **−12 pp** portfolio toxicity. **n = 59** weather opens (35+24 attributed); **effective-n ≈ 2
  event-days**, ~24 markets / 4–5 cities.
- **What would deflate it:** effective-n = 2 — a single morning's CLI/boundary regime could drive the
  whole signal; weather is intermittent so cells grow slowly. The close-attribution toxicity is a *proxy*
  for "which leg you'd miss," not a measured realized-PnL loss (read-only can't fill). If the weather
  cohort's 06-10 day was atypical, the split could shrink.
- **Worth pre-registering? YES.** This is the project's one usable execution edge and it already has a
  pre-registration trigger (probe §5: confirm at ~1 week, cells n≈150–250). Pre-register it as a
  **per-category (weather) leg-sequencing rule keyed on at-open `led_by`**, with the corrected direction
  (lead the cheap leg; the toxic case is dear-made), and the explicit null-on-sports / null-as-skip-filter
  guards baked in so it isn't re-litigated.

---

## H2 — edge-RATE (velocity-adjusted) allocation → **CAN'T-TELL (null separation on ~1 day)**

> **EXPLORATORY method-demo — NOT the 0014 confirmatory result** (that needs ≥14 event-days + the frozen
> fold protocol; this modifies none of the frozen scripts). Labeled so throughout.

Rank capturable arbs by `booked_edge ÷ expected_lock_days` (**RATE**, frozen priors weather 1.2 d /
sports 15 d / econ 21 d) vs by raw `booked_edge` (**LEVEL**); walk a fixed $500 bankroll with the
account_sim economics. Result across bankrolls ($100/$250/$500/$2000):

| rule | $500 total PnL | deployed | cap-wt lock-days | $/lock-day | funded mix |
|---|---|---|---|---|---|
| fifo (today) | $3.14 | $564 | 15.0 d | 0.21 | 14 sports, 0 weather |
| **level** | **$26.0** | $830 | 11.7 d | 2.23 | 14 sports, 5 weather |
| **rate** | $15.8 | $1051 | **8.4 d** | 1.88 | 16 sports, 5 weather |

- **The two rankings do NOT separate cleanly.** **LEVEL wins raw PnL** (it packs the fat-edge sports);
  **RATE wins velocity** (cap-weighted lock-days 11.7→8.4 d, and at $250 RATE wins $/lock-day 1.82 vs
  1.20). Which "wins" depends entirely on whether you score PnL or capital-turnover, and the bankroll
  **doesn't bind hard enough** on ~1 day to make velocity decisive (most settlements that recycle in-
  window are the stale-dated 06-08/09 stragglers, not a real turnover regime).
- **Both edge-ranked rules beat FIFO** (the bot's current rule) by 5–8× on PnL — but that's the *already-
  known* 0012/0014 result (selection beats arrival-order), not new information about RATE-vs-LEVEL.
- **Category reweighting (the one clear, directionally-sensible effect):** RATE deploys **$500 of $500
  into weather** vs LEVEL's $202 — it correctly front-runs the fast-settling category, matching the
  capital-velocity finding that weather is the only capital-efficient leg. That reweighting is the real
  content; the $/‑day verdict is noise at this n.
- **Effect size:** RATE vs LEVEL = **−39% PnL / −28% cap-wt lock-days** at $500 (sign of the PnL gap is
  stable across bankrolls; magnitude is not). **n = 270** candidates, **effective-n ≈ 1** (81% on 06-10).
- **What would deflate / inflate it:** the lock-day priors are **hardcoded estimates, not measured**
  (0014 audit C3d) — sports could be 7.5 d not 15 d, which would shrink the reweighting. On ~1 day the
  bankroll barely binds, so the velocity benefit RATE is *designed* to capture can't express itself; this
  is structurally un-measurable until multi-day data makes $500 bind across days.
- **Worth pre-registering separately? NO — it already is.** H2 *is* 0014's secondary arm
  (`booked_edge/expected_lock_days`). This run is a method-demo confirming the machinery works and the
  reweighting points the right way; the confirmatory call waits for the frozen ≥14-day protocol. Do not
  read this as that result.

---

## H3 — time-of-day arrival window (13–20Z ≈ 9am–4pm ET) → **NULL / mostly volume, no edge-per-$ gain**

Does restricting to the arrival peak improve capturable-edge-per-$, toxicity, or fill-survival? The
peak reproduces (a 09–20Z daytime hump), but restricting to it does **not** buy a per-$ edge:

| cohort | n | open_net med | edge/$ (cap-wt) | toxic | surv @150 ms / @1 s |
|---|---|---|---|---|---|
| 13–20Z (in) | 287 (65 wx / 222 sp) | 1.68¢ | **1.65¢** | 51% | 87% / 63% |
| outside | 405 (40 wx / 365 sp) | 2.01¢ | **2.22¢** | 53% | 68% / 41% |

- **Edge-per-$ is *lower* in the window** (1.65 vs 2.22¢) — the off-peak (overnight) actually carries
  *fatter* edges. So "restrict to the peak" would shrink edge/$, not grow it.
- **Decomposed by category (rules out the in/out category-mix confound):** there *is* a mild quality
  signal, but it's split and small-n: **weather** in-window has lower toxicity (34% vs 56%, n=62/39);
  **sports** in-window has higher fill-survival (88%/68% vs 67%/40% @150 ms/1 s) but lower open_net. These
  partly track **effective-n** (off-peak spans 4 event-days vs 2 in-window — overnight pulls in the
  06-08/09/11 stragglers), i.e. a calendar artifact as much as a time-of-day one.
- **Net read:** the "peak" is **mostly where the volume is**, not where the quality is. No basis to gate
  on time-of-day; if anything the fatter-but-faster-dying off-peak edges argue *against* a daytime-only
  restriction.
- **Effect size:** edge/$ **−26%** in-window (1.65 vs 2.22¢); toxicity ≈ flat (51 vs 53%). **n = 692**,
  **effective-n ≈ 1–2**.
- **What would deflate it:** the whole split is one weekday's intraday shape with overnight = a different
  day-mix; a clean test needs several full 24 h days so "hour-of-day" isn't entangled with "which
  event-date." **Not worth pre-registering** — no signal pointing the profitable direction.

---

## Bottom line

| Idea | Verdict | Effect size | n / effective-n | Pre-register? |
|---|---|---|---|---|
| **H1** direction/sequencing gate | **SIGNAL (weather-only); prompt's direction inverted** | cheap_made 17% vs dear_made 79% toxic (Fisher p=2.7e-6); corrected skip-dear gate −12 pp portfolio toxicity | 59 weather opens / **eff-n ≈ 2** | **YES** — as a weather leg-sequencing rule (lead the cheap leg), already triggered for ~1-wk confirm |
| **H2** edge-rate allocation | **CAN'T-TELL** (exploratory) | RATE −39% PnL / −28% cap-wt-lock-days vs LEVEL; reweights $202→$500 into weather | 270 cands / **eff-n ≈ 1** | already IS 0014-H2 — no new prereg |
| **H3** time-of-day window | **NULL** (peak = volume, not quality) | edge/$ −26% in-window; toxicity flat | 692 / **eff-n ≈ 1–2** | NO |

**Single most promising:** **H1** — it is the only one that produced a strong, replicated, statistically
robust signal (p ~1e-6), it corrects a real inversion in the stated hypothesis, and it maps onto a
concrete, buildable design (the rust-review's `led_by` leg-sequencer). It is also the only one whose
deflator is "needs more weather-days" rather than "no signal here." The honest caveat that keeps it
hypothesis-grade and not a go: **effective-n ≈ 2** — strong within-sample, but two event-days cannot
distinguish a durable structural edge from one weekday's CLI regime.

**Artifacts:** `scripts/strategy_idea_tests.py` (selftest green) ·
`scripts/_data/strategy_idea_tests_postepoch_20260611.json`.
