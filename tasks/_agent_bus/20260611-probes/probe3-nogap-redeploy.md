# Probe 3 — no-gap build redeploy readiness + censoring cost of NOT redeploying

2026-06-10 (analysis run ~20:00 local, on the data pulled this hour). READ-ONLY: no deploy, no restart,
no droplet writes performed. Deploy remains GATED by [0006](../../../decisions/0006-deploy-on-digitalocean-consult-first.md).

## Verdict

**READY TO SHIP — yes.** Offline gate 18/18 green incl. the no-gap integration test; the behavioral
delta is confined to `bot/monitor.py`; droplet build verified = HEAD (so the local diff is exactly what
ships). **Cost of NOT redeploying: ~310 censored episodes/day on the old build, of which the no-gap
build eliminates ~152/day net (~49% of all censoring; ~76% of censor-event victims are cycle-on-add).**

## 1. Test evidence (run now, before concurrent edits landed)

- `python scripts/selftest_all.py` → **18/18 PASS, ALL GREEN** (bot cores: ledger, kalshi_book, monitor,
  colisted_map; + 14 script selftests incl. `test_monitor_nogap.py`, `analyze_persistence.py`,
  `shadow_fill.py`, `adverse_selection.py`, `settle_recon.py` — all green at run time).
- `python scripts/test_monitor_nogap.py` standalone → **ALL GREEN**:
  - Scenario A (happy): 1 connection, `update_subscription add_markets` on the live sid (no reconnect),
    snapshot confirms the pending add, prune sends `delete_markets`; 0 resyncs, 0 k reconnects.
  - Scenario B (fallback): add acked but never snapshotted → next heartbeat cycles the connection,
    `ws_reconnect` marker logged, reconnect subscribes the FULL ticker set.

## 2. What ships (the deploy cone is the 4 bot modules + requirements.txt)

| File | Local sha256 | vs deployed (HEAD) |
|---|---|---|
| `bot/monitor.py` | `20c3efa4fafdd1d83e4d2cb2a07a15ae0133f6db9ec95cd13393735b32de1c5b` (build id `20c3efa4fafd`) | **+80/−18 — the no-gap change (the only behavioral delta)** |
| `bot/ledger.py` | `7bf950ef4f8a6371a2385c22d14bd4475676b207ddd5ee2453b5ee0e9a1b8103` | +12/−7 **comment-only** (fee-pin 2026-06-10 doc block; zero code change) |
| `bot/kalshi_book.py` | `476618a1bc94adba5bdf262d932c17e88b20f647e0c02b3a900f793263d9349a` | identical to HEAD |
| `bot/colisted_map.py` | `4bc9c241b83046e147339a0c3d22ed9fb6837712e4064795923b608efcffbf65` | identical to HEAD |

`requirements.txt`, unit file, deploy scripts: unchanged. Local repo state: these are **uncommitted
working-tree changes** (no git commit/push performed per phase rules) — commit before/with the deploy
so the shipped bytes are reproducible.

## 3. Behavior change (old cycle-on-add → no-gap)

- **Old (deployed, build `bfa9e2fccf15`):** any discovery ADD of Kalshi tickers → `conns["k"].close()`
  → clean-close reconnect → `books.clear()` → full ~650-ticker re-snapshot → every open episode
  force-closed (`ws_reconnect` censor marker, venue=k reason=clean).
- **New (local, build `20c3efa4fafd`):** in-place `update_subscription add_markets` on the live sid
  (probe-verified wire shape, selftested); only added tickers snapshot; existing books stream
  uninterrupted. Snapshot-confirm: an added ticker that never snapshots within `ADD_CONFIRM_SECS=60`
  trips a **fallback cycle** next heartbeat (marker logged → still censorable). Prune now also sends
  `delete_markets` (subscription hygiene on the long-lived connection).
- **Unchanged / NOT fixed:** a real Kalshi **seq gap still cycles** (no replay exists); **pm
  reconnects** unchanged; **process restarts/deploys** still censor; fallback cycles still censor.

## 4. Droplet state (read-only check, 2026-06-10 — no restart, no writes)

`ssh cross-arb-droplet "sha256sum /opt/cross-arb/bot/monitor.py && systemctl is-active cross-arb-monitor"` →

