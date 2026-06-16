# deploy/bot-rs/ — droplet deployment for the **LIVE trading bot** (Rust)

Artifacts to run `bot-rs/` as an always-on, supervised, **real-money** service on the DigitalOcean droplet.

> **OWNER OVERRIDE OF [0007] — [decision 0028](../../decisions/0028-live-bot-on-droplet-override-0007.md).**
> The read-only monitor deploy ([../README.md](../README.md)) deliberately keeps the **trade-capable key off
> the box**. This deploy **reverses** that: the bot needs the Kalshi read-write key + pmus secret on the
> droplet to place orders. It runs as a **separate** confined `cross-arb-bot` user so the monitor's
> read-only `cross-arb` user and its no-trade-key invariant are **untouched**. Real money — treat with care.

## Why this works cleanly

- The bot uses **rustls (ring), not OpenSSL** (`bot-rs/Cargo.toml`), so the Linux binary has **no system-TLS
  dependency** — only glibc. Build it in **WSL Ubuntu-24.04** (matches the droplet OS exactly) and ship the
  single binary; the box needs no toolchain and no extra apt packages.
- Logging is **identical to the laptop run**: stdout+stderr → `bot-live.out` (systemd `append:`), and the bot
  writes `executions.jsonl` in its working dir — the same two files `./run-live.sh > bot-live.out 2>&1` makes.

## Files

| File | Runs where | Purpose |
|---|---|---|
| [`build-linux.sh`](build-linux.sh) | WSL (laptop) | build the Linux release binary → `dist/cross-arb-bot` (installs rustup/build-essential if absent) |
| [`run-live-droplet.sh`](run-live-droplet.sh) | the droplet | ExecStart wrapper — the FULL arming+risk env (mirror of `bot-rs/run-live.sh`, droplet paths). **Keep in sync.** |
| [`cross-arb-bot.service`](cross-arb-bot.service) | the droplet | systemd unit: `cross-arb-bot` user, `Restart=always`, FS-hardened but network-capable, logs → `bot-live.out` |
| [`provision-bot.sh`](provision-bot.sh) | the droplet (root) | create the `cross-arb-bot` user, `0700` the dir, lock secrets, install+enable the unit (idempotent) |
| [`deploy-bot.sh`](deploy-bot.sh) | laptop | one command: ship binary + secrets out-of-band → provision → start |
| [`pull-bot-logs.ps1`](pull-bot-logs.ps1) | laptop | mirror `bot-live.out` + `executions.jsonl` → `Kalshi/data/cross-arb-bot/` |

## Deploy (owner-run — the sandbox can't submit live orders or SSH the droplet)

```bash
# 1. build the Linux binary (once, and after any bot-rs code change)
wsl bash deploy/bot-rs/build-linux.sh

# 2. ship + provision + start  (cross-arb-droplet = ~/.ssh/config alias -> 198.199.67.245)
deploy/bot-rs/deploy-bot.sh cross-arb-droplet
```

Risk config baked into the wrapper this session ([0028]): **3 contracts/pair HARD cap**
(`MAX_CONTRACTS_PER_PAIR=3`, `MAX_NOTIONAL_PER_PAIR=3`), initial clip sizes up to 3, edge floor 2.0¢,
`$25` total notional. **Settlement gates default GATED** (`ASSUME_SPORTS_SETTLED`/`ECON=false`) — arm them
deliberately (recon ~6/23 sports, ~7/2 econ) via a drop-in:

```bash
ssh cross-arb-droplet systemctl edit cross-arb-bot
#   [Service]
#   Environment=ASSUME_SPORTS_SETTLED=true ASSUME_ECON_SETTLED=true
ssh cross-arb-droplet systemctl restart cross-arb-bot
```

## Operate

```bash
ssh cross-arb-droplet systemctl status  cross-arb-bot      # health
ssh cross-arb-droplet tail -f /opt/cross-arb-bot/bot-live.out   # live fires / heartbeats
ssh cross-arb-droplet systemctl restart cross-arb-bot      # restart
ssh cross-arb-droplet systemctl stop    cross-arb-bot      # *** STOP TRADING ***
pwsh deploy/bot-rs/pull-bot-logs.ps1                       # pull logs -> Kalshi/data/cross-arb-bot/
```

- **Kill without stopping the unit:** `systemctl edit cross-arb-bot` → `Environment=CROSSARB_KILL=1` → restart.
- **After 20 crashes/5min** the unit gives up (a real fault, not a blip): `systemctl reset-failed cross-arb-bot`
  then restart once the cause is fixed.
- **Logs append across restarts** (`append:`), so `bot-live.out` is continuous; a stop/reboot loses nothing
  in flight (`executions.jsonl` is per-line durable).

## Security posture ([0028])

The box now holds a **trade-capable** key. FS-confinement (`ProtectSystem=strict`, `0700`,
`NoNewPrivileges`) limits a compromised *process*, but the box retains outbound network — so a root/user
compromise means trade capability + key exfiltration. Mitigations: the bot's own caps (`$3/pair`,
`$25` total), the venue-side per-key limits, and (recommended) a DO firewall (inbound SSH only) + key
rotation if the box is ever suspect. **Never commit the secrets** (`secrets/*.pem`, `.env` are gitignored).
