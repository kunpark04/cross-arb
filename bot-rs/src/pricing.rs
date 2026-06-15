use crate::config::Config;
use crate::pair::LivePair;
use crate::risk::Exposure;
use crate::types::*;

/// A single planned leg before pricing-to-tick: the venue, the VENUE-NATIVE market id (Kalshi ticker for
/// a Kalshi leg, pmus slug for a pmus leg — the leg-market fix), the side, and the limit price in dollars
/// read from the BOOKS (never the pair edge). `tag` ('A'/'B') makes the two client_order_ids distinct.
struct PlannedLeg {
    venue: Venue,
    market: String,
    side: Side,
    price: f64,
    tag: char,
}

/// Plan both hedge legs for `dir`, with each leg's market id VENUE-NATIVE and each limit price read from
/// the quote's BOOKS. This is the single unified builder (it replaced the old weather-only `leg_prices` +
/// `fire_pair`, which sent the pmus SLUG as the Kalshi leg's ticker — a wrong-ticker live order).
///   - weather/econ (1:1): PK = YES@pmus(slug) + NO@Kalshi(ticker); KP = YES@Kalshi(ticker) + NO@pmus(slug).
///   - sports (2-outcome): PK = YES@pmus(slug) + YES@Kalshi-B(ticker_b); KP = YES@Kalshi-A(ticker_a) + NO@pmus(slug).
///
/// Prices (monitor.py game_edge / signal): a YES leg pays that book's YES ask; a NO@pmus leg pays
/// `1 - pm_bid`; a NO@Kalshi leg pays `1 - k_bid`. Returns `None` if any leg lacks a book price (one-sided
/// book) -> the caller skips rather than fire a naked leg.
fn plan_legs(pair: &LivePair, q: &Quote, dir: Dir) -> Option<[PlannedLeg; 2]> {
    let slug = pair.slug.clone();
    let ka = pair.kalshi.clone();
    let no_at = |b: &Book| b.yes_bid.map(|bid| 1.0 - bid); // NO ask = 1 - that book's YES bid
    match (pair.kalshi_b.as_ref(), dir) {
        // ---- SPORTS ----
        (Some(kb_ticker), Dir::PK) => {
            // back A@pmus (YES@pmus slug) + B@Kalshi (YES@Kalshi-B ticker).
            let pm_ask = q.pm.yes_ask?;
            let kb_ask = q.k_b.as_ref().and_then(|b| b.yes_ask)?;
            Some([
                PlannedLeg { venue: Venue::Pmus, market: slug, side: Side::Yes, price: pm_ask, tag: 'A' },
                PlannedLeg { venue: Venue::Kalshi, market: kb_ticker.clone(), side: Side::Yes, price: kb_ask, tag: 'B' },
            ])
        }
        (Some(_), Dir::KP) => {
            // back A@Kalshi (YES@Kalshi-A ticker) + B@pmus (NO@pmus slug = 1 - pm_bid).
            let ka_ask = q.k.yes_ask?;
            let pm_no = no_at(&q.pm)?;
            Some([
                PlannedLeg { venue: Venue::Kalshi, market: ka, side: Side::Yes, price: ka_ask, tag: 'A' },
                PlannedLeg { venue: Venue::Pmus, market: slug, side: Side::No, price: pm_no, tag: 'B' },
            ])
        }
        // ---- WEATHER / ECON (1:1) ----
        (None, Dir::PK) => {
            // YES@pmus(slug) + NO@Kalshi(ticker = 1 - Kalshi YES bid).
            let pm_ask = q.pm.yes_ask?;
            let k_no = no_at(&q.k)?;
            Some([
                PlannedLeg { venue: Venue::Pmus, market: slug, side: Side::Yes, price: pm_ask, tag: 'A' },
                PlannedLeg { venue: Venue::Kalshi, market: ka, side: Side::No, price: k_no, tag: 'B' },
            ])
        }
        (None, Dir::KP) => {
            // YES@Kalshi(ticker) + NO@pmus(slug = 1 - pm_bid).
            let k_ask = q.k.yes_ask?;
            let pm_no = no_at(&q.pm)?;
            Some([
                PlannedLeg { venue: Venue::Kalshi, market: ka, side: Side::Yes, price: k_ask, tag: 'A' },
                PlannedLeg { venue: Venue::Pmus, market: slug, side: Side::No, price: pm_no, tag: 'B' },
            ])
        }
    }
}

/// Plan + price-to-tick both legs into `OrderIntent`s ready to submit. `None` if any leg can't be priced
/// (one-sided book) or rounds outside the 1..=99c venue tick range.
///
/// `pos_index` is the POSITION sequence on this slug at fire-decision time (the count of already-held legs:
/// 0 for the initial entry, 1 for the first scale-in/re-entry add, …). It is woven into each leg's
/// `client_order_id` (`xarb-{slug}-{idx}-{tag}`) so the coid is UNIQUE PER POSITION while still DETERMINISTIC
/// per (position, leg): a RETRY of the SAME fire-attempt reuses the same coid (so the venue's deterministic-
/// coid dedup still prevents a double-fill within one attempt), but an ADD onto a held slug gets a fresh idx
/// and so does NOT dedup-collide with the held position's coid (which would `409 order already exists` and —
/// post the Change-1 narrowing — HALT). `pos_index` only advances when a NEW position is actually recorded
/// (`track_position` after a both-filled lock); a non-filling retry leaves the held count, hence the index,
/// unchanged — no double-fill window. The default single-position path always passes `0`, so its coid is
/// `xarb-{slug}-0-{tag}` (the only behavioral delta from the prior `xarb-{slug}-{tag}` is the `-0-` segment;
/// the coid is still a stable, deterministic, per-leg id).
///
/// FIX C — per-market pmus constraints: a pmus leg is (1) SKIPPED (whole pair -> None) if `size` is below
/// the market's `minimumTradeQty` (a sub-min order would REJECT, leaving the OTHER leg naked — fail safe),
/// and (2) QUANTIZED to the market's `orderPriceMinTickSize` if the whole-cent price isn't already a valid
/// multiple (a coarser-than-cent tick; finer ticks like 0.001 leave whole cents unchanged). Kalshi is
/// integer-cent + whole-share, so its legs are untouched.
pub(crate) fn build_legs(pair: &LivePair, q: &Quote, dir: Dir, size: u32, pos_index: u32) -> Option<[OrderIntent; 2]> {
    let planned = plan_legs(pair, q, dir)?;
    let mut out: Vec<OrderIntent> = Vec::with_capacity(2);
    for leg in planned {
        let price = if leg.venue == Venue::Pmus {
            // a pmus order below the market's minimumTradeQty would reject -> skip the WHOLE pair (a fired
            // single leg with the other rejected is the naked-leg case this gate prevents).
            if let Some(min_qty) = pair.pm_min_qty {
                if (size as f64) < min_qty {
                    return None;
                }
            }
            // quantize UP (entries are BUYs) to the market's price tick so it stays marketable (W1).
            quantize_to_tick(leg.price, pair.pm_min_tick, Action::Buy)
        } else {
            leg.price // Kalshi: integer-cent, no per-market tick
        };
        // entries are BUYs -> ceil to the cent (limit >= touch, still crosses); W1.
        let pc = cents(Some(price), Action::Buy)?;
        out.push(OrderIntent {
            venue: leg.venue,
            market: leg.market,
            action: Action::Buy,
            side: leg.side,
            price_cents: pc,
            qty: size,
            frac_qty: None, // entries are whole-share; only a partial-fill recovery SELL sets a fractional qty
            // unique-per-position (pos_index), deterministic-per-(position,leg) so a within-fire retry reuses
            // it (dedup prevents a double-fill) but an add gets a fresh idx (no collision with the held coid).
            client_order_id: format!("xarb-{}-{}-{}", pair.slug, pos_index, leg.tag),
        });
    }
    Some([out.remove(0), out.remove(0)])
}

