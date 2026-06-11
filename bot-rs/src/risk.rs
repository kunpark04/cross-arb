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
    TooEarly,             // event-proximity gate: arb is too many days before settlement (capital velocity)
    CrossedBook(Venue),   // L12 — single-venue bid>ask phantom
    StaleBook(Venue),     // L13 — book older than max_book_age_s
    MidDivergence(f64),   // L1 — identical-settlement pair's mids disagree wildly (bad join/stale)
    NonPositiveEdge,      // L11 — never book net<=0
    BelowEdgeFloor,       // opt-in 0014 floor (skip thin arbs); always reported, never silent (L15)
    ToxicDirection,       // H1 — dear-led WEATHER edge (~79% toxic); weather-only, tested signal
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

    // 1. settlement identity (invariant #1) — the catastrophic both-legs-loss axis. Weather is
    //    empirically verified; econ/sports need settle_clean=true, OR the owner's reconciliation
    //    override per category (assume_sports_settled / assume_econ_settled). The residual VOID/
    //    postpone tail is handled by the unwind rule (`crate::unwind`), not this gate.
    let settle_ok = q.settle_clean
        || q.cat == Cat::Weather
        || (q.cat == Cat::Sports && cfg.assume_sports_settled)
        || (q.cat == Cat::Econ && cfg.assume_econ_settled);
    if cfg.require_settle_clean && !settle_ok {
        return Err(Reject::SettlementUnverified);
    }

    // 1b. EVENT-PROXIMITY gate (capital velocity) — don't lock capital long before the settlement
    //     EVENT (the game for sports, the release for econ); monitoring is free, capital only freezes
    //     on entry. Category-agnostic: it keys purely on `days_to_event`, so weather (event ~now ->
    //     days_to_event ~0 or None) naturally passes while a sports/econ arb weeks out is skipped.
    //     `days_to_event` is None until stage-2 computes it -> dormant. (max_days_to_event <= 0 = off.)
    if cfg.max_days_to_event > 0.0
        && q.days_to_event.map_or(false, |d| d > cfg.max_days_to_event)
    {
        return Err(Reject::TooEarly);
    }

    // 2. per-venue book sanity (L12 crossed, L13 stale). For SPORTS the AWAY-team Kalshi book (`k_b`) is
    //    a THIRD book the hedge fills against, so it gets the same crossed/stale gates as the other two —
    //    a stale/crossed Kalshi-B book is as fatal as a stale Kalshi-A book (both legs can leg out). It is
    //    `None` for weather/econ, so those paths are unchanged.
    if q.k.crossed() {
        return Err(Reject::CrossedBook(Venue::Kalshi));
    }
    if q.pm.crossed() {
        return Err(Reject::CrossedBook(Venue::Pmus));
    }
    if q.k_b.is_some_and(|kb| kb.crossed()) {
        return Err(Reject::CrossedBook(Venue::Kalshi));
    }
    if q.k.age_s > cfg.max_book_age_s {
        return Err(Reject::StaleBook(Venue::Kalshi));
    }
    if q.pm.age_s > cfg.max_book_age_s {
        return Err(Reject::StaleBook(Venue::Pmus));
    }
    if q.k_b.is_some_and(|kb| kb.age_s > cfg.max_book_age_s) {
        return Err(Reject::StaleBook(Venue::Kalshi));
    }

    // 3. cross-venue mid-divergence (L1): two settlement-identical legs should price close; a huge
    //    gap is usually a bad join or a stale quote. Econ TWINS are *exactly* settlement-identical, so
    //    they get a TIGHTER bound than the cross-category default — the 18¢ U-3 phantom sailed through
    //    the 40¢ guard (audit + both rust-reviews). A divergence past the bound on a thin/pre-release
    //    book is the stale/informed-book risk, not free money. (settle_clean still gates econ upstream.)
    if let (Some(km), Some(pm)) = (q.k.mid(), q.pm.mid()) {
        let dd_cents = (km - pm).abs() * 100.0;
        let ceiling = if q.cat == Cat::Econ {
            cfg.econ_twin_max_divergence_cents
        } else {
            cfg.mid_divergence_reject_cents
        };
        if dd_cents > ceiling {
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

    // 4b. TOXICITY-DIRECTION gate (H1 — the one tested idea that produced a signal; WEATHER-ONLY).
    //     A weather edge where the DEAR venue led the move is ~79% toxic vs ~17% for cheap-led; the
    //     cheap quote was right and you'd be adversely-selected onto the wrong leg. Skipping dear-led
    //     weather cut portfolio toxicity ~43%->31% at ~0c realized-edge cost (strategy-idea tests,
    //     Fisher p=2.7e-6, weather-only — sports is null, so this MUST stay weather-scoped). `led_by`
    //     is None until stage-2 tracks the prior book snapshot, so the gate is dormant until then.
    //     (Stage-2 may swap skip -> serial-lead-the-cheap-leg to KEEP the edge instead of skipping it.)
    if cfg.skip_dear_led_weather && q.cat == Cat::Weather && q.led_by == Some(edge.dir.dear_venue()) {
        return Err(Reject::ToxicDirection);
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

    // FAT-EDGE TOXICITY (rust trading review GAP-1 / readiness audit): edges past the knee are
    // adversely-selected (~66% toxic, die ~0.5s), so a bigger gap is NOT strictly better — size it
    // DOWN (but never to 0: L15 says toxic = small size, not skip; some fat edges are benign line-lag
    // and worth a probe). The stage-2 toxicity-DIRECTION gate (which venue led) replaces this blunt
    // haircut with a real benign-vs-informed decision. FAT_EDGE_SIZE_FACTOR=1.0 disables it — set it
    // to test whether a sub-0.5s concurrent two-leg fire can actually capture fat edges.
    if edge.net * 100.0 >= cfg.fat_edge_knee_cents && cfg.fat_edge_size_factor < 1.0 {
        size = ((size as f64) * cfg.fat_edge_size_factor).floor().max(1.0) as u32;
    }

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
            econ_twin_max_divergence_cents: 15.0,
            fat_edge_knee_cents: 6.0,
            fat_edge_size_factor: 0.5,
            skip_dear_led_weather: true,
            assume_sports_settled: false,
            assume_econ_settled: false,
            max_days_to_event: 2.0,
            kalshi_void_window_days: 2.0,
            leg_fill_timeout_ms: 500,
            require_settle_clean: true,
            discovery_refresh_s: 300,
            kill_switch: false,
        }
    }
    fn quote() -> Quote {
        Quote {
            market: "tc-temp-nychigh-2026-06-11-gte95f".into(),
            cat: Cat::Weather,
            pm: Book { yes_bid: Some(0.74), yes_ask: Some(0.75), age_s: 0.1 },
            k: Book { yes_bid: Some(0.86), yes_ask: Some(0.87), age_s: 0.0 },
            k_b: None, // weather is 1:1 — no away-team book
            depth: Depth { c2: 50, c1: 60, c0: 70 },
            settle_clean: true,
            cluster: "nychigh-2026-06-11".into(),
            led_by: None,        // unknown until stage-2 tracks the prior book snapshot
            days_to_event: None, // ~now for weather
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
    fn rejects_crossed_or_stale_away_team_book() {
        // SPORTS pair: the away-team Kalshi book (k_b) gets the same crossed + stale gates as k/pm.
        let e = Edge { net: 0.03, dir: Dir::PK };
        let mut q = quote();
        q.cat = Cat::Sports;
        q.settle_clean = true;
        // crossed k_b (bid > ask) -> CrossedBook(Kalshi)
        q.k_b = Some(Book { yes_bid: Some(0.60), yes_ask: Some(0.50), age_s: 0.0 });
        assert_eq!(evaluate(&cfg(), &q, &e, &Exposure::new(), 1000), Err(Reject::CrossedBook(Venue::Kalshi)));
        // stale k_b (old) -> StaleBook(Kalshi)
        q.k_b = Some(Book { yes_bid: Some(0.40), yes_ask: Some(0.45), age_s: 9.0 });
        assert_eq!(evaluate(&cfg(), &q, &e, &Exposure::new(), 1000), Err(Reject::StaleBook(Venue::Kalshi)));
        // a fresh, uncrossed k_b passes the book-sanity gates (other gates may still apply, but not these).
        q.k_b = Some(Book { yes_bid: Some(0.40), yes_ask: Some(0.45), age_s: 0.1 });
        assert!(evaluate(&cfg(), &q, &e, &Exposure::new(), 1000).is_ok());
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

    #[test]
    fn econ_twin_gets_a_tighter_divergence_bound() {
        // an 18c econ twin-mid divergence (the U-3 phantom) is REJECTED at the 15c econ bound,
        // even though it would pass the 40c cross-category guard. settle_clean=true isolates the test.
        let mut q = quote();
        q.cat = Cat::Econ;
        q.settle_clean = true;
        q.pm = Book { yes_bid: Some(0.66), yes_ask: Some(0.69), age_s: 0.1 }; // mid 0.675
        q.k = Book { yes_bid: Some(0.85), yes_ask: Some(0.86), age_s: 0.0 }; // mid 0.855 -> 18c apart
        match evaluate(&cfg(), &q, &edge(), &Exposure::new(), 1000) {
            Err(Reject::MidDivergence(d)) => assert!(d > 15.0),
            other => panic!("expected MidDivergence, got {:?}", other),
        }
        // the same 18c gap on a WEATHER pair passes (cross-category bound is 40c)
        let mut w = quote();
        w.pm = Book { yes_bid: Some(0.66), yes_ask: Some(0.69), age_s: 0.1 };
        w.k = Book { yes_bid: Some(0.85), yes_ask: Some(0.86), age_s: 0.0 };
        assert!(evaluate(&cfg(), &w, &edge(), &Exposure::new(), 1000).is_ok());
    }

    fn evt_quote(cat: Cat, days_to_event: Option<f64>) -> Quote {
        let mut q = quote();
        q.cat = cat;
        q.market = "aec-mlb-lad-pit-2026-06-14".into();
        q.cluster = "mlb-lad-pit-2026-06-14".into();
        q.settle_clean = false; // not yet reconciled
        q.days_to_event = days_to_event;
        q
    }

    #[test]
    fn settlement_assumed_per_category_then_event_proximity_governs() {
        let e = Edge { net: 0.03, dir: Dir::PK };
        // default: sports + econ settlement unverified -> rejected regardless of timing
        assert_eq!(
            evaluate(&cfg(), &evt_quote(Cat::Sports, Some(1.0)), &e, &Exposure::new(), 1000),
            Err(Reject::SettlementUnverified)
        );
        assert_eq!(
            evaluate(&cfg(), &evt_quote(Cat::Econ, Some(1.0)), &e, &Exposure::new(), 1000),
            Err(Reject::SettlementUnverified)
        );
        // owner asserts BOTH reconciled -> now the event-proximity gate governs, for both
        let mut c = cfg();
        c.assume_sports_settled = true;
        c.assume_econ_settled = true;
        c.max_days_to_event = 2.0;
        for cat in [Cat::Sports, Cat::Econ] {
            // 5 days before settlement -> TOO EARLY (would freeze capital early)
            assert_eq!(
                evaluate(&c, &evt_quote(cat, Some(5.0)), &e, &Exposure::new(), 1000),
                Err(Reject::TooEarly),
                "cat {:?} should be TooEarly at 5d",
                cat
            );
            // 1 day before -> within the window -> approved
            assert!(evaluate(&c, &evt_quote(cat, Some(1.0)), &e, &Exposure::new(), 1000).is_ok());
            // unknown event time (None) -> gate dormant, keeps it (stage-2 populates days_to_event)
            assert!(evaluate(&c, &evt_quote(cat, None), &e, &Exposure::new(), 1000).is_ok());
        }
        // gate disabled (<=0) -> a 5-day-early arb is allowed
        c.max_days_to_event = 0.0;
        assert!(evaluate(&c, &evt_quote(Cat::Sports, Some(5.0)), &e, &Exposure::new(), 1000).is_ok());
        // weather (event ~now -> days_to_event None) is never gated by proximity
        assert!(evaluate(&cfg(), &quote(), &e, &Exposure::new(), 1000).is_ok());
    }

    #[test]
    fn toxicity_direction_gate_is_weather_only_and_dear_led() {
        // edge dir PK => cheap=Pmus, dear=Kalshi. A weather edge LED BY the dear venue (Kalshi) is the
        // ~79%-toxic class -> rejected. Same edge cheap-led (Pmus) or unknown (None) -> allowed.
        let e = Edge { net: 0.03, dir: Dir::PK };
        let mut dear_led = quote();
        dear_led.led_by = Some(Venue::Kalshi); // dear venue led -> toxic
        assert_eq!(evaluate(&cfg(), &dear_led, &e, &Exposure::new(), 1000), Err(Reject::ToxicDirection));

        let mut cheap_led = quote();
        cheap_led.led_by = Some(Venue::Pmus); // cheap venue led -> benign, allowed
        assert!(evaluate(&cfg(), &cheap_led, &e, &Exposure::new(), 1000).is_ok());

        // sports is NULL for this signal -> the gate must NOT fire even when dear-led
        let mut sport = quote();
        sport.cat = Cat::Sports;
        sport.settle_clean = true;
        sport.led_by = Some(Venue::Kalshi);
        assert!(evaluate(&cfg(), &sport, &e, &Exposure::new(), 1000).is_ok());

        // knob off -> dear-led weather allowed (e.g. to A/B the gate or run serial-lead-cheap instead)
        let mut c = cfg();
        c.skip_dear_led_weather = false;
        assert!(evaluate(&c, &dear_led, &e, &Exposure::new(), 1000).is_ok());
    }

    #[test]
    fn fat_edge_is_sized_down_but_not_skipped() {
        let mut c = cfg();
        c.max_contracts_per_pair = 100;
        c.fat_edge_size_factor = 0.5;
        // thin edge (3c, below the 6c knee): full size up to depth (50)
        let thin = evaluate(&c, &quote(), &Edge { net: 0.03, dir: Dir::PK }, &Exposure::new(), 1000).unwrap();
        // fat edge (10c, above the knee): same depth, but sized DOWN by the factor
        let fat = evaluate(&c, &quote(), &Edge { net: 0.10, dir: Dir::PK }, &Exposure::new(), 1000).unwrap();
        assert!(fat.size < thin.size && fat.size >= 1, "fat={} thin={}", fat.size, thin.size);
        // factor=1.0 disables the haircut (speed-capture test mode)
        c.fat_edge_size_factor = 1.0;
        let chase = evaluate(&c, &quote(), &Edge { net: 0.10, dir: Dir::PK }, &Exposure::new(), 1000).unwrap();
        assert_eq!(chase.size, thin.size);
    }
}
