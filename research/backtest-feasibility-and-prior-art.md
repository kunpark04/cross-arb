# Cross-Venue Arb Backtest Feasibility & Prior Art

**Date:** 2026-06-07  
**Scope:** Kalshi vs. Polymarket cross-platform arbitrage — data availability, backtest design, and prior art survey.

---

## Part A — Backtest Feasibility: The Central Data Problem

### A1. Kalshi Historical Data

**What exists:**
- Official REST API with two tiers: **live** (last ~3 months) and **historical** (everything older, partitioned at a dynamic cutoff fetched via `GET /historical/cutoff`).
- Data types available: markets, candlesticks (OHLC with YES bid/ask), trades, and orders. Events/Series always available.
- **Candlestick granularity:** 1-minute, 60-minute (hourly), and 1440-minute (daily). Each bar includes open/high/low/close for YES bid, YES ask, and trade price, plus volume and open interest.
- **History depth:** Full history since Kalshi's 2021 launch, ~36 GB total (per Lychee). No hard cutoff on historical depth documented; the live/historical partition is a retrieval detail, not a deletion boundary.
- **Access:** Public REST API, no documented cost for data access. Rate limits: 20 reads/sec (Basic) to 400 reads/sec (Prime tier). Pagination limit of 100 items per page means bulk pulls require scripting.
- **Key limitation: no historical order-book depth.** The API returns current top-of-book quotes in live markets and OHLC candlesticks for historical markets. You cannot retrieve a full L2 ladder at a past timestamp. The `orderbook` endpoint is live-only.
- **Third-party archive:** Lychee Data aggregates Kalshi's full history into a queryable no-code interface with CSV/JSON export.

**Inference flag:** History depth of ~36 GB since 2021 is from a third-party (Lychee); Kalshi's official docs do not state a retention limit beyond the live/historical partition mechanism.

### A2. Polymarket Historical Data

**What exists:**

| Source | Data type | Granularity | Depth | History |
|--------|-----------|-------------|-------|---------|
| Official CLOB API `/prices-history` | Midpoint price | 1m, 1h, 1d, 1w | None (midpoint only) | Per-market from creation |
| Dune Analytics (unified Polymarket+Kalshi dataset, launched May 2026) | Hourly candlesticks + per-fill trades + resolved market history | 1h, trade-level | No L2 | 4 years (~2022–) |
| Goldsky (Turbo Pipelines, v2 post-April 2026 migration) | On-chain fills, matched orders, positions | Event-level | No L2 | From v2 contracts |
| PolymarketData (commercial) | Full L2 order-book snapshots | 1-minute | Full ladder (every resting bid/ask) | August 2025 onward |
| PolyData / ZenHodl archive | Tick-level snapshots with full bid/ask depth | Millisecond | Full ladder | Undisclosed start; 136M snapshots |
| `warproxxx/poly_data` (GitHub) | Markets, order events, trades | Trade-level | No L2 | On-chain (Polygon from ~late 2022) |

**Critical fact:** The free/official API gives you **midpoint prices only** — no historical depth. The CLOB API's `GET /book` endpoint returns live state, not historical snapshots.

**Note on Goldsky subgraphs:** As of April 2026, Polymarket migrated to v2 contracts. Old subgraph endpoints return incomplete/incorrect data. Use Goldsky Turbo Pipelines for post-April 2026 data.

### A3. The Synchronization Problem

**What you can align:** Both venues provide per-trade timestamps (Kalshi via trade endpoint, Polymarket via on-chain events). Hourly candlesticks for both are available on Dune (unified dataset, May 2026+). Kalshi's 1-minute candlesticks and PolymarketData's 1-minute L2 snapshots (August 2025+) can be joined on market slug + Unix timestamp.

**What you cannot align:** Full synchronized L2 books at arbitrary historical timestamps before August 2025 do not exist in any public archive. For the pre-August 2025 period you have midpoints, hourly OHLC, and trade prints — but not ladder depth.

**The synchronization gap in practice:**
- For the 2024 election period (the richest arbitrage window documented), you have hourly candlesticks from Dune and trade-level data from Polygon on-chain, but no matched-timestamp depth.
- For August 2025+, PolymarketData provides 1-minute L2 snapshots; Kalshi provides 1-minute candlesticks. These can be synchronized but at different structural granularities (Polymarket: full ladder; Kalshi: OHLC only, no resting depth).

