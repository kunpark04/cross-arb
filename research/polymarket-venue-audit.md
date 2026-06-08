# Polymarket Venue Audit
## Cross-Arb Strategy Research — Polymarket Side

**Audited:** 2026-06-07  
**Purpose:** Design input for Kalshi × Polymarket cross-venue arbitrage strategy  
**Status:** Research-only phase — no live trading  

> **Drift Warning:** This document reflects the state as of June 2026. Polymarket underwent a major contract migration (CLOB V2, April 28 2026) and fee structure rollout (March–June 2026). The fee regime in particular is still actively evolving — re-verify before going live.

---

## 1. APIs

### 1.1 CLOB API (Central Limit Order Book)

**Base URL:** `https://clob.polymarket.com`  
**SDK Versions (V2 only — V1 is deprecated as of April 28, 2026):**
- TypeScript: `@polymarket/clob-client-v2`
- Python: `py-clob-client-v2`

#### Public Endpoints (No Auth Required)

| Endpoint | Purpose |
|----------|---------|
| `GET /book?token_id=<id>` | Full live order book snapshot (bids + asks with size) |
| `GET /books` | Batch order book for multiple tokens |
| `GET /price?token_id=<id>&side=<BUY/SELL>&amount=<n>` | Execution price estimate |
| `GET /prices` | Batch prices |
| `GET /midpoint?token_id=<id>` | Best midpoint price |
| `GET /midpoints` | Batch midpoints |
| `GET /prices-history?market=<token_id>&interval=<>&fidelity=<N>` | Historical midpoint prices |
| `GET /spread` | Bid-ask spread |
| `GET /trades` | Recent executed trades |
| `GET /markets` | Market/token metadata by condition ID or token ID |
| `GET /ok` | Health check |

**Order Book Depth (live):** `GET /book` returns full ladder (all bid/ask price levels with size). For real-time streaming, use the WebSocket market channel instead of polling — see §1.5.

#### Authenticated Endpoints (L2 Required)

| Endpoint | Purpose |
|----------|---------|
| `POST /order` | Place a new order |
| `DELETE /order` | Cancel a single order |
| `DELETE /orders` | Batch cancel |
| `DELETE /cancel-all` | Cancel all open orders |
| `DELETE /cancel-market-orders` | Cancel all orders in a market |
| `GET /orders` | List open/historical orders |
| `GET /balance-allowance` | Wallet balance and allowance check |

#### CLOB V2 Order Structure Changes (April 28, 2026)
The order struct was overhauled. Fields **removed**: `nonce`, `feeRateBps`, `taker`. Fields **added**: `timestamp`, `metadata`, `builder`. EIP-712 Exchange domain version bumped to `"2"`. All pre-migration V1 orders were wiped; V1 SDKs will not work against production.

**pUSD Collateral (V2):** USDC.e is replaced by pUSD as collateral.
- pUSD token address: `0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB` (6 decimals, Polygon)
- CTF Exchange V2: `0xE111180000d2663C0091e4f400237545B87B996B`
- Neg Risk Exchange V2: `0xe2222d279d744050d28e00520010520000310F59`

### 1.2 Authentication

Two-level authentication scheme:

#### L1 — Private Key (EIP-712)
- Signs an EIP-712 struct over the domain `ClobAuthDomain` (version 1, chain ID 137 = Polygon)
- Message fields: wallet address, server timestamp, nonce, attestation statement
- Used to: create API credentials, derive credentials, sign individual orders
- Non-custodial: private key never leaves client

#### L2 — API Key (HMAC-SHA256)
Derived from L1. Returns `apiKey`, `secret`, `passphrase`.

**Derivation:**
- Create: `POST /auth/api-key` with L1 headers
- Retrieve existing: `GET /auth/derive-api-key`

**L2 Request Headers (required on all trading endpoints):**
```
POLY_ADDRESS       # signer's wallet address
POLY_SIGNATURE     # HMAC-SHA256 of request
POLY_TIMESTAMP     # Unix timestamp
POLY_API_KEY
POLY_PASSPHRASE
```

