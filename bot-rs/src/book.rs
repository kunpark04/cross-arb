//! Price-indexed single-venue order books + a cross-venue depth walk.
//!
//! - [`KalshiBook`]: reconstructs a live book from an `orderbook_snapshot` + a stream of
//!   `orderbook_delta` messages (port of `bot/kalshi_book.py`). Kalshi quotes resting BIDS per side
//!   (`yes`/`no`) in DOLLARS; a YES ask is the reciprocal of a NO bid (`yes_ask = 1 - no_bid`).
//! - [`PmusBook`]: a pmus book from its YES bid/ask snapshot ladders.
//! - [`depth_at_edge`]: the cross-venue two-pointer walk that reports fillable contract-PAIRS at gross
//!   marginal edge `>= {2c, 1c, 0c}` on BOTH legs (port of `bot/monitor.py::depth_curve` +
//!   `MarketTracker._depth`).
//!
//! Storage is a `BTreeMap<PriceKey, qty>` keyed on integer ticks (not a HashMap scanned every read);
//! best-bid/ask are O(1) via the map's ordered ends. Prices are dollars in `0.0..=1.0`, quantized to
//! the 1c venue tick for the key so float dust can't fragment a level.

use crate::types::{Book, Depth, Dir, Venue};
use std::collections::BTreeMap;

/// Integer price key in hundredths of a cent (4dp dollars * 10_000) so the BTreeMap orders by price
/// and equal prices collapse to one level regardless of float representation. Range 0..=10_000.
type PriceKey = i32;

fn to_key(price_dollars: f64) -> PriceKey {
    (price_dollars * 10_000.0).round() as PriceKey
}
fn from_key(k: PriceKey) -> f64 {
    k as f64 / 10_000.0
}

const QTY_EPS: f64 = 1e-9; // a level at/under this is empty (drop it) — mirrors kalshi_book.py.

/// Kalshi local book for ONE market: `yes`/`no` hold resting BID qty keyed by price.
#[derive(Default, Debug, Clone)]
pub struct KalshiBook {
    yes: BTreeMap<PriceKey, f64>, // resting YES bids
    no: BTreeMap<PriceKey, f64>,  // resting NO bids (a YES ask = 1 - no bid)
}

impl KalshiBook {
    pub fn new() -> Self {
        KalshiBook::default()
    }

    /// Apply a full snapshot, replacing both sides. `yes`/`no` are `[(price_dollars, qty)]` (a side may
    /// be empty/absent). Mirrors `apply_snapshot` — takes levels verbatim (no qty filter; the venue does
    /// not send zero-qty levels in a snapshot, and a delta drops a level the moment it empties).
    pub fn apply_snapshot(&mut self, yes: &[(f64, f64)], no: &[(f64, f64)]) {
        self.yes = yes.iter().map(|&(p, q)| (to_key(p), q)).collect();
        self.no = no.iter().map(|&(p, q)| (to_key(p), q)).collect();
    }

    /// Apply one signed delta to a side. `delta_qty` is additive; a level at/under zero is dropped.
    /// Mirrors `apply_delta` (`side` = Yes/No).
    pub fn apply_delta(&mut self, side: crate::types::Side, price_dollars: f64, delta_qty: f64) {
        let book = match side {
            crate::types::Side::Yes => &mut self.yes,
            crate::types::Side::No => &mut self.no,
        };
        let k = to_key(price_dollars);
        let v = book.entry(k).or_insert(0.0);
        *v += delta_qty;
        if *v <= QTY_EPS {
            book.remove(&k);
        }
    }

    /// (best YES bid, best YES ask). YES ask = 1 - best NO bid. Mirrors `best()`. O(1) via map ends.
    pub fn best(&self) -> (Option<f64>, Option<f64>) {
        let yb = self.yes.keys().next_back().map(|&k| from_key(k));
        let ya = self.no.keys().next_back().map(|&k| round4(1.0 - from_key(k)));
        (yb, ya)
    }

    /// The `Book` touch the signal/risk layers consume (best YES bid/ask + staleness age).
    pub fn touch(&self, age_s: f64) -> Book {
        let (yb, ya) = self.best();
        Book { yes_bid: yb, yes_ask: ya, age_s }
    }

