# decisions/ — decision log

Short, append-only records of **load-bearing** decisions: a choice that shapes the project and would
be expensive or confusing to silently reverse. Capture *why*, not just *what* — so a future session
(human or agent) doesn't relitigate a settled question or undo it without knowing the cost.

## Convention

- One file per decision: `NNNN-kebab-title.md`, zero-padded (`0001-…`), monotonically increasing.
- Use [template.md](template.md) as the skeleton.
- **Append, don't rewrite.** To change a past decision, file a *new* entry that supersedes it and add a
  `Superseded by 00NN` line to the old one's Status. History stays legible.
- File an entry when a decision: (a) constrains scope, (b) picks one architecture/approach over a
  viable alternative, (c) encodes a non-obvious rule, or (d) reverses a prior decision.
- **Don't** log routine/obvious choices, or facts that belong in a `research/` brief (evidence) or
  `tasks/lessons.md` (a mistake-driven rule). Decisions are *forks taken*; lessons are *mistakes fixed*.

## Index

| # | Decision | Status |
|---|---|---|
| [0001](0001-us-legal-only-venue-pair.md) | US-legal venues only (Kalshi × polymarket.us); identical settlement required | Accepted |
| [0002](0002-comprehensive-coverage-no-pruning.md) | Comprehensive coverage — scanner prunes nothing; the bot decides what to trade | Accepted |
| [0003](0003-event-driven-persistence.md) | Edge persistence via event-driven detection, not fixed-cadence polling | Accepted (mechanism → 0005) |
| [0004](0004-ledger-layer-by-default.md) | Ledger decision rule: layer by default, rotate only on a real flip | Accepted |
| [0005](0005-dual-stream-persistence-monitor.md) | Persistence monitor = dual-stream WebSocket (Kalshi + polymarket.us WS confirmed) | Accepted |
| [0006](0006-deploy-on-digitalocean-consult-first.md) | Live loggers + bot deploy to a DigitalOcean droplet; consult owner before any deploy | Accepted |
| [0007](0007-readonly-kalshi-key-least-privilege.md) | Use the read-only Kalshi key for the monitor; keep trade-capable keys out of the repo | Accepted |
| [0008](0008-colisted-map-discovery-and-coverage-audit.md) | Co-listed map rebuilt by full discovery + coverage-audited (never static) | Accepted |
| [0009](0009-event-date-partition-copy-keep-pull.md) | Persistence data: event-date partitioning + checksum-verified move-pull (+ liveness alert) | Accepted |
| [0010](0010-all-in-edge-filtering-and-cost-model.md) | Filter on the ALL-IN edge (slippage + latency + leg-risk); collector logs the cost inputs | Accepted |
| [0011](0011-econ-co-listing-same-orientation-only.md) | Map econ (CPI/U-3/NFP/GDP/Fed) — co-list only SAME-orientation `≥`/categorical pairs; skip `≤`-tails + point-buckets | Accepted; pairing rule superseded by 0013 |
| [0012](0012-clip-allocation-edge-floor-and-phantom-filter.md) | Clip-stage allocation = 2¢ edge-floor + deploy-to-full cap (not FIFO/batch); `capturable()` drops restart-censored phantoms | Accepted (allocation preliminary; phantom fix firm; magnitudes corrected by 0013) |
| [0013](0013-econ-grid-step-twin-and-measurement-integrity.md) | Econ joins on the grid-step TWIN (`≥T` ↔ `>T−step`; old pairing = off-by-one phantom edges, data quarantined) + measurement integrity: detection-time CLOSE stamps, ws_reconnect censoring markers, single-subscription Kalshi invariant, degraded-discovery prune skip | Accepted (single-subscription invariant retired 2026-06-10 — multi-sub semantics probe-verified, monitor uses no-gap `update_subscription` adds with a cycle fallback) |
| [0014](0014-preregistered-allocation-rule.md) | PRE-REGISTERED allocation rule (τ=2¢ + category caps 20/10/5%) + frozen confirmatory protocol — the multi-week test is confirmatory, not another tuning pass | Accepted |
| [0015](0015-owner-override-live-trading-phase.md) | Owner OVERRIDE of the read-only phase: build the live trading bot (Rust, `bot-rs/`), safe-by-default (dry-run/demo/caps/kill-switch); live submission runs in the owner's env. **Supersedes the read-only constraint of 0006/0007** | Accepted (Claude protest-of-record: edge unvalidated; staged rollout strongly recommended) |
| 0016 | *(referenced by the 2026-06-11 session log for the live venue-contract verification work, but the entry file was never filed — see docs/sessions.md; backfill pending owner)* | **MISSING** |
| [0017](0017-live-edge-rate-lock-days.md) | Live edge-RATE reservation (`MIN_EDGE_RATE_CPD`, opt-in) on **corrected days-to-grade** lock-days (sports dynamic), **without** un-freezing 0014-H2's confirmatory priors | Accepted |
| [0018](0018-settlement-identity-outcome-not-source.md) | Settlement identity = identical **OUTCOME** not source STRING (ESPN vs FIFA = same winner); **4-category** priced gate (IDENTICAL / TAIL[¢-cost] / DIVERGENT[structural] / NEEDS_MANUAL) — refines 0001 | Accepted |
| [0019](0019-scale-in-reentry-multi-position.md) | Scale-in + re-entry capability (multi-position-per-slug, exact per-position exposure release); separate `ENABLE_*` flags **OFF by default** — option-value, marginal on current data (capital binds) | Accepted (safe-by-default; arming gated) |
