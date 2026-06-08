# Kalshi Venue Audit — Cross-Arb Strategy Research

**Date:** 2026-06-07  
**Scope:** Comprehensive audit for Kalshi × Polymarket cross-venue arbitrage strategy  
**Status:** Research-only phase; no live trading  
**Verification note:** All facts marked [VERIFIED] were confirmed against primary sources (official Kalshi docs, CFTC filings) as of 2026-06-07. Facts marked [INFERRED] or [UNVERIFIED] should be re-checked before relying on them in production.

---

## 1. API

### 1.1 Base URLs

| Environment | REST | WebSocket |
|---|---|---|
| Production (primary) | `https://external-api.kalshi.com/trade-api/v2` | `wss://external-api-ws.kalshi.com/` |
| Production (elections alt) | `https://api.elections.kalshi.com/trade-api/v2` | — |
| Demo / sandbox | `https://external-api.demo.kalshi.co/trade-api/v2` | — |
| Demo alt | `https://demo-api.kalshi.co/trade-api/v2` | — |

**[VERIFIED]** from `openapi.yaml` servers block and WebSocket docs.  
**[FLAG — likely drift]:** The `api.elections.kalshi.com` subdomain appears to be a legacy or elections-specific URL; prefer `external-api.kalshi.com` for general use. Confirm with current docs before hardcoding.

### 1.2 Authentication — API Key + RSA-PSS Signing

All requests use **stateless per-request signing** (no session tokens). [VERIFIED]

**Setup:**
1. Generate credentials at `https://kalshi.com/account/profile` → API Keys → "Create New API Key."
2. Download the RSA private key (PEM format, non-retrievable after page close) and note the Key ID.

**Signing each request:**
```
message  = str(timestamp_ms) + HTTP_METHOD + path_without_query_params
signature = RSA-PSS-SHA256(message, private_key, salt_len=32)
b64_sig  = base64.encode(signature)
```

**Required headers on every request:**

| Header | Value |
|---|---|
| `KALSHI-ACCESS-KEY` | Key ID string |
| `KALSHI-ACCESS-TIMESTAMP` | Unix timestamp in **milliseconds** |
| `KALSHI-ACCESS-SIGNATURE` | base64-encoded RSA-PSS signature |

**WebSocket:** Same three headers sent during the HTTP handshake upgrade. [VERIFIED]

**Official Python packages:** `kalshi_python_sync` and `kalshi_python_async` (pip). Old `kalshi-python` is **deprecated**. TypeScript: `kalshi-typescript` (npm). SDKs lag the API by ~1 week (published Tuesday–Wednesday). [VERIFIED]

**OpenAPI spec:** `https://docs.kalshi.com/openapi.yaml` (294 KB; authoritative).  
**AsyncAPI spec:** `https://docs.kalshi.com/asyncapi.yaml`

### 1.3 Rate Limits

Token-bucket system with **separate Read and Write buckets** [VERIFIED]:

| Tier | Read tokens/s | Write tokens/s |
|---|---|---|
| Basic | 200 | 100 |
| Advanced | 300 | 300 |
| Premier | 1,000 | 1,000 |
| Paragon | 2,000 | 2,000 |
| Prime | 4,000 | 4,000 |

- Most operations cost **10 tokens**; cheaper ops (cancel, quote) cost less.
- Effective req/s ≈ budget ÷ 10 (e.g., Basic Read = ~20 req/s, Basic Write = ~10 req/s).
- Write bucket bursts up to **2× per-second budget** (accumulated during idle).
- Exceeds → `429 Too Many Requests`; no `Retry-After` header — use exponential backoff.
- Tier qualification: **automatic daily review** based on 30-day volume share.
- Batch requests bill **per item** (no discount from batching).
- **[FLAG]:** One older source cited "20 req/s read, 10 req/s write" for Basic — this aligns with the 200/100 token budget at 10 tokens/op. The token framing is the current official model.

### 1.4 Market Discovery Endpoints

All market-data endpoints are **publicly accessible without authentication**.

```
GET /series                                        # List all series templates
GET /series/{series_ticker}                        # Single series
GET /events?series_ticker=X&status=open&limit=1000 # Events, paginated
GET /events/{event_ticker}                         # Single event
GET /markets?series_ticker=X&status=open&limit=1000 # Markets, paginated (max 1000/page, cursor-based)
GET /markets/{ticker}                              # Single market
GET /markets/orderbooks                            # Batch orderbooks (multiple tickers)
GET /markets/{ticker}/orderbook                    # Single market orderbook
```

