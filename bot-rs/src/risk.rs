//! Pre-trade risk gates — every edge case the project has learned, enforced BEFORE a single order is
//! sent. These are the SYNCHRONOUS gates (evaluated on a complete dual-venue snapshot). The
//! execution-TIME edge cases (leg-fill timeout -> unwind, MLB postponement kill, fill/partial
//! handling) live in `legs.rs` (stage 2) and are referenced where relevant.

use crate::config::Config;
use crate::types::*;
use std::collections::HashMap;

#[derive(Clone, Debug, PartialEq)]
pub enum Reject {
    KillSwitch,           // CROSSARB_KILL
    StreamPaused,         // WS reconnect / seq-gap: book is rebuilding -> do NOT trade it (0013)
    SettlementUnverified, // invariant #1: econ/sports pair not empirically settlement-clean
    CrossedBook(Venue),   // L12 — single-venue bid>ask phantom
    StaleBook(Venue),     // L13 — book older than max_book_age_s
    MidDivergence(f64),   // L1 — identical-settlement pair's mids disagree wildly (bad join/stale)
    NonPositiveEdge,      // L11 — never book net<=0
    BelowEdgeFloor,       // opt-in 0014 floor (skip thin arbs); always reported, never silent (L15)
    NoFillableSize,       // size collapsed to 0 after depth/clip/affordability/caps
    PairCap,
    ClusterCap,
    TotalCap,
    ConcurrencyCap,
}

/// Live exposure the gates read (updated by the bot as positions open/settle).
#[derive(Default)]
pub struct Exposure {
    pub per_pair: HashMap<String, f64>,    // $ notional locked per market
    pub per_cluster: HashMap<String, f64>, // $ notional per city-date / game cluster (correlated risk)
    pub total: f64,
    pub open_positions: u32,
    pub stream_paused: bool, // set true while EITHER venue WS is reconnecting/resubscribing
}

impl Exposure {
    pub fn new() -> Self {
        Exposure::default()
    }
}

/// An approved trade: the size to actually send + the per-contract cash cost.
#[derive(Clone, Debug, PartialEq)]
pub struct Approved {
    pub size: u32,
    pub cost_per: f64,
    pub edge: Edge,
}

