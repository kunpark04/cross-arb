# 0028 — Live trading bot on the droplet (owner override of 0007's no-trade-key invariant)

- **Date:** 2026-06-16
- **Status:** Accepted
- **Deciders:** Owner (explicit, this session), implemented by Claude

## Context

The live bot (`bot-rs/`) had been launched by hand on the owner's laptop (`run-live.sh > bot-live.out`); the
DigitalOcean droplet ran **only** the read-only persistence monitor (`bot/monitor.py`). The owner wants the
bot to run **perpetually**, which a laptop can't guarantee, so it must move to the always-on droplet — and,
same session, to **3 contracts/pair** with the 1-contract initial-clip limit removed.

This collides with **[0007]** and [`deploy/README.md`](../deploy/README.md): the droplet's safety rests on the
loaded Kalshi key being **read-only on Kalshi's side** ("never stage a trade-capable key here" / "The
trade-capable Kalshi key never goes near the box"). Order-blocking there is a **venue-side ACL, not droplet
confinement** — the box keeps outbound network. Running the live bot there **requires** the trade-capable
Kalshi RW key + pmus Ed25519 secret on the box. The owner was shown this conflict explicitly and chose to
override — the same authority that reversed the read-only research phase in **[0015]**.

## Decision

Deploy `bot-rs` to the droplet as an always-on `Restart=always` systemd service, accepting the trade-capable
key on the box. Isolate it under a **separate** unprivileged `cross-arb-bot` user / `/opt/cross-arb-bot`
(`0700`), leaving the monitor's read-only `cross-arb` user and its no-trade-key invariant **intact**. Build
the Linux binary in **WSL Ubuntu-24.04** (matches the droplet glibc; rustls means no system-TLS dep), ship the
binary + secrets out-of-band, and mirror the laptop's logs (`executions.jsonl` + `bot-live.out`) back via
`pull-bot-logs.ps1`. Risk config this session: `MAX_CONTRACTS_PER_PAIR=3` + `MAX_NOTIONAL_PER_PAIR=3` (3 ctr
is the hard per-pair ceiling incl. scale-in), initial clip up to 3. Artifacts: [`deploy/bot-rs/`](../deploy/bot-rs/).

## Alternatives considered

- **Keep it on the laptop under a Task-Scheduler supervisor** — no key on a cloud host, no [0007] override,
  logging already identical; but "perpetual" then hinges on the laptop staying powered/awake. Owner rejected
  in favour of always-on infra.
- **Build on the droplet** — avoids WSL, but a release build of the full tokio/reqwest tree risks OOM on the
  `$6`/1 GB box; WSL Ubuntu-24.04 matches the droplet exactly and keeps the box a thin binary host.
- **Reuse the monitor's `cross-arb` user** — simpler, but would put the trade key in the read-only user's dir
  and destroy [0007]'s isolation for the *monitor* too. Rejected; a separate user is strictly cleaner.

## Consequences

- **[0007] no longer holds for the box as a whole.** A compromise of `cross-arb-bot` (or root) now means
  trade capability + key exfiltration, not just data read. Mitigations: FS-confinement (`ProtectSystem=strict`,
  `0700`, `NoNewPrivileges`), the venue-side per-key caps, the bot's own caps (`$3/pair`, `$25` total), and
  (recommended) a DO firewall (inbound SSH only) + key rotation if the box is ever suspect.
- The monitor's read-only invariant is **preserved** (separate user; its key stays read-only).
- New dependency: `pull-bot-logs.ps1` mirrors the live logs to `Kalshi/data/cross-arb-bot/`.
- Settlement-gate flags (`ASSUME_SPORTS_SETTLED`/`ECON`) default **GATED** in the droplet wrapper (recon
  ~6/23 sports / ~7/2 econ not yet reached) — the owner arms them deliberately via a systemd drop-in. This is
  a conscious divergence from the laptop's last run (which armed them) given the bot is now unattended.
- The 3-contract sizing runs against the **[0027]** phantom-liquidity finding (conversion *decreases* with
  displayed depth; ~85% of size-1 fires already die on the pmus hedge non-fill). Revisit if the recover/naked
  rate makes size-3 net-negative, if the box is compromised, or once settlement recon lets the gates default open.
