# Adversarial review — DISCOVERY / CATALOG-PARSE (`bot-rs/src/discovery.rs`)

**Reviewer:** independent, read-only (no edits). **Date:** 2026-06-11.
**Scope:** the catalog PARSE + ASSEMBLE + sports binding + pagination + coverage + live `discover` I/O.
The `matcher.rs` joins are taken as correct per the parity reviews and NOT re-reviewed. Findings are
verified by **differential execution** against `bot/colisted_map.py` (live Python) and against the **real
Rust parsers** compiled standalone (`rustc` harness); every quoted divergence below was reproduced, not
inferred.

## Summary

- **CRITICAL: 1.** The refresh loop prunes live markets on a **truncated-but-OK** pmus pull (`d.truncated`
  ignored on the heartbeat path) — the canonical "partial pull treated as authoritative → prunes live"
  hazard. Needs >12k live pmus markets to trigger; latent today, but that is exactly what the cap guards.
- **WARN: 4.** (1) Kalshi cursor pagination has **no max-page / stuck-cursor guard** (the one unbounded
  I/O loop; pmus and the Python both have a cap). (2) `parse_release_period` 3-char-prefix collision on a
  **digit-bearing** segment (`-jun2-`, `-may1adj-`) diverges from Python and picks the WRONG month. (3)
  `parse_cpi_period` scans months in **calendar order**, not left-to-right → on two `monthYYYYyoy` tokens
  it picks a different month than Python's leftmost-`re.search`. (4) Sports doubleheader event→index
  assignment is **non-deterministic** (HashMap iteration) so a held sports pair can swap its Kalshi
  tickers across re-discovery; Python is stable.
- **INFO: 3.** `weather_city`/`event_date`/`field_str` minor corners — see below.

