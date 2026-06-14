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
use crate::types::{Book, Dir, Edge, OutcomeQuote, Venue};

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
    // opts: detection uses the at-scale MARGINAL fee (no ceil) on both venues — capture any arb +EV at size.
    let mut opts: Vec<(Dir, f64)> = Vec::new();
    if let (Some(pa), Some(kb)) = (pm_ask, kb_ask) {
        // PK: back A@pmus (pay pm_ask) + B@Kalshi (pay kb_ask).
        opts.push((Dir::PK, round4((1.0 - (pa + kb)) - pmus_marginal_fee(pa) - kalshi_marginal_fee(kb))));
    }
    if let (Some(ka), Some(pb)) = (ka_ask, pm_bid) {
        // KP: back A@Kalshi (pay ka_ask) + B@pmus (pay NO = 1 - pm_bid).
        let pm_backb = round4(1.0 - pb);
        opts.push((Dir::KP, round4((1.0 - (ka + pm_backb)) - kalshi_marginal_fee(ka) - pmus_marginal_fee(pm_backb))));
    }
    if opts.is_empty() {
        // nothing priceable. monitor.py returns None; the loop skips (no dir to act on). Default dir PK.
        return GameSignal { edge: Edge { net: 0.0, dir: Dir::PK }, no_arb: true, crossed: false };
    }
    // max by net; ties resolve to the first-pushed (PK before KP), matching Python's `max(opts)` stability.
    let best = opts.iter().copied().fold(opts[0], |acc, o| if o.1 > acc.1 { o } else { acc });
    GameSignal { edge: Edge { net: best.1, dir: best.0 }, no_arb: best.1 <= 0.0, crossed: false }
}

// ----------------------------------------------------------------------------------------------------
// 3-LEG DUTCH-BOOK (World Cup) SIGNAL — a PARALLEL signal to `signal`/`game_signal`. Independent of them;
// neither is touched. The 2-leg arb pays $1 if a single binary resolves the way you hedged; the Dutch
// book buys YES on ALL THREE mutually-exclusive outcomes (each on its cheapest venue) so EXACTLY ONE of
// them pays $1 regardless of the result — locked iff the 3 cheapest YES asks sum to < $1 net of fees +
// the void tail.
// ----------------------------------------------------------------------------------------------------

/// Per-outcome leg of a priced Dutch book: which VENUE was cheapest, its YES-ask price, and that leg's
/// at-scale marginal taker fee (all per `$1` of payout, like the 2-leg signal).
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct DutchLeg {
    pub venue: Venue,
    pub yes_ask: f64,
    pub fee: f64,
}

/// The Dutch-book signal. `net > 0` iff the cheapest-venue basket locks a guaranteed profit; `no_arb`
/// mirrors the 2-leg flag (the booking path must refuse a non-positive / unpriceable / crossed basket).
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct DutchSignal {
    /// The 3 chosen legs (one YES per outcome, on its cheapest venue), index-aligned to the input outcomes.
    pub legs: [DutchLeg; 3],
    /// Sum of the 3 cheapest YES asks ($ per $1 payout) — what the basket COSTS, before fees.
    pub basket_cost: f64,
    /// `(1 - basket_cost) - sum(per-leg marginal fees) - void_tail` — the locked profit per $1 payout.
    pub net: f64,
    /// True when nothing priceable (an outcome has no YES ask on EITHER venue), the net is `<= 0`, or a
    /// touched venue book is strictly crossed (a stale phantom — L12).
    pub no_arb: bool,
    /// True when ANY outcome's chosen-venue book is strictly crossed (`bid > ask`, both present).
    pub crossed: bool,
}

