# bot-rs — cross-arb live trading bot (Rust)

The live trading bot, in Rust, per [decision 0015](../decisions/0015-owner-override-live-trading-phase.md)
(owner override of the read-only phase). **SAFE BY DEFAULT** — and built so the safe path is the path
of least resistance, because the readiness audit + backtest concluded the edge is **not yet validated**
([deployment-readiness](../research/deployment-readiness-2026-06-11.md) ·
[backtest](../research/backtest-2026-06-11.md)).

> **Live order submission runs in the OWNER's environment, with the OWNER's read-write key.** Claude's
> sandbox blocks real-money order submission (it blocked a read-only balance check this session), so
> Claude builds + dry-run-tests only. This is also good practice: trade execution should never be gated
> behind a sandbox.

## Safety model (defaults)

| Knob | Env | Default | Meaning |
|---|---|---|---|
| Execution mode | `EXECUTION_MODE` | **`dry-run`** | never sends an order unless set to `live` |
| Venue env | `VENUE_ENV` | **`demo`** | sandbox even when live, until set to `prod` |
| Prod consent | `CROSSARB_I_UNDERSTAND_PROD` | unset → **refuses** | live+prod won't start without it |
| Kill switch | `CROSSARB_KILL` | off | `1` halts all trading |
| Contracts/pair | `MAX_CONTRACTS_PER_PAIR` | **1** | the 1-contract staged-rollout brake |
| Notional caps | `MAX_NOTIONAL_PER_{PAIR,CLUSTER}`, `MAX_TOTAL_NOTIONAL` | $1 / $5 / $20 | tiny |
| Edge floor | `EDGE_FLOOR_CENTS` | 2.0 | 0014 pre-registered floor |
| Fat-edge haircut | `FAT_EDGE_SIZE_FACTOR` / `FAT_EDGE_KNEE_CENTS` | 0.5 / 6c | size **down** above the knee — fat edges are ~66% toxic & die ~0.5s (readiness audit + rust review); **set factor=1.0 to test** whether a sub-0.5s concurrent fill can capture them |
| Econ twin divergence | `ECON_TWIN_MAX_DIVERGENCE_CENTS` | 15c | tighter divergence bound for settlement-identical econ twins (vs the 40c cross-category guard) — the 18c U-3 phantom must not sail through |
| Settle-clean | `REQUIRE_SETTLE_CLEAN` | true | only trade settlement-verified pairs (weather; econ/sports per recon) |
| Key path | `KALSHI_RW_KEY_PATH` | unset | external path to the read-write key — loaded at runtime, never copied into the repo |

The bot **refuses to start** in `live`+`prod` without `CROSSARB_I_UNDERSTAND_PROD=yes`. The recommended
staged rollout (decision 0015): **dry-run → demo sandbox → 1-contract prod → scale** — and scaling only
after the 0014 confirmatory run passes on multi-week data and the naked-unwind cost is measured.

## Edge cases enforced (pre-trade gates, `src/risk.rs`)

Kill-switch · WS-reconnect/seq-gap pause (never trade a rebuilding book) · **settlement-identity**
(invariant #1 — econ/sports must be empirically verified) · crossed/locked book (L12) · staleness (L13)
· cross-venue **mid-divergence** (L1 bad-join/stale guard; **tighter category bound for econ twins**) ·
non-positive edge (L11) · opt-in edge floor (L15) · **fat-edge toxicity haircut** (size down above the
~6c knee — fat edges are adversely-selected; knob to test speed-capture) · per-pair / per-cluster
(city-date, game) / total notional caps · concurrency cap · depth- and bankroll-limited sizing (a thin
book is *small* size, not no-trade — L15) · order **idempotency** (`client_order_id`).

Execution is **pair-shaped**: `ExecutionBackend::submit_pair` fires BOTH legs as the unit (the live
backend fires them *concurrently* over two warm connections — serial legging ~doubles latency, the one
in-code latency lever). Reviews: `tasks/_agent_bus/20260611-rust-review/`.

Execution-time edge cases (stage 2, `legs.rs`): leg-fill timeout → **unwind leg A** at market; **MLB
postponement** kill before Kalshi's void window; partial-fill handling; venue rejection / rate-limit
backoff.

## Build / run

Needs Rust (`rustup`). Stage 1 is **std-only** — no external crates, compiles offline.

```bash
cd bot-rs
cargo test          # runs the risk-gate + fee + PnL unit tests
cargo run           # dry-run smoke (no orders; prints the safety banner + gate decisions)
```

To arm (owner env only): set `EXECUTION_MODE=live` (+ `VENUE_ENV`, caps, `KALSHI_RW_KEY_PATH`, and
`CROSSARB_I_UNDERSTAND_PROD=yes` for production) — and complete the stage-2 transport (below).

## What's built (stage 1) vs. next (stage 2)

- **Built (this commit), std-only, safety-critical spine:** `types` (domain), `config` (safe defaults),
  `risk` (all pre-trade gates + tests), `exec` (dry-run backend + the real Kalshi order-payload builder;
  live POST is the seam), `ledger` (outcome-independent PnL + taker fees, ⚠️ fee parity-vs-`ledger.py`
  still TODO before live), `main` (banner + hard prod gate + smoke).
- **Stage 2 (owner env, adds tokio/tungstenite/reqwest/ed25519-dalek/rsa/serde):** the two venue WS
  clients + auth (port `bot/kalshi_book.py` + the signers), the colisted matcher + signal port
  (`bot/colisted_map.py` + `bot/ledger.py`, with a **parity test** asserting the Rust signal/fees match
  the Python selftest vectors), the leg-sequencer/unwind, and the **live transport** (RSA-PSS/Ed25519
  sign + HTTPS POST) that the owner runs with the read-write key.

⚠️ **Do not trade real money on the Rust path until** (a) the fee/signal parity test vs `bot/ledger.py`
is green, (b) a demo-sandbox session is clean, and (c) the 0014 data validates the edge. The defaults
above make that the natural order of operations.
