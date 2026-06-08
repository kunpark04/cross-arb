# polymarket.us (QCX) API — Auth & Order-Book Access Brief

**Scope:** READ / market-data (live order book) on the CFTC-regulated US venue polymarket.us
(QCX LLC d/b/a Polymarket US), distinct from international polymarket.com.
**Date:** 2026-06-08. **Confidence legend:** ✅ verified from official `docs.polymarket.us` /
official SDK · ⚠️ inferred / single-source · ❓ unverified, confirm empirically once a key exists.

---

## TL;DR — the one thing that matters

There are **TWO separate API surfaces** on polymarket.us, with **different hosts, different
identifiers, and different auth**. Conflating them is the source of the 401 you hit.

| | **Retail / App API** | **Institutional API** |
|---|---|---|
| Public host (market data) | `https://gateway.polymarket.us` ✅ | — |
| Auth host (trading) | `https://api.polymarket.us` ✅ | `https://api.prod.polymarketexchange.com` ⚠️ (preprod: `api.preprod.polymarketexchange.com`) |
| Order-book path | `GET /v1/markets/{slug}/book` ✅ | `GET /v1/orderbook/{symbol}` ✅ |
| Keyed on | market **`slug`** ✅ | instrument **`symbol`** (e.g. `tec-nfl-sbw-2026-02-08-kc`) ✅ |
| Order-book auth | **PUBLIC, none** (`security: []`) ✅ | **Auth0 JWT**, `Authorization: Bearer`, scope `read:marketdata` ✅ |
| Trading auth | Ed25519 `X-PM-*` headers ✅ | Auth0 JWT + `x-participant-id` ⚠️ |

**Your 401** (`api.polymarket.us/.../orderbook` → `missing required API key headers`) is almost
certainly you hitting the **institutional symbol-based order-book route on the authenticated
host**, which is gated. ⚠️ **The order book you actually want is PUBLIC and needs no key at all** —
just use the retail gateway: `GET https://gateway.polymarket.us/v1/markets/{slug}/book`. ✅

> Practical implication: for read-only order-book depth you may **not need to generate an API key
> at all**. Keys are for trading/portfolio (Ed25519) or for the institutional JWT track. Confirm by
> calling the gateway book endpoint with a real slug (steps below). ❓

---

## 1. Generating API keys on polymarket.us

**Portal:** `https://polymarket.us/developer` ✅ (consistently cited by docs + official SDK guides).

**Steps (verified flow):** ✅
1. Download the Polymarket US app, create an account.
2. **Complete identity verification (KYC) — mandatory.** Docs: *"You'll be asked to verify your
   identity before you can trade or access the API."* ✅
3. Sign in to `polymarket.us/developer` using the **same** method (Apple / Google / email).
   ⚠️ Switching sign-in methods can break key access (per docs).
4. Create a new API key → you receive a **Key ID (UUID)** and a **Secret Key (base64 Ed25519
   private key)**. ✅
5. **The secret is shown ONCE** — copy it immediately. ✅

**KYC required:** ✅ Yes.
**Funding required to create read/market-data keys:** ❓ **Not stated.** Docs only mandate KYC, not
a deposit. Since the order book is public anyway, a verified-but-unfunded account almost certainly
suffices for market-data reads — but the funding gate on *key creation itself* is unconfirmed.
Confirm empirically.
**Approval / waitlist / allowlist (retail):** ❓ None documented for the retail developer portal.
**Institutional track:** ⚠️ The `read:marketdata` JWT scope implies an Auth0 client-credentials
onboarding (client_id/secret/audience) that is **not self-serve in the public docs** — no token
endpoint, audience, or signup form is published. Treat institutional access as
"contact-Polymarket / partner onboarding." ❓

---

## 2. Auth scheme

### 2a. Retail authenticated endpoints (`api.polymarket.us`) — Ed25519, NOT HMAC ✅

Three headers:

| Header | Value |
|---|---|
| `X-PM-Access-Key` | Key ID (UUID) |
| `X-PM-Timestamp`  | current time in **milliseconds** |
| `X-PM-Signature`  | base64( Ed25519_sign( message ) ) |