**Signature Types (funder identity):**

| Type | Value | Use Case |
|------|-------|----------|
| EOA | 0 | Standard wallet |
| POLY_PROXY | 1 | Magic Link / Google login |
| GNOSIS_SAFE | 2 | Existing Safe multisig |
| POLY_1271 | 3 | ERC-1271 (recommended for new integrations) |

#### Proxy / Relayer Wallet Architecture
Polymarket uses a **proxy wallet + relayer** system. Gas for on-chain settlement is paid by the Polymarket relayer — standard trading is **effectively gasless** for the user despite running on Polygon. Users must approve both USDC/pUSD and outcome tokens for the CTF Exchange contract before trading.

### 1.3 Gamma API (Market Discovery / Metadata)

**Base URL:** `https://gamma-api.polymarket.com`  
**Authentication:** None required (fully public)

| Endpoint | Purpose |
|----------|---------|
| `GET /events` | List events (paginated) |
| `GET /events/slug/{slug}` | Fetch event by slug |
| `GET /markets` | List markets |
| `GET /markets?slug={slug}` | Fetch market by slug |
| `GET /markets/keyset` | Cursor-based pagination (added April 10, 2026) |
| `GET /events/keyset` | Cursor-based event pagination |
| `GET /tags` | Available market categories |
| `GET /public-search` | Text search across markets |

**Hierarchy:** Event (top-level question) → one or more Markets (binary outcome pairs)

**Slug format:** URL path segment, e.g., from `polymarket.com/event/fed-decision-in-october` → slug is `fed-decision-in-october`

**Key filters:** `active=true`, `closed=false`, `tag_id=<id>`, `order=volume24hr`, `limit`, `offset` (legacy) or cursor (new keyset endpoints)

> **Pagination note:** As of May 14, 2026, max `limit` is 100 per page. Prefer keyset pagination over offset for large enumerations.

### 1.4 Data API

**Base URL:** `https://data-api.polymarket.com`  
**Authentication:** Wallet-address-based (for user-specific data)

| Endpoint | Purpose |
|----------|---------|
| `GET /positions?user=<addr>` | Current holdings |
| `GET /trades?user=<addr>` | Trade history |
| `GET /closed-positions?user=<addr>` | Closed position history |
| User PnL endpoints | Realized/unrealized PnL |

### 1.5 WebSocket (Real-Time Streaming)

**Market channel:** `wss://ws-subscriptions-clob.polymarket.com/ws/market`  
**Auth required:** No (public)

Subscribe message:
```json
{
  "assets_ids": ["<token_id_1>", "<token_id_2>"],
  "type": "market",
  "custom_feature_enabled": true
}
```

**Message types:**

| Event | Description |
|-------|-------------|
| `book` | Full snapshot of entire order book |
| `price_change` | Incremental update to a price level (bid or ask) |
| `tick_size_change` | Tick increment change |
| `last_trade_price` | Executed trade price |
| `best_bid_ask` | Top-of-book only (requires `custom_feature_enabled`) |
| `new_market` | Market creation |
| `market_resolved` | Settlement event |

**Order book reconstruction pattern:**
1. Fetch REST snapshot via `GET /book`
2. Subscribe to WebSocket market channel
3. Apply `price_change` messages as incremental updates

**Heartbeat:** Send `PING` every 10 seconds; server replies `PONG`.

**RTDS (Real-Time Data Socket):** `wss://ws-live-data.polymarket.com` — streams crypto prices (Binance + Chainlink) and comments. Separate from market order book. Requires `PING` every 5 seconds.

### 1.6 Rate Limits (as of June 1, 2026)

All limits are sliding-window, Cloudflare-enforced (throttle/queue, not hard reject).

