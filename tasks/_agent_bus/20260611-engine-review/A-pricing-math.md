# Adversarial review — PRICING/MATH core (`signal.rs`, `ledger.rs`, `book.rs`, `matcher.rs`)

**Reviewer:** independent, read-only (no edits made). **Date:** 2026-06-11.
**Scope:** the four math modules + fidelity vs `bot/ledger.py`, `bot/monitor.py`, `bot/kalshi_book.py`,
`bot/colisted_map.py`. Differential tests run against live Python (vectors inline). Builds clean;
`cargo clippy` = 5 warnings repo-wide, 1 in-scope (matcher arg count) + the rest cosmetic.

## Summary

- **CRITICAL: 0.** No wrong-number/sign/loss/crash reachable on the **live order path**. The signal +
  game-edge math is bit-faithful to the Python on every vector I threw at it (incl. the L21 econ twin and
  the C3 guard); the depth walk is a line-for-line port; the matcher join is identity-correct.
- **WARN: 3.** (1) `order_taker_fee_cents` **ceils the pmus leg**, but Python `pfee` is linear-no-ceil →
  the *accounting* core diverges from its source-of-truth (NOT live: `book_pair` has zero non-test
  callers). (2) `matcher.rs::re_two` does **not** retry at later `gte` occurrences like Python's
  `re.search` backtracks → a decoy `gte<digits>` before the real `gteXltY` mis-parses (latent: current
  weather slugs have exactly one `gte`). (3) `to_key`/doc mismatch: keys quantize to **4dp**, not the
  "1c tick" the doc claims (safe for 2dp venue prices today; a genuine sub-cent pmus price would
  silently fragment/merge a level differently than Python's raw-float dict).
- **INFO: 3.** `round4` vs Python `round` banker's (0 divergence on the grid — documented assumption is
  correct, noted for the record); `match_econ` arg-count clippy warn; minor dead-doc.

### Top findings (one-line each)
1. **[WARN] ledger.rs:30** — `order_taker_fee_cents` ceils pmus too; Python `pfee` is linear → booked PnL over-states pmus fee (n=1 @0.5: 2c vs 1.25c). Not live (no caller), but breaks the documented `ledger.py` parity.
2. **[WARN] matcher.rs:237** — `re_two` (`gteXltY`) stops at the **first** `gte` and never retries; `re.search` backtracks → `…gte5high…gte64lt65f` → Rust `(5,None)` vs Python `(64,65)`. Latent today (one `gte` per real slug).
3. **[WARN] book.rs:23** — `to_key` quantizes price to 4dp (×10000), not the "1c venue tick" the module doc claims; OK for 2dp inputs, would diverge from Python's raw-float book on a true sub-cent price.
4. **[INFO] matcher.rs:125** — clippy `too_many_arguments` (8/7) on `match_econ` (the only in-scope clippy hit).
5. **[INFO] signal.rs:35 / book.rs:211** — `round4` is half-away-from-zero vs Python banker's `round`; **verified 0 divergence** for all (1−p) on the 1c grid, so the inline "half-way never occurs" claim holds.

---

### [WARN] ledger.rs:30-33 — `order_taker_fee_cents` ceils the pmus leg; Python `pfee` is linear (no ceil)

**Problem:** The function is generic over `coef` and **always ceils**:
```rust
pub fn order_taker_fee_cents(coef: f64, n: u32, p: f64) -> u32 {
    let cents = coef * (n as f64) * p * (1.0 - p) * 100.0;
    (cents - CEIL_EPS).ceil().max(0.0) as u32
}
```
But the Python source-of-truth (`bot/ledger.py:45-47`) computes the **pmus** fee as *linear, no ceil*,
returning fractional cents:
```python
def pfee(p, n=1, taker=True):
    if not 0 < p < 1: return 0.0
    return (0.05 * n * p*(1-p)) * (1.0 if taker else 0.0)
```
Only Kalshi (`kfee`) ceils per order. So calling `order_taker_fee_cents(PMUS_TAKER_COEF, …)` over-charges
the pmus leg whenever its raw fee isn't already a whole cent. Differential (verified live):

