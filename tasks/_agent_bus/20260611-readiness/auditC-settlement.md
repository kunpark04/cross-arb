# Adversarial Readiness Audit — Dimension C: SETTLEMENT-IDENTITY COMPLETENESS (invariant #1)

**Date:** 2026-06-11 · **Scope:** the catastrophic-risk axis. If both venues don't grade off the same
number, a "locked" pair loses BOTH legs. Per category: what is empirically VERIFIED vs rules-text-only
vs OPEN, and every both-legs-loss tail. READ-ONLY (public-API reads + offline scripts; no code edited).

**Evidence run this session (all reproducible):**
- `settle_recon.py --no-sports --days-back 12` (live, 2026-06-11 ~00:00Z) → **360/360 weather buckets
  identical** (dump: `scripts/_data/settle_recon_weather_20260611-000044.json`).
- Independent raw NWS CLI pull (newest version) MIA=88 / MDW=91 / SFO=79 for 06-10 → all match `cli.jsonl`
  + the recon's `kal_val`. (Recon's own NWS-walk anchored 06-01→06-07 too: every `kal_val`==CLI.)
- Live Kalshi rules text per econ family (`strike_type` + `rules_primary`) + live pmus "at least" text.
- `colisted_map.py --live` + `build_colisted_map()` programmatic enumeration of what would trade TODAY.
- Live droplet archive inspection: `transitions-2026-07-02.jsonl` (econ phantom pre/post redeploy);
  `analyze_persistence.load()` quarantine fires (36 records dropped).

---

## TASK 1 — Per-category settlement-identity status table

Legend: **VERIFIED** = empirical (settled outcomes reconciled, n stated) · **RULES** = primary rules
text confirmed but no settled-outcome reconciliation yet · **OPEN** = neither.

### WEATHER (5 cities: LAX/MDW/MIA/NYC/SFO)

| Axis | Status | Evidence (n) |
|---|---|---|
| Settlement-source identity | **VERIFIED** | Both = "NWS Climatological Report (Daily)", same station incl. NYC=Central Park; `verify_settlement.py` rules pull (5/5 cities) + 3-way recon anchored to independent NWS CLI 330/330 |
| Bucket-grid / inequality (L21) | **VERIFIED** | `colisted_map` canonicalizes BOTH venues to inclusive `[lo,hi]`; middles `[floor,cap]`, tails `floor+1`/`cap−1`; pm `gteXltY=[X,Y]`. Boundary-equality guard enforced, **0 misaligned** live today |
| Settled-outcome reconciliation | **VERIFIED n=360** | This session: 360/360 identical, 0 divergence, 0 CLI mismatch (60 city-days 05-29→06-09). Incl. the one real revision day MDW 06-09 87→88 → both settled 88 |
| Void/tail divergence | **narrow residual** | Only tail = **downward 8–11 AM CLI correction on a boundary day** (Kalshi waits for final, pmus locks 8 AM, pmus 11 AM path triggers on CLI≠METAR not a downward correction). **0 downward revisions in 14 station-days** (rate logged live, `cli_revisions.py`) |

### SPORTS (per league)

Normal completed games agree on the factual winner (deterministic). All divergence is in the void/
postpone tail. **Settled-outcome reconciliation is OPEN for every league** — pmus sports `endDate` =
gameStart **+14 d exactly** (live-confirmed `aec-mlb-az-mia-2026-06-11` → endDate 2026-06-25), so every
settled pmus sports object is still INTERIM; `settle_recon` correctly reports **0 sports compared** today.

