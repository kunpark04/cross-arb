---
from: stats-ml-logic-reviewer
run_id: 20260611-readiness
timestamp: 2026-06-11T05:10:00Z
scope_reviewed: Dimension F (statistical validity meta-audit) — what the CURRENT post-0013 dataset can/cannot conclude about a deployable edge; effective n, power, multiple testing, IS/OOS discipline, survivorship, CI on daily return. Verified against analyze_persistence.py, clip_threshold_test.py, capital_sim.py, and the live data mirror.
verdict: partially sound
critical_flags: 3
warn_flags: 5
info_flags: 4
cross_references: [tasks/_agent_bus/20260611-readiness/auditB-execution.md, tasks/_agent_bus/20260610-prereg/stats-ml-logic-reviewer.md, research/allocation-prereg-2026-06-10.md, decisions/0014-preregistered-allocation-rule.md, research/probe-program-2026-06-11.md]
---

# Audit F — Statistical validity (the meta-audit): can the current data support a deploy decision?

## Verdict

**Partially sound — and the soundness is almost entirely in what the project SAYS, not in what the data SUPPORTS.** The project's own discipline is correct and unusual: the prereg (0014), the L18–L21 lessons, the "method demo not validation" labels, and the post-epoch quarantine are exactly right, and the prior prereg audit fixed the right things. But the *binding statistical fact* is blunt: the post-0013-epoch dataset is **19.83 hours (0.826 days) of one mostly-weekday, sports-heavy window** — and that window is effectively **one arrival-day** (06-10 holds 246 of 269 deduped candidate arbs = 91%, and 84% of the total edge mass; 06-11 is a 23-market tail because the pull cut at 04:52 UTC). On a single day's draw you **cannot form a confidence interval on daily realized return at all** — n=1. The honest answer to "does the data support deploying capital?" is **NO, and not by a small margin**: the data can demonstrate *mechanics* (a positive booked gap exists intraday; FIFO is beatable on paper) but cannot establish *existence of a net edge after frictions, its magnitude, its persistence, per-category differences, or the allocation rule's superiority* — every one of those is either single-day-confounded, booked-not-realized, or below the project's own pre-registered power floor. The prereg trigger (≥21 event-days, K≥7) is the correct gate and the data is **~1/21 of the way there**. The single biggest statistical blindspot is that **every headline number in the project is BOOKED edge at entry, not realized PnL** — positions cannot lose by construction (`profit = size·max(0, edge−haircut−void) ≥ 0`), so no settlement outcome, naked-leg loss, CLI revision, or void has *ever* entered any "edge" figure, yet the deploy question is precisely about realized money.

Note on the task framing: the prompt's "62.6k post-0013 records" does not reproduce — I measure **33,568 post-epoch transitions** (the 62.6k likely double-counts the raw + .gz of `transitions-2026-06-10`, which `load()` dedups: `analyze_persistence.py:52-67`). Audit B independently measures the same 33,568 / 19.83 h. Using the inflated record count would itself overstate the evidence base ~1.9×.

---

## 1. What the data CAN and CANNOT support (per claim)

All figures: post-epoch only (t ≥ 1781082189), `capturable()` frozen gate (edge_min=0, liq_floor=1, drop_restart=True), `one_per_market`. Verified by re-running the repo's own loaders.