| n  | p    | Python `pfee` (cents) | Rust `order_taker_fee_cents(0.05,…)` |
|----|------|-----------------------|--------------------------------------|
| 1  | 0.50 | 1.2500                | **2** (+60%)                         |
| 10 | 0.75 | 9.3750                | **10**                               |
| 10 | 0.14 | 6.0200                | **7**                                |
| 3  | 0.33 | 3.3165                | **4**                                |
| 100| 0.50 | 125.0000              | 125 (exact — only matches at integers)|

The module's **own** test bakes the wrong value in: `ledger.rs:103` `order_taker_fee_cents(PMUS_TAKER_COEF, 10, 0.75)` → 10c, where Python books 9.375c — so `pnl_is_outcome_independent_and_nets_fees` asserts a PnL that disagrees with `ledger.py` by 0.625c/pair on that leg.

**Why WARN not CRITICAL:** `Pnl::book_pair` and `order_taker_fee_cents` have **zero callers** outside
`ledger.rs`/`signal.rs` tests (grepped `main.rs`/`exec.rs`/`risk.rs`/`unwind.rs` — no references; the live
exec path computes no fees at all yet). So nothing books real money through this path today. It is a
fidelity break in a tested accounting primitive whose docstring explicitly promises “reproduce
[ledger.py's] selftest vectors … a wrong fee silently turns a +EV arb into a loss (L10/L15).”

**Fix:** Don't ceil the linear-fee venue. Make the rounding venue-specific, e.g. add a sibling
`order_pmus_fee_cents(n, p)` that returns the **unrounded** `0.05·n·p·(1−p)·100` (Python keeps `pfee`
unrounded and lets the cent-rounding fall out only when summing), OR carry pmus fees in dollars (f64)
rather than integer cents so `book_pair` nets `gross − pfee_dollars − kfee_dollars` exactly as
`ledger.py:100` does (`net_edge = (1-cost)*size - feeY - feeN`). At minimum, gate this behind the
“parity test green before live” checklist the file already cites, and fix the `ledger.rs:103` test vector.

---

### [WARN] matcher.rs:237-245 — `re_two` (`gteXltY`) does not retry at later `gte`; `re.search` backtracks

**Problem:**
```rust
fn re_two(s: &str, t1: &str, t2: &str) -> Option<(i64, i64)> {
    let p1 = s.find(t1)?;                       // <-- FIRST occurrence only
    let (x, after_x) = digits_at(s, p1 + t1.len())?;
    if !s[after_x..].starts_with(t2) {
        return None;                            // <-- gives up; never tries the NEXT `gte`
    }
    let (y, _) = digits_at(s, after_x + t2.len())?;
    Some((x, y))
}
```
Python `pm_bounds` (`colisted_map.py:98`) uses `re.search(r"gte(\d+)lt(\d+)", s)`, which **backtracks**:
if the first `gte<digits>` isn't followed by `lt`, the engine keeps scanning and finds a later
`gte<digits>lt<digits>`. Rust commits to the first `gte`, and on failure falls through to
`re_one("gte")`, returning the wrong tail. Differential (verified live):

| slug                                          | Python    | Rust        |
|-----------------------------------------------|-----------|-------------|
| `tc-temp-gte5high-2026-06-09-gte64lt65f`      | `(64,65)` | **`(5,None)`** ✗ |

`(5,None)` is a *valid-looking* high-tail bounds that would then `k_bounds`-match a real Kalshi
`floor=4` bucket — i.e. a **wrong-bucket settlement-identity pair** (an L17-class phantom), exactly the
failure mode the bounds-equality join exists to prevent.

**Why WARN not CRITICAL:** current pmus weather slugs are `tc-temp-{alphabetic-city}high-{date}-{bucket}`
(`wcity` = `([a-z]+?)high`), so `gte`/`lt` appear **once**, in the bucket — the decoy can't occur today.
It bites only if a future slug shape (or an adversarial/renamed market) places a `gte<digits>` not
followed by `lt` ahead of the real bucket tail. The parity review checked `gte72f` precedence but not a
**preceding decoy** `gte`, so this was missed.

**Fix:** Make `re_two` loop over occurrences of `t1` (mirror `re_one`'s `while let Some(rel) = s[from..].find(t1)` retry), and only return `None` after exhausting them:
```rust
fn re_two(s: &str, t1: &str, t2: &str) -> Option<(i64, i64)> {
    let mut from = 0;
    while let Some(rel) = s[from..].find(t1) {
        let p1 = from + rel;
        if let Some((x, after_x)) = digits_at(s, p1 + t1.len()) {
            if s[after_x..].starts_with(t2) {
                if let Some((y, _)) = digits_at(s, after_x + t2.len()) {
                    return Some((x, y));
                }
            }
        }
        from = p1 + t1.len();
    }
    None
}
```
Add the decoy slug to `weather_bounds_canonicalize_both_venues` as a regression vector.

---

### [WARN] book.rs:19-28 — `to_key` quantizes to 4dp, not the "1c venue tick" the doc claims

**Problem:** The module header (book.rs:13) and the type doc (book.rs:19-21) say prices are “quantized to
the 1c venue tick … so float dust can't fragment a level,” but the code quantizes to **4 decimal places**:
```rust
fn to_key(price_dollars: f64) -> PriceKey { (price_dollars * 10_000.0).round() as PriceKey }
```
At 4dp, `0.6699` and `0.6700` are **distinct** keys (6699 vs 6700) — the level dedup is at 1/100¢, not 1¢.
For the current 2dp venue prices (Kalshi dollars, pmus 2dp) this is harmless and ordering is preserved, so
`best()` and the ladders match Python’s raw-float dict exactly (verified: no key collisions, no ordering
inversions on the 0–100¢ grid). The Python `KalshiBook` keys on the **raw float** (`{float(p): q}`), so for
any price with >4 significant dp the two implementations would diverge: Python keeps two separate dict
entries; Rust **merges** them into one tick (summing qty) if they round equal, or keeps a 4dp-fragmented
ladder Python wouldn't. That is a latent depth/`best` divergence the moment a genuine sub-cent price appears.

**Why WARN not CRITICAL:** purely latent — needs a real >2dp price, which neither venue emits on the tracked
series today. But the doc is actively misleading (says 1c; is 1/100c), and the safety argument (“1c tick
dedup”) is not what the code does.

**Fix:** Either (a) make the comment truthful — “quantized to 4dp (1/100¢) — safe for the 2dp venue grid;
a true sub-cent price would key separately,” or (b) if 1¢ dedup is actually intended, use `×100.0` and a
0..=100 key range. Given pmus *could* serve finer prices, keep 4dp but fix the doc and add an assert/round
of inputs to the venue tick on ingest if 1c is the contract.

---

### [INFO] matcher.rs:125-134 — clippy `too_many_arguments` (8/7) on `match_econ`

**Problem:** `cargo clippy` warns:
```
warning: this function has too many arguments (8/7)
   --> src\matcher.rs:125:1   pub fn match_econ(ineq, thr, step, fed_label, period, family, k_floors, k_labels)
```
Functional, but the positional `Option<f64>, f64, Option<&str>, &str, &str` run is easy to transpose at a
call site (e.g. swap `period`/`family`, both `&str`) — a silent cluster-label bug, not a price bug.

**Fix:** Bundle the parsed-slug fields into a small `EconQuery { ineq, thr, step, fed_label, period, family }`
struct (or reuse `discovery::EconParse`) and pass `&query` + the two Kalshi catalogs. Clears the lint and
removes the transposition foot-gun.

---

### [INFO] signal.rs:35 & book.rs:211 — `round4` half-away-from-zero vs Python banker's `round`

**Problem:** Both `round4` impls use `(x*1e4).round()/1e4`, where Rust `f64::round` is **half-away-from-zero**;
Python `round()` is **banker's (half-to-even)**. The inline comments assert the half-way case “effectively
never occurs” for these edges. **I verified this:** across all `1−p` for p∈{0..100}¢ there are **0**
divergences between Rust `round4` and Python `round(·,4)`/`round(·,6)`; and a 4dp net edge lands exactly on a
half-ULP only for inputs the venue can’t produce. So the documented assumption is **correct** — flagging only
so the next person doesn’t “fix” it into a real divergence. The risk surface (a net edge sitting *exactly* on
the τ floor where banker's vs half-up flips the trade/no-trade decision) does not occur on the grid.

**Fix:** None required. If you want belt-and-suspenders, add a debug-only assert that `round4` and a
round-half-even reference agree on ingested prices, so a future finer tick that breaks the assumption trips a
test rather than silently flipping a near-floor edge.

---

### Verified-clean (explicitly checked, no issue) — so these aren't re-flagged later

- **`signal` / `game_signal`:** dir mapping (PK=YES@pmus), crossed-venue skip (both-touch-only), per-direction
  one-sided pricing, the `0<p<1` fee guard, tie→first-pushed stability (`fold` replaces on strictly-greater =
  Python `max` keeps-first), and `no_arb = best ≤ 0`. Bit-for-bit vs `ledger.py::signal` and
  `monitor.py::game_edge` on S1/S2/flat/crossed/one-sided + PK/KP/C3 vectors. The C3 guard's `pm_ask.or(pm_bid)`
  fallback covers dir KP in both venues — matches `monitor.py:165`.
- **`book.rs::depth_curve`:** the two-pointer walk is a line-for-line port of `monitor.py:81-99` — same
  `edge<lo` break, same `step=min(a_rem,b_rem)`, same `<=QTY_EPS` stop, same `i`/`j` advance order (both can
  advance on one step), same partial-remainder carry. `depth_at_edge`/`game_depth_at_edge` dir-wiring pairs the
  two venues correctly in both directions (never a venue with itself); `no_ask_ladder = 1−bid`,
  `yes_ask_ladder = 1−no_bid`, `best()` O(1) via map ends — all match. Equal-price levels collapse correctly.
- **`matcher.rs` econ:** `econ_twin = round6(T−step)` (float-safe: `4.4−0.1`, `2.0−0.1`, `250000−1000` all
  exact-to-1e-9), `≥`→twin-floor / `≤`,`==`→skip / Fed label==label (5-entry table character-identical to
  `_FEDLBL`), `(floor−twin).abs()<1e-9` comparison vs Python's 6dp-keyed dict — both True on float-nasty cases.
  The L21 off-by-one cannot recur. `digits_at`/`re_one`/`re_one_after` UTF-8-safe (ASCII byte scans, `parse`
  can't panic on a digit run within i64 range — a >19-digit run would `parse::<i64>()`-fail to `None`, harmless).
- **`ledger.rs::book_pair`:** outcome-independence by construction (`1−cost_yes−cost_no`), the old tautological
  assert correctly removed, `debug_assert` leg-cost guard. Kalshi `order_taker_fee_cents` ceil + `CEIL_EPS=1e-9`
  matches `kfee` exactly (175c/125c boundaries, n=1 ceil). `n: u32` × price keeps the f64 product well within
  range (no overflow before `as u32`; `max(0.0)` guards a negative from a bad coef). The **only** ledger issue
  is the pmus-ceil above.

## clippy result
`cargo clippy --manifest-path bot-rs/Cargo.toml` → **5 warnings, repo-wide**; in the four target files: **1**
(`matcher.rs:125` too_many_arguments). Others: `main.rs:7` doc-indent (cosmetic), `risk.rs:88` & `unwind.rs:29`
`map_or`→`is_some_and`/`is_none_or` (cosmetic, out of scope). No warnings in `signal.rs`/`ledger.rs`/`book.rs`.
