# Probe 4 — Empirical settlement reconciliation, weather-first (2026-06-10 ~20:00–20:45 CDT)

**Owner:** settle-recon agent. **Files touched:** `scripts/settle_recon.py` (extended; selftest green at every save), this note, `scripts/_data/` evidence dumps. Read-only, no orders, no deploys.

## Verdict

**Invariant #1 for WEATHER: empirically CONFIRMED, zero divergence — n=360 settled co-listed buckets
(60 city-days, 12 days × 5 cities, 2026-05-29 → 2026-06-09), 100% identical grading on both venues.**
Three-way anchored to the independent NWS CLI: Kalshi result vs CLI **330/330** match, pmus outcome vs
CLI **330/330** match (30 buckets = 5 MIA days lacked an independent CLI value → two-way only), and
Kalshi's self-published settlement number (`expiration_value`) equals the monitor's independently
logged CLI **55/55** day-cells. The one real CLI **revision** day in window (MDW 2026-06-09, 87→88
upward overnight) settled to 88 on **both** venues — clean.

**But the headline finding is a measurement bug, not a market fact:** the first pass of this same
recon reported 22 weather "divergences" + 44/70 internally-impossible pmus days (multi-YES on disjoint
buckets) — **all phantom, caused by our own pmus winner-parsing convention** (details below). After the
fix: 0 and 0. The previously-published findings "pmus interim settled data can be WRONG (ATP case)" and
"pmus MIA 06-08 returned Yes for 4 disjoint buckets" are **explained by the same bug** (the ATP case is
retro-unadjudicable; today it reads correct under both conventions).

## The parse bug (CRITICAL correction to the documented pmus convention)

The 2026-06-09 convention — *"pair `outcomes[i]` with `outcomePrices[i]`; order varies"* — is **wrong**.
The two flat arrays are **not index-aligned with each other**: `outcomePrices` follows the
**`marketSides` order** (Yes/long side first in every object seen), while `outcomes`' order is cosmetic
display noise (`["Yes","No"]` or `["No","Yes"]`, and it can apparently change between resyncs).
The authoritative read is **`marketSides`**: each side carries its own label (`description` /
`team.name`) + settled `price` ("1" wins, "0" loses).

Smoking-gun raw object (in dump 202440, kept under `_pm_raw`), `tc-temp-laxhigh-2026-06-04-lt70f`
(true high 72 → No):

```
outcomes: ["No","Yes"]   outcomePrices: ["0","1"]      <- label-pairing reads "Yes wins" (WRONG)
marketSides: [ {description: Yes, long: true,  price: "0"},
               {description: No,  long: false, price: "1"} ]   <- No wins (== Kalshi == CLI)
```

**Bulk validation (n=286 raw settled weather buckets saved in the first run's dump):**
marketSides-paired winner == Kalshi result **286/286**; label-paired winner wrong on **exactly the 141
objects with `outcomes=["No","Yes"]`**, right on the 145 Yes-first ones (where both pairings coincide —
which is why the 06-09 spot-checks "verified" the wrong convention; L17-class failure). Sports same
shape: `aec-atp-talgri-botzan-2026-06-08` outcomes-order says Zandschulp, sides say Griekspoor=1
(== Kalshi). Fix shipped in `pm_winner()` (marketSides-primary, label-pairing only as fallback,
sides-not-cleanly-graded → unresolved), with regression selftests from the live cases.

## Numbers (corrected run, `--days-back 12`)

| Cell | n |
|---|---|
| Settled joined buckets compared | **360** — agree **360**, diverge **0** |
| pmus internally-inconsistent days (multi-YES tripwire) | **0** (was 44/70 under the bug) |
| Pending (event-dates 06-10/06-11, not yet settled) | 47 buckets — correctly `pm_unresolved` |
| pmus bucket constructed-but-not-listed / fetch errors | 0 / 0 |
| Kalshi result vs independent CLI | 330/330 match |
| pmus outcome vs independent CLI | 330/330 match |
| Kalshi `expiration_value` vs monitor/NWS CLI | 55/55 match |

Pair enumeration went **beyond the archive** (archive weather = only 06-10/06-11, untracked before the
0013 redeploy): for every settled Kalshi bucket, the bounds-identical pmus slug is **constructed**
(`pm_slug_for` = `pm_bounds` inverted — same identity join as `colisted_map`, run in reverse) and
fetched; pmus listed **every** constructed bucket for all 12 days (pmus weather listings exist back to
~2026-05-15; 2026-05-01 absent — probed).

## pmus weather finalization lag (task 3)

- **No settlement timestamp exists on the pmus object.** `updatedAt`/`ep3SyncedAt` are touched by a
  periodic resync (~hourly; all settled markets show fresh stamps), and `ep3Status` is `EXPIRED` for
  both finalized weather and 2-weeks-out interim sports — useless as a state machine. Lag is therefore
  **bracketed by reads at known times**, not read off a field.
- **Kalshi settles weather at ~8:02 AM ET D+1** (`settlement_ts`: NYC/MIA/MDW ≈ 12:02Z, LAX/SFO ≈
  11:32Z; occasional late LAX day when the final CLI is delayed, e.g. 06-04 LAX 18:31Z). pmus weather
  `endDate` (close) = **1 AM local D+1** (05:00Z eastern cities, 08:00Z pacific — slightly different
  from the "~2 AM ET" in CLAUDE.md), settlement per FAQ 8 AM ET.
- **Earliest probed post-settlement read: T+13.5 h** (event-date 06-09, read 2026-06-11 ~01:35Z) —
  outcomes already final, correct, and internally consistent on **all 5 cities**; same at every probed
  age out to **T+11.6 days**. So: pmus weather is trustworthy (via marketSides) by ~13.5 h after
  settlement; the **0–13.5 h window is unobserved** (would need a ~12–18 Z poll; the safe behavior is
  already in `pm_winner` — fractional/unflipped sides → `None` → "pending", never a wrong read).
- **Weather vs sports contrast stands structurally**: weather `endDate` passes *before* settlement →
  once graded it is final; sports keeps `endDate` ≈ **D+14** (observed 06-22/23/24 for the 06-08/09/10
  cohorts) with `closed:true` + already-populated outcomePrices = interim by the venue's own state.
  Under the corrected parser sports interim reads **56/56 == Kalshi** — the earlier "interim verified
  WRONG" (Diallo–Mannarino) is unadjudicable retroactively (today it reads correct under both
  conventions); keep treating pre-endDate sports reads as interim, but the known wrongness evidence
  is now gone.