- `/opt/cross-arb/bot/monitor.py` = `bfa9e2fccf15971a74ddddb3c8b76a4f4b28f241a9eada5f3eb04491e97d0076`
  = **exactly `git show HEAD:bot/monitor.py`**'s sha256 = the `build: "bfa9e2fccf15"` in the epoch
  `session_start` record. Triple-confirmed: droplet runs HEAD's 0013 cycle-on-add build.
- Service: `active`.

## 5. Censoring quantification — post-0013 epoch (t ≥ 1781082189, all old-build data)

Window: 15.94 h (0.664 d), n=31,297 transitions (shared loader `analyze_persistence.load()`,
36 pre-remap econ records quarantined by it). All numbers from code run now.

**Reconnect markers (sessions.jsonl, post-epoch):**

| marker | n | /day |
|---|---|---|
| `ws_reconnect` k/clean | **32** | 48.2 |
| `ws_reconnect` pm (12 clean + 4 drop) | 16 | 24.1 |
| `kalshi_resync` (seq gap) | **0** | 0 |
| `session_start` | 1 (the epoch deploy boot) | — |

**Cycle-on-add identification:** seq-gap cycles log `kalshi_resync` first — there are **zero**, so no
k-clean reconnect is gap-driven. The cycle-on-add tell is heartbeat quantization: all **31/31**
inter-reconnect gaps sit within ±20 s of an integer multiple of a best-fit **u=309.1 s** unit
(= the 300 s refresh + ~9 s discovery work; argv `--forever 300`); gap/u ratios are integers
(1,2,3,4,5,6,7,8,9,10,20,26). 21/32 are also followed by a first-ever market key within 300 s (the
add the cycle existed to subscribe; the rest added tickers that logged no transition yet — expected).
**Est. cycle-on-add share: 32/32 (100%)** — a server-initiated close landing on the discovery grid
31/31 times is implausible. *Recount of the prior "4 cycle-on-add in 1 h" claim: 3 in the first hour,
4 in the first 2 h; full-window rate = 32/15.94 h = **48.2/day ≈ 2.0/h** (the 1-h figure was the busy
morning start, ~1.5–2× the steady-state rate).*

**Episode censoring (shared `build_episodes()`, post-epoch records + markers):**

| | episodes | clean | eod | restart-censored | censored/day |
|---|---|---|---|---|---|
| Baseline (old build) | 4,964 | 4,749 | 9 | **206 (4.1%)** | **310/day** |
| Counterfactual (k-clean markers removed = no-gap) | 4,963 | 4,847 | 11 | **105** | **158/day** |

- Attribution of the 206 (censored episode's close_t == marker t): **k/clean 157 (76%)**, pm/clean 28,
  pm/drop 21. "Mattering" censored (≥1¢ open net OR c2≥100): **87 (131/day)**, of which **70 were
  k-cycle-censored**; counterfactual leaves 39 (−55%).
- **Net elimination: 101 censored episodes (152/day, 49% of all censoring)** — less than the 157
  attributed because some episodes, uncut at the k-cycle, get re-censored later by a pm event.
  Measured (clean+eod) episodes gained: **+100** (+2.1%). Capturable-grade measured episodes
  (≥1¢ net, c2≥1): **575 → 592 (+17, +3.0%)** over the window.
- Whole-dataset comparator: 5,644 episodes, 265 restart-censored (**4.7%**) — supersedes the prior
  "193 of 3,446 (~6%)" (dataset has grown; post-epoch share is 4.1% but the absolute cost is the
  ~310/day above, ~3/4 of it eliminable).
- **Caveat:** the counterfactual is an approximation — the records were produced by the old build, so
  post-cycle rebuild re-OPENs exist in the stream; removing the censor markers coalesces them as
  dup-OPEN continuations (≈ what the no-gap build would log), but edge values inside the ~rebuild
  window can still carry book-init noise. Direction and order of magnitude are solid; exact counts ±.

## 6. Rollback plan

The deployed build is byte-identical to HEAD: `git show HEAD:bot/monitor.py` (sha256
`bfa9e2fccf15…d0076`). Rollback = re-scp that one file to `/opt/cross-arb/bot/monitor.py` +
`systemctl restart cross-arb-monitor` (one more restart marker). No schema change: the new build
writes the same record shapes (`session_start` now distinguishable by `build: "20c3efa4fafd"`), so
analyses are unaffected either way.

## 7. Owner ask (0006 gate)

Approve **one `systemctl restart cross-arb-monitor`'s worth of downtime** (seconds; every JSONL line is
durable, the restart logs its own `session_start` censor marker, heartbeat re-discovers). Benefit:
stops ~152 episodes/day of avoidable censoring (~49% of all censoring; ~70 "mattering" episodes/day
drop to ~39) and removes the ~650-book rebuild every ~2–6 heartbeats. **Bundling:** if the wave-2
ladder-logging + fee-tripwire code lands today, it should ride the **same** redeploy — one restart,
one censor marker, instead of two. Post-deploy verification (per L8/L9): confirm the new
`session_start` shows `build: "20c3efa4fafd"` (old: `"bfa9e2fccf15"`), then watch one discovery add produce
`update_subscription add_markets` in journalctl with **no** k `ws_reconnect` marker, and confirm
`kalshi_resync`/fallback-cycle counts stay ~0 (zero seq gaps observed in the 15.94 h window).

