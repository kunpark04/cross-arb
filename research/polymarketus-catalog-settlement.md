# Polymarket US (polymarket.us / QCX LLC) — Catalog & Settlement Brief

**Purpose:** Determine whether a **US-legal** Kalshi ↔ Polymarket cross-venue arb exists via **polymarket.us** (the CFTC-regulated DCM), and how polymarket.us *settles* each family — because as a CFTC Designated Contract Market it uses a **deterministic rulebook**, not international Polymarket's UMA optimistic oracle.

**Context:** A US person cannot legally use international `polymarket.com` (geoblocked under the 2022 CFTC settlement). `polymarket.us`, operated by **QCX LLC d/b/a Polymarket US** (a Delaware LLC, wholly owned by QCL Quad Code USA), is a CFTC DCM (designation 2025-07-09; amended order 2025-11-25; public launch **2025-12-03**). Clearing is via **QC Clearing LLC d/b/a Polymarket Clearing**.

**Date of research:** 2026-06-08. **Confidence convention:** [VERIFIED] = quoted from a primary polymarket.us doc / CFTC filing; [PARTIAL] = supported but not from the venue's own contract spec; [INFERENCE] = my reasoning; [UNVERIFIED] = could not confirm.

---

## TL;DR (the two facts that decide everything)

1. **Does polymarket.us list the high-value families (weather / crypto / econ)?**
   - **Weather: YES — documented and live/imminent** with a dedicated rulebook FAQ. Five cities incl. **Miami (KMIA)**. [VERIFIED docs page exists]
   - **Sports: YES — live, the launch family** (Dec 2025 opened sports-first). [VERIFIED]
   - **Crypto / Econ: roadmap families, listing on the *US venue* only PARTIALLY verified.** Polymarket has publicly stated macro/econ + crypto are part of the rollout, and the API category filter is open-ended, but I could **not find a polymarket.us-specific crypto or econ contract spec / FAQ** (the `crypto-faqs` page 404s). Most "BTC $XX traded" / "CPI 105 markets" figures in search are **international polymarket.com**, not polymarket.us. Treat crypto/econ on polymarket.us as **announced/rolling-out, not confirmed-listed**. [PARTIAL / UNVERIFIED for the US venue specifically]

2. **Weather settlement source on polymarket.us — NWS or Wunderground?**
   - **NWS. [VERIFIED]** polymarket.us settles daily temperature on the **official NWS Daily Climate Report (CLI)** from the local Weather Forecast Office — **the same family of source Kalshi uses** (Kalshi uses NWS/NOAA CLI). This is the single most important finding: **polymarket.us did NOT inherit international Polymarket's Weather Underground source.** It moved weather onto the government print.

**Implication:** A US-legal weather arb against Kalshi is *plausible on settlement grounds* (both NWS CLI) — pending strike-grid and station-micrositing alignment (see §4, §6). Crypto/econ arb is blocked until polymarket.us actually lists those families.

---

## 1. Catalog — what is actually listed on polymarket.us

| Family | Status on polymarket.us | Breadth / signal | Evidence |
|---|---|---|---|
| **Sports** (winners, spread, total, props, futures) | **Listed — LIVE** (launch family, Dec 2025) | Full game + player-prop suite; the marketed flagship (e.g. `chiefs-super-bowl-lx`) | [VERIFIED] `sports-faqs`; multiple news (CoinDesk/FinanceMagnates) confirm sports-first launch |
| **Weather** (daily HIGH/LOW temp by city) | **Listed / documented** (dedicated rulebook FAQ; live or imminent) | **5 cities: NYC (KNYC), SF (KSFO), Miami (KMIA), Chicago (KMDW), LA (KLAX)** | [VERIFIED] `faqs/weather-faqs` |
| **Crypto** (BTC/ETH levels, up/down) | **Unverified on US venue** — roadmap/announced | No polymarket.us crypto FAQ found (`crypto-faqs` 404). Volume figures in search are international `.com` | [PARTIAL] roadmap statements; [UNVERIFIED] US listing |
| **Economics** (CPI, FOMC/Fed, GDP, NFP) | **Unverified on US venue** — roadmap/announced ("Macro & Economy" explicitly named as a planned vertical) | No polymarket.us econ FAQ/series found; CPI/Fed markets in search are international `.com` | [PARTIAL] roadmap; [UNVERIFIED] US listing |
| **Politics / elections** | **Unverified / rolling out** — named as planned vertical; some reports say non-sports (politics) began rolling out 2026 | — | [PARTIAL] |
| **Culture / novelty / mentions** | **Unverified** — named as planned vertical (Culture & Entertainment) | — | [PARTIAL] |

