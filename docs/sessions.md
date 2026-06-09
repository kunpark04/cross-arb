# Session log — cross-arb

Process changelog: what each working session shipped, decided, or changed in scope. Newest first.
Append an entry for any session that ships code, makes a decision, or moves scope. Keep entries
terse — link the artifact (brief / script / decision / todo item) rather than restating it.

---

## 2026-06-09 — Independent review → correctness fixes, settlement verified, de-filtering

Ran a fresh-eyes adversarial review of the whole project (given only the thesis, none of our own findings)
and acted on every confirmed finding. Saved as [tasks/independent-review-2026-06-09.md](../tasks/independent-review-2026-06-09.md).

- **Confirmed bugs fixed** (`bot/ledger.py`, `bot/monitor.py`): per-**order** Kalshi fee (was per-contract,
  over-charging) + a float-imprecision ceil overshoot (175.0000003→176) (**C1**); `ledger.enter` refuses a
  non-positive-edge book (**C2**); crossed/locked-book rejection (**C3**); a Kalshi seq-resync now writes a
  `kalshi_resync` marker the analyzer censors (**C6**). Lessons **L10–L13**.
- **Instrumentation:** added per-venue **staleness (`age`)** to each transition + a `capital_sim`
  **clean-fillable** filter (liquidity + freshness) to separate persistent-fillable from persistent-stale-
  phantom (review **L2**).
