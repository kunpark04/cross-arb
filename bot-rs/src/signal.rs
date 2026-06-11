//! Cross-venue arb SIGNAL — a faithful port of `bot/ledger.py::signal` + its fee model.
//!
//! Given two venue `Book`s (pmus + Kalshi YES bid/ask), pick the best cross-venue arb DIRECTION and
//! its net edge per `$1` of payout, net of fees. Detection uses the at-scale MARGINAL Kalshi fee (no
//! ceil) so nothing that is +EV at size is dropped (L10/L15); the exact per-order ceil fee is applied
//! only at booking (`ledger::order_taker_fee_cents`).
//!
//! DIRECTION MAPPING (load-bearing — a flip silently inverts every trade): the Python `dir` string is
//! the CHEAP venue you buy YES on. Python `"P"` (YES@pmus + NO@Kalshi) == [`Dir::PK`]; Python `"K"`
//! (YES@Kalshi + NO@pmus) == [`Dir::KP`]. See `types::Dir` (PK = buy YES on pmus). The parity test pins
//! this against ledger.py's selftest vectors.

use crate::ledger::{marginal_taker_fee, KALSHI_TAKER_COEF, PMUS_TAKER_COEF};
use crate::types::{Book, Dir, Edge};

/// pmus taker fee in dollars per contract at price `p` (linear, no ceil) — `pfee(p)` in ledger.py.
fn pmus_marginal_fee(p: f64) -> f64 {
    if !(0.0 < p && p < 1.0) {
        return 0.0;
    }
    marginal_taker_fee(PMUS_TAKER_COEF, p)
}

/// Kalshi at-scale marginal taker fee in dollars per contract — `kfee(p, marginal=True)` in ledger.py.
fn kalshi_marginal_fee(p: f64) -> f64 {
    if !(0.0 < p && p < 1.0) {
        return 0.0;
    }
    marginal_taker_fee(KALSHI_TAKER_COEF, p)
}

/// Round to 4 decimal places — matches ledger.py's `round(x, 4)` on every net-edge value so the Rust
/// edge compares bit-for-bit against the Python (Python uses banker's rounding; for the 4dp arb edges
/// here the half-way case effectively never occurs, so round-half-up agrees on the test vectors).
fn round4(x: f64) -> f64 {
    (x * 1.0e4).round() / 1.0e4
}

/// The signal outcome. `edge.net > 0` iff a real arb exists; `no_arb` mirrors ledger.py's flag for the
/// no-priceable / non-positive / crossed cases (the booking path must refuse those).
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Signal {
    pub edge: Edge,
    /// True when nothing priceable, the best edge is `<= 0`, or a touched venue is crossed/stale.
    pub no_arb: bool,
    /// True when a venue with BOTH touches present is strictly crossed (`bid > ask`) — a stale phantom.
    pub crossed: bool,
}