/// Quantize a price (dollars) to a valid multiple of `tick` (dollars) TOWARD-MARKETABLE for `action`: a
/// BUY ceils (limit >= the touch, still lifts the offer), a SELL floors (limit <= the touch, still hits the
/// bid). `None`/non-positive/non-finite tick -> the price unchanged. Used for the pmus per-market
/// `orderPriceMinTickSize` (FIX C/W1): when the tick is coarser than a cent (e.g. 0.05) a whole-cent price
/// like 0.07 snaps UP to 0.10 for a BUY (DOWN to 0.05 for a SELL); a finer tick (0.001) leaves whole cents
/// unchanged. Nearest-rounding could move a marketable order to a RESTING limit (W1) — direction-aware
/// rounding keeps it crossing. Since `OrderIntent.price_cents` is whole cents, sub-cent ticks are then
/// honored only to cent granularity (W1 FLAG: a bounded <=0.5c precision cost per pmus leg, not a safety bug).
pub(crate) fn quantize_to_tick(price: f64, tick: Option<f64>, action: Action) -> f64 {
    match tick {
        // EPS snaps a value already within ~1e-6 ticks of a boundary ONTO it before the directional
        // ceil/floor, so float noise (e.g. 0.10/0.05 = 1.9999999998) can't push an exact multiple a whole
        // tick the wrong way (the L10 cent-boundary class). 1e-6 << half a tick, so it never crosses a real one.
        Some(t) if t.is_finite() && t > 0.0 => match action {
            Action::Buy => (price / t - TICK_EPS).ceil() * t,
            Action::Sell => (price / t + TICK_EPS).floor() * t,
        },
        _ => price,
    }
}

/// Tolerance (in tick/cent multiples) for snapping a near-boundary value onto the boundary before a
/// directional ceil/floor — guards the L10 float-noise-at-a-cent-boundary class without crossing a real tick.
const TICK_EPS: f64 = 1e-6;

/// Dollars (0..1) -> a valid integer-cent venue tick price in 1..=99c, rounded TOWARD-MARKETABLE for
/// `action`: a BUY ceils to the cent (limit >= the touch so it still crosses), a SELL floors (limit <= the
/// touch). NEAREST-rounding a marketable BUY down (or a SELL up) yields a RESTING limit -> the leg rests ->
/// the sibling goes naked -> recovery/halt (W1). Kalshi touches are already whole cents so ceil/floor is a
/// no-op there; this matters for pmus sub-cent book prices. The BUY ceil is safe because
/// `realized_edge_clears_floor` re-checks the ceil'd cost against the edge floor before firing. `None` if
/// non-finite or the rounded cent is out of 1..=99.
pub(crate) fn cents(price: Option<f64>, action: Action) -> Option<u8> {
    let p = price?;
    if !p.is_finite() {
        return None;
    }
    // EPS snaps a price already within ~1e-6c of a cent onto it before the directional ceil/floor, so a
    // whole-cent touch like 0.07 (0.07*100 = 7.0000000000000001) doesn't ceil up to 8c (the L10 class).
    let cx = p * 100.0;
    let c = match action {
        Action::Buy => (cx - TICK_EPS).ceil(),
        Action::Sell => (cx + TICK_EPS).floor(),
    };
    if (1.0..=99.0).contains(&c) {
        Some(c as u8)
    } else {
        None
    }
}

/// Contracts the bankroll can fund at this pair price — the `affordable` arg to `risk::evaluate`. Derived
/// from the REMAINING total-notional room (W4: subtract already-open `exposure.total`, matching the
/// docstring "the bankroll can fund" — the raw cap ignored open positions and double-counted once any
/// position was open; only the gate's `tot_cap` happened to subtract it). The gate still applies all the
/// finer caps; `cost_per` mirrors the gate's reconstruction (`(1-edge.net).max(0.1)`).
pub(crate) fn affordable(cfg: &Config, edge: &Edge, exposure: &Exposure) -> u32 {
    let cost_per = (1.0 - edge.net).max(0.1);
    let room = (cfg.max_total_notional - exposure.total).max(0.0);
    (room / cost_per).floor().max(0.0) as u32
}

/// W6: recompute the REALIZED net edge from the two ROUNDED leg prices and confirm it still clears the
/// edge floor (and the pair costs < 100c). `build_legs` rounds each leg's price to a whole cent
/// independently, so the paid cost (`a_cents + b_cents`) can drift up to +1c above the cost implied by the
/// gated `edge.net`; without this the booked edge can be materially thinner than the gated edge. The fee
/// model mirrors `signal`/`game_signal` exactly: each leg pays its venue's at-scale MARGINAL taker fee at
/// that leg's PAID price (the same per-leg `venue_fee(leg_price)` those functions sum), so this is the
/// honest "size and price are consistent" check the gate splits across two functions.
pub(crate) fn realized_edge_clears_floor(cfg: &Config, legs: &[OrderIntent; 2]) -> bool {
    let cost = (legs[0].price_cents as f64 + legs[1].price_cents as f64) / 100.0;
    if cost >= 1.0 {
        return false; // a >= $1 pair pays more than the $1 payout — never book it
    }
    realized_net_dollars(legs) * 100.0 >= cfg.edge_floor_cents
}