| League (pairs today) | Source identity | Void/tail divergence | Status |
|---|---|---|---|
| **MLB (23)** | RULES (same factual winner) | 🔴 **MATERIAL**: Kalshi voids to "fair price" if reschedule >2 days; pmus settles real winner if ≤2 weeks → a game replayed **3–14 d later settles OPPOSITELY** = both-legs loss. The actionable detection window is **minutes** (Kalshi closed voided markets 47–90 min after scheduled start, n=3) | OPEN + named-not-quantified tail |
| **ITF-W (77), ITF-M (72), ATP (11), WTA (14)** | RULES | ⚠️ walkover/retire/forfeit: Kalshi resolves only "after a ball played"; pmus → last-traded for pre-start walkover. Divergence on retirement mechanics | OPEN |
| **UFC (7)** | RULES | ⚠️ no-contest handled differently (Kalshi explicit tie/NC; pmus → last-traded) | OPEN |
| **CS2 (6), LoL (6), Valorant (4)** | RULES | 🔴 abandonment asymmetry: **Kalshi SILENT**, pmus → last fair price | OPEN |
| **WNBA (4)** | RULES | 🔴 same silent-vs-last-price abandonment asymmetry | OPEN |
| **NBA / NHL / COD** | n/a | **mapped but 0 actual pairs** — pmus lists no co-listed moneylines (NBA = celebrity-attendance novelties only). NOT a deploy risk today | — |
| **Symmetric-void basis** | — | Even when BOTH void: Kalshi "fair price" ≠ pmus "last-traded". n=1 observed (`aec-mlb-tb-nyy-2026-05-23`): both voided 0.44/0.56, basis mismatch **$0.00** that once | partly observed |

### ECON (per family)

L21 three-part check done live against **primary rules text** (number / inequality / grid), per family:

| Family (pairs today) | Inequality (live rules) | Grid | Twin join | Status |
|---|---|---|---|---|
| **U-3 (8 pairs)** | Kalshi `strike_type=greater` "is above" (STRICT); pmus "is **at least** 4.4%" (INCLUSIVE) — both pulled live this session | 0.1 | `≥4.4 ↔ T4.3` (`floor=T−0.1`), live-verified pairing `atl4pt4↔KXU3-26JUN-T4.3` | **RULES-correct**; settled recon OPEN (release 07-02) |
| **CPI YoY** | `greater` "increases by **more than** 5.0%" (live) | 0.1 | `floor=T−0.1`; CPI *middles* are point-buckets (no twin) → tails only | RULES; no live pairs today |
| **GDP SAAR** | `greater` "increases by more than 4.0" (live) | 0.1 | `floor=T−0.1` | RULES; 0 live pairs (no listed twin) |
| **NFP** | `greater` "is above 150000" (live) | 1000 | `floor=T−1000` | RULES; 0 live pairs |
| **Fed (5 pairs)** | CATEGORICAL (`strike_type=custom`, label↔label) — no inequality | n/a | `maintains`/±25bps etc., label match | **RULES** (cleanest); settled recon OPEN (FOMC 06-17) |

**Phantom-elimination verified in LIVE logging** (not just the matcher): in `transitions-2026-07-02.jsonl`,
pre-redeploy records show the documented **12.2¢ U-3 phantom** (`atl4pt4`, dir K, net 0.1222, the off-by-one
`↔T4.4` pairing); post-redeploy (t≥`1781082189`) the SAME slug family logs **median 0.7¢** (efficient
twins). The two post-epoch >5¢ records are `atl4pt2↔T4.1` (correct twin) with a **22¢-wide / flat-ladder
pmus quote** = a fillability/stale-quote artifact (L13/L20 — handled by depth+crossed guards), categorically
NOT a settlement phantom. So the phantom is gone from both the matcher and the live stream.

---

## TASK 2 — Weather recon RECONFIRM on freshest data + raw-NWS spot-check

**Result: 360/360 identical, 0 divergence, 0 CLI mismatch — NO DRIFT.** (`scripts/_data/settle_recon_weather_20260611-000044.json`)
- Kalshi result vs NWS CLI: 330/330 match. pmus outcome vs NWS CLI: 330/330 match. Kalshi recorded
  `expiration_value` vs independent CLI: 55/55 match.
