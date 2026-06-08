# US-Legal Overlap Audit — Kalshi × polymarket.us

**Date:** 2026-06-08 · **Method:** live pulls of both public APIs (`external-api.kalshi.com/trade-api/v2`, `gateway.polymarket.us/v1`). Scripts in `scripts/`; raw data in `scripts/_data/`. "US-legal" = Kalshi (CFTC DCM) × **polymarket.us** (QCX, CFTC DCM). International polymarket.com is excluded (US-blocked).

## Headline corrections vs. our earlier (docs-based) map

Live data overturned two assumptions:
1. **Econ IS live and US-legal on polymarket.us.** CPI, Unemployment (U-3), GDP, Nonfarm Payrolls, and Fed/FOMC are all open right now, each settling on the **same government print Kalshi uses** (BLS / BEA / Federal Reserve). This is the cleanest arb subset — and it is *not* blocked.
2. **Weather on polymarket.us settles on NWS CLI**, verbatim *"the National Weather Service's Climatological Report (Daily)"*, same station as Kalshi (Chicago=KMDW, Miami=KMIA, etc.). So the international "NWS-vs-Wunderground" divergence — the entire premise of the original HTML — **does not exist on the US-legal venue.** Weather is settlement-clean here, which also means the fat spread is gone.
3. **Crypto is absent** from polymarket.us's open catalog → no US-legal crypto pair (it exists only on international Poly + Kalshi).

## polymarket.us open catalog (4,510 live markets)
sports 4,164 · culture 159 · politics 103 · **climate/weather 48** · **macro/econ 36** · (crypto: none)

Structure: every polymarket.us market is a **binary Yes/No threshold** ("≤ 85°F", "CPI ≤ 3.7%", "U-3 ≥ 4.6%"), grouped into families; taker fee coefficient **0.05**, maker rebate −0.0125, min qty 1.

---

## The overlap audit (by category, ranked by arb-quality)

### 1. ECON / MACRO — CLEAN, US-legal, the prize subset
36 open polymarket.us markets · 5 families · all same-government-source as Kalshi.