> **Key disambiguation:** polymarket.us **launched sports-first** and is expanding "to markets on everything." The **only two families with dedicated polymarket.us rulebook FAQ pages are Sports and Weather** — these are the confirmable live/near-live families. Everything else is roadmap. Do **not** assume the rich crypto/econ catalog you see on `polymarket.com` exists on `polymarket.us` yet.

---

## 2. Settlement source per family (polymarket.us rulebook)

| Family | polymarket.us resolution source | Matches Kalshi? | Evidence |
|---|---|---|---|
| **Weather (daily high/low)** | **Official NWS Daily Climate Report (CLI)** published by the local NWS Weather Forecast Office (per-city CLI product, e.g. CLINYC). Cross-checked against the **24-hour METAR**. | **YES** (Kalshi also uses NWS/NOAA CLI) | [VERIFIED] `faqs/weather-faqs` |
| **Sports** | Hierarchy: **(1) the official governing body / sanctioning organization**, then official competition records / umpire reports, then tertiary press (AP, Reuters, ESPN, BBC Sport, official league sites) | Compatible in spirit (Kalshi also uses official league results) | [VERIFIED] `faqs/sports-faqs` |
| **Crypto** | **Not published for the US venue.** (International `.com` uses Chainlink / Binance feeds — does **not** govern polymarket.us.) | Unknown | [UNVERIFIED] |
| **Econ (CPI/Fed/GDP/NFP)** | **Not published for the US venue.** Would presumably be the same govt prints Kalshi uses (BLS/BEA/Fed) — standard for a DCM — but **unconfirmed**. | Likely (inference) | [INFERENCE] / [UNVERIFIED] |

### Weather settlement — verbatim detail [VERIFIED]
- **Source:** "the official **NWS Daily Climate Report (CLI)** published by the local Weather Forecast Office."
- **Stations:** NYC = KNYC (Central Park); SF = KSFO; **Miami = KMIA (Miami Intl Airport)**; Chicago = KMDW (Midway); LA = KLAX.
- **Settlement timing / TZ:** "Settlement occurs at **8:00 AM ET** on the day following the Contract's specified date." If the CLI is inconsistent with the 24-hr METAR for the same station, settlement may be **delayed to 11:00 AM ET** for review. If no data within ~a week, contract settles at **last fair-market prices**.
- **Contract definition:** "Temperature Contracts — Event Contracts that resolve based on whether the temperature in a specified location during a specified period satisfies a specified condition relative to a specified value."

> **Micrositing caveat for the arb:** polymarket.us Miami = **KMIA (Miami Intl)**. Confirm Kalshi's Miami station — if Kalshi also reads Miami's official CLI (KMIA), the underlying observation is identical. **NYC is the classic trap:** polymarket.us uses **KNYC = Central Park**; verify whether Kalshi's NYC contract reads Central Park vs LaGuardia/KLGA — different stations = different settle even with the same "NWS CLI" wording. (Cross-reference your `kalshi-venue-audit.md` / `settlement-compatibility-matrix.md`.)

---

## 3. Resolution mechanism — UMA vs deterministic DCM rulebook

**polymarket.us uses a DETERMINISTIC, rulebook-based settlement — NOT UMA. [VERIFIED from the rulebook + CFTC structure]**

Evidence from the **Polymarket US Rulebook** (CFTC filing, Nov/Dec 2025) — defined-terms and structure:
- Contracts are **"Fully-Collateralized Contracts"**; settlement runs on a **"Payout Condition"** → **"Settlement Amount"** (fixed $ the Seller pays the Purchaser on the **Settlement Date** if the Payout Condition is satisfied at **Expiration Time**).
- **No mention of UMA, optimistic oracle, token vote, proposers, or disputers** anywhere in the definitions or chapter structure. Resolution disputes route through **Chapter VIII Dispute Resolution** + the exchange's **Markets Team / Regulatory Oversight Committee**, not a crypto-token vote.
- Clearing: **"Clearinghouse" = QC Clearing LLC d/b/a Polymarket Clearing.** Trades cleared on a CFTC-regulated clearinghouse.
- This is the expected posture: **a CFTC DCM cannot outsource settlement to a permissionless token-holder vote.** International Polymarket's UMA OO model is a `.com`-only construct.

**Bottom line:** On settlement *mechanism*, polymarket.us behaves **like Kalshi** (deterministic, designated-source, exchange-adjudicated) — **not** like international Polymarket (UMA). This removes the "UMA-subjective" risk that plagues `.com` arb for the families polymarket.us lists.

---

## 4. Bucket / strike conventions (weather + crypto)