    /// YES BID ladder as `[(price, qty)]` DESCENDING (best/highest first).
    pub fn yes_bid_ladder(&self) -> Vec<(f64, f64)> {
        self.yes.iter().rev().map(|(&k, &q)| (from_key(k), q)).collect()
    }

    /// YES ASK ladder as `[(ask_price, qty)]` ASCENDING (best/lowest ask first). YES ask = 1 - NO bid,
    /// so the highest NO bid is the lowest YES ask. Mirrors `offer_pairs()`.
    pub fn yes_ask_ladder(&self) -> Vec<(f64, f64)> {
        // iterate NO bids high->low so the resulting ask prices come out low->high.
        self.no
            .iter()
            .rev()
            .map(|(&k, &q)| (round4(1.0 - from_key(k)), q))
            .collect()
    }
}

/// pmus book for ONE market: YES bid + YES ask ladders straight from its snapshot (pmus serves the
/// YES book directly — no merge). Ladders are stored sorted on access.
#[derive(Default, Debug, Clone)]
pub struct PmusBook {
    yes_bids: Vec<(f64, f64)>, // (price, qty)
    yes_asks: Vec<(f64, f64)>,
}

impl PmusBook {
    pub fn new() -> Self {
        PmusBook::default()
    }

    /// Replace both YES ladders from a snapshot. Inputs need not be pre-sorted. Levels are taken
    /// verbatim (the depth walk already stops on a `<= QTY_EPS` step), matching `KalshiBook`.
    pub fn apply_snapshot(&mut self, yes_bids: &[(f64, f64)], yes_asks: &[(f64, f64)]) {
        self.yes_bids = yes_bids.to_vec();
        self.yes_asks = yes_asks.to_vec();
    }

    /// (best YES bid = highest bid, best YES ask = lowest ask).
    pub fn best(&self) -> (Option<f64>, Option<f64>) {
        let yb = self
            .yes_bids
            .iter()
            .map(|&(p, _)| p)
            .fold(None, |acc, p| Some(acc.map_or(p, |a: f64| a.max(p))));
        let ya = self
            .yes_asks
            .iter()
            .map(|&(p, _)| p)
            .fold(None, |acc, p| Some(acc.map_or(p, |a: f64| a.min(p))));
        (yb, ya)
    }

    pub fn touch(&self, age_s: f64) -> Book {
        let (yb, ya) = self.best();
        Book { yes_bid: yb, yes_ask: ya, age_s }
    }

    /// YES BID ladder DESCENDING (best first).
    pub fn yes_bid_ladder(&self) -> Vec<(f64, f64)> {
        let mut v = self.yes_bids.clone();
        v.sort_by(|a, b| b.0.partial_cmp(&a.0).unwrap_or(std::cmp::Ordering::Equal));
        v
    }

    /// YES ASK ladder ASCENDING (best/lowest ask first).
    pub fn yes_ask_ladder(&self) -> Vec<(f64, f64)> {
        let mut v = self.yes_asks.clone();
        v.sort_by(|a, b| a.0.partial_cmp(&b.0).unwrap_or(std::cmp::Ordering::Equal));
        v
    }
}

fn round4(x: f64) -> f64 {
    (x * 1.0e4).round() / 1.0e4
}

/// Convert a YES BID ladder into the NO-ASK ladder you'd consume to buy NO: NO ask = 1 - YES bid.
/// A bid ladder descending by price maps to an ask ladder ascending by price. Mirrors `_no_ask_pairs`.
fn no_ask_ladder(yes_bids_desc: &[(f64, f64)]) -> Vec<(f64, f64)> {
    yes_bids_desc.iter().map(|&(p, q)| (round4(1.0 - p), q)).collect()
}