#### CLOB API (`https://clob.polymarket.com`)
| Endpoint Group | Limit |
|----------------|-------|
| General | 9,000 req/10s |
| GET /book, /price, /midpoint | 1,500 req/10s |
| GET /books, /prices, /midpoints | 500 req/10s |
| GET /prices-history | 1,000 req/10s |
| POST/DELETE /order | 5,000 req/10s burst; 120,000 req/10min sustained |
| POST/DELETE /orders (batch) | 2,000 req/10s burst; 21,000 req/10min sustained |
| DELETE /cancel-all | 250 req/10s burst; 6,000 req/10min sustained |
| DELETE /cancel-market-orders | 1,500 req/10s burst; 21,000 req/10min sustained |
| GET /orders, /trades, /notifications | 900 req/10s |
| GET /balance-allowance | 200 req/10s |
| POST /auth/api-key | 100 req/10s |

#### Gamma API (`https://gamma-api.polymarket.com`)
| Endpoint | Limit |
|----------|-------|
| General | 4,000 req/10s |
| GET /events | 500 req/10s |
| GET /markets | 300 req/10s |
| GET /markets + /events listing | 900 req/10s |
| GET /public-search | 350 req/10s |

#### Data API (`https://data-api.polymarket.com`)
| Endpoint | Limit |
|----------|-------|
| General | 1,000 req/10s |
| GET /trades | 200 req/10s |
| GET /positions | 150 req/10s |

**WebSocket connections do not count against REST rate limits.**

---

## 2. Fees and Costs

### 2.1 International Platform (polymarket.com) — Fee Structure as of March 30, 2026

Polymarket introduced taker fees in waves starting January 2026. Prior to this, the platform was 0% maker/taker.

**Fee formula (international):**
```
fee = C × feeRate × p × (1 − p)
```
Where:
- `C` = contract quantity
- `feeRate` = category-specific constant
- `p` = share price (peaks at p=0.50)
- Fee is zero at p=0.01 and p=0.99 extremes

**Current fee rates by category (international):**

| Category | Taker feeRate | Max effective fee (at p=0.50) |
|----------|--------------|-------------------------------|
| Crypto | ~0.072 | ~1.80% |
| Finance, Politics, Tech | ~0.040 | ~1.00% |
| Weather, Economics, Culture, Other | ~0.050 | ~1.25% |
| Sports | ~0.030 | ~0.75% |
| Geopolitical / World Events | 0 | **0% (fee-free)** |

> **Drift flag:** The phased rollout was still expanding as of this research date. The exact `feeRate` constants per category should be re-verified against the current `help.polymarket.com/en/articles/13364478-trading-fees` article before any live trading.

**Maker rebates:** 25% of all collected taker fees (20% for crypto) are redistributed to liquidity providers daily. Maker limit orders pay 0% taker fee (fees only apply when you are the taker/aggressor).

**Sell order treatment:** Sell orders are currently exempt from taker fees on the international platform. Fees are collected in shares on buys and in pUSD on sells.

**Fee floor:** Minimum fee is 0.0001 pUSD; amounts below this round to zero.

### 2.2 Polymarket US (polymarket.us / QCX LLC) — Fee Structure

**Effective:** April 3, 2026

**Formula:**
```
Fee = Θ × C × p × (1 − p)
```

| Role | Theta (Θ) | Max fee at p=0.50 |
|------|-----------|-------------------|
| Taker | 0.05 | $1.25 per 100 contracts |
| Maker rebate | -0.0125 | -$0.31 per 100 contracts (25% rebate) |

**Volume rebate program:** Takers exceeding $250,000 volume (May 15–June 30, 2026) receive 30% taker fee reduction.

### 2.3 Gas Costs on Polygon

- **User-facing gas cost: ~$0 (effectively zero)** — Polymarket's relayer pays all Polygon gas on the user's behalf for order placement, cancellation, and CTF operations
- **Polygon native gas:** Typically < $0.01 per transaction (MATIC/POL), but this is absorbed by the relayer
- **Deposit from Polygon USDC:** Gas < $0.01 (user bears this if sending from external wallet)
- **Deposit via fiat on-ramp (MoonPay):** 2–3% fee (e.g., $20–30 on $1,000)
- **Withdrawal to Ethereum mainnet:** $5–20 gas depending on ETH gas prices
- **Withdrawal to Polygon:** Near-zero

