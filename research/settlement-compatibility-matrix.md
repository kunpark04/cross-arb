# Cross-Venue Settlement Compatibility — Kalshi vs Polymarket

**Question:** Do Kalshi and Polymarket resolve "equivalent" markets off the *same underlying reading*? If not, the cross-venue "arb" is not arbitrage — it is a directional bet on whose data feed wins, with double-loss tail risk.

**Date:** 2026-06-07 · **Status:** Primary-source research (Kalshi help center + contract terms, Polymarket docs + live market rules, UMA resolution docs, third-party comparison wethr.net/defirate). Facts vs inference flagged inline.

---

## 0. TL;DR — the single most important conclusion

**The Miami daily-high temperature "arb" is NOT a clean arbitrage. It is a directional bet on a known, recurring, *directionally-biased* source disagreement, and it should be priced as such.**

- **Same station** (KMIA, Miami Intl Airport), **but different data sources for that station:**
  - **Kalshi** settles on the **NWS final Daily Climate Report (CLI)** — meteorologist-finalized, QC'd, and **incorporates the "6-Hour High/Low" derived from 1-minute observations.**
  - **Polymarket** settles on the **Weather Underground "History" tab** (`wunderground.com/history/daily/us/fl/miami/KMIA`), which uses **only hourly + special METARs.**
- Because the CLI sees 1-minute data and Wunderground does not, **the NWS CLI will occasionally print a high that is 1°F (sometimes more) *higher* than Wunderground — and this is a regular occurrence, not a rare edge case** (wethr.net, citing the structural METAR vs 1-min difference). The disagreement is **biased upward for Kalshi**, not symmetric noise.
- Buckets are **2°F wide**, so a **1°F source gap that straddles a bucket edge flips the outcome and makes BOTH legs lose.** Add a **timezone mismatch** (Kalshi LST year-round → 1:00 AM–12:59 AM clock time during DST; Polymarket local clock 12:00 AM–11:59 PM) and you get additional flip paths from a late-night/early-morning reading landing in different "days."

**Verdict on the seed example:** the +24¢ headline edge on the 90-91°F pair is real *only* in the worlds where the two sources agree. In the (non-trivial, structurally upward-biased) subset where CLI > Wunderground by 1°F across a bucket boundary, the trade is −76¢. Once you price that tail, the temperature "arb" is **Source-risk → Directional-bet-in-disguise**, not Clean. Do not treat KMIA temperature as a true arb.

---

## 1. Seed-example deep dive — Miami daily-high temperature

### 1a. Kalshi resolution (FACT, primary + corroborated)

| Field | Value | Source |
|---|---|---|
| Station | KMIA (Miami Intl Airport), NWS office Miami **MFL** | wethr.net market guide |
| Settlement source | **NWS final Daily Climate Report (CLI)** — "the only source used for settlement is the NWS Daily Climate Report" | Kalshi Help Center — Weather Markets |
| What the CLI is | Official daily climate summary for an **ASOS** station, produced by **NWS meteorologists**, QC'd before publication; **includes 6-hr high/low from 1-min obs** | wethr.net/market-resolution |
| Daily-high window | Full calendar day **12:00 AM–11:59 PM Local *Standard* Time**, year-round. During DST this is **1:00 AM–12:59 AM local *clock* time** the next day | Kalshi Help Center; wethr.net |
| Settlement timing | Next morning, after the **final** CLI is issued | Kalshi Help Center |
| Revisions / edge cases | Determination may be delayed if (a) the high is inconsistent with 6-hr/24-hr METAR highs, or (b) the final CLI high is *lower* than a preliminary report | Kalshi Help Center |

### 1b. Polymarket resolution (FACT, primary — read directly off a live Miami market)