**[VERIFIED]** from `openapi.yaml` and quick-start docs.

**Orderbook note:** The API returns **bids only** for both YES and NO sides. Asks are implied by the reciprocal: YES ask = 1 − NO bid. This is a design choice reflecting the binary contract structure. [VERIFIED]

### 1.5 Order Types

Endpoint: `POST /portfolio/orders` [VERIFIED]

| Parameter | Values |
|---|---|
| `type` | `"limit"`, `"market"` |
| `time_in_force` | `"good_till_canceled"`, `"immediate_or_cancel"`, `"fill_or_kill"` |
| `side` | `"yes"`, `"no"` |
| `action` | `"buy"`, `"sell"` |
| `post_only` | `true`/`false` (ensures maker placement) |

**Cancel:** `DELETE /portfolio/orders/{order_id}`  
**Batch submit:** `POST /portfolio/orders/batched`  
**Batch cancel:** `DELETE /portfolio/orders/batched`  
**Amend:** `POST /portfolio/orders/{order_id}/amend`  
**Max open orders:** 200,000 per user.

---

## 2. Fee Schedule

### 2.1 Trading Fees

**Taker fee formula** [VERIFIED]:
```
taker_fee_per_contract = ceil(0.07 × N × P × (1 − P))
```
where:
- `N` = number of contracts
- `P` = contract price in dollars (range: $0.01–$0.99)
- Result in **cents**, rounded **up** (ceiling)

**Maker fee formula** [VERIFIED]:
```
maker_fee_per_contract = ceil(0.07 × 0.25 × N × P × (1 − P))
                       = ceil(0.0175 × N × P × (1 − P))
```
Makers pay **25% of taker fee** (75% discount).

**Fee examples at key price points:**

| Contract price | Taker fee/contract | Maker fee/contract |
|---|---|---|
| $0.05 | ~$0.0033 | ~$0.0008 |
| $0.20 | ~$0.0112 | ~$0.0028 |
| $0.50 (max) | $0.0175 | ~$0.0044 |
| $0.80 | ~$0.0112 | ~$0.0028 |
| $0.95 | ~$0.0033 | ~$0.0008 |

**For backtesting the arb spread:** The fee drag at the midpoint is ~3.5¢ round-trip (buy + sell both as taker at $0.50) per contract. Maker-only execution cuts this to ~0.88¢ round-trip.

**Category-specific multipliers** [INFERRED — not confirmed from primary source]:  
Some markets (especially Crypto) carry a higher multiplier (~0.08 vs 0.07 standard). Always verify the `fee_multiplier` field returned in market metadata.

**Settlement fees:** None beyond the standard trading fee. [VERIFIED]

### 2.2 Deposit / Withdrawal Fees

| Method | Deposit | Withdrawal | Speed |
|---|---|---|---|
| ACH bank transfer | Free | Free | Deposit: 3–5 days (new) / instant (verified); Withdrawal: 1–3 business days |
| Wire transfer | ~$25 (bank fee) | ~$25 (bank fee) | Same-day |
| Debit / Apple Pay / Google Pay | ~2–3% (processor) | N/A | Instant |

**[FLAG — likely drift]:** Exact wire fee depends on your bank. Kalshi does not charge its own wire fee; the cost is the receiving bank's fee. Confirm at account funding page before live trading.

---

## 3. Settlement and Resolution by Category

### 3.1 Weather / Temperature — HIGHEST PRIORITY [VERIFIED]

**Source:** NWS Climatological Daily Climate Report (CLI)  
**Station (Miami):** **KMIA** (Miami International Airport), issued by MFL (Miami forecast office) [VERIFIED]

**Observation window:**  
- Kalshi uses **Local Standard Time (LST)** year-round — NOT local clock time.
- **Outside DST (Nov–Mar):** Midnight 12:00 AM–11:59 PM EST (aligned with wall clock).
- **During DST (Mar–Nov):** **1:00 AM–12:59 AM local clock time** (i.e., midnight LST to midnight LST), which is one hour offset from the calendar day.

