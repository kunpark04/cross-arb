# Adversarial review — RISK-GATE + DOMAIN + CONFIG (`risk.rs`, `types.rs`, `config.rs`)

**Reviewer:** independent, read-only (no edits). **Date:** 2026-06-11.
**Scope:** the last-line pre-trade gates of the LIVE bot (decision 0015, safe-by-default, real money) +
their integration in `main.rs::run_live`. Verified against `exec.rs`, `signal.rs`, `main.rs`, the README
gate list, and the prior reviews (`20260611-rust-review/*`, `20260611-readiness/auditG`,
`20260611-engine-review/A-pricing-math`). Builds clean; clippy in-scope = 1 cosmetic (risk.rs:88).

## Summary — counts by severity

- **CRITICAL: 2** — both are *latent-but-real* gate-coverage holes on the **sports** path (live the day
  `ASSUME_SPORTS_SETTLED=true`). Neither is a today-on-weather loss; both are a direct loss when sports is
  enabled, and the safe defaults do **not** save them.
- **WARN: 4** — robustness / integration-mismatch / float edge cases.
- **INFO: 2** — clippy + a clarity nit.

Not re-flagged (prior reviews accepted these; I confirmed each still stands and is *correctly* deferred or
guarded): one-sided `mid()` half-spread skew at the 40¢ threshold (latency-review §4), `f64→u32`
saturation under guarded `.max(0.0)` (latency-review §4), `cost_per=(1-edge.net).max(0.1)` reconstruction
vs real `ask_y+ask_n` (latency-review §4 / GAP), fat-edge toxicity now *encoded* (was rust-review GAP-1),
econ-twin tight divergence bound now *present* (was rust-review GAP-3 / auditG §5). The `< 1.0` guard on
the fat-edge haircut correctly prevents `fat_edge_size_factor>1.0` from ever sizing **up** — checked, not a
bug.

