# Independent parity review — `matcher.rs` / `discovery.rs` vs `colisted_map.py`

**Reviewer:** independent adversarial code reviewer (no authorship stake in the Rust).
**Scope:** identity-critical JOIN + catalog PARSE only. `matcher.rs` does **not** reference `ledger.py`
(grep: no `ledger`/`signal`/`fee` hits) — no fee/signal coupling to review.
**Date:** 2026-06-11. Tests: Rust 15/15 green (8 matcher + 7 discovery); Python `colisted_map.py`
self-test green. Differential checks run against live Python execution (vectors below).

## VERDICT: FAITHFUL (one WARN, coverage-only, cannot reach the order path)

The econ L21 off-by-one **cannot recur** through this code. Every identity-critical rule (econ twin
direction + grid_step, weather bounds-NUMBER equality, threshold/period extraction incl. the GDP fix) is
a faithful port. The single divergence is sports date-binding looseness, and sports is **never** emitted
as a tradeable pair — it is a coverage counter only.

---

## Claim 1 — ECON grid-step twin (floor = T − step), inequality direction, Fed unchanged → CONFIRMED

- **grid_step per series identical.** `discovery.rs:78-84` `ECON` = `cpic/.1, urc/.1, nfpc/1000, gdpc/.1,
  rdc/None` ≡ `colisted_map.py:174-175` `{"cpic":(...,0.1),"urc":(...,0.1),"nfpc":(...,1000),"gdpc":(...,0.1),"rdc":(...,None)}`.
- **Twin = T − step (not T).** `matcher.rs:108-110` `econ_twin(thr,step){ round6(thr-step) }` ≡
  `colisted_map.py:220-223` `return round(thr - step, 6)`. Direction: `matcher.rs:137-138`
  `Ineq::Ge => { let twin = econ_twin(thr?, step); ... floor == twin }` binds pmus `≥T` to Kalshi floor
  `T−step` ≡ `colisted_map.py:250-251` `elif p["ineq"] == ">=": twin = econ_twin(...); tk = kby[period].get(twin)`.
- **`≤` / `==` skipped, Fed categorical unchanged.** `matcher.rs:169` `Ineq::Le | Ineq::Eq => None` ≡
  `colisted_map.py:256-257` (`le_skip_OPPOSITE_orientation` / `point_bucket_skip`). Fed: `matcher.rs:153-167`
  `Ineq::Cat` label==label via `fed_canonical_label` ≡ `colisted_map.py:245-249` `_FEDLBL` lookup; the two
  5-entry label maps are character-identical (`matcher.rs:174-183` vs `colisted_map.py:177-178`).
- **Threshold rounding numerically equivalent.** Rust compares raw Kalshi floor to the 6dp-rounded twin
  (`matcher.rs:140` `(floor - twin).abs() < 1e-9`); Python keys the dict on the 6dp-rounded floor
  (`colisted_map.py:240`). Differential over float-nasty cases (incl. `4.4-0.1 = 4.300000000000001`,
  `0.3-0.1`): **both True on every case** — the `round6` on the twin side neutralizes the subtraction error.
- The pre-0013 phantom pair `≥4.4 ↔ floor 4.4` is impossible: `econ_ge_pairs_the_identical_twin_only`
  + `assemble_joins_...` both assert binding to `KXU3-26JUN-T4.3` and refusing `T4.4`.

## Claim 2 — Kalshi period/threshold extraction (the GDP `26JUL` vs `26JUL30` self-catch) → CONFIRMED

- `k_econ_period` (`discovery.rs:172-196`) ports `-(\d{2}[A-Z]{3}\d{0,2})-` faithfully: scans `-YY MMM`,
  consumes **0–2** trailing day digits, then **requires** a trailing `-`. Differential vs the live Python
  regex, all identical: `KXU3-26JUN-T4.3→26JUN`, `KXGDP-26JUL30-T1.9→26JUL30` (keeps the day → GDP joins),
  `KXGDP-26JUL3-T1.9→26JUL3`, `KXU3-26JUN→NONE` (no trailing dash), `KXU3-26JUN-T44→26JUN` (the `44`
  strike is after the dash, never grabbed), and **`KXGDP-26JUL301-T1.9→NONE` on BOTH** (Rust greedy-cap-2
  then `≠'-'` ⇒ fail-this-position == Python `\d{0,2}` backtrack-fail). No other series truncates: U-3/CPI/
  NFP/Fed have no trailing day so the `\d{0,2}` consumes 0 and the dash follows immediately.