**Critical implication for cross-arb:** Polymarket and other venues (Robinhood, IBKR) use **Weather Underground** (midnight-to-midnight local clock time). During DST, Kalshi's window and WU's window are **offset by one hour**. This means a temperature spike between 12:00 AM–1:00 AM local clock time may affect Kalshi's settlement but NOT WU-based platforms, and vice versa for temperatures after 11:00 PM. Settlement discrepancies of 1°F+ are documented.

**Additional NWS vs. WU difference:** NWS CLI incorporates 6-hour maximum snapshots and special observations; WU uses only hourly readings. NWS can report a 1°F higher spike that WU misses. [VERIFIED from wethr.net platform comparison]

**Settlement timing:** Kalshi settles the morning after the weather event, once the final NWS CLI report is published (~08:22 UTC for KMIA). May be delayed if:
- High temperature is inconsistent with 6-hr / 24-hr METAR reports; or
- Final CLI value is lower than the preliminary report.

**Revision policy:** Data revised *after* contract expiration time is **ignored**. Revisions made between the last trading date and expiration *may* be incorporated.

### 3.2 Crypto Price Markets [VERIFIED]

**Source:** CF Benchmarks Real-Time Indexes (CFB RTIs)  
**Settlement method:** Average of 60 one-second CFB RTI prices taken at contract expiration (60-second TWAP window).  
**Rationale:** Manipulation-resistant; regulated by UK FCA; same provider used by CME.  
**WebSocket channel:** `cfbenchmarks_value` provides live CFB RTI streaming.

### 3.3 Economic Indicators (CPI, NFP, Fed Rate) [VERIFIED — source-level, contract-specific terms vary]

- **CPI / Core CPI / PCE:** Settles against the official BLS (Bureau of Labor Statistics) data release.
- **Nonfarm Payrolls (NFP):** BLS Employment Situation report.
- **Fed Rate / FOMC:** FOMC official rate decision announcement.
- **GDP:** BEA (Bureau of Economic Analysis) advance estimate.
- Each contract lists its specific source agency in the contract terms filed with the CFTC. Verify `rules` field in market metadata via API.

### 3.4 Sports [INFERRED — primary source not retrieved]

- Source agencies are specified per-contract in CFTC-filed contract terms.
- Each contract's `rules` field in the API response names the resolution source.
- AP (Associated Press) is commonly cited for game outcomes; official league statistics for props.
- **[FLAG]:** Kalshi has experienced settlement errors on NFL win totals (2024–2025); credits were issued. Monitor for operational risk.

### 3.5 Politics / Elections [VERIFIED — general framework]

- Typically settles against Associated Press calls or official certified results.
- Contract terms specify the exact determination source.

---

## 4. Market Taxonomy

### 4.1 Hierarchy

```
Series  →  Event  →  Market (Strike/Bucket)
```

| Level | Ticker example | Description |
|---|---|---|
| Series | `HIGHMIA` | Template for a recurring contract type (e.g., "Miami daily high") |
| Event | `HIGHMIA-23NOV25` | One instance of the series (a specific date) |
| Market | `HIGHMIA-23NOV25-T84` | One strike/bucket within that event (e.g., "Will it exceed 84°F?") |

**Ticker format:** `{SERIES}-{DATE}-{STRIKE_DESCRIPTOR}` — the suffix after the date encodes the bucket boundary. The exact encoding varies by contract type; parse the `subtitle` and `floor_strike`/`cap_strike` fields in the market object rather than parsing the ticker string.

### 4.2 Programmatic Enumeration of All Open Markets

```python
# Paginate through all open markets
GET /markets?status=open&limit=1000
# → returns `markets[]` + `cursor`; loop until cursor is null

# For weather specifically:
GET /series  # find series_ticker for each city
GET /markets?series_ticker=HIGHMIA&status=open&limit=1000
```

**Key market-object fields for bucket boundaries:**
- `floor_strike` — lower bound of bucket
- `cap_strike` — upper bound of bucket  
- `subtitle` — human-readable bucket label
- `yes_bid_dollars`, `yes_ask_dollars`, `no_bid_dollars`, `no_ask_dollars` — current NBBO
- `volume_fp` — total contracts traded (FixedPoint string, 2 decimals)
- `open_interest_fp` — open interest

---

## 5. Regulatory Status