/// Best cross-venue arb from two YES books. Port of `bot/ledger.py::signal`.
///
/// Each direction is evaluated on ONLY the two quotes it needs, so a one-sided book never kills the
/// direction that doesn't use the missing quote. A strictly-crossed venue (both touches present,
/// `bid > ask`) is stale: every direction touching it is skipped (L12).
pub fn signal(pm: &Book, k: &Book) -> Signal {
    let (p_yb, p_ya) = (pm.yes_bid, pm.yes_ask);
    let (k_yb, k_ya) = (k.yes_bid, k.yes_ask);

    // venue crossed ONLY when both touches present (a one-sided book is not "crossed").
    let p_x = matches!((p_yb, p_ya), (Some(b), Some(a)) if b > a);
    let k_x = matches!((k_yb, k_ya), (Some(b), Some(a)) if b > a);
    let crossed = p_x || k_x;

    // (dir, net_edge) candidates. dir PK = YES@pmus + NO@Kalshi (Python "P"); KP = YES@Kalshi + NO@pmus.
    let mut opts: Vec<(Dir, f64)> = Vec::new();
    if let (Some(p_ya), Some(k_yb)) = (p_ya, k_yb) {
        if !crossed {
            let (ay, an) = (p_ya, 1.0 - k_yb); // YES@pmus, NO@Kalshi
            let net = round4((1.0 - (ay + an)) - pmus_marginal_fee(ay) - kalshi_marginal_fee(an));
            opts.push((Dir::PK, net));
        }
    }
    if let (Some(k_ya), Some(p_yb)) = (k_ya, p_yb) {
        if !crossed {
            let (ay, an) = (k_ya, 1.0 - p_yb); // YES@Kalshi, NO@pmus
            let net = round4((1.0 - (ay + an)) - kalshi_marginal_fee(ay) - pmus_marginal_fee(an));
            opts.push((Dir::KP, net));
        }
    }

    if opts.is_empty() {
        // nothing priceable (one-sided both ways, or crossed). ledger.py defaults dir "P" == PK.
        return Signal { edge: Edge { net: 0.0, dir: Dir::PK }, no_arb: true, crossed };
    }
    // max by net edge; ties resolve to the first-pushed (PK), matching Python's `max(opts)` stability.
    let best = opts
        .iter()
        .copied()
        .fold(opts[0], |acc, o| if o.1 > acc.1 { o } else { acc });
    Signal {
        edge: Edge { net: best.1, dir: best.0 },
        no_arb: best.1 <= 0.0,
        crossed,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ledger::order_taker_fee_cents;

    fn bk(yb: Option<f64>, ya: Option<f64>) -> Book {
        Book { yes_bid: yb, yes_ask: ya, age_s: 0.0 }
    }

    /// MANDATORY PARITY GATE (ledger.py selftest S6): a wrong fee silently turns +EV into a loss.
    /// Reproduce ledger.py's exact fee vectors with the Rust fee path.
    #[test]
    fn fee_parity_with_ledger_py() {
        // C1: kfee(.5, n=100) = $1.75/order ; 100x per-contract n=1 ceil = $2.00 (the old over-charge).
        assert_eq!(order_taker_fee_cents(KALSHI_TAKER_COEF, 100, 0.5), 175);
        assert_eq!(100 * order_taker_fee_cents(KALSHI_TAKER_COEF, 1, 0.5), 200);
        // pmus taker 100@0.5 -> 125c.
        assert_eq!(order_taker_fee_cents(PMUS_TAKER_COEF, 100, 0.5), 125);
        // C1b: detection MARGINAL Kalshi fee (no ceil) = 1.75c, strictly below the n=1 ceil (2c).
        assert!((kalshi_marginal_fee(0.5) - 0.0175).abs() < 1e-12);
        assert!(kalshi_marginal_fee(0.5) < order_taker_fee_cents(KALSHI_TAKER_COEF, 1, 0.5) as f64 / 100.0);
        // pmus marginal at 0.5 = 1.25c.
        assert!((pmus_marginal_fee(0.5) - 0.0125).abs() < 1e-12);
        // degenerate prices charge nothing (the `0 < p < 1` guard).
        assert_eq!(kalshi_marginal_fee(0.0), 0.0);
        assert_eq!(pmus_marginal_fee(1.0), 0.0);
    }

    /// PARITY: the edge SIGN + DIRECTION on ledger.py's S1 known book (dir "P" == PK).
    /// px = {p_yb:.59, p_ya:.61, k_yb:.67, k_ya:.70}: YES@pmus .61 + NO@Kalshi (1-.67=.33) = .94 cost,
    /// gross .06, minus pmus(.61)+kalshi-marginal(.33) fees -> a real +edge in direction PK.
    #[test]
    fn signal_dir_and_sign_match_ledger_py_s1() {
        let s = signal(&bk(Some(0.59), Some(0.61)), &bk(Some(0.67), Some(0.70)));
        assert_eq!(s.edge.dir, Dir::PK, "ledger.py dir 'P' must map to Dir::PK");
        assert!(!s.no_arb && !s.crossed);
        // hand-recompute the exact net the way ledger.py does and compare bit-for-bit (round4).
        let expect = round4((1.0 - (0.61 + (1.0 - 0.67)))
            - (0.05 * 0.61 * (1.0 - 0.61))
            - (0.07 * 0.33 * (1.0 - 0.33)));
        assert!((s.edge.net - expect).abs() < 1e-12, "net {} != {}", s.edge.net, expect);
        assert!(s.edge.net > 0.0);
    }

    /// PARITY: the FLIPPED book (ledger.py S2 t2) selects dir "K" == KP.
    #[test]
    fn signal_picks_kp_on_flipped_book() {
        // {p_yb:.70, p_ya:.72, k_yb:.60, k_ya:.62}: YES cheap on Kalshi now -> KP.
        let s = signal(&bk(Some(0.70), Some(0.72)), &bk(Some(0.60), Some(0.62)));
        assert_eq!(s.edge.dir, Dir::KP);
        assert!(s.edge.net > 0.0 && !s.no_arb);
    }

    /// PARITY: a flat (venues-agree) book is no_arb (ledger.py S6 C2 `flat`).
    #[test]
    fn flat_book_is_no_arb() {
        let s = signal(&bk(Some(0.59), Some(0.61)), &bk(Some(0.59), Some(0.61)));
        assert!(s.no_arb, "venues agree -> no positive edge");
        assert!(s.edge.net <= 0.0);
    }

    /// PARITY: an internally-crossed venue is rejected as no_arb (ledger.py S6 C3 `crossed`).
    #[test]
    fn crossed_venue_is_rejected() {
        // pmus crossed (bid .70 > ask .60). Was a phantom +edge.
        let s = signal(&bk(Some(0.70), Some(0.60)), &bk(Some(0.30), Some(0.32)));
        assert!(s.crossed && s.no_arb);
        // K crossed with a STALE-HIGH bid (.70 > ask .60): 1-.70=.30 NO looks cheap -> must still reject.
        let s2 = signal(&bk(Some(0.30), Some(0.32)), &bk(Some(0.70), Some(0.60)));
        assert!(s2.crossed && s2.no_arb);
    }

    /// PARITY: one-sided books still price the direction that doesn't need the missing quote
    /// (ledger.py S6 WARN-fix / monitor.py `make_px` per-direction pricing).
    #[test]
    fn one_sided_book_prices_the_valid_direction() {
        // pmus has only a YES ask (no bid); Kalshi full. dir PK needs (p_ya, k_yb) -> priceable;
        // dir KP needs (k_ya, p_yb) and p_yb is missing -> only PK prices.
        let s = signal(&bk(None, Some(0.61)), &bk(Some(0.67), Some(0.70)));
        assert_eq!(s.edge.dir, Dir::PK);
        assert!(!s.no_arb && s.edge.net > 0.0);
        // Kalshi missing its BID kills PK (needs k_yb); only KP prices (k_ya .70 + NO@pmus 1-.59=.41
        // = 1.11 cost -> negative, so no_arb, but the dir is KP — matches ledger.py's per-dir pricing).
        let s2 = signal(&bk(Some(0.59), Some(0.61)), &bk(None, Some(0.70)));
        assert_eq!(s2.edge.dir, Dir::KP);
        assert!(s2.no_arb && s2.edge.net <= 0.0);
    }
}