### 2.4 Deposit and Withdrawal Flow

**Proxy Wallet Architecture:**
1. User connects wallet (MetaMask, Magic Link, or Coinbase Wallet)
2. Polymarket generates a "proxy wallet" on the user's behalf (POLY_PROXY type)
3. User deposits USDC/pUSD to the proxy wallet address on Polygon
4. The relayer executes all on-chain operations — user never signs Polygon gas transactions directly

**Optimal deposit path (lowest friction):**
- Buy USDC on a CEX → withdraw to Polygon network → deposit to Polymarket proxy wallet
- Total friction: ~$0.01 gas on Polygon

**Withdrawal:**
- From Polymarket to external Polygon wallet: near-zero
- To Ethereum mainnet: $5–20 gas
- No platform-side withdrawal fee

---

## 3. Resolution Mechanism (CRITICAL)

### 3.1 UMA Optimistic Oracle — How It Works

Polymarket uses UMA's **Optimistic Oracle** for decentralized market resolution. This is the primary source of **dispute/subjectivity risk** vs. deterministic data feeds.

**Process:**

1. **Propose:** After market end date, any participant posts a $750 pUSD bond and proposes the winning outcome
2. **2-Hour Challenge Window:** Anyone can dispute by posting an equal $750 counter-bond
3. **No Dispute → Resolution:** Market resolves immediately; proposer's bond returned + reward
4. **Dispute → Second Round:** A second proposal is submitted; another 2-hour window opens
5. **Second Dispute → DVM:** Escalates to UMA's **Data Verification Mechanism** — UMA token holders vote

**DVM Vote Timeline:**
- 24–48 hour evidence debate period (UMA Discord: `#evidence-rationale`, `#voting-discussion`)
- ~48 hour voting period
- Bond distribution: winner takes bond + 50% of loser's bond
- Edge case: "Too Early/Unknown" vote → $0.50 per token (50/50 split); disputer retains advantage

**Total timeline:**
- Undisputed: ~2 hours
- One dispute round: ~4–6 hours
- DVM escalation: 4–6 days

**Dispute risk signal:** Polymarket logged >1,150 disputed markets in 2026 YTD (already exceeding full-year 2025 total). A high-profile $60M+ dispute over a Bitcoin sale assertion tested UMA's DVM in 2026.

### 3.2 Subjectivity / Dispute Risk vs. Deterministic Feeds

**Key design risk for cross-arb strategy:** Polymarket resolutions are not deterministic at time of trade. Unlike an exchange with a rules-based data feed:
- A proposer can be incorrect, triggering a multi-day dispute
- Market prices may not converge to true value if a dispute is anticipated
- Weather markets have relatively clear resolution criteria (§3.3) but are still subject to the 2-hour window and potential escalation
- Positions are locked until resolution completes; capital efficiency is impacted during dispute periods
- Resolution disputes can cause divergence from Kalshi prices even when both markets reference the same underlying event

### 3.3 Weather Markets — Specific Resolution Criteria

**VERIFIED from live market rules (Miami, May 30, 2026):**

> "Settlement is determined by the highest temperature recorded at the Miami Intl Airport Station in degrees Fahrenheit, sourced from **Weather Underground** (wunderground.com/history/daily/us/fl/miami/KMIA)"

**Station:** KMIA — Miami International Airport  
**Data Source:** Weather Underground (NOT NWS CLI directly)  
**Temperature Precision:** Whole degrees Fahrenheit only  
**Resolution Timing:** After the first data point for the following day is published on Weather Underground; revisions accepted until that cutoff  
**Fallback:** Markets cannot resolve until data is finalized; if no data publishes within 7 days, settlement at prevailing market prices

**Temperature range buckets (11 buckets, Miami example):**
- 75°F or below
- 76–77°F, 78–79°F, 80–81°F, 82–83°F, 84–85°F, 86–87°F, 88–89°F, 90–91°F, 92–93°F
- 94°F or higher

### 3.4 CRITICAL: Kalshi vs. Polymarket Resolution Divergence

This is the most important finding for cross-arb design:

| Factor | Polymarket | Kalshi |
|--------|-----------|--------|
| **Data Source** | Weather Underground (hourly METARs + SPECIs only) | NWS Daily Climate Report (CLI) |
| **Station (Miami)** | KMIA (Weather Underground history page) | KMIA (NWS official) |
| **Data products used** | Hourly METARs, special METARs | Hourly METARs, 6-hr highs, daily summaries, 1-minute observations |
| **Trading day window (summer)** | 12:00 AM – 11:59 PM local time | 1:00 AM – 12:59 AM local time (LST year-round, during DST this shifts) |
| **Resolution mechanism** | UMA Optimistic Oracle (dispute risk) | Deterministic data feed rule |

**Practical impact:** NWS CLI occasionally reports a daily high 1°F (sometimes more) higher than Weather Underground for the same station/day, because NWS incorporates additional observation products that Weather Underground omits. In 2°F-wide bucket markets, this 1°F discrepancy can cause **both venues to resolve differently for the same physical event** — a genuine structural basis for arbitrage but also a resolution divergence risk.

**The day-window mismatch adds further risk:** During DST months (March–November), Kalshi's observation window starts at 1:00 AM local time while Polymarket's starts at midnight. A temperature spike between midnight and 1:00 AM would count on Polymarket but not on Kalshi.

---

## 4. Market Taxonomy and Structure

### 4.1 CTF (Conditional Token Framework)

Polymarket uses the Gnosis **Conditional Token Framework** — all outcomes are ERC-1155 tokens.

**Hierarchy:**
```
Event (slug) → Market (binary pair) → YES token + NO token (ERC-1155 positions)
```

**Multi-market events:** A single event (e.g., "temperature range") has multiple Markets as mutually exclusive binary outcomes. Each temperature bucket is its own Market with a YES/NO token pair. These are linked via `neg_risk` mechanics.

**Neg Risk Markets:** For categorical/range markets where exactly one outcome resolves YES, Polymarket uses the **Neg Risk CTF Exchange** (`0xe2222d279d744050d28e00520010520000310F59`). This contract allows conversion between NO tokens of different outcomes. Weather temperature range markets use neg_risk.

### 4.2 Identifier Hierarchy

```
conditionId = keccak256(oracle, questionId, outcomeSlotCount)
  where outcomeSlotCount = 2 for binary markets
  
collectionId_YES = keccak256(parentCollectionId=0, conditionId, indexSet=1)
collectionId_NO  = keccak256(parentCollectionId=0, conditionId, indexSet=2)

tokenId_YES = keccak256(collateralToken, collectionId_YES)
tokenId_NO  = keccak256(collateralToken, collectionId_NO)
```

**In practice:** Do not compute manually — look up token IDs directly via Gamma API. The `/markets` response includes `clobTokenIds` (array of [YES_token_id, NO_token_id]).

### 4.3 Programmatic Market Enumeration (Weather Markets)

```
# Step 1: Get the weather tag ID
GET https://gamma-api.polymarket.com/tags
# → find tag with label "Weather" or slug "weather"

# Step 2: Enumerate active weather markets
GET https://gamma-api.polymarket.com/events?active=true&closed=false&tag_id=<weather_tag_id>&limit=100

# Step 3: Extract condition ID and token IDs from each market object
# Each market has: conditionId, clobTokenIds[YES, NO], slug, endDateIso

# Step 4: Use token IDs to query live order book
GET https://clob.polymarket.com/book?token_id=<YES_token_id>
```

**Key market fields from Gamma API:**
- `conditionId`: Condition identifier (32-byte hex)
- `clobTokenIds`: Array of two token IDs [YES, NO]
- `question`: Market title text
- `endDateIso`: Resolution date
- `active`: Whether tradable
- `neg_risk`: Boolean (true for categorical/range markets)
- `slug`: URL slug for the market

---

## 5. US Access — Legal and Regulatory Status

### 5.1 The 2022 CFTC Settlement

