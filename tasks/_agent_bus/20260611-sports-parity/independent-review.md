# Independent adversarial review — Rust sports trading logic parity

**Reviewer:** independent, no authorship stake. **Scope:** the LIVE ORDER PATH for the newly-added
2-outcome SPORTS logic in `bot-rs/`, verified byte-faithful to the Python it ports.

**VERDICT: FAITHFUL — no discrepancies found. A sports order cannot be sent mis-hedged or on the wrong game by this code.**

`cargo test` = 80 passed / 0 failed. A differential Python re-run of `game_edge` on the PK/KP/C3/crossed
vectors matched the Rust test expectations bit-for-bit (PK net 0.0002, KP net 0.2425, C3 guard trips,
crossed-pm → None).

---

## Checklist results (each confirmed against actual code)

### 1. `game_signal` vs `game_edge` — FAITHFUL
`signal.rs:120-149` ports `monitor.py:157-179` verbatim:
- Strictly-crossed-pm reject: Rust `matches!((pm_bid,pm_ask),(Some(b),Some(a)) if b>a)` (`signal.rs:122`) == Py `if pm_bid is not None and pm_ask is not None and pm_bid > pm_ask` (`monitor.py:163`).
- **C3 guard covers BOTH directions:** `guard_pm = pm_ask.or(pm_bid)` (`signal.rs:126`) == Py `guard_pm = pm_ask if pm_ask is not None else pm_bid` (`monitor.py:165`); reject on `(g-ka).abs() > 0.40` (`signal.rs:128`) == `abs(guard_pm - kA_ask) > 0.40` (`monitor.py:166`). The bid-fallback is what keeps dir KP guarded when pm has no ask — present in both.
- PK net = `round4((1-(pa+kb)) - pmus_marginal_fee(pa) - kalshi_marginal_fee(kb))` (`signal.rs:136`) == Py `round((1-(pm_ask+kB_ask)) - pfee(pm_ask) - kfee(kB_ask,marginal=True),4)` (`monitor.py:172`).
- KP net with `pm_backb = round4(1-pb)` (`signal.rs:140-141`) == Py `pm_backB = round(1-pm_bid,4)` (`monitor.py:174-175`). Both `round4`s present (the inner `1-pm_bid` AND the outer net).
- Tie-stability: `fold(opts[0], |acc,o| if o.1 > acc.1 {o} else {acc})` replaces only on **strictly greater**, so on a PK/KP tie PK (pushed first) wins — matches Python `max(opts,key=…)` which keeps the first max. (`signal.rs:148`)
- `arb = best.net > 0`: Rust `no_arb: best.1 <= 0.0` (`signal.rs:149`) is the logical complement of Py `"arb": best[1] > 0` (`monitor.py:179`).
- Hand-recompute confirmed: PK(.50/.52, kA.55, kB.45) → 0.0002 both; KP(.60/.62, kA.33, kB.70) → 0.2425 both.

### 2. `game_depth_at_edge` vs `GameTracker._depth` — FAITHFUL
`book.rs:281-287` ports `monitor.py:210-216`:
- PK → `(pm.yes_ask_ladder(), kb.yes_ask_ladder())` == Py `a = _pairs(pm_offers); b = kb_book.offer_pairs()` (`monitor.py:214-215`, `direction[0]=='P'`, `direction[1]!='P'`).
- KP → `(ka.yes_ask_ladder(), no_ask_ladder(&pm.yes_bid_ladder()))` == Py `a = ka_book.offer_pairs(); b = _no_ask_pairs(pm_bids)`.
- Ladders are paired ACROSS venues in both directions (PK = pmus×Kalshi-B; KP = Kalshi-A×pmus). Never a venue with itself; dir wiring is not swapped. `offer_pairs` = `1 - no_bid` (`kalshi_book.py:64`) matches `yes_ask_ladder` (`book.rs:120-127`); `_no_ask_pairs` = `1 - yes_bid` (`monitor.py:79`) matches `no_ask_ladder` (`book.rs:217-219`).

### 3. `discovery.rs` sports emission vs `pick_game`/`_match_game` — FAITHFUL
`discovery.rs:381-435` ports `colisted_map.py:133-158`:
- EXACT-date arm runs first, unconditionally (`discovery.rs:390-400` == `monitor`/`colisted_map.py:151-153`): slug ET date == Kalshi ticker date.
- Doubleheader `used`-set: each Kalshi event consumed at most once per league pass — Rust marks `used.insert((date,i))` by (date,index) (`discovery.rs:396`); Py marks by `id(pl)` (`colisted_map.py:149`). Different key, identical semantics (per-event one-shot within the shared set). The league pass builds ONE `used` and threads it through every pm game (`discovery.rs:583` ↔ `colisted_map.py` caller).
- ±1-day fallback ONLY when `!slug_dated` AND `hits.len()==1` (`discovery.rs:402-421`) == Py `if not slug_dated … if len(near)==1` (`colisted_map.py:154-157`). A dated slug whose date has no event returns None (no fallback) — test `pick_game_binds_exact_date_not_adjacent`.
- Two DISTINCT tickers required: `resolve_two` returns `Some` only when `ta != tb` (`discovery.rs:430-434`) == Py `(mA and mB and mA != mB)` (`colisted_map.py:138`).
- A/B ORDER: pmus YES = the long-named side. `sports_abbrevs` takes the truthy-`long` side as A else side[0] (`discovery.rs:631`) == Py `lo = next((s for s in sides if s.get("long")), sides[0])` (sample-pinned in test: PIT listed first but LAD has `long` → A=LAD). So `kalshi`=team-A ticker, `kalshi_b`=team-B — the same team pmus prices as YES.
- No path binds a wrong-date game or lets two pm games share one Kalshi event (tests `…doubleheader_used_set_binds_distinct_events`, `…undated_fallback_only_when_unique` both pass).