/// REALIZED net edge (dollars) from the two ROUNDED leg prices, after each leg's at-scale MARGINAL taker fee
/// at its paid price — the shared core of `realized_edge_clears_floor` (≥ floor?) and the second-leg surplus.
/// The fee model mirrors `signal`/`game_signal` exactly (the same per-leg `venue_fee(leg_price)` they sum).
fn realized_net_dollars(legs: &[OrderIntent; 2]) -> f64 {
    use crate::ledger::{marginal_taker_fee, KALSHI_TAKER_COEF, PMUS_TAKER_COEF};
    let leg_fee = |leg: &OrderIntent| {
        let p = leg.price_cents as f64 / 100.0;
        if !(0.0 < p && p < 1.0) {
            return 0.0;
        }
        let coef = match leg.venue {
            Venue::Kalshi => KALSHI_TAKER_COEF,
            Venue::Pmus => PMUS_TAKER_COEF,
        };
        marginal_taker_fee(coef, p)
    };
    let cost = (legs[0].price_cents as f64 + legs[1].price_cents as f64) / 100.0;
    round4((1.0 - cost) - leg_fee(&legs[0]) - leg_fee(&legs[1]))
}

/// Sanity ceiling (cents) on the capped-aggressive second-leg pay-up, on TOP of the per-arb edge surplus —
/// so even a fat-edge (possibly phantom) arb can't post a wildly aggressive limit.
const SECOND_LEG_MARKUP_CAP_CENTS: u8 = 5;

/// Whole-cent surplus of the realized net edge ABOVE the floor, clamped to [0, cap] — the budget the
/// second-leg pay-up may spend. 0 for a thin arb (net ≈ floor) → no pay-up (it falls back to the cheap
/// recovery); positive for a fat arb.
fn realized_surplus_cents(cfg: &Config, legs: &[OrderIntent; 2]) -> u8 {
    let s = realized_net_dollars(legs) * 100.0 - cfg.edge_floor_cents;
    s.floor().clamp(0.0, SECOND_LEG_MARKUP_CAP_CENTS as f64) as u8
}

/// CAPPED-AGGRESSIVE SECOND LEG (decision 0020 follow-up; live-validated need 2026-06-15). Under pmus-first
/// the Kalshi leg fires SECOND — after the pmus block — and a passive marketable limit then MISSES when the
/// price ticked during the wait (most edges are sub-second; the re-test's Kalshi leg missed exactly this
/// way). Raise the Kalshi BUY limit by the realized edge SURPLUS above the floor so it still fills through a
/// small adverse move, WITHOUT ever eating below the floor: a thin arb (net ≈ floor) gets ~0 markup and
/// falls back to the cheap recovery; a fat arb spends its surplus to GUARANTEE the lock. The order still
/// FILLS at the live price (≤ the raised limit), so we pay only the ACTUAL move, bounded by the surplus.
/// Re-checks the floor after the bump (fees shift at the new price) and REVERTS if it would not clear.
/// Returns the markup (cents) actually applied to the Kalshi leg — 0 if none (thin arb / no Kalshi leg /
/// reverted) — so the live path can LOG the pay-up per fire (instrumentation: is the pay-up helping?).
pub(crate) fn apply_second_leg_markup(cfg: &Config, legs: &mut [OrderIntent; 2]) -> u8 {
    // the SECOND-fired leg is the Kalshi one (pmus-first fires pmus, then Kalshi). Mark up that BUY only.
    let Some(i) = legs.iter().position(|l| l.venue == Venue::Kalshi) else {
        return 0; // no Kalshi leg (never for a cross-venue arb) -> nothing to do
    };
    let markup = realized_surplus_cents(cfg, legs);
    if markup == 0 {
        return 0; // thin arb: no surplus -> stay passive, rely on the cheap recovery
    }
    let bumped = legs[i].price_cents.saturating_add(markup);
    if bumped >= 100 {
        return 0; // never post a >= 100c leg
    }
    let prev = legs[i].price_cents;
    legs[i].price_cents = bumped;
    if !realized_edge_clears_floor(cfg, legs) {
        // DEFENSIVE: the surplus is derived to keep the floor, but a fee shift at the bumped price could
        // nudge the realized net under it -> revert. The pay-up never drops realized edge below the floor.
        legs[i].price_cents = prev;
        return 0;
    }
    markup
}

/// 4dp round, matching `signal::round4` / ledger.py — keeps the realized-edge re-check bit-consistent with
/// the edge the gate approved.
fn round4(x: f64) -> f64 {
    (x * 1.0e4).round() / 1.0e4
}

/// The HELD position the live loop derives from a both-filled entry: build a `Position` straight from the
/// two entry `OrderIntent`s (each leg = its venue/market/side; the pair `market` = the pmus slug). The two
/// intents are the exact legs we now own, so the unwind SELLs back the same (venue, market, side). `pm_tick`
/// (the pair's pmus `orderPriceMinTickSize`) is recorded ONLY on the pmus leg (FIX W2) so a later recovery
/// SELL can floor-quantize its flatten price to a valid tick; Kalshi legs carry `None`.
pub(crate) fn position_from_intents(slug: &str, cat: Cat, cluster: &str, pm_tick: Option<f64>, legs: &[OrderIntent; 2]) -> Position {
    Position {
        market: slug.to_string(),
        cat,
        legs: std::array::from_fn(|i| PositionLeg {
            venue: legs[i].venue,
            market: legs[i].market.clone(),
            side: legs[i].side,
            venue_order_id: String::new(), // filled from the fill ack in `apply_outcome` before tracking
            pm_min_tick: if legs[i].venue == Venue::Pmus { pm_tick } else { None },
        }),
        size: legs[0].qty,
        cluster: cluster.to_string(),
    }
}