**Signed message (verbatim format):** `"{timestamp}{method}{path}"` — concatenated, **no
separators**, in that order. ✅ Body is **not** included in the documented sample. ❓ (POST body
inclusion unconfirmed — verify before signing order placements.)
**Timestamp tolerance:** within **30 seconds** of server time (NTP-sync your clock or you get 401). ✅
**Secret key format:** base64 string; decode and take **first 32 bytes** (`[:32]`) as the Ed25519
private-key seed. ✅

Official doc Python sample (verbatim):
```python
import time, base64, requests
from cryptography.hazmat.primitives.asymmetric import ed25519

private_key = ed25519.Ed25519PrivateKey.from_private_bytes(
    base64.b64decode("YOUR_SECRET_KEY")[:32]
)

def auth_headers(method, path):
    timestamp = str(int(time.time() * 1000))
    message = f"{timestamp}{method}{path}"
    signature = base64.b64encode(private_key.sign(message.encode())).decode()
    return {
        "X-PM-Access-Key": "YOUR_KEY_ID",
        "X-PM-Timestamp": timestamp,
        "X-PM-Signature": signature,
        "Content-Type": "application/json",
    }
```
Note: this is a **static-keypair Ed25519** scheme (no passphrase, no separate HMAC secret, no
rotating JWT). One secret seed signs every request.

### 2b. Institutional endpoints (`*.polymarketexchange.com`) — Auth0 JWT ✅

- `Authorization: Bearer <JWT>` ✅
- Scope **`read:marketdata`** required for order book / BBO. ✅
- `x-participant-id` header and KYC onboarding **NOT** required for order-book reads. ✅
  Docs: *"These endpoints only require Auth0 JWT authentication with `read:marketdata` scope. You do
  not need to provide the `x-participant-id` header"*.
- ❓ Auth0 domain, `/oauth/token` URL, client_id/secret, and `audience` are **not published**.

---

## 3. Order-book / market-data endpoints

### 3a. Retail (recommended for your use case) — PUBLIC ✅

**`GET https://gateway.polymarket.us/v1/markets/{slug}/book`** — keyed on market **`slug`**, no
auth (`security: []`), no documented query params.

Response shape (field names verbatim): ✅
```json
{
  "marketData": {
    "marketSlug": "string",
    "bids":   [ { "px": { "value": "string", "currency": "USD" }, "qty": "string" } ],
    "offers": [ { "px": { "value": "string", "currency": "USD" }, "qty": "string" } ],
    "state": "MARKET_STATE_OPEN",   // also PREOPEN, SUSPENDED, EXPIRED, TERMINATED, HALTED, MATCH_AND_CLOSE_AUCTION
    "stats": {
      "openPx": "...", "closePx": "...", "lowPx": "...", "highPx": "...",
      "lastTradePx": "...", "settlementPx": "...",
      "sharesTraded": "...", "notionalTraded": "...", "openInterest": "..."
    },
    "transactTime": "2026-..T..Z"   // ISO-8601
  }
}
```
⚠️ Returns full depth ladder (`bids`/`offers` arrays). No depth-limit query param is documented for
the retail route (unlike institutional, which has `depth`). Returned levels = whatever the book has.

**Companion retail endpoints:** ✅
- `GET /v1/markets/{slug}/bbo` — best bid/offer + lightweight stats.
- `GET /v1/markets/{slug}/settlement` — settlement px after resolution.
- `GET /v1/markets` — list/query markets. Query params include `slug[]`, `id[]`, `active`,
  `closed`, `archived`, `limit`, `offset`, `orderBy`, `orderDirection`, `categories[]`,
  `marketTypes[]`, `sportsMarketTypes[]`, `gameId`, `tagIds[]`, `volumeNumMin/Max`,
  `startDateMin/Max`, `endDateMin/Max`. ✅ This is the host you already probed that returns
  `outcomePrices` / `bestAskQuote` (top-of-book). Use it to discover the `slug`, then call
  `/{slug}/book` for depth.
- ❌ No dedicated trades / candles / OHLC REST endpoints documented on the retail surface (only the
  `stats` block embedded in book/bbo, plus WebSocket).