| Claim | Verdict | Honest basis |
|---|---|---|
| **(a) Positive net edge EXISTS** | **CANNOT conclude.** A positive *booked* gap exists intraday (median capturable open_net 0.49¢, 28 arbs ≥2¢). But "net" here is fee-net only; **friction (naked-leg) is NOT realized** and at the measured 17–29% naked rate × the prereg's own E_loss=5¢ the expected friction is **0.85–1.45¢/contract — up to ~73% of the 2¢ floor** (Audit B: breakeven-u sits *inside* the unmeasured band; pmus-surviving legs ≥5¢ spread floor). So the sign of the *net-of-realized-friction* edge is genuinely undetermined by this data. | n=269 deduped; friction term unmeasured-by-construction |
| **(b) Edge MAGNITUDE** | **CANNOT conclude; the point estimate is a ceiling.** Headline "scalability" ≈ **351¢/day per $1**, but its iid bootstrap 95% CI is **[261, 462]** (±33%) — and that bootstrap is *non-iid-optimistic* (episodes cluster; true width is wider). It is also a *booked* number on a single day. | bootstrap B=10k over n=269 |
| **(c) PERSISTENCE / stability** | **CANNOT conclude — single window.** Durations are measured (median 3s, 19% ≥30s) but stability across days/regimes needs ≥2 independent days; we have ~1. Audit B's split-half regime swing was 16pp @150ms *within* the window. | 1 arrival-day |
| **(d) Per-CATEGORY differences** | **CANNOT conclude; econ is n≈0.** Post-epoch one-per-market: sports 229 / weather 38 / **econ 2**. Any per-category claim is one window; the *cleanest-settlement* category (econ) has **effectively zero** capturable episodes (first datapoint not until FOMC 06-17 / U-3 07-02). Weather vs sports differences are confounded with a sports-heavy single weekday. | sports 229 / wx 38 / econ 2 |
| **(e) ALLOCATION rule superiority** | **CANNOT conclude — method demo only.** Re-running `clip_threshold_test` on post-epoch: OOS "test half" funds **6–8 positions, booked $4–7 on $500**, and both halves are ~10h *within one calendar day* (split cut 17:09 UTC) — not the K-fold independent-days replication the verdict requires. K=0 today vs K≥7 required. | OOS halves 10h vs 10h |

## 2. Independence / clustering — the honest effective n

Records are not the unit. Episodes cluster within a market (re-detections) and within a (category, day). After `one_per_market` dedup and post-epoch filter:

- **Edge-EXISTENCE claim:** the honest unit is the **arrival-day**, because intraday episodes share the same liquidity/regime/news state. Effective n = **1** (06-10 = 91% of candidates; 06-11 is a 10-hour stub, not an independent day). 235 distinct event-clusters across 269 markets means intra-cluster correlation is *mild at the market grain* but irrelevant to the binding constraint, which is **between-day** replication — of which there is one.
- **Per-CATEGORY claim:** weather effective n is **~1 day × ~38 markets clustered in 5 cities** (city-date is the real cluster: largest weather clusters are 2 markets each). econ effective n = **0**. sports n is the only non-trivial cell (229) but is one sports slate on one day.
- **Allocation (fold) claim:** the prereg's own unit is the **3-arrival-day fold**; current span yields **K=0 complete folds**. The fold-level t-interval (the valid primary inference per the prior C1 fix) is undefined below K=2 and distribution-free-impossible at 95% below K=6.

**Bottom line:** for the question "is there an edge," the dataset is a **single observation**, dressed as 33k records and 269 candidates.

## 3. Multiple testing / garden of forking paths

This session ran ~**37 distinct test cells** across the 10 probes (leg-fill latency grid ×6, toxicity subgroups, allocation arms ×5, EV×P(flip) ×4, cluster-cap sweep, recycle cells, maker rest-venue, MLB-EV cells). Two structural points:

- **The allocation/clip headline is protected** *by the prereg* — it is frozen and not re-swept post-data, so it is not at forking-paths risk **provided** the confirmatory run honors 0014 (no re-cut folds, no τ/cap re-tune). This is the explore→confirm pattern done correctly.
- **The weather leg-sequencing subgroup (18% vs 79% toxic, z=4.58, found AFTER an overall null z=−0.68) is the live forking-paths hazard — BUT I must report accurately: the z SURVIVES Bonferroni.** Raw two-sided p = 4.6e-6; even at a conservative m=20 toxicity-subgroup family, p_adj ≈ 9.3e-5, z_crit ≈ 3.02 < 4.58. **So family-wise error is NOT what threatens it.** What threatens it is (i) **tiny cells** (n=24–33), (ii) **found post-hoc after a null** (the textbook subgroup-mining shape, L19), and (iii) **zero out-of-sample replication**. The probe doc already labels it "hypothesis-grade, needs pre-registered confirmation at ~1 week / n≈150–250" — that is the correct posture; the only error would be to *act* on it before that. No fabricated correction-failure here: the statistic is real, the *stability* is unproven.

## 4. Survivorship / selection — fragility to dropping the top observations

The "capturable" cohort conditions on episodes that opened *and* survived the phantom filter — a survivor set. The edge mass rides a fat tail:

