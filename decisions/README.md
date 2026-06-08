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
