//! Core domain types for the cross-arb live bot. Pure data; no I/O, no network.

#[derive(Clone, Copy, Debug, PartialEq, Eq, Default)]
pub enum Venue {
    #[default]
    Kalshi,
    Pmus,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Default)]
pub enum Side {
    #[default]
    Yes,
    No,
}

/// Order direction. Entries are `Buy`; an unwind (closing a held leg) is `Sell`.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Action {
    Buy,
    Sell,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Cat {
    Weather,
    Sports,
    Econ,
    Other,
}

/// Cross-venue arb direction = which venue you BUY YES on (the cheap side).
/// `KP` = buy YES on Kalshi + NO on pmus; `PK` = buy YES on pmus + NO on Kalshi.
/// Mirrors the monitor's `dir` field so logs join 1:1.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Dir {
    KP,
    PK,
}

impl Dir {
    /// The DEAR (expensive) venue — the leg you buy NO on. A weather edge led by THIS venue is the
    /// toxic class (H1: ~79% toxic vs ~17% for the cheap-led venue).
    pub fn dear_venue(self) -> Venue {
        match self {
            Dir::PK => Venue::Kalshi, // pmus cheap (buy YES), Kalshi dear (buy NO)
            Dir::KP => Venue::Pmus,
        }
    }
    /// The CHEAP venue — the leg you buy YES on.
    pub fn cheap_venue(self) -> Venue {
        match self {
            Dir::PK => Venue::Pmus,
            Dir::KP => Venue::Kalshi,
        }
    }
}

/// One venue's top-of-book for a market. Prices are dollars in `0.0..=1.0`.
#[derive(Clone, Copy, Debug, Default)]
pub struct Book {
    pub yes_bid: Option<f64>,
    pub yes_ask: Option<f64>,
    /// Seconds since this venue's book last updated — coarse staleness hint (L13).
    pub age_s: f64,
}

impl Book {
    /// A single-venue crossed/locked book (`bid > ask`) is almost always a stale phantom (L12).
    /// `bid == ask` (a legitimate locked book) is allowed.
    pub fn crossed(&self) -> bool {
        matches!((self.yes_bid, self.yes_ask), (Some(b), Some(a)) if b > a)
    }

    pub fn mid(&self) -> Option<f64> {
        match (self.yes_bid, self.yes_ask) {
            (Some(b), Some(a)) => Some((b + a) / 2.0),
            (Some(b), None) => Some(b),
            (None, Some(a)) => Some(a),
            (None, None) => None,
        }
    }
}

/// Fillable depth (contracts) at gross marginal edge `>= {2c, 1c, 0c}` on BOTH legs.
/// Comes straight from the monitor's `depth:{c2,c1,c0}` field.
#[derive(Clone, Copy, Debug, Default)]
pub struct Depth {
    pub c2: u32,
    pub c1: u32,
    pub c0: u32,
}

/// A complete dual-venue snapshot for one co-listed market (classify on the COMPLETE state — L5).
#[derive(Clone, Debug)]
pub struct Quote {
    pub market: String,
    pub cat: Cat,
    pub pm: Book,
    /// Kalshi book for the SAME team pmus lists as YES (= team-A ticker for sports; the only Kalshi
    /// book for weather/econ). The C3 mid-divergence guard compares this against `pm`.
    pub k: Book,
    /// SPORTS only: the Kalshi book for the AWAY team (team-B ticker). `None` for weather/econ, which are
    /// 1:1 (one pmus market <-> one Kalshi market). The game signal needs both Kalshi YES asks.
    pub k_b: Option<Book>,
    pub depth: Depth,
    /// Settlement-identity EMPIRICALLY verified for this pair (weather=true; sports/econ per recon).
    pub settle_clean: bool,
    /// Correlated-exposure cluster key (city-date for weather, game for sports) — see [risk].
    pub cluster: String,
    /// Which venue's quote MOVED to open this edge (the at-open "led_by"). `None` = unknown / first
    /// sighting. For WEATHER, a DEAR-led edge is ~79% toxic vs ~17% cheap-led (H1, weather-only) — the
    /// toxicity-direction signal. Stage-2 populates this by diffing against the prior book snapshot.
    pub led_by: Option<Venue>,
    /// Days until the settlement EVENT (the game for sports, the release for econ; ~0 for weather).
    /// Feeds the event-proximity entry gate — don't lock capital long before the event settles.
    /// `None` = unknown -> gate dormant. Stage-2 computes it from the event date in the market key.
    pub days_to_event: Option<f64>,
}

/// A priced cross-venue edge, net of fees, per `$1` of payout.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Edge {
    pub net: f64,
    pub dir: Dir,
}

/// One leg's order to place. Price is integer cents (venue tick = 1c, whole-share min qty).
#[derive(Clone, Debug)]
pub struct OrderIntent {
    pub venue: Venue,
    pub market: String,
    pub action: Action, // Buy on entry; Sell on unwind/close
    pub side: Side,
    pub price_cents: u8, // 1..=99
    pub qty: u32,
    /// Idempotency key. **Kalshi ONLY** dedupes on this (`client_order_id` is a real idempotency token).
    /// **pmus does NOT** — its CreateOrder has no `clientOrderId` field (silently dropped) and order
    /// creation is NOT idempotent, so a pmus timeout/RateLimited is "unknown" and must be reconciled via
    /// positions/open-orders before any resend, never blindly retried on this key.
    pub client_order_id: String,
}

/// One leg of a held position: the exact (venue, venue-native market id, side) we own. Generalizes the
/// old yes_venue/no_venue pair so a SPORTS hedge — whose two legs can be two YES legs on two different
/// Kalshi tickers (dir PK: YES@pmus + YES@Kalshi-B) — is represented faithfully, not forced into a
/// yes-leg/no-leg shape. The unwind SELLs each leg with this exact (venue, market, side).
#[derive(Clone, Debug, PartialEq, Eq, Default)]
pub struct PositionLeg {
    pub venue: Venue,
    pub market: String, // venue-native id: Kalshi ticker for a Kalshi leg, pmus slug for a pmus leg
    pub side: Side,
    /// The exchange-assigned order id from this leg's fill ack, persisted so the leg can be CANCELLED
    /// (Kalshi by id-in-path; pmus by id + `market`). Empty until the entry acks (set in `apply_outcome`).
    pub venue_order_id: String,
}

/// A held, hedged cross-arb position — the two legs we own. Used by the unwind logic to close out
/// (e.g. a postponement that would break the hedge). `market` is the PAIR identity (the pmus slug),
/// distinct from each leg's venue-native `market`.
#[derive(Clone, Debug)]
pub struct Position {
    pub market: String, // pair identity = the pmus slug (NOT a venue-native order id)
    pub cat: Cat,
    pub legs: [PositionLeg; 2],
    pub size: u32,
    pub cluster: String,
}