- **Weather buckets:** **[UNVERIFIED — not in the FAQ].** The FAQ defines contracts generically ("…satisfies a specified condition relative to a specified value") and does not publish the bucket width. Search snippets describe **multi-outcome temperature *range* markets** ("Highest temperature in NYC on <date>?" with multiple buckets), consistent with a banded grid, but I **cannot confirm 2°F buckets or integer edges** from a primary polymarket.us source. **Action:** pull live `gateway.polymarket.us/v1/markets` for a temperature event and read the actual strike ladder; compare width + edge convention (e.g. ">=", open/closed boundary) against Kalshi's 2°F bands. Boundary alignment is **not yet established**.
- **Crypto strikes:** **[UNVERIFIED]** — no US-venue crypto spec found.

> This is the **second gap** (after crypto/econ listing) that must close before declaring a "clean" weather arb: same source ≠ same payoff if the bucket grids or boundary inequalities differ.

---

## 5. API / market data

- **Public REST API: YES. [VERIFIED]** Base URL **`https://gateway.polymarket.us`**; documented at `docs.polymarket.us/api-reference`. `GET /v1/markets` and `GET /v1/events` are documented with **`security: []` (no auth required for public market data)**. Filters include `categories`, `marketTypes`, `sportsMarketTypes` (moneyline/spread/total/prop/future), `tagIds`, `gameId`, `participantId`. Returns `bestBid`/`bestAsk`, tags, subject metadata. WebSocket / gRPC / FIX also documented.
- **Separate from `.com`:** This is a **distinct** API from international `clob.polymarket.com`. The rulebook defines the **"PMUS Direct System"** (proprietary order entry/execution) and a **"Trading System"** explicitly including **"direct connectivity… through API, front-end, GUI, ISV."** Order placement (authenticated) appears to use **Ed25519** auth + PMUS-specific SDKs (TypeScript/Python), per docs landing + secondary sources — **distinct from `.com`'s EIP-712/Polygon signing.** [PARTIAL on the exact auth/order-placement flow]
- **Programmatic trading:** Market data is clearly programmatic (public REST/WS). **Authenticated order placement via API appears supported** (rulebook contemplates API order entry; SDKs exist) but the precise public availability of programmatic *order placement* for retail at this stage is **[PARTIAL]** — verify by attempting key issuance post-KYC.
- **KYC / funding:** **USD.** "Deposit via **debit card or bank transfer (ACH)** through the Polymarket US app." KYC/identity verification required (standard DCM onboarding; app-based). Participant types in the rulebook include **FCM Participants** (CFTC-registered Futures Commission Merchants) and **Direct Access Participants**; retail funds sit in **Customer Accounts** at an FCM Clearing Member of QC Clearing. So funding is **USD via the regulated clearing stack (FCM/ACH/debit)** — *not* USDC-on-Polygon like `.com`. [VERIFIED funding method; PARTIAL on retail-vs-FCM account mechanics]

---

## 6. Bottom-line per family — is there a US-legal, same-settlement Kalshi ↔ polymarket.us pair?

| Family | Verdict |
|---|---|
| **Weather (daily high/low temp, incl. Miami)** | **US-legal AND same-settlement-source (both NWS CLI) — strongest candidate.** Mechanism is deterministic on both venues (no UMA). **BUT not yet "clean"**: (a) confirm bucket grid / boundary inequality alignment [§4 UNVERIFIED], (b) confirm same station micrositing per city (Miami KMIA likely matches; **NYC KNYC=Central Park needs a Kalshi-station check**). **Classification: US-legal, same-source, pending grid/station alignment → "near-clean, verify before trusting."** |
| **Sports** | **US-legal; source-compatible** (official governing body both sides), deterministic. Arb viability is the usual sports-microstructure question (line/format matching), not a settlement-source gap. **Classification: US-legal, source-compatible (directional/structural, not a pure boundary arb).** |
| **Crypto (BTC/ETH levels, up/down)** | **NOT confirmed on polymarket.us.** Until listed, this is **not a US-legal pair** — international `.com` (Chainlink/Binance + UMA) is **US-blocked**. **Classification: not on polymarket.us (US-blocked, international only) — UNVERIFIED whether/when it lists.** |
| **Econ (CPI / FOMC / GDP / NFP)** | **NOT confirmed on polymarket.us.** If/when listed, a DCM would almost certainly use the **same BLS/BEA/Fed prints as Kalshi** (→ potential clean arb), but that is **inference, unconfirmed.** **Classification: not yet on polymarket.us (roadmap); would-be same-source — UNVERIFIED.** |
| **Politics / elections** | Roadmap/rolling out; deterministic mechanism expected. **Classification: UNVERIFIED listing; not a confirmed pair yet.** |
| **Culture / novelty / mentions** | Roadmap. **Classification: UNVERIFIED.** |

