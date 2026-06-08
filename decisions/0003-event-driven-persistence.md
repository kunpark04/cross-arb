# 0003 — Edge persistence via event-driven detection, not fixed-cadence polling

- **Date:** 2026-06-08
- **Status:** Accepted — the event-driven *principle* stands; the venue mechanism is resolved by
  [0005](0005-dual-stream-persistence-monitor.md) (polymarket.us WS confirmed → dual-stream, hybrid fallback dropped)
- **Deciders:** Project owner + Claude

## Context

The next work item (`tasks/todo.md` #10) is a persistence layer: capture how each edge appears,
persists, flips, and fades over the day — the input to the bot's layer-vs-rotate logic. The naive
approach is fixed-cadence snapshot polling of all ~200 co-listed markets. But the edges we hunt are
**transient**, and flips are the highest-value signal; fixed cadence **aliases** them, is stale by
one interval, and burns REST limits across the whole universe.

## Decision

Use **event-driven detection**: subscribe to each venue's order-book stream, recompute the
cross-venue edge on every book change, and log edge **state-transitions** (open / size-change / flip /
close). Keep periodic full REST sweeps only as a **resync + coverage heartbeat**, not as the primary
signal.

**Venue reality → hybrid:** Kalshi has a documented WebSocket (orderbook-delta); polymarket.us retail
WS is unverified (our gateway is REST). So: **stream Kalshi + fast-poll polymarket.us's active
(at/near-edge) subset + slow full-universe REST sweep** for discovery. Cadence by category: weather
slow (~60–120s fine), MLB/sports fast (event-driven or ≤15–30s).

## Alternatives considered

- **Blind fixed-cadence polling of all markets** — rejected: aliases transient edges, misses flips in
  time to act, stale by an interval, wastes REST budget.
- **Pure WebSocket on both venues** — blocked: polymarket.us retail WS not confirmed to exist.

## Consequences

- Persistence data is keyed on **transitions**, not periodic rows — directly drives the ledger's
  rotate-vs-layer decision ([0004](0004-ledger-layer-by-default.md)), which only triggers on a real flip.
- Supersedes the early `scripts/persistence_scan.py` (fixed 17-min snapshot loop) as the design.
- **Next concrete step:** confirm whether polymarket.us exposes a retail WS, then build the hybrid
  monitor (transition log + heartbeat snapshot).
- **Resolved (2026-06-08):** polymarket.us *does* expose a retail WS → fast-poll fallback dropped for a
  clean dual-stream ([0005](0005-dual-stream-persistence-monitor.md)).