| Field | Value | Source |
|---|---|---|
| Station | "**Miami Intl Airport Station**" = **KMIA** (same physical station) | Polymarket market rules (live) |
| Settlement source | "**Wunderground**, … the highest temperature recorded for all times on this day for the Miami Intl Airport Station" at **`https://www.wunderground.com/history/daily/us/fl/miami/KMIA`** | Polymarket market rules (live) |
| What Wunderground "History" is | Historical **observation** data for airport stations; high/low shown on the History page = **hourly + special METARs only** (no 1-min 6-hr high) | wethr.net/market-resolution |
| Precision | "measures temperatures to **whole degrees Fahrenheit**" — that precision is used to resolve | Polymarket market rules (live) |
| Bucket assignment | Resolves to the **2°F range** that contains the highest recorded temp (…, 86-87, 88-89, 90-91, 92-93, …) | Polymarket market rules (live) |
| Daily window | **12:00 AM–11:59 PM local *clock* time**, year-round (no LST shift) | wethr.net/market-resolution |
| Resolution timing | Cannot resolve until the **first datapoint for the following date** is published; revisions counted until then | Polymarket market rules (live) |
| Oracle | UMA Optimistic Oracle wraps it, but the *named deterministic source* is Wunderground (INFERENCE: a clean Wunderground number rarely escalates to a UMA vote) | docs.polymarket.com/concepts/resolution |

### 1c. Do they match? — **NO (same station, different data pipeline).**

Three independent contamination channels, all confirmed:

1. **Data vendor / methodology (primary driver).** NWS CLI ⟂ Wunderground History. CLI ingests the ASOS **6-hour synoptic high from 1-minute observations**; Wunderground reads **hourly/special METARs**. A brief 1-minute spike between METARs is captured by CLI but *not* by Wunderground → **CLI ≥ Wunderground, biased upward**, "1°F or sometimes more," "happens regularly" (wethr.net/market-resolution — direct quote: *"NWS and Weather Underground frequently report different high and low temperatures for the same station on the same day. These are not rare edge cases…even a 1°F difference can flip the outcome."*).
2. **Timezone / day definition.** Kalshi LST (1 AM–12:59 AM clock during DST) vs Polymarket local clock (12 AM–11:59 PM). A late-night/just-after-midnight reading can fall in **different "days,"** so even with identical raw data the two venues can bucket the day's high differently. (Miami is on EDT in June, so the seed example IS in DST — this channel is live for the screenshot.)
3. **Finalization timing.** Kalshi waits for the *final* meteorologist-QC'd CLI (can be revised down from preliminary); Polymarket freezes at the first datapoint of the next day off an automated web table. Different cutoffs → occasional different finals.

### 1d. Boundary-risk quantification (INFERENCE — order-of-magnitude, not calibrated)

Setup: 2°F buckets; the source gap is **discrete** (0°F most days; +1°F for Kalshi on the spike days; rarely +2°F).

- Let **p₁** = P(CLI exceeds Wunderground by exactly 1°F on a given day). wethr.net calls this "occasional… regular," not "rare" — plausibly **~5–15%** of days for a humid, convective, marine-influenced site like Miami where brief gust-front/cumulus spikes are common. (No published frequency for KMIA specifically — *flagged uncertainty; this is the key number to measure empirically before sizing.*)
- Given a 1°F gap, it only flips a 2°F bucket when the true value sits on the **odd→even boundary** the gap crosses. With 2°F buckets, a +1°F shift moves the reading into the next bucket **whenever the Wunderground value is the *lower* of its bucket's two integers** (e.g., Wunderground 89 in the 88-89 bucket → CLI 90 in the 90-91 bucket). That is **~50%** of the in-bucket positions (1 of the 2 integers).
- So **P(both legs lose on a given pair) ≈ p₁ × 0.5 ≈ 2.5%–7.5%** per day on a boundary-adjacent pair, *conditional on holding to settlement.* On the modal bucket near the distribution's peak (exactly where the fat 90-91 / 88-89 edges live) the conditional flip prob is at the **higher** end, because the daily high is most likely to land right there.
- **Expected-value check on the 90-91 pair (cost 76¢, win +24¢, double-loss −76¢):** break-even double-loss probability = 24/(24+76) = **24%**. So as a *standalone* arb you can tolerate a fairly high mismatch rate. BUT: (i) the mismatch is **upward-biased for Kalshi**, so it does not wash out across the 88-89 (mirror) leg — it stacks adversely on whichever leg has Kalshi on the "No-it's-higher" side; (ii) liquidity, fees, and the fact that the screenshot edge exists *because the two books already disagree on the mode* (which is partly the market pricing this very source risk) all erode the cushion. **Net: the edge survives only if true p₁(boundary-straddling) ≪ 24% AND the books aren't already discounting it — neither is established.** Measure p₁ on historical KMIA CLI-vs-Wunderground before trusting the headline.

