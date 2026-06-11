# Adversarial Readiness Audit — Dimension A: EDGE REALITY

**Verdict (≤15 lines)**
- **Is there a real, persistent, positive NET edge? → CAN'T-TELL, leaning "tiny-but-nonzero".** A small clean edge survives every gate, but it is too few episodes on too little time to call persistent.
- **Survived-all-filters:** **16 episodes (15 unique markets), ΣΣ 39.1¢ open-edge, max 7.03¢** at (net≥1¢, lasts≥30s, depth c2≥10, fresh≤10s both sides, non-flat-ladder, non-restart). Hand-px-adjudicated down to **~11 genuinely-clean (~28¢)** after dropping wide/illiquid books. Canonical `capital_sim` clean gate → **34 markets, ~$118/day gross on ~$9.7k peak (~1.2%/day)** — gross, no leg-fill haircut.
- **Phantom share:** of 5,804 measured episodes / 4,361¢ apparent edge, **~71% of edge-¢ sits in flagged artifacts** (flat-ladder 36%, thin-c2<10 31%, stale-age>10s 25%; overlapping). **Of the top-20 fattest episodes, 19/20 are artifacts** (flat single-contract book-init, stale 530s quotes, internally-broken/illiquid books). The fattest "edges" (42.6¢, 42.2¢, 37.7¢) are ALL flat/thin phantoms.
- **Persistence:** of capturable ≥1¢ episodes (n=799), **median duration 1.0s; 70% of ≥1¢ edge-¢ dies <5s, 50% sub-second; only 13% is in persistent ≥30s episodes.** Most apparent edge is un-fillable flicker (corroborates L18/L22).
- **Per-category (clean capturable):** **sports 11 eps / 28.7¢** (deep — incl. the MLB min-det c2=12,249 line-lag case, the L16 depth-AND-edge proof); **weather 5 eps / 10.4¢** (thin, median c2≈4); **econ 0 eps / 0¢** — post-0013 quarantine, econ edge is GONE (4 measured eps, max 0.49¢; the old 12.2¢ "U-3 edge" was the L21 phantom).
- **Top blindspots:** (1) **ALL 16 clean arbs are on ONE event-date (06-10)** — the only 24/24h-covered day; 06-11 (5h overnight) yielded zero → severe survivorship/sample. (2) <2 days, ~47h wall-clock, 1 weekday, no multi-day regime. (3) c2 is *displayed* depth, never pinged — true fillable size unknown; per-leg Kalshi depth not in records. (4) No fills executed → leg-fill survival, slippage, adverse selection unmodeled here.
- **Artifact path:** `scripts/_data/auditA_edge_funnel.json` (funnel + top-20 + clean-capturable episodes with px).

---

## Method (reused shared loaders, edited no code)

- `analyze_persistence.load()` + `build_episodes()` + `capital_sim.capturable()` exactly as shipped. Selftest passes.
- **Dedup verified (L20 / prompt flag):** 06-10 has both `.jsonl.gz` (55,835 recs) AND raw `.jsonl` (57,909 recs). Loader's `chosen` dict correctly picks the **raw** per event-date token; the `.gz` is shadowed → **no double-count**. Used raw-line total 60,399 − 36 quarantined econ = 60,363 loaded ✓.
- Quarantine fired: 36 pre-remap threshold-econ records dropped (0013).
- Coverage: 60,363 transitions, 354 markets, 85 restarts, span 47.4h (1.98d). Post-epoch (t≥1781082189) = 33,568 recs / 19.8h / detection-time ms+px+depth — funnel shape **identical** to all-data, so the result is driven by clean post-epoch data, not pre-epoch lag artifacts.

## Task 1 — NET-edge funnel (all data; baseline shown alongside per L15)

| stage | n | ΣEdge | median | p90 | max |
|---|---:|---:|---:|---:|---:|
| 0b. measured (clean+eod) **[baseline]** | 5,804 | 4,361¢ | 0.39¢ | 1.22¢ | 42.63¢ |
| 1. + net>0, c2≥1, drop-restart | 4,061 | 3,698¢ | 0.47¢ | 1.79¢ | 42.63¢ |
| 2. + fresh both sides (age≤10s) | 2,750 | 2,802¢ | 0.47¢ | 2.11¢ | 42.63¢ |
| 3. + depth floor (c2≥10) | 2,159 | 2,302¢ | 0.49¢ | 2.27¢ | 37.65¢ |
| 4. + drop flat-ladder (L20) | 1,638 | 1,269¢ | 0.46¢ | 1.43¢ | 22.18¢ |
| 5. + edge≥1¢ + lasts≥30s **(TRADEABLE)** | **16** | **39.1¢** | 2.14¢ | 3.63¢ | **7.03¢** |

- Each gate roughly halves surviving edge-¢; **stage 3→4 (flat-ladder drop) alone removes ~45% of edge-¢** and pulls the max from 37.7¢→22.2¢. The fat tail is phantom.
- Restart-censored = 281 eps (4.6%), already excluded from "measured".
- Post-epoch-only mirrors this: measured 5,183 → tradeable 14 (35.9¢, max 7.03¢).