/// Run the full pre-trade gate. `affordable` = contracts the bankroll can fund at this price.
/// Order matters: cheap/structural rejects first, sizing last.
pub fn evaluate(
    cfg: &Config,
    q: &Quote,
    edge: &Edge,
    exp: &Exposure,
    affordable: u32,
) -> Result<Approved, Reject> {
    // 0. global halts
    if cfg.kill_switch {
        return Err(Reject::KillSwitch);
    }
    if exp.stream_paused {
        return Err(Reject::StreamPaused); // never trade a half-rebuilt book
    }

    // 1. settlement identity (invariant #1) — the catastrophic both-legs-loss axis.
    //    Weather is empirically verified; econ/sports must carry settle_clean=true.
    if cfg.require_settle_clean && !q.settle_clean && q.cat != Cat::Weather {
        return Err(Reject::SettlementUnverified);
    }

    // 2. per-venue book sanity (L12 crossed, L13 stale)
    if q.k.crossed() {
        return Err(Reject::CrossedBook(Venue::Kalshi));
    }
    if q.pm.crossed() {
        return Err(Reject::CrossedBook(Venue::Pmus));
    }
    if q.k.age_s > cfg.max_book_age_s {
        return Err(Reject::StaleBook(Venue::Kalshi));
    }
    if q.pm.age_s > cfg.max_book_age_s {
        return Err(Reject::StaleBook(Venue::Pmus));
    }

    // 3. cross-venue mid-divergence (L1): two settlement-identical legs should price close;
    //    a huge gap is almost always a bad join or a stale quote, not free money.
    if let (Some(km), Some(pm)) = (q.k.mid(), q.pm.mid()) {
        let dd_cents = (km - pm).abs() * 100.0;
        if dd_cents > cfg.mid_divergence_reject_cents {
            return Err(Reject::MidDivergence(dd_cents));
        }
    }

    // 4. edge sign + opt-in floor (L11 / L15 — only ever filter on <=0 or the explicit floor)
    if edge.net <= 0.0 {
        return Err(Reject::NonPositiveEdge);
    }
    if edge.net * 100.0 < cfg.edge_floor_cents {
        return Err(Reject::BelowEdgeFloor);
    }

    // 5. concurrency
    if exp.open_positions >= cfg.max_concurrent_positions {
        return Err(Reject::ConcurrencyCap);
    }

    // 6. sizing: min(displayed depth c2, per-pair contract cap, affordable, notional caps)
    let cost_per = (1.0 - edge.net).max(0.1); // ~$0.89..0.98 paid per $1 payout pair
    let mut size = q
        .depth
        .c2
        .min(cfg.max_contracts_per_pair)
        .min(affordable);

    let pair_room = cfg.max_notional_per_pair - exp.per_pair.get(&q.market).copied().unwrap_or(0.0);
    let clus_room =
        cfg.max_notional_per_cluster - exp.per_cluster.get(&q.cluster).copied().unwrap_or(0.0);
    let tot_room = cfg.max_total_notional - exp.total;

    let pair_cap = (pair_room.max(0.0) / cost_per).floor() as u32;
    let clus_cap = (clus_room.max(0.0) / cost_per).floor() as u32;
    let tot_cap = (tot_room.max(0.0) / cost_per).floor() as u32;

    if pair_cap == 0 {
        return Err(Reject::PairCap);
    }
    if clus_cap == 0 {
        return Err(Reject::ClusterCap);
    }
    if tot_cap == 0 {
        return Err(Reject::TotalCap);
    }
    size = size.min(pair_cap).min(clus_cap).min(tot_cap);

    if size < 1 {
        return Err(Reject::NoFillableSize); // a thin book is SMALL size, not no-trade (L15)
    }
    Ok(Approved {
        size,
        cost_per,
        edge: *edge,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn cfg() -> Config {
        // construct directly (Config::from_env reads env); these are the staged-rollout defaults.
        Config {
            mode: crate::config::ExecutionMode::DryRun,
            venue_env: crate::config::VenueEnv::Demo,
            kalshi_key_path: String::new(),
            pmus_env_path: String::new(),
            edge_floor_cents: 2.0,
            max_contracts_per_pair: 100,
            max_notional_per_pair: 1000.0,
            max_notional_per_cluster: 1000.0,
            max_total_notional: 1000.0,
            max_concurrent_positions: 5,
            max_book_age_s: 5.0,
            mid_divergence_reject_cents: 40.0,
            leg_fill_timeout_ms: 500,
            require_settle_clean: true,
            kill_switch: false,
        }
    }
    fn quote() -> Quote {
        Quote {
            market: "tc-temp-nychigh-2026-06-11-gte95f".into(),
            cat: Cat::Weather,
            pm: Book { yes_bid: Some(0.74), yes_ask: Some(0.75), age_s: 0.1 },
            k: Book { yes_bid: Some(0.86), yes_ask: Some(0.87), age_s: 0.0 },
            depth: Depth { c2: 50, c1: 60, c0: 70 },
            settle_clean: true,
            cluster: "nychigh-2026-06-11".into(),
        }
    }
    fn edge() -> Edge {
        Edge { net: 0.09, dir: Dir::PK }
    }

    #[test]
    fn approves_a_clean_weather_arb() {
        let r = evaluate(&cfg(), &quote(), &edge(), &Exposure::new(), 1000).unwrap();
        assert!(r.size >= 1 && r.size <= 50);
    }

    #[test]
    fn rejects_kill_switch() {
        let mut c = cfg();
        c.kill_switch = true;
        assert_eq!(evaluate(&c, &quote(), &edge(), &Exposure::new(), 1000), Err(Reject::KillSwitch));
    }

    #[test]
    fn rejects_crossed_book() {
        let mut q = quote();
        q.k.yes_bid = Some(0.90); // bid > ask -> crossed
        q.k.yes_ask = Some(0.80);
        assert_eq!(
            evaluate(&cfg(), &q, &edge(), &Exposure::new(), 1000),
            Err(Reject::CrossedBook(Venue::Kalshi))
        );
    }

    #[test]
    fn rejects_stale_book() {
        let mut q = quote();
        q.pm.age_s = 9.0;
        assert_eq!(
            evaluate(&cfg(), &q, &edge(), &Exposure::new(), 1000),
            Err(Reject::StaleBook(Venue::Pmus))
        );
    }

    #[test]
    fn rejects_unverified_econ_settlement() {
        let mut q = quote();
        q.cat = Cat::Econ;
        q.settle_clean = false;
        assert_eq!(
            evaluate(&cfg(), &q, &edge(), &Exposure::new(), 1000),
            Err(Reject::SettlementUnverified)
        );
    }

    #[test]
    fn rejects_non_positive_and_below_floor() {
        assert_eq!(
            evaluate(&cfg(), &quote(), &Edge { net: -0.01, dir: Dir::PK }, &Exposure::new(), 1000),
            Err(Reject::NonPositiveEdge)
        );
        assert_eq!(
            evaluate(&cfg(), &quote(), &Edge { net: 0.01, dir: Dir::PK }, &Exposure::new(), 1000),
            Err(Reject::BelowEdgeFloor)
        );
    }

    #[test]
    fn one_contract_cap_is_the_default_brake() {
        let mut c = cfg();
        c.max_contracts_per_pair = 1; // staged-rollout default
        let r = evaluate(&c, &quote(), &edge(), &Exposure::new(), 1000).unwrap();
        assert_eq!(r.size, 1);
    }
}