**Identifier resolution (answers your id-vs-marketSides[].id question):** ✅ The retail order-book
route keys on **`slug`**, NOT on market `id` and NOT on `marketSides[].id`. The market object
exposes `id` (unique market id), `slug` (URL id), and `marketSides[].id` / `marketSides[].identifier`
(per-outcome side ids), but the book endpoint takes the **slug**. ⚠️ A binary market's two sides
(YES/NO) — how `bids`/`offers` map to a specific side vs. the market as a whole — is not spelled out
in the schema; confirm against a live binary market. ❓

### 3b. Institutional — JWT-gated ✅
- `GET /v1/orderbook/{symbol}` — L2 snapshot, keyed on instrument **`symbol`**
  (e.g. `tec-nfl-sbw-2026-02-08-kc`). Query: `depth` (int, default 3, **max 10**). ✅
- `GET /v1/orderbook/{symbol}/bbo` — best bid/offer + `spread`, `midPrice`. ✅
- Prices/qty are integer (`int64`) ticks here, vs the retail `{value,currency}` money object. ✅
- Docs recommend the **gRPC market-data stream** over polling for production. ✅
- ⚠️ **slug→symbol mapping is NOT documented.** The retail market object has no `symbol` field, so
  bridging retail discovery → institutional symbol route is an open gap. ❓

---

### 3c. Real-time WebSocket (retail) — ✅ VERIFIED 2026-06-08

**Endpoints** (`docs.polymarket.us/api-reference/websocket`):
- Public market data: **`wss://api.polymarket.us/v1/ws/markets`** ✅
- Private (orders / positions / balance): `wss://api.polymarket.us/v1/ws/private` ✅

**Auth — REQUIRED** (note: *unlike* the public REST `/{slug}/book`, the WS needs a key). Same Ed25519
scheme as REST — `X-PM-Access-Key` / `X-PM-Timestamp` / `X-PM-Signature` on the connection
**handshake**. ✅ Empirically the endpoint is live and auth-gated: an unauthenticated upgrade returns
`401 unauthorized: valid API key authentication required` (`scripts/probe_pmus_ws.py`). ✅
✅ **CONFIRMED 2026-06-08** (`scripts/probe_pmus_ws_auth.py`): the WS upgrade signs the **same REST
string** `"{ts}GET/v1/ws/markets"` → `101 Switching Protocols`. The **camelCase** subscribe envelope
(`requestId` / `subscriptionType` / `marketSlugs`) works; frames carry `marketData.{bids,offers}`
exactly like the REST book, with `state: MARKET_STATE_OPEN`. In practice frames repeat **without** an
`eof:true` marker — treat each `marketData` frame as the latest book snapshot.

**Subscribe** (keys on **`slug`**, same as the REST book): ✅
```json
{"subscribe": {"requestId": "md-sub-1",
  "subscriptionType": "SUBSCRIPTION_TYPE_MARKET_DATA",
  "marketSlugs": ["<slug-1>", "<slug-2>"]}}
```
Unsubscribe: `{"unsubscribe": {"requestId": "md-sub-1"}}`. Subscribe replies with a snapshot
terminated by an `eof: true` marker, then streams live deltas.

**Channels** (`subscriptionType`): ✅
- `SUBSCRIPTION_TYPE_MARKET_DATA` — full order book + stats (**the one the monitor uses**)
- `SUBSCRIPTION_TYPE_MARKET_DATA_LITE` — lightweight price only
- `SUBSCRIPTION_TYPE_TRADE` — real-time trade prints (separate channel from book)

**Update frame shape** = **identical to the REST book**: `marketSlug`, `bids[]{px,qty}`,
`offers[]{px,qty}`, `state`, `stats`, `transactTime`. → the existing book parser + edge calc reuse
unchanged. ✅

**Limits:** ≤ **100 markets per subscription** ✅ (our ~200 co-listed universe → shard across ≥2 subs);
respond to server heartbeats / keep-alive; the 20 req/s key cap applies to connection setup, not
streamed frames. Architecture decision: [decisions/0005](../decisions/0005-dual-stream-persistence-monitor.md).

⚠️ **Market-discovery gotcha (verified 2026-06-08):** the REST `?active=true` filter returns **stale
backfilled** markets (e.g. Nov-2025 NFL games with `state=null`). To list **currently-open** markets
use `?closed=false&archived=false`; for today's weather use `?categories[]=climate&closed=false`.

