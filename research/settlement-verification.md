# Settlement-identity verification — weather pair (2026-06-09)

**Why this brief exists.** Settlement identity is invariant #1: the cross-arb is only *clean* (outcome-
independent, can't lose both legs) if both venues grade the *same* market off the *identical* number. The
earlier briefs left this **unverified and self-contradictory** (`settlement-compatibility-matrix.md` said
weather was "a directional bet in disguise" on a NWS-vs-Wunderground source split; later briefs reversed to
"NWS CLI, clean" but flagged the boundary/station checks as `[UNVERIFIED]`). An independent review made this
the #1 risk to the thesis. This brief records the **live verification** that resolves it.

**Method.** `scripts/verify_settlement.py` (read-only, public APIs) pulls BOTH venues' actual market rules
for each co-listed weather city and diffs source / station / bucket-boundary / settlement-timing. Re-runnable.

## Result (2026-06-09, all 5 mapped cities: LAX, MDW, MIA, NYC, SFO)

| Check | Finding | Verdict |
|---|---|---|
| **Settlement source** | **Both venues: "NWS Climatological Report (Daily)."** pmus descriptions say so explicitly; Kalshi `rules_primary` says so. | ✅ identical |
| **Station** | Identical per city — LAX→LAX airport (KLAX), MDW→Chicago Midway (KMDW), MIA→Miami Intl (KMIA), **NYC→Central Park (KNYC)**, SFO→SF airport. The NYC station the review specifically doubted **matches**. | ✅ identical |
| **Bucket boundary** | Matches on the sampled (low-tail) buckets: e.g. LAX Kalshi "less than 67° / 66° or below" == pmus "less than or equal to 66F". | ✅ matches (sampled) |
| **Settlement timing / revision** | Researched 2026-06-09 (both rulebooks + NWS) **+ live market objects** (`verify_settlement.py`): both grade off the same next-morning CLI. **Primary-source asymmetry now read directly:** Kalshi's rules name the *"official and **final** value"* and a live MIA market's **expiry is 10 AM EDT** (`2026-06-10T14:00Z`) — so Kalshi waits past 8 AM for the *final*; pmus's live object says only *"Outcome verified from NWS Climatological Report"* with **no timing/correction language at all**. So on a *downward* 8–10 AM correction the two can split. The **rate** of such corrections is now logged live (`cli.jsonl` → `cli_revisions.py`). | ⚠️ matched; narrow residual risk, now being measured |

## Conclusion

The worst case — different source or station, i.e. a "directional bet in disguise" — is **refuted for all
5 cities** by the live rules. Source + station + (sampled) boundary are identical. So the weather pair is a
**genuine same-number settlement** to the extent verified, and the persistent NYC ~17¢ "edge" in the old
persistence probe is **not** a settlement-divergence artifact (settlement matches) — it is far more likely a
**stale / illiquid quote** (now detectable via the monitor's per-transition `age` staleness field +
crossed-book rejection).

## Still to close before deploying capital