### A4. Proposed Backtest Designs

#### Design 1 (Recommended): Snapshot-Based Edge-Survival Analysis

**Inputs required:**
- Kalshi 1-minute OHLC from historical API (full history since 2021)
- Polymarket hourly prices from Dune Analytics (4-year history) or 1-minute from official CLOB API per market

**Method:**
1. Match markets by event/resolution string (NLP fuzzy match + manual validation).
2. For each aligned timestamp, compute the raw cross-venue spread: `(1 - Kalshi_YES_mid - Polymarket_NO_mid)` where NO_mid = `1 - YES_mid`.
3. Net the spread by fees: subtract Kalshi taker fee `0.07 × P_K × (1 - P_K)` per side and Polymarket taker fee `category_rate × P × (1 - P)` per side.
4. Flag windows where net spread > threshold (e.g., 3¢, 5¢, 10¢).
5. Measure: frequency of edge, duration of persistence, magnitude distribution, and market/category breakdown.

**What this tells you:**
- How often a profitable gross spread exists and for how long
- Which market categories and price regions generate the most edge
- Fee sensitivity and breakeven spread

**What this does NOT tell you:**
- Whether you could fill at midpoint (you almost certainly cannot at any meaningful size)
- True P&L — this is an edge-existence upper bound, not a realized P&L estimate

**Honest limitation statement:** A snapshot-based analysis measures *apparent* edge at midpoint prices. It cannot model: (a) whether your order moves the book, (b) partial fills, (c) bid-ask spread consumption, (d) the gap between placing an order and actual execution while prices move. All of these shrink realized edge significantly.

#### Design 2 (For August 2025+ Only): Depth-Aware Fill Simulation

**Inputs required:**
- PolymarketData's 1-minute L2 snapshots (commercial, August 2025+)
- Kalshi 1-minute OHLC (free API)

**Method:**
1. Same market matching as Design 1.
2. On Polymarket side: use the full ladder to estimate fill price and partial fill probability at a target size (e.g., $500, $2,000, $5,000 notional).
3. On Kalshi side: use bid/ask spread from OHLC as a proxy; model fill as occurring at the ask (for buys) using observed spread. Note: Kalshi OHLC does not give you book depth — only the spread between best bid/ask per period.
4. Model leg risk: simulate a random execution delay (50ms to 5s) and assess how often the second leg moves against you by more than your net edge.

**Limitation:** Kalshi's historical data never exposes book depth. You can simulate realistic fills on the Polymarket side but must apply a conservative spread-consumption model on the Kalshi side. This is asymmetric and introduces model error.

**Recommendation: Use Design 1 for the full historical window (2022–present) to characterize when and how often edges exist. Use Design 2 only for August 2025+ to validate whether those edges survive realistic fill assumptions. Never claim realized P&L from Design 1 alone.**

### A5. Estimating Capturable Edge After All Costs

For a representative mid-probability trade at 50¢ (worst-case fee scenario):

| Cost component | Kalshi (taker) | Polymarket (taker, politics) | Total per round-trip |
|---|---|---|---|
| Platform fee | 1.75¢ per contract (formula: `0.07 × 0.5 × 0.5`) | 1.00¢ per share (`0.04 × 0.5 × 0.5`) | ~2.75¢ |
| Bid-ask spread consumption (estimated) | 1–3¢ | 1–4¢ (varies by depth) | 2–7¢ |
| Polymarket gas (Polygon) | — | ~$0.003–$0.005 per trade (negligible) | negligible |
| Leg risk (price movement in execution gap) | — | — | 0–10¢ (unbounded) |
| **Total minimum cost floor** | | | **~4.75¢ per $1 contract** |

**Implication:** A cross-venue spread must exceed ~5¢ to break even on fees + minimum spread; must exceed ~8–10¢ to cover realistic slippage. The 2024 Trump/Harris election markets showed 6-point (6¢) Kalshi-Polymarket gaps — this is at or near the cost floor. Edges below 5¢ are fee-negative before any slippage.