## What unlocks sports/econ recon (task 4 — noted, not executed)

- **Sports finalized read:** tracked cohort pmus `endDate`s are 2026-06-22T08Z (06-08 games), 06-23
  (06-09), 06-24 (06-10). Re-run `settle_recon.py` **~2026-06-23/25** for the first post-endDate
  sports reconciliation (Kalshi already finalized).
- **Econ:** FOMC `rdc-usfed-fomc-2026-06-17-*` ↔ `KXFEDDECISION-26JUN` decides **2026-06-17 ~2 PM ET**
  (categorical, cleanest first datapoint — re-run ~06-18, then watch pmus's econ finalization lag);
  U-3 ×7 strikes + NFP ×1 (`...-2026-07-02-*` ↔ `KXU3/KXPAYROLLS-26JUN`) print **2026-07-02 8:30 AM
  ET** — re-run ~07-03.

## Recommendations for the owner (files not mine to edit)

1. **Correct two research briefs**: `research/settlement-verification.md` §4 and
   `research/execution-feasibility-2026-06-09.md` §"settle-recon" — the "pmus settled data
   unreliable/interim-wrong/4-YES" claims are explained by the parse bug; the surviving true claims are
   (a) sports endDate ≈ D+14 (closed ≠ finalized), (b) no pmus settlement timestamp. CLAUDE.md core
   findings ("one verified wrong") inherits the same correction, and weather close = 1 AM local (not ~2 AM ET).
2. **Lessons candidate (L23)**: when one API response carries the same fact in two encodings (flat
   sibling arrays vs self-labeling objects), never assume sibling arrays are index-aligned; prefer the
   self-labeling encoding; and validate a pairing convention on cases where the two conventions
   *diverge*, not on cases where they coincide (this bug survived because Yes-first objects agree under
   both). Reconfirms L17.
3. `scripts/weather_spread_snapshot.py` (superseded display probe) also label-pairs
   `outcomes.index("Yes")` → `outcomePrices` — harmless now, but fix-or-retire before reuse.
4. Optional: add a ~13:00Z daily probe (or monitor task) reading the just-settled day's pmus sides to
   close the 0–13.5 h lag window empirically.

## Artifacts

- `scripts/settle_recon.py` — extended: three-way weather recon (`recon_weather`, `classify_day`,
  `pm_slug_for`, `bucket_hit`, `cli_finals`, `nws_cli_walk`, `kal_recorded_value`, `iso_lag_h`),
  marketSides-primary `pm_winner`, evidence dump writer; offline `--selftest` green (run pre/post every edit).
- `scripts/_data/settle_recon_run_20260610.log` — buggy-parser run (the phantom 22-divergence picture, preserved).
- `scripts/_data/settle_recon_weather_20260610-202440.json` — dump with **286 raw venue objects** (basis of the 286/286 validation; raw kept for every flagged bucket).
- `scripts/_data/settle_recon_run_20260610_fixed.log` — corrected verdict run (360/360).
- `scripts/_data/settle_recon_weather_20260610-203509.json` — corrected-run dump (47 pending raws incl. 06-10/06-11).