In January 2022, the CFTC settled charges against **Blockratize, Inc. d/b/a Polymarket** for:
- Offering off-exchange event-based binary options contracts without proper registration
- Failure to register as a Designated Contract Market (DCM) or Swap Execution Facility (SEF)

**Settlement terms:**
- $1.4 million civil monetary penalty
- Cease and desist from violating the Commodity Exchange Act
- Implement geoblocking of US IP addresses
- Prohibit US persons in Terms of Service
- Wind down all non-compliant markets

### 5.2 Current Status (June 2026): Two Distinct Platforms

**Platform 1 — International Polymarket (polymarket.com):**
- Runs on Polygon blockchain; settles in pUSD (USDC-backed)
- Geoblocked for US IP addresses (2022 settlement obligation, still enforced)
- US persons prohibited by ToS
- Thousands of markets; no KYC required globally
- **US legal status: Prohibited for US persons under CFTC settlement and ToS**

**Platform 2 — Polymarket US (polymarket.us / QCX LLC):**
- CFTC-regulated **Designated Contract Market (DCM)** — authorized November 2025 via Amended Order of Designation
- Launched December 3, 2025
- Polymarket acquired QCEX (CFTC-licensed exchange + clearinghouse) for $112M in 2025
- Requires full KYC: government ID, SSN, proof of residency, live selfie
- Settles in USD via approved FCMs — NOT crypto/blockchain
- Operates under QCX LLC d/b/a Polymarket US
- **US legal status: Fully authorized for US persons**

### 5.3 Design Constraints for Cross-Arb Strategy

| Constraint | Impact |
|------------|--------|
| International polymarket.com is geoblocked for US IPs | Any US-based bot targeting the international platform needs to address IP origin — this is a terms violation and likely regulatory violation |
| Polymarket US (polymarket.us) is CFTC-regulated | Likely viable for US persons with KYC; but the market catalog, liquidity, and API may differ significantly from the international platform |
| Settlement mechanism differs | polymarket.us settles in USD via FCM; polymarket.com settles in pUSD on Polygon |
| The high-volume weather markets live on the international platform | Cross-arb against Kalshi (US-regulated) requires either operating on the international platform (US restriction applies) or waiting for polymarket.us market catalog to grow |

> **Legal note (neutral/factual):** US persons using the international Polymarket via VPN or other means would be violating the platform's ToS and operating counter to the 2022 CFTC settlement conditions. This is a legal constraint that must be designed around, not worked around. Any production strategy targeting US operation should be scoped to polymarket.us.

---

## 6. Historical Data for Backtesting

### 6.1 CLOB API — Price History

**Endpoint:** `GET https://clob.polymarket.com/prices-history`

**Parameters:**
- `market`: token ID (required)
- `interval`: `max`, `all`, `1m`, `1w`, `1d`, `6h`, `1h`
- `fidelity`: resampling granularity in minutes (default: 1 min)
- `startTs` / `endTs`: Unix timestamp filter

**Returns:** Array of `{t: unix_timestamp, p: float}` — **midpoint prices only, NOT full order book depth**

**Critical limitation:** The CLOB `prices-history` endpoint returns mid-price time series. It does NOT return historical bid/ask depth (ladder). You cannot reconstruct historical execution costs from this endpoint alone.

### 6.2 Order Book Depth History — Availability

**Bad news:** The CLOB `/orderbook-history` endpoint stopped returning data ~February 20, 2026. Any query after that cutoff returns empty.

**Additionally:** Polymarket migrated to new CTF Exchange contracts on April 28, 2026 (CLOB V2). The old Goldsky subgraph indexer was deprecated and no longer returns complete data for the post-migration period. The V1 subgraph is preserved at `v1-final` tag for historical pre-migration analysis only.

**For full order-book depth backtesting:** The only known source for L2 depth snapshots is third-party:

### 6.3 Third-Party Historical Data Sources

**PolymarketData (polymarketdata.co):**
- L2 order book snapshots at 1-minute resolution from August 2025 onward
- Full ladder (every resting bid/ask level at each snapshot)
- API endpoints: `/markets/{slug}/prices`, `/markets/{slug}/metrics`, `/markets/{slug}/books`
- Requires API key; freemium model (pricing not publicly disclosed)
- Covers 1M+ markets (open and resolved)
- Latency: ~50ms per endpoint