For makers: Kalshi maker fee is `0.0175 × P × (1-P)` (updated July 2025). At 50¢ that's ~0.44¢ — a 4x reduction. If you can provide liquidity (rest limit orders) on the Kalshi side and take on Polymarket, fee drag drops substantially. The adverse-selection risk of limit orders in a cross-venue arb context (your Kalshi order fills when the arb has already closed) is the key counterbalancing risk.

**Capital lockup cost:** Contracts held to expiry lock capital for days to months. A 3¢ edge on a 30-day contract = ~36% annualized gross return on that position — but only if it survives all costs and executes at modeled prices. Capital rotation (enter, exit when spread closes rather than holding to expiry) dramatically improves capital efficiency but requires active management.

---

## Part B — Prior Art

### B1. Open-Source Tools and Bots

| Project | Description | What it actually does |
|---|---|---|
| `ImMike/polymarket-arbitrage` (GitHub) | Python bot, 5,000+ markets, cross-platform + bundle arb + market-making | NLP text-similarity market matching, Opportunity objects with entry/exit, dry-run + live modes |
| `realfishsam/prediction-market-arbitrage-bot` (GitHub) | Educational bot Kalshi+Polymarket | Detects YES_Poly + NO_Kalshi < $1 inversions; built on pmxt unified API |
| `OctoBot-Prediction-Market` (GitHub, Drakkar-Software) | Polymarket open-source trading bot | Copy trading + arbitrage automation on crypto prediction markets |
| `PredictOS` (GitHub, PredictionXBT) | All-in-one prediction market framework | Cross-platform arb detection Kalshi/Polymarket with AI-powered matching |
| `warproxxx/poly_data` (GitHub) | Data retrieval tool | Fetches and structures Polymarket markets, order events, and trades |
| ZenHodl / zenhodl.net | Commercial data + backtest layer | 136M tick-level snapshots, conservative/volatility/depth-aware slippage models |
| PolymarketData | Commercial API + S3 | Full L2 order book, 1-minute, August 2025+; 19B+ rows |
| Lychee Data | Data aggregator for Kalshi | 36GB full history, no-code query and CSV export |

**None of these open-source bots publish live P&L logs or backtested Sharpe ratios.** The documented results are theoretical examples or anecdotes, not audited track records.

### B2. Academic and Quantitative Research

**IMDEA Networks Institute — "Unravelling the Probabilistic Forest: Arbitrage in Prediction Markets" (2025, arXiv 2508.03474)**
- Analyzed 86 million executed bids across 17,218 Polymarket conditions, April 2024–April 2025.
- Documented $40M in extracted arbitrage: $10.6M from single-condition (YES+NO ≠ $1) arb; ~$28.9M from multi-condition/rebalancing.
- The top single arbitrageur extracted $2.01M across 4,049 transactions (~$496/trade average).
- **Important nuance:** Most of this was *intra-Polymarket* (bundle pricing inefficiency), not cross-venue (Kalshi vs. Polymarket). Cross-venue arbitrage is a separate, harder problem.
- Methodology: VWAP across 950-block windows (~1 hour) to detect anomalies >2¢; LLM-based market dependency matching.
- Oracle risk documented: UMA voting concentration allowed a single whale to manipulate a resolution, causing $73K losses to counterparties.

**Key insight from the paper:** "Remarkable market inefficiency" with median single-condition arb yields of ~60¢ per dollar — but this is about mispriced probability sums *within* Polymarket, not cross-venue gap-filling.

### B3. Practitioner Writeups

**DEV.to — "How I Built a Risk-Free Arbitrage Bot for Polymarket & Kalshi" (realfishsam)**
- Documented consistent spreads of 1.5–4.5% on high-volume events.
- 2% example: buy YES at 35¢ Kalshi, NO at 63¢ Polymarket = 98¢ total, $1 payout.
- Key pitfalls encountered: price movement between API calls; incomplete fills; fiat withdrawal delays on Kalshi slowing capital rotation; engineering overhead normalizing two disparate APIs.

**Substack — "Building a Prediction Market Arbitrage Bot" (navnoorbawa)**
- Confirmed $39.6M documented extraction from Polymarket (citing IMDEA study).
- 78% of arb attempts in low-volume markets failed due to execution issues.
- Most retail-accessible opportunities yield 2–3%, which are fee-negative or barely fee-positive.
- Recommended minimum threshold: 2% net ROI after all costs.