### 1e. What we'd need to verify (to convert "directional bet" → "known-size bet")

- **Historical KMIA divergence panel:** scrape final NWS CLI high vs Wunderground History high for KMIA over 1–2 years; compute the empirical distribution of the gap and the per-day boundary-flip rate. This is the single highest-value piece of due diligence and turns p₁ from a guess into a number.
- Confirm Polymarket's exact bucket edges match Kalshi's **integer** edges (seed example: middle buckets align 86-87/88-89/90-91/92-93; **tails differ** — Kalshi "85 or below / 94 or above" vs Polymarket "84-85 …" — no clean arb in the tails regardless).
- Confirm the **calendar day** is identical after the LST/DST shift for the specific date traded.

---

## 2. Settlement-compatibility matrix

Arb-safety scale: **Clean** (both settle on the *same single deterministic public number*, named by both) → **Mostly-safe** (same source, minor cutoff/precision risk) → **Source-risk** (different vendors/stations/indices for the "same" thing) → **Directional-bet-in-disguise** (different source *and* either subjective UMA resolution or a structural bias that can sink both legs).

| Category | Equivalent markets exist on both? | Kalshi resolution source | Polymarket resolution source | Same source? | Timezone / cutoff | Bucket / strike match | **Arb-safety** |
|---|---|---|---|---|---|---|---|
| **Weather — temperature (Miami & other US cities)** | Yes (daily high/low, 2°F buckets) | **NWS final CLI** (ASOS, meteorologist-QC'd, incl. 6-hr 1-min high) | **Wunderground "History" tab** (hourly METARs only) | **No** — same station, different pipeline; **CLI biased ≥ Wunderground by ~1°F, regularly** | **No** — Kalshi **LST** (1 AM–12:59 AM clock in DST) vs Poly **local clock** 12–11:59 PM | Middle buckets align; **tails differ** | **Source-risk → Directional-bet-in-disguise** |
| **Crypto — BTC/ETH price levels & up/down** | Yes (≥$X by date, daily up/down, intraday) | **CF Benchmarks Real-Time Index (BRTI)** — multi-exchange trimmed mean (excl. top/bottom 20%), 60-sec window, **ET** cutoff | **Mixed by market:** daily = **Binance BTC/USDT 1-min candle** (single exchange); 5-min = **Chainlink** multi-exchange feed; other = **UMA** | **No** — different index *and* Poly's daily markets carry **single-exchange (Binance) flash-crash tail risk** Kalshi's trimmed index resists | Both reference **ET noon** boundaries on daily markets (close-ish), but index construction differs | Strikes are exact $ levels — comparable | **Source-risk** (daily) / **Directional-bet** at the tails — a Binance-only wick can settle Poly opposite to BRTI |
| **Economic — CPI (headline/core YoY)** | Yes (thresholds, 0.1% precision) | **BLS** CPI release, **one-decimal** reported value | **BLS** CPI news release, **one-decimal** | **YES — same single number** | Release-time, same print | Thresholds on the same 0.1% value | **Clean** (best-in-class) |
| **Economic — Jobs / Nonfarm Payrolls** | Yes (job-add thresholds, beat/miss) | **BLS** Employment Situation (Source Agency: BLS) | **BLS** "Employment Situation Summary" | **YES** | Same release | Need identical headline figure & rounding | **Clean → Mostly-safe** (confirm both use *headline* NFP, same revision/first-print rule) |
| **Economic — Fed / FOMC rate decision** | Yes (hike/cut/hold, target range) | **Federal Reserve** FOMC statement | **Federal Reserve** FOMC statement / official calendar | **YES** | Decision day, deterministic | Discrete outcomes (hike/hold/cut) — identical universe | **Clean** (binary, unambiguous) |
| **Economic — GDP (quarterly growth)** | Yes (growth-rate buckets) | **BEA** (Advance Estimate) | **BEA "Advance Estimate"** — revisions after advance ignored | **YES — both pin to Advance Estimate** | Same release | Confirm identical rounding & SAAR basis | **Clean → Mostly-safe** (both explicitly use the *advance* print) |
| **Sports — game/series outcomes (NFL/NBA/MLB)** | Yes | Official league / **AP / ESPN / Sportradar**-type desks; **Outcome Review Committee**; Rule 6.3(c) can settle at last price if "unresolvable" | Official source per market (**Sportradar** for MLB, etc.); **UMA** can re-interpret | Usually same factual winner, BUT **resolution *criteria* can diverge** | Game-time deterministic for clean win/loss | Win/loss universe matches for the score itself | **Mostly-safe for clean win/loss; Source-risk/Directional for props & edge cases** (see Cardi B) |
| **Politics / Elections** | Yes | State electoral certification / AP call (Source Agency on file w/ CFTC) | Official results, BUT **UMA tokenholder vote** on disputes; clarifications binding | Same underlying result usually; **resolution-rule interpretation is the risk** | Call/certification timing can differ | Candidate/outcome universe matches | **Mostly-safe (clear results) → Directional-bet (ambiguous/"spirit of market" calls)** |

---

## 3. The structurally-safe subset (TRUE cross-venue arb candidates)

**A true arb requires BOTH venues to settle on the SAME deterministic public number that BOTH explicitly name.** Sorted by safety:

### ✅ CLEAN — both name the identical government print (these are the real arbs)
- **CPI** (headline & core YoY) — both → **BLS news release, one-decimal**. Same number, same precision, same release instant. *This is the gold standard.*
- **Fed / FOMC decision** — both → **FOMC statement**; discrete hike/hold/cut. No vendor, no station, no rounding ambiguity.
- **GDP** — both → **BEA Advance Estimate** (Polymarket explicitly ignores post-advance revisions; Kalshi uses BEA). Same first print.
- **Nonfarm Payrolls** — both → **BLS Employment Situation**. *Caveat:* confirm both grade off the **headline** number and identical first-print/revision convention before trusting.

For these, the *only* residual risks are (i) bucket/threshold-edge alignment (a market-design check, easily verified per market) and (ii) timing of when each venue grades — not a data-source disagreement. **These are where a genuine deterministic cross-venue arb lives.**

### ⚠️ CONTAMINATED — avoid or treat as directional

- **(a) Different data vendors/stations →** **Weather/temperature** (NWS CLI vs Wunderground; biased, recurring 1°F gap) and **crypto daily** (CF BRTI vs Binance single-exchange). The "same" market settles off *different numbers by construction.*
- **(b) Polymarket UMA human-resolution subjectivity vs Kalshi deterministic feed →** **sports props, politics/election edge cases, any "did X happen / count as Y" market.** UMA is explicitly **"not algorithmic or deterministic"** (Polymarket docs); ~1.5% of markets escalate to a tokenholder DVM vote, and documented reversals (Zelenskyy suit, Ukraine mineral deal, "Trump says China") show outcomes can hinge on *"spirit of the market"* vs literal reading — unpredictably. **Canonical proof of contamination: Cardi B Super Bowl performance — Kalshi invoked Rule 6.3(c) and settled at last price (~$0.26 YES); Polymarket resolved YES at $1.00. Same event, opposite payout, purely from resolution-criteria interpretation.** A cross-venue "arb" on such a market can lose both legs by construction.
- **(c) Timezone / "same calendar day" →** weather (LST vs local clock under DST) and any intraday/daily crypto where the ET-noon candle vs index window edge matters. Even with a matching nominal source, the *window* differs.

---

## 4. Boundary & timezone pitfalls (flips even when the source nominally matches)

1. **2°F weather buckets + 1°F vendor gap.** A single integer of disagreement on the high straddles a bucket edge ~50% of the time it occurs → both legs lose. The gap is **upward-biased toward Kalshi** (CLI ≥ Wunderground), so it does not diversify away across the two mirror legs (88-89 vs 90-91); it stacks against the side holding Kalshi-"No."
2. **LST vs local clock (DST).** Kalshi's day is 1:00 AM–12:59 AM clock time in summer; Polymarket's is midnight–11:59 PM. A high (or a freak post-midnight reading) near those edges can be attributed to **different calendar days**, flipping which bucket "owns" it. The Miami seed example is in **EDT** → this channel is *live*, not hypothetical.
3. **Crypto ET-noon candle vs 60-sec index window.** Kalshi's BRTI is a 60-second average around expiry; Polymarket daily reads a **discrete Binance 1-minute "Close."** Near a strike, a one-minute Binance print can settle Poly one way while the BRTI average lands the other — a flip with *no* nominal "source mismatch," just different aggregation.
4. **First-print vs revised (economic).** GDP/NFP/CPI can be revised. Both venues mostly pin to the **first/advance** print (good), but any venue that quietly grades off a revised figure, or grades at a different timestamp, creates a transient flip window around the release. **Verify the revision rule per market** — it's the one place a "clean" economic arb can still crack.
5. **Bucket-edge convention (open vs closed intervals, rounding).** "≥ 90" vs "90-91" vs ">89.5": confirm whether each venue's boundary is inclusive/exclusive and at what precision it rounds, especially for crypto strikes and temperature integers. A mismatched edge at the exact settle value is a deterministic both-lose.

---

## Sources

- Kalshi Help Center — Weather Markets (settlement = NWS final Daily Climate Report; LST window; 6-hr/METAR revision rule): https://help.kalshi.com/markets/popular-markets/weather-markets
- Kalshi NHIGH contract terms (PDF; binary — not text-extracted, but corroborates NWS CLI basis): https://kalshi-public-docs.s3.amazonaws.com/contract_terms/NHIGH.pdf
- wethr.net — *Platform Differences: Kalshi vs Polymarket vs Robinhood vs IBKR* (NWS CLI vs Wunderground "History"; 6-hr 1-min vs hourly METAR; "frequently report different… not rare edge cases… 1°F can flip"; LST vs local-clock windows): https://wethr.net/market-resolution
- wethr.net — Miami (KMIA) market guide (station KMIA, NWS office MFL; narrow 10–15°F daily range): https://wethr.net/edu/market/miami
- Polymarket — live "Highest temperature in Miami" market rules (Wunderground KMIA History URL, whole-°F precision, 2°F buckets, next-day-datapoint resolution): https://polymarket.com/event/highest-temperature-in-miami-on-may-30-2026
- Polymarket Documentation — Resolution / UMA Optimistic Oracle (proposal+$750 bond, dispute, 48h tokenholder vote, "not algorithmic or deterministic," per-market resolution source, clarifications): https://docs.polymarket.com/concepts/resolution
- defirate — *How Kalshi and Polymarket Settle Markets (and Disputes)* (Kalshi Source Agencies: BLS/BEA/Fed/NWS/CF Benchmarks/leagues; CF BRTI trimmed-20% index; UMA DVM flow & disputes; **Cardi B Super Bowl** $0.26 vs $1.00 divergence; Outcome Review Committee; Rule 6.3(c)): https://defirate.com/prediction-markets/how-contracts-settle/
- Polymarket daily BTC market rules (Binance BTC/USDT 1-min "Close," ET-noon candles): https://polymarket.com/event/bitcoin-up-or-down-on-june-3-2026
- Chainlink/Polymarket 5-min BTC settlement (Chainlink multi-exchange feed for short-window crypto): https://cryptoslate.com/polymarket-just-made-bitcoin-bets-settle-instantly-with-chainlink-upgrade/
- Kalshi CPI market (BLS, one-decimal): https://kalshi.com/markets/kxcpiyoy/inflation
- Polymarket CPI predictions (BLS news release, one-decimal): https://polymarket.com/predictions/cpi
- Kalshi GDP market (BEA): https://kalshi.com/markets/kxgdp/us-gdp-growth/kxgdp-27jan30
- Polymarket recession/GDP rules (BEA **Advance Estimate**, revisions ignored): https://sportshandle.com/polymarket-promo-code/recession/
- BLS Employment Situation (NFP source of record): https://www.bls.gov/news.release/empsit.nr0.htm

*Fact vs inference:* All resolution-source mappings, timezone windows, the 6-hr-vs-METAR mechanism, and the Cardi B divergence are **sourced facts**. The numeric **boundary-flip probability in §1d is inference/order-of-magnitude** — no published KMIA-specific CLI-vs-Wunderground frequency exists; building that empirical panel (§1e) is the key open due-diligence item.
