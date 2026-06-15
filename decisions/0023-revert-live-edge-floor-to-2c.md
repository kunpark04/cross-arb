# 0023 — revert the LIVE edge floor to 2.0¢ (verify FOK live first; no demo available)

- **Date:** 2026-06-15
- **Status:** Accepted (owner-directed) — supersedes [0022](0022-live-edge-floor-1.5c.md)'s floor VALUE
- **Deciders:** owner (directed the revert + disclosed no demo access); Claude

## Context

[0022](0022-live-edge-floor-1.5c.md) lowered the live floor `2.0 → 1.5¢` hours earlier (owner a-priori
risk-appetite; EV + governance + scope verified). Two owner-disclosed facts the same day changed the calculus:

1. **The owner no longer has demo/sandbox access.** The FOK kill-confirm that 0021/0022 deferred to "demo-sandbox"
   can now ONLY be done **LIVE** (1-contract prod) — the next live run *is* the FOK verification.
2. The owner directed **returning to the 2¢ gate** before that live run.

## Decision

Revert the LIVE bot's `EDGE_FLOOR_CENTS` default `1.5 → 2.0¢` (`config.rs:80` + `.env.example` + README). The first
live FOK verification runs at the **conservative pre-registered 2¢ floor** — fewer, higher-quality arbs while an
unverified-LIVE execution primitive is exercised with real money. This supersedes only 0022's floor **value**;
0022's analysis (EV sound, governance-clean) and the **Kalshi FOK API-doc verification** (`exec.rs` comment, retained)
STAND.

## Alternatives considered

- **Keep 1.5¢ and verify FOK live there.** Rejected: that couples a loosened floor with an unproven primitive — the
  exact pairing 0022's adversary warned against. With no demo the verification must be live, so do it at the tighter
  floor.
- **Stay dry-run until FOK is otherwise verified.** Moot: no demo ⇒ live is the only verification path. The
  1-contract caps + the Kalshi FOK API-doc verification + the recovery-cost gate bound the first-fire downside to cents.

## Consequences

- Live floor back to **2.0¢** (which was the FOK run's effective floor anyway, so this forfeits nothing measured —
  the +1¢ NYC buckets never cleared 1.5¢). `config.rs:80`, `.env.example`, README reverted; the 0014 **research-path**
  τ=2¢ was never touched.
- **No demo henceforth — execution verification is LIVE-only (1-contract prod).** This supersedes the
  "demo-sandbox kill-confirm" recommendation in 0021/0022: the live 1-contract run is the FOK verification. Recorded
  in memory (`no-demo-verify-live-only`).
- **Re-lowering to 1.5¢ later is a clean re-apply of 0022** once a live FOK fire confirms *killed-not-rested* + no
  naked leg (and the account reconciles). Until then the live floor stays 2.0¢.
- Re-arming/running stays the owner's gated action ([0006](0006-deploy-on-digitalocean-consult-first.md) /
  [0015](0015-owner-override-live-trading-phase.md)). See docs/sessions.md 2026-06-15.