- **Full capturable cohort (n=269):** top-1 = **8.1%** of total edge, top-3 = **21.7%**, top-5 = **30.1%**. Moderately concentrated.
- **τ-clearing deployable cohort (n=28):** top-1 = **14.2%**, top-3 = **38.0%**. **Drop the top 3 of the 28 deployable arbs and you lose ~38% of the deployable edge** — on a cohort that is itself one day's draw. This is fragile.
- **Compounding with Audit B's selection finding:** the fat-edge tail that drives the EV *mean* is **disproportionately the part that goes naked** (5–10¢ bucket: 64.6% naked @1s, median life 0.42s). So the realized survivor mean is **below** the booked mean by more than the naked rate alone implies — the headline is mean-driven by exactly the observations least likely to be captured. The single fattest "survivor" (42¢ SFO weather) is **c2=1** (one contract, untradeable). Read every headline as **median-anchored**, never mean.

## 5. The deployment question, statistically: CI width on daily realized return

- There is **no valid CI on daily realized return** because (a) realized return is never computed (booked-only economics), and (b) even the booked daily figure has **n=1 day** of support — you cannot bootstrap a 2-point day sample, and 06-11 is a stub.
- The tightest defensible statement is on the **booked daily-edge proxy**: point **351¢/day per $1**, optimistic-iid 95% CI **[261, 462]**, i.e. **±33% before** you (i) widen for clustering, (ii) subtract realized friction (0.85–1.45¢/contract expected, Audit B), (iii) subtract settlement/void realization (sports/econ recon still OPEN), and (iv) account for single-day regime. After those, the **honest bound straddles zero**: the same data is consistent with *both* "a solid intraday gross gap" **and** "≤0 net after frictions and settlement," because the terms that would distinguish them are unmeasured. **This is the crux: the CI on the decision-relevant quantity is so wide it spans the sign.**

## 6. Sample size / span needed to conclude a deployable edge at reasonable power

The prereg already specifies the right gate; the data is far short of it:

- **Fold dispersion:** K ≥ 7 (the prior audit's arithmetic: distribution-free 95% impossible below K=6; t-interval needs K≥7 for CV≤1). **K=0 today.** → **≥ 21 post-epoch arrival-days** (≈ 2026-07-01+), exactly the 0014 trigger.
- **Candidate floor:** ≥30 τ-clearing one-per-market in the *folded* span. **Currently 28 over the whole post-epoch span — already below floor**, though at ~34 τ-clearing/day the 21-day span clears the *count* easily; the **binding limit is K (between-day dispersion) and the per-MARKET ≤33% share gate**, not raw candidate count.
- **Per-category:** econ needs ≥1 real release in-window (FOMC 06-17, U-3/NFP 07-02) before *any* econ execution/edge claim — currently n≈0.
- **The decisive extra requirement beyond the prereg:** the prereg confirms *booked-edge allocation efficiency*, NOT realized money. A genuine deploy decision additionally needs (a) `settle_recon` realized-outcome reconciliation for sports + econ (open until 06-23/07-03), and (b) **a measurement of naked-unwind cost `u`** — which Audit B shows is structurally unmeasurable read-only and is the single term that flips the EV sign. **No span of read-only data answers (b).**

## Impact on project scope

- **If the (booked) result stands at 21 days:** it licenses "allocation rule beats FIFO on booked edge" — an efficiency claim — and nothing about realized capital. The deploy gate must remain *downstream* of settle_recon + a live `u` measurement.
- **If acted on now (1 day):** premature on every axis. Any "$/day," "weather doubly efficient," per-category ranking, or "+9–141% allocation" number quoted as a *result* (vs method demo) would repeat L18/L19 one level up — the single-day, booked-not-realized, fat-tail-driven trifecta the project's own lessons exist to prevent.
- **Consistency with prior findings:** **confirms** the project's stated posture (CLAUDE.md "measurement rig, not a go"; 0014; L19's "method demo not validation"). It **complicates** any reading of the probe-program verdicts as decision-grade — they are direction-grade at ≤1.8 days, which §9 of that doc says explicitly. Nothing here contradicts the engineering; it contradicts only an over-eager reading of the magnitudes.

## Recommended alternatives (ranked by information value)