**Dune Analytics:**
- As of May 2026: Dune unified Polymarket + Kalshi prediction market data in a single dataset
- Includes hourly candlestick prices, per-fill trades, position-level data, resolved market history (4 years for Kalshi; Polygon on-chain data from late 2022 for Polymarket)
- SQL-queryable; suitable for aggregate analysis and strategy calibration
- Does NOT include live order book depth

**Goldsky / CryptoHouse:**
- Real-time streaming pipelines for on-chain Polymarket activity (trades, balances, positions)
- Partners with ClickHouse via CryptoHouse for SQL queries
- **Note:** V2 contract migration means new subgraph indexing required — re-verify coverage post-April 28, 2026

**GitHub: warproxxx/poly_data:**
- Open-source data retriever for Polymarket markets, order events, and trades
- Community-maintained; may lag API changes

**Bitquery:**
- Provides on-chain CTF Exchange data via GraphQL API
- Covers CTF Exchange V1 and V2 contract events

### 6.4 Granularity Summary

| Source | Type | Granularity | Start Date | Depth? |
|--------|------|-------------|------------|--------|
| CLOB `/prices-history` | Mid-price | 1 min (configurable) | Market creation | No |
| PolymarketData | L2 book + prices | 1 min | Aug 2025 | Yes (full ladder) |
| Dune Analytics | Per-fill trades + OHLCV | Hourly candles | Late 2022 (Polygon) | No |
| Goldsky / CryptoHouse | On-chain events | Block-level (~2s) | Late 2022 | No |
| CLOB `/orderbook-history` | L2 book | — | Deprecated Feb 2026 | Was yes |

**Conclusion:** Sub-minute L2 depth data for historical periods is only available from PolymarketData (paid, from Aug 2025). For pre-Aug 2025 backtests, use CLOB mid-price history + Dune trade data with assumed spread assumptions.

---

## 7. Key Risks and Open Questions for Strategy Design

| Risk | Severity | Notes |
|------|----------|-------|
| Resolution divergence (WU vs NWS) | HIGH | 1°F discrepancy between Polymarket (WU) and Kalshi (NWS CLI) is structural; can cause same-event different-resolution |
| Day-window mismatch during DST | HIGH | Polymarket starts at midnight; Kalshi uses LST year-round → 1-hour overlap gap Mar–Nov |
| UMA dispute delay | MEDIUM | 2-hour to 4-6 day resolution delay locks capital; high dispute rate in 2026 |
| US access restriction | HIGH | International polymarket.com is off-limits for US persons; polymarket.us has different catalog and mechanics |
| CLOB V2 SDK migration | MEDIUM | V1 integrations are broken; must use V2 SDK — verify SDK compatibility before any live code |
| Fee regime still evolving | MEDIUM | Fees introduced in waves; weather fee rate should be re-verified before live trading |
| Goldsky/subgraph deprecation | LOW (for bot) | Affects historical data access; real-time via REST+WS is unaffected |
| pUSD collateral change | LOW | pUSD is USDC-backed and on Polygon; minor operational change from USDC.e |

---

## Sources