**AhaSignals Research — "Prediction Market Arbitrage Strategies: Cross-Platform Trading"**
- Explicitly rejects the "risk-free" framing. Argues most cross-venue gaps are < 3–5¢ and do not survive the cost stack.
- Most useful reframe: treat cross-venue divergence as a *consensus disagreement signal* rather than an executable arb opportunity.

### B4. Known Pitfalls Catalog

**1. Settlement-source mismatch (highest severity)**
The single most dangerous risk. Platforms resolve using different criteria and oracles:
- Kalshi: CFTC-regulated, designated contract market. Resolution via exchange-controlled process with specific rule language.
- Polymarket: UMA Optimistic Oracle. A resolution is proposed and, if unchallenged, accepted; if disputed, UMA token holders vote.
- **Government shutdown example (2024):** Kalshi required "actual shutdown exceeding 24 hours"; Polymarket's standard was "OPM issues announcement." Same event, opposite resolutions.
- **Cardi B performance example:** Kalshi distinguished qualifying vs. non-qualifying dance; Polymarket used "consensus of credible reporting." Opposite outcomes.
- **Mitigation:** Only enter cross-venue arbs on markets where resolution criteria are explicitly verified as identical. This massively limits the universe.

**2. Execution/leg risk**
Polymarket uses off-chain matching with on-chain settlement — execution is not atomic across venues. Price can move between leg 1 and leg 2 placement:
- If leg 1 fills and leg 2 moves adversely, you have an unhedged directional position.
- In practice, the author of the DEV.to bot used sub-5-second execution windows as a soft atomicity guarantee.

**3. Liquidity and depth**
- Kalshi typically has shallower books than Polymarket in high-volume events.
- The 2024 election was an atypical liquidity event; everyday markets often show only $50–$500 max capturable profit at available depth.
- **Most edges visible in midpoint data cannot be filled at that price for any meaningful size.**

**4. Capital lockup and withdrawal friction**
- Kalshi: regulated U.S. exchange, ACH/wire withdrawals take 1–5 business days.
- Polymarket: USDC on Polygon, on-chain settlement within hours (if uncontested), but USDC-to-fiat conversion adds friction.
- Holding to expiry ties up capital for potentially weeks/months. Rotation strategies mitigate this but require active exit management.

**5. Geoblocking**
- Polymarket has geoblocked U.S. users at various points due to CFTC pressure. U.S. traders face regulatory and access uncertainty.
- Kalshi is U.S.-only (CFTC-regulated DCM), geoblocked for most non-U.S. traders.
- Any backtester or trader must account for the legal question of operating both legs from the same jurisdiction.

**6. Oracle disputes and manipulation**
- The IMDEA paper documented a March 2025 incident where a UMA whale with 25% voting power manipulated a resolution, causing $73K in losses.
- Even "correct" resolutions can be delayed: Polymarket settlement takes "minimum two hours if uncontested; days to weeks if disputed."
- During a dispute window, your Polymarket position is locked with an unknown outcome.