/// The cheapest YES ask across the two venues for ONE outcome: `(venue, yes_ask)`, skipping a strictly
/// crossed book (stale phantom — L12) on a venue so the cheap side can't be a crossed quote. `None` if
/// neither venue offers a (non-crossed) YES ask.
fn cheapest_yes(q: &OutcomeQuote) -> Option<(Venue, f64)> {
    let leg = |b: &Book, v: Venue| {
        // a strictly-crossed book (both touches present, bid>ask) is stale -> not a usable ask (L12).
        if matches!((b.yes_bid, b.yes_ask), (Some(bid), Some(ask)) if bid > ask) {
            return None;
        }
        b.yes_ask.map(|a| (v, a))
    };
    match (leg(&q.pm, Venue::Pmus), leg(&q.k, Venue::Kalshi)) {
        (Some(p), Some(k)) => Some(if p.1 <= k.1 { p } else { k }), // tie -> pmus (stable, arbitrary)
        (Some(p), None) => Some(p),
        (None, Some(k)) => Some(k),
        (None, None) => None,
    }
}

/// Whether a venue book is strictly crossed (both touches present, `bid > ask`) — a stale phantom (L12).
fn book_crossed(b: &Book) -> bool {
    matches!((b.yes_bid, b.yes_ask), (Some(bid), Some(ask)) if bid > ask)
}

/// The per-outcome marginal taker fee on a chosen leg (the venue's at-scale fee at the YES-ask price —
/// same model the 2-leg signal sums; detection uses the no-ceil marginal fee so nothing +EV at size is
/// dropped, L10/L15).
fn dutch_leg_fee(venue: Venue, yes_ask: f64) -> f64 {
    match venue {
        Venue::Pmus => pmus_marginal_fee(yes_ask),
        Venue::Kalshi => kalshi_marginal_fee(yes_ask),
    }
}