**Net:** The **only family that is simultaneously (a) confirmed-listed on polymarket.us, (b) settled on the same government source as Kalshi, and (c) deterministic (no UMA)** is **WEATHER**. That is where a US-legal version of this strategy can actually exist today — contingent on closing the bucket-grid and per-city-station checks. Crypto and econ — the other high-value families — are **not yet confirmable on the US venue**, so for those the strategy remains **international-only and therefore US-illegal** for now.

---

## Open items to close before trading

1. **Pull live `GET https://gateway.polymarket.us/v1/markets`** filtered to temperature; confirm Miami/NYC temp contracts are actually *trading* (not just documented) and read the **exact strike ladder + boundary inequality**.
2. **Cross-check each city's station vs Kalshi** (esp. NYC: polymarket.us=KNYC Central Park; verify Kalshi's NYC station). Miami KMIA vs Kalshi Miami.
3. **Confirm crypto/econ listing status on polymarket.us** directly via the API category filter (the docs enum is open-ended; the live `categories` values will reveal what truly exists).
4. **Verify programmatic order-placement availability** (Ed25519 key issuance post-KYC) vs UI-only.
5. **Liquidity reality check on the US venue specifically** — discount the `.com` volume figures; measure polymarket.us depth on temp contracts.

---

## Sources

**Primary — Polymarket US docs (`docs.polymarket.us`):**
- Weather FAQs (NWS CLI, cities/stations, 8AM/11AM ET, METAR cross-check): https://docs.polymarket.us/faqs/weather-faqs
- Sports FAQs (governing-body resolution hierarchy, market types): https://docs.polymarket.us/faqs/sports-faqs
- General FAQs (debit/ACH funding): https://docs.polymarket.us/faqs/general-faqs
- Market Integrity FAQs (pointer to integrity.polymarket.us): https://docs.polymarket.us/faqs/market-integrity-faqs
- Docs index / SDKs (REST + WebSocket; TS/Python): https://docs.polymarket.us  and  https://docs.polymarket.us/llms.txt
- Get Markets API (`gateway.polymarket.us/v1/markets`, `security: []`, category/marketType filters): https://docs.polymarket.us/api-reference/markets/get-markets.md
- Get Events API (`gateway.polymarket.us`, category field): https://docs.polymarket.us/api-reference/events/get-events.md

**Primary — CFTC filings:**
- Polymarket US Rulebook (Nov 25 / Dec 30 2025) — Fully-Collateralized Contracts, Payout Condition/Settlement Amount, QC Clearing, FCM Participants, PMUS Direct System; **no UMA**: https://www.cftc.gov/filings/orgrules/rules12302535965.pdf
- CFTC DCM registry — QCX LLC d/b/a Polymarket US (designation 2025-07-09): https://www.cftc.gov/IndustryOversight/IndustryFilings/TradingOrganizations/49571
- Amended Order of Designation (2025-11-25, intermediated access): https://www.cftc.gov/media/12806/Polymarket%20US%20Amended%20Order%20of%20Designation/download
- CFTC Letter 25-48 (No-Action, 2025-12-11): https://www.cftc.gov/csl/25-48/download

**Secondary — launch sequence / status (corroborating, not primary):**
- CoinDesk — Polymarket launches US app with CFTC green light (2025-12-03): https://www.coindesk.com/markets/2025/12/03/polymarket-launches-app-with-cftc-green-light-in-u-s-return
- Finance Magnates — US app rollout starting with sports: https://www.financemagnates.com/forex/polymarket-rolls-out-us-mobile-app-after-cftc-green-light-starting-with-sports-events/
- PRNewswire — QCEX acquisition $112M (2025-07-21): https://www.prnewswire.com/news-releases/polymarket-acquires-cftc-licensed-exchange-and-clearinghouse-qcex-for-112-million-302509626.html
- PRNewswire — Amended Order of Designation / intermediated access: https://www.prnewswire.com/news-releases/polymarket-receives-cftc-approval-of-amended-order-of-designation-enabling-intermediated-us-market-access-302625833.html
- TradingVPS — Polymarket US guide (separate `api.polymarket.us`, Ed25519): https://tradingvps.io/polymarket-us-guide/

**Reference — international `.com` (CONTRAST only; US-blocked, does NOT govern polymarket.us):**
- `.com` UMA resolution docs: https://docs.polymarket.com/developers/resolution/UMA
- `.com` crypto feeds (Chainlink/Binance RTDS): https://docs.polymarket.com/market-data/websocket/rtds
