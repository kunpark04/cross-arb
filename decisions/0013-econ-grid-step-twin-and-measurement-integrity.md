# 0013 — Econ pairs join on the grid-step TWIN (≥T ↔ >T−step); measurement-integrity fixes (close stamps, reconnect markers, single-subscription invariant)

- **Date:** 2026-06-10
- **Status:** Accepted (supersedes the pairing rule in [0011](0011-econ-co-listing-same-orientation-only.md))
- **Deciders:** owner + Claude (full-project review)

## Context

A full adversarial review verified both venues' econ rules text live and found the 0011 econ mapping
**off by one bucket**: pmus `≥ T` is **inclusive** ("…is at least 4.6%…") while Kalshi "Above T" is
**strict** (`strike_type: greater`, "…is above 4.6%…" — verified for KXU3/KXCPIYOY/KXGDP/KXPAYROLLS).
On the print grid (BLS/BEA report one decimal for U-3/CPI/GDP; payrolls in 1000s), `≥ T` ≡ `> T−step`,
so the settlement-identical twin of a pmus `≥ T` market has `floor_strike = T − step` — **not** `T`.

0011 had called the exact-on-T print a "narrow residual, akin to the weather downward-correction."
That was wrong: for an at-the-money threshold the exact-T print is the **modal region**, and the
cross-venue gap on a `T==floor` pair is the **market-priced P(print == T)** — a phantom "edge" that
loses both legs when the modal print lands. Proven live (2026-06-10): pmus `≥4.4` mid ≈ 0.275 sat next
to Kalshi `T4.3` (its true twin, mid ≈ 0.33) and ~17¢ away from its then-partner `T4.4` (mid ≈ 0.105).
The monitor had logged that pair as a persistent **12.2–13.3¢ "edge" with 423 contracts of depth** —
celebrated in the session log as the first econ data point; it was the boundary mass. It contributed
~20% of the allocation-test OOS headline (+64%/+269% → **+9%/+141%** without it).

The same review found two measurement-integrity defects and one unverified-protocol dependency:
1. **FlipDebouncer stamped flushed CLOSEs at FLUSH time** (+1.0–1.5 s on *every* episode duration) —
   fatal to the sub-second leg-fill measurement (the #1 execution risk; corrected shadow-fill leg-fail
   @1 s: 39% → **55.5%**, and 27.5% of capturable 1¢+ episodes die ~instantly).
2. **WS reconnects wrote no censoring marker** (only boot + seq-gap did), so reconnect-rebuild phantom
   re-OPENs — the exact [L20] class — were invisible to analysis. The heartbeat was also unsupervised
   (one send-race exception killed discovery/prune/beacon forever).
3. **Kalshi second-`subscribe` semantics were never probed** (only a single subscribe was live-verified),
   yet the heartbeat sent one per new ticker — silent no-data or a seq-counter break, unverifiable.

## Decision

1. **Econ joins on the twin**: `econ_colisted` pairs pmus `≥ T` with Kalshi `floor_strike = T − step`
   (`econ_twin`; step 0.1 for U-3/CPI/GDP, 1000 for NFP; Fed categorical unchanged). No listed twin →
   **not co-listed** (`ge_no_identical_twin` flag; live result: 24 pairs → **14 identical pairs + 13
   honest skips**). Corrected pairs verified live: no phantom edges (all small negatives).
2. **Quarantine pre-remap econ data**: `analyze_persistence.load()` drops threshold-econ records
   (`urc-/cpic-/gdpc-/nfpc-`, keeps `rdc-`) logged before `ECON_REMAP_DEPLOY_TS` (None = fix not yet
   deployed → all records). Their "edges" are boundary mass, not arbs.
3. **CLOSE records carry detection time**: the debouncer flushes a held CLOSE with the timestamp at
   which the edge died. For pre-fix data, `build_episodes` subtracts `CLOSE_FLUSH_LAG = 1.25 s`
   (clamped at the last continuation) from clean CLOSEs until `DEBOUNCE_STAMP_FIXED_TS` is set.
4. **Every state-loss event is marked**: `ws_reconnect` (per venue, both drop and clean-close) joins
   `session_start`/`kalshi_resync` in sessions.jsonl, and all three censor episodes in analysis.
5. **Single-subscription invariant (Kalshi WS)**: one `subscribe` per connection — a seq gap or a
   mid-session ticker add **cycles the connection** (supervised reconnect re-subscribes the full
   current universe) instead of resubscribing in place on unverified semantics. The heartbeat is
   supervised, and a **degraded discovery pass (fetch errors) skips pruning** so an API outage can't
   masquerade as mass settlement.

## Alternatives considered

- **Keep `T==floor` pairs and model P(print==T) explicitly** (estimable from adjacent Kalshi strikes) —
  rejected for the read-only monitor: it turns a "locked arb" log into a probabilistic-EV log and
  violates invariant #1's plain meaning. Revisit if the trade layer ever wants boundary bets *priced as such*.
- **Verify-and-use `update_subscription` for ticker adds** — deferred until probed (todo); cycling is
  strictly safe at ~1 reconnect/5-min-heartbeat worst case, and reconnects are now censored markers anyway.
- **Leave old econ records in the dataset with a flag** — rejected: every consumer would need the flag;
  quarantine at the single shared loader matches [L20]'s chokepoint rule.

## Consequences

- **Coverage**: econ = 14 identical pairs (was 24, of which the 13 twin-less were settlement-divergent);
  `cod` (KXCODGAME, 3 live pmus markets) added to LEAGUES. Counts in CLAUDE.md/README corrected.
- **Headline corrections**: allocation OOS = **+9% (5% cap) to +141% (20% cap) vs FIFO, 10 diversified
  pairs, econ-free**; shadow-fill leg-fail **55.5% @1 s / 62.7% @2 s** (lag-corrected, FLIP=fail);
  episode durations shrink (median 2 s). All still PRELIMINARY (0.86 d).
- **The multi-week accumulation clock restarts again** at the next gated redeploy (0006). *(Done
  2026-06-10 09:03 UTC, owner-greenlit: build `bfa9e2fccf15` deployed sha-verified; both epochs set to
  `1781082189` in `analyze_persistence.py`; smoke test + droplet records verified on the new schema.)*
- **Open**: probe Kalshi multi-subscription semantics (would remove the cycle-on-add); re-pin both
  venues' fee schedules from primary sources (web fetch blocked this session); `session_start` now logs
  a `build` hash + argv so deploys and crashes are distinguishable in sessions.jsonl.
- Lessons [L21] (threshold number ≠ inequality semantics) and [L22] (a smoothing layer in front of a
  logger biases the measurement) recorded.
