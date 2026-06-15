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
//! 4dp (1/100c) for the key so float dust can't fragment a level — sub-cent-safe for the 2dp venue grid
//! (a genuine sub-cent price keys separately, matching no current series).

use crate::types::{Book, Depth, Dir, Venue};
use std::collections::BTreeMap;
use std::time::Instant;

/// Integer price key in hundredths of a cent (4dp dollars * 10_000) so the BTreeMap orders by price
/// and equal prices collapse to one level regardless of float representation. Range 0..=10_000. The
/// quantum is 1/100c (4dp), NOT 1c — safe for the 2dp venue grid; a true sub-cent price keys separately.
type PriceKey = i32;

fn to_key(price_dollars: f64) -> PriceKey {
    (price_dollars * 10_000.0).round() as PriceKey
}
fn from_key(k: PriceKey) -> f64 {
    k as f64 / 10_000.0
}

const QTY_EPS: f64 = 1e-9; // a level at/under this is empty (drop it) — mirrors kalshi_book.py.

/// Kalshi local book for ONE market: `yes`/`no` hold resting BID qty keyed by price.
/// `last_update` stamps the wall-clock instant of the most recent applied snapshot/delta — the source
/// of the staleness `age` the risk gate reads (L13: a wedged stream stops restamping, so its `age`
/// grows and `Reject::StaleBook` fires). Not `#[derive(Default)]` because `Instant` has no `Default`.
#[derive(Debug, Clone)]
pub struct KalshiBook {
    yes: BTreeMap<PriceKey, f64>, // resting YES bids
    no: BTreeMap<PriceKey, f64>,  // resting NO bids (a YES ask = 1 - no bid)
    last_update: Instant,
}