**7. Spread compression over time**
- The 2024 election cycle was the peak inefficiency window. Institutional capital (ICE's reported $2B investment in Polymarket) and professional market makers are compressing spreads.
- Analysts widely note the opportunity window follows the "crypto 2016–2018 trajectory" — first-mover extraction followed by rapid professionalization.

**8. Market matching accuracy**
- Automated NLP matching (text similarity) of Kalshi vs. Polymarket markets returns false positives — events that look identical but resolve on different definitions.
- The IMDEA paper's LLM-based dependency detection achieved only 81.45% validity on single-market inference; false matches in arb context mean directional exposure.

---

## Part C — Recommended Backtest Design Summary

**Phase 1 (2 weeks):** Run Design 1 (snapshot edge-survival) on:
- Data: Dune unified dataset (hourly, 4-year Kalshi+Polymarket history) for initial market matching and spread characterization.
- Supplement with Kalshi 1-minute candlesticks from API for confirmed matched markets.
- Output: frequency/duration/magnitude distribution of gross spreads; heatmap by market category and price region; fee-adjusted edge map.

**Phase 2 (1 week):** Validate on August 2025+ data using PolymarketData's 1-minute L2 snapshots (commercial). For each identified edge in Phase 1, simulate realistic fill using Polymarket ladder depth. Apply conservative Kalshi spread model (ask-side fill, half observed OHLC spread consumed).

**What the backtest can prove:** Whether a visible midpoint edge exists historically, how often, and its gross magnitude.

**What it cannot prove:** True realized P&L. The fill gap (midpoint vs. execution price) plus leg-risk exposure are structurally unquantifiable without synchronized dual-venue L2 depth, which does not exist for the pre-August 2025 period.

**Honest conclusion from prior art:** The 2024 election cycle was uniquely favorable (6-point gaps, high volume). Post-election, documented spreads are 1–4¢ in most markets, which is at or below the ~5¢ cost floor. The strategy requires either (a) superior execution infrastructure to operate at sub-2¢ net costs as a maker, or (b) selective focus on event markets with abnormally high cross-venue divergence (political announcements, major economic data releases).

---

## Sources

- [Kalshi Historical Data API — Official Docs](https://docs.kalshi.com/getting_started/historical_data)
- [Kalshi Get Historical Market Candlesticks](https://docs.kalshi.com/api-reference/historical/get-historical-market-candlesticks)
- [Lychee Data — Kalshi Historical Data Guide](https://lycheedata.com/guides/kalshi-historical-data)
- [Polymarket Get Prices History — Official API Docs](https://docs.polymarket.com/api-reference/markets/get-prices-history)
- [Polymarket Trading Fees — Official Docs](https://docs.polymarket.com/trading/fees)
- [PolymarketData — Historical L2 Order Book API](https://www.polymarketdata.co/polymarket-order-book-data)
- [Dune Analytics Unifies Polymarket and Kalshi Data (May 2026)](https://www.crowdfundinsider.com/2026/05/280950-dune-analytics-unifies-prediction-markets-data-from-polymarket-and-kalshi/)
- [Goldsky — Official Polymarket Datasets](https://goldsky.com/blog/polymarket-dataset)
- [IMDEA Paper — "Unravelling the Probabilistic Forest: Arbitrage in Prediction Markets" (arXiv 2508.03474)](https://arxiv.org/html/2508.03474v1)
- [ZenHodl — Backtesting Polymarket Strategies: Tools, Datasets, and the Depth Data Problem](https://zenhodl.net/blog/backtesting-polymarket-strategies-tools-datasets)
- [ImMike/polymarket-arbitrage — GitHub](https://github.com/ImMike/polymarket-arbitrage)
- [realfishsam/prediction-market-arbitrage-bot — GitHub](https://github.com/realfishsam/prediction-market-arbitrage-bot)
- [DEV.to — "How I Built a Risk-Free Arbitrage Bot for Polymarket & Kalshi"](https://dev.to/realfishsam/how-i-built-a-risk-free-arbitrage-bot-for-polymarket-kalshi-4f)
- [Substack — "Building a Prediction Market Arbitrage Bot" (navnoorbawa)](https://navnoorbawa.substack.com/p/building-a-prediction-market-arbitrage)
- [AhaSignals — Prediction Market Arbitrage Strategies](https://ahasignals.com/research/prediction-market-arbitrage-strategies/)
- [Kalshi Fee Schedule](https://kalshi.com/fee-schedule)
- [Kalshi Fees — Help Center](https://help.kalshi.com/en/articles/13823805-fees)
- [Maker/Taker Math on Kalshi (Andrew Courtney, Substack)](https://whirligigbear.substack.com/p/makertaker-math-on-kalshi)
- [Polymarket Fees — Polygon Gas Explained](https://docs.polymarket.com/trading/fees)
- [How Kalshi and Polymarket Settle Event Contracts](https://defirate.com/prediction-markets/how-contracts-settle/)
- [Kalshi vs. Polymarket Arbitrage — Claw Arbs](https://clawarbs.com/blog/kalshi-vs-polymarket-arbitrage/)
- [Indexing Polymarket with Goldsky](https://docs.goldsky.com/chains/polymarket)
- [PolyData — Polymarket Historical Orderbook Data](https://polydata.live/)
- [warproxxx/poly_data — GitHub](https://github.com/warproxxx/poly_data)