- **Designation:** Designated Contract Market (DCM) [VERIFIED]
- **Regulator:** U.S. Commodity Futures Trading Commission (CFTC) [VERIFIED]
- **Eligible traders:** U.S. residents in all 50 states, age 18+. [VERIFIED as of mid-2025; see flag below]
- **KYC requirements:** Full name, DOB, SSN, residential address, government-issued photo ID. Mandatory before trading. USA PATRIOT Act compliance. [VERIFIED]
- **Exclusions:** OFAC-sanctioned individuals; SDN list. No international traders.
- **[FLAG — active legal risk]:** As of mid-2025, Nevada state courts ruled Kalshi subject to state gaming laws; NJ challenge also in progress. Appeals courts have sided with Kalshi so far. Legal status in certain states could change. Monitor before deploying capital. CFTC federal preemption argument appears to be winning in courts as of audit date.

---

## 6. Historical Data for Backtesting

### 6.1 Native Kalshi API

The API partitions data into **live** (recent 3 months) and **historical** (>3 months old) tiers:

| Endpoint | Data type |
|---|---|
| `GET /historical/cutoff` | Current cutoff timestamps |
| `GET /historical/markets` | Settled markets |
| `GET /historical/markets/{ticker}` | Single settled market |
| `GET /historical/markets/{ticker}/candlesticks` | Price candles |
| `GET /historical/trades` | All trades past cutoff |
| `GET /historical/fills` | User-scoped fills |
| `GET /historical/orders` | Completed/canceled orders |
| `GET /series/{series_ticker}/markets/{ticker}/candlesticks` | Candlesticks (live range) |
| `GET /markets/candlesticks` | Batch candlesticks |

**Candlestick granularity:** [UNVERIFIED — not confirmed from primary source; check `period_interval` parameter in openapi.yaml]  
**Orderbook snapshots:** Native API provides **no historical orderbook snapshots** — only trades and candles. [VERIFIED]  
**History depth:** Data available from Kalshi's 2021 launch. [VERIFIED via Lychee]  
**Access cost:** No documented additional charge for historical endpoints; subject to same rate-limit tiers. [VERIFIED]

### 6.2 Third-Party Data Sources

| Provider | Data type | Granularity | Notes |
|---|---|---|---|
| **Lychee** (`lycheedata.com`) | Trades, prices, metadata, outcomes | Trade-level | 36 GB, from 2021 launch; CSV/XLSX/JSON export; no-code UI; "free to explore" |
| **KalshiBackTest** (`kalshibacktest.com`) | Orderbook snapshots, trade prints, reference prices | 100 ms | BTC/crypto markets only; paid service |
| **DepthFeed** (`kalshibacktesting.com`) | Orderbook depth snapshots | Tick-level | 7 crypto assets; bulk Parquet download or REST API |
| **Apify actors** | Settled market data, candlesticks | Candle-level | Scraper-based; settlement data |

**Recommendation for weather arb backtest:** Lychee for price history + settlement data; KalshiBackTest / DepthFeed if you need crypto orderbook depth. No single source provides cross-venue (Kalshi + Polymarket) synchronized orderbook data — you'll need to build your own aligned dataset.

---

## 7. Key Findings for Cross-Arb Strategy

1. **Weather is the highest-signal arb category.** Kalshi uses NWS CLI (LST), Polymarket uses Weather Underground (local clock). During DST, the observation windows differ by 1 hour. NWS also captures 6-hr spike maximums that WU misses. Same event, genuinely different underlying data → persistent pricing divergence.

2. **Fee asymmetry favors maker-only strategies.** Maker fee = 25% of taker. At $0.50, taker round-trip cost ≈ 3.5¢; maker round-trip ≈ 0.88¢. For thin arb spreads, resting limit orders on both legs is essential to viability.

3. **No historical orderbook data natively.** For backtesting cross-venue spread dynamics, you must use third-party providers (Lychee/DepthFeed) or build a live data collector before paper-trading.

4. **Authentication is not trivial.** RSA-PSS signing on every request — plan for ~1–2 ms signing overhead per order in Python. Use the async SDK for parallel order placement.

5. **Rate limits constrain HFT.** Basic tier = ~10 writes/sec. For arb a bot needs Premier+ tier (1,000 tokens/s = ~100 writes/sec). Volume-based auto-upgrade.

6. **Regulatory risk is real but contained.** CFTC DCM status is the strongest possible US regulatory shield. State-level challenges (NV, NJ) appear to be losing in federal appeals courts, but monitor.

---

## Sources