Sources: `docs.polymarket.us/api-reference/websocket/{overview,markets,private}.md`.

## 4. Rate limits & scopes

- **Global: 20 req/s per API key** (authenticated) and **20 req/s per IP** (public). ✅
- `429 Too Many Requests` on breach; back off ≥1s then exponential. ✅
- No documented per-endpoint cap on the retail surface. ✅ Docs push WebSocket for real-time.
- **Special scope for market-data read?** Retail: **none** — it's public. ✅ Institutional: **yes**
  — Auth0 scope **`read:marketdata`**. ✅
- ⚠️ Earlier generic web guides cited "60 req/min" — that's international polymarket.com, **not**
  the US venue. Use 20 req/s (US docs).

---

## 5. SDKs / OpenAPI

- **Official Python SDK:** `pip install polymarket-us` → repo
  `github.com/Polymarket/polymarket-us-python`. ✅
  Init: `PolymarketUS(key_id="...uuid...", secret_key="...base64 Ed25519...", timeout=30.0,
  max_retries=2)`. Order book: **`client.markets.book("<slug>")`** (takes a **slug**). ✅ Public
  reads (`client.markets.list()`, `client.markets.book(...)`) work without credentials. ✅
  ⚠️ Default host constant not surfaced in README (assume `gateway`/`api.polymarket.us`); env
  selector (prod/preprod) not exposed in the public init signature. ❓
- **Official TypeScript SDK:** quickstart present
  (`docs.polymarket.us/api-reference/sdks/typescript/quickstart`); usage:
  `new PolymarketUS(); client.markets.book('chiefs-super-bowl')`. ✅
- **OpenAPI specs (US-specific, NOT international clob-client):** ✅
  - Retail markets: `https://docs.polymarket.us/api-reference/oapi-schemas/markets-schema.json`
  - Institutional order book: `https://docs.polymarket.us/institutional/oapi-schemas/orderbook-schema.json`
- **Doc index for agents:** `https://docs.polymarket.us/llms.txt` ✅

---

## Steps to get live order-book depth (do this first — likely NO key needed)

1. **Discover a slug:** `GET https://gateway.polymarket.us/v1/markets?active=true&limit=20` → read
   `slug` from a market you care about. (Public.) ✅
2. **Pull depth:** `GET https://gateway.polymarket.us/v1/markets/{slug}/book` → `marketData.bids[]`
   / `marketData.offers[]`, each `{ px:{value,currency}, qty }`. (Public, no headers.) ✅
3. If you specifically need the **institutional symbol route** (`/v1/orderbook/{symbol}` with
   `depth`), you must obtain an **Auth0 JWT with `read:marketdata`** and send
   `Authorization: Bearer …` — and you'll need Polymarket to provision Auth0 client creds (not
   self-serve). ⚠️❓

## Steps to generate an API key (only needed for trading, or the Ed25519 retail private endpoints)

1. Install app → create account.
2. Complete **KYC** (mandatory). ✅
3. Go to **`https://polymarket.us/developer`**, sign in with the same method. ✅
4. **Create API key** → save **Key ID (UUID)** + **Secret Key (base64 Ed25519)** — shown once. ✅
5. Sign requests with `X-PM-Access-Key` / `X-PM-Timestamp` / `X-PM-Signature` over
   `"{timestamp}{method}{path}"`, ≤30s clock skew. ✅
6. ❓ Confirm whether key creation needs a funded account (undocumented).

---

## Sample requests

**A) Public order-book depth (retail gateway — the read path you want):** ✅
```bash
# 1) find a slug
curl "https://gateway.polymarket.us/v1/markets?active=true&limit=5"

# 2) full bid/ask ladder with size — NO auth headers
curl "https://gateway.polymarket.us/v1/markets/<SLUG>/book"
```
```python
import requests
slug = "<SLUG>"                      # from /v1/markets
r = requests.get(f"https://gateway.polymarket.us/v1/markets/{slug}/book", timeout=10)
md = r.json()["marketData"]
bids = [(b["px"]["value"], b["qty"]) for b in md["bids"]]
asks = [(a["px"]["value"], a["qty"]) for a in md["offers"]]
print("bids", bids[:5]); print("asks", asks[:5])
```