- **3-bucket raw-NWS spot-check (independent of the script's own read), 2026-06-10:**
  MIA MAXIMUM=88, MDW=91, SFO=79 from `forecast.weather.gov` newest version → all equal `cli.jsonl` and the
  recon's `kal_val`. Plus the recon's NWS version-walk independently re-derived 06-01→06-07 maxes (≈35
  station-days) all == `kal_val`. **No drift on any bucket.**
- Revision-day behavior holds: MDW 06-09 logged 87 then 88; both venues settled **88** (the settlement-
  relevant *downward*-correction tail did NOT fire — this was an upward revision, guarded by neither but
  aligned).

---

## TASK 3 — OPEN both-legs-loss risks in production: enumerated, quantified-or-named, $ exposure

| # | Risk | Quantified? | Unmitigated $/occurrence | Notes |
|---|---|---|---|---|
| 1 | **MLB 3–14 d replay void** (Kalshi fair-price void vs pmus real winner) | **NAMED, not fully quantified.** Frequency partly bounded: postpone rate ~1.2–1.3%/game (5/415 scan; 29/31 per ~2430 in 2024). The 3–14 d gap-share `p_gap≈0.4` is an *estimate* (observed 0/5 in-window so far → maybe ~2.5× pessimistic). | **~50¢/contract** on the held pair (one leg pays $1, the void leg refunds ~fair price ≈ entry, so net loss ≈ the position). On a 23-pair MLB book this is the single largest tail. | **Mitigable read-only**: statsapi `detailedState=Postponed` detected 5/5 with reschedule date; unwind in MINUTES (not the 2-day comfort) → EV +12–13¢/contract on trigger. Detector buildable now; not yet built into a trade layer. |
| 2 | **Econ off-by-one** (any family's grid wrong) | **QUANTIFIED + CLOSED.** = market-priced P(print==T), was ~12.2¢ on ATM U-3. | $0 forward (twin join live-verified all 4 families this session; phantom gone from live stream post-redeploy; pre-remap quarantined). | Residual is only if a NEW family/series is added without re-running the L21 3-part check. |
| 3 | **Sports endDate interim-vs-final** | **NAMED.** pmus `closed=true` ≠ finalized; endDate = +14 d (live-confirmed). | Not a both-legs-loss by itself — it's a **capital-lockup** (≤14 d) + a measurement gap: we have **zero** settled-sports reconciliation, so every sports identity claim is RULES-only. | Recon re-runs post-endDate ~06-23/25 (first real sports datapoint). Until then sports settlement identity is unproven empirically. |
| 4 | **Esports/WNBA abandonment asymmetry** (Kalshi silent, pmus last-price) | **NAMED, not quantified** (no abandonment observed; rate unknown). | Up to **~50¢/contract** if Kalshi voids/last-prices differently than pmus on an abandoned match. Smaller books (esports 4–6, WNBA 4 pairs). | Same class as MLB but no detection rule specced; lower exposure by pair count. |
| 5 | **Weather 8–11 AM downward CLI correction on a boundary day** | **PARTIALLY QUANTIFIED.** Rate measured: **0 downward revisions / 14 station-days** (Jeffreys 95% ceiling ~12.6%); boundary evenings ≈ 6% of pair-evenings. | **~$1/contract** (full both-legs loss) on the rare joint event (boundary day AND downward 8–11 AM correction). EV term tiny: P(boundary)×P(downward flip) — early-exit EV analysis says hold. | The narrowest tail; the only weather one. Bounded live by `cli_revisions.py`; ~3 more weeks resolves it vs the 2% maker-exit breakeven. |

**Quantified:** #2 (closed), #5 (rate bounded). **Named-not-quantified:** #1 (freq partly bounded, gap-
share + per-event $ estimated), #3 (lockup not loss), #4 (rate unknown).

---

## TASK 4 — Coverage: what a bot would trade TODAY whose identity is RULES-only (deploy-risk surface)

Live `build_colisted_map()` this session (counts drift intra-session with catalog churn):

| Category | Pairs today | Settled-recon status | Deploy-risk surface |
|---|---|---|---|
| **Weather** | 58–60 (0 misaligned) | **VERIFIED n=360** | **SAFE** — the only empirically-closed category. |
| **Sports** | 224 (ITFW 77, ITFM 72, MLB 23, WTA 14, ATP 11, UFC 7, CS2 6, LoL 6, WNBA 4, Val 4) | **OPEN (0 reconciled)** — all pmus endDates future | **ALL 224 are RULES-only.** Completed games fine; the void/postpone/abandon tail is the both-legs risk. MLB (23) carries the material 3–14 d tail. |
| **Econ** | 13 (U-3 8, Fed 5) | **OPEN (0 reconciled)** — releases 06-17/07-02 | RULES-only but **rules-correct + efficient** (twins verified, phantom gone). Fed categorical = cleanest. |

So **282 of ~295 co-listed pairs (95%, all sports + econ) would trade on RULES-only identity.** Weather (the
20% category-capped slice) is the empirically-verified one.

---

## TASK 5 — BLINDSPOTS: identity ASSUMED without primary-rules check (L21/L23)

1. **Sports "same factual winner" is assumed, never settled-reconciled.** We have the rules text and a
   strong prior, but **0** settled-outcome reconciliations (vs weather's 360). The 06-09 sports "interim
   reads 56/56 == Kalshi" came from pmus INTERIM objects (pre-endDate) and was itself once corrupted by the
   L23 array-order parse bug — so even that is not a finalized read. **Until ~06-23/25 this is the biggest
   unverified-identity surface by pair count (224).**
2. **CPI/GDP/NFP grids are rules-verified but UNEXERCISED.** The twin join is correct in code + rules, but
   no CPI/GDP/NFP pair has ever co-listed-and-settled (0 live GDP/NFP pairs today; CPI middles are point-
   buckets). The first real CPI/GDP/NFP settlement (07-02 NFP, later CPI/GDP) is the first test of those
   grids end-to-end. A wrong `step` (e.g. if BLS changed reporting precision) would be a fresh off-by-one.
3. **pmus has NO settlement timestamp** (L23) — weather finality is only BRACKETED (correct by T+13.5 h
   post-Kalshi; 0–13.5 h unobserved). A bot that early-exits inside that window on a "closed" pmus object
   could read an interim state. Mitigated by policy (treat pre-bracket reads as interim) + the daily
   in-window recon read, not by a hard finality signal.
4. **The transition log stores only the pmus `market` slug, not the Kalshi ticker it was paired to.** So a
   pairing-correctness bug is invisible in the archive — it can only be inferred from the matcher version
   (epoch timestamp). The 0013 phantom was caught this way (epoch split), but it means **future mispairings
   would not be self-evident from logged records**; the guard is entirely in `colisted_map` at log time.
   (Not a settlement divergence per se, but it's the audit-trail blindspot for invariant #1.)
5. **Symmetric-void basis (Kalshi "fair price" vs pmus "last-traded") is assumed ≈equal from n=1.** The one
   observed case agreed to $0.00, but that's a single coincidence; the basis can differ on a thin/divergent
   book and is not bounded.

---

## VERDICT

- **Settlement-SAFE to trade now: WEATHER only** — empirically VERIFIED n=360/360 (3-way, reconfirmed this
  session, raw-NWS spot-checked, no drift). Its sole tail (downward boundary CLI correction) is rate-bounded
  and EV-tiny.
- **Carry UNQUANTIFIED both-legs risk: SPORTS (224 pairs, all RULES-only, 0 settled reconciliations) and
  ECON (13 pairs, rules-correct but 0 settled reconciliations).** Sports is the larger surface and holds the
  material MLB 3–14 d replay-void tail (~50¢/contract, named-not-fully-quantified, mitigable by a minutes-
  latency unwind that isn't built yet). Esports/WNBA abandonment is the same class, smaller.
- **Weather reconfirm: 360/360 identical, 0 divergence, 0 CLI mismatch; 3 buckets independently raw-NWS
  spot-checked (MIA 88 / MDW 91 / SFO 79) — match. No drift.**
- **Open tails enumerated (5):** QUANTIFIED/closed = econ off-by-one (gone from live stream + quarantined),
  weather boundary correction (rate-bounded). NAMED-not-quantified = MLB replay void, esports/WNBA
  abandonment, sports interim-vs-final (lockup, and the unverified-identity surface).
- **Bottom line for capital:** the catastrophic axis is CLEAN for weather and UNPROVEN-EMPIRICALLY for
  sports + econ. The honest gate is: deploy capital only into weather until the post-endDate sports recon
  (~06-23/25) and post-release econ recon (FOMC 06-18, NFP/U-3 07-03) convert those 224+13 RULES-only pairs
  into VERIFIED — OR size sports/econ tiny with the MLB/abandonment unwind rules built first.

**Artifact:** `tasks/_agent_bus/20260611-readiness/auditC-settlement.md` ·
**Dump:** `scripts/_data/settle_recon_weather_20260611-000044.json`
