# bot-rs — cross-arb live trading bot (Rust)

The live trading bot, in Rust, per [decision 0015](../decisions/0015-owner-override-live-trading-phase.md)
(owner override of the read-only phase). **SAFE BY DEFAULT** — and built so the safe path is the path
of least resistance, because the readiness audit + backtest concluded the edge is **not yet validated**
([deployment-readiness](../research/deployment-readiness-2026-06-11.md) ·
[backtest](../research/backtest-2026-06-11.md)).

> **Live order submission is a deliberate OWNER action.** Correction (2026-06-11): the environment does
> **NOT** block venue I/O — live verification reached both venues with auth (Kalshi demo+prod signed reads;
> a 1¢ pmus `BUY_LONG`/`BUY_SHORT` placed+cancelled on the real venue). So the guardrail is the **code's
> safe-by-default** (dry-run default, prod-consent, the pmus gate), not a sandbox wall — which is the right
> place for it: execution should never be gated behind an external block that might not be there.

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
| Edge-RATE floor | `MIN_EDGE_RATE_CPD` | **1.0** (on) | **0014-H2** reservation floor — skip arbs whose `booked_edge ÷ lock-days` (¢/$-day) is below this; reserves scarce capital for high-**velocity** arbs (a 13c econ @ ~21d = 0.6 ¢/$-day is a *worse* use of capital than a 3c weather @ 1.2d = 2.5). **ENABLED live 2026-06-13** (owner) at a conservative **1.0** — below the fast-category rates (weather ≥1.7, sports ≥4), so it only cuts genuinely-slow arbs (econ / far-dated sports); **inert on current data**, uncalibrated, recalibrate once slow arbs accrue. `edge_rate` is logged on every live ENTRY; **`0` disables**. Lock-days = corrected days-to-grade — sports is **dynamic** (`days_to_event`); decision 0017 |
| Fat-edge haircut | `FAT_EDGE_SIZE_FACTOR` / `FAT_EDGE_KNEE_CENTS` | 0.5 / 6c | size **down** above the knee — fat edges are ~66% toxic & die ~0.5s (readiness audit + rust review); **set factor=1.0 to test** whether a sub-0.5s concurrent fill can capture them |
| Econ twin divergence | `ECON_TWIN_MAX_DIVERGENCE_CENTS` | 15c | tighter divergence bound for settlement-identical econ twins (vs the 40c cross-category guard) — the 18c U-3 phantom must not sail through |
| Toxicity-direction gate | `SKIP_DEAR_LED_WEATHER` | true | **H1** (the one tested strategy idea with a signal) — skip **dear-led weather** edges (~79% toxic vs ~17% cheap-led, Fisher p=2.7e-6); **weather-only** (sports null); dormant until stage-2 supplies `led_by` |
| Assume settled (per category) | `ASSUME_SPORTS_SETTLED` / `ASSUME_ECON_SETTLED` | false | owner override: treat sports / econ as settlement-reconciled (else gated until recon ~June 23 / July 2). The residual void/postpone tail is handled by the unwind rule, not this flag |
| Event-proximity gate | `MAX_DAYS_TO_EVENT` | 2.0 | capital velocity — skip **any** arb more than this many days before its settlement event (game for sports, release for econ); weather (event ~now) is naturally exempt. Category-agnostic; dormant until stage-2 supplies `days_to_event`. `<=0` disables |
| Postponement unwind | `KALSHI_VOID_WINDOW_DAYS` | 2.0 | flatten a held **sports** pair (SELL both legs) when a postponement's makeup is past Kalshi's void window (or unknown) — before Kalshi voids. Logic in `unwind.rs`; stage-2 wires live statsapi detection |
| Settle-clean | `REQUIRE_SETTLE_CLEAN` | true | only trade settlement-verified pairs (weather; econ/sports per recon) |
| Add-to-held (scale-in / re-entry) | `ENABLE_SCALE_IN` / `ENABLE_REENTRY` / `MAX_POSITIONS_PER_SLUG` | **off / off / 1** | **OPTION-VALUE, OFF by default** ([0019](../decisions/0019-scale-in-reentry-multi-position.md)): allow a SECOND locked pair on a held bucket — scale-in (base edge still live) or re-entry (edge closed, position still held) — when a bigger same-direction arb (`net ≥ base + ADD_TAU_GAIN`) appears. Multi-position-per-slug with **exact per-position exposure release**; concentration bounded by the per-pair notional + per-slug count caps. Marginal value on current data (capital, not opportunity, binds); built for readiness. Arming = the riskiest money-path change since the Dutch book — needs a final independent review of the W-1 fix first |
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
non-positive edge (L11) · opt-in edge floor (L15) · **edge-RATE reservation floor** (0014-H2 — skip
low-velocity arbs by `edge ÷ lock-days`; reserve capital for fast turns; **live at 1.0¢/$-day** since
2026-06-13, conservative — only cuts slow arbs) ·
**fat-edge toxicity haircut** (size down above the
~6c knee — fat edges are adversely-selected; knob to test speed-capture) · **toxicity-DIRECTION gate**
(H1 — skip dear-led *weather* edges, ~79% toxic; weather-only, dormant until stage-2 supplies `led_by`) ·
per-pair / per-cluster (city-date, game) / total notional caps · concurrency cap · depth- and
bankroll-limited sizing (a thin book is *small* size, not no-trade — L15) · order **idempotency**
(`client_order_id`).