/// The 3-LEG DUTCH-BOOK signal: for each of the 3 outcomes pick the cheaper YES ask across the two venues;
/// the basket cost is their sum; `net = (1 - basket_cost) - sum(marginal fees) - void_tail`; it's a real
/// arb iff `net > 0`. `void_tail` is the WC void/postpone-tail cost per $1 (a small positive haircut, the
/// caller passes it — 0.0 to disable). A strictly-crossed chosen book (L12) or an outcome with NO YES ask
/// on either venue makes it `no_arb` (the booking path refuses it). Per-$1-of-payout throughout, like the
/// 2-leg signal, so it composes with the same fee model + edge floor.
pub fn dutch_book(outcomes: &[OutcomeQuote; 3], void_tail: f64) -> DutchSignal {
    // ANY chosen-venue book strictly crossed -> stale; reject the whole basket (mirrors the 2-leg `crossed`).
    let crossed = outcomes.iter().any(|q| book_crossed(&q.pm) || book_crossed(&q.k));

    // pick the cheapest (non-crossed) YES ask per outcome; if any outcome is unpriceable the basket can't lock.
    let mut legs = [DutchLeg { venue: Venue::Pmus, yes_ask: 0.0, fee: 0.0 }; 3];
    let mut priceable = true;
    let mut basket_cost = 0.0;
    let mut fees = 0.0;
    for (i, q) in outcomes.iter().enumerate() {
        match cheapest_yes(q) {
            Some((v, ask)) if !crossed => {
                let fee = dutch_leg_fee(v, ask);
                legs[i] = DutchLeg { venue: v, yes_ask: ask, fee };
                basket_cost += ask;
                fees += fee;
            }
            _ => {
                priceable = false;
            }
        }
    }

    if !priceable || crossed {
        // unpriceable (an outcome has no ask either side) or stale-crossed -> no lockable basket.
        return DutchSignal { legs, basket_cost: 0.0, net: 0.0, no_arb: true, crossed };
    }
    let net = round4((1.0 - basket_cost) - fees - void_tail);
    DutchSignal {
        legs,
        basket_cost: round4(basket_cost),
        net,
        no_arb: net <= 0.0,
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

    // ---- 3-LEG DUTCH-BOOK (World Cup) signal ----------------------------------------------------------

    use crate::types::OutcomeTag;

    /// Build an OutcomeQuote from (pm bid/ask, k bid/ask). Helper for the dutch_book vectors.
    fn oc(tag: OutcomeTag, pmb: Option<f64>, pma: Option<f64>, kb: Option<f64>, ka: Option<f64>) -> OutcomeQuote {
        OutcomeQuote { tag, pm: bk(pmb, pma), k: bk(kb, ka) }
    }
    /// Hand-recompute the dutch-book net the way the function does: (1 - sum cheapest) - sum fees - tail.
    fn dutch_net(legs: [(Venue, f64); 3], tail: f64) -> f64 {
        let fee = |v: Venue, p: f64| match v {
            Venue::Pmus => 0.05 * p * (1.0 - p),
            Venue::Kalshi => 0.07 * p * (1.0 - p),
        };
        let cost: f64 = legs.iter().map(|(_, p)| *p).sum();
        let fees: f64 = legs.iter().map(|(v, p)| fee(*v, *p)).sum();
        round4((1.0 - cost) - fees - tail)
    }

    /// A cross-venue basket summing to < $1 is a real arb. The 3 cheapest YES asks (pmus A 0.42, Kalshi
    /// draw 0.20, pmus B 0.30) sum to 0.92 -> locked. Net matches the hand-recompute; cheapest VENUE per
    /// outcome is selected (A/B from pmus, draw from Kalshi). This is THE Dutch-book lock case.
    #[test]
    fn dutch_book_basket_under_one_dollar_is_an_arb() {
        let outs = [
            oc(OutcomeTag::A, Some(0.40), Some(0.42), Some(0.44), Some(0.46)), // A cheap on pmus (0.42)
            oc(OutcomeTag::D, Some(0.24), Some(0.26), Some(0.18), Some(0.20)), // draw cheap on Kalshi (0.20)
            oc(OutcomeTag::B, Some(0.28), Some(0.30), Some(0.33), Some(0.35)), // B cheap on pmus (0.30)
        ];
        let s = dutch_book(&outs, 0.005);
        assert!(!s.no_arb && !s.crossed, "a sub-$1 basket locks");
        assert_eq!(s.legs[0].venue, Venue::Pmus, "outcome A cheapest on pmus");
        assert_eq!(s.legs[1].venue, Venue::Kalshi, "draw cheapest on Kalshi");
        assert_eq!(s.legs[2].venue, Venue::Pmus, "outcome B cheapest on pmus");
        assert!((s.basket_cost - 0.92).abs() < 1e-9, "basket_cost = 0.42+0.20+0.30 = 0.92");
        let expect = dutch_net([(Venue::Pmus, 0.42), (Venue::Kalshi, 0.20), (Venue::Pmus, 0.30)], 0.005);
        assert!((s.net - expect).abs() < 1e-12, "net {} != {}", s.net, expect);
        assert!(s.net > 0.0);
    }

    /// A basket whose 3 cheapest YES asks sum to >= $1 is NOT an arb (no_arb, net <= 0) — even though it
    /// still picks the cheapest venue per outcome. Here the cheapest set is 0.40+0.35+0.30 = 1.05 > 1.
    #[test]
    fn dutch_book_basket_over_one_dollar_is_no_arb() {
        let outs = [
            oc(OutcomeTag::A, Some(0.38), Some(0.40), Some(0.45), Some(0.47)), // cheapest A = pmus 0.40
            oc(OutcomeTag::D, Some(0.33), Some(0.35), Some(0.40), Some(0.42)), // cheapest draw = pmus 0.35
            oc(OutcomeTag::B, Some(0.28), Some(0.30), Some(0.34), Some(0.36)), // cheapest B = pmus 0.30
        ];
        let s = dutch_book(&outs, 0.0);
        assert!(s.no_arb && s.net <= 0.0, "basket 1.05 > $1 -> no lock");
        // it still selected the cheapest venue per outcome (all pmus here).
        assert!(s.legs.iter().all(|l| l.venue == Venue::Pmus));
    }

    /// The cheapest-venue-per-outcome selection MIXES venues correctly: when Kalshi is cheaper for one
    /// outcome and pmus for the others, the basket sums the per-outcome minima — NOT a single venue's 3
    /// asks (one venue's 3 YES always sum > 1, the overround). Proven: same outcomes, the single-venue
    /// pmus sum (0.40+0.36+0.30=1.06) and Kalshi sum (0.38+0.34+0.46=1.18) BOTH exceed the cross sum (0.98).
    #[test]
    fn dutch_book_picks_cross_venue_minimum_not_one_venue() {
        let outs = [
            oc(OutcomeTag::A, Some(0.36), Some(0.40), Some(0.36), Some(0.38)), // A: Kalshi 0.38 < pmus 0.40
            oc(OutcomeTag::D, Some(0.32), Some(0.36), Some(0.32), Some(0.34)), // draw: Kalshi 0.34 < pmus 0.36
            oc(OutcomeTag::B, Some(0.28), Some(0.30), Some(0.44), Some(0.46)), // B: pmus 0.30 < Kalshi 0.46
        ];
        let s = dutch_book(&outs, 0.0);
        // cheapest set = Kalshi 0.38 + Kalshi 0.34 + pmus 0.30 = 1.02. (still > 1 here; the point is the MIX.)
        assert_eq!((s.legs[0].venue, s.legs[1].venue, s.legs[2].venue), (Venue::Kalshi, Venue::Kalshi, Venue::Pmus));
        assert!((s.basket_cost - 1.02).abs() < 1e-9, "basket = cross-venue minima, not one venue's 3 asks");
        // confirm neither single-venue sum is what we used (both overround > the cross sum 1.02... here equal-ish,
        // but the venues DIFFER per leg, which a single-venue basket could never produce).
        assert!(s.legs[0].venue != s.legs[2].venue, "the basket spans both venues");
    }

    /// An outcome with NO YES ask on EITHER venue makes the basket unpriceable -> no_arb (can't lock a
    /// 3-outcome book with a missing leg). The other two outcomes being cheap doesn't rescue it.
    #[test]
    fn dutch_book_unpriceable_outcome_is_no_arb() {
        let outs = [
            oc(OutcomeTag::A, Some(0.10), Some(0.12), Some(0.11), Some(0.13)),
            oc(OutcomeTag::D, None, None, None, None), // draw has no ask on either venue
            oc(OutcomeTag::B, Some(0.20), Some(0.22), Some(0.21), Some(0.23)),
        ];
        let s = dutch_book(&outs, 0.0);
        assert!(s.no_arb, "a missing outcome leg -> no lockable basket");
    }

    /// A strictly-crossed chosen book (bid > ask on an outcome's venue) is stale -> the whole basket is
    /// rejected (no_arb + crossed), even if the apparent basket would sum < $1 (a phantom — L12).
    #[test]
    fn dutch_book_crossed_book_is_rejected() {
        let outs = [
            oc(OutcomeTag::A, Some(0.50), Some(0.40), Some(0.44), Some(0.46)), // pmus A crossed (bid .50 > ask .40)
            oc(OutcomeTag::D, Some(0.24), Some(0.26), Some(0.18), Some(0.20)),
            oc(OutcomeTag::B, Some(0.28), Some(0.30), Some(0.33), Some(0.35)),
        ];
        let s = dutch_book(&outs, 0.0);
        assert!(s.crossed && s.no_arb, "a crossed outcome book makes the basket stale -> reject");
    }

    /// One-sided outcome books still price the basket from whatever YES ask exists per outcome: an outcome
    /// quoted on only ONE venue uses that venue's ask. (Each outcome needs SOME ask, but not both venues.)
    #[test]
    fn dutch_book_one_sided_per_outcome_still_prices() {
        let outs = [
            oc(OutcomeTag::A, None, Some(0.40), None, None),           // A: only pmus ask 0.40
            oc(OutcomeTag::D, None, None, None, Some(0.20)),           // draw: only Kalshi ask 0.20
            oc(OutcomeTag::B, None, Some(0.30), None, None),           // B: only pmus ask 0.30
        ];
        let s = dutch_book(&outs, 0.0);
        assert!(!s.no_arb, "every outcome has an ask -> priceable");
        assert_eq!((s.legs[0].venue, s.legs[1].venue, s.legs[2].venue), (Venue::Pmus, Venue::Kalshi, Venue::Pmus));
        assert!((s.basket_cost - 0.90).abs() < 1e-9);
    }
}