/// Price ONE held leg's marketable EXIT (a SELL) from the live book of the venue it sits on: a SELL YES
/// leg lifts that book's best `yes_bid`; a SELL NO leg unwinds at `1 - yes_ask` (selling NO = buying YES
/// back, which pays the YES ask -> the NO sale nets `1 - yes_ask`). `None` when the needed side isn't
/// quoted (a one-sided book) -> the caller leaves the position and the poll re-emits.
pub(crate) fn exit_price(leg: &PositionLeg, book: &Book) -> Option<f64> {
    match leg.side {
        Side::Yes => book.yes_bid,
        Side::No => book.yes_ask.map(|a| 1.0 - a),
    }
}

/// Price a held leg's RECOVERY-flatten SELL to a valid integer-cent tick (W1/W2): take the marketable exit
/// from `book`, FLOOR-quantize it to this leg's pmus `pm_min_tick` (a SELL floors — limit <= touch stays
/// marketable; W2), then FLOOR to the cent. `None` when the leg can't be priced (one-sided book) OR the
/// floor lands outside 1..=99c -> the caller halts rather than fire a rejecting/mispriced flatten. A
/// coarse-tick pmus market would otherwise reject a whole-cent SELL and bounce the recovery to the halt.
pub(crate) fn flatten_exit_cents(leg: &PositionLeg, book: &Book) -> Option<u8> {
    let p = quantize_to_tick(exit_price(leg, book)?, leg.pm_min_tick, Action::Sell);
    cents(Some(p), Action::Sell)
}

