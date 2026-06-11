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
| Toxicity-direction gate | `SKIP_DEAR_LED_WEATHER` | true | **H1** (the one tested strategy idea with a signal) — skip **dear-led weather** edges (~79% toxic vs ~17% cheap-led, Fisher p=2.7e-6); **weather-only** (sports null); dormant until stage-2 supplies `led_by` |
| Assume settled (per category) | `ASSUME_SPORTS_SETTLED` / `ASSUME_ECON_SETTLED` | false | owner override: treat sports / econ as settlement-reconciled (else gated until recon ~June 23 / July 2). The residual void/postpone tail is handled by the unwind rule, not this flag |
| Event-proximity gate | `MAX_DAYS_TO_EVENT` | 2.0 | capital velocity — skip **any** arb more than this many days before its settlement event (game for sports, release for econ); weather (event ~now) is naturally exempt. Category-agnostic; dormant until stage-2 supplies `days_to_event`. `<=0` disables |
| Postponement unwind | `KALSHI_VOID_WINDOW_DAYS` | 2.0 | flatten a held **sports** pair (SELL both legs) when a postponement's makeup is past Kalshi's void window (or unknown) — before Kalshi voids. Logic in `unwind.rs`; stage-2 wires live statsapi detection |
| Settle-clean | `REQUIRE_SETTLE_CLEAN` | true | only trade settlement-verified pairs (weather; econ/sports per recon) |
| Key path | `KALSHI_RW_KEY_PATH` | unset | external path to the read-write key — loaded at runtime, never copied into the repo |

The bot **refuses to start** in `live`+`prod` without `CROSSARB_I_UNDERSTAND_PROD=yes`. The recommended
staged rollout (decision 0015): **dry-run → demo sandbox → 1-contract prod → scale** — and scaling only
after the 0014 confirmatory run passes on multi-week data and the naked-unwind cost is measured.

## Edge cases enforced (pre-trade gates, `src/risk.rs`)

