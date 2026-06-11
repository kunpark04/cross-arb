# 0015 — Owner override: open the live-trading phase (Rust bot), safe-by-default

- **Date:** 2026-06-11
- **Status:** Accepted
- **Deciders:** Project owner (explicit override + provided the read-write key) — Claude implementing under protest-of-record (see Consequences)

## Context

The entire project to date is **read-only** ([0006](0006-deploy-on-digitalocean-consult-first.md) gated
deploy, [0007](0007-readonly-kalshi-key-least-privilege.md) read-only key, the CLAUDE.md working
agreement "do not write order-placement code"). On 2026-06-11 the owner **explicitly overrode** the
read-only governance, stated they hold the Kalshi **read-write** key
(`…\Projects\Authentication\kalshi-api-keys`), and directed: build the live bot in Rust.

This override lands **immediately after** a same-day 7-dimension deployment-readiness audit
([research/deployment-readiness-2026-06-11.md](../research/deployment-readiness-2026-06-11.md)) and a
full backtest ([research/backtest-2026-06-11.md](../research/backtest-2026-06-11.md)) that both
concluded **NOT READY**: effective n ≈ 1 day, ~71% of apparent edge is phantom, 85% sub-tick, the
median arb is friction-negative, the fattest edges are the most toxic, sports/econ settlement is only
just being reconciled, and the decisive naked-unwind cost is **unmeasured**. The owner accepts this
risk; it is their project, their capital, their key, their call.

## Decision

Build the live trading bot in **Rust** (`bot-rs/`), implementing the full live-trade architecture, but
**safe-by-default**: (1) **dry-run is the default** — no order is sent without an explicit
`EXECUTION_MODE=live` env + config flag; (2) live execution defaults to the **demo/sandbox** venue
(0007's demo key) before production; (3) hard **position / exposure / per-cluster caps + a global
kill-switch** gate every send; (4) the bot loads the read-write key **from the owner's external path at
runtime** — it is never copied into the repo, read by Claude, or committed. **Live order submission runs
in the owner's environment, not Claude's sandbox** (the harness blocks real-money submission — it
blocked a read-only balance check this session — so Claude builds + dry-run-tests only).

## Alternatives considered

- **Refuse / stay read-only** — rejected: the owner has the authority to reverse their own governance and
  has done so explicitly and informedly; continued refusal is paternalism, not safety.
- **Paper/dry-run bot only** — the owner declined this; but it is preserved *as the default mode* of the
  live bot, which is the responsible reconciliation.
- **Python** — rejected by the owner (wants Rust). Noted for record: the project's own latency-playbook
  puts Rust at Tier 4 ("network-bound at 86–261 ms RTT; compute language is noise") — Rust is a forward
  bet on a co-located future, not a present-day edge.
- **Auto-fire on production immediately** — rejected on safety: an auto-firing bot on an unvalidated edge
  with an unmeasured unwind cost is how you lose money fast. Hence dry-run + sandbox + caps defaults.

## Consequences

- **Supersedes the read-only constraint** of [0006](0006-deploy-on-digitalocean-consult-first.md) /
  [0007](0007-readonly-kalshi-key-least-privilege.md) and the CLAUDE.md "no order-placement code" rule —
  but **only** under the safe-by-default rails above. The least-privilege spirit of 0007 is preserved:
  the read-write key stays external, never in repo/history.
- **Protest of record (Claude):** the evidence says this edge is not validated; the strong recommendation
  is a **staged rollout** — dry-run → demo/sandbox → **1-contract** production → scale only after the
  0014 confirmatory run passes on multi-week data and the naked-unwind cost is measured. The bot is built
  to make that staging the path of least resistance.
- **Revisit if:** a dry-run/sandbox session shows the live frictions (leg-fill, unwind, toxicity) confirm
  the audit's concerns → halt before production. The kill-switch + caps exist precisely for this.