/// Price BOTH legs of a held position to exit `cents`, reading each leg's book from `book_of` (the live
/// per-venue books). `None` if EITHER leg can't be priced or rounds outside the venue tick — the caller
/// then logs a WARN and holds (the poll re-emits; the idempotent `unwind-…` coids prevent a double-flatten).
pub(crate) fn unwind_exit_cents<F>(pos: &Position, book_of: F) -> Option<[u8; 2]>
where
    F: Fn(&PositionLeg) -> Option<Book>,
{
    let mut out = [0u8; 2];
    for (i, leg) in pos.legs.iter().enumerate() {
        let book = book_of(leg)?;
        // route through flatten_exit_cents so the postpone-unwind SELL gets the SAME pmus tick FLOOR (W2)
        // the recovery SELL has — a coarse-tick pmus market would otherwise reject a whole-cent unwind.
        out[i] = flatten_exit_cents(leg, &book)?;
    }
    Some(out)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::test_support::*;
    use crate::unwind;

    /// REGRESSION GUARD for the self-review CRITICAL: leg prices come from the BOOKS (YES = cheap
    /// venue's YES ask; NO = 1 - dear venue's YES bid), NOT from the pair edge. The buggy version set
    /// YES = (1-edge) ~= 0.97 and NO ~= edge, so the NO leg's limit sat far below its real ask and could
    /// never fill -> a naked YES leg. Here YES must be 7c and NO must be 90c (sum = pair cost = 1-edge).
    /// ALSO pins the leg-market FIX: the Kalshi leg carries the Kalshi TICKER, never the pmus slug.
    #[test]
    fn leg_prices_come_from_books_not_edge() {
        let (pair, q) = (wx_pair(), q_pk());
        let pk = build_legs(&pair, &q, Dir::PK, 1, 0).unwrap();
        // leg A = YES@pmus(slug) @ 7c (pmus YES ask 0.07); leg B = NO@Kalshi(ticker) @ 90c (1 - 0.10).
        assert_eq!((pk[0].venue, pk[0].side, pk[0].price_cents), (Venue::Pmus, Side::Yes, 7));
        assert_eq!(pk[0].market, "tc-temp-nychigh-2026-06-11-gte95f"); // pmus leg -> slug
        assert_eq!((pk[1].venue, pk[1].side, pk[1].price_cents), (Venue::Kalshi, Side::No, 90));
        assert_eq!(pk[1].market, "KXHIGHNY-26JUN11-T95"); // LEG-MARKET FIX: Kalshi leg -> the TICKER, not the slug
        // KP flips cheap/dear: YES@Kalshi(ticker) 0.11; NO@pmus(slug) = 1 - 0.05 = 0.95.
        let kp = build_legs(&pair, &q, Dir::KP, 1, 0).unwrap();
        assert_eq!((kp[0].venue, kp[0].side, kp[0].price_cents), (Venue::Kalshi, Side::Yes, 11));
        assert_eq!(kp[0].market, "KXHIGHNY-26JUN11-T95");
        assert_eq!((kp[1].venue, kp[1].side, kp[1].price_cents), (Venue::Pmus, Side::No, 95));
        assert_eq!(kp[1].market, "tc-temp-nychigh-2026-06-11-gte95f");
    }

    /// UNIQUE-PER-POSITION, STABLE-PER-FIRE-RETRY COID (the scale-in/re-entry dedup fix). `pos_index` makes
    /// the entry coid `xarb-{slug}-{idx}-{tag}`: the INITIAL position (idx 0) and the 1st ADD (idx 1) get
    /// DISTINCT coids (so the add never `409 order already exists`-collides with the held position on Kalshi's
    /// deterministic-coid dedup), while a RE-FIRE at the SAME index reuses the SAME coid (so a within-fire
    /// retry still dedups -> no double-fill). The leg tag ('A'/'B') keeps the two legs of one position distinct.
    #[test]
    fn entry_coid_is_unique_per_position_and_stable_per_index() {
        let (pair, q) = (wx_pair(), q_pk());
        let initial = build_legs(&pair, &q, Dir::PK, 1, 0).unwrap();
        let first_add = build_legs(&pair, &q, Dir::PK, 1, 1).unwrap();
        // the default single-position path is `xarb-{slug}-0-{tag}` (only delta from the old `-{tag}` is `-0-`).
        assert_eq!(initial[0].client_order_id, format!("xarb-{}-0-A", pair.slug));
        assert_eq!(initial[1].client_order_id, format!("xarb-{}-0-B", pair.slug));
        // the 1st ADD (idx 1) gets DISTINCT coids per leg -> no dedup collision with the held idx-0 position.
        assert_eq!(first_add[0].client_order_id, format!("xarb-{}-1-A", pair.slug));
        assert_ne!(initial[0].client_order_id, first_add[0].client_order_id, "initial vs add: DISTINCT coids");
        assert_ne!(initial[1].client_order_id, first_add[1].client_order_id, "both legs differ across positions");
        // a RE-FIRE at the SAME index (a within-fire retry that didn't lock -> the held count is unchanged)
        // reuses the EXACT coid, so the venue's deterministic-coid dedup prevents a double-fill within one add.
        let refire = build_legs(&pair, &q, Dir::PK, 1, 1).unwrap();
        assert_eq!(refire[0].client_order_id, first_add[0].client_order_id, "same index -> same coid (idempotent retry)");
        assert_eq!(refire[1].client_order_id, first_add[1].client_order_id);
    }

    /// ROUTING (the money-path self-review item a): a WORLD-CUP outcome pair MUST take the BINARY `signal`
    /// arm, NEVER `game_signal`. The live loop routes on `kalshi_b.is_some()` (Some -> game_signal; None ->
    /// signal), so a WC pair (kalshi_b=None) is structurally guaranteed the binary arm — assert that, AND
    /// prove it via the LEG SHAPE: `build_legs` on a WC pair produces the 1:1 binary shape (YES@pmus(slug) +
    /// NO@Kalshi(ticker) for PK; YES@Kalshi + NO@pmus for KP), which ONLY the `(None, dir)` arms of plan_legs
    /// emit. A `game_signal`/2-team pair would instead make a YES@Kalshi-B leg — the mis-hedge this prevents.
    #[test]
    fn world_cup_pair_routes_through_binary_signal_not_game_signal() {
        let pair = wc_pair();
        // the exact routing predicate the live loop uses (main::run_live): None -> binary `signal` arm.
        assert!(pair.kalshi_b.is_none(), "a WC pair has no kalshi_b -> the loop takes the binary signal arm");
        assert!(pair.soccer, "and it is flagged soccer (regulation-settlement basis)");
        // dir PK: pmus YES ask 0.42 (back 'Germany wins' cheap on pmus) + Kalshi NO = 1 - Kalshi YES bid 0.45.
        let q = Quote {
            market: pair.slug.clone(),
            cat: Cat::Sports,
            pm: Book { yes_bid: Some(0.40), yes_ask: Some(0.42), age_s: 0.0 },
            k: Book { yes_bid: Some(0.45), yes_ask: Some(0.46), age_s: 0.0 },
            k_b: None, // BINARY: there is NO away-team book — the structural proof WC isn't the 2-team model
            depth: Depth { c2: 50, c1: 50, c0: 50 },
            settle_clean: true,
            cluster: pair.cluster.clone(),
            led_by: None,
            days_to_event: Some(1.0),
        };
        // PK legs = YES@pmus(slug) @ 42c + NO@Kalshi(ticker) @ (1-0.45)=55c — the weather/econ 1:1 shape.
        let pk = build_legs(&pair, &q, Dir::PK, 1, 0).unwrap();
        assert_eq!((pk[0].venue, pk[0].side, pk[0].price_cents), (Venue::Pmus, Side::Yes, 42));
        assert_eq!(pk[0].market, "atc-fwc-ger-cuw-2026-06-14-ger", "the pmus leg uses the outcome SLUG");
        assert_eq!((pk[1].venue, pk[1].side, pk[1].price_cents), (Venue::Kalshi, Side::No, 55));
        assert_eq!(pk[1].market, "KXWCGAME-26JUN14GERCUW-GER", "the Kalshi leg uses the outcome TICKER (binary), not a team-B ticker");
        // the per-outcome legs LOCK (self-review item b): YES on the cheap venue + NO on the dear venue, on
        // the SAME outcome (same slug/ticker pair) — so it pays $1 whichever way THIS outcome resolves.
        assert!(pk[0].side == Side::Yes && pk[1].side == Side::No, "YES@one venue + NO@other on the same outcome");
        // KP flips: YES@Kalshi(ticker) 46c + NO@pmus(slug) = 1 - 0.40 = 60c. Still the 1:1 binary shape.
        let kp = build_legs(&pair, &q, Dir::KP, 1, 0).unwrap();
        assert_eq!((kp[0].venue, kp[0].side, kp[0].market.as_str()), (Venue::Kalshi, Side::Yes, "KXWCGAME-26JUN14GERCUW-GER"));
        assert_eq!((kp[1].venue, kp[1].side, kp[1].market.as_str()), (Venue::Pmus, Side::No, "atc-fwc-ger-cuw-2026-06-14-ger"));
    }

    /// SPORTS leg construction: PK = "YES@pmus(slug) and YES@Kalshi-B(ticker_b)"; KP = "YES@Kalshi-A
    /// (ticker_a) and NO@pmus(slug)". Both Kalshi legs carry their own TICKER (the leg-market fix), and
    /// the sports PK second leg is a YES on the AWAY team's book (kb ask), not a NO leg.
    #[test]
    fn sports_legs_use_venue_native_tickers_and_correct_sides() {
        let pair = LivePair {
            slug: "aec-mlb-lad-pit-2026-06-16".into(),
            kalshi: "KXMLBGAME-26JUN16-LAD".into(),
            kalshi_b: Some("KXMLBGAME-26JUN16-PIT".into()),
            cat: Cat::Sports,
            cluster: "mlb-2026-06-16".into(),
            settle_clean: false,
            soccer: false,
            days_to_event: Some(1.0),
            pm_min_tick: None,
            pm_min_qty: None,
        };
        let q = Quote {
            market: pair.slug.clone(),
            cat: Cat::Sports,
            pm: Book { yes_bid: Some(0.54), yes_ask: Some(0.55), age_s: 0.0 }, // back A@pmus pays 0.55
            k: Book { yes_bid: Some(0.56), yes_ask: Some(0.58), age_s: 0.0 },  // Kalshi-A (LAD) ask 0.58
            k_b: Some(Book { yes_bid: Some(0.40), yes_ask: Some(0.42), age_s: 0.0 }), // Kalshi-B (PIT) ask 0.42
            depth: Depth { c2: 50, c1: 50, c0: 50 },
            settle_clean: false,
            cluster: "mlb-2026-06-16".into(),
            led_by: None,
            days_to_event: Some(1.0),
        };
        // PK: leg A = YES@pmus(slug) @ pm_ask 55c; leg B = YES@Kalshi-B(PIT ticker) @ kB_ask 42c.
        let pk = build_legs(&pair, &q, Dir::PK, 3, 0).unwrap();
        assert_eq!((pk[0].venue, pk[0].side, pk[0].price_cents), (Venue::Pmus, Side::Yes, 55));
        assert_eq!(pk[0].market, "aec-mlb-lad-pit-2026-06-16");
        assert_eq!((pk[1].venue, pk[1].side, pk[1].price_cents), (Venue::Kalshi, Side::Yes, 42));
        assert_eq!(pk[1].market, "KXMLBGAME-26JUN16-PIT", "PK leg2 = YES on the AWAY team's Kalshi TICKER");
        assert!(pk[0].qty == 3 && pk[1].qty == 3);
        // KP: leg A = YES@Kalshi-A(LAD ticker) @ kA_ask 58c; leg B = NO@pmus(slug) @ 1-pm_bid = 46c.
        let kp = build_legs(&pair, &q, Dir::KP, 3, 0).unwrap();
        assert_eq!((kp[0].venue, kp[0].side, kp[0].price_cents), (Venue::Kalshi, Side::Yes, 58));
        assert_eq!(kp[0].market, "KXMLBGAME-26JUN16-LAD", "KP leg1 = YES on the HOME team's Kalshi TICKER");
        assert_eq!((kp[1].venue, kp[1].side, kp[1].price_cents), (Venue::Pmus, Side::No, 46));
        assert_eq!(kp[1].market, "aec-mlb-lad-pit-2026-06-16");
    }

    /// `cents` rounds TOWARD-MARKETABLE (W1): a BUY ceils to the cent (limit >= touch -> still crosses), a
    /// SELL floors (limit <= touch). It rejects prices that round outside 1..=99 or are non-finite/absent.
    /// The whole-cent Kalshi-touch cases (exact multiples) are unchanged by direction.
    #[test]
    fn cents_rounds_toward_marketable() {
        // W1 core: a BUY at a sub-cent book price ceils UP (0.074 -> 8c); a SELL floors DOWN (0.076 -> 7c)
        // so each stays marketable. Nearest-rounding would have rested the BUY at 7c / the SELL at 8c.
        assert_eq!(cents(Some(0.074), Action::Buy), Some(8)); // BUY ceils 7.4c -> 8c (still lifts the offer)
        assert_eq!(cents(Some(0.076), Action::Sell), Some(7)); // SELL floors 7.6c -> 7c (still hits the bid)
        // whole-cent (Kalshi) touches are exact multiples -> direction is a no-op.
        assert_eq!(cents(Some(0.07), Action::Buy), Some(7));
        assert_eq!(cents(Some(0.07), Action::Sell), Some(7));
        assert_eq!(cents(Some(0.90), Action::Buy), Some(90));
        // a BUY at <0.5c still ceils to the 1c floor tick; a SELL at <1c floors to 0c -> rejected.
        assert_eq!(cents(Some(0.004), Action::Buy), Some(1)); // BUY ceils up to the 1c floor tick
        assert_eq!(cents(Some(0.004), Action::Sell), None); // SELL floors to 0c -> below the valid range
        assert_eq!(cents(Some(0.0), Action::Buy), None); // free -> not a tradeable tick
        assert_eq!(cents(Some(0.995), Action::Buy), None); // BUY ceils 99.5c -> 100c -> out of range
        assert_eq!(cents(Some(0.995), Action::Sell), Some(99)); // SELL floors 99.5c -> 99c -> valid
        assert_eq!(cents(Some(1.0), Action::Buy), None); // 100c -> out of range
        assert_eq!(cents(None, Action::Buy), None);
        assert_eq!(cents(Some(f64::NAN), Action::Buy), None);
    }

    /// FIX C/W1 — per-market pmus tick + min-size in the leg builder. (1) a configured size BELOW the pmus
    /// `minimumTradeQty` skips the WHOLE pair (sub-min would reject -> naked leg). (2) a coarse pmus price
    /// tick quantizes the pmus leg's BUY price UP to a valid multiple (W1 toward-marketable); a fine tick
    /// (0.001) is a no-op. Kalshi legs are never quantized. `quantize_to_tick` direction is checked directly.
    #[test]
    fn pmus_min_qty_skips_and_tick_quantizes_the_pmus_leg() {
        // quantize_to_tick toward-marketable: a BUY at 0.07 on a 0.05 tick ceils UP to 0.10; a SELL floors to
        // 0.05. A finer tick (0.001) and None/0 ticks leave the price unchanged either direction.
        assert!((quantize_to_tick(0.07, Some(0.05), Action::Buy) - 0.10).abs() < 1e-9); // BUY ceils up
        assert!((quantize_to_tick(0.07, Some(0.05), Action::Sell) - 0.05).abs() < 1e-9); // SELL floors down
        assert!((quantize_to_tick(0.10, Some(0.05), Action::Buy) - 0.10).abs() < 1e-9); // exact multiple: no-op
        assert!((quantize_to_tick(0.07, Some(0.001), Action::Buy) - 0.07).abs() < 1e-9); // finer tick: whole cent unchanged
        assert!((quantize_to_tick(0.07, None, Action::Buy) - 0.07).abs() < 1e-9); // no tick known -> unchanged
        assert!((quantize_to_tick(0.07, Some(0.0), Action::Sell) - 0.07).abs() < 1e-9); // non-positive tick ignored

        // (1) min-qty skip: pmus minimumTradeQty = 2, configured size 1 -> the pmus leg is sub-min -> None.
        let mut pair = wx_pair();
        pair.pm_min_qty = Some(2.0);
        assert!(build_legs(&pair, &q_pk(), Dir::PK, 1, 0).is_none(), "size below pmus minimumTradeQty -> skip the pair");
        assert!(build_legs(&pair, &q_pk(), Dir::PK, 2, 0).is_some(), "size at the minimum is allowed");

        // (2) tick quantization: pmus tick 0.05; dir PK leg A = YES@pmus (a BUY) @ pm_ask 0.07 -> ceils UP to
        // 0.10 = 10c (stays marketable; nearest-rounding to 5c would have rested it below the 7c offer).
        let mut pair2 = wx_pair();
        pair2.pm_min_tick = Some(0.05);
        let pk = build_legs(&pair2, &q_pk(), Dir::PK, 1, 0).unwrap();
        assert_eq!((pk[0].venue, pk[0].price_cents), (Venue::Pmus, 10), "pmus BUY leg ceils UP to the 0.05 tick (marketable)");
        // the Kalshi NO leg (1 - 0.10 = 0.90) is NOT quantized by the pmus tick -> stays 90c.
        assert_eq!((pk[1].venue, pk[1].price_cents), (Venue::Kalshi, 90), "Kalshi leg is integer-cent, untouched");
    }

    /// A one-sided book (no dear-venue YES bid) yields no NO-leg price -> `build_legs` returns None and
    /// the live loop skips (no naked fire). Pins the "skip rather than leg out" invariant.
    #[test]
    fn one_sided_book_blocks_leg_construction() {
        let (pair, mut q) = (wx_pair(), q_pk());
        q.k.yes_bid = None; // no Kalshi bid -> can't price the NO@Kalshi leg (dir PK)
        assert!(build_legs(&pair, &q, Dir::PK, 1, 0).is_none()); // -> the live loop `continue`s, no naked leg
        // the OTHER direction (KP needs Kalshi YES ask + pmus YES bid) still prices -> two legs.
        assert!(build_legs(&pair, &q, Dir::KP, 1, 0).is_some());
    }

    /// `position_from_intents` builds the held Position straight from the two entry OrderIntents: each leg
    /// = that intent's venue/market/side, the pair `market` = the slug, size = the intent qty. The unwind
    /// then SELLs back the exact (venue, market, side) — so this round-trips a sports PK fill (YES@pmus +
    /// YES@Kalshi-B) into a Position whose two legs are both YES, on the right venues/tickers.
    #[test]
    fn position_from_intents_records_exact_legs() {
        let legs = [
            OrderIntent { venue: Venue::Pmus, market: "aec-mlb-lad-pit-2026-06-16".into(), action: Action::Buy, side: Side::Yes, price_cents: 55, qty: 7, frac_qty: None, client_order_id: "xarb-…-A".into() },
            OrderIntent { venue: Venue::Kalshi, market: "KXMLBGAME-26JUN16-PIT".into(), action: Action::Buy, side: Side::Yes, price_cents: 42, qty: 7, frac_qty: None, client_order_id: "xarb-…-B".into() },
        ];
        let pos = position_from_intents("aec-mlb-lad-pit-2026-06-16", Cat::Sports, "mlb-2026-06-16", None, &legs);
        assert_eq!(pos.market, "aec-mlb-lad-pit-2026-06-16"); // pair identity = the pmus slug
        assert_eq!(pos.size, 7);
        assert_eq!(pos.cluster, "mlb-2026-06-16");
        assert_eq!(pos.legs[0], PositionLeg { venue: Venue::Pmus, market: "aec-mlb-lad-pit-2026-06-16".into(), side: Side::Yes, ..Default::default() });
        assert_eq!(pos.legs[1], PositionLeg { venue: Venue::Kalshi, market: "KXMLBGAME-26JUN16-PIT".into(), side: Side::Yes, ..Default::default() });
        // the unwind SELLs back the EXACT legs (venue/market/side), priced at the supplied exit cents.
        let u = unwind::unwind_orders(&pos, [98, 55]);
        assert_eq!((u[0].action, u[0].venue, u[0].side, u[0].market.as_str()), (Action::Sell, Venue::Pmus, Side::Yes, "aec-mlb-lad-pit-2026-06-16"));
        assert_eq!((u[1].action, u[1].venue, u[1].side, u[1].market.as_str()), (Action::Sell, Venue::Kalshi, Side::Yes, "KXMLBGAME-26JUN16-PIT"));
    }

    /// The exit-pricing helper: a SELL YES leg lifts that book's best `yes_bid`; a SELL NO leg nets
    /// `1 - yes_ask` (selling NO = buying YES back at the ask). A missing needed side -> None (one-sided
    /// book) so `unwind_exit_cents` declines to price the pair and the caller holds.
    #[test]
    fn exit_pricing_yes_takes_bid_no_takes_one_minus_ask() {
        let yes_leg = PositionLeg { venue: Venue::Pmus, market: "s".into(), side: Side::Yes, ..Default::default() };
        let no_leg = PositionLeg { venue: Venue::Kalshi, market: "K".into(), side: Side::No, ..Default::default() };
        let book = Book { yes_bid: Some(0.98), yes_ask: Some(0.99), age_s: 0.0 };
        assert_eq!(exit_price(&yes_leg, &book), Some(0.98)); // SELL YES -> hit the YES bid
        assert_eq!(exit_price(&no_leg, &book), Some(1.0 - 0.99)); // SELL NO -> 1 - YES ask = 0.01
        // a YES leg with no bid -> None; a NO leg with no ask -> None (one-sided book).
        assert_eq!(exit_price(&yes_leg, &Book { yes_bid: None, yes_ask: Some(0.99), age_s: 0.0 }), None);
        assert_eq!(exit_price(&no_leg, &Book { yes_bid: Some(0.98), yes_ask: None, age_s: 0.0 }), None);

        // unwind_exit_cents prices BOTH legs (here a YES@pmus + NO@Kalshi weather pair) from their books.
        let pos = Position {
            market: "tc-temp-nychigh-2026-06-11-gte95f".into(), cat: Cat::Weather,
            legs: [yes_leg.clone(), no_leg.clone()], size: 1, cluster: "c".into(),
        };
        // YES@pmus bid 0.98 -> 98c; NO@Kalshi 1 - ask(0.11) = 0.89 -> 89c.
        let pm = Book { yes_bid: Some(0.98), yes_ask: Some(0.99), age_s: 0.0 };
        let k = Book { yes_bid: Some(0.10), yes_ask: Some(0.11), age_s: 0.0 };
        let cents = unwind_exit_cents(&pos, |leg| if leg.venue == Venue::Pmus { Some(pm) } else { Some(k) }).unwrap();
        assert_eq!(cents, [98, 89]);
        // one leg's book missing the needed side -> the whole pair declines to price (hold).
        assert!(unwind_exit_cents(&pos, |leg| if leg.venue == Venue::Pmus { Some(Book { yes_bid: None, yes_ask: Some(0.99), age_s: 0.0 }) } else { Some(k) }).is_none());
    }

    /// W6: when rounding each leg to a whole cent pushes the realized net under the floor, the fire is
    /// SKIPPED. A pair whose two book legs round to 49c + 49c = 98c gross 2c, minus marginal fees, nets
    /// under the 2c floor -> rejected; a clearly-fat pair (cheap legs) clears it.
    #[test]
    fn realized_edge_recheck_skips_a_rounded_under_floor_pair() {
        let cfg = crate::config::Config::test_default(); // edge_floor_cents = 2.0
        let leg = |v: Venue, c: u8| OrderIntent { venue: v, market: "m".into(), action: Action::Buy, side: Side::Yes, price_cents: c, qty: 1, frac_qty: None, client_order_id: "x".into() };
        // 49 + 49 = 98c -> gross 2c, but marginal taker fees on both legs eat it below the 2c floor -> SKIP.
        assert!(!realized_edge_clears_floor(&cfg, &[leg(Venue::Pmus, 49), leg(Venue::Kalshi, 49)]));
        // 5 + 90 = 95c -> gross 5c, fees on the cheap+dear legs leave well over 2c -> FIRE.
        assert!(realized_edge_clears_floor(&cfg, &[leg(Venue::Pmus, 5), leg(Venue::Kalshi, 90)]));
        // a >= $1 pair (51 + 50 = 101c) can never be booked -> SKIP.
        assert!(!realized_edge_clears_floor(&cfg, &[leg(Venue::Pmus, 51), leg(Venue::Kalshi, 50)]));
    }

    /// 0020 follow-up — CAPPED-AGGRESSIVE second leg: a FAT arb pays up on the KALSHI (second-fired) leg,
    /// capped at the 5c sanity cap, never the pmus leg, and the bumped pair STILL clears the floor; with no
    /// surplus above the floor there is NO pay-up (the arb falls back to the cheap recovery).
    #[test]
    fn second_leg_markup_pays_up_on_kalshi_capped_and_floor_safe() {
        let leg = |v: Venue, c: u8| OrderIntent { venue: v, market: "m".into(), action: Action::Buy, side: Side::Yes, price_cents: c, qty: 1, frac_qty: None, client_order_id: "x".into() };
        let mut cfg = crate::config::Config::test_default(); // edge_floor_cents = 2.0

        // FAT arb (30 + 30 = 60c, ~40c gross) -> surplus far exceeds the 5c cap -> Kalshi pays up by 5c.
        let mut fat = [leg(Venue::Pmus, 30), leg(Venue::Kalshi, 30)];
        assert_eq!(apply_second_leg_markup(&cfg, &mut fat), 5, "returns the 5c markup actually applied");
        assert_eq!(fat[0].price_cents, 30, "the pmus (first) leg is NEVER marked up");
        assert_eq!(fat[1].price_cents, 35, "the Kalshi (second) leg pays up by the 5c cap");
        assert!(realized_edge_clears_floor(&cfg, &fat), "the bumped pair still clears the floor");

        // NO surplus above the floor (raise it so the same arb has no room) -> NO pay-up.
        cfg.edge_floor_cents = 50.0;
        let mut none = [leg(Venue::Pmus, 30), leg(Venue::Kalshi, 30)];
        assert_eq!(apply_second_leg_markup(&cfg, &mut none), 0, "no surplus -> returns 0 markup");
        assert_eq!(none[1].price_cents, 30, "no surplus above the floor -> no pay-up (cheap recovery instead)");
    }

    /// W4: `affordable` subtracts already-open `exposure.total` from the total-notional cap (the bankroll
    /// truly fundable), not the raw cap. With half the cap deployed, affordable halves.
    #[test]
    fn affordable_subtracts_open_exposure() {
        let cfg = crate::config::Config::test_default(); // max_total_notional = 1000
        let e = Edge { net: 0.0, dir: Dir::PK }; // cost_per = (1-0).max(0.1) = 1.0 -> affordable == room
        let mut exp = Exposure::new();
        assert_eq!(affordable(&cfg, &e, &exp), 1000); // nothing open -> full room
        exp.total = 600.0;
        assert_eq!(affordable(&cfg, &e, &exp), 400); // 600 deployed -> 400 room (raw cap would say 1000)
        exp.total = 1200.0; // over-deployed -> clamps at 0, never negative
        assert_eq!(affordable(&cfg, &e, &exp), 0);
    }

    /// FIX W2 — the recovery-flatten SELL FLOOR-quantizes to the held pmus leg's coarse `orderPriceMinTickSize`
    /// so a coarse-tick pmus market doesn't REJECT it (which would bounce the recovery to a needless halt). A
    /// SELL floors (limit <= touch -> still hits the bid). Directly on the pure pricer + end-to-end on the
    /// recovery path: a YES@pmus leg with a 0.05 tick, book YES bid 0.93 -> floors to 0.90 = 90c (not 93c).
    #[test]
    fn flatten_sell_floors_to_the_pmus_tick() {
        // a pmus YES leg carrying a coarse 0.05 tick; book best YES bid 0.93 (not a 0.05 multiple).
        let coarse = PositionLeg { venue: Venue::Pmus, market: "tc-temp-nychigh-2026-06-11-gte95f".into(), side: Side::Yes, pm_min_tick: Some(0.05), ..Default::default() };
        let book = Book { yes_bid: Some(0.93), yes_ask: Some(0.95), age_s: 0.0 };
        // SELL floors 0.93 -> the 0.05 tick 0.90 -> 90c (a 93c SELL would reject on a 0.05-tick market).
        assert_eq!(flatten_exit_cents(&coarse, &book), Some(90), "the recovery SELL floors to a valid coarse tick");
        // a NO@pmus leg on the same tick: exit = 1 - yes_ask(0.95) = 0.05 -> floors to the 0.05 tick = 5c.
        let coarse_no = PositionLeg { side: Side::No, ..coarse.clone() };
        assert_eq!(flatten_exit_cents(&coarse_no, &book), Some(5), "NO-leg flatten also floors to the tick");
        // no pmus tick (Kalshi/weather/econ leg) -> just the cent floor, unchanged: 0.93 -> 93c.
        let fine = PositionLeg { pm_min_tick: None, ..coarse.clone() };
        assert_eq!(flatten_exit_cents(&fine, &book), Some(93), "no tick -> cent-granularity floor, no tick snap");
        // a one-sided book (no YES bid for a YES leg) -> None so the caller halts rather than misprice.
        assert_eq!(flatten_exit_cents(&coarse, &Book { yes_bid: None, yes_ask: Some(0.95), age_s: 0.0 }), None);
    }
}