### 4. LEG MAPPING (`main.rs::plan_legs` + `exec.rs::build_kalshi_payload`) — FAITHFUL
`main.rs:551-595`:
- Sports PK → leg A = Buy **YES** @ (Pmus, **slug**) @ pm_ask; leg B = Buy **YES** @ (Kalshi, **kalshi_b ticker**) @ kB_ask (`main.rs:557-565`). Second leg is a YES on the AWAY team's book, NOT a NO.
- Sports KP → leg A = Buy **YES** @ (Kalshi, **kalshi_a ticker**) @ kA_ask (= `q.k.yes_ask`, the team-A book); leg B = Buy **NO** @ (Pmus, **slug**) @ `1 - pm_bid` (`main.rs:566-573`).
- Every Kalshi leg's `market` is the Kalshi TICKER, every pmus leg's `market` the slug — `build_kalshi_payload` emits `"ticker":"{intent.market}"` (`exec.rs:174`), `build_pmus_payload` emits `"slug":"{intent.market}"` (`exec.rs:194`). The Kalshi-leg-carries-the-slug bug is gone (regression test `sports_legs_use_venue_native_tickers_and_correct_sides`).
- Per-leg prices come from the BOOKS, never the edge: YES = that book's `yes_ask`; NO = `1 - that book's yes_bid` (`main.rs:554,559-560,587-591`). A missing book price → `None` → caller `continue`s (no naked leg). Test `leg_prices_come_from_books_not_edge` + `one_sided_book_blocks_leg_construction`.
- Weather/econ legs unchanged except the Kalshi leg now carries the ticker (`main.rs:576-593`).
- No path sends a leg with the wrong venue-native id, wrong side, or an edge-derived price.

### 5. `risk.rs` — `q.k_b` gets the SAME crossed/stale gates — FAITHFUL
`risk.rs:103-105` (`q.k_b.is_some_and(|kb| kb.crossed())` → `CrossedBook(Kalshi)`) and `risk.rs:112-114`
(`kb.age_s > max_book_age_s` → `StaleBook(Kalshi)`) apply the identical crossed+stale gates the team-A
book `q.k` and `q.pm` get (`risk.rs:97-111`). The away-team book is the third fill leg and is gated like
the rest. No sports path skips a safety gate weather/econ receive — the gate chain (kill/pause/settlement/
proximity/book-sanity/mid-div/edge/toxicity/caps/sizing) is category-agnostic and unconditional. Test
`rejects_crossed_or_stale_away_team_book`.

### 6. No sports path reaches the order path settlement-unverified or beyond max-days — FAITHFUL
`risk.rs:74-80`: `settle_ok = settle_clean || Weather || (Sports && assume_sports_settled) || (Econ &&
assume_econ_settled)`; with `require_settle_clean` (staged-rollout default true) a sports pair with
`settle_clean=false` and `assume_sports_settled=false` → `SettlementUnverified`. Discovery hard-codes
sports `settle_clean=false` (`discovery.rs:603`), so sports needs the explicit owner override to trade —
correct. The event-proximity gate (`risk.rs:87-91`) rejects `days_to_event > max_days_to_event`
(`TooEarly`). Tests `rejects_unverified_econ_settlement`, `settlement_assumed_per_category_then_event_proximity_governs`.

### Unwind (sports void/postpone tail) — FAITHFUL
`unwind.rs:38-51`/`55-73`: SELL each leg with its EXACT held (venue, venue-native market, side); the
sports two-YES-legs shape (PK: YES@pmus + YES@Kalshi-B) is represented in `PositionLeg`, and the Kalshi
leg carries the ticker. `should_unwind` = unwind unless reschedule confirmed inside the void window
(`None`/late → unwind). Tests green.

---

## Bottom line
A sports order **cannot** be sent mis-hedged (wrong side / edge-derived price / wrong venue-native id),
on the wrong game (exact-date bind + doubleheader used-set + distinct-ticker requirement), or with a
flipped orientation reaching the order path (C3 guard rejects a >40c same-team gap; both directions). The
away-team Kalshi book is gated identically to the others, and sports requires an explicit settlement
override + event-proximity to trade at all. Faithful port, no issues found.