- [Kalshi API Documentation — Introduction](https://docs.kalshi.com/welcome)
- [Kalshi API Keys & Authentication](https://docs.kalshi.com/getting_started/api_keys)
- [Kalshi Rate Limits](https://docs.kalshi.com/getting_started/rate_limits)
- [Kalshi Historical Data](https://docs.kalshi.com/getting_started/historical_data)
- [Kalshi SDKs Overview](https://docs.kalshi.com/sdks/overview)
- [Kalshi OpenAPI Specification](https://docs.kalshi.com/openapi.yaml) ← canonical
- [Kalshi AsyncAPI (WebSocket) Specification](https://docs.kalshi.com/asyncapi.yaml)
- [Kalshi API Reference — Get Markets](https://docs.kalshi.com/api-reference/market/get-markets)
- [Kalshi API Reference — Create Order](https://docs.kalshi.com/api-reference/orders/create-order)
- [Kalshi WebSocket Connection](https://docs.kalshi.com/websockets/websocket-connection)
- [Kalshi Fee Schedule (PDF)](https://kalshi.com/docs/kalshi-fee-schedule.pdf) — 429 at time of audit; fee data confirmed from secondary sources
- [Kalshi Fees Help Center](https://help.kalshi.com/en/articles/13823805-fees)
- [Kalshi Weather Markets Help](https://help.kalshi.com/markets/popular-markets/weather-markets)
- [Kalshi Crypto Markets Help](https://help.kalshi.com/en/articles/13823838-crypto-markets)
- [Kalshi How is Kalshi Regulated?](https://help.kalshi.com/en/articles/13823765-how-is-kalshi-regulated)
- [CF Benchmarks × Kalshi — Crypto Settlement](https://www.cfbenchmarks.com/blog/kalshi-leads-surging-crypto-event-contract-market-powered-by-cf-benchmarks)
- [NHIGH Contract Terms (PDF)](https://kalshi-public-docs.s3.amazonaws.com/contract_terms/NHIGH.pdf) — binary PDF, not fully parsed
- [NOWDATASNOW Contract Terms (PDF)](https://kalshi-public-docs.s3.amazonaws.com/contract_terms/NOWDATASNOW.pdf) — binary PDF, not fully parsed
- [Wethr.net — Platform Comparison: Kalshi vs Polymarket vs Robinhood vs IBKR](https://wethr.net/market-resolution) ← critical for weather window differences
- [Wethr.net — Miami KMIA Market Guide](https://wethr.net/edu/market/miami)
- [PM.wiki — Kalshi Fees Explained](https://pm.wiki/learn/kalshi-fees-explained)
- [PredictionHunt — Kalshi Fees 2026](https://www.predictionhunt.com/blog/kalshi-fees-complete-guide-2026)
- [Kalshi BackTest — Third-Party Data](https://kalshibacktest.com/)
- [Lychee Data — Kalshi Historical Data Guide](https://lycheedata.com/guides/kalshi-historical-data)
- [DepthFeed — Kalshi Orderbook Data](https://kalshibacktesting.com/)
- [Is Kalshi Legal? (PredScope)](https://predscope.com/guide/is-kalshi-legal)
- [Maker/Taker Math on Kalshi — Andrew Courtney (Substack)](https://whirligigbear.substack.com/p/makertaker-math-on-kalshi)

---

## Unverified / Items That May Drift

| Item | Status | Action needed |
|---|---|---|
| Exact fee PDF (kalshi.com/docs/kalshi-fee-schedule.pdf) | HTTP 429 at audit time — could not fetch directly | Verify formula `ceil(0.07 × N × P × (1−P))` from PDF before live use |
| Category-specific fee multipliers (e.g., Crypto > 0.07) | Inferred from third-party sources | Check `fee_multiplier` field in market API response |
| Candlestick granularity options (`period_interval` values) | Not confirmed | Check openapi.yaml `period_interval` enum |
| Sports settlement source agencies per contract | Framework verified; per-contract source not checked | Read `rules` field per market object before trading |
| Nevada / NJ legal status | Appeals courts favor Kalshi as of audit date | Monitor quarterly |
| `api.elections.kalshi.com` vs `external-api.kalshi.com` precedence | Both listed in openapi.yaml servers block | Use `external-api.kalshi.com` as primary |
| DST transition exact dates affecting KMIA observation window | Framework verified; specific 2026 transition dates | Account for Mar 8 and Nov 1, 2026 DST transitions |
