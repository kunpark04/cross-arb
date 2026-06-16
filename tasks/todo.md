# TODO — Kalshi × polymarket.us cross-venue arbitrage

**Goal:** measure whether a structurally clean, US-legal cross-venue edge (Kalshi × polymarket.us) is
**persistent and large enough to justify a live trading bot**. Phase: **READ-ONLY** (no orders).
This file is the live plan; the step-by-step history is in [sessions](../docs/sessions.md).

## bot-rs — LIVE droplet deploy @ 3-contract cap (2026-06-16, [0028](../decisions/0028-live-bot-on-droplet-override-0007.md)) — ✅ LIVE on droplet, ALL configs enabled
- [x] `run-live.sh` + droplet wrapper → **3 ctr/pair hard cap** (`MAX_CONTRACTS_PER_PAIR=3`, `MAX_NOTIONAL_PER_PAIR=3`); 1-ctr initial-fill limit removed.
- [x] `deploy/bot-rs/` cone — WSL Linux build + systemd unit (separate confined `cross-arb-bot` user) + out-of-band secrets + log-pull parity. **Overrides 0007** (owner-explicit).
- [x] Linux binary build VERIFIED in WSL Ubuntu-24.04 (rustls → glibc-only, GLIBC_2.34 ≤ droplet 2.39; smoke = safe DryRun banner).
- [x] **DEPLOYED + LIVE 2026-06-16 10:21 UTC** — owner ran `deploy-bot.sh` (agent hard-blocked from the secret push, [L45](lessons.md)); active, 0 restarts, both venues connected, no auth errors.
- [x] **ALL configs enabled** — sports+econ armed via `systemd` drop-in (`ASSUME_*_SETTLED=true`, 10:27 UTC); full 417-pair universe, settle gate cleared, already firing. Logs mirror to `Kalshi/data/cross-arb-bot/`.
- [ ] Watch the recover/naked rate at size 3 ([0027](../decisions/0027-depth-gate-refuted-pmus-book-phantom.md) phantom-liquidity risk); optional: schedule `pull-bot-logs`, DO firewall (inbound SSH only).

## bot-rs — ENGINE REFACTOR: time-complexity + monolith split (2026-06-14) — IN PROGRESS

Owner directive: "complete refactor of the engine code + related parts; minimize time complexity."
Scope chosen (owner): **surgical hot-path complexity wins + split the 2809-line `main.rs` monolith**,
**ZERO behavior change** — bit-identical numerics, all 147 tests + every safety invariant preserved.
Baseline before any edit: **147 passed, 0 failed** (the green gate to hold). Caveat surfaced to owner:
execution is network-bound (~86–261 ms RTT) so this buys CPU/allocation efficiency + clean scaling +
maintainability, **not faster fills**. The discovery join is already pre-indexed O(P+K) — not a target.

### Phase 1 — surgical hot-path complexity wins — ✅ (commit `51c4e9a`)
- [x] `book.rs` `PmusBook` **sorted storage**: ladders sorted once in `apply_snapshot` (bids desc / asks asc);
      `best()` O(n)→**O(1)** via `.first()`; `yes_bid/ask_ladder()` drop the per-read clone+sort (O(n log n)→O(n)).
      API + return types UNCHANGED → all `book` tests pass verbatim.
- [x] `signal.rs` `signal` + `game_signal`: per-call `opts: Vec<(Dir,f64)>` heap alloc → fixed `Option` locals;
      PK-wins-ties + `no_arb`/`crossed` + empty-default semantics IDENTICAL.
- [x] `main.rs` hot loop: non-allocating freshness check (no per-frame `kalshi_tickers()` Vec); `by_slug`
      stored as `Arc<LivePair>` so the per-frame `.cloned()` is a refcount bump (deep clone only on the rare fire path).
- [x] `cargo test` 147 green + `cargo clippy --all-targets` clean; `--smoke` reference captured.

### Phase 2 — split `main.rs` (2970→142 lines; mechanical, zero behavior change) — ✅
Delegated to the coding-agent against hard gates (147 tests / clippy / byte-identical smoke), then
independently re-verified. 7 new modules, all moved items `pub(crate)` (incl. struct fields):
- [x] `pair.rs` (172) — `LivePair`/`PairState`/`SubmitKind`/`FlatKind`/`SubmitOutcome` + `lock`/`pair_tickers`.
- [x] `pricing.rs` (545) — leg planning/building/pricing + `affordable`/`realized_edge_clears_floor`/exit-pricing.
- [x] `bookkeeping.rs` (1274) — exposure/position/recovery/unwind + `apply_outcome`/`qualifying_add` (W-1 verbatim).
- [x] `refresh.rs` (225) — `refresh_loop`/`report_coverage`/`prune_step`/`diff_targets`.
- [x] `smoke.rs` (185) — offline `smoke`/`report`/`fire_legs`.
- [x] `live.rs` (438) — `run_live`/`poll_opt`/`supervise_fatal`/`led_by_from_prior`.
- [x] `test_support.rs` (cfg-test) — the 3 cross-module fixtures (`q_pk`/`wx_pair`/`wc_pair`); tests distributed
      per owning module (pair 2 / pricing 11 / bookkeeping 24 / refresh 2).
- [x] **Re-verified independently:** `cargo test` 147/0, `cargo clippy --all-targets` clean, `--smoke` program
      output byte-identical, `git status` = only `M main.rs` + the 7 new files. Update sessions.md.

## bot-rs — WORLD CUP tradeable (LIVE path) — IN PROGRESS

Plan: `~/.claude/plans/deep-waddling-barto.md` (§1, §2). Python side landed (17ab0ea, `bot/colisted_map.py`).
WC = 3 INDEPENDENT per-outcome BINARY arbs/game (NOT a 3-leg basket). Reuse the weather/econ 1:1 path
entirely; the ONLY changes are DISCOVERY + a routing-MARKER. DO NOT touch exec/Position/unwind/game_signal.