| polymarket.us family | Source | Kalshi series | Kalshi source | Same #? |
|---|---|---|---|---|
| CPI YoY (e.g. ≤3.7%, May'26) | BLS | `KXCPIYOY`, `KXCPICOREYOY` | Bureau of Labor Statistics | ✅ |
| Unemployment U-3 (≥4.6%, Jun) | BLS Employment Situation | `KXU3`, `KXECONSTATU3` | Bureau of Labor Statistics | ✅ |
| GDP SAAR (≥2.0%, Q2 Advance) | BEA Advance Estimate | `GDP`, `GDPUSMAX/MIN` | Bureau of Economic Analysis | ✅ (confirm advance basis) |
| Nonfarm Payrolls (≥250k, Jun) | BLS Employment Situation | `KXPAYROLLS`, `PAYROLLS` | BLS | ✅ |
| Fed decision (hold, Jun FOMC) | Federal Reserve | `FEDDECISION`, `FED` | Federal Reserve | ✅ |

- **Why it's the best:** identical deterministic public number on both sides → zero settlement-source risk; the only residual checks are threshold/period alignment and first-print-vs-revised (GDP: both must use the Advance Estimate).
- **The catch:** episodic (monthly/quarterly around the release calendar), and both venues price the same consensus — so a capturable spread appears mainly **pre-release on genuine disagreement** or from liquidity imbalance, not continuously.
- **Execution note:** polymarket.us uses `≤ threshold` ladders; Kalshi uses range buckets and/or its own thresholds. A clean 2-leg hedge needs a matching Kalshi strike; otherwise replicate by summing buckets (more legs, more fees).

### 2. WEATHER / CLIMATE — settlement-CLEAN, but spread likely gone
48 open polymarket.us markets · 5 cities · **same NWS CLI source + same station** as Kalshi.

| City (station) | polymarket.us | Kalshi series | Both settle on |
|---|---|---|---|
| Chicago (KMDW) | `tc-temp-mdwhigh` | `KXHIGHCHI` | NWS Climatological Report **Chicago Midway** ✅ |
| Miami (KMIA) | `tc-temp-miahigh` | `KXHIGHMIA` | NWS Climatological Report **Miami** ✅ |
| Los Angeles (KLAX) | `tc-temp-laxhigh` | `KXHIGHLAX` | NWS Climatological Report ✅ (confirm KLAX vs downtown) |
| San Francisco (KSFO) | `tc-temp-sfohigh` | `KXHIGHTSFO` | NWS Climatological Report San Francisco ✅ |
| NYC (KNYC/Central Park) | `tc-temp-nychigh` | `KXHIGHNY` | NWS Climatological Report ✅ (confirm Central Park) |

- **Key point:** because *both* defer to the published NWS CLI daily high (not their own window computation), they get the **identical number by construction** — even the LST-vs-local-clock window risk collapses, as long as both map the calendar label to the same CLI date. The seed example's directional risk was an artifact of international Poly's Wunderground feed, which is **not** used here.
- **Consequence:** same source ⇒ no structural divergence ⇒ the fat 24¢ spread from the HTML should **not** appear. Any edge is pure liquidity/timing, daily and small.
- **Residual checks:** station identity for LA/SF/NYC; bucket-edge ↔ threshold alignment; the calendar-date↔CLI-date mapping.

### 3. POLITICS / ELECTIONS — MOSTLY-SAFE, long-dated
103 open polymarket.us markets · 40 families ↔ Kalshi 663+ gov/senate/house/primary series.

- polymarket.us: state governor/senate/house **primaries + general**, midterm chamber control. Source: *"relevant party/government authorities"* / official election certification.
- Kalshi: `GOVPARTY*`, `CONTROLH/S`, etc. Source: *Library of Congress*, *US State Governments*, *Republican/Democratic Party*.
- **Same certified winner usually agrees** → mostly-safe. **Risk is resolution-criteria wording**, not data: primary "advance/nomination" definitions, runoff handling, and *call timing* can differ per race. Requires a **per-race rules read** before pairing. Capital locks until Nov 2026 for generals.

### 4. SPORTS — MOSTLY-SAFE on winners, thin & efficient
4,164 open polymarket.us markets ↔ Kalshi 2,133 series. Moneyline game winners + props.

- Winner is a deterministic factual outcome (official league/AP/Sportradar both) → safe, but these are **liquid, fast, sharply priced**; edges are tiny and evaporate in seconds. Props carry interpretation risk. Largest surface, lowest per-trade edge, highest operational tempo.

### 5. CULTURE / NOVELTY — THIN / NO CLEAN OVERLAP
159 open polymarket.us markets · 14 families (NBA Finals attendance/Bad Bunny, Love Island, Nobel Peace Prize, Netflix #1, Spotify Top Artist).

- Kalshi has entertainment markets but **different specifics and often different sources** (e.g., polymarket.us "Spotify Top Global Artist" settles on **Spotify**; Kalshi `KXBBARTIST` settles on **Billboard Awards**; streaming markets use **Luminate**). Rare exact twins; skip for arb.

### 6. CRYPTO — NOT US-LEGAL
Absent from polymarket.us. A Kalshi×Polymarket crypto pair exists only via international Poly (US-blocked). N/A.

---

## Fees (both legs, US-legal venues)
- **Kalshi:** taker `ceil(0.07 × N × P × (1−P))` (fee_multiplier=1 on all these series); maker = 25% of taker.
- **polymarket.us:** taker `0.05 × C × P × (1−P)`; maker rebate `−0.0125`.
- Round-trip both-taker at P≈0.5 ≈ **~3¢/contract** + spread ⇒ practical edge floor **~3–5¢**. Maker-on-both cuts the fee piece by ~75% but adds fill risk.

## Verdict / prioritization
1. **Econ (CPI/U-3/GDP/NFP/Fed)** — only structurally clean, US-legal arb. Build here first. Episodic; edge is event-driven.
2. **Weather (5 cities)** — clean settlement, daily cadence, but expected spread ≈ 0 (same source). Worth a *measurement* to confirm there's no liquidity-driven edge before dismissing.
3. **Politics** — mostly-safe, needs per-race rule audit; long capital lockup.
4. **Sports** — safe winners but efficient/thin; only viable with low-latency infra.
5. **Culture / Crypto** — skip (no clean overlap / not US-legal).

## Immediate next experiment
Pull live order books for the overlapping econ + weather families on **both** venues and measure the actual net-of-fee spread distribution. That converts "an arb is structurally possible" into "an edge of size X exists Y% of the time," which is the real go/no-go.

## Sources
Live API pulls 2026-06-08 (`scripts/enumerate_catalogs.py`, `pmus_open.py`, `match_kalshi.py`); settlement text quoted from polymarket.us market `description` fields and Kalshi `settlement_sources`. Background in `settlement-compatibility-matrix.md`, `polymarketus-catalog-settlement.md`.
