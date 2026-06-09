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
| **Settlement timing / revision** | Kalshi names final-vs-preliminary CLI with a next-day expiry; pmus states **no time** in the market object (it's in the rulebook/FAQ, not the API description). | ⚠️ **OPEN** |

## Conclusion

The worst case — different source or station, i.e. a "directional bet in disguise" — is **refuted for all
5 cities** by the live rules. Source + station + (sampled) boundary are identical. So the weather pair is a
**genuine same-number settlement** to the extent verified, and the persistent NYC ~17¢ "edge" in the old
persistence probe is **not** a settlement-divergence artifact (settlement matches) — it is far more likely a
**stale / illiquid quote** (now detectable via the monitor's per-transition `age` staleness field +
crossed-book rejection).

## Still to close before deploying capital

1. **Settlement timing/revision** — confirm pmus's settlement time (reported ~8 AM ET next day, delayable)
   and whether it grades off the **same CLI revision** Kalshi does (Kalshi may revise a preliminary down to
   final). Same source + same station but **different timestamp/revision** can still resolve to different
   buckets on a borderline day. Needs the pmus rulebook (not in the API object).
2. **Middle buckets** — the probe sampled low-tail buckets today; re-run when 2°-wide middle buckets are
   listed to confirm their inclusive/exclusive inequality alignment per the 1:1 map.

Re-run any time: `python scripts/verify_settlement.py [--city lax]`.
