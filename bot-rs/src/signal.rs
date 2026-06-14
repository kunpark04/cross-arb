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

    // The two (dir, net_edge) candidates, each priced only if its two needed quotes exist AND nothing is
    // crossed. dir PK = YES@pmus + NO@Kalshi (Python "P"); KP = YES@Kalshi + NO@pmus. Fixed `Option` locals
    // — no heap alloc (was an `opts` Vec pushed/folded every call).
    let pk = match (crossed, p_ya, k_yb) {
        (false, Some(p_ya), Some(k_yb)) => {
            let (ay, an) = (p_ya, 1.0 - k_yb); // YES@pmus, NO@Kalshi
            Some((Dir::PK, round4((1.0 - (ay + an)) - pmus_marginal_fee(ay) - kalshi_marginal_fee(an))))
        }
        _ => None,
    };
    let kp = match (crossed, k_ya, p_yb) {
        (false, Some(k_ya), Some(p_yb)) => {
            let (ay, an) = (k_ya, 1.0 - p_yb); // YES@Kalshi, NO@pmus
            Some((Dir::KP, round4((1.0 - (ay + an)) - kalshi_marginal_fee(ay) - pmus_marginal_fee(an))))
        }
        _ => None,
    };

    // best by net edge; a TIE resolves to PK (the first candidate), matching Python's stable `max(opts)`
    // over [PK, KP]. Nothing priceable (one-sided both ways, or crossed) -> no_arb, default dir PK (ledger.py).
    let best = match (pk, kp) {
        (Some(p), Some(k)) => if k.1 > p.1 { k } else { p },
        (Some(p), None) => p,
        (None, Some(k)) => k,
        (None, None) => return Signal { edge: Edge { net: 0.0, dir: Dir::PK }, no_arb: true, crossed },
    };
    Signal {
        edge: Edge { net: best.1, dir: best.0 },
        no_arb: best.1 <= 0.0,
        crossed,
    }
}

/// The 2-outcome GAME signal. Same shape as [`Signal`] (so the live loop treats both paths uniformly):
/// `edge.net > 0` iff a real arb exists; `no_arb` is the booking-refuse flag; `crossed` flags a stale
/// strictly-crossed pm book OR the C3 orientation guard tripping (both mean "do not trade this frame").
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct GameSignal {
    pub edge: Edge,
    pub no_arb: bool,
    /// True when the pm book is strictly crossed OR the C3 guard tripped (a >40c same-team gap = flip).
    pub crossed: bool,
}