1. **Do not deploy; execute the 0014 prereg unchanged at ≥21 event-days.** Answers: does the allocation efficiency replicate across independent days with valid fold-level inference. Better than anything possible now because it is the *only* design that gives between-day dispersion (the missing axis). Decision rule: the prereg's §4 gates as written.
2. **Gate capital on a realized-`u` measurement, not on booked EV (Audit B's ask).** Answers: is the edge net-positive after the one term that flips its sign. Better because booked-edge can never answer it. Decision rule: shadow/IOC naked-unwind clears < breakeven-u (6.7¢ @261ms) on the *pmus-surviving* leg, not just Kalshi. Requires post-greenlight (cannot be read-only) — so it is a *blocking* gate, not a data-accumulation gate.
3. **Report all headline edge/return numbers as median-anchored with the top-3-dropped sensitivity shown inline.** Answers: how much of the claim is 3 observations. Better than the mean-driven headline (which leans on the most adversely-selected tail). Decision rule: if dropping top-3 of the deployable cohort moves the verdict, the verdict is not robust (today it moves it ~38%).
4. **Pre-register the weather leg-sequencing hypothesis NOW (before the confirmation data), separately from 0014.** Answers: is the z=4.58 signal real or a post-null subgroup artifact. Better because it converts an exploratory hit into a confirmable bit before the cells grow. Decision rule: replicate sign + |z|>2 on the held-out week at n≈150–250 with the split frozen in advance.
5. **When the 7-day descriptive peek runs, report effective-n (day-blocks) and per-day candidate concentration explicitly**, so a single fat day cannot masquerade as a week. Decision rule: if any one arrival-day holds >50% of candidates, the "week" is one day — say so.

## Severity flags

- **CRITICAL-1 — Booked ≠ realized, project-wide.** Every "edge"/"$/day"/allocation number is `profit = size·max(0,·) ≥ 0` (`capital_sim.py` book-avg model; `alloc_policy_experiment` accounting): positions cannot lose. The deploy question is about realized money; the data contains none. Any capital decision on these numbers is unsupported. *(Evidence: capital_sim.py:18-20, 90; analyze_persistence headline is open_net at entry.)*
- **CRITICAL-2 — Effective n = 1 day for edge existence.** 06-10 = 91% of deduped candidates / 84% of edge mass; 06-11 is a 10h stub. No CI on daily return is formable; the booked-proxy CI [261,462]¢/day already spans ±33% and widens past the sign once friction/settlement/clustering enter. *(Evidence: day-level decomposition, this audit §2/§5.)*
- **CRITICAL-3 — Below the project's own power floor; K=0 folds.** 28 τ-clearing < 30 floor; K=0 < K≥7 required; econ n≈0. The dataset is ~1/21 of the prereg trigger. Acting now violates 0014. *(Evidence: this audit §6; prereg §2.)*
- **WARN-1 — Survivor fat tail.** Deployable cohort (n=28) top-3 = 38% of edge; full cohort top-5 = 30%. Headlines are mean-driven by the most adversely-selected observations (Audit B). Read median-anchored.
- **WARN-2 — Record-count inflation in the task framing.** "62.6k post-epoch" does not reproduce (actual 33,568; `load()` dedups raw+gz). Downstream audits leaning on 62.6k overstate the base ~1.9×.
- **WARN-3 — OOS split is intra-day.** `clip_threshold_test` "OOS" halves are ~10h vs ~10h within 06-10; this is not out-of-sample in the days sense the deploy claim needs. It is a method demo, as the script's own caveat says.
- **WARN-4 — Per-category confound.** Weather-vs-sports differences are entangled with one sports-heavy weekday; econ absent. No category ranking is supportable yet.
- **WARN-5 — Settlement realization OPEN for sports + econ.** A both-legs-loss void tail (MLB postpone) and econ release outcomes are not in any number; recon finalizes 06-23/07-03. Caps net EV regardless of the booked figures.
- **INFO-1 — The z=4.58 subgroup SURVIVES Bonferroni** (p_adj≈9.3e-5 at m=20). Its risk is small-cell/post-null instability + no replication, NOT family-wise error. Do not mislabel it as a multiple-testing failure; do not act on it pre-confirmation. *(Verified, not asserted.)*
- **INFO-2 — The project's discipline is correct.** 0014, L18–L21, the post-epoch quarantine, "method demo not validation" labels, and the prior prereg audit are all sound and rare. The gap is data volume, not methodology.
- **INFO-3 — Phantom filter is working post-epoch.** 222 restart-censored excluded; both Audit B (n=688 all `censored=none`) and this cohort are phantom-clean. The L20 chokepoint fix held.
- **INFO-4 — Direction-grade ≠ decision-grade.** The probe-program verdicts are explicitly ≤1.8-day directional reads; treat them as hypotheses with upgrade triggers, exactly as that doc states.