- **Settlement identity VERIFIED** (`scripts/verify_settlement.py`, the review's #1 risk): pulled both venues'
  live weather rules → **same NWS CLI Daily + same station (incl. NYC = Central Park) + matching boundaries**
  across 5 cities; *timing/revision* still open. → [research/settlement-verification.md](../research/settlement-verification.md).
- **De-filtering** (owner's rule — filter a trade ONLY when its ACTUAL edge ≤ 0): **marginal-fee detection**
  (the n=1 ceil fee over-charged 0.25–0.9c and dropped real at-size arbs; booking keeps the exact per-order
  fee); **permissive analysis baseline** (every positive arb; 1c/liquidity/staleness are opt-in knobs); and
  **per-direction pricing** so a one-sided book no longer drops the valid direction (`make_px`/`signal`/
  `game_edge`). Lessons **L14–L15**; decision [0010](../decisions/0010-all-in-edge-filtering-and-cost-model.md)
  (filter on the ALL-IN edge — fees + spread IN; slippage/latency/leg-risk OUT, to model in the trade layer).
- Monitor redeployed several times (live, `active`, 0 restarts). Pushed to `origin/main` through `34554dc`.

## 2026-06-09 — Deployed live + foolproof data pipeline + liveness alerting

Took the read-only monitor from build-complete to **running 24/7 on a DigitalOcean droplet**, then made the
data collection robust and self-monitoring.

- **Hardened deploy** (gated [0006](../decisions/0006-deploy-on-digitalocean-consult-first.md), owner-greenlit).
  Droplet `cross-arb-droplet` (`198.199.67.245`, 1 vCPU·1 GB·NYC1·Ubuntu 24.04 — the *measured* sizing:
  `scripts/probe_monitor_footprint.py` showed ~7 MB heap **flat over 30 sim-days** after **idle-market
  pruning**, vs ~216 MB unpruned). A dedicated unprivileged **`cross-arb`** user owns only `/opt/cross-arb`
  (`0700`); the systemd unit runs as that user with `ProtectSystem=strict`, `--forever`, `Restart=always`.
  `deploy/deploy.sh` ships **only the runtime cone** (4 bot modules + reqs + unit) via scp; secrets
  out-of-band; ed25519 key `cross-arb_ed25519` + `~/.ssh/config` alias. RSS ~76 MB live.
- **Monitor changes:** `--forever`; idle-market pruning (flat memory); **event-date partitioning**
  (`transitions-<date>.jsonl` — a market lifecycle is never split at wall-clock midnight); `sessions.jsonl`
  restart marker; `health.json` liveness beacon. Self-tests extended; all green.
- **Foolproof pull** (`deploy/pull-data.ps1`, [0009](../decisions/0009-event-date-partition-copy-keep-pull.md)):
  sha256-verified, idempotent mirror to `Kalshi/data/cross-arb/`; **verified-move** of finalized days
  (gzip local → decompress-verify == remote sha256 → delete on droplet); live files copied, never deleted.
- **Alerting:** `deploy/healthcheck.ps1` (every 30 min) checks service-active + beacon freshness → desktop
  balloon + `ALERT.txt` + non-zero exit on failure; closes the "is it still collecting?" gap (catches a
  hung-but-`active` process). Both jobs scheduled via `deploy/register-tasks.ps1` (`PullCrossArbData` daily,
  `CrossArbHealthcheck` 30 min); both test-ran clean. **All paths verified end-to-end** (move deletes a
  synthetic finalized file after verify; alert fires on forced-stale; healthy clears the alert).
- **Captured:** decision [0009](../decisions/0009-event-date-partition-copy-keep-pull.md); lessons **L8**
  (systemd has no inline comments — `ProtectSystem=strict  # …` was silently ignored, caught only in
  verification) + **L9** (verify the property you changed, not "active").

## 2026-06-08 — Version control: git init + private GitHub remote

Put the project under git (it had none) and pushed it. Early in the session: `git init -b main`, verified
the gitignore excludes `scripts/.env` / `*.pem` / `scripts/_data/` (only `.env.example` tracked), initial
commit. End of session: created **private** `github.com/kunpark04/cross-arb` via `gh` and pushed `main`
(9 commits, `7342c35`..`3241bf2`) — secrets never left the machine. Visibility is private by the owner's
choice (flip with `gh repo edit --visibility public`). Work was committed in focused units as it landed.

## 2026-06-08 — Monitor refinements: dynamic re-subscribe + FLIP debounce (build complete)

Closed the two remaining monitor pieces. (1) **Dynamic re-subscribe:** `run_live` holds the live WS
connections and, on each heartbeat, re-runs full discovery and subscribes any NEW weather days / games on
both streams (`register()`), so a day-long run keeps coverage without a restart (settled markets idle
out). (2) **FLIP debounce:** extracted `FlipDebouncer` (own self-test) — a CLOSE is held ~1s; an
opposite-direction OPEN within the window becomes one FLIP, a same-direction reopen is a suppressed
flicker, otherwise the CLOSE is flushed. **Live-verified** (`--live 120 30`): both streams + dispatch +
debouncer + heartbeat ran clean — captured a full LAX weather edge lifecycle (OPEN→WIDEN→NARROW→CLOSE)
plus live MLB edges, heartbeat re-discovery executing each cycle. The persistence-layer monitor (todo #10)
is **build-complete** as a read-only logger; an extended data-collection run is the gated DigitalOcean
deployment (decision 0006).

## 2026-06-08 — Sports 2-outcome tracker (GameTracker) + first live dual-stream run

Built `GameTracker` in `bot/monitor.py`: the 2-outcome cross-venue tracker for the sports topology (the
polymarket game market YES=team A + the TWO Kalshi single-team tickers), computing the cheapest-venue-
per-side edge (`game_edge`) and emitting the same OPEN/CLOSE/FLIP/WIDEN/NARROW transitions as the weather
`MarketTracker`. Self-test passes. Rewired `run_live` into a unified dispatch (pmus slug → fn, Kalshi
ticker → fn) routing weather to `MarketTracker` and sports to `GameTracker`; `--live N` now does a bounded
read-only run. **First live dual-stream run** (`--live 75`): connected both streams, tracked 60 weather +
124 sports (184 pmus slugs / 292 Kalshi tickers), and logged real live MLB cross-venue edges
(`aec-mlb-nyy-cle` OPEN dir PK net 0.0375; `aec-mlb-phi-tor` OPEN dir KP net 0.0681) — also confirming a
sports slug's pmus WS frame delivers a usable book. Remaining: dynamic (un)subscribe on discovery churn;
per-frame FLIP debounce.

## 2026-06-08 — Co-listed map: full-discovery builder + coverage audit

Built `bot/colisted_map.py`: `build_colisted_map()` rebuilds the {pmus slug ↔ Kalshi ticker} map from a
FULL catalog pull each call (dynamic date/event grouping → new weather days + sports games auto-covered),
reusing `scan_all.py`'s identity matching (no false positives, L1). It also returns a COVERAGE AUDIT that
flags any pmus climate city / sports league not in `WX`/`LEAGUES`. Live run: 60 weather + 123 sports
pairs; the audit immediately caught an unmapped league `twc` (influencer soccer, 1 market, no Kalshi
co-listing — left unmapped by choice). Wired into `bot/monitor.py`: `run_live` now self-discovers the
weather map and the heartbeat re-runs discovery (coverage + churn logging). Design = decision 0008;
gap-class lesson L7. Remaining for a live run: the SPORTS 2-outcome tracker, dynamic re-subscribe on
churn, FLIP debounce.

## 2026-06-08 — Kalshi snapshot/delta merge built (KalshiBook) + wired into the monitor

Built `bot/kalshi_book.py`: `KalshiBook` reconstructs a live Kalshi book from an `orderbook_snapshot`
plus `orderbook_delta` stream (prices/qty as `*_dollars`/`*_fp` strings; YES ask = 1 − best NO bid),
with a connection-level `SeqTracker` (gap → resubscribe). Confirmed empirically that Kalshi's `seq` is
**one counter per connection** (not per-market) by observing live frames across 8 markets on one `sid`.
Offline self-test passes; the live test merged 8 snapshots + **28 real deltas with no seq gaps**. Wired
it into `bot/monitor.py`'s `kalshi_stream` (replacing the stub) — both venue streams now feed the same
`MarketTracker`. Promoted the verified WS message format into `research/kalshi-venue-audit.md`. Remaining
before a live dual-stream run: the co-listed `{slug:ticker}` map (`scan_all.py`), FLIP debounce, REST
heartbeat.

## 2026-06-08 — Kalshi WS fully validated (read-only key added)

Copied the **read-only** Kalshi API key into the project (`scripts/kalshi_readonly.pem`, gitignored;
`.env` updated with the key id) — demo + read-write keys intentionally left out (least privilege,
[decision 0007](../decisions/0007-readonly-kalshi-key-least-privilege.md)). Re-ran
`scripts/probe_kalshi_ws.py`: RSA-PSS signed handshake → `101`, `subscribed` + `orderbook_snapshot`
received → **Kalshi `orderbook_delta` VERIFIED**. Both venues' WebSockets are now proven end-to-end
(polymarket.us Ed25519 + Kalshi RSA-PSS). Remaining before a live dual-stream run: the Kalshi
snapshot/delta merge in `bot/monitor.py`, the co-listed `{slug:ticker}` map, and FLIP debounce.

## 2026-06-08 — Kalshi WS: endpoint validated; authed stream blocked on creds

Validated the Kalshi side of the dual-stream as far as possible without creds (`scripts/probe_kalshi_ws.py`):
the WS endpoint is **`wss://api.elections.kalshi.com/trade-api/ws/v2`** (unauth → `401
token_authentication_failure` = live + auth-gated). Corrected the venue audit's endpoint drift — legacy
`trading-api.kalshi.com` is dead; `external-api-ws.kalshi.com/` root 404s. Kalshi WS needs auth even for
orderbook data (RSA-PSS over `{ts}GET/trade-api/ws/v2`). **Blocker:** `scripts/.env` has only PMUS creds,
so the authenticated `orderbook_delta` validation can't run — needs a Kalshi API key
(kalshi.com/account/profile). The probe is written to finish in one command once the key is added. The
`bot/monitor.py` Kalshi stream stays a stub (endpoint + auth scheme documented) until then.

## 2026-06-08 — Step 2: WS auth verified + dual-stream monitor scaffold

Verified the polymarket.us WS end-to-end (`scripts/probe_pmus_ws_auth.py`): the upgrade signs the same
REST string `{ts}GET/v1/ws/markets` → `101`; camelCase `SUBSCRIPTION_TYPE_MARKET_DATA` subscribe; live
snapshot on `tc-temp-nychigh-2026-06-08-lt72f` with frames identical to the REST book. (Also learned
`?active=true` returns stale markets — use `?closed=false` / `categories[]=climate`.) Built the
persistence-monitor scaffold `bot/monitor.py`: a pure, self-verifying transition core
(OPEN/CLOSE/FLIP/WIDEN/NARROW) + the polymarket.us stream wired with the verified protocol. Remaining
live wiring: Kalshi `orderbook_delta` auth/merge, the co-listed `{slug:ticker}` map, FLIP debounce,
REST heartbeat. Recorded the owner's deployment constraint as
[decision 0006](../decisions/0006-deploy-on-digitalocean-consult-first.md) (DigitalOcean droplet;
consult before deploy) + a cross-session memory. WS protocol promoted into
`research/polymarketus-api-auth.md` §3c (now CONFIRMED). Lessons L5 (per-frame flicker) + L6 (stale
`active=true`) captured.

## 2026-06-08 — Step 1: confirm polymarket.us WebSocket (architecture fork resolved)

Probed whether polymarket.us exposes a retail WebSocket — the one unknown blocking the persistence
monitor (`tasks/todo.md` #10). **Confirmed it does:** `wss://api.polymarket.us/v1/ws/markets`,
slug-keyed, channels MARKET_DATA / MARKET_DATA_LITE / TRADE, ≤100 markets/subscription, Ed25519 auth
on the handshake; update frames identical to the REST book. Verified empirically
(`scripts/probe_pmus_ws.py` → live, auth-gated `401`) and against `docs.polymarket.us`. Protocol
promoted into `research/polymarketus-api-auth.md` §3c. **Fork resolved:** the monitor is a clean
**dual-stream** ([decision 0005](../decisions/0005-dual-stream-persistence-monitor.md)), not the
hybrid fallback; 0003's fast-poll path is dropped. **Next:** authenticated handshake to confirm the
WS signed-string, then build the transition logger.

## 2026-06-08 — Managerial-doc scaffolding

Stood up the full managerial-doc set (the project had research + scripts + a ledger but no working
agreement). Created `CLAUDE.md` (index + invariants), `README.md`, per-directory READMEs
(`research/`, `scripts/`, `bot/`), the `decisions/` log (convention + template + entries 0001–0004),
`tasks/lessons.md` (L1–L4), and this log. Back-filled the four load-bearing decisions and four
lessons that were already implicit in the work. No code or research changed.

- **Decisions filed:** [0001](../decisions/0001-us-legal-only-venue-pair.md) ·
  [0002](../decisions/0002-comprehensive-coverage-no-pruning.md) ·
  [0003](../decisions/0003-event-driven-persistence.md) ·
  [0004](../decisions/0004-ledger-layer-by-default.md)
- **Lessons captured:** L1 (fuzzy match → fake edge), L2 (settlement identity), L3 (parse before
  "mispriced"), L4 (book keys on slug).

## 2026-06-08 — Research + scanning + accounting core *(reconstructed from artifacts)*

The substantive work that already existed when the docs were written, summarized from the briefs,
scripts, and `tasks/todo.md`:

- **Settlement + legality mapped.** Confirmed the US-legal overlap is **weather + sports**; the
  clean-settlement econ/crypto families are international-Polymarket-only (US-blocked). Briefs:
  `research/settlement-map.md`, corrected by `research/us-legal-overlap-audit.md`.
- **Data access cracked.** polymarket.us full-depth books are **public**, keyed on slug; weather uses
  the same 6 buckets as Kalshi (1:1). `research/live-edge-findings.md`.
- **Weather edge validated.** First clean, fee-netted, depth-aware scan: ~5¢ modal-bucket edge,
  ~$20/day gross, intermittent. `scripts/weather_arb_scan.py`.
- **Sports matched without false positives.** Rebuilt the matcher on `(league, date, abbrev)` after
  the v1 city-name false positive (→ lesson L1). MLB ~$23 live; deep books (tennis/UFC/ITF) fully
  arbitraged. `scripts/sports_match_v2.py`, `scripts/sports_name_match.py`.
- **Coverage made comprehensive.** Unified scanner keeps the full ~200-market co-listed universe,
  prunes nothing. `scripts/scan_all.py` → `_data/scan_all.json`.
- **Accounting core built + self-verified.** `bot/ledger.py` proves additive PnL + outcome-independence
  and encodes the layer/rotate/hold rule.
- **Open item:** the event-driven persistence monitor (`tasks/todo.md` #10) — design decided, not yet built.
