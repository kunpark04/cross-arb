//! Postponement unwind rule — the void/postpone-tail mitigation (sports).
//!
//! A held cross-arb pair is "riskless" only because its two legs offset. A postponement breaks that:
//! pmus waits up to ~2 weeks for the replay and settles on the real winner, while Kalshi VOIDS the
//! market to a "fair price" if the reschedule is past its ~2-day window — so the legs stop offsetting
//! and the position becomes a large directional bet (~half the stake at risk). And Kalshi voids FAST
//! (it closed voided markets 47–90 min after the scheduled start — `scripts/probe_mlb_postpone.py`).
//! So: on a postponement whose reschedule isn't CONFIRMED inside Kalshi's window, **unwind both legs at
//! market before Kalshi voids.**
//!
//! This module: the DECISION + the closing orders — pure and tested. The live detection (statsapi poll,
//! `postpone.rs`), held-position tracking (`main`), and the actual firing (`main::handle_unwind`) are
//! BUILT + wired. The statsapi /teams abbrev join was verified live (30/30 MLB clubs match, 2026-06-11).

use crate::types::*;

/// A detected postponement for a market we may hold.
#[derive(Clone, Debug)]
pub struct Postponement {
    pub market: String,
    /// Days until the rescheduled makeup game, if known. `None` = unknown (treat as outside Kalshi's
    /// window — you can't confirm it'll settle cleanly, so unwind).
    pub reschedule_in_days: Option<f64>,
}

/// Should we unwind on this postponement? YES unless the reschedule is CONFIRMED inside Kalshi's void
/// window (`kalshi_void_window_days`, ~2 d). A confirmed-soon makeup settles cleanly on both venues
/// (hold); anything later or unknown means Kalshi voids while pmus pays the real result (unwind).
pub fn should_unwind(p: &Postponement, kalshi_void_window_days: f64) -> bool {
    p.reschedule_in_days
        .is_none_or(|d| d > kalshi_void_window_days)
}

/// The two closing orders to flatten a held pair: SELL each of the position's two legs with the EXACT
/// (venue, venue-native market, side) it holds — correct for ALL categories, including a sports hedge
/// whose two legs are two YES legs on two different Kalshi tickers (dir PK: YES@pmus + YES@Kalshi-B). The
/// caller supplies a marketable exit price per leg (`exit_cents[i]` for `legs[i]`; stage-2 reads the live
/// bids). Idempotent `unwind-…-{0,1}` client_order_ids tag the close so a retry can't double-flatten.
pub fn unwind_orders(pos: &Position, exit_cents: [u8; 2]) -> [OrderIntent; 2] {
    std::array::from_fn(|i| {
        let leg = &pos.legs[i];
        OrderIntent {
            venue: leg.venue,
            market: leg.market.clone(),
            action: Action::Sell,
            side: leg.side,
            price_cents: exit_cents[i],
            qty: pos.size,
            client_order_id: format!("unwind-{}-{}", pos.market, i),
        }
    })
}

/// Scan held positions against detected postponements; return the unwind orders for every SPORTS
/// position that should be flattened. (Weather/econ have no postponement concept.)
pub fn postponement_unwinds(
    positions: &[Position],
    postponements: &[Postponement],
    kalshi_void_window_days: f64,
) -> Vec<[OrderIntent; 2]> {
    let mut out = Vec::new();
    for pos in positions {
        if pos.cat != Cat::Sports {
            continue;
        }
        if let Some(p) = postponements.iter().find(|p| p.market == pos.market) {
            if should_unwind(p, kalshi_void_window_days) {
                // exit "at market" — stage-2 supplies the live bids; 1c placeholders here.
                out.push(unwind_orders(pos, [1, 1]));
            }
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn pos() -> Position {
        // a SPORTS dir-PK hedge: leg0 = YES@pmus(slug), leg1 = YES@Kalshi-B(away-team ticker). Both legs
        // are YES — the old yes_venue/no_venue model could not represent this (it assumed a YES + a NO).
        Position {
            market: "aec-mlb-lad-pit-2026-06-14".into(), // pair identity = the pmus slug
            cat: Cat::Sports,
            legs: [
                PositionLeg { venue: Venue::Pmus, market: "aec-mlb-lad-pit-2026-06-14".into(), side: Side::Yes, ..Default::default() },
                PositionLeg { venue: Venue::Kalshi, market: "KXMLBGAME-26JUN14-PIT".into(), side: Side::Yes, ..Default::default() },
            ],
            size: 10,
            cluster: "mlb-lad-pit-2026-06-14".into(),
        }
    }

    #[test]
    fn unwind_when_reschedule_outside_or_unknown() {
        // 6 days out (past Kalshi's 2d window) -> Kalshi voids -> UNWIND
        assert!(should_unwind(&Postponement { market: "m".into(), reschedule_in_days: Some(6.0) }, 2.0));
        // unknown reschedule -> treat as outside -> UNWIND
        assert!(should_unwind(&Postponement { market: "m".into(), reschedule_in_days: None }, 2.0));
        // confirmed next-day (<=2d) -> both venues settle on the replay -> HOLD
        assert!(!should_unwind(&Postponement { market: "m".into(), reschedule_in_days: Some(1.0) }, 2.0));
    }

    #[test]
    fn unwind_orders_sell_both_legs() {
        let o = unwind_orders(&pos(), [53, 45]);
        // each leg is SOLD with the EXACT venue/market/side held — leg1 is YES@Kalshi-B (the away ticker),
        // NOT a NO leg; the venue-native market id is the Kalshi TICKER, not the pmus slug.
        assert_eq!((o[0].action, o[0].side, o[0].venue), (Action::Sell, Side::Yes, Venue::Pmus));
        assert_eq!(o[0].market, "aec-mlb-lad-pit-2026-06-14"); // pmus leg carries the slug
        assert_eq!((o[1].action, o[1].side, o[1].venue), (Action::Sell, Side::Yes, Venue::Kalshi));
        assert_eq!(o[1].market, "KXMLBGAME-26JUN14-PIT"); // Kalshi leg carries the TICKER (leg-market fix)
        assert_eq!((o[0].price_cents, o[1].price_cents), (53, 45));
        assert!(o[0].qty == 10 && o[1].qty == 10);
    }

    #[test]
    fn scan_only_unwinds_affected_sports_positions() {
        let positions = vec![pos()];
        // postponement on a DIFFERENT market -> nothing
        let other = vec![Postponement { market: "aec-mlb-nyy-bos-2026-06-14".into(), reschedule_in_days: None }];
        assert!(postponement_unwinds(&positions, &other, 2.0).is_empty());
        // postponement on OUR market, reschedule unknown -> one unwind (two orders)
        let ours = vec![Postponement { market: pos().market, reschedule_in_days: None }];
        let u = postponement_unwinds(&positions, &ours, 2.0);
        assert_eq!(u.len(), 1);
        assert_eq!(u[0][0].action, Action::Sell);
    }
}