- pmus-side `econ_parse` (`discovery.rs:229-271`) differential vs Python on 14 vectors incl. adversarial
  (`gte3pt8pct`, `atm2pt5`, `atm250k`, Dec→prior-year `26DEC`, `cpic-december2026yoy→26DEC`): **all match**
  family/ineq/thr/period/label. `atl/atm → Ge` and the `[0-9pt]` (ineq) vs `[0-9ptk]` (atl/atm) charsets
  match Python exactly (`discovery.rs:279,288`).

## Claim 3 — Weather bucket boundary-NUMBER equality (refuse mismatches, no positional zip) → CONFIRMED

- `match_weather` (`matcher.rs:76-99`) is a **bounds-equality** join: `if k_bounds(*floor,*cap) == b` over
  the date's buckets, with the `b == (None,None)` no-pattern guard ≡ `pair_weather_date`
  (`colisted_map.py:114-130`, dict keyed on `kbounds`, `if k is None or b == (None,None): flag`). NOT a
  positional zip. `pm_bounds`/`k_bounds` canonicalization byte-identical to Python (verified live:
  `gte64lt65f→(64,65)`, `-lt64f→(None,63)`, `gte72f→(72,None)`; k-tail offsets `cap-1`/`floor+1`).
  Precedence `gteXltY` > `-ltYf` > `gteX` matches Python (`matcher.rs:49-57` vs `colisted_map.py:98-103`).
- No-twin buckets are **flagged, not paired**: `discovery.rs:399` `None => weather_buckets_misaligned += 1`
  ≡ Python `bucket_misaligned`. Offset-listing test confirms the dict-join finds the true twin (not index).

## Claim 4 — Sports date-binding (exact ET date, no wrong-game join) → WARN (narrower than Python, **coverage-only**)

- **Divergence:** `assemble` sports (`discovery.rs:472-484`) binds via
  `by_event.values().any(|ev| match_sports_abbrev(a,b,league,date,ev,None).is_some())` — it iterates **all**
  events in the league with **no exact-date filter** (`match_sports_abbrev` uses `date` only for the
  cluster string, `matcher.rs:200-214`) and **no doubleheader `used` set**. Python `pick_game`
  (`colisted_map.py:139-158`) pins `date in kbydate` (exact ET date), uses the ±1 fallback only when
  undated AND globally-unique, and threads a `used` set to bind one Kalshi event at most once.
- **Why WARN not CRITICAL:** sports is **matched-and-COUNTED only** — `d.sports_pairs += 1`
  (`discovery.rs:482`), never pushed to `d.pairs`. Only `Cat::Weather`/`Cat::Econ` enter `d.pairs`
  (`discovery.rs:389,435`); `LivePair`s are built solely from `d.pairs` (`main.rs:159,177,407-411`); the
  assemble test asserts `d.pairs.iter().all(|p| p.cat != Cat::Sports)`. So a wrong-date sports bind can
  only mis-COUNT a coverage telemetry int — it **cannot** wire a wrong-game pair to the order path.
- **Latent risk if sports ever goes 1:1:** if a future change emits sports as a `Pair`, this loose binding
  becomes an L1/C2 wrong-game hazard (a same-matchup series on consecutive days, or a doubleheader, could
  bind the wrong Kalshi event). Recommend porting `pick_game`'s exact-date + `used`-set guard **before**
  sports is ever made subscribable. (INFO-grade today; flagged so it isn't lost.)

## Claim 5 — Silent drop / mis-join class (the GDP silent-no-join) → CONFIRMED no regression

- GDP full-date period is preserved on both sides (Claim 2) → no silent no-join; regression-guarded by
  `econ_gdp_full_date_period_joins`. `≥T` with no listed twin is skipped+counted identically
  (`matcher.rs:151` `None` → `discovery.rs:445` `econ_skipped += 1` ≡ `ge_no_identical_twin`). No econ
  family silently drops. The only behavioral narrowing is the sports counter (Claim 4), not a trade pair.

---

**Bottom line:** The econ phantom (L21) **cannot recur through this code** — the twin direction, per-series
grid_step, threshold rounding, and both period parsers are faithful ports, verified by differential
execution against the live Python. The lone discrepancy is sports date-binding, which is confined to a
coverage counter and has no path to the live order loop; port `pick_game`'s exact-date + doubleheader
guard before sports is ever emitted as a subscribable pair.