**B) Authenticated retail request (Ed25519 — pattern for when you do need a key):** ✅ (header
construction) / ❓ (whether any read endpoint requires it)
```python
import time, base64, requests
from cryptography.hazmat.primitives.asymmetric import ed25519

KEY_ID = "your-key-id-uuid"
SECRET = "your-base64-ed25519-secret"
priv = ed25519.Ed25519PrivateKey.from_private_bytes(base64.b64decode(SECRET)[:32])

def auth_headers(method, path):
    ts = str(int(time.time() * 1000))
    sig = base64.b64encode(priv.sign(f"{ts}{method}{path}".encode())).decode()
    return {"X-PM-Access-Key": KEY_ID, "X-PM-Timestamp": ts,
            "X-PM-Signature": sig, "Content-Type": "application/json"}

path = "/v1/portfolio/positions"     # example private path
resp = requests.get("https://api.polymarket.us" + path, headers=auth_headers("GET", path))
print(resp.status_code, resp.text[:300])
```

**C) Institutional order book (JWT, symbol-keyed — the host that 401'd you):** ✅ (shape) / ❓ (token)
```bash
curl "https://api.prod.polymarketexchange.com/v1/orderbook/<SYMBOL>?depth=10" \
  -H "Authorization: Bearer <AUTH0_JWT_WITH_read:marketdata>"
```

---

## Open items to confirm empirically (once you have an account/key)
1. ❓ Does the public gateway `/{slug}/book` actually return full depth (multiple levels) live, or
   collapse to top-of-book? (Schema says array; verify level count.)
2. ❓ For a binary YES/NO market, how do `bids`/`offers` map to a side — is the book per-`slug`
   (whole market) or do you need a side identifier? Check against a real binary market.
3. ❓ Does retail API-key creation require a funded account, or is KYC-only enough?
4. ❓ Is the body included in the Ed25519 signed string for POSTs? (Sample shows only ts+method+path.)
5. ❓ Auth0 token mechanics for the institutional `read:marketdata` scope (domain, audience,
   client-credentials) + how to map a retail `slug` to an institutional instrument `symbol`.

---

## Sources
- Polymarket US Authentication — https://docs.polymarket.us/api-reference/authentication
- Markets API Overview — https://docs.polymarket.us/api-reference/market/overview
- Get Markets — https://docs.polymarket.us/api-reference/markets/get-markets.md
- Get Market By Slug — https://docs.polymarket.us/api-reference/markets/get-market-by-slug.md
- Get Market Book (retail) — https://docs.polymarket.us/api-reference/markets/get-market-book.md
- Get Order Book (institutional) — https://docs.polymarket.us/api-reference/order-book/get-order-book.md
- Institutional Order Book API Overview — https://docs.polymarket.us/institutional/orderbook/overview.md
- Rate Limits — https://docs.polymarket.us/api-reference/rate-limits.md
- Quickstart — https://docs.polymarket.us/getting-started/quickstart.md
- Doc index (llms.txt) — https://docs.polymarket.us/llms.txt
- Markets OpenAPI schema — https://docs.polymarket.us/api-reference/oapi-schemas/markets-schema.json
- Institutional order-book OpenAPI schema — https://docs.polymarket.us/institutional/oapi-schemas/orderbook-schema.json
- Official Python SDK — https://github.com/Polymarket/polymarket-us-python  (`pip install polymarket-us`)
- TypeScript SDK quickstart — https://docs.polymarket.us/api-reference/sdks/typescript/quickstart
- Context (venue identity, QCX/CFTC): CFTC DCM filing https://www.cftc.gov/IndustryOversight/IndustryFilings/TradingOrganizations/49571 ; acquisition PR https://www.prnewswire.com/news-releases/polymarket-acquires-cftc-licensed-exchange-and-clearinghouse-qcex-for-112-million-302509626.html
- Third-party corroboration (treat as secondary): AgentBets PMUS API guide https://agentbets.ai/guides/polymarket-us-api-guide/ ; TradingVPS https://tradingvps.io/polymarket-us-guide/