**Design decision (load-bearing):** REUSE `Cat::Sports` (no new `Cat` variant). Traced against every
`Cat`-keyed branch: routing is `kalshi_b==None` (already binary); settlement passes via `settle_clean=true`;
`lock_days(Sports)` is the correct near-dated model (a `Cat::Soccer` variant would mis-route to the 21d econ
fallback); the MLB postpone poll only acts on `league=="mlb"` so WC is never wrongly unwound. Add a minimal
`soccer: bool` to `discovery::Pair`+`LivePair` (the plan's "minimal marker"): carries the regulation-settlement
semantics + lets `track_position` skip enrolling WC in the MLB poll. NOT a routing field.

- [x] `discovery.rs`: SOCCER3 (fwc→KXWCGAME) + SOCCER_CC_ALIAS (irn→iri, alg→dza, hai→hti), SEPARATE from
      LEAGUES_ABBREV. `soc_parts`, `pm_yes_price` (L23 marketSides Yes-side; pmus `price`/`outcomes` STRING-safe).
      `soccer3` assemble branch: group 3 siblings/(a,b,date), bind GAME via `pick_wc_game`(exact-date+used+TIE)
      +alias, require all 3 + TIE, emit 3 PER-OUTCOME BINARY Pairs (kalshi_b=None, soccer=true, settle_clean=true).
      `soccer` on `Pair`; `soccer_pairs`/`soccer_leagues_unmapped` on `Discovery`.
- [x] `main.rs`: `soccer` on `LivePair`/`From`/smoke/tests; `track_position` skips MLB-poll enrollment for a
      soccer pair (empty poll metadata); WC smoke case (binary `signal` arm). `postpone.rs`: skip the non-mlb
      warning for an empty-league position.
- [x] tests (+8): 3 binary Pairs/game; alias join (irn↔iri); draw↔TIE; NO false joins / no partial bind /
      no-TIE skip; unmapped-league report; WC routes binary (leg shape = YES@pmus + NO@Kalshi, not team-B);
      track_position skips MLB poll; moneyline + weather/econ untouched.
- [x] `cargo test` 136 green (was 128); `cargo clippy --all-targets` clean; `--smoke` shows WC binary dry-run
      (Pmus Yes @42c + Kalshi NO @55c on the SAME outcome — the 1:1 shape, contrast the sports `Kalshi Yes -PIT`).
- [x] Self-review (money path) — PROCEED, 0 CRITICAL / 0 WARN / 2 INFO. (a) WC never→game_signal (binary arm,
      proven by leg shape + smoke); (b) per-outcome legs lock (YES@pmus+NO@Kalshi on the SAME outcome);
      (c) no false joins (explicit alias, exact-date, used+TIE-distinct guard — differential-verified byte-
      identical to Python `soccer3_emit`); (d) build_legs/exec/unwind/Position UNCHANGED; (e) moneyline+wx/econ
      untouched. Removed a dead `pm_yes_price` port (live YES book is the per-slug WS frame, not the catalog).
      Artifacts → `tasks/_agent_bus/20260614-wcrust/{coding-agent,code-logic-reviewer}.md`.

## bot-rs — CONCURRENCY-CORE + GATE FIXES (2026-06-11) — engine-fix-B

Adversarial-engine-review fixes on the LIVE order path. Core refactor: route ALL submissions through a
spawned/acked path so the event loop never blocks on network + the unwind arm stays hot. Touch only
main.rs/risk.rs/config.rs/exec.rs/types.rs.

- [x] CORE (C4+C5+W14+W16): `&self` backends + `Arc<dyn …Send+Sync>`; spawn-submit + `outcome_rx` arm;
      `pending_entries`/`flattening` in-flight sets; reserve-on-spawn exposure (+ exact `release_exposure`);
      W14 naked-leg fail-close (live one-leg fill ⇒ runtime halt).
- [x] C3 per-pair freshness `k_fresh`; C6 preserve `prev` on re-track; W16 don't-prune-held; W17 unwrap→let-else.
- [x] C1 supervise spawned tasks (dead collector ⇒ runtime halt + break) + poison-tolerant `lock()` helper.
- [x] GATES: C7 sports `k_b` divergence; C8 settle-clean off-switch consent gate + loud banner; C9 truncated-
      prune skip; W4 affordable−exposure; W5 NaN proximity fail-closed; W6 post-rounding realized-edge re-check.
- [x] 112 tests (was 103; +9 regression) + clippy clean (no NEW warnings) + `--smoke` + C8 refuse/unlock verified.
      Artifacts → `tasks/_agent_bus/20260611-engine-fix-B/`.

## bot-rs — POSTPONEMENT-UNWIND TRIGGER (2026-06-11) — ✅

ARMED the (built+tested) postponement-unwind rule: held-position tracking + live MLB statsapi detection +
the actual SELL-both-legs firing. Closes the #1 sports follow-up (the void/postpone tail is the main
sports risk). Faithful port of `scripts/probe_mlb_postpone.py::unwind_trigger`/`snap`. **98/98 tests;
independent review verdict FAITHFUL + SAFE** (`tasks/_agent_bus/20260611-unwind-parity/`).