1. **Settlement timing — mostly resolved (2026-06-09 research; refs below).** Both venues settle **~8 AM ET
   the day after**, off the **same morning-after NWS CLI** read in the same window — so on normal days the
   snapshotted max is identical. Key realization: **Kalshi does NOT wait for a fully-final CLI**; its contract
   terms snapshot the morning *preliminary* value (same as pmus) and only ignore revisions landing *after*
   expiration. **Residual risk** is asymmetric + narrow: on a **bucket-boundary day with a *downward* morning
   CLI correction**, Kalshi's rules **delay to 11 AM ET and can settle on the lower corrected value**, while
   pmus **locks at 8 AM** (delaying to 11 AM only for a **CLI-vs-METAR inconsistency**, NOT a downward CLI
   correction) — so the two could resolve to **opposite buckets → lose both legs**. *Upward* morning corrections
   are guarded by neither, so they stay aligned. **Status 2026-06-09:**
   - **(a) primary-source read — DONE, both sides now confirmed.** `verify_settlement.py` pulled a live MIA pair:
     Kalshi's `rules_secondary` names the *"official and **final** value"*, expiry **10 AM EDT** (`2026-06-10T14:00Z`)
     — Kalshi waits past 8 AM for the *final*. **pmus timing is now ALSO primary-source confirmed**: although the
     market *object* carries no timing language, the **pmus weather FAQ does** (re-read 2026-06-09):
     *"Settlement occurs at **8:00 AM ET** on the day following the Contract's specified date … may be delayed
     until **11:00 AM ET** for review [if the CLI reading is **inconsistent with the 24-hour METAR**] … last
     fair market prices if no data within one week."* So pmus is **not** a naive 8 AM hard-lock — it has its own
     11 AM review path, but the trigger is **CLI≠METAR**, not a downward CLI correction. The downward-correction
     case is therefore **still not symmetrically guarded**, but the asymmetry is **narrower** than feared. Fully
     closing the magnitude still needs the empirical rate (below) or one observed correction day.
   - **(b) empirical rate — NOW BEING MEASURED.** `bot/monitor.py` (`cli_stream`) logs every distinct
     `(station, report_date, max)` to `_data/cli.jsonl`; `scripts/cli_revisions.py` reports the revision rate,
     the **downward** rate (the settlement-relevant direction), and the drop-magnitude distribution. It's an
     **upper bound** on the loss rate (counts any intra-day downward move, not just 8–10 AM boundary-straddling
     ones). Live on the droplet since 2026-06-09; day-1 read = 0 revisions / 5 station-days (accrues over weeks).

   **Contradiction RESOLVED (2026-06-09, live FAQ re-read).** The pmus weather FAQ **does** specify timing —
   [`polymarketus-catalog-settlement.md`](polymarketus-catalog-settlement.md)'s verbatim quote was **correct**;
   this brief's earlier *"the FAQ is silent"* was **wrong** (fixed in §1(a) above). Net: both venues have an
   11 AM review path, but on **different triggers** (Kalshi: downward/final-value; pmus: CLI-vs-METAR
   inconsistency), so the downward-correction boundary case is still not symmetrically guarded — the residual
   shrinks but does not vanish, and `cli_revisions.py` still bounds its rate.

   **Primary refs:** [Kalshi `NHIGH` terms](https://kalshi-public-docs.s3.amazonaws.com/contract_terms/NHIGH.pdf)
   (live `KXHIGH*`) · [polymarket.us weather FAQ](https://docs.polymarket.us/faqs/weather-faqs).
2. **Middle buckets — VERIFIED aligned (2026-06-09).** A live SFO 2°-wide middle bucket checked both ways:
   Kalshi `KXHIGHTSFO-26JUN09-B66.5` (`yes_sub_title` *"66° to 67°"*, `floor_strike=66`/`cap_strike=67`,
   rules *"maximum temperature … is between 66-67°F … NWS Climatological Report (Daily)"*) and pmus
   `…-gte66lt67f` (*"between 66F and 67F … as reported by the NWS Climatological Report (Daily)"*, KSFO) **both
   canonicalize to the inclusive range [66, 67]** and cite the **same source + station**. The complete 1:1
   tiling (pmus `lt64 / gte64lt65 / gte66lt67 / … / gte72` ↔ Kalshi `≤63 / 64-65 / 66-67 / … / ≥72`) confirms
   the inclusive `{66,67}` interpretation on both sides — no off-by-one at the boundary. The
   `colisted_map.py` boundary-equality guard (`pm_bounds`/`kbounds`) now **enforces** this on every pair.
3. **Bucket count/boundary drift** — the guard above refuses (and loudly flags) any future pair whose
   `(floor,cap)` numbers differ, so a venue changing its ladder can no longer silently mispair.
4. **EMPIRICAL reconciliation — still open; pmus finalization lag discovered (2026-06-09,
   `scripts/settle_recon.py`, [execution-feasibility brief](execution-feasibility-2026-06-09.md)).** Settlement
   identity is rules-text-verified but **not yet empirically confirmed** by comparing both venues' actual graded
   outcomes — because **pmus flips `closed:true` immediately but keeps a `endDate` ~2 weeks in the future and
   serves an INTERIM `outcomePrices` that can be WRONG** (a verified ATP case had pmus's interim winner wrong,
   Kalshi correct; a MIA weather day returned "Yes" for 4 disjoint buckets). So the public pmus settled value is
   **unreliable until finalization** — re-run the recon after markets pass `endDate`.

Re-run any time: `python scripts/verify_settlement.py [--city lax]` (weather) ·
`python scripts/verify_sports_settlement.py [--league mlb]` (sports — see the sibling brief).
