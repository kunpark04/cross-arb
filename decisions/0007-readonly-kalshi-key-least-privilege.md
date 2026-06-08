# 0007 — Use the READ-ONLY Kalshi key for the monitor; keep trade-capable keys out of the repo

- **Date:** 2026-06-08
- **Status:** Accepted
- **Deciders:** Project owner (provided keys) + Claude

## Context

The owner has three Kalshi API key pairs on hand (at `…\Projects\Authentication\kalshi-api-keys`):
**demo** (sandbox), **readonly** (production read), **readwrite** (production trade). The persistence
monitor and all current work are **read-only** — they consume market data and place no orders.

## Decision

The monitor and the entire read-only phase use the **read-only** Kalshi key only, copied to
`scripts/kalshi_readonly.pem` (gitignored) with its key id in `scripts/.env`. The **read-write**
(trade-capable) key is **not** copied into the project and is **not** used until the gated trading
phase, and then only deliberately and with owner consent ([decision 0006](0006-deploy-on-digitalocean-consult-first.md)).

## Alternatives considered

- **Copy the read-write key in now too** — rejected: zero benefit in a read-only phase, and a
  trade-capable credential in the working repo/.env raises the blast radius if it leaks (funds vs data).
- **Use the demo key** — rejected for live validation: demo is the sandbox venue, not the production
  books the arb actually trades against. (Still useful later for safe order-placement testing.)

## Consequences

- **Least privilege:** a leaked read-only key or `.env` cannot move funds — only read market data.
- `*.pem` and `scripts/.env` are gitignored; the read-only PEM and key id never enter git history.
- When the trading phase opens, wire the read-write key deliberately (and consider on-host secret
  management on the droplet, per 0006) — do not let the monitor process ever hold it.
- **Revisit if:** the owner explicitly wants the read-write key staged earlier (then update this entry).