### Top findings (one line each)
1. **[CRITICAL] main.rs:545-551 (re: discovery.rs:705-707)** — refresh loop ignores `d.truncated`; a persistently capped pmus catalog prunes its tail markets (2-miss debounce can't save a persistent truncation).
2. **[WARN] discovery.rs:713-730** — `pull_kalshi_series` cursor loop has no page cap / stuck-cursor break: a non-empty stuck cursor = infinite loop + unbounded `Vec` growth. No Kalshi-side analog of `PM_CATALOG_CAP`.
3. **[WARN] discovery.rs:339-351** — `parse_release_period` accepts a 3-char month prefix on a digit-bearing segment (`urc-jun2-…-july-…` → Rust `26JUN` vs Python `26JUL`); Python's `-(month)[a-z]*-` rejects it. Wrong period → wrong cluster + wrong/no twin-join.
4. **[WARN] discovery.rs:313-335** — `parse_cpi_period` iterates `MON` in calendar order; on a slug with two `monthYYYYyoy` tokens it returns the calendar-earliest (`sep…yoy mar…yoy` → Rust `26MAR`) vs Python leftmost (`25SEP`).
5. **[WARN] discovery.rs:577-583** — doubleheader event list built from a `HashMap` → per-process-random index; the `(date,i)` `used`-key makes a same-team DH pair bind a non-deterministic Kalshi game across refreshes (Python's list order is API-stable).

clippy: **0 warnings in `discovery.rs`** (5 repo-wide: `matcher.rs:125` arg-count + `main.rs:6/7` doc-indent + `risk.rs:88`/`unwind.rs:29` `map_or` — all out of this module, all already noted by sibling reviewers).

---

### [CRITICAL] main.rs:545-551 — refresh loop prunes live markets on a TRUNCATED (but `Ok`) pmus pull

**Problem:** `discover` returns `Ok(Discovery{ truncated: true, .. })` when the pmus catalog hits the cap
(`discovery.rs:705-707`):
```rust
if all.len() > PM_CATALOG_CAP {
    return Ok((all, true)); // TRUNCATED -> coverage incomplete (caller warns)
}
```
The refresh loop treats any `Ok` as fully authoritative and proceeds to prune (`main.rs:545-551`, then
`prune_step` at `main.rs:570`):
```rust
let fresh = match discovery::discover(&http).await {
    Ok(d) => d,
    Err(e) => { println!("[refresh] discovery DEGRADED ({e}) — keeping current set, no prune (H4)"); continue; }
};
```
It never **gates pruning** on `fresh.truncated`. `report_coverage(&fresh)` IS called each refresh
(`main.rs:552`) so truncation is *logged* every cycle — but logging is all it does; the `Ok(d) => d` arm
falls straight through to `prune_step` regardless. A truncated catalog OMITS the
tail markets (offset beyond the cap) — those tracked slugs are now "absent from `current`" and after **2
consecutive** truncated refreshes `prune_step` (`main.rs:518`) prunes them as settled. The 2-miss debounce
does **not** rescue them: a genuine >12k catalog truncates *every* pull, so the miss count climbs
monotonically. This is the precise "partial pull → prune live" failure mode (`monitor.py` H4) the degraded-
path guard exists to prevent — but `truncated` is a *successful partial*, so it slips past the `Err` arm.

The Python (`colisted_map.py:266-269`) has the same blind spot in principle, but the Python monitor's prune
logic is upstream of this review; the Rust port reintroduces the gap on the live order path.

**Why CRITICAL not WARN:** it silently drops *live, tradeable* pairs (and frees their books, `main.rs:606-610`)
on a recurring condition, with no human in the loop — the inverse of the L7 "never silently miss" invariant,
on the bot that actually trades. Reachability is gated by `PM_CATALOG_CAP = 12_000`; pmus is nowhere near
that today, but the cap is a scale guard, so this fires exactly when coverage grows.

**Fix:** In the refresh loop, treat a truncated pull like a degraded one — skip pruning:
```rust
let fresh = match discovery::discover(&http).await {
    Ok(d) if d.truncated => { println!("[refresh] pmus catalog TRUNCATED — keeping current set, no prune"); /* still apply ADDs */ ... }
    Ok(d) => d,
    Err(e) => { ...; continue; }
};
```
At minimum gate `prune_step`/`prune_tickers` on `!fresh.truncated` (still allow the `add` side so new pairs
are picked up). Truncation is already logged every cycle (`report_coverage`, `main.rs:552`) — the missing
piece is that the log does not stop the prune. Raising the cap is not a fix (it just moves the cliff).

---

### [WARN] discovery.rs:713-730 — Kalshi cursor pagination: no page cap / no stuck-cursor guard (only unbounded loop)

**Problem:**
```rust
loop {
    let mut url = format!("{KALSHI_MARKETS}?series_ticker={series}&status=open&limit=1000");
    if let Some(c) = &cursor { url.push_str(&format!("&cursor={c}")); }
    let v = fetch_json(http, &url).await?;
    if let Some(arr) = v.get("markets").and_then(Value::as_array) { all.extend(arr.iter().cloned()); }
    cursor = v.get("cursor").and_then(Value::as_str).filter(|c| !c.is_empty()).map(str::to_string);
    if cursor.is_none() { return Ok(all); }
}
```
Termination depends **entirely** on Kalshi eventually returning an empty/absent `cursor`. There is no
max-page bound, no "cursor unchanged since last page" detection, and **no `PM_CATALOG_CAP` analog on the
Kalshi side** (the pmus loop has one, `discovery.rs:705`; the Python has none because it does not paginate
Kalshi at all — single page, `limit=400`/`1000`). A buggy/misbehaving endpoint that returns the **same
non-empty cursor** every page (or a self-referential cursor) loops forever while `all` grows without bound
— a hang + OOM on the live droplet, and it blocks the refresh task (so no further discovery/prune happens).
A page with **0 markets but a non-empty cursor** is itself fine (valid Kalshi paging) but provides no
progress signal to break a stuck cursor.

**Why WARN not CRITICAL:** requires a Kalshi paging bug (not a parse/logic error in this file), and the
endpoint is well-behaved today. But it is the only unbounded loop in the module and the only pull without a
safety valve, on a 24/7 process.

**Fix:** add a page cap + stuck-cursor break, mirroring the pmus cap:
```rust
let mut pages = 0usize;
loop {
    ...
    let next = v.get("cursor").and_then(Value::as_str).filter(|c| !c.is_empty()).map(str::to_string);
    pages += 1;
    if next.is_none() || next == cursor || pages > 50 || all.len() > 50_000 {
        return Ok(all); // empty / repeated cursor / page-cap / row-cap all terminate
    }
    cursor = next;
}
```
(50 pages × 1000 covers any tracked series with huge headroom; `next == cursor` catches the stuck cursor.)

---

### [WARN] discovery.rs:339-351 — `parse_release_period` 3-char prefix collision on a digit-bearing segment diverges from Python

**Problem:** the data-month is found by scanning dash-segments and testing the **first 3 chars**:
```rust
let dmon = s.split('-')
    .find_map(|seg| emon(&seg.chars().take(3).collect::<String>()).map(|m| (m, seg)))
    .map(|(m, _)| m)?;
```
Python (`colisted_map.py:211`) uses `re.search(r"-(jan|feb|…|dec)[a-z]*-", s)`, which requires the
month token to be **alphabetic up to the next dash**. When a segment's first 3 chars spell a month but the
segment then contains a **digit**, the two diverge — Rust accepts the prefix, Python's `[a-z]*-` cannot
cross the digit and skips to the real month. Verified live (real Rust + live Python):

| slug | Rust `parse_release_period` | Python `econ_parse.period` |
|---|---|---|
| `urc-jun2-gte-july-2026-08-02-atl4pt4` | **`26JUN`** ✗ | `26JUL` |
| `urc-may1adj-gte-july-2026-08-02-atl4pt4` | **`26MAY`** ✗ | `26JUL` |
| `urc-marathon-gte-october-2026-11-06-atl4pt4` | `26MAR` | `26MAR` (both collide — `marathon`→`mar`, *agree*) |
| `urc-us-augmented-gte-june-…-atl4pt4` | `25AUG` | `25AUG` (agree) |

So the pure-alpha collisions (`marathon→mar`, `augmented→aug`, `decadelong→dec`) match Python (a *shared*
limitation, not an infidelity), but a **digit-bearing** decoy segment (`jun2`, `may1adj`) is a true
Rust-only mis-read. A wrong `period` → wrong `cluster` key (`family-period`) **and** a wrong/no Kalshi
twin-join (the floor is keyed by `k_econ_period`, which would be a different month) → either a silent
no-join (real pair dropped) or, if a Kalshi market happens to carry the decoy month, a **wrong-period
pair** (an L21-class settlement-mismatch: two different release months priced as one).

**Why WARN not CRITICAL:** current urc/nfpc pmus slugs put the data-month as a clean alpha token
(`-june-`, `-december-`), so no digit-decoy occurs today; the parity review's 14 vectors didn't include a
digit-bearing month-prefix segment, so this corner was uncovered.

**Fix:** require the month segment to be **purely alphabetic** before accepting it (match Python's `[a-z]*-`):
```rust
let dmon = s.split('-')
    .find_map(|seg| {
        let pre: String = seg.chars().take(3).collect();
        // accept only if the WHOLE segment is alphabetic (Python's -(month)[a-z]*- left/right alpha boundary)
        (seg.chars().all(|c| c.is_ascii_alphabetic())).then(|| emon(&pre)).flatten()
    })?;
```
Add `urc-jun2-…` and `urc-may1adj-…` as regression vectors.

---

### [WARN] discovery.rs:313-335 — `parse_cpi_period` calendar-order scan vs Python leftmost-match

**Problem:** the CPI month is found by iterating `MON` (Jan→Dec) and taking the first that `find`s in the
slug:
```rust
for (mi, m) in MON.iter().enumerate() {
    let ml = m.to_ascii_lowercase();
    if let Some(p) = s.find(&ml) { ... return Some(format!("{}{}", &yr[2..], MON[mi])); }
}
```
Python (`colisted_map.py:202`) uses one `re.search(r"(jan|…|dec)[a-z]*?(\d{4})yoy", s)` — **leftmost**
match in the string. When a slug contains **two** `monthYYYYyoy` tokens, Rust returns the calendar-earliest
month, Python the textually-first. Verified live (real Rust + live Python):

| slug | Rust | Python |
|---|---|---|
| `cpic-sep2025yoy-mar2026yoy-x-3pt8pct` | **`26MAR`** ✗ | `25SEP` |
| `cpic-jan2025-may2026yoy-…-3pt8pct` | `26MAY` | `26MAY` (agree — `jan2025` has no `yoy`, both skip it) |
| `cpic-maybe2026yoy-…` | `26MAY` | `26MAY` (both mis-read `maybe`→`may`; shared limitation) |

The single-token case (every real CPI slug today) agrees; only a **two-`yoy`-token** slug diverges. Same
consequence class as the release-period finding: wrong period → wrong cluster + wrong/no twin-join.

**Why WARN not CRITICAL:** real pmus CPI slugs carry exactly one `…yoy` token; the divergence needs a
second one, which doesn't occur today (and the parity review didn't probe a two-token slug). Note both
sides mis-handle a slug-WORD containing a month substring (`maybe`→`may`) via the all-alpha `inter` gate —
a *shared* weakness, flagged for the record but not a port infidelity.

**Fix:** to match Python exactly, find the **leftmost** `monthYYYYyoy` rather than scanning months in
calendar order — e.g. iterate byte positions and at each, test whether a 3-letter month begins there
followed (after optional alpha) by `\d{4}yoy`, returning on the first hit. Cheaper interim: collect all
`(pos, month)` candidates and pick the min `pos`. Add the two-token slug as a regression vector.

---

### [WARN] discovery.rs:577-583 — doubleheader event→index assignment is non-deterministic (HashMap order)

**Problem:** Kalshi events are bucketed by date out of a `HashMap`, so the per-date event **list order**
(hence each event's index `i`) is randomized per process:
```rust
let mut by_event: HashMap<String, HashMap<String, String>> = HashMap::new(); // random iteration
...
for (ev, dict) in by_event {                       // <-- non-deterministic order
    let date = event_date.get(&ev).cloned().unwrap_or_default();
    kbydate.entry(date).or_default().push(dict);   // index i = push order = random
}
let mut used: HashSet<(String, usize)> = HashSet::new(); // used-key is (date, i)
```
`pick_game` consumes the first un-`used` event at `(date, i)` (`discovery.rs:391-399`). For a same-team
**doubleheader** (two Kalshi events on one date), two pm games bind `{i=0, i=1}` — but **which** pm slug
gets `i=0` depends on this random list order. Across the periodic re-discovery (`refresh_loop`), the same
held pm slug can therefore bind **Kalshi game-1 on one pass and game-2 on the next**, swapping its
`kalshi`/`kalshi_b` tickers under a live position → it would then track the *other* game's books. Python
keys `used` by `id(pl)` over a list built in **Kalshi-API order** (`colisted_map.py:319-320`), which is
stable per pull.

Note: *neither* Python nor Rust actually disambiguates DH game-1 from game-2 (the `(league, date, abbrev)`
key genuinely can't — a shared, known gap; the `used`-set only guarantees two *distinct* events). The
Rust-specific defect is the **instability**: Python is deterministic across passes, Rust is not.

**Why WARN not CRITICAL:** (a) sports requires an explicit settlement override to reach the order path at
all (`settle_clean=false`, per the sports-parity review), so it isn't live by default; (b) both legs are
real games either way; (c) it needs a true same-team same-date doubleheader with two same-dated pmus slugs.
But it *is* a wrong-game-book bind under re-discovery once sports is enabled — worth pinning before then.

**Fix:** make the per-date event list deterministic — sort `kbydate[date]` by a stable key (e.g. the
event_ticker) before indexing, or carry the event_ticker into the `used`-key instead of the positional
index:
```rust
let mut by_event: std::collections::BTreeMap<String, ...> = ...; // or sort the Vec by event_ticker
```
A `BTreeMap`/sorted push gives a stable index across passes; alternatively key `used` on
`(date, event_ticker)` so the bind is identity-stable regardless of list order.

---

### [INFO] discovery.rs:108-116 — `weather_city` drops a city token that STARTS with "high" (Python keeps it)

**Problem:** `after.split("high").next()` returns `""` when the city token itself begins with `high`, and
the `city.is_empty()` guard then drops the market:
```rust
let city = after.split("high").next()?;   // "highlandhigh-…" -> "" (splits at the leading "high")
if city.is_empty() || city.contains('-') { return None; }
```
Verified: `tc-temp-highlandhigh-2026-06-09-…` → Rust `None`, Python `wcity` (`[a-z]+?high`) → `"highland"`.
A city whose token starts with "high" is **silently missed** by Rust but parsed by Python. (`newhighhigh` →
both `"new"`; only the *leading*-high case diverges.)

**Why INFO:** no mapped city (`sfo/lax/nyc/mia/mdw`) starts with "high", and a miss is **fail-safe** (drops
to coverage, never a phantom). But it's a silent-drop divergence from the port; if pmus ever lists such a
token it lands in `weather_cities_unmapped` (loud) only if `weather_city` returns `Some` — here it returns
`None`, so it's dropped *without* even a coverage flag.

**Fix:** match Python's "first non-empty prefix before `high`" — e.g. find the LAST `high` if the first
yields empty, or port the non-greedy regex semantics: take the substring up to the first `high` that leaves
a non-empty prefix. Low priority; document the divergence if not fixing.

---

### [INFO] discovery.rs:568-571 — `event_date` is first-ticker-wins; an event with mixed ticker dates silently takes the first

**Problem:**
```rust
event_date.entry(ev.clone()).or_insert_with(|| ktok_date(&tk).unwrap_or_default());
```
The event's date is taken from whichever of its markets is iterated first; if two markets under one
`event_ticker` carried different ticker-date tokens (malformed Kalshi data, or a ticker whose first
`ktok_date` match is wrong), the event is dated by the first one only. Equivalent to Python's `evd[ev] =
…` last-wins (`colisted_map.py:317` overwrites per market) — actually a **subtle divergence**: Python is
*last*-write-wins (plain assignment in the loop), Rust is *first*-write-wins (`or_insert_with`). Same date
for a well-formed event; differs only if an event's markets disagree on the date token.

**Why INFO:** well-formed Kalshi events share one date across both team tickers, so first==last; purely a
malformed-data corner. Flagged because it's a real first-vs-last-wins difference from the Python.

**Fix:** none required for correct data; if hardening, assert all of an event's ticker dates agree, or pick
deterministically (Python's last-wins) for exact parity.

---

### Verified-CLEAN (explicitly checked against live Python + real Rust — not re-flagged)

- **`enum_val` cannot emit inf/NaN/overflow on the path.** The upstream charset guards
  (`parse_ineq_tail`/`parse_point_tail`, `discovery.rs:282,291,305` = `[0-9 p t (k)]`) reject `inf`/`nan`/
  `1e2` (letters `i/n/f/e` not allowed) **before** `enum_val` is called. Worst real input → `None`
  (`"t"`,`"pt"`,`"ptk"`) or a finite value; `999999999k`→`9.99e11` is harmless (no twin matches). Python
  `_enum` sees the same charset and matches Rust on every vector (`4pt4`,`250k`,`4ptk`,`pt4`,`4pt4pt4`→None).
- **`econ_parse` point-vs-ineq disambiguation is correct.** `-gteXpct`/`-lteXpct`→`Ge`/`Le`,
  `-atlX`/`-atmX`→`Ge`, bare `-Xpct`→`Eq`; a `-gteXpct` is never mis-read as a point bucket (the ineq tail
  is tried first, `discovery.rs:253-258`) and a bare `-Xpct` is never mis-read as `Ge`. Matches Python on
  all 5 orientation vectors. `atm`→`Ge` (cumulative "at least") matches `colisted_map.py:194`.
- **`k_econ_period` GDP day-digit + trailing-dash requirement** is faithful (parity Claim 2, re-confirmed):
  `KXGDP-26JUL30-…`→`26JUL30`, `KXU3-26JUN-…`→`26JUN`, `KXU3-26JUN`(no dash)→`None`, `KXU3-26JUN-T44`→
  `26JUN` (the `44` strike after the dash is never grabbed).
- **`ktok_date` / `iso_date` malformed + two-date corners:** both take the **first** valid token
  (`KXMLBGAME-26JUN16-LAD-26JUN17`→`2026-06-16`; `…-2026-06-16-then-2026-06-17`→`2026-06-16`), reject a
  bad month (`-26ZZZ11-`→`None`), and `iso_date` does **not** range-check (`2026-13-99`→`2026-13-99`) — but
  that only feeds `days_to_event` via `ymd_to_epoch_days`, which **does** range-check (`!(1..=12)`,
  `!(1..=31)` → `None` → gate dormant), so a bad date can't produce a bogus proximity number.
- **`ymd_to_epoch_days`** (Hinnant civil-days) matches known anchors and leap-year deltas (tests +
  re-derived: 1970-01-01→0, 2000-01-01→10957, 2024 Feb-29 spans correctly); month/day bounds enforced.
- **`pull_pmus` pagination termination** is correct and matches Python's order (short-page check *before*
  the cap check, `discovery.rs:702-708` ≡ `colisted_map.py:264-269`); no off-by-one, no infinite loop (the
  cap bounds it). pmus URL (`closed=false&limit=500&offset=`, no `status`) matches Python exactly.
- **Degraded-pull handling is SOUND** (the headline safety property): any page `Err` propagates via `?` →
  `discover` returns `Err` → `refresh_loop` does `continue` with **no prune** (`main.rs:547-550`, H4), and
  pruning is 2-miss-debounced on the pmus slug (`prune_step`). The ONLY hole is the *successful-but-
  truncated* case (the CRITICAL above) — a true fetch error is handled correctly.
- **`sports_abbrevs` A/B ordering** (long-named side = A) matches Python on 3-way / both-`long` /
  string-`long` / third-no-name vectors. The one divergence — a side missing `abbreviation` → Rust `None`
  (whole pair dropped) vs Python `("", …)` — is **benign**: Python's `""` fails to resolve in `pick_game`
  too, so both drop the pair (different stage, same outcome). A 3-outcome `marketSides` silently keeps 2 of
  3 on **both** sides (shared limitation; the formed pair is self-consistent).
- **Category/field coercion:** `field_str` returns `Some` only for JSON strings, so a numeric/absent
  `category` → dropped (matches Python's `== "climate"` string compare). `field_i64`/`field_f64` coerce a
  numeric *string* strike to a number — **more** robust than Python `kbounds`, which on a string strike
  returns `('64','65')` and silently never matches (no Rust regression).
- **Sports IS now emitted into `d.pairs`** (`discovery.rs:597-606`, `Cat::Sports` with `kalshi_b`) — this
  is NEWER than the matcher/discovery parity review (Claim 4 said "counted, never pushed to `d.pairs`",
  now stale). The current binding (`pick_game` exact-date + `used`-set + distinct-ticker) is the faithful
  one the **sports-parity** review verified, and it's gated off the live path by `settle_clean=false` +
  the risk-layer settlement override — so emitting it is safe, but it does make every sports parse corner
  above *live-path-relevant* (raising findings #3/#5 from "coverage telemetry" to "tracked-book") once the
  override is set.
