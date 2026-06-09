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
| **Settlement timing / revision** | Researched 2026-06-09 (both rulebooks + NWS): both settle ~**8 AM ET next day off the same morning CLI** (Kalshi snapshots the morning *preliminary*, not a fully-final CLI — same value pmus reads), so timing matches. Residual **asymmetry on a *downward* morning correction**: Kalshi delays to 11 AM & can take the lower value; pmus locks at 8 AM. | ⚠️ matched; narrow residual risk |

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
   pmus (per its public FAQ) **locks at 8 AM** — so the two could resolve to **opposite buckets → lose both
   legs**. *Upward* morning corrections are guarded by neither, so they stay aligned. **Owner to confirm:**
   (a) pmus's post-8 AM correction policy — the FAQ is silent; the "only the final report matters" claim is
   third-party, not primary (ask QCX support, read a live `tc-temp-*high*` market's full resolution text, or
   observe one real correction day); (b) the empirical rate — log the morning preliminary CLI **and** any
   same-day corrected CLI per station to measure how often the daily max actually moves. **Primary refs:**
   [Kalshi `NHIGH` terms](https://kalshi-public-docs.s3.amazonaws.com/contract_terms/NHIGH.pdf) (live `KXHIGH*`)
   · [polymarket.us weather FAQ](https://docs.polymarket.us/faqs/weather-faqs).
2. **Middle buckets** — the probe sampled low-tail buckets today; re-run when 2°-wide middle buckets are
   listed to confirm their inclusive/exclusive inequality alignment per the 1:1 map.

Re-run any time: `python scripts/verify_settlement.py [--city lax]`.