Kill-switch · WS-reconnect/seq-gap pause (never trade a rebuilding book) · **settlement-identity**
(invariant #1 — econ/sports must be empirically verified, or via `ASSUME_{SPORTS,ECON}_SETTLED`) ·
**event-proximity** (skip ANY arb >N days before its settlement event — capital velocity; weather
exempt) · **postponement unwind** (flatten a held sports pair before Kalshi voids — `unwind.rs`) ·
crossed/locked book (L12) · staleness (L13)
· cross-venue **mid-divergence** (L1 bad-join/stale guard; **tighter category bound for econ twins**) ·
non-positive edge (L11) · opt-in edge floor (L15) · **fat-edge toxicity haircut** (size down above the
~6c knee — fat edges are adversely-selected; knob to test speed-capture) · **toxicity-DIRECTION gate**
(H1 — skip dear-led *weather* edges, ~79% toxic; weather-only, dormant until stage-2 supplies `led_by`) ·
per-pair / per-cluster (city-date, game) / total notional caps · concurrency cap · depth- and
bankroll-limited sizing (a thin book is *small* size, not no-trade — L15) · order **idempotency**
(`client_order_id`).

Execution is **pair-shaped**: `ExecutionBackend::submit_pair` fires BOTH legs as the unit (the live
backend fires them *concurrently* over two warm connections — serial legging ~doubles latency, the one
in-code latency lever). Reviews: `tasks/_agent_bus/20260611-rust-review/`.

Execution-time edge cases (stage 2, `legs.rs`): leg-fill timeout → **unwind leg A** at market; **MLB
postponement** kill before Kalshi's void window; partial-fill handling; venue rejection / rate-limit
backoff.

## Build / run

Needs Rust (`rustup`). Stage 2 adds async/TLS/crypto deps (tokio, tokio-tungstenite, reqwest/rustls,
rsa, ed25519-dalek). **Windows-gnu build note:** the rustup self-contained mingw is too minimal to
build the `windows-sys` crate (`tokio` pulls it in on Windows) — install a full mingw-w64
(`winget install BrechtSanders.WinLibs.POSIX.MSVCRT`, which adds `dlltool`/`as`/`gcc` to PATH). The
deploy target is **Linux**, where none of this applies (no `windows-sys`, no mingw). TLS uses **rustls**
(not OpenSSL) so there's no OpenSSL build dep.

```bash
cd bot-rs
cp .env.example .env   # fill in key paths + safety vars (gitignored; keys stay external)
cargo test             # full suite: risk gates + fee/signal parity (1:1 + game) + book + venue parsers + discovery + postpone-detector + auth + unwind (98 tests)
cargo run              # dry-run smoke (no orders; safety banner + gate/unwind decisions)
cargo run -- --smoke   # force the offline smoke even with creds present
```

To arm (owner env only): set `EXECUTION_MODE=live` (+ `VENUE_ENV`, caps, `KALSHI_RW_KEY_PATH`, and
`CROSSARB_I_UNDERSTAND_PROD=yes` for production) — and complete the stage-2 transport (below).

## What's built (stages 1–2.5, complete) vs. what remains (owner env)

**Built + tested (98 tests, all green; dry-run-default, live gated):**
- **Safety-critical spine (std-only):** `types`, `config` (safe defaults), `risk` (all pre-trade gates
  + tests), `exec` (dry-run backend + real Kalshi order-payload builder, pair-shaped `submit_pair`),
  `ledger` (outcome-independent PnL + taker fees), `unwind` (postponement-unwind), `main` (banner + hard
  prod gate + smoke).
- **Signal/match core:** `book` (O(1)-best order book + Kalshi snapshot/delta merge + pmus book +
  `depth_at_edge` for 1:1 + `game_depth_at_edge` for two-outcome sports), `signal` (`bot/ledger.py::signal`
  port for weather/econ + `monitor.py::game_edge` port `game_signal` for sports, both **parity-verified**),
  `matcher` (weather bounds-equality + econ grid-step-twin + sports abbrev join). The econ/weather decoders
  AND the sports `game_signal`/`pick_game`/leg-mapping are **independently parity-verified** vs the Python
  by differential execution — the L21 econ off-by-one phantom, a C2 wrong-game bind, and a C3 flipped sports
  orientation all cannot reach the order path (`tasks/_agent_bus/20260611-parity-review/` + `…-sports-parity/`).
- **All three categories tradeable:** weather + econ are 1:1; **sports is two-outcome** (a pmus game YES=team
  A hedged against the OTHER team's separate Kalshi market — PK = A@pmus + B@Kalshi, KP = A@Kalshi + B@pmus-NO).
  Each leg carries its venue-native id (Kalshi ticker / pmus slug) with prices read from the books, never the edge.
- **Network + transport:** `venue` (Kalshi RSA-PSS WS + pmus Ed25519 WS, snapshot/delta merge, seq-gap
  reconnect, in-place no-gap subscribe add/delete), `auth` (both signers, round-trip-tested), `discovery`
  (paginated public catalog pull → matcher joins → tracked-pair set + periodic refresh), and the **live
  transport** in `LiveBackend::submit_pair` (RSA-PSS/Ed25519 sign + HTTPS POST, **both legs concurrent**
  via `tokio::join!`, keys-absent → `KeysUnavailable`). The `#[tokio::main]` live loop wires it all:
  discovery → WS books (real `age_s`) → matcher → `Quote` → `risk::evaluate` → `submit_pair`.
- **Postponement-unwind ARMED:** `postpone` (faithful port of `probe_mlb_postpone.py::unwind_trigger` —
  detects MLB postponements off statsapi and measures the makeup gap from the BOUND event date, not the
  trap `officialDate`; **independently parity-verified**) + held-position tracking + `handle_unwind` (SELLs
  both legs at book-derived exits before Kalshi voids). **Reduce-only** (fires under the kill-switch; dry-run
  logs only; `CROSSARB_NO_AUTO_UNWIND=1` disables). The live statsapi poll is the owner/droplet step.

**Remains — inherently the owner's environment (sandbox blocks auth'd venue I/O), or deferred features:**
- **Demo-sandbox session** (owner): confirm WS sid-capture, `update_subscription` acceptance, the live
  catalog HTTP shapes, and a clean dry→demo round-trip.
- **pmus POST-body signing** (owner): the order POST signs `{ts}{METHOD}{path}` only — verify live whether
  pmus folds the body into the canonical string (typed error until confirmed; never a silent guess).
- **Sports postponement unwind — live `/teams` smoke** (owner): the trigger is BUILT + parity-verified
  (detection + tracking + firing); the one residual is a one-time live `statsapi /teams` check — the join
  matches the Kalshi-ticker team suffix to the `/teams` abbreviation, so a club whose two diverge is a MISSED
  detection (never a wrong-game fire). Non-MLB leagues have no auto-detection source yet (statsapi is MLB).
- **Sports settlement recon** (owner): endDates ~06-23/25 — until then `ASSUME_SPORTS_SETTLED` is an owner
  override, not an empirical clean.
- **Deferred to next session:** **edge-RATE allocation** (`edge ÷ lock-days`, the 0014-H2 arm) + **maker-side
  execution mode** (rest the cheap leg on Kalshi + taker-hedge pmus — the maker study's +EV config).

⚠️ **Do not trade real money on the Rust path until** (a) a demo-sandbox session is clean and (b) the 0014
confirmatory run validates the edge on multi-week data. The signal/fee parity test is green; the defaults
above make this the natural order of operations.