### Top findings (one line each)
1. **[CRITICAL] risk.rs:121** — the mid-divergence bad-join/stale guard reads only `q.k`+`q.pm`; the SPORTS away-team book `q.k_b` is **never** divergence-checked, and `game_signal`'s C3 guard only covers team-A — a stale/flipped team-B Kalshi book sails through into a 2-leg fire.
2. **[CRITICAL] config.rs:89 / risk.rs:78** — `REQUIRE_SETTLE_CLEAN=false` (one bool, default-safe) disables the **entire** settlement-identity gate for **all** categories at once; nothing in `main.rs` re-asserts it for live+prod, so the single catastrophic-axis gate is a single unguarded point of failure.
3. **[WARN] main.rs:631 / risk.rs:168** — `affordable()` sizes off **raw** `max_total_notional`, ignoring already-open `exposure.total`; only the in-gate `tot_cap` actually subtracts exposure, so `affordable` is wrong-by-design once any position is open (saved by belt-and-suspenders, but it's a real accounting drift in a safety control).
4. **[WARN] risk.rs:88** — the event-proximity gate's `map_or(false, |d| d > max)` lets a **NaN** `days_to_event` through (`NaN > x` is false) — a corrupt/parsed-bad event date silently defeats the capital-velocity gate instead of failing safe.
5. **[WARN] risk.rs:158 / main.rs:735** — sizing approves on `edge.net`, but `build_legs` rounds each leg price to a tick **independently** with no post-rounding `sum<$1` re-check; up to +1¢/pair of edge can be eroded below the intended floor (bounded, not sign-flipping at τ=2¢, but it means the *booked* pair can be thinner than the gated edge).
6. **[WARN] types.rs:73 / risk.rs:103** — `Book::crossed()` and the `k_b` stale/crossed gates compare the away book, but `Book::mid()` on a one-sided `k_b`/`pm` returns the single side; combined with #1 the sports mid-sanity is doubly weak (no k_b coverage AND skew-prone where it does run).
7. **[INFO] risk.rs:88** — clippy: `map_or(false, …)` → `is_some_and(…)` (also fixes #4 if rewritten to treat NaN as out-of-range).
8. **[INFO] config.rs:139] — numeric env parse silently swallows a malformed value to the default; safe in the cap direction but a typo'd *larger* intended cap silently shrinks with no log.

---

### [CRITICAL] risk.rs:121-131 — mid-divergence gate never checks the SPORTS away-team book `k_b`

**Problem:** the L1 bad-join/stale "two settlement-identical legs must price close" guard compares **only**
the pmus book against the team-A Kalshi book:
```rust
if let (Some(km), Some(pm)) = (q.k.mid(), q.pm.mid()) {     // q.k = team-A Kalshi; q.pm = pmus
    let dd_cents = (km - pm).abs() * 100.0;
    ...
    if dd_cents > ceiling { return Err(Reject::MidDivergence(dd_cents)); }
}
```
`q.k_b` (the **away-team** Kalshi book — the SECOND book a sports hedge actually fills against, `types.rs:103`)
is **absent from this gate entirely.** For a sports PK fill the legs are `YES@pmus(team A)` +
`YES@Kalshi-B(team B)` (`main.rs:692-699`), so the **team-B book is a load-bearing leg** whose price the
gate never sanity-checks. The only team-B protections are: the crossed/stale gates (`risk.rs:103-114` —
catch bid>ask and age, but **not** a wrong/mislabeled level), and `game_signal`'s C3 guard
(`signal.rs:127-131`) which checks `|guard_pm − ka_ask| > 0.40` — i.e. **team-A (pmus-YES vs Kalshi-A)
only**. Nothing checks that the team-B Kalshi book is consistent with the implied "team B wins" probability
(`1 − pm_yes`). A stale or flipped team-B book (e.g. a doubleheader/duplicate-ticker misbind that survives
discovery, or a one-sided team-B that `mid()` collapses) produces a *fat apparent edge* on the team-B leg
that this gate is blind to — exactly the L1 "bad join / stale quote = both-legs loss" failure the gate
exists to stop, on the one category (sports) where the join is 2-outcome and most error-prone.

Why CRITICAL not WARN: this is the **catastrophic both-legs-loss axis** (invariant #1/#2), it is on the
*live order path* for sports, and it is reachable the moment `ASSUME_SPORTS_SETTLED=true` (an owner flag the
smoke + tests already exercise). The fat-edge subset is also where adverse selection concentrates (auditG:
≥8¢ = 66% toxic) — a team-B-only blindspot is precisely where a phantom fat edge hides.

**Fix:** extend the gate to validate the team-B leg for sports. Minimal: when `q.k_b.is_some()`, also assert
the team-B implied price is consistent with pmus — e.g. `|k_b.mid() − (1 − pm.mid())| ≤ ceiling` (the two
single-team Kalshi YES asks + the pmus YES should be mutually coherent: `ka + kb ≈ 1`, and `pm_yes ≈ ka`).
Or apply a C3-style guard to team-B inside `game_signal` (`|kb_ask − (1 − guard_pm)| > 0.40 → reject`). Add a
sports vector to the risk tests where a divergent `k_b` mid is rejected (today `rejects_crossed_or_stale_away_team_book`
only covers crossed/stale, never *divergent-but-uncrossed*).

---

### [CRITICAL] config.rs:89 + risk.rs:74-80 — `REQUIRE_SETTLE_CLEAN=false` disables the settlement gate for ALL categories at once, with no live+prod re-assertion

**Problem:** the settlement-identity gate — the project's named "catastrophic both-legs-loss axis" — is
guarded behind a **single bool**:
```rust
// config.rs:89
require_settle_clean: env_bool("REQUIRE_SETTLE_CLEAN", true),
// risk.rs:78
if cfg.require_settle_clean && !settle_ok { return Err(Reject::SettlementUnverified); }
```
The default is safe (`true`). But `REQUIRE_SETTLE_CLEAN=false` (or `=0`) turns the **entire** gate off for
**every** category simultaneously — weather, sports, *and* econ — collapsing the per-category override
design (`assume_sports_settled` / `assume_econ_settled`, which exist precisely so each category can be
enabled independently after its own recon: sports ~06-23, econ 07-02). With the flag off, an
**un-reconciled econ twin** (the U-3 phantom class, settles 07-02) and an **un-reconciled sports pair** (the
postpone/void both-legs-loss tail) both become tradeable in one keystroke. Unlike `EXECUTION_MODE=live` +
`VENUE_ENV=prod` — which `main.rs:36` hard-gates behind `CROSSARB_I_UNDERSTAND_PROD` — **there is no
consent/assertion that `require_settle_clean` is still true when running live+prod.** A single env var in a
`.env` / systemd unit silently removes the last structural guard against the exact loss the whole project is
organized around, and nothing logs it as anomalous (the banner prints `settle-clean={}` but does not refuse).

Why CRITICAL: it is the single most consequential safety toggle, it is bypassable by one typo'd/leftover env
var, the failure mode is the catastrophic axis, and (unlike the prod gate) it has **no informed-consent
backstop**. Defaults being safe is necessary but not sufficient for a real-money gate that can be globally
disabled.

**Fix:** (a) in `main.rs`, refuse to start (or force-`true`) when `is_live() && is_prod() &&
!require_settle_clean` unless an explicit second consent env is set — mirror the existing prod gate. (b)
Make the banner shout (`SETTLE-CLEAN GATE: DISABLED`) in red when off + live. (c) Strongly consider removing
the global kill entirely and relying only on the per-category `assume_*` flags (which already give
per-category control with safe `false` defaults) — a global off-switch for the catastrophic gate has no safe
use case the per-category flags don't cover.

---

### [WARN] main.rs:629-634 + risk.rs:159-168 — `affordable()` ignores open exposure; only `tot_cap` subtracts it

**Problem:** the `affordable` argument fed to `evaluate` is computed from the **full** total-notional cap,
with no subtraction of capital already deployed:
```rust
// main.rs:631
fn affordable(cfg: &Config, edge: &Edge) -> u32 {
    let cost_per = (1.0 - edge.net).max(0.1);
    (cfg.max_total_notional / cost_per).floor().max(0.0) as u32   // <-- raw cap, ignores exposure.total
}
```
Its docstring says "contracts the **bankroll** can fund," but once positions are open the real fundable size
is `(max_total_notional − exposure.total)/cost_per`. The gate's own `tot_cap` (`risk.rs:168-172`) *does*
subtract `exp.total`, so `size.min(tot_cap)` saves correctness — `affordable` is redundant-when-right and
wrong-when-it-matters. That's fragile: `affordable` is presented as a real bankroll control but is only
inert because a *second* control happens to overlap it. If a refactor ever trusts `affordable` as the
bankroll truth (e.g. for a per-category budget, which the rust-review GAP-2 explicitly wants to add), the
double-count returns. It also double-derives `cost_per` from `edge.net` in two places (main.rs:632 and
risk.rs:158) — same magic-0.1 reconstruction the latency-review flagged, now in two spots that can drift.

**Fix:** pass `exp` (or the remaining total room) into `affordable`, or just compute it inside `evaluate`
from `tot_room` and delete the separate arg. At minimum, change the call to
`(cfg.max_total_notional - exposure.total).max(0.0) / cost_per` so the name matches the math.

---

### [WARN] risk.rs:87-91 — event-proximity gate passes a NaN `days_to_event` (fails open, not safe)

**Problem:**
```rust
if cfg.max_days_to_event > 0.0 && q.days_to_event.map_or(false, |d| d > cfg.max_days_to_event) {
    return Err(Reject::TooEarly);
}
```
`days_to_event` is `Option<f64>` computed in stage-2 from a date parsed out of the market key. If that
computation ever yields **NaN** (a malformed/again-parsed date, a subtraction of two bad timestamps),
`Some(NaN)` flows in, and `NaN > max_days_to_event` is **false** in IEEE-754 → `map_or` returns false → the
gate does **not** fire. A capital-velocity gate whose job is "skip arbs far before settlement" thus *passes*
an arb whose lock-time is uncomputable — failing **open** on exactly the input it can't trust. The
category-agnostic design leans entirely on this number being meaningful; a NaN defeats it silently (no log,
no reject).

**Fix:** treat a non-finite `days_to_event` as "unknown but suspicious" — either reject (`TooEarly`/a new
`UnknownLockTime`) or require finiteness:
```rust
if cfg.max_days_to_event > 0.0 {
    if let Some(d) = q.days_to_event {
        if !d.is_finite() || d > cfg.max_days_to_event { return Err(Reject::TooEarly); }
    }
}
```
(`None` stays dormant by design; only a *present-but-NaN* value is the hole.)

---

### [WARN] risk.rs:158 + main.rs:735-765 — no post-tick-rounding edge re-check; rounding can erode the gated edge

**Problem:** `evaluate` approves on `edge.net` (the marginal-fee net from `signal`), but the *actual* limit
prices are computed later and **rounded to whole cents per leg, independently**, in `build_legs`→`cents`
(`main.rs:754-765`, `(p*100).round()`). Each leg can round **up** by as much as 0.5¢, so the pair's paid
cost (`sum of leg prices`) can be up to **+1¢** higher than the cost implied by `edge.net`. There is **no
re-validation that the rounded pair is still ≥ floor (or even +edge) before `submit_pair`.** At τ=2¢ this
can't flip the sign (worst case a 2¢-gated edge books as ~1¢), so it's not a both-legs-loss — but the *booked*
edge can be materially thinner than the gated edge, which matters because the whole readiness thesis is that
the median arb is already friction-marginal (auditE: median 0.39¢ friction-negative). Approving a 2¢ edge
and booking 1.0–1.5¢ silently eats half the margin the floor was protecting.

**Fix:** after `build_legs`, recompute the realized pair cost from the **rounded** `price_cents`
(`a_cents + b_cents` for the same-direction pair) and re-assert it clears the floor (and is < 100¢) before
submitting; skip the fire if rounding pushed it under. This is the honest "size and price are consistent"
check the gate currently splits across two functions that never reconcile.

---

### [WARN] types.rs:73-81 + risk.rs:121 — `Book::mid()` one-sided collapse compounds the sports blindspot

**Problem (a refinement of #1, not a duplicate of the accepted latency-review note):** `Book::mid()` returns
the single present side for a one-sided book:
```rust
(Some(b), None) => Some(b),
(None, Some(a)) => Some(a),
```
The latency-review flagged this for the *weather/econ* k-vs-pm comparison at the loose 40¢ threshold
(accepted). The unaddressed part: for **sports**, the mid-divergence gate already omits `k_b` (#1), AND
where it *does* run (k vs pm), a one-sided pmus or Kalshi-A book makes `(km − pm)` a bid-vs-midpoint
half-spread skew — so the sports mid-sanity is weak on *both* axes simultaneously. A one-sided team-A book
that collapses to its bid can duck the 40¢ guard while the real (ask-based) divergence is larger, on the
category where a mislabel is most likely.

**Fix:** folded into #1 — when adding the team-B check, compare like-for-like (ask vs ask, or skip the
divergence gate and rely on a tightened C3 when either side is one-sided), and document that `mid()` on a
one-sided book is a *fallback*, not a true mid, so no future consumer treats it as one.

---

### [INFO] risk.rs:88 — clippy `map_or(false, …)` → `is_some_and(…)`

**Problem:** `cargo clippy` warns (in-scope): `risk.rs:88` `this map_or can be simplified` →
`q.days_to_event.is_some_and(|d| d > cfg.max_days_to_event)`. Cosmetic, but rewriting it is the natural place
to also fix the NaN hole (#4) — `is_some_and(|d| d.is_finite() && d > max)` reads cleanly.

**Fix:** apply the lint + the finiteness guard together.

---

### [INFO] config.rs:139-154 — malformed numeric env silently defaults (safe direction, but unlogged)

**Problem:** `env_f64`/`env_u32`/`env_u64` swallow any unparseable value to the default
(`env::var(k).ok().and_then(|v| v.parse().ok()).unwrap_or(d)`). A typo like `MAX_TOTAL_NOTIONAL=1OOO`
(letter O) or `EDGE_FLOOR_CENTS=2.0c` parses to `None` → falls back to the **default** (20.0 / 2.0). For
caps this fails *safe* (the default is the tiny staged-rollout floor, smaller than any intended raise), and
`env_bool` correctly only flips on exact `1/0/true/false/TRUE/FALSE` (so `REQUIRE_SETTLE_CLEAN=flase` →
keeps the safe default `true`, **not** false — good, the prompt's worry doesn't bite here). The gap is purely
*observability*: an operator who sets a deliberately larger cap and typos the value gets the tiny default
with **no warning**, and may believe a larger cap is live. Not a safety hole; a silent-misconfig hole.

**Fix:** log a `WARN` when an env var is *present but unparseable* (distinguish "unset → default" from
"set-but-bad → default"), so a typo'd cap is visible at startup rather than discovered by under-filling.

---

## clippy result
`cargo clippy --manifest-path bot-rs/Cargo.toml` → **5 warnings repo-wide**; **in the three target files: 1**
— `risk.rs:88` `map_or`→`is_some_and` (cosmetic; see #7). `config.rs` and `types.rs`: **0 warnings**. The
other 4 are out-of-scope/cosmetic (2 doc-indent in `main.rs`, `matcher.rs:125` too_many_arguments,
`unwind.rs:29` map_or). No errors; build is clean.