/// 2-outcome CROSS-VENUE game edge. FAITHFUL port of `bot/monitor.py::game_edge` (lines ~157-179).
///
/// pmus lists the game as ONE market (YES = team A); Kalshi lists it as TWO single-team markets
/// (`ka_ask` = "A wins" YES ask, `kb_ask` = "B wins" YES ask). Two cross-venue hedges:
///   - **PK** = back A@pmus + B@Kalshi: cost = `pm_ask + kb_ask`; net = `(1-(pm_ask+kb_ask)) - pfee(pm_ask) - kfee(kb_ask)`.
///   - **KP** = back A@Kalshi + B@pmus: `pm_backB = 1 - pm_bid`; net = `(1-(ka_ask+pm_backB)) - kfee(ka_ask) - pfee(pm_backB)`.
///
/// Best over the priceable opts; `arb = best.net > 0`. Python "PK"/"KP" -> [`Dir::PK`]/[`Dir::KP`].
///
/// C3 ORIENTATION GUARD (monitor.py 163-169, verbatim): a strictly-crossed pm book (`pm_bid>pm_ask`,
/// both present) is stale -> reject; else `guard_pm = pm_ask` (fall back to `pm_bid` when no ask), and a
/// `|guard_pm - ka_ask| > 0.40` gap on the SAME team (pm-YES=A and Kalshi-A) is a flip/mislabel, not edge.
pub fn game_signal(pm_bid: Option<f64>, pm_ask: Option<f64>, ka_ask: Option<f64>, kb_ask: Option<f64>) -> GameSignal {
    // strictly-crossed pm book -> stale (monitor.py 163-164).
    if matches!((pm_bid, pm_ask), (Some(b), Some(a)) if b > a) {
        return GameSignal { edge: Edge { net: 0.0, dir: Dir::PK }, no_arb: true, crossed: true };
    }
    // C3 orientation guard (monitor.py 165-169): one-sided pm book falls back to the bid for the guard.
    let guard_pm = pm_ask.or(pm_bid);
    if let (Some(g), Some(ka)) = (guard_pm, ka_ask) {
        if (g - ka).abs() > 0.40 {
            return GameSignal { edge: Edge { net: 0.0, dir: Dir::PK }, no_arb: true, crossed: true };
        }
    }
    // Candidates: detection uses the at-scale MARGINAL fee (no ceil) on both venues — capture any arb +EV
    // at size. Fixed `Option` locals (no `opts` Vec alloc).
    let pk = match (pm_ask, kb_ask) {
        // PK: back A@pmus (pay pm_ask) + B@Kalshi (pay kb_ask).
        (Some(pa), Some(kb)) => Some((Dir::PK, round4((1.0 - (pa + kb)) - pmus_marginal_fee(pa) - kalshi_marginal_fee(kb)))),
        _ => None,
    };
    let kp = match (ka_ask, pm_bid) {
        // KP: back A@Kalshi (pay ka_ask) + B@pmus (pay NO = 1 - pm_bid).
        (Some(ka), Some(pb)) => {
            let pm_backb = round4(1.0 - pb);
            Some((Dir::KP, round4((1.0 - (ka + pm_backb)) - kalshi_marginal_fee(ka) - pmus_marginal_fee(pm_backb))))
        }
        _ => None,
    };
    // best by net; a TIE resolves to PK (the first candidate), matching Python's stable `max(opts)`. Nothing
    // priceable -> no_arb, default dir PK (monitor.py returns None; the loop skips).
    let best = match (pk, kp) {
        (Some(p), Some(k)) => if k.1 > p.1 { k } else { p },
        (Some(p), None) => p,
        (None, Some(k)) => k,
        (None, None) => return GameSignal { edge: Edge { net: 0.0, dir: Dir::PK }, no_arb: true, crossed: false },
    };
    GameSignal { edge: Edge { net: best.1, dir: best.0 }, no_arb: best.1 <= 0.0, crossed: false }
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

    // ---- GAME (2-outcome) signal: parity vs monitor.py::game_edge -------------------------------------

    /// Hand-recompute monitor.py's `game_edge` net exactly the way Python does and compare bit-for-bit.
    fn game_net_pk(pa: f64, kb: f64) -> f64 {
        round4((1.0 - (pa + kb)) - (0.05 * pa * (1.0 - pa)) - (0.07 * kb * (1.0 - kb)))
    }
    fn game_net_kp(ka: f64, pm_bid: f64) -> f64 {
        let pb = round4(1.0 - pm_bid);
        round4((1.0 - (ka + pb)) - (0.07 * ka * (1.0 - ka)) - (0.05 * pb * (1.0 - pb)))
    }

    /// PK is the best config (back A@pmus + B@Kalshi) on a vector where the two single-team Kalshi YES
    /// asks + pm YES ask sum to < $1. dir maps Python "PK" -> Dir::PK; net matches game_edge bit-for-bit.
    #[test]
    fn game_signal_pk_matches_game_edge() {
        // pm: YES bid .50 / ask .52 ; Kalshi A .55, B .45. PK cost .52+.45=.97 -> thin +edge.
        let g = game_signal(Some(0.50), Some(0.52), Some(0.55), Some(0.45));
        assert_eq!(g.edge.dir, Dir::PK, "Python 'PK' must map to Dir::PK");
        assert!(!g.no_arb && !g.crossed);
        assert!((g.edge.net - game_net_pk(0.52, 0.45)).abs() < 1e-12, "net {} != {}", g.edge.net, game_net_pk(0.52, 0.45));
        assert!(g.edge.net > 0.0);
    }

    /// KP is the best config (back A@Kalshi + B@pmus = NO@pmus) when Kalshi-A is the cheap way to back A.
    /// pm_backB = 1 - pm_bid; net matches game_edge's KP branch bit-for-bit.
    #[test]
    fn game_signal_kp_matches_game_edge() {
        // pm: YES bid .60 / ask .62 ; Kalshi A .33, B .70. KP: A@K .33 + NO@pmus (1-.60=.40) = .73 cost.
        let g = game_signal(Some(0.60), Some(0.62), Some(0.33), Some(0.70));
        assert_eq!(g.edge.dir, Dir::KP);
        assert!(!g.no_arb && !g.crossed);
        assert!((g.edge.net - game_net_kp(0.33, 0.60)).abs() < 1e-12, "net {} != {}", g.edge.net, game_net_kp(0.33, 0.60));
        assert!(g.edge.net > 0.0);
        // and PK here is NEGATIVE (pm_ask .62 + kB .70 = 1.32 > 1) -> KP strictly wins.
        assert!(game_net_pk(0.62, 0.70) < 0.0);
    }

    /// The C3 orientation guard: a >40c gap between guard_pm and Kalshi-A (the SAME team) is a flip, not
    /// edge -> no_arb + crossed, regardless of how fat the apparent gap looks (monitor.py 165-169).
    #[test]
    fn game_signal_c3_orientation_guard_rejects_flip() {
        // pm YES(=A) .20 vs Kalshi-A .80 -> |.20-.80|=.60 > .40 -> guarded out (a mislabeled/flipped pair).
        let g = game_signal(Some(0.18), Some(0.20), Some(0.80), Some(0.15));
        assert!(g.no_arb && g.crossed, "C3: same-team >40c gap must reject");
        // one-sided pm (no ask) falls back to the BID for the guard (still covers dir KP). bid .82 vs A .30.
        let g2 = game_signal(Some(0.82), None, Some(0.30), Some(0.65));
        assert!(g2.no_arb && g2.crossed);
        // a SMALL same-team gap (<=40c) passes the guard (a real cross-venue arb is a few cents).
        let g3 = game_signal(Some(0.50), Some(0.52), Some(0.55), Some(0.45));
        assert!(!g3.crossed);
    }

    /// A strictly-crossed pm book (bid > ask, both present) is stale -> rejected (monitor.py 163-164).
    #[test]
    fn game_signal_crossed_pm_is_rejected() {
        let g = game_signal(Some(0.60), Some(0.50), Some(0.55), Some(0.45));
        assert!(g.no_arb && g.crossed);
    }

    /// One-sided Kalshi books: PK needs (pm_ask, kB), KP needs (kA, pm_bid). A missing quote drops only
    /// the direction that needs it (monitor.py's per-direction opt construction).
    #[test]
    fn game_signal_one_sided_prices_valid_direction_only() {
        // no kA_ask -> only PK is priceable (needs pm_ask + kB). guard is skipped (kA_ask None).
        let g = game_signal(Some(0.50), Some(0.52), None, Some(0.45));
        assert_eq!(g.edge.dir, Dir::PK);
        assert!((g.edge.net - game_net_pk(0.52, 0.45)).abs() < 1e-12);
        // no kB_ask AND no pm_bid -> nothing priceable -> no_arb, default dir PK.
        let g2 = game_signal(None, Some(0.52), None, None);
        assert!(g2.no_arb && !g2.crossed && g2.edge.dir == Dir::PK);
    }
}