## Task 2 — Phantom hunt (top-20 fattest, raw-record inspected with px)

- **19 of 20 are artifacts.** Top 3: `aec-mlb-lad-pit` 42.63¢ (FLAT, c2=**1**, 0s flicker), `tc-temp-sfohigh-…-lt82f` 42.18¢ (FLAT, c2=1, pmus YES bid 0.53 vs Kalshi 0.09 — venues disagree 44¢ = L2/L21 not arb), `aec-mlb-cin-sd` 37.65¢ (FLAT; Kalshi book internally broken ka=0.02/kb=0.99). Recurrent tells: flat c2==c1==c0 ladder, single-contract depth, fresh-subscribe (ageK=0) book-init, 530s-stale quotes, both-sides-wide illiquid books.
- **Disciplined phantom share (flat | c2<10 | max-age>10s): 4,166 eps (72%) = 3,093¢ (71%) of edge-¢.** Of ≥2¢ episodes, **96% of edge-¢ is flagged**.
- NOTE on decode: for **sports**, `px.ka`/`px.kb` are the YES-asks of **two separate Kalshi single-team markets** (not one book's bid/ask) — so "Kalshi crossed/wide" is meaningless there; pmus is the only 2-sided sports book. Verified against `bot/monitor.py:157-225` (`game_edge`/`GameTracker`). The monitor already guards crossed-pmus + >40¢ orientation mismatch.

## Task 3 — Persistence (capturable ≥1¢, n=799)

| duration | eps | %eps | edge-¢ | %edge |
|---|---:|---:|---:|---:|
| <1s (sub-second flicker) | 396 | 50% | 1,393 | 56% |
| 1–5s | 119 | 15% | 336 | 14% |
| 5–30s | 171 | 21% | 410 | 17% |
| 30–60s | 35 | 4% | 106 | 4% |
| 60–300s | 63 | 8% | 174 | 7% |
| ≥300s | 15 | 2% | 54 | 2% |

- **Median 1.0s. Persistent (≥30s) = 13% of ≥1¢ edge-¢; flicker (<5s) = 70%.** The tradeable window exists for a small minority; the bulk is flicker you cannot two-leg fill.

## Task 4 — By category

| cat | measured eps | medEdge | maxEdge | medC2 | clean-capturable eps | clean ΣEdge | capturable rate |
|---|---:|---:|---:|---:|---:|---:|---:|
| sports | 4,565 | 0.40¢ | 42.63¢ | 50 | 11 | 28.7¢ | 0.2% |
| weather | 1,235 | 0.28¢ | 42.18¢ | 4 | 5 | 10.4¢ | 0.4% |
| econ | 4 | 0.05¢ | 0.49¢ | 136 | 0 | 0.0¢ | 0.0% |

- **Sports** holds the depth (median c2=50, max 22,743; the only depth-AND-edge cases, e.g. MLB min-det c2=12,249/228s/7¢). **Weather** is clean-but-thin. **Econ is empty post-quarantine** — the structurally-cleanest category currently shows no edge.
- Concentration in clean cohort: top-1 market 18%, top-3 40% of edge — moderately diversified across MLB + ITF-tennis + 4 weather buckets, but all 06-10.

## Task 5 — BLINDSPOTS (what this data CANNOT tell us)

1. **Single-day survivorship (DOMINANT).** All 16 clean arbs occur on event-date 06-10, the only 24/24h-covered day. 06-09 = 19/24h (98-min gap); 06-11 = 5h overnight, 0 clean arbs. *Resolves with:* multi-week continuous collection (the gated redeploy).
2. **Tiny sample.** ~47h wall-clock, 1 active weekday. 16 clean episodes cannot establish *persistence* (vs lucky cluster). *Resolves with:* weeks of data + bootstrap/temporal-split (L19 discipline).
3. **Displayed-depth ≠ fillable.** c2 = displayed depth at gross≥2¢, never pinged; per-leg Kalshi depth absent from records (only the combined c2 pair). True clip size unknown. *Resolves with:* live taker probes / small real fills.
4. **No execution reality.** Leg-fill survival, slippage, adverse selection, settlement-void are not in *this* edge measurement (sibling probes: shadow_fill ~17–29% naked <250ms; settle_recon weather 360/360). Gross $118/day has no fill haircut.
5. **Regime.** One weekend-adjacent window; no FOMC/NFP print day, no high-vol weather event, no full sports week. *Resolves with:* coverage spanning event days.
6. **px present only post-epoch.** Pre-epoch fat episodes (e.g. `aec-mlb-sea-bal` 2.01¢) can't be book-adjudicated; relied on flat/thin/stale flags there.

**Bottom line for the capital decision:** the honest, artifact-free edge over the best-covered day is ~11–16 clean episodes totaling ~28–39¢, ~1%/day gross *before* the leg-fill tax that kills 70% of raw flow — i.e., a *plausible* small edge concentrated in deep sports line-lag, **not yet proven persistent**. Deploying capital now would be acting on one day of data; the gating need is weeks of multi-day coverage, not more capital.
