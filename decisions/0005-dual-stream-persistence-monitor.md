# 0005 — Persistence monitor is a dual-stream WebSocket service (Kalshi + polymarket.us)

- **Date:** 2026-06-08
- **Status:** Accepted
- **Deciders:** Project owner + Claude

## Context

[Decision 0003](0003-event-driven-persistence.md) chose event-driven detection but hedged to a
**hybrid** (stream Kalshi + *fast-poll* polymarket.us's near-edge subset) because the polymarket.us
retail WebSocket was **unverified** — that was the single open unknown blocking the monitor build
(`tasks/todo.md` #10). Step 1 probed it:

- `docs.polymarket.us` documents a public **Markets WebSocket** at `wss://api.polymarket.us/v1/ws/markets`
  (and a private `/v1/ws/private` for orders/positions/balance).
- Empirically the endpoint is **live and auth-gated** — an unauthenticated handshake returns
  `401 unauthorized: valid API key authentication required` (`scripts/probe_pmus_ws.py`).
- It keys on **`slug`** (same as the REST book and the matcher's join key), offers channels
  `SUBSCRIPTION_TYPE_MARKET_DATA` (full book + stats), `MARKET_DATA_LITE`, and `TRADE`, caps at
  **100 markets per subscription**, and its update frames are **identical in shape to the REST book**.
  Full protocol in `research/polymarketus-api-auth.md` §3c.

## Decision

Build the persistence monitor (`tasks/todo.md` #10) as a **dual-stream, event-driven** service:

- **Kalshi** — subscribe the documented `orderbook-delta` WebSocket.
- **polymarket.us** — subscribe `wss://api.polymarket.us/v1/ws/markets`, `SUBSCRIPTION_TYPE_MARKET_DATA`,
  **sharded into ≤100-slug subscriptions** (our ~200 co-listed universe → ≥2 subscriptions).
- On any book delta from **either** venue, recompute the cross-venue edge for the affected market and
  log **state-transitions** (open / size-change / flip / close) — the input to the ledger's
  layer-vs-rotate rule ([0004](0004-ledger-layer-by-default.md)).
- Keep a **slow full-universe REST sweep** as a resync + coverage heartbeat (unchanged from 0003).

**Drop the "fast-poll polymarket.us active subset" fallback** from 0003 — a real WS makes it unnecessary.

## Alternatives considered

- **Hybrid stream + fast-poll** (0003's fallback) — rejected: only existed because the WS was
  unverified; now superseded.
- **Pure fixed-cadence REST polling** — already rejected in 0003 (aliases transient edges, misses flips).

## Consequences

- **Code reuse:** the WS frame (`marketSlug`, `bids/offers{px,qty}`, `state`, `stats`, `transactTime`)
  matches the REST book, so the existing parser + edge calc in `scripts/weather_arb_scan.py` /
  `scan_all.py` reuse unchanged; `slug` keying needs no slug→symbol bridge.
- **Auth:** the monitor must load the Ed25519 key (`scripts/.env`) and sign the WS upgrade — *unlike*
  the public REST book, the WS needs the key (project creds already verified, `tasks/todo.md`).
- **⚠️ Residual unknown to resolve FIRST in step 2:** the exact signed message for the WS handshake.
  REST signs `"{ts}{method}{path}"`; confirm it holds for the upgrade via a live authenticated connect
  (expect `101 Switching Protocols` + a snapshot frame terminated by `eof:true`). This is the kickoff
  task of the monitor build.
- **Limits:** ≤100 markets/subscription; respond to server heartbeats/keep-alive; the 20 req/s key cap
  applies only to connection setup, not to streamed frames.
- **Revisit if:** polymarket.us changes the WS auth scheme or the 100-market cap, or the authenticated
  handshake reveals the signed string differs from the REST format.
