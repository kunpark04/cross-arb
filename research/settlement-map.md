# Kalshi × Polymarket — Settlement Identity + US-Legality Map

**Date:** 2026-06-08 · **Status:** Synthesis of `settlement-compatibility-matrix.md`, `kalshi-venue-audit.md`, `polymarket-venue-audit.md`, `polymarketus-catalog-settlement.md`. Facts sourced there; this is the decision-grade map.

> 🔁 **PARTIALLY SUPERSEDED by `us-legal-overlap-audit.md` (live API pulls, 2026-06-08).** Two corrections: (1) **Econ (CPI/U-3/GDP/NFP/Fed) IS live and US-legal on polymarket.us** — *not* blocked as the row below claims — settling on the same BLS/BEA/Fed prints as Kalshi. (2) **Weather on polymarket.us settles on NWS CLI** (same as Kalshi), so it is settlement-clean there (and the fat spread is gone). Crypto is **absent** on polymarket.us. Trust the audit over the "US-legal?" column below where they differ.

## Two principles govern the map

**1. Settlement identity.** A true cross-venue arb is structurally possible only when **both venues grade off the same single deterministic public number that each explicitly names.** Otherwise the "arb" can lose *both* legs.

**2. US-legality (the filter that dominates).** There are **two different Polymarkets**, and which one a row uses decides both legality *and* settlement:
- **Polymarket international (polymarket.com):** US-**BLOCKED** for US persons (geoblocked under the 2022 CFTC settlement + ToS). Settles weather via **Weather Underground**, many markets via **UMA** optimistic oracle. This is where the *fat* divergences live.
- **Polymarket US (polymarket.us / QCX):** US-**LEGAL** (CFTC-regulated DCM, USD via FCM). Settles via a **deterministic rulebook with the SAME sources as Kalshi** (weather = **NWS CLI**, no UMA). Catalog is currently **narrow** (weather + sports confirmed; crypto + econ not).
- **Kalshi:** US-legal in all 50 states (CFTC DCM; minor NV/NJ legal noise).

**The interaction is the whole story:** the source-divergence that creates the spread is *exactly* what the US-legal venue removes. Legal ⇒ thin; fat ⇒ blocked or directional.

---

## Master map (US column added)

Ordered US-legal first. "Poly venue" notes which Polymarket lists the family.

| Category — Market family | **US-legal?** | Kalshi settles on | Polymarket settles on (venue) | Same #? | Net verdict |
|---|---|---|---|---|---|
| **Weather — daily HIGH temp** (Miami + 4 cities) | ✅ **via polymarket.us** | NWS CLI | **.us: NWS CLI** ✅ · .com: Wunderground ❌ | **.us: ✅** · .com: ❌ | **US-legal & settlement-CLEAN — but spread was source-driven, so a real edge may not exist. Bucket-grid + station match TBD¹** |
| **Sports — game winner / series** | ✅ **via polymarket.us** | league / AP / Sportradar | .us: deterministic DCM rulebook · .com: named + UMA | usually ✅ | **US-legal, deterministic both sides — but a structural play, not a boundary arb** |
| **Weather — daily LOW temp** | ✅ if listed on .us | NWS CLI | .us: NWS CLI ✅ (verify listed) · .com: Wunderground | .us: ✅ | Same as high-temp: legal+clean, edge questionable |
| **Politics — election winner** | ⚠️ **.us unverified** | AP / certification | .us: unverified · .com: official + UMA | usually ⚠️ | US-legality unconfirmed on .us; .com blocked |
| **Econ — CPI / FOMC / GDP / NFP** | ❌ **BLOCKED** (intl only) | BLS / Fed / BEA print | BLS / Fed / BEA — **.com only** | ✅ Yes | **Cleanest settlement of all — but US-BLOCKED. The painful one.** |
| **Crypto — BTC/ETH levels & up/down** | ❌ **BLOCKED** (intl only) | CF BRTI 60-sec TWAP | Binance 1-min / Chainlink / UMA — .com | ❌ | Source-risk **and** US-blocked |
| **Weather — *intl divergence* play** | ❌ **BLOCKED** | NWS CLI | .com: Wunderground (the fat ~24¢ seed example) | ❌ | The original fat edge — directional **and** illegal for US persons |
| **Sports props / "did X do Y"** | ❌ **BLOCKED** (intl/UMA) | league stat / Rule 6.3(c) | .com: **UMA "spirit of market"** | ❌ can diverge | Directional (Cardi B class) **and** blocked |
| **Politics — "will X say/do Y"** | ❌ **BLOCKED** (intl/UMA) | determination source | .com: **UMA tokenholder vote** | ❌ | Directional **and** blocked |
| **Culture / novelty / mentions** | ❌ **BLOCKED** (intl/UMA) | varies / often none | .com: **UMA** (most subjective) | ❌ | Worst-case directional **and** blocked |
| **Econ minor (PCE, claims, retail) · Weather precip/snow** | — | BLS / BEA / NWS | not listed on either Poly | — | **No overlap → no arb** (Kalshi-only) |

¹ **Open on polymarket.us weather:** (a) temperature **bucket grid / boundary inequality** not published in the FAQ — pull the live `gateway.polymarket.us/v1/markets` API; (b) **per-city station match** — Miami=KMIA likely aligns with Kalshi; NYC uses KNYC/Central Park — verify Kalshi's NYC station before pairing.

---

## What the US filter does to the strategy

- **The clean-arb universe (CPI/FOMC/GDP/NFP) is US-BLOCKED.** Those identical-government-print markets exist on international Polymarket only. A US person cannot legally trade them against Kalshi today.
- **The US-legal universe (Kalshi × polymarket.us) is weather + sports — and both settle on the same deterministic sources as Kalshi.** That's *good* for settlement safety (no double-loss source gap, no UMA), but it removes the divergence engine: the seed example's fat 24¢ came from Kalshi(NWS) vs Polymarket(**Wunderground**). The legal version is Kalshi(NWS) vs polymarket.us(**NWS**) — same number, so the expected spread is small and driven only by liquidity/latency/demand imbalance, not by a structural source gap.
- **Net:** the legal opportunity is real but modest — capturing small, *genuinely clean* spreads on Kalshi × polymarket.us weather (and possibly sports), not the fat directional weather "arb" from the HTML. The fat stuff is either blocked (econ, crypto) or a disguised bet (intl weather, UMA markets).

## Top due-diligence items (priority order)

1. **Pull live polymarket.us weather markets** (`gateway.polymarket.us/v1/markets`, no-auth) and **measure the actual Kalshi-vs-polymarket.us spread** on the same city/day. This is now the single decisive experiment: if same-source markets still show a recurring >5¢ spread, there's a clean US-legal edge; if not, the legal version is dead.
2. **Confirm polymarket.us bucket grid + boundary inequality** vs Kalshi (integer edges, inclusive/exclusive) per city.
3. **Verify per-city station match** (Miami KMIA; NYC KNYC/Central Park vs Kalshi's station).
4. **Confirm whether polymarket.us lists politics / will list crypto+econ** (roadmap) — that would reopen the clean-arb families to US persons.

## Sources
Primary citations in the four research briefs' Sources sections. Key US-venue facts: `polymarketus-catalog-settlement.md` (docs.polymarket.us weather FAQ = NWS CLI; deterministic rulebook; `gateway.polymarket.us/v1/markets`; CFTC Amended Order of Designation).