Execution is **pair-shaped**: `ExecutionBackend::submit_pair` fires BOTH legs as the unit (the live
backend fires them *concurrently* over two warm connections — serial legging ~doubles latency, the one
in-code latency lever). Reviews: `tasks/_agent_bus/20260611-rust-review/`.

Execution-time edge cases: **MLB postponement** kill before Kalshi's void window — BUILT (`postpone.rs`
+ `unwind.rs` + `main::handle_unwind`); venue rate-limit/5xx backoff — BUILT (`discovery::fetch_json`
retry). **Naked-leg AUTO-RECOVERY — BUILT** (`main::recover_naked_leg`): a one-legged live fill CANCELS the
resting leg (`backend.cancel`) + FLATTENS the filled leg with a single marketable SELL read from its live
book (the new single-leg `ExecutionBackend::submit`), recording NO hedge; the halt is the BACKSTOP only —
when the flatten can't be priced (one-sided book) or the recovery SELL itself doesn't fill (the leg stays
naked → `SubmitKind::Recovery` arm engages the kill-switch for a manual flatten). Still TODO: a leg-fill
TIMEOUT (cancel a slow-but-not-yet-rejected resting leg before it fills late) and true partial-fill sizing.

**Price rounding is direction-aware (W1/W2).** A marketable limit rounds TOWARD-MARKETABLE so it still
crosses: an entry **BUY** ceils to the cent/tick (limit ≥ the touch), a flatten/unwind **SELL** floors
(limit ≤ the touch). Nearest-rounding a BUY down (or a SELL up) would have placed a *marketable* order at a
*resting* limit → the leg rests → the sibling goes naked → recovery/halt. Kalshi touches are whole cents so
ceil/floor is a no-op there; it matters for pmus sub-cent book prices (and the `realized_edge_clears_floor`
re-check guards a ceil'd BUY from eroding the edge below the floor). The recovery SELL also FLOOR-quantizes
to the held pmus leg's `orderPriceMinTickSize` (`PositionLeg::pm_min_tick`), so a coarse-tick pmus market
can't reject the flatten and bounce recovery to a needless halt.

**Execution edge cases — price granularity (FOLLOW-UP, not a safety bug).** `OrderIntent.price_cents` is a
whole-cent `u8`, coarser than pmus's 0.001 tick — so a pmus leg loses up to ~0.5c of price precision per leg
(a bounded COST; the ceil/floor keeps the order marketable, so it never silently rests). TODO before pmus is
armed past dry-run: a finer price representation (sub-cent) for pmus legs. Dormant today (all live pmus ticks
are 0.001, where whole cents are already valid multiples).

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
cargo test             # full suite: risk gates + fee/signal parity (1:1 + game) + book + venue parsers + discovery + postpone-detector + auth + unwind + naked-leg recovery + multi-position scale-in/re-entry (147 tests)
cargo run              # dry-run smoke (no orders; safety banner + gate/unwind decisions)
cargo run -- --smoke   # force the offline smoke even with creds present
```

To arm (owner env only): set `EXECUTION_MODE=live` (+ `VENUE_ENV`, caps, `KALSHI_RW_KEY_PATH`, and
`CROSSARB_I_UNDERSTAND_PROD=yes` for production) — and complete the stage-2 transport (below).

## What's built (stages 1–2.5, complete) vs. what remains (owner env)

**Built + tested (123 tests, all green, clippy-clean; dry-run-default, live gated). A full-engine
adversarial review (5 parallel subsystem reviewers + an independent review of the loop rewrite) hardened
the concurrency core, the gates, and the transport — see `tasks/_agent_bus/20260611-engine-review/`:**
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

**VERIFIED LIVE (2026-06-11) — against the real venues, not just sample-tested:**
- **Data path** ✅ — the bot ran dry-run vs live prod (read-only key, zero orders): Kalshi WS connected +
  subscribed 153 tickers, pmus WS connected + subscribed 113 slugs, ~300 frames/s, 153+112 books built,
  discovery pulled 60 weather + 13 econ + 40 sports pairs from live catalogs. (Found + fixed a real bug:
  `discovery::fetch_json` had no retry, so a cold-start burst 429'd and aborted — ported the Python's
  retry + inter-series pacing. Added connect-success logging + a 20s health heartbeat.)
- **Kalshi order path** ✅ — a LIVE 1-contract order placed (`[201]`, status `resting`, `fill_count 0`)
  + cancelled (`[200]`) on the real account with the read-write key. Signing verified on demo + prod.
- **pmus order lifecycle** ✅ — signing (body-less `{ts}POST{path}`, proven by a bad-vs-good-sig control),
  **create** `POST /v1/orders` + **cancel** `POST /v1/order/{id}/cancel`, with 1¢ `BUY_LONG` **and**
  `BUY_SHORT` placed + cancelled live (corrected a real `exec.rs` endpoint+shape bug).
- **Postpone `/teams` join** ✅ — statsapi's 30 MLB abbreviations == Kalshi's 30 `KXMLBGAME` suffixes
  (identical set), so no club's postponement is missed on a mismatch.

**Still remains (genuinely blocked, not skipped):**
- **`SELL_*` pmus intents** — doc-derived (same enum family as the live-verified `BUY_*`); can't be live-probed
  within a tiny-capital cap (a naked short isn't bounded by a 1¢ price). Exercised naturally on the first real
  unwind OR the first naked-leg recovery flatten (both fire `SELL_*`).
- **Naked-leg auto-recovery — BUILT** (was TODO): a one-legged live fill cancels the resting leg + flattens the
  filled leg (single-leg `submit` SELL), halt as the backstop. Still TODO: a leg-fill TIMEOUT (cancel a slow
  resting leg before a LATE fill) + true partial-fill sizing (today a partial counts as not-filled → recovery).
- **Sports settlement recon** — endDates ~06-23/25 (date-gated; can't complete now). Non-MLB leagues have no
  auto-postpone source yet (statsapi is MLB-only).
- **The dominant gate — edge validation** — the 0014 confirmatory run needs multi-week data that doesn't
  exist yet. Plumbing is proven; the edge is not.
- **Deferred features:** edge-RATE allocation (`edge ÷ lock-days`) + maker-side execution mode.

⚠️ **Do not scale real money on the Rust path until the 0014 confirmatory run validates the edge on
multi-week data** — that, not the plumbing, is the binding gate. The auth/order paths + the live data
pipeline are now verified end-to-end and the signal/fee parity test is green; what's unproven is the
*edge*. The safe-by-default defaults make a 1-contract armed pilot the natural next rung after the edge data.