- [x] `postpone.rs` (new) DETECTOR: `snap` + `detect_postponement(prev,cur,event_date,market)` — port of
      `unwind_trigger`. POSTPONE_STATES={Postponed,Suspended,Cancelled}; Cancelled/no-makeup → reschedule None
      (→ unwind); **gap measured from the BOUND event_date, NEVER officialDate** (the L3 trap — verified: the
      TB@NYY officialDate-already-moved vector fires UNWIND at 104d, not the trap's 0d→WATCH); officialDate-
      moved-without-status case. ALL Python `_selftest` vectors ported as Rust tests + match the live Python
      selftest. `days_between` = dep-free civil-days.
- [x] `postpone.rs` async `poll_mlb_postponements` (owner/droplet; off all test paths): caches
      `teams?sportId=1` id→abbrev; groups held MLB sports positions by event date; `schedule?date=`; matches
      each game by {team_a,team_b}; snap→detect→`should_unwind` → `UnwindRequest{slug}`. MLB-only (other
      leagues logged no-auto-source). Supervised (a bad pull logs + continues).
- [x] held-position TRACKING (`main`): `positions: Arc<Mutex<HashMap<slug,HeldPosition>>>`. On a fired ENTRY
      where `PairAck.both_filled()` → records the Position from the `[OrderIntent;2]` legs + bumps exposure
      (per_pair/cluster/total/open — was never tracked live, so caps didn't bind: now they do). Game meta
      derived from the slug + the two Kalshi tickers' last segments.
- [x] FIRING (`main`): `select!` over the venue rx + `unwind_rx`; `handle_unwind` prices each leg's exit from
      the live books (SELL YES→yes_bid, NO→1−yes_ask), fires the two SELLs, removes the position + decrements
      exposure. One-sided book → WARN + retry (idempotent `unwind-…` coids; never a one-legged unwind).
      Dry-run LOGS only. **Reduce-only: fires under the kill-switch** (flattening a void reduces risk), logged;
      `CROSSARB_NO_AUTO_UNWIND=1` disables. Smoke demonstrates snap→detect→should_unwind→two SELLs offline.
- [x] `config`: `postpone_poll_s` (60) + `auto_unwind` (default on). discovery `iso_date`/`pm_league`
      → `pub(crate)`. **98 tests** (80 + 18) green; self-review caught + fixed a CRITICAL (dropped `unwind_tx`
      busy-looping the select!); independent review FAITHFUL+SAFE (no CRITICAL/blocking WARN).

**One residual (owner/droplet INFO, not a code gap):** the statsapi join matches the Kalshi-ticker team
suffix to the `/teams` abbreviation (lowercased); if a club's two ever diverge it's a MISSED detection
(never a wrong-game fire) — warrants a one-time live `/teams` smoke on the droplet. Plus the general live
poll/connect verification (same owner step as the other categories).

> **Project docs:** [CLAUDE.md](../CLAUDE.md) (index) · [decisions/](../decisions/README.md) · [lessons.md](lessons.md) · [sessions](../docs/sessions.md)

## bot-rs STAGE-2.5 — SPORTS TRADABLE (2026-06-11) — ✅

Made sports a tradeable category in the live loop. Sports is genuinely TWO-OUTCOME (not 1:1): a pmus
game market (YES = team A) hedges against the OTHER team's SEPARATE Kalshi market. Two cross-venue
configs (port of `monitor.py::game_edge`): **PK** = A@pmus + B@Kalshi (Buy YES @ **Kalshi ticker B**);
**KP** = A@Kalshi + B@pmus (Buy **NO** @ pmus slug = 1 − pm_bid). **80/80 tests; independent parity
review verdict FAITHFUL** (`tasks/_agent_bus/20260611-sports-parity/`).

- [x] `signal::game_signal` — port of `game_edge` (strictly-crossed-pm reject; C3 orientation guard
      |guard_pm − kA_ask|>0.40 covering BOTH dirs; PK/KP marginal-fee nets + both `round4`s; PK-stable max).
- [x] `book::game_depth_at_edge(pm,kA,kB,dir)` — port of `GameTracker._depth` (PK: pm-asks × kB-asks;
      KP: kA-asks × pmus-NO). Cross-venue, dir not swapped. Reuses `depth_curve`.
- [x] `discovery`: emits sports as real `Pair{kalshi:tickerA, kalshi_b:Some(tickerB)}` via a faithful
      `pick_game` bind — **exact slug-ET-date** + **doubleheader `used`-set** (closes the prior parity WARN);
      ±1-day fallback only when slug undated AND globally unique. `days_to_event` = slug-date − today (dep-free
      civil-days, no chrono; pure `assemble` takes `today_epoch_days`, live `discover` passes SystemTime).
- [x] `types::Quote.k_b: Option<Book>` (away-team book; None for wx/econ); `risk` gives k_b the SAME
      crossed/stale gates. `q.k` = Kalshi-A (same team as pm) so the existing C3 mid-div gate still applies.
- [x] **Leg-market FIX (latent live-order bug):** unified `build_legs` emits VENUE-NATIVE ids — Kalshi legs
      carry the Kalshi **ticker**, pmus legs the slug (was: pmus slug sent as the Kalshi leg's ticker → would
      404 live). Per-leg prices from the BOOKS (never the edge); correct sides (sports PK leg2 = YES@KalshiB).
      Smoke-verified: weather Kalshi leg now `market=KXHIGHNY-…` (ticker); sports PK leg2 = `YES @ KXMLBGAME-…-PIT`.
- [x] `main`: sports branch (pm + kA + kB → `game_signal`/`game_depth` → Quote w/ k_b → evaluate → fire);
      `PairState`/`k_tracked`/refresh register + prune BOTH tickers → slug; both books freed on prune.
- [x] `Position` generalized to two explicit legs `[{venue,market,side};2]`; the postponement unwind SELLs
      the correct sports legs (was: yes/no-venue model mis-flattened a sports pair). `unwind` + smoke updated.
- [x] All 80 tests green (66 + 14 new) + clippy clean; self-review (0 CRITICAL); **independent parity review
      FAITHFUL** by differential Python re-exec (PK/KP nets bit-for-bit; C2 exact-date+doubleheader; C3 flip).

**Remaining for sports beyond this (owner/droplet — same as the other categories):** live statsapi
postponement DETECTION + held-position tracking to ARM the unwind rule (the decision/orders are built +
tested; only the live trigger is unwired); sports settlement recon (endDates ~06-23/25) before
`ASSUME_SPORTS_SETTLED` is anything but an owner override; demo-sandbox confirm a real two-ticker game fires.

> **Project docs:** [CLAUDE.md](../CLAUDE.md) (index) · [decisions/](../decisions/README.md) · [lessons.md](lessons.md) · [sessions](../docs/sessions.md)

## bot-rs stage-2 NETWORK LAYER (2026-06-11) — ✅

Build the venue I/O + live transport that completes the Rust bot (compile + unit-test only; live
connect/orders are the owner's droplet step). Safety spine (42 tests) stays green.

- [x] `venue.rs` — Kalshi WS client (RSA-PSS handshake, `orderbook_delta` sub, snapshot+delta merge into
      `book::KalshiBook`, single-sid seq-gap → reconnect) + pmus WS client (Ed25519 handshake,
      `SUBSCRIPTION_TYPE_MARKET_DATA` sub, `marketData` frame → `book::PmusBook`). Supervised reconnect
      with backoff. Pure frame-PARSERS unit-tested against embedded sample JSON (no live connection).
- [x] `main.rs` → `#[tokio::main]`; keep dry-run `smoke` (no-creds / `--smoke`); add the live loop:
      connect both venues, maintain books per pair, build a `Quote` on each dual-venue update,
      `risk::evaluate`, `exec.submit_pair` (dry-run default). Honors kill-switch + prod-consent gate.
- [x] `exec.rs` `LiveBackend::submit_pair` — REAL signed POST. Kalshi `POST /portfolio/orders`
      (auth::kalshi_headers + build_kalshi_payload); BOTH LEGS CONCURRENT (`tokio::join!`). pmus
      best-effort POST with a loud `// TODO verify pmus POST signing live` (typed error if uncertain).
      Gated: keys absent → `KeysUnavailable` (never sends in sandbox); dry-run path unchanged; trait dyn-compatible.
- [x] Full `cargo test` green (53; 42 existing + 11 new venue/payload/leg-pricing) + self-review (artifacts:
      `tasks/_agent_bus/20260611-0940/`). **Self-review caught + fixed a CRITICAL** (live loop fired the pair
      cost as the YES-leg limit → NO leg couldn't fill → naked leg; now book-derived per-leg prices + regression test).
- **Stage-2 follow-ups (out of this run's network scope, must close before real-money arm):** colisted-map
      DISCOVERY port (fills the live loop's `pairs`); per-venue book `age` (the staleness gate `Reject::StaleBook`
      is inert while books are fed `age_s=0.0`); pmus POST-body signing live-verify; demo-sandbox session.

## bot-rs DISCOVERY + STALENESS (2026-06-11) — closing the last two functional gaps ✅

- [x] `discovery.rs` (new): async catalog pull BOTH venues (reqwest/rustls, PUBLIC/no-auth). pmus
      `?closed=false&limit=500&offset=` (page<limit = last); Kalshi `?series_ticker=&limit=&cursor=`
      (empty cursor = last). Pure slug PARSERS (weather city, econ fam/ineq/period/thr, sports
      league/abbrevs/date) → apply `matcher.rs` joins → `Vec<Pair>` (weather+econ subscribable 1:1;
      sports matched+counted only — 1:1 loop can't price two tickers). UNIT-TESTED on embedded JSON
      (joins + no-false-joins, incl. the L21 econ off-by-one + GDP full-date period); live pull NOT tested.
- [x] Staleness: `KalshiBook`/`PmusBook` carry `last_update: Instant`, stamped on every applied
      snapshot/delta; `touch()` derives real `age_s` from it. Loop's per-leg gate = worst-of-two-legs →
      `Reject::StaleBook` fires on a wedged stream (deterministic backdate test, no sleep).
- [x] Wired into `main.rs`: initial discover → seed shared `tracked` + `PairState`; periodic
      `refresh_loop` (`DISCOVERY_REFRESH_S`, default 300) re-discovers, diffs (`diff_targets`), prunes
      (2-miss `prune_step`), applies in-place add/delete + frees pruned books. Dry-run + all gates intact.
- [x] venue.rs: in-place subscribe-set update via a `SubUpdate` control channel — Kalshi no-gap
      `update_subscription` add/delete on the captured sid; pmus add-shard, delete local no-op. Fair select.
- [x] `cargo test` GREEN (**66**: 53 existing + 13 new) + clippy clean (new code) + self-review
      (artifacts: `tasks/_agent_bus/20260611-1015/`). **Self-review caught + fixed a CRITICAL** (GDP econ
      pairs silently dropped — Kalshi period parser truncated the day vs the python `\d{0,2}`; fixed + test).
      Live-verify pending on the droplet (catalog HTTP, WS sid-capture, `update_subscription` acceptance).
- [x] **INDEPENDENT parity review** (no authorship stake, verdict **FAITHFUL** — artifacts:
      `tasks/_agent_bus/20260611-parity-review/`): the econ/weather/sports decoders in `matcher.rs` +
      `discovery.rs` confirmed byte-faithful to `colisted_map.py` by **differential execution vs live
      Python** (incl. the float-nasty `4.4−0.1` twin case + the GDP `26JUL30` period). **The L21 12.2¢
      econ phantom CANNOT recur** through the live order path (twin direction, grid_step, threshold
      rounding, both period parsers all match). One WARN: sports date-binding looser than Python
      `pick_game` — but sports never becomes a tradeable `Pair`, so zero live-path exposure.

### bot-rs — what remains (all owner/droplet, OR the deferred sports feature)

The bot layer is **functionally complete + parity-verified** (66 tests; discovery → WS books w/ real
`age` → matcher → signal → risk → concurrent dry-run exec; live transport gated). Nothing further is
doable in Claude's sandbox — the rest is inherently the owner's environment:

- [ ] **Demo-sandbox session** (owner): `EXECUTION_MODE=live VENUE_ENV=demo` — confirm WS sid-capture,
      `update_subscription` acceptance, the live catalog HTTP shapes, and a clean dry→demo order round-trip.
- [ ] **pmus POST-body signing** (owner): the order POST signs `{ts}{METHOD}{path}` only — verify live
      whether pmus requires the body in the canonical string (brief-flagged; typed error until confirmed).
- [ ] **Sports subscribable (stage-2.5 feature, not a gap):** the 1:1 loop can't price a two-ticker game;
      before making sports tradeable, port `pick_game`'s exact-ET-date + doubleheader `used`-set guard
      (the parity-review WARN) so the C2 wrong-game join can't reach the order path.
- [ ] **Edge validation gates the money, not the code:** even green, do NOT arm beyond demo until the
      0014 confirmatory run passes on multi-week data + the naked-unwind cost is measured (README ⚠️).
- [x] **edge-RATE allocation (0014-H2, `edge ÷ lock-days`) — DONE 2026-06-13 ([0017](../decisions/0017-live-edge-rate-lock-days.md)).**
      Still deferred: **maker-side execution mode** (rest cheap leg on Kalshi + taker-hedge pmus — the maker
      study's +EV config; needs the now-logging trade-print/ladder data to model fill rates).

> **Project docs:** [CLAUDE.md](../CLAUDE.md) (index) · [decisions/](../decisions/README.md) · [lessons.md](lessons.md) · [sessions](../docs/sessions.md)

## Reviewer audit (2026-06-09) — fixes ([reviewer-audit-2026-06-09.md](reviewer-audit-2026-06-09.md))

Second adversarial review (92-agent find→verify pass + manual cross-read). 8 CRITICAL root causes + cheap
WARNs. **All landed + self-tests green this session (2026-06-09):**

- [x] **C1 — `ledger.enter()` bypasses crossed/no-arb guard** → now refuses crossed/stale/no-arb + unpriceable
      dirs; S6 regression added. *(+ WARN: mtm/unwind None-guards for one-sided books)*
- [x] **C2 — sports date-join binds the WRONG game** → now uses the pm slug ET date + exact-match (`pick_game`),
      unique-±1 fallback only when slug undated. Mirrored `colisted_map.py` + `scan_all.py`. (173 live pairs.)
- [x] **C3 — sports leg ORIENTATION unverified** → `game_edge` >40c orientation/identity price-guard (live path);
      `verify_sports_settlement.py` written.
- [x] **C4 — weather bucket pairing was a blind positional zip** → pair only on canonical inclusive `[lo,hi]`
      boundary equality (`pm_bounds`/`kbounds`, live-verified convention); loud MISALIGNED report. All 3 matchers.
      (60 live pairs, 0 false misalignments.)
- [x] **C5 — sports settlement-identity** → `scripts/verify_sports_settlement.py` (source + void/postpone diff
      per league); CLAUDE.md tempered (sports cleanliness UNVERIFIED). *Owner: run it live per league.*
- [x] **C6 — monitor WS streams now have supervised reconnect-with-backoff** (clean-close half-dead hole closed);
      `return_exceptions=True`; per-venue `rx_age` in the health beacon.
- [x] **C7 — econ-legality corrected** across CLAUDE.md + README + research/README + decisions/0001 + catalog brief.
- [x] **C8 — capital/profit de-double-counted** (`one_per_market`) + book-average (trapezoid) profit; self-test.
      Corrected live headline: peak ≈ **$13.6k** (was $100k), **~6%/day** (was 8.2%). *(+ persistence headline now
      depth/age-gated + excludes restart-censored; `load()` per-date dedup.)*
- [x] **WARN/INFO sweep** — `smatch` ≤1-char prefix guard + `colisted_map._selftest`; `weather_arb_scan` date
      de-hardcoded (5 cities); pull-data TOCTOU re-hash; security/deploy egress hardening + RO-key warning +
      `.env.example`; settlement-VERIFIED / `age` / brief-count wording.
- [x] **Settlement residuals closed (2026-06-09, same session):**
      - [x] **Weather-FAQ timing contradiction RESOLVED** (WebFetch) — pmus FAQ *does* specify 8 AM / 11 AM-if-
            CLI≠METAR (catalog brief was right; verification brief corrected). Asymmetry narrowed, not eliminated.
      - [x] **Middle 2° bucket boundary VERIFIED** — SFO `66-67°` ↔ pmus `gte66lt67f` both `[66,67]`, same
            source/station; enforced by the `colisted_map.py` guard.
      - [x] **Sports settlement verifier RUN** → finding: clean for completed games, **void/abandonment tail
            diverges** (esports → pmus "last fair price" vs Kalshi silent; tennis >2wk reschedule → $0.50).
            New brief [research/sports-settlement-verification.md](../research/sports-settlement-verification.md).
- [x] **Per-league sports void read DONE (2026-06-09, 10 leagues)** — material finding: **MLB** Kalshi
      reschedule window is **2 days** vs pmus **2 weeks**, so a game replayed in that gap settles real-winner on
      pmus but fair-price-void on Kalshi → both-legs loss on the proof case. Esports/WNBA: pmus last-price vs
      Kalshi silent. In [research/sports-settlement-verification.md](../research/sports-settlement-verification.md).
- [x] **Sports-void EV term BUILT + gate added (2026-06-09):** `capital_sim.void_haircut()` charges the MLB
      postpone divergence (`P(postpone)1.3% · P(2d–2wk gap)0.4 · loss0.5` ≈ 0.26c/contract MLB, 0.10c other
      sports; `--void-mult` knob) — sports profit drops ~7% ($825→$767/day). `colisted_map` tags every sports
      pair `void_clean=False`. Decision [0010](../decisions/0010-all-in-edge-filtering-and-cost-model.md) item 3b.
      Postpone rate grounded in mlbschedulegrid.com (29/31 per ~2430 games, 2024/2023).
- [x] **Read-only execution-feasibility tests built + run (2026-06-09):** `latency_probe` (RTT ~86–261ms,
      network-bound → Rust deferred, Tier 4), `shadow_fill` (leg-fill the gating risk; hit-rate collapses with
      latency), `settle_recon` (invariant #1 empirically open — pmus finalization lag), `adverse_selection`
      (instrumented). Acted: `monitor.py` now logs **ms timestamps** + per-venue `px`. See
      [execution-feasibility brief](../research/execution-feasibility-2026-06-09.md) + [latency-playbook](../research/latency-playbook.md).
- [x] **GATED redeploy DONE (2026-06-10 UTC, owner-greenlit [0006](../decisions/0006-deploy-on-digitalocean-consult-first.md)):**
      droplet brought from a pre-`dabc106` build (integer-second `t`, no `px`, **no ECON**) to current HEAD
      (`monitor.py` sha verified byte-identical to local). Verified live on disk: ms-precision `t`
      (`…082.402`), per-venue `px` touches, **ECON now tracked** (U-3 `urc-…-atl4pt4` OPEN net **0.1222**,
      depth c2=423 — first econ edge ever captured), event-date partitioned (`transitions-2026-07-02.jsonl`).
      The multi-week accumulation clock effectively **restarts now** on the correct schema.
- [x] **Clip-stage allocation tested + phantom fix (2026-06-10, [0012](../decisions/0012-clip-allocation-edge-floor-and-phantom-filter.md), [brief](../research/allocation-policy-2026-06-10.md)):**
      FIFO-by-arrival loses to a **2¢ edge floor + a deploy-to-full per-pair cap** (~+64% to +269% over FIFO
      out-of-sample, 11 diversified pairs); the "wait 1 s + sort" idea captures only 3.6% of the gap; clip-cap
      alone is **risk-control, not PnL** (−3% OOS). Found + fixed a **book-init phantom** (37.7¢ ITF tennis,
      depth 690, captured 1.5 s post-resubscribe in a restart storm, `censored=restart`) that was **75% of the
      old in-sample headline** — `capital_sim.capturable()` now drops restart-censored ([L20]); candidates
      226→219, in-sample +7127%→+1744%, **OOS unchanged**. New `alloc_policy_experiment.py` +
      `clip_threshold_test.py`. **Method demo on 0.81 d** — re-run on the multi-week data.
## Full review 2026-06-10 — ALL FIXES LANDED ([0013](../decisions/0013-econ-grid-step-twin-and-measurement-integrity.md), [econ brief](../research/econ-settlement-identity-2026-06-10.md), lessons [L21]/[L22])

Third adversarial pass (full code+data+docs, live-API verification). **17/17 self-tests green; full
backtest pipeline re-run on the corrected pipeline.**

- [x] **CRITICAL — econ pairing off by one bucket**: pmus `≥T` (inclusive) was joined to Kalshi "Above T"
      (STRICT, `strike_type: greater` — verified live). The persistent **12.2¢ U-3 "edge" was the
      market-priced P(print==T)** (proven: pmus ≥4.4 mid 0.275 vs twin T4.3 mid 0.33 vs old partner T4.4
      mid 0.105). Fixed: `econ_twin` joins `floor = T − grid_step` (24 pairs → **14 identical** + 13 honest
      skips; remapped pairs verified live = no phantom edges); pre-remap econ records **quarantined** in
      `load()`; **headline correction: allocation OOS +64%/+269% → +9%/+141%** (econ-free, 10 pairs).
- [x] **CRITICAL — debouncer stamped CLOSEs at flush time** (+1.0–1.5s on EVERY duration; sub-second regime
      structurally unmeasurable). Fixed: detection-time stamps; pre-fix data lag-corrected −1.25s in
      `build_episodes` (clamped). **Corrected shadow-fill: leg-fail 55.5% @1s / 62.7% @2s** (was 39%/67%);
      ~27% of capturable ≥1¢ episodes die ~instantly.
- [x] **HIGH — WS reconnects now write `ws_reconnect` markers** (both venues, both drop+clean paths);
      `load()` censors them like restarts/resyncs (reconnect-rebuild phantoms were invisible).
- [x] **HIGH — heartbeat supervised** (was: one send-race exception killed discovery/prune/beacon forever);
      **degraded discovery (fetch_errors) skips pruning** (an API outage looked like mass settlement →
      uncensored re-OPEN phantoms); `get()` retries transient errors.
- [x] **HIGH — single-subscription Kalshi invariant**: seq gap / new tickers CYCLE the connection (the old
      in-place second `subscribe` + gap-resubscribe ran on never-probed semantics — silent no-data or a
      seq-counter storm).
- [x] **MEDIUM sweep**: weather pairing = bounds-dict join (offset listings no longer zero out a date);
      econ Dec/Jan year-boundary fix; doubleheader guard (`pick_game` used-set) + duplicate-ticker
      registration guard; `game_edge` ±40¢ orientation guard now covers one-sided pm books;
      `analyze_persistence` headline stats restricted to measured episodes; `shadow_fill` counts FLIP-
      before-fill as leg-fail; `scan_all` imports the bot's matchers + MARGINAL detection fees (private
      copies had drifted, an [L15] violation) + same host; `exit_liquidity` TRADEABLE requires bids;
      `adverse_selection` uses the shared loader + censor-aware pairing; `cod` league mapped (KXCODGAME,
      live-verified).
- [x] **Phantom hardening (no monitor-schema change needed)**: `build_episodes` keeps BOTH venue ages
      (`open_age_p/k`) + an `open_flat` (c2==c1==c0) flag; `capturable(drop_flat=True)` opt-in lens ([L15]).
- [x] **LOW sweep**: maker fee = `ceil(0.0175·N·P(1−P))` per venue-audit §2.1 (was 0.25× the ceiled taker);
      ledger S4 print; `pm_catalog` cap warning; `cli_stream` seeds dedup state from cli.jsonl (no more
      8× restart re-logs); `session_start` logs `build` hash + argv (deploy-vs-crash forensics);
      `selftest_all.py` one-command test gate; doc drift (bot/README fees per-ORDER, etc.).
- [x] **GATED REDEPLOY DONE (2026-06-10 09:03 UTC, owner-greenlit, [0006](../decisions/0006-deploy-on-digitalocean-consult-first.md)):** droplet on the 0013
      build — sha **byte-identical** to local (`bfa9e2fccf15` monitor / `4bc9c241…` colisted_map),
      `session_start` now self-identifies (`build` + argv), tracking **30 weather + 307 sports + 14 econ**
      (remapped twins; old build's last session said 24). First new-schema records verified on-disk
      (detection-time ms stamps + px + depth). `ECON_REMAP_DEPLOY_TS = DEBOUNCE_STAMP_FIXED_TS =
      1781082189` set in `analyze_persistence.py`; pre-epoch econ stays quarantined, post-epoch is clean.
      **The multi-week accumulation clock restarts here on the corrected schema.**
- [x] **Live-WS smoke test DONE (2026-06-10, owner-greenlit):** bounded `--live 75` ran clean — 351 pmus /
      658 Kalshi subscribed, remapped econ pairs priced, OPEN/CLOSE/WIDEN/NARROW logged at detection-time
      ms precision, CLI dedup-seeding worked, clean shutdown. (A logged 13¢ U-3 dir-P record was inspected:
      a REAL wide-book dislocation — pmus 0.47/0.69 vs Kalshi 0.84/0.87 on the now-identical ≥4.2 bucket —
      with flat-ladder + age instrumentation attached for fillability analysis; not a settlement phantom.)
- [x] **Multi-subscription probe RUN + no-gap add SHIPPED (2026-06-10):** `probe_kalshi_ws.py --multisub`
      live-verified the semantics (ONE sid per channel — a 2nd subscribe MERGES, no 2nd seq counter; control
      acks consume seq slots so SeqTracker stays contiguous across add/delete; add snapshots only the new
      tickers). Monitor now adds in-place (`update_subscription add_markets` + snapshot-confirm w/
      `ADD_CONFIRM_SECS` cycle fallback) and `delete_markets`-unsubscribes pruned tickers; seq gaps still
      cycle. Offline integration test `scripts/test_monitor_nogap.py` (in `selftest_all`, 18/18 green).
      Kills the censored ~650-ticker rebuild the old cycle-on-add caused (4 such cycles seen in 1 h of
      2026-06-10 droplet data). *Shipped to the droplet in the 2026-06-11 redeploy (build `f8f261298097`).*
- [x] **Fee schedules RE-PINNED from primary sources (2026-06-10, [research/fee-pin-2026-06-10.md](../research/fee-pin-2026-06-10.md)):**
      all 4 coefficients CONFIRMED (Kalshi taker verbatim via the CFTC-filed schedule — kalshi.com PDF still
      429s; pmus via docs.polymarket.us/fees eff. 2026-04-03 + live `feeCoefficient=0.05`). Real finding:
      **Kalshi maker fees exist only on `quadratic_with_maker_fees` series — 12/23 tracked (all 5 weather,
      esports, ITF, UFC) charge makers $0**, and pmus REBATES makers −0.0125 → a weather maker-maker
      round-trip is fee-negative (~−0.3¢) vs ~3.5¢ taker-taker. `fee_multiplier=1` all 23 series;
      `/series/fee_changes` (live tripwire) empty. `kfee(taker=False)` documented as series-blind
      (selftest-only today; the maker study models `fee_type`).
- [ ] **Now: let it run ≥ weeks + re-pull**, then re-run on the new-schema data: `shadow_fill`
      (sub-second leg-fill at ~150ms — measurable only on post-0013 data), `adverse_selection` (toxic-close
      share from `px`), `settle_recon` (after pmus markets pass `endDate`), `analyze_persistence`/`capital_sim`
      (multi-day edge), and a first **ECON** persistence/depth read on the 14 identical pairs.
      *(Weather settle-recon CLOSED 2026-06-11 — 360/360 identical; sports recon unlocks ~06-23/25
      post-`endDate`; econ recon 06-18 (FOMC) + 07-03 (U-3/NFP) — [probe brief](../research/probe-program-2026-06-11.md).)*
- [ ] **Still needs the trade layer or in-season data:** (a) the **bot unwind rule** (close MLB before Kalshi's
      2-day window; reads `void_clean`); (b) latency-haircut from a real order-ack study + leg-fill EV (0010 items
      2/3); (c) `p_gap`/`loss_frac` refinement; (d) NBA/NHL settlement read in season; (e) CLI-revision rate.

## NEXT SESSION — bot-rs features deferred 2026-06-11 (owner directive)

Two strategy features designed + reviewed this session but explicitly deferred to next session for
implementation in `bot-rs` (the rest of the rust-review/strategy-test findings are already built):

- [x] **Edge-RATE allocation layer (DONE 2026-06-13, [0017](../decisions/0017-live-edge-rate-lock-days.md)):**
      `risk::lock_days` re-derives per-category lock-days to **corrected days-to-grade** (weather 1.2 floor /
      **sports = dynamic `days_to_event`**, was a flat 15 / econ 21 fallback — no release calendar in the bot);
      `edge_rate = booked_edge ÷ lock_days` is always computed + returned in `Approved` (logged); a **reservation
      floor** `MIN_EDGE_RATE_CPD` (new `Reject::BelowEdgeRateFloor`, **default 0 = OFF**) skips low-velocity arbs.
      Reservation-only (sizing untouched); batch-ranking deliberately out of scope (latency). **0014-H2's frozen
      backtest priors NOT touched** (live ≠ confirmatory; reconciliation is a labelled sensitivity arm at
      data-arrival). 128 tests (+3), clippy clean, smoke shows a 1-day sports arb @ 3.0¢/$-day > weather @ 2.5.
- [ ] **Maker-side execution mode** — `bot-rs` is taker-only; add a maker/conditional-hedge mode that
      rests the cheap leg on Kalshi (weather maker fee $0) + taker-hedges pmus — the maker study's only
      +EV config ([research/probe-program-2026-06-11.md](../research/probe-program-2026-06-11.md) §1).
      Needs the trade-print/ladder data (now logging) to model fill rates, and per-series `fee_type`.

## Next-session explorations — policy + strategy upgrades (queued 2026-06-10)

Owner-reviewed suggestions from the post-0013 policy read (OOS tables in the corrected pipeline).
Ordered by expected value; none are decisions yet — each is an experiment or spec item.

- [x] **Pre-registered (2026-06-10, [0014](../decisions/0014-preregistered-allocation-rule.md),
      [research/allocation-prereg-2026-06-10.md](../research/allocation-prereg-2026-06-10.md)):** H1 = τ=2¢
      booked floor + category caps weather 20% / sports 10% / econ 5%, $500 (+$2k robustness), frozen
      economics; H2 = edge-RATE ordering (lock-days frozen upstream); post-epoch data only, run at ≥14
      event-days (≥30 candidates or descriptive-only), disjoint 3-day folds, four-cell lever decomposition,
      per-position bootstrap, friction arm, fixed pass/fail gates. Stats-methodology audit run pre-freeze
      (findings incorporated; report under `tasks/_agent_bus/`). The 0012 cap sweep is superseded — no
      swept row may be quoted as the result.
- [ ] **Edge-RATE ranking (the one real policy upgrade):** reservation on
      `booked_edge / expected_lock_days` instead of edge-level — capital velocity is the binding
      constraint and a flat τ gets categories backwards (13¢ U-3 locking ~22 d = **0.6¢/$-day** vs a
      3¢ weather arb locking 1.2 d = **2.5¢/$-day**). Lockups exist in `capital_velocity.py`,
      settlement proxies in `settle_t`; ~small change to the `_profit_per`-based ranking; test with
      the existing OOS harness on the multi-week data.
- [ ] **Maker-side execution study (attacks the gating risk + the fee wall at once):** rest the cheap
      leg as MAKER on the wide/sleepy venue (pmus weather quotes 20¢+ spreads), take the Kalshi side
      only AFTER the maker fill (conditional hedge at the measured ~86–261 ms). Fee asymmetry pays for
      it — **stronger post fee re-pin ([fee-pin brief](../research/fee-pin-2026-06-10.md)): Kalshi weather/
      esports/ITF/UFC charge makers $0 and pmus REBATES −0.0125, so a weather maker-maker round-trip is
      fee-negative (~−0.3¢) vs ≈3.5¢ taker-taker** → widens the +EV universe below the 2¢ taker floor
      AND shrinks the 55%-naked-@1s tail. Must model per-series `fee_type` (kfee is series-blind). Largely
      simulatable READ-ONLY from book data (quote-presence sim) before any capital.
      **PROBED 2026-06-11 ([brief](../research/probe-program-2026-06-11.md) §1, `maker_feasibility.py`): the
      rest-on-pmus premise INVERTS — the sticky wide pmus quote fills only on bucket-death moves Kalshi already
      repriced (15–16¢ hedge slip, −EV); the one plausibly +EV config is rest-on-KALSHI + taker-hedge-on-pmus
      (+0.14–0.44¢/attempt, bounded). Trade-print + ladder logging BUILT → becomes a measurement after the
      next gated redeploy.**
- [ ] **Fill-contingency rule in the policy spec:** if leg B unfilled within X ms of leg A → exit leg A
      at market immediately (known small insurance premium vs unbounded naked coin-flip); price it into
      the all-in edge filter as 0010's leg-fill EV term. Extend `shadow_fill` to simulate it from `px`.
      *(Probe #2 priced the inputs 2026-06-11: breakeven naked-unwind ≈4–6¢ @150–261 ms vs ~1–3¢ plausible
      actual cost; survivors keep ~1.9¢ median — the insurance premium looks affordable.)*
- [ ] **Adverse-selection gate on direction** (needs accumulated `px`): prefer arbs whose DEAR side
      moved away (benign line-lag) over ones whose CHEAP side led (informed quote — the U-3-style wide
      sleepy book where the tighter venue is righter). Use `adverse_selection.py` toxic-close share.
      **PROBED 2026-06-11 (§5): NULL as a skip gate (z=−0.68 overall, n=217/217); weather-only
      leg-SEQUENCING signature (cheap-made 18% vs dear-made 79% toxic, z=4.6, small cells) —
      pre-register, then confirm at ~1 wk of post-0013 data (cells n≈150–250).**
- [x] **Correlated-exposure cap per event cluster** (city-date / game), alongside the per-pair cap — a
      single CLI-revision day hits every same-city weather pair at once; per-pair caps don't bound it.
      **MEASURED + knob built 2026-06-11 (§9, exploratory): max same-(city,date) share 20% today (one pair
      at its own category cap); 20/30% caps bind 0 fills; 10% costs −13.6% PnL → pure risk-control. Opt-in
      `--cluster-cap` lives in `recycle_arm_experiment.py`; size it when `cli_revisions.py` has weeks of data.**
- [x] **Recycle-time re-evaluation in the sims:** when settlement frees capital, re-score all still-open
      arbs (arrival-or-never skips them today); the live monitor gives this for free — the backtest
      should model what the bot will actually do.
      **MEASURED $0.00 2026-06-11 (§8, exploratory): structurally starved — skipped arbs die ~1 s median
      (0% survive ≥1 h) vs hours-to-days skip→settle gaps, and capital never binds under H1. Deprioritized;
      one-command recheck on the multi-week pull.**
- [ ] **Weather-first scaling note:** the only category with {identity ✓, fast capital ✓, exit ✓};
      constraint is crossable depth (~157 contracts) → growth = breadth (cities × buckets × days) +
      layering on WIDEN, never bigger clips in one corner ([L16]).

### Probe program — 2026-06-11 UTC (owner directive: "probe these", all read-only) — **DONE, all 10**

Synthesis: [research/probe-program-2026-06-11.md](../research/probe-program-2026-06-11.md); per-item
notes in `tasks/_agent_bus/20260611-probes/`. Data basis: fresh pull, ~31k post-0013-epoch records
(~16 h, t ≥ 1781082189) + live API reads. Selftest gate after all changes: **19/19 green**.

- [x] #1 maker-side weather: **config INVERTS** — rest-on-Kalshi + taker-hedge-pmus is the only plausibly
      +EV mode (+0.14–0.44¢/attempt bounded); rest-on-pmus toxic (15–16¢ slip). Ladder/trade logging
      spec'd + BUILT (`maker_feasibility.py`, [spec](_agent_bus/20260611-probes/ladder-logging-spec.md))
- [x] #2 sub-second leg-fill: naked 17–23% @100–150 ms (n=575) — **taker NOT rejected at measured RTT**;
      ~29% die <250 ms (never raceable); re-read at ~1 wk + first econ release
- [x] #3 no-gap build: 18/18 + integration green, droplet sha verified vs HEAD; old build censors ~310
      episodes/day (~152/day avoidable) — **REDEPLOYED 2026-06-11 02:55 UTC (owner-greenlit, 0006)**:
      droplet on build `f8f261298097` (sha byte-verified = local), session_start self-identified,
      60 weather + 216 sports + 13 econ tracking, NRestarts=0; rollback ref = HEAD `bfa9e2fccf15`
- [x] #4 settlement recon weather: **CONFIRMED 360/360** (3-way vs NWS CLI, incl. a real revision day);
      pmus sibling-array parse bug found+fixed → "interim verified WRONG" RETRACTED ([L23]); sports recon
      ~06-23/25, econ 06-18 + 07-03
- [x] #5 direction gate: skip-filter NULL overall; weather leg-sequencing signature (18% vs 79% toxic,
      z=4.6) → pre-register, confirm at ~1 wk
- [x] #6 early-exit: **HOLD-ALL** (exit-all ≈ −1.5¢/pair; boundary maker-exit breakeven P(flip)=2% vs
      0/14 station-days measured); revisit only if `cli_revisions.py` shows P>2% (`early_exit_ev.py`)
- [x] #7 MLB: window is **MINUTES** (Kalshi closed voided mkts 47–90 min post-start, n=3); 5-min statsapi
      poll detects 5/5; unwind +12–13¢/contract on trigger; rule spec written (trade-layer item)
- [x] #8 recycle arm: **$0.00 every cell** (structurally starved) — deprioritized; one-command recheck on
      the multi-week pull (`recycle_arm_experiment.py`, exploratory)
- [x] #9 cluster cap: max same-(city,date) exposure 20% today; 20/30% caps bind nothing, 10% costs −13.6%
      → opt-in risk knob only (same script, exploratory)
- [x] #10 fee tripwire: **BUILT** — `healthcheck.ps1` poll+alert active now (laptop); monitor-heartbeat
      half (`fee_changes.jsonl` + beacon field) rides the next gated redeploy
- [x] **Wave-2 redeploy bundle implemented + tested (19/19):** weather trade prints (Kalshi REST
      cursor-poll → `trades-<date>.jsonl`), top-5 ladders (`ladders-<date>.jsonl`, detection-time `tr` +
      delta-suppressed 300 s `hb`), fee tripwire, `pull-data.ps1` finalize/delete extended to the new
      prefixes; new offline test `test_monitor_trades_ladders.py`; transitions schema untouched
      (asserted). ≤ +9.5 MB/day raw (~+1.1 gzipped). **Shipped in the 2026-06-11 redeploy.**
- [x] **Flagged-item fixes (2026-06-11, owner-directed):** (a) `pull-data.ps1` recreate-after-delete
      hazard FIXED — a late-append-recreated archived day is kept RAW beside its canonical `.gz`
      (never re-gzipped/overwritten; loaders read both, per-date dedup covers overlap); (b)
      `weather_spread_snapshot.py` pmus read fixed to marketSides-primary ([L23]); (c) daily
      settle-recon step added to `pull-data.ps1` (runs at the ~12:30Z scheduled pull = inside the
      previously-unobserved 0–13.5 h pmus-finality window; ALERT.txt on DIVERGE; `CA_NO_RECON=1`
      to skip). Selftest gate re-run post-fixes: 19/19.

## Done (discovery → matcher → scanner → monitor)
- [x] **1–5. Sports matcher** — Kalshi game structure discovered; robust `(league, date, abbrev)` join
      (`sports_match_v2.py`); 2-outcome arb metric; **no false positives** (price-sanity guard, [L1](lessons.md)); MLB ~$23.
- [x] **6–8. Complete coverage** — weather = HIGH temp, 5 cities (all map to Kalshi); 12 co-listed
      moneyline leagues; tennis/UFC/ITF surname matcher (`sports_name_match.py`). No pruning
      ([0002](../decisions/0002-comprehensive-coverage-no-pruning.md)).
- [x] **9. Unified scanner** (`scan_all.py`) — entire co-listed universe, uniform metrics, nothing
      pruned → `_data/scan_all.json`.
- [x] **10. Persistence monitor** (`bot/monitor.py`) — **BUILD COMPLETE, live-verified.** Event-driven
      dual-stream logger ([0003](../decisions/0003-event-driven-persistence.md) · [0005](../decisions/0005-dual-stream-persistence-monitor.md)):
      polymarket.us WS (Ed25519) + Kalshi `orderbook_delta` WS (RSA-PSS; `kalshi_book.py` snapshot/delta
      merge), self-discovering + coverage-audited map (`colisted_map.py`, [0008](../decisions/0008-colisted-map-discovery-and-coverage-audit.md)),
      weather `MarketTracker` + sports `GameTracker`, FLIP debounce, dynamic re-subscribe. Logs
      OPEN/CLOSE/FLIP/WIDEN/NARROW → `_data/transitions-<event-date>.jsonl` (event-date partitioned, 0009).
- [x] **Accounting core** (`bot/ledger.py`) — self-verifying PnL; layer-by-default rotate rule
      ([0004](../decisions/0004-ledger-layer-by-default.md)).

## Next
- [x] **Idle-market pruning** — `run_live` frees settled markets (gone from discovery for
      `PRUNE_THRESHOLD=2` heartbeats; symmetric teardown). Verified flat heap ~7 MB over 30 sim-days vs
      ~216 MB unpruned (`scripts/probe_monitor_footprint.py`) → droplet = **1 vCPU·1 GB·NYC1·Ubuntu 24.04**.
- [x] **DEPLOYED + LIVE (2026-06-09)** — monitor runs 24/7 on the DO droplet (`cross-arb-droplet`,
      `198.199.67.245`) as a confined **`cross-arb`** user (owns only `/opt/cross-arb`, `0700`;
      `ProtectSystem=strict`). `deploy/deploy.sh` ships only the runtime cone via scp; secrets out-of-band.
      Both streams up, logging real MLB + weather transitions. RSS ~76 MB.
- [x] **Foolproof data pipeline** ([0009](../decisions/0009-event-date-partition-copy-keep-pull.md)) —
      monitor partitions by **event-date** (`transitions-<date>.jsonl`, lifecycle never split at midnight) +
      `sessions.jsonl` restart marker. `deploy/pull-data.ps1` = copy-keep + sha256-verified + idempotent
      mirror to `Kalshi/data/cross-arb/`, gzips finalized days; scheduled daily (`PullCrossArbData`, 8:30am).
- [ ] **Let it run + pull** — accumulate ≥days of `Kalshi/data/cross-arb/` data; spot-check the daily pull.
- [x] **Persistence-analysis harness** (`scripts/analyze_persistence.py`) — reconstructs edge episodes
      (OPEN→CLOSE per market, restart-aware via `sessions.jsonl`) → edge-magnitude / persistence (fill
      window) / capturable-rate / scalability proxy. Self-tested; validated on preliminary data (0.8 h:
      median edge ~0.7c, median duration ~2 s with a thin persistent tail — the MLB line-lag). Re-run as
      the dataset grows.
- [x] **Depth-logging + capital/throughput simulator** — monitor logs per-transition fillable depth
      (`depth:{c2,c1,c0}` = contracts at gross marginal edge ≥2c/1c/0c, both legs); `scripts/capital_sim.py`
      models hold-to-settlement concurrency (Little's Law) → required-capital ↔ daily-return frontier,
      W-sensitivity + intraday arrival profile. Self-tested; validated end-to-end (data still too sparse for
      a read). MLB edges show **thousands of contracts of depth** (e.g. `lad-pit` c2≈4800).
- [x] **Independent review + hardening (2026-06-09)** — fresh-eyes adversarial review
      ([tasks/independent-review-2026-06-09.md](independent-review-2026-06-09.md)) → fixed per-order fee +
      float-ceil (C1), entry guard (C2), crossed-book rejection (C3), seq-resync marker (C6); added
      per-transition staleness (`age`) + clean-fillable filter (L2); **verified settlement identity**
      (`scripts/verify_settlement.py` → same NWS CLI Daily + station + boundary, 5 cities;
      [research/settlement-verification.md](../research/settlement-verification.md)).
- [ ] **All-in edge filter + cost model** ([0010](../decisions/0010-all-in-edge-filtering-and-cost-model.md)) —
      today we filter only the QUOTED taker-fee-net edge (fees + spread in; **slippage / latency / leg-fill
      risk OUT**). Build the all-in filter: size-aware slippage (from `depth`, + net-marginal depth curve),
      a measured **latency** haircut (order-ack study), and a **leg-fill-failure** EV term
      (`P(both)·quoted − P(one)·naked-loss`). [`make_px`/`signal`/`game_edge` per-direction pricing for
      one-sided books — **done 2026-06-09**.]
- [ ] **Open items from the review** — settlement timing **researched + live-object-read** (both ~8 AM ET /
      same morning CLI; residual = downward-correction asymmetry. Kalshi side now **primary-source confirmed**
      via `verify_settlement.py` — rules say *"final value"*, MIA expiry 10 AM EDT; pmus's 8 AM-lock is **still
      third-party** (live object has no timing language) → owner: confirm via QCX support / one observed
      correction day). **Revision-rate logger BUILT + LIVE** (`monitor.py` `cli_stream` → `_data/cli.jsonl`;
      `scripts/cli_revisions.py` reports revised / **downward** / drop-magnitude; day-1 = 0/5 station-days,
      accrues over weeks). Remaining: middle-bucket boundaries; **cost-of-carry** in `capital_sim`; live
      **mid-divergence** guard (L1 in the monitor); WS snapshot-vs-delta confirmation.
- [ ] **Size the bankroll + intraday strategy** — as data accumulates, re-run the harness + simulator to
      set the initial capital (peak concurrent), per-arb clip (depth-capped), and intraday allocation
      (verify/refute the evening-cluster hypothesis). **The scalable lever is breadth of depth-AND-edge
      events** (MLB line-lag — edge *with* depth), each sized to its own book, **not** larger clips in a thin
      corner; capital is sized to peak concurrent *deployable depth*, not opportunity count ([L16](lessons.md)).
      Per the owner this is sizing/tuning, **not** a hard go/no-go gate (confident the arb works).
- [ ] **(then, per user) Live-bot trade-selection** — wire `ledger.py` to live monitor signals: capital
      allocation, per-market layer/rotate, leg-risk fill management. Exits the read-only phase.

## Coverage (all US-legal series)
- [x] **ECON mapped (2026-06-09; pairing corrected 2026-06-10, 0013)** — `colisted_map.ECON` covers
      CPI/U-3/NFP/GDP/Fed via the grid-step twin join (`≥T` ↔ `>T−step`): **14 settlement-identical pairs**
      (+13 no-twin skips; the original 24 equal-number pairs included off-by-one phantoms). Settlement
      identity via `scripts/verify_econ_settlement.py`. Monitor + analyses cover the full US-legal universe.
- [ ] **Politics** (103 pmus markets, US-legal, long-dated) — not yet mapped; needs a per-race rule audit +
      accepts months-long capital lockup. Lower priority. `colisted_map` audit flags it.
- `colisted_map.py`'s audit flags unmapped polymarket.us categories every run. `cod` (KXCODGAME) was
  flagged, verified co-listed, and **mapped 2026-06-10**; `twc` (influencer soccer) has no Kalshi
  co-listing. `scan_all.py` now imports `LEAGUES`/`WX`/`ECON` from `colisted_map` — one config to extend.

## Key finding (2026-06-08)
Edge lives in INEFFICIENT corners, not deep books. Tennis/UFC/ITF (deepest liquidity) = $0 cross-venue
(sharp). Real edge: **MLB** (new-venue line lag) + **weather** (intermittent) — the live monitor has logged
real MLB + weather transitions. Keep ALL in scope; the bot decides when/what to trade.
> **Magnitudes PRELIMINARY** — the old "~$23 MLB / ~$20/day weather" were a single ~8h window under a
> since-fixed fee model + a ~6–7× capital double-count (corrected: peak ≈ $13.6k / ~6%/day). See
> [reviewer-audit C8](reviewer-audit-2026-06-09.md). MLB depth-and-edge is n=1, not yet a class property.

## Status / context
- **Read-only phase** (no orders). Creds verified: polymarket.us (Ed25519) + Kalshi **read-only** key
  (RSA-PSS, `scripts/kalshi_readonly.pem`; read-write key intentionally out of repo, [0007](../decisions/0007-readonly-kalshi-key-least-privilege.md)).
- Monitor build-complete; the next concrete step is the gated day-long data-collection run on a droplet.
- Private GitHub repo `kunpark04/cross-arb` (`origin/main`). Research in `research/`; raw data in
  `scripts/_data/` (gitignored).
