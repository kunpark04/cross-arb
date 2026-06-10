# Econ settlement identity: pmus `≥T` is NOT Kalshi "Above T" — the twin is `T − step`

**Date:** 2026-06-10 · **Status:** VERIFIED live against both venues' rules text + order books
**Scripts:** `bot/colisted_map.py` (`econ_twin`, ECON steps), `scripts/verify_econ_settlement.py`
**Decision:** [0013](../decisions/0013-econ-grid-step-twin-and-measurement-integrity.md) (supersedes the 0011 pairing rule)

## The finding

The 0011 econ mapping joined pmus and Kalshi threshold markets on **equal threshold numbers**
(`pmus ≥ 4.4 ↔ Kalshi T4.4`). The threshold *numbers* matched; the **inequality semantics do not**:

| Venue | Rules text (live, 2026-06-10) | Meaning |
|---|---|---|
| pmus `urc-…-atl4pt6` | "…unemployment rate (U-3) … is **at least 4.6%** … settles to Yes" | **≥ T** (inclusive) |
| Kalshi `KXU3-26OCT-T5.0` | "…is **above 5.0%** …resolves to Yes", `strike_type: "greater"` | **> T** (strict) |

Same `greater` semantics verified for **KXCPIYOY** ("increases by **more than** 5.0% … one-decimal
place value"), **KXGDP** ("more than 4.0"), **KXPAYROLLS** ("above 150000"). `KXFEDDECISION` is
categorical (`strike_type: custom`, label↔label) — clean, unaffected.

BLS/BEA prints are quantized: U-3/CPI-YoY/GDP-SAAR to **one decimal**, payrolls to **1,000s**. On that
grid, `> T` ≡ `≥ T + step`, so:

- `pmus ≥ T` is settlement-**identical** to `Kalshi > (T − step)` — *that* is the twin.
- `pmus ≥ T` paired with `Kalshi > T` (the 0011 join) **differs on exactly one outcome**: a print
  landing exactly on `T` settles pmus **YES** and Kalshi **NO**.

For an at-the-money threshold, "exactly T" is the **modal print region**, not a tail. The market
prices it directly:

```
live mids 2026-06-10 (June U-3, release 07-02):
  pmus  ≥4.4            bid 0.27 / ask 0.28   (mid ~0.275)
  Kalshi T4.3 (>4.3≡≥4.4) bid 0.30 / ask 0.36  (mid ~0.33)   <- the true twin, prices ADJACENT
  Kalshi T4.4 (>4.4≡≥4.5) bid 0.09 / ask 0.12  (mid ~0.105)  <- the 0011 partner, ~17c AWAY
```

The ~17¢ gap **is P(June U-3 prints exactly 4.4%) as priced** — boundary probability mass, not a
mispricing. The deployed monitor logged this pair as a persistent **12.2–13.3¢ "arb", dir K, depth
c2=423** (`transitions-2026-07-02.jsonl`): direction K buys YES@Kalshi(>4.4) + NO@pmus(≥4.4), i.e.
the side that **loses both legs** on the modal exact-4.4 print. EV ≈ +0.15 − P(4.4) ≈ **negative**.
The session log had celebrated it as "the first econ edge ever captured."

## Why the project missed it

The weather matcher already encodes this exact convention correctly — `kbounds` maps a floor-only
Kalshi tail to `[floor+1, ∞)` on the integer °F grid (live-verified in [settlement-verification.md](settlement-verification.md)).
The econ matcher verified "same family + period + threshold **number** + same source" and reasoned the
inequality difference away as "a narrow residual, akin to the weather downward-correction" (0011).
Verifying the *number* is not verifying the *bucket*: the weather residual needs two coincidences
(boundary day AND a late downward CLI correction); the econ "residual" fires on the single most likely
print. See lesson [L21](../tasks/lessons.md).

## The fix (0013, landed 2026-06-10)

- `econ_colisted` joins pmus `≥ T` to `floor_strike = econ_twin(T, step) = T − step`
  (steps: U-3/CPI/GDP **0.1**, NFP **1000**; Fed categorical unchanged). No listed twin → **not
  co-listed**, flagged `ge_no_identical_twin`.
- Live result: **24 pairs → 14 identical pairs + 13 honest skips** (the 13 had no listed twin — under
  0011 they were all settlement-divergent false pairs). Re-verified live: every remapped pair shows a
  small **negative** net edge (efficient identical markets; the phantom is gone).
- Pre-remap threshold-econ records are **quarantined** in `analyze_persistence.load()` (their removal
  moved the allocation OOS headline from +64%/+269% to **+9%/+141%** — the phantom was ~20% of design
  PnL and sat in the published tables).

## Residuals that remain (real, smaller)

- **Revisions**: both venues settle on the *initial* print (pmus: "Any subsequent revisions…" excluded;
  Kalshi expiration on the release) — aligned, no divergence.
- **No-twin thresholds aren't tradeable as locks.** Pairing them anyway with an explicit market-implied
  P(print==T) term would be a *priced boundary bet*, not an arb — out of scope for the read-only phase
  (0013 alternatives).
- The **running droplet still logs the old pairs** until the gated redeploy (0006); quarantine covers
  the analysis side meanwhile. On redeploy set `ECON_REMAP_DEPLOY_TS` in `analyze_persistence.py`.

## Sources

- Kalshi API market objects (rules_primary, strike_type) for KXU3 / KXCPIYOY / KXGDP / KXPAYROLLS /
  KXFEDDECISION — live pulls 2026-06-10.
- pmus gateway market descriptions (`urc-…`, "at least") + `/book` mids — live pulls 2026-06-10.
- Archive evidence: `Kalshi/data/cross-arb/transitions-2026-07-02.jsonl` (the 12.2¢ phantom records).
- `scripts/verify_econ_settlement.py` (now joins with the same `econ_parse`/`econ_twin` the monitor uses).