## 8. What ships in the next gated redeploy — wave-2 bundle (appended 2026-06-11)

The wave-2 measurement code is now implemented + offline-tested locally and rides the SAME single
restart as §3's no-gap build (deploy still GATED by 0006; nothing deployed, nothing committed).
**Supersedes §2's build id:** `bot/monitor.py` is now `f8f261298097` (was `20c3efa4fafd`; the no-gap
change is contained in it unmodified — wave-2 is purely additive on top).

- **`bot/monitor.py`** (per [ladder-logging-spec.md](ladder-logging-spec.md), weather-only scope):
  - `trades-<event-date>.jsonl` — Kalshi trade prints via the public REST cursor-poll (`/markets/trades`
    envelope `{cursor, trades}` + `min_ts` filter re-verified live 2026-06-11; the WS `trade` channel
    deliberately NOT wired, per spec). `vt` = venue fill time verbatim + `t` = local receipt (L22);
    `trade_id` dedupe with a 1 s min_ts overlap; a restart seeds from the log tail (no re-logging).
  - `ladders-<event-date>.jsonl` — top-5 dual-venue snapshots: `k:"tr"` on every weather transition,
    captured at DETECTION time with the transition's `t` (the join key); `k:"hb"` every 300 s per
    market, delta-suppressed when all four ladders are unchanged.
  - `/series/fee_changes` tripwire — envelope key pinned live (`series_fee_change_arr`); non-empty or
    changed → loud `fee_changes.jsonl` record + a `fee_changes` count in the health beacon.
  - All additive: transitions/sessions shapes untouched; `analyze_persistence.py` + every loader
    unmodified. Budget ≤ +9.5 MB/day raw (spec table; ~+1.1 MB/day gzipped). First REST poll at
    t+300 s (`WX_POLL_SEC`, decoupled from the discovery refresh); trade backfill = one cycle lookback.
- **`deploy/pull-data.ps1`** — finalize/gzip/verify-then-delete now covers
  `(transitions|ladders|trades)-<date>.jsonl` (spec compat item 2: otherwise the new files accumulate
  on the droplet unbounded). Undated live files (sessions/cli/fee_changes) still never deleted.
- **`deploy/healthcheck.ps1`** — every 30-min run also polls `/series/fee_changes` and raises the
  existing alert path (balloon + ALERT.txt + exit 1, titled "FEE CHANGE scheduled") on non-empty.
- **Offline gate: 19/19 ALL GREEN** (`scripts/selftest_all.py`; was 18/18 — the new
  `scripts/test_monitor_trades_ladders.py` covers trade-poll dedupe/restart-seed, detection-time
  `k:"tr"` ladders, hb delta-suppression, the fee tripwire + beacon field, and asserts the transitions
  record shape is untouched; `test_monitor_nogap.py` unchanged and green).
- **Post-deploy verification additions** (L8/L9, on top of §7's): `session_start` must show
  `build: "f8f261298097"`; within ~10 min confirm `trades-*.jsonl` and `ladders-*.jsonl` exist and grow
  on the droplet and the beacon carries `fee_changes: 0`; the next morning's pull must show the new
  dated files gzipped + removed remotely exactly like transitions.
