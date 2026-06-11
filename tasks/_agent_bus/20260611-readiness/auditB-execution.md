# Adversarial Readiness Audit — Dimension B: EXECUTION VIABILITY

**Question:** can the two-leg arb actually be EXECUTED profitably at the measured latency, and what are the execution blindspots?
**Verdict:** **DEPENDS ON `u` (naked-unwind cost), and the go/no-go sits inside the unmeasured band.** Taker execution is *not refuted* at the measured RTT, but it is *not proven safe* either — the single term that decides it (`u`) is the one term these logs structurally cannot measure, and the indirect floor evidence (touch spreads) puts `u` near or above breakeven on the pmus side.

Basis: post-0013 epoch, **19.83 h**, 33,568 detection-time-stamped records, **n=688 capturable ≥1¢ episodes** (584 sports / 104 weather / **0 econ**), all `censored=none` (phantom filter clean). Measured live RTT this session: Kalshi p50 70 ms / pmus p50 78 ms. Scripts reused unmodified: `shadow_fill.py --post-epoch`, `ev_at_latency.py`, `latency_probe.py`. Dumps: `scripts/_data/auditB_shadow_fill.json`, `scripts/_data/auditB_ev_at_latency.json`.

---

## TASK 1 — Shadow-fill + EV on ALL post-epoch capturable ≥1¢ episodes

### Baseline cohort (n=688) — per-attempt per-contract EV

| L | P(both fill) | naked-leg % | survivor edge mean / median (¢) | EV u=1¢ | EV u=2¢ | EV u=3¢ | breakeven-u (mean) |
|--:|--:|--:|--:|--:|--:|--:|--:|
| **86 ms** | 84.3% | **15.7%** | 3.19 / 2.01 | +2.53 | +2.37 | +2.22 | 17.1¢ |
| **150 ms** | 76.0% | **24.0%** | 3.17 / 1.90 | +2.17 | +1.93 | +1.69 | 10.0¢ |
| **261 ms** | 68.0% | **32.0%** | 3.15 / 1.82 | +1.82 | +1.50 | +1.18 | 6.7¢ |
| **1000 ms** | 50.1% | **49.9%** | 3.05 / 1.95 | +1.03 | +0.53 | +0.03 | 3.1¢ |

n = 688 per cell. **All EV cells are positive across u∈{1,2,3}¢ at L≤261 ms.** At L=1 s, EV(u=3¢) is essentially zero (+0.03¢) — the 1 s regime is the knife-edge.

> ⚠ This sample runs ~1 pp WORSE than the 06-11 brief at the fast end (naked 15.7%/24.0% @86/150 ms here vs 9.4%/17.0%/23.3% @50/100/150 ms in the brief). The grid points differ (86 vs 50/100), and this is a larger, longer window — but the direction is "no improvement on re-pull," which is the adversarially relevant sign.

### Realized survivor edge & duration (the decision-relevant detail)
- Survivor **median** edge holds ~1.8–2.0¢ at every L; survivor **mean** ~3.1¢ — mean > median at every L because a fat-edge tail drives it (top-3 survivors @150 ms = 6.5% of all survivor edge). **The EV mean leans on the fat tail — see Task 3, that tail is exactly what goes naked.**
- **Duration distribution of the cohort: 30.7% of capturable ≥1¢ episodes die <0.25 s, 42.3% <0.5 s, 49.7% <1 s.** Half the "capturable" universe is already gone inside one second. These are not raceable — you never get the second leg, regardless of language/colo.

### The L18 deeper/persistent subset, shown alongside
- **dur≥30 s AND c2≥100 (n=61): 100% survival, 0% naked at EVERY latency incl. 10 s.** Reads great — but this is **outcome-conditioned selection, not a strategy**: you cannot know an episode will last ≥30 s at the instant you must decide to enter. Quoting it as the execution number is the L18 trap (conditioning on the future).
- **Ex-ante-observable depth lens — c2≥100 only (n=385, no duration condition):** naked **21.0% @150 ms**, 46.0% @1 s. Depth you *can* see at open buys only ~3 pp of survival vs baseline (24.0%→21.0%). **Depth does not rescue the race.**

