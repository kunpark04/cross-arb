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
//! Stage-1 (here): the DECISION + the closing orders — pure and tested. Stage-2 wires live detection
//! (statsapi poll, `probe_mlb_postpone.py`), the held-position tracking, and the actual firing.

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
        .map_or(true, |d| d > kalshi_void_window_days)
}

/// The two closing orders to flatten a held pair: SELL the YES leg and SELL the NO leg, each at the
/// venue's marketable price (stage-2 supplies the live bid; the caller passes the target here).
/// Idempotent `unwind-…` client_order_ids tag the close so a retry can't double-flatten.
pub fn unwind_orders(pos: &Position, yes_exit_cents: u8, no_exit_cents: u8) -> [OrderIntent; 2] {
    [
        OrderIntent {
            venue: pos.yes_venue,
            market: pos.market.clone(),
            action: Action::Sell,
            side: Side::Yes,
            price_cents: yes_exit_cents,
            qty: pos.size,
            client_order_id: format!("unwind-{}-Y", pos.market),
        },
        OrderIntent {
            venue: pos.no_venue,
            market: pos.market.clone(),
            action: Action::Sell,
            side: Side::No,
            price_cents: no_exit_cents,
            qty: pos.size,
            client_order_id: format!("unwind-{}-N", pos.market),
        },
    ]
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
                // exit "at market" — stage-2 supplies the live bids; 1c placeholder here.
                out.push(unwind_orders(pos, 1, 1));
            }
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn pos() -> Position {
        Position {
            market: "aec-mlb-lad-pit-2026-06-14".into(),
            cat: Cat::Sports,
            yes_venue: Venue::Pmus,
            no_venue: Venue::Kalshi,
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
        let o = unwind_orders(&pos(), 53, 45);
        assert_eq!((o[0].action, o[0].side, o[0].venue), (Action::Sell, Side::Yes, Venue::Pmus));
        assert_eq!((o[1].action, o[1].side, o[1].venue), (Action::Sell, Side::No, Venue::Kalshi));
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
