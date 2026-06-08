# Live Edge Findings — Kalshi × polymarket.us weather (first validated scan)

**Date:** 2026-06-08 · **Method:** simultaneous, depth-aware, fee-netted, bucket-aligned scan using **public** order books on both venues (no auth). Script: `scripts/weather_arb_scan.py`.

## Data-access breakthrough
- polymarket.us live **full-depth** order book is **PUBLIC**: `GET https://gateway.polymarket.us/v1/markets/{slug}/book` → `marketData.bids[]/offers[]` of `{px:{value}, qty}`. Keyed on **slug** (not numeric id — that 404s). No API key needed for reads.
- polymarket.us weather uses the **same 6 buckets as Kalshi** (≤85, 86-87, 88-89, 90-91, 92-93, ≥94) → 1:1 alignment. (Earlier "non-monotonic CDF" was a parsing artifact from the wrong endpoint/threshold assumption — resolved.)
- The book is the **YES side** (bids = sell-YES, offers = buy-YES); confirmed by price sanity vs Kalshi.

## Result (Miami + Chicago, settle 2026-06-09 ~AM, ~1-day hold)
| Bucket | Kalshi YES (bid/ask) | PM.us YES (bid/ask) | Best capture | Net/contract | Size |
|---|---|---|---|---|---|
| **Miami 90-91** | 0.69 / 0.70 | 0.60 / 0.61 | buy PM YES @0.61 + Kalshi NO @0.31 | **+4.8¢** | ~81 |
| **Chicago 84-85** | 0.35 / 0.37 | 0.44 / 0.48 | buy Kalshi YES @0.37 + PM NO @0.56 | **+3.8¢** | ~18 |
| all other buckets | | | venues agree | negative after fees | — |

Fees modeled: Kalshi taker `ceil(0.07·N·P(1−P))`, polymarket.us taker `0.05·P(1−P)`.

## Interpretation
- **Thesis validated:** the new venue (polymarket.us, Dec-2025) misprices the **modal bucket** ~9¢ mid-to-mid vs mature Kalshi. Settlement is **identical NWS CLI** → genuinely clean (no double-loss), **US-legal**, **~1-day** rotation. This is the real, legal version of the original HTML idea — edge ≈5¢, not 24¢ (the source divergence that inflated the HTML is absent on the US-legal venue).
- **But small & sparse:** typically **~1 bucket per city per day** clears fees; capacity ~tens of contracts → **single-digit $ profit per opportunity** at current depth. Edge % per trade is decent (~5% on staked capital in ~1 day) but absolute capacity is low.

## Caveats / open items
1. **Snapshot + leg risk:** both legs must be hit ~simultaneously; prices move. Not yet measured for persistence.
2. **Taker fees both sides** assumed; maker would widen edge but adds fill risk.
3. Only **2 of 5 cities** scanned; sports (4,164 PM.us markets, deeper books seen) not yet measured.
4. **Capture requires order-placement APIs** (reads are public): polymarket.us key (`polymarket.us/developer`, Ed25519) + Kalshi key (RSA). See `polymarketus-api-auth.md`.

## Recommended next step
Scan **all 5 weather cities + a sports sample** on the public books to size the **daily aggregate** edge, and snapshot over time for **persistence**. Decide whether the aggregate justifies setting up trading keys + capital. No key needed for any of this.