### Dollarized / H1 account (context, not the headline — one window, sports-heavy)
- $500 H1 frozen rule funds 11 positions; @150 ms 6/11 fill both legs → realized +$18.79 gross; NET after naked tax: **+$17.09 (u=1¢) / +$15.39 (u=2¢) / +$13.69 (u=3¢)** over the 19.8 h window. 5/11 went naked. Positive but n=11, one window, paper-gross, settlement-recon still open.

---

## TASK 2 — The DECISIVE unmeasured term: naked-leg UNWIND cost `u`

**What `u` is:** when one leg fills and the edge dies before the second lands, you hold a naked directional position. `u` = the cost to flatten it — re-cross the bid/ask spread on the surviving leg's venue **plus** any adverse price move since the fill **plus** that venue's taker fee. It is a *per-contract loss*, applied to the full naked size.

**Why we cannot measure it from current logs (structural, not fixable by re-pull of the same schema):**
1. The transition log records `px` (touch prices) only **at transition events**, and a full 2-sided book is present on just **15% of records** (4,966/33,568 Kalshi, 5,073 pmus). Unwinding happens *between* transitions, against a book we did not log at that instant — there is no replayable continuous order book to walk an unwind against.
2. The naked-unwind is a **counterfactual order** (you'd be the aggressor crossing the spread on a book that moved *because* the edge died). Nothing read-only reconstructs the price a market order would actually clear at post-event.

**What WOULD measure it:** (a) the `ladders-*.jsonl` top-5 depth on both venues (already being logged post-redeploy, but weather-only and transition-triggered) walked against a simulated market-order at `open_t + L + δ`; or decisively (b) **shadow market orders** — submit-and-immediately-cancel or tiny live IOC orders on the surviving leg right after a simulated leg-fail, to observe the real clear price. (b) needs the trade engine + (arguably) capital, so it is genuinely a post-greenlight measurement.

**Indirect floor from the px we DO have — and it is NOT reassuring:** the logged touch *spread* is a hard lower bound on a taker re-cross (you pay at least half-spread each way; realistically the full spread to flatten immediately):

| venue (surviving leg) | touch-spread median | p75 | p90 |
|---|--:|--:|--:|
| Kalshi | **3.0¢** | 4.0¢ | 6.0¢ |
| pmus | **5.0¢** | 9.0¢ | **16.0¢** |

**Sensitivity of go/no-go to `u`:**
- Breakeven-u (cohort mean) = **10.0¢ @150 ms, 6.7¢ @261 ms, 3.1¢ @1 s**.
- If the surviving leg is **Kalshi** (median spread 3¢): comfortably below the 150/261 ms breakeven → survives.
- If the surviving leg is **pmus** (median spread 5¢, p90 16¢): a 5¢ floor is **half the 150 ms breakeven but ABOVE the 1 s breakeven (3.1¢)**, and on the p90 pmus book (16¢) a naked unwind is a net loss at **every** latency. The "1–3¢ plausible u" assumption baked into the prior briefs is **optimistic for pmus-surviving naked legs** — the spread floor alone is 5¢ before any adverse move or fee.
- **Conclusion on Task 2:** the breakeven cushion is real at ≤261 ms *if* you can guarantee the cheap (Kalshi) leg is the one that survives. You cannot — which leg survives is set by which leg fills first and which side moved (Task 3 + adverse selection). **The go/no-go is genuinely inside the unmeasured band; this is the #1 thing to measure before capital, and it cannot be measured read-only.**

---

## TASK 3 — Adverse fill timing: does naked risk cluster where the edge is FATTEST? (SELECTION)

**Yes — and this is the audit's sharpest adversarial finding.** Naked-leg rate by entry-edge size bucket:

| open edge bucket | n | naked% @150 ms | naked% @1 s | median duration |
|---|--:|--:|--:|--:|
| 1–2¢ | 361 | 22.2% | 45.2% | 1.61 s |
| 2–3¢ | 131 | 26.7% | 52.7% | 0.63 s |
| 3–5¢ | 96 | 22.9% | 51.0% | 1.00 s |
| **5–10¢** | 65 | **29.2%** | **64.6%** | **0.42 s** |
| 10¢+ | 35 | 25.7% | 57.1% | 0.55 s |

**The fattest edges die fastest.** The 5–10¢ bucket goes naked 64.6% at 1 s vs 45.2% for thin 1–2¢ edges, and its median life is 0.42 s vs 1.61 s. Mechanism: a fat cross-venue gap usually means one quote is *informed/stale* and about to be corrected — so the fat edge is both the most attractive and the most adversely-selected. The EV table's positive mean is driven by a fat-edge tail (top-3 = 6.5% of survivor edge) that is **disproportionately the part you fail to capture** — so the realized mean is below the shadow mean by more than the naked-rate alone implies. **By time/category:** the cohort is 81% sports (4,484/5,528 OPENs sports, 1,015 weather, 29 econ) over one ~16 h sports-heavy window; weather naked@150 ms (16.3%) is milder than sports (25.3%) but weather realized edge degrades harder with L (median 1.18¢ @1 s vs sports 2.01¢). No clean intraday clustering measurable in one window.

**Compounding phantom check:** the single fattest "survivor" (42¢ SFO-weather @150 ms) is **c2=1** — one contract, untradeable size. Only 7.8% of survivor-edge sits at c2≤2, and ≥5¢ survivors have median c2=418 (real depth), so EV isn't *pure* phantom — but the headline mean is sensitive to a handful of thin outliers and should be read as median-anchored.

---

## TASK 4 — Latency realism: is 86–261 ms right for a TWO-leg SEQUENTIAL execution?

**No — 86–261 ms is the CONCURRENT (fire-both-at-once) read-path floor. It understates a hedged execution.** Live this session:

| | p50 | p99 |
|---|--:|--:|
| CONCURRENT two-leg (fire both, wait slower ack) | **78 ms** | 191 ms |
| **SERIAL two-leg (fire A → ack → fire B)** | **148 ms** | **339 ms** |

- The shadow model bakes in the **concurrent** assumption (one latency `L` to land *both* legs). A genuine cross-venue arb that confirms leg A before committing leg B — the prudent hedge sequence that avoids doubling your naked exposure — lives at the **serial floor (~150 ms p50, 339 ms p99)**, i.e. the 150 ms / "between 261 ms and 1 s" rows, where naked is 24–32%+. Firing concurrently halves the floor but means **both** orders are live before either confirms → if one rejects you are instantly naked the other (Task 5).
- **The order path adds latency the read-path RTT does NOT capture (named blindspot):** read-path = unauthenticated GET. The order path adds (1) request **signing** (Ed25519 for pmus, RSA-PSS for Kalshi) — compute, but also (2) **matching-engine accept + risk-check queue** server-side, (3) the **unverified pmus POST round-trip** (we have never timed an authenticated pmus order — only public reads), and (4) any **rate-limit / token-bucket** delay under burst. None of these are in the 78–148 ms number. The realistic per-leg order latency is **≥ the read RTT, plausibly materially above it on the write path** — pushing effective `L` toward the 261 ms / 1 s rows where the EV cushion thins to +1.2¢ / breakeven.

**Net Task 4:** the relevant `L` for a hedged two-leg fill is **≥150 ms (serial p50), with a 339 ms p99 and an unmeasured write-path premium on top** — not the rosy 86 ms concurrent figure. Re-reading the EV table at L=261 ms (breakeven-u 6.7¢) is the honest baseline, not L=86 ms.

---

## TASK 5 — BLINDSPOTS (execution unknowns NOT in the shadow model, with likely EV sign)

| # | Blindspot | In shadow model? | Likely EV sign | Note |
|---|---|---|---|---|
| 1 | **Naked-unwind cost `u`** | No (priced as a labelled assumption) | **−, decisive** | The go/no-go term; pmus re-cross spread floor 5¢ (p90 16¢) ≥ part of breakeven. Unmeasurable read-only. |
| 2 | **Partial fills** | No — assumes full fill at touch net for the entire logged depth | **−** | Real fills clear part of the clip at touch then walk the ladder; you may get the cheap leg partially and the dear leg fully → mini-naked on the residual. Edge decays down the ladder; touch-net × full-depth overstates. |
| 3 | **Queue position (maker legs)** | No — taker-only shadow; no time-priority model | **−** | The brief's maker config (rest-Kalshi) lives or dies on queue-ahead, entirely unmodeled here. For taker legs, n/a. |
| 4 | **Order rejection** | No | **−** | A rejected leg (price moved past limit, self-match, risk-check) = instant naked on the *filled* leg. Concurrent firing makes this strictly worse than serial. |
| 5 | **Venue rate limits / throttling** | No | **−** | Burst of simultaneous opens (this is event-driven, bursty) can queue or 429 your orders → added latency exactly when many edges fire at once. |
| 6 | **Unverified pmus auth POST** | No — only public reads ever timed | **− (latency), unknown (reliability)** | Never placed/timed an authenticated pmus order. Write-path latency, signing correctness, and order-ack semantics are all unverified. |
| 7 | **Adverse selection on the surviving leg** | No (FLIP-before-fill counted as fail, but the *informed* dear leg moving away is not priced) | **−** | When the cheap leg was the informed one, you fill cheap and the dear leg runs → you're naked the *worse* side, raising realized `u`. The 06-11 adverse-selection probe found weather cheap-made opens 18% toxic vs dear-made 79% (z=4.58) — a real, unpriced directional hazard. |
| 8 | **Settlement-identity recon (sports/econ) still OPEN** | No (assumes clean settlement) | **− tail** | Weather confirmed 360/360; sports/econ rules-verified only, finalize 06-23/07-03. A both-legs-loss void tail (MLB postpone) is not in the fill model — separate dimension but it caps net EV. |
| 9 | **econ entirely unmeasured for execution** | n=0 capturable econ in window | **unknown** | The *cleanest-settlement* category has **zero** capturable execution episodes here. Any econ execution claim is unsupported by data; first datapoint not until FOMC 06-17/U-3 07-02. |

**Every named blindspot has a negative or unknown EV sign.** The shadow model is optimistic on *every* axis except one (it conservatively prices all leg-fails as full one-leg −u rather than the cheaper neither-leg $0). On net the realized edge is **below** the shadow EV, and the margin is set almost entirely by `u`.

---

## BOTTOM LINE

- **Does execution survive at measured latency? DEPENDS-ON-u.** At the *honest* hedged latency (serial p50 150 ms / p99 339 ms, + unmeasured write-path premium), EV is positive for u≤~6¢ **if the surviving leg is Kalshi**. It is **not** robustly positive if the pmus leg survives (5¢ spread floor, 16¢ p90) or at the 1 s regime half the cohort lives in.
- **Decisive unmeasured term:** naked-unwind cost `u`. Breakeven 10.0¢/6.7¢/3.1¢ @150 ms/261 ms/1 s; plausible actual 1–3¢ on Kalshi but **≥5¢ on pmus by spread floor alone**. Cannot be measured read-only — needs shadow/IOC orders post-greenlight.
- **Top execution blindspots:** (1) `u` itself; (2) serial-not-concurrent latency + unverified pmus write-path; (3) selection — the fattest edges go naked most (5–10¢ bucket 64.6% naked @1 s, dies in 0.42 s); (4) partial fills + rejection → residual naked exposure; (5) econ execution entirely unmeasured.
- **Recommendation:** do NOT treat the +1.5–2.4¢/contract EV as bankable. The number that flips it is `u`, it is unmeasured, the indirect evidence puts it near breakeven on the pmus side, and the fat-edge tail driving the mean is the part most adversely selected. Execution is a **measure-`u`-first gate**, not a go.

**Artifacts:** `scripts/_data/auditB_shadow_fill.json`, `scripts/_data/auditB_ev_at_latency.json`.
