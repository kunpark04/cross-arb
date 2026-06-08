# Session log — cross-arb

Process changelog: what each working session shipped, decided, or changed in scope. Newest first.
Append an entry for any session that ships code, makes a decision, or moves scope. Keep entries
terse — link the artifact (brief / script / decision / todo item) rather than restating it.

---

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