/// Cumulative fillable contract-PAIRS while the marginal GROSS pair edge `1 - a_ask - b_ask` stays
/// `>= {2c, 1c, 0c}`. `a`/`b` are YES-ASK ladders ASCENDING by price. Two-pointer merge; port of
/// `bot/monitor.py::depth_curve` with the fixed `(0.02, 0.01, 0.0)` thresholds. Returns `Depth`.
pub fn depth_curve(a: &[(f64, f64)], b: &[(f64, f64)]) -> Depth {
    let thresholds = [0.02_f64, 0.01, 0.0];
    let lo = 0.0_f64; // min(thresholds)
    let mut acc = [0.0_f64; 3];
    let (mut i, mut j) = (0usize, 0usize);
    let mut a_rem = a.first().map(|&(_, q)| q).unwrap_or(0.0);
    let mut b_rem = b.first().map(|&(_, q)| q).unwrap_or(0.0);
    while i < a.len() && j < b.len() {
        let edge = 1.0 - a[i].0 - b[j].0;
        if edge < lo {
            break;
        }
        let step = a_rem.min(b_rem);
        if step <= QTY_EPS {
            break;
        }
        for (t_idx, &t) in thresholds.iter().enumerate() {
            if edge >= t {
                acc[t_idx] += step;
            }
        }
        a_rem -= step;
        b_rem -= step;
        if a_rem <= QTY_EPS {
            i += 1;
            a_rem = a.get(i).map(|&(_, q)| q).unwrap_or(0.0);
        }
        if b_rem <= QTY_EPS {
            j += 1;
            b_rem = b.get(j).map(|&(_, q)| q).unwrap_or(0.0);
        }
    }
    Depth {
        c2: acc[0].round() as u32,
        c1: acc[1].round() as u32,
        c0: acc[2].round() as u32,
    }
}

/// CROSS-VENUE fillable depth on the SIGNALLED direction. Always pairs the two venues (never a venue
/// with itself). Port of `MarketTracker._depth`:
///   - `Dir::PK` (YES@pmus + NO@Kalshi): leg A = pmus YES-ask ladder; leg B = `1 - Kalshi YES-bid`.
///   - `Dir::KP` (YES@Kalshi + NO@pmus): leg A = Kalshi YES-ask ladder; leg B = `1 - pmus YES-bid`.
pub fn depth_at_edge(k: &KalshiBook, pm: &PmusBook, dir: Dir) -> Depth {
    let (a, b) = match dir {
        Dir::PK => (pm.yes_ask_ladder(), no_ask_ladder(&k.yes_bid_ladder())),
        Dir::KP => (k.yes_ask_ladder(), no_ask_ladder(&pm.yes_bid_ladder())),
    };
    depth_curve(&a, &b)
}