- [Polymarket Documentation — Overview](https://docs.polymarket.com/trading/overview)
- [Polymarket Documentation — API Introduction](https://docs.polymarket.com/api-reference/introduction)
- [Polymarket Documentation — Authentication](https://docs.polymarket.com/api-reference/authentication)
- [Polymarket Documentation — Rate Limits](https://docs.polymarket.com/quickstart/introduction/rate-limits)
- [Polymarket Documentation — CLOB Methods Overview](https://docs.polymarket.com/developers/CLOB/clients/methods-overview)
- [Polymarket Documentation — Timeseries / Price History](https://docs.polymarket.com/developers/CLOB/timeseries)
- [Polymarket Documentation — Gamma API / Fetch Markets](https://docs.polymarket.com/developers/gamma-markets-api/fetch-markets-guide)
- [Polymarket Documentation — CTF Overview](https://docs.polymarket.com/trading/ctf/overview)
- [Polymarket Documentation — Resolution](https://docs.polymarket.com/concepts/resolution)
- [Polymarket Documentation — Blockchain Data Resources](https://docs.polymarket.com/resources/blockchain-data)
- [Polymarket Documentation — WebSocket Overview](https://docs.polymarket.com/market-data/websocket/overview)
- [Polymarket Documentation — Changelog](https://docs.polymarket.com/changelog)
- [Polymarket Documentation — V2 Migration](https://docs.polymarket.com/v2-migration)
- [Polymarket US Documentation — Fees](https://docs.polymarket.us/fees)
- [Polymarket US Documentation — Weather FAQs](https://docs.polymarket.us/faqs/weather-faqs)
- [Polymarket Help Center — Trading Fees](https://help.polymarket.com/en/articles/13364478-trading-fees)
- [Polymarket Help Center — Exchange Upgrade April 28, 2026](https://help.polymarket.com/en/articles/14762452-polymarket-exchange-upgrade-april-28-2026)
- [Polymarket — Miami Highest Temperature May 30, 2026](https://polymarket.com/event/highest-temperature-in-miami-on-may-30-2026)
- [Gamma API Base URL](https://gamma-api.polymarket.com/)
- [CFTC Press Release — $1.4M Penalty (Polymarket)](https://www.cftc.gov/PressRoom/PressReleases/8478-22)
- [Polymarket Acquires QCEX for $112M — PR Newswire](https://www.prnewswire.com/news-releases/polymarket-acquires-cftc-licensed-exchange-and-clearinghouse-qcex-for-112-million-302509626.html)
- [Polymarket US Amended Order of Designation — CFTC](https://www.cftc.gov/media/12806/Polymarket%20US%20Amended%20Order%20of%20Designation/download)
- [Kalshi vs. Polymarket Weather Resolution Comparison — wethr.net](https://wethr.net/market-resolution)
- [PolymarketData — Historical L2 Order Book Data](https://www.polymarketdata.co/polymarket-order-book-data)
- [Dune Analytics Unifies Polymarket + Kalshi Data — Crowdfund Insider](https://www.crowdfundinsider.com/2026/05/280950-dune-analytics-unifies-prediction-markets-data-from-polymarket-and-kalshi/)
- [Goldsky — Index Polymarket Blockchain Data](https://goldsky.com/chains/polymarket)
- [Chainstack — Polymarket API for Developers](https://chainstack.com/polymarket-api-for-developers/)
- [GitHub — Polymarket py-clob-client](https://github.com/Polymarket/py-clob-client)
- [GitHub — Polymarket CTF Exchange](https://github.com/Polymarket/ctf-exchange)
- [Phemex — Polymarket Expands Fee Structure](https://phemex.com/news/article/polymarket-expands-fee-structure-to-new-market-categories-68526)
- [The Defiant — Polymarket CFTC Settlement / US Geoblocking](https://thedefiant.io/news/defi/polymarket-settlement-cftc)
- [The Defiant — $60M Polymarket Dispute (UMA Oracle)](https://thedefiant.io/news/markets/usd85m-polymarket-dispute-over-strategy-s-may-bitcoin-sale-puts-uma-s-token-voting-oracle-on)
- [AgentBets.ai — Polymarket Rate Limits Guide (March 2026)](https://agentbets.ai/guides/polymarket-rate-limits-guide/)
- [AgentBets.ai — Polymarket WebSocket Guide (2026)](https://agentbets.ai/guides/polymarket-websocket-guide/)
- [Nautilus Trader Issue — CLOB orderbook-history deprecated](https://github.com/nautechsystems/nautilus_trader/issues/3635)
- [Backtesting Polymarket Strategies: Tools & Depth Data Problem — zenhodl.net](https://zenhodl.net/blog/backtesting-polymarket-strategies-tools-datasets-and-the-depth-data-problem)
