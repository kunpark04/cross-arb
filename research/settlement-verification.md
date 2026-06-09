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
   pmus (per the third-party "only the final report matters" claim) **locks at 8 AM** — so the two could
   resolve to **opposite buckets → lose both legs**. *Upward* morning corrections are guarded by neither, so
   they stay aligned. **Status 2026-06-09:**
   - **(a) live-object read — DONE, partially resolved.** `verify_settlement.py` pulled a live MIA pair: Kalshi's
     `rules_secondary` names the *"official and **final** value"* and its **expiry is 10 AM EDT**
     (`2026-06-10T14:00Z`) — Kalshi's side of the asymmetry (waits past 8 AM, takes the *final*) is now
     **primary-source confirmed**. pmus's live market object carries **no** timing/preliminary/final/revision
     language (only *"Outcome verified from NWS Climatological Report"*) — so its 8 AM-lock policy is **still
     not in primary text**; the FAQ is silent and the live object is silent. Closing it fully needs QCX support
     or one observed correction day.
   - **(b) empirical rate — NOW BEING MEASURED.** `bot/monitor.py` (`cli_stream`) logs every distinct
     `(station, report_date, max)` to `_data/cli.jsonl`; `scripts/cli_revisions.py` reports the revision rate,
     the **downward** rate (the settlement-relevant direction), and the drop-magnitude distribution. It's an
     **upper bound** on the loss rate (counts any intra-day downward move, not just 8–10 AM boundary-straddling
     ones). Live on the droplet since 2026-06-09; day-1 read = 0 revisions / 5 station-days (accrues over weeks).

   **Open contradiction (flag, do not resolve here).** Two briefs disagree on whether the **pmus weather FAQ
   specifies settlement timing**. This brief (above, §1(a)) says *"the FAQ is silent and the live object is
   silent."* But [`polymarketus-catalog-settlement.md`](polymarketus-catalog-settlement.md) (§Weather
   settlement — verbatim detail) quotes the pmus weather FAQ **verbatim**: *"Settlement occurs at 8:00 AM ET
   … may be delayed to 11:00 AM ET for review."* The two cannot both be right. **Do not resolve it here** — it
   must be settled by **re-reading the live FAQ** at <https://docs.polymarket.us/faqs/weather-faqs>. The
   residual-risk conclusion **depends on which is correct**: if the FAQ *does* specify 8/11 AM, the asymmetry
   **narrows** because pmus *also* delays for review — though note its 11 AM trigger is **METAR-inconsistency**,
   not a downward CLI correction, so it does not symmetrically guard the downward-correction case.

   **Primary refs:** [Kalshi `NHIGH` terms](https://kalshi-public-docs.s3.amazonaws.com/contract_terms/NHIGH.pdf)
   (live `KXHIGH*`) · [polymarket.us weather FAQ](https://docs.polymarket.us/faqs/weather-faqs).
2. **Middle buckets** — the probe sampled low-tail buckets today; re-run when 2°-wide middle buckets are
   listed to confirm their inclusive/exclusive inequality alignment per the 1:1 map.

Re-run any time: `python scripts/verify_settlement.py [--city lax]`.