/// The cheap venue's YES-ask ladder for a direction, exposed for sizing/leg-sequencing in stage-2.
pub fn cheap_yes_ask_ladder(k: &KalshiBook, pm: &PmusBook, dir: Dir) -> Vec<(f64, f64)> {
    match dir.cheap_venue() {
        Venue::Pmus => pm.yes_ask_ladder(),
        Venue::Kalshi => k.yes_ask_ladder(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::types::Side;

    /// Port of `bot/kalshi_book.py::_selftest`: snapshot + deltas -> correct best YES bid/ask + ladders.
    #[test]
    fn kalshi_snapshot_and_deltas_track_best() {
        let mut b = KalshiBook::new();
        b.apply_snapshot(&[(0.67, 100.0), (0.66, 50.0)], &[(0.31, 40.0)]);
        assert_eq!(b.best(), (Some(0.67), Some(0.69))); // YES bid .67; YES ask = 1 - .31

        b.apply_delta(Side::Yes, 0.68, 30.0);
        assert_eq!(b.best().0, Some(0.68)); // new best YES bid
        b.apply_delta(Side::Yes, 0.68, -30.0);
        assert_eq!(b.best().0, Some(0.67)); // level emptied -> back to .67
        b.apply_delta(Side::No, 0.33, 10.0);
        assert_eq!(b.best().1, Some(0.67)); // better NO bid .33 -> YES ask = 1 - .33

        // ladders: best YES bid level + best YES ask (derived from best NO bid .33).
        assert_eq!(b.yes_bid_ladder()[0].0, 0.67);
        assert_eq!(b.yes_ask_ladder()[0].0, 0.67);
    }

    #[test]
    fn pmus_book_best_and_ladders() {
        let mut p = PmusBook::new();
        p.apply_snapshot(&[(0.59, 10.0), (0.57, 99.0)], &[(0.61, 40.0), (0.60, 5.0)]);
        assert_eq!(p.best(), (Some(0.59), Some(0.60)));
        assert_eq!(p.yes_bid_ladder()[0].0, 0.59); // highest bid first
        assert_eq!(p.yes_ask_ladder()[0].0, 0.60); // lowest ask first
    }

    /// Port of `bot/monitor.py` depth_curve vector: a=[(.40,10),(.42,20)], b=[(.50,5),(.55,30)] with
    /// thresholds (.04,.02,0) gives {.04:10, .02:30, 0:30}. Our fixed thresholds are (.02,.01,0) so
    /// re-derive the expected against THOSE: edge .10 (5 pairs) + edge .08 (5) both >= .02 -> then the
    /// (.42,.50) rung edge .08 continues... walk it explicitly below.
    #[test]
    fn depth_curve_matches_python_walk() {
        // Exact python assert vector but read at OUR thresholds:
        // rung1 (.40,.50) edge .10, step min(10,5)=5 -> all three +5
        // rung2 (.40,.55) edge .05, step min(5,30)=5 -> all three +5  (a_rem now 0 -> i advances)
        // rung3 (.42,.55) edge .03, step min(20,25)=20 -> all three +20
        // c2(>=.02)=30, c1(>=.01)=30, c0=30
        let a = [(0.40, 10.0), (0.42, 20.0)];
        let b = [(0.50, 5.0), (0.55, 30.0)];
        let d = depth_curve(&a, &b);
        assert_eq!((d.c2, d.c1, d.c0), (30, 30, 30));

        // And reproduce the python (.04,.02,0) result by hand to prove the walk: at >=.04 only the first
        // two rungs (edge .10,.05) qualify, the .03 rung is excluded -> 10. We can't pass .04 here, but
        // the c2(.02) branch already includes that rung, consistent with python's {.02:30}.
    }

    /// Weather MarketTracker depth (monitor.py): P yes-cheap (.59/.60 offers), NO cheap on K (.68/.67
    /// bids) -> dir PK depth {c2:50,c1:50,c0:50}.
    #[test]
    fn depth_at_edge_pk_weather_vector() {
        // pmus YES asks .59@10, .60@40 ; pmus bids irrelevant for PK leg A.
        let mut pm = PmusBook::new();
        pm.apply_snapshot(&[(0.57, 99.0)], &[(0.59, 10.0), (0.60, 40.0)]);
        // Kalshi YES bids .68@20, .67@50 -> NO-ask leg = (1-.68=.32)@20, (1-.67=.33)@50.
        let mut k = KalshiBook::new();
        // store as YES bids; in Kalshi terms a YES bid is the `yes` side.
        k.apply_snapshot(&[(0.68, 20.0), (0.67, 50.0)], &[]);
        let d = depth_at_edge(&k, &pm, Dir::PK);
        // leg A asks: .59@10,.60@40 ; leg B asks: .32@20,.33@50.
        // rung(.59,.32) edge .09 step 10 -> +10 ; rung(.60,.32) edge .08 step 10 -> +10 (B .32 has 20: 10 left)
        // rung(.60,.33) edge .07 step 30 -> +30. total 50 across all thresholds.
        assert_eq!((d.c2, d.c1, d.c0), (50, 50, 50));
    }

    /// Sports-style depth: dir PK back A@pmus(.50 ask, 8 qty) + B@Kalshi (yes ask .44 from NO bid .56,
    /// 30 qty) -> min(8,30)=8 pairs (monitor.py GameTracker vector). Here modelled as a single-market PK.
    #[test]
    fn depth_at_edge_pk_thin_leg_caps() {
        let mut pm = PmusBook::new();
        pm.apply_snapshot(&[(0.48, 10.0)], &[(0.50, 8.0)]); // YES ask .50@8
        let mut k = KalshiBook::new();
        // Kalshi YES bid such that NO-ask leg = 1 - yes_bid = .44 -> yes_bid = .56, qty 30.
        k.apply_snapshot(&[(0.56, 30.0)], &[]);
        let d = depth_at_edge(&k, &pm, Dir::PK);
        // leg A ask .50@8 ; leg B ask (1-.56=.44)@30. edge .06 step min(8,30)=8.
        assert_eq!((d.c2, d.c1, d.c0), (8, 8, 8));
    }

    /// Best is O(1) via map ends, not a scan; a deeper book doesn't change the touch.
    #[test]
    fn deep_book_best_is_top_of_book() {
        let mut b = KalshiBook::new();
        let yes: Vec<(f64, f64)> = (1..=50).map(|i| (i as f64 / 100.0, 10.0)).collect();
        b.apply_snapshot(&yes, &[(0.49, 5.0)]);
        assert_eq!(b.best(), (Some(0.50), Some(0.51))); // top YES bid .50; ask = 1 - .49
    }
}