impl Default for KalshiBook {
    fn default() -> Self {
        KalshiBook { yes: BTreeMap::new(), no: BTreeMap::new(), last_update: Instant::now() }
    }
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
        self.last_update = Instant::now();
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
        self.last_update = Instant::now();
    }

    /// (best YES bid, best YES ask). YES ask = 1 - best NO bid. Mirrors `best()`. O(1) via map ends.
    pub fn best(&self) -> (Option<f64>, Option<f64>) {
        let yb = self.yes.keys().next_back().map(|&k| from_key(k));
        let ya = self.no.keys().next_back().map(|&k| round4(1.0 - from_key(k)));
        (yb, ya)
    }

    /// The `Book` touch the signal/risk layers consume (best YES bid/ask + staleness age). `age_s` is
    /// derived from `last_update` so a wedged/half-dead stream's book ages out and the staleness gate
    /// (L13) fires — the live loop no longer hands a hard-coded `0.0`.
    pub fn touch(&self) -> Book {
        self.touch_at(self.last_update.elapsed().as_secs_f64())
    }

    /// `touch` with an EXPLICIT age — for synthetic snapshots/tests that pin staleness deterministically
    /// (real-time `Instant` elapsed is non-deterministic). The live path uses `touch()`.
    pub fn touch_at(&self, age_s: f64) -> Book {
        let (yb, ya) = self.best();
        Book { yes_bid: yb, yes_ask: ya, age_s }
    }

    /// Seconds since the last applied snapshot/delta (the raw staleness `age`). Exposed so the live loop
    /// can take the WORST of the two legs as the pair's staleness.
    pub fn age_s(&self) -> f64 {
        self.last_update.elapsed().as_secs_f64()
    }

    /// TEST-ONLY: backdate `last_update` so `touch()`/`age_s()` report a deterministic age without a real
    /// sleep — lets a test prove a wedged book ages out (the `Reject::StaleBook` path) end-to-end.
    #[cfg(test)]
    pub fn backdate(&mut self, secs: u64) {
        self.last_update = Instant::now() - std::time::Duration::from_secs(secs);
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
/// YES book directly — no merge). Ladders are kept **pre-sorted at apply time** (bids high→low, asks
/// low→high) so `best()` is O(1) off the front and the per-frame depth-walk reads no longer re-sort
/// (the old store-unsorted/sort-on-every-read path was O(n log n) per ladder read). `last_update`
/// carries the staleness clock, same as `KalshiBook`.
#[derive(Debug, Clone)]
pub struct PmusBook {
    yes_bids: Vec<(f64, f64)>, // (price, qty) — sorted DESCENDING by price (best/highest first)
    yes_asks: Vec<(f64, f64)>, // (price, qty) — sorted ASCENDING by price (best/lowest first)
    last_update: Instant,
}

impl Default for PmusBook {
    fn default() -> Self {
        PmusBook { yes_bids: Vec::new(), yes_asks: Vec::new(), last_update: Instant::now() }
    }
}

impl PmusBook {
    pub fn new() -> Self {
        PmusBook::default()
    }

    /// Replace both YES ladders from a snapshot. Inputs need not be pre-sorted — we sort each ONCE here
    /// (bids high→low, asks low→high) so every later read (`best`, the ladder accessors, the depth walk)
    /// is sort-free. Levels are taken verbatim otherwise (the depth walk already stops on a `<= QTY_EPS`
    /// step), matching `KalshiBook`.
    pub fn apply_snapshot(&mut self, yes_bids: &[(f64, f64)], yes_asks: &[(f64, f64)]) {
        self.yes_bids = yes_bids.to_vec();
        self.yes_bids.sort_by(|a, b| b.0.partial_cmp(&a.0).unwrap_or(std::cmp::Ordering::Equal)); // high → low
        self.yes_asks = yes_asks.to_vec();
        self.yes_asks.sort_by(|a, b| a.0.partial_cmp(&b.0).unwrap_or(std::cmp::Ordering::Equal)); // low → high
        self.last_update = Instant::now();
    }

    /// (best YES bid = highest bid, best YES ask = lowest ask). O(1): the ladders are kept sorted, so the
    /// best of each side is the FRONT element (was an O(n) max/min fold over the whole ladder every touch).
    pub fn best(&self) -> (Option<f64>, Option<f64>) {
        let yb = self.yes_bids.first().map(|&(p, _)| p);
        let ya = self.yes_asks.first().map(|&(p, _)| p);
        (yb, ya)
    }

    /// `Book` touch with the staleness age derived from `last_update` (see `KalshiBook::touch`).
    pub fn touch(&self) -> Book {
        self.touch_at(self.last_update.elapsed().as_secs_f64())
    }

    /// `touch` with an EXPLICIT age — synthetic/test path (see `KalshiBook::touch_at`).
    pub fn touch_at(&self, age_s: f64) -> Book {
        let (yb, ya) = self.best();
        Book { yes_bid: yb, yes_ask: ya, age_s }
    }

    /// Seconds since the last applied snapshot (the raw staleness `age`).
    pub fn age_s(&self) -> f64 {
        self.last_update.elapsed().as_secs_f64()
    }

    /// TEST-ONLY: backdate `last_update` (see `KalshiBook::backdate`).
    #[cfg(test)]
    pub fn backdate(&mut self, secs: u64) {
        self.last_update = Instant::now() - std::time::Duration::from_secs(secs);
    }

    /// YES BID ladder DESCENDING (best first). Stored pre-sorted, so this is a plain clone (no re-sort).
    pub fn yes_bid_ladder(&self) -> Vec<(f64, f64)> {
        self.yes_bids.clone()
    }

    /// YES ASK ladder ASCENDING (best/lowest ask first). Stored pre-sorted, so this is a plain clone.
    pub fn yes_ask_ladder(&self) -> Vec<(f64, f64)> {
        self.yes_asks.clone()
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

/// SPORTS (2-outcome) cross-venue fillable depth on the SIGNALLED direction. Port of
/// `MarketTracker._depth` (monitor.py 210-216 — the `GameTracker._depth` arm):
///   - `Dir::PK` (back A@pmus + B@Kalshi): leg A = pmus YES-ask ladder; leg B = Kalshi-B YES-ask ladder.
///   - `Dir::KP` (back A@Kalshi + B@pmus): leg A = Kalshi-A YES-ask ladder; leg B = `1 - pmus YES-bid`.
///
/// `ka`/`kb` are the two single-team Kalshi books (A = team pmus lists as YES, B = the other team).
pub fn game_depth_at_edge(pm: &PmusBook, ka: &KalshiBook, kb: &KalshiBook, dir: Dir) -> Depth {
    let (a, b) = match dir {
        Dir::PK => (pm.yes_ask_ladder(), kb.yes_ask_ladder()),
        Dir::KP => (ka.yes_ask_ladder(), no_ask_ladder(&pm.yes_bid_ladder())),
    };
    depth_curve(&a, &b)
}

/// Sum of resting ask-volume on a (price, qty) ladder — one leg's gross fillability.
fn ladder_qty(l: &[(f64, f64)]) -> f64 {
    l.iter().map(|(_, q)| q).sum()
}

/// DYNAMIC ORDER (2026-06-15): should the pmus leg fire FIRST? Fire the THINNER (less ask-volume) leg first
/// so its FOK-reject is a clean abort, never a committed-then-naked leg. Reuses [`depth_at_edge`]'s per-leg
/// ask ladders (the same YES/NO + dir transform); returns `true` (pmus first — the [0020] default, pmus
/// usually the thinner one) iff the pmus leg has `<=` the Kalshi leg's ask-volume, and `false` (Kalshi first)
/// when the Kalshi book is the thinner one (the 4 live Kalshi-409 fails: pietai/laxhigh/sly-gal). Ties + an
/// empty book default to pmus-first.
pub fn fire_pmus_first(k: &KalshiBook, pm: &PmusBook, dir: Dir) -> bool {
    let (pmus, kalshi) = match dir {
        Dir::PK => (pm.yes_ask_ladder(), no_ask_ladder(&k.yes_bid_ladder())),
        Dir::KP => (no_ask_ladder(&pm.yes_bid_ladder()), k.yes_ask_ladder()),
    };
    ladder_qty(&pmus) <= ladder_qty(&kalshi)
}

/// SPORTS (2-outcome) variant of [`fire_pmus_first`] — the per-leg ask ladders of [`game_depth_at_edge`].
pub fn fire_pmus_first_game(pm: &PmusBook, ka: &KalshiBook, kb: &KalshiBook, dir: Dir) -> bool {
    let (pmus, kalshi) = match dir {
        Dir::PK => (pm.yes_ask_ladder(), kb.yes_ask_ladder()),
        Dir::KP => (no_ask_ladder(&pm.yes_bid_ladder()), ka.yes_ask_ladder()),
    };
    ladder_qty(&pmus) <= ladder_qty(&kalshi)
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

    /// GAME depth, dir PK (back A@pmus + B@Kalshi): leg A = pmus YES-asks, leg B = Kalshi-B YES-asks.
    /// A Kalshi YES ask is read from the NO-bid side (yes_ask = 1 - no_bid), so Kalshi-B's NO bid .56@30
    /// gives a YES ask .44@30. pmus A .50@8 -> min(8,30)=8 pairs (GameTracker vector).
    #[test]
    fn game_depth_pk_pairs_pmus_with_kalshi_b() {
        let mut pm = PmusBook::new();
        pm.apply_snapshot(&[(0.48, 10.0)], &[(0.50, 8.0)]); // pmus YES ask .50@8 (back A@pmus)
        let ka = KalshiBook::new(); // unused in PK; present to prove it's NOT consulted
        let mut kb = KalshiBook::new();
        kb.apply_snapshot(&[], &[(0.56, 30.0)]); // Kalshi-B NO bid .56 -> YES ask 1-.56=.44 @30
        let d = game_depth_at_edge(&pm, &ka, &kb, Dir::PK);
        // leg A ask .50@8 ; leg B (Kalshi-B YES) ask .44@30. edge .06 step min(8,30)=8.
        assert_eq!((d.c2, d.c1, d.c0), (8, 8, 8));
    }

    /// GAME depth, dir KP (back A@Kalshi + B@pmus): leg A = Kalshi-A YES-asks, leg B = pmus NO-asks
    /// (= 1 - pmus YES-bid). Kalshi-A YES ask .55@12 ; pmus YES bid .42 -> NO ask .58@20 -> min(12,20)=12.
    #[test]
    fn game_depth_kp_pairs_kalshi_a_with_pmus_no() {
        let mut pm = PmusBook::new();
        pm.apply_snapshot(&[(0.42, 20.0)], &[(0.44, 99.0)]); // pmus YES bid .42 -> NO-ask leg .58@20
        let mut ka = KalshiBook::new();
        ka.apply_snapshot(&[(0.30, 5.0)], &[(0.45, 12.0)]); // Kalshi-A YES ask = 1 - NO bid .45 = .55 @12
        let kb = KalshiBook::new(); // unused in KP
        let d = game_depth_at_edge(&pm, &ka, &kb, Dir::KP);
        // leg A (Kalshi-A) ask .55@12 ; leg B (pmus NO) ask .58@20. edge 1-.55-.58 = -.13 < 0 -> 0 depth.
        assert_eq!((d.c2, d.c1, d.c0), (0, 0, 0));
        // widen: make Kalshi-A cheaper (.40 ask) so the KP edge is positive and depth caps on leg A.
        let mut ka2 = KalshiBook::new();
        ka2.apply_snapshot(&[(0.30, 5.0)], &[(0.60, 12.0)]); // YES ask = 1 - .60 = .40 @12
        let d2 = game_depth_at_edge(&pm, &ka2, &kb, Dir::KP);
        // leg A .40@12 ; leg B (pmus NO) .58@20. edge 1-.40-.58=.02 step min(12,20)=12 -> c2(>=.02)=12.
        assert_eq!((d2.c2, d2.c1, d2.c0), (12, 12, 12));
    }

    /// DYNAMIC ORDER (the fire-thinner-leg-first decision): fire pmus first when it is thinner-or-equal (the
    /// 0020 default), fire KALSHI first when the Kalshi book is the thin one (the live 409-fail class:
    /// pietai/laxhigh/sly-gal). Uses the SAME per-leg ask ladders `depth_at_edge` does, so the YES/NO + dir
    /// transform stays consistent.
    #[test]
    fn fire_pmus_first_picks_the_thinner_leg() {
        // BINARY PK: pmus YES-ask thin (5), Kalshi NO-ask (from a deep YES-bid) deep (50) -> pmus first.
        let mut pm = PmusBook::new();
        pm.apply_snapshot(&[], &[(0.59, 5.0)]); // pmus YES ask .59 @5 (thin)
        let mut k = KalshiBook::new();
        k.apply_snapshot(&[(0.68, 50.0)], &[]); // Kalshi YES bid .68 @50 -> NO ask deep
        assert!(fire_pmus_first(&k, &pm, Dir::PK), "pmus thinner -> pmus first (the 0020 default)");

        // flip the thinness: pmus YES-ask deep (50), Kalshi NO-ask thin (1) -> KALSHI first (the 409 fix).
        let mut pm2 = PmusBook::new();
        pm2.apply_snapshot(&[], &[(0.59, 50.0)]);
        let mut k2 = KalshiBook::new();
        k2.apply_snapshot(&[(0.68, 1.0)], &[]); // Kalshi YES bid @1 -> NO ask thin
        assert!(!fire_pmus_first(&k2, &pm2, Dir::PK), "thin Kalshi book -> Kalshi first (clean abort, no naked pmus)");

        // SPORTS PK: pmus YES-ask deep (30) vs Kalshi-B YES-ask thin (2) -> Kalshi first.
        let mut pm3 = PmusBook::new();
        pm3.apply_snapshot(&[], &[(0.50, 30.0)]);
        let ka = KalshiBook::new();
        let mut kb = KalshiBook::new();
        kb.apply_snapshot(&[], &[(0.44, 2.0)]); // Kalshi-B YES ask @2 (thin)
        assert!(!fire_pmus_first_game(&pm3, &ka, &kb, Dir::PK), "sports: thin Kalshi-B -> Kalshi first");
    }

    /// Best is O(1) via map ends, not a scan; a deeper book doesn't change the touch.
    #[test]
    fn deep_book_best_is_top_of_book() {
        let mut b = KalshiBook::new();
        let yes: Vec<(f64, f64)> = (1..=50).map(|i| (i as f64 / 100.0, 10.0)).collect();
        b.apply_snapshot(&yes, &[(0.49, 5.0)]);
        assert_eq!(b.best(), (Some(0.50), Some(0.51))); // top YES bid .50; ask = 1 - .49
    }

    /// Staleness `age` is derived from the last applied update (L13): a just-applied snapshot/delta is
    /// ~fresh (age ~0), so `touch()` no longer hard-codes 0.0. (The "old book -> StaleBook reject" path
    /// is exercised at the risk layer via `touch_at`, since fast-forwarding a real `Instant` isn't
    /// deterministic — `touch_at` presents exactly what an aged book yields.)
    #[test]
    fn touch_age_tracks_last_update() {
        let mut k = KalshiBook::new();
        k.apply_snapshot(&[(0.67, 100.0)], &[(0.31, 40.0)]);
        assert!(k.touch().age_s < 1.0, "just-updated Kalshi book is fresh"); // wall-clock, well under 1s
        let mut pm = PmusBook::new();
        pm.apply_snapshot(&[(0.05, 300.0)], &[(0.07, 90.0)]);
        assert!(pm.touch().age_s < 1.0, "just-updated pmus book is fresh");
        // an applied delta restamps freshness too.
        k.apply_delta(Side::Yes, 0.68, 30.0);
        assert!(k.age_s() < 1.0);
    }

    /// A wedged stream stops restamping -> `touch()` reports a large age that the risk gate rejects
    /// (`Reject::StaleBook`). Backdating is deterministic (no sleep); this is the real `touch()` path,
    /// not a hand-built `Book{age_s}`. The threshold lives in config (`max_book_age_s`, default 5s).
    #[test]
    fn wedged_book_ages_out_and_is_rejected_by_risk() {
        use crate::types::*;
        let mut k = KalshiBook::new();
        k.apply_snapshot(&[(0.86, 100.0)], &[(0.13, 100.0)]);
        let mut pm = PmusBook::new();
        pm.apply_snapshot(&[(0.74, 100.0)], &[(0.75, 100.0)]);
        pm.backdate(9); // pmus stream wedged ~9s -> stale (> the 5s default)
        let q = Quote {
            market: "tc-temp-nychigh-2026-06-11-gte95f".into(),
            cat: Cat::Weather,
            pm: pm.touch(), // REAL age from last_update, not 0.0
            k: k.touch(),
            k_b: None,
            depth: Depth { c2: 50, c1: 60, c0: 70 },
            settle_clean: true,
            cluster: "nychigh-2026-06-11".into(),
            led_by: None,
            days_to_event: None,
            fire_pmus_first: true,
        };
        assert!(q.pm.age_s > 5.0 && q.k.age_s < 1.0);
        let cfg = crate::config::Config::test_default();
        let r = crate::risk::evaluate(&cfg, &q, &Edge { net: 0.09, dir: Dir::PK }, &crate::risk::Exposure::new(), 1000);
        assert_eq!(r, Err(crate::risk::Reject::StaleBook(Venue::Pmus)));
    }
}
