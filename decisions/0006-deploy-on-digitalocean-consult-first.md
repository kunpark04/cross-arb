# 0006 — Live loggers + bot run on a DigitalOcean droplet; consult the owner before any deploy

- **Date:** 2026-06-08
- **Status:** Accepted
- **Deciders:** Project owner (instruction) + Claude

## Context

The persistence logger (`bot/monitor.py`) and the eventual live bot are long-lived processes meant to
run continuously against both venues — not on the dev laptop. The owner specified the intended runtime
and an explicit guardrail: **consult them before actually deploying these.**

## Decision

The live loggers and the bot will run on a **DigitalOcean droplet**. **Do not deploy, provision cloud
infra, or start the live processes anywhere without first consulting the owner.** During development,
work stays local and read-only: self-tests (`python bot/monitor.py`) and the read-only probes in
`scripts/` only. The monitor's `--live` path is gated behind this decision.

## Alternatives considered

- **Run the logger on the dev machine** — rejected: not durable (sleep/restarts/IP), and the owner
  specified a droplet.
- **Auto-deploy once the code is ready** — rejected: the owner wants to be consulted at deploy time
  (capital/venue-facing process; secrets leave the laptop).

## Consequences

- `bot/monitor.py --live` is gated; agents must **not** stand up a droplet, copy secrets to a remote
  host, or launch the live logger autonomously. Local probes/self-tests are always fine.
- When the owner green-lights deployment, scope the droplet work then: instance sizing, **secret
  management on the droplet** (Ed25519 + Kalshi keys, never in git), process supervision
  (systemd/pm2), reconnect + heartbeat, and durable transition-log storage.
- **Revisit when:** the owner says it's time to deploy.
