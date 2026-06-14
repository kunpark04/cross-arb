//! Pre-trade risk gates — every edge case the project has learned, enforced BEFORE a single order is
//! sent. These are the SYNCHRONOUS gates (evaluated on a complete dual-venue snapshot). The
//! execution-TIME edge cases live elsewhere: MLB-postponement detection + unwind in `postpone.rs` +
//! `unwind.rs` + `main::handle_unwind` (BUILT); a one-legged live fill fail-closes in `main` (halt + log).
//! Leg-fill-timeout retry / partial-fill handling are NOT yet built (the naked-leg auto-recovery TODO).

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
    BelowEdgeRateFloor(f64), // opt-in 0014-H2 edge-RATE floor (¢/$-day); carries the rate (never silent, L15)
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
    pub edge_rate: f64, // booked edge ÷ lock-days (¢ per $-day) — the 0014-H2 velocity metric, always logged
}

// --- Edge-RATE (0014-H2) lock-day model -------------------------------------------------------------
// Corrected lock-day priors. The 2026-06-11 owner correction showed pmus credits cash AT GRADE (not at
// the +14d endDate), so capital is locked entry->grade ≈ `days_to_event`, NOT capital_velocity.py's
// passive 15/21d "hold to endDate". These are floors/fallbacks; SPORTS uses the live `days_to_event`
// directly (a game today ranks far above one 7 days out — the whole point of edge-rate). These are NOT
// the frozen 0014-H2 *backtest* priors (weather 1.2 / sports 15 / econ days-to-release) — those stay
// frozen for the confirmatory test (decision 0017); this is the LIVE bot's best-current model.
const LOCK_DAYS_WEATHER: f64 = 1.2; // settles ~same evening; days_to_event is 0/None for weather anyway
const LOCK_DAYS_SPORTS_FLOOR: f64 = 0.4; // a same-day game still locks ~to tonight's grade
const LOCK_DAYS_ECON_FALLBACK: f64 = 21.0; // days_to_event is None for econ (no release calendar in the
                                           // bot) -> ranks econ last, correctly (0017 follow-up: real cal)

/// Effective capital-lock horizon in days (entry -> grade) for the edge-RATE metric. ALWAYS finite and
/// `>=` a positive floor: `None` OR a corrupt non-finite `days_to_event` (NaN or ±inf) falls back to the
/// category prior, and a present finite value is clamped up to the floor. So `edge_rate = edge / lock_days`
/// can never divide by zero or go non-finite — even if the upstream proximity gate (which fail-closes NaN)
/// is disabled. (`f64::max` alone collapses NaN but would LEAK +inf through, hence the explicit `is_finite`.)
fn lock_days(cat: Cat, days_to_event: Option<f64>) -> f64 {
    let floored = |d: Option<f64>, floor: f64| match d {
        Some(x) if x.is_finite() => x.max(floor),
        _ => floor, // None or a corrupt non-finite time -> the category prior (keeps lock_days finite)
    };
    match cat {
        Cat::Weather => LOCK_DAYS_WEATHER,
        Cat::Sports => floored(days_to_event, LOCK_DAYS_SPORTS_FLOOR),
        Cat::Econ => floored(days_to_event, LOCK_DAYS_ECON_FALLBACK),
        Cat::Other => floored(days_to_event, LOCK_DAYS_ECON_FALLBACK),
    }
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
    //     A present-but-NON-FINITE value (NaN from a bad date subtraction) fails CLOSED: `NaN > max` is
    //     false in IEEE-754, which would let a corrupt lock-time defeat the capital-velocity gate, so we
    //     reject it explicitly (W5). `None` stays dormant by design.
    if cfg.max_days_to_event > 0.0 {
        if let Some(d) = q.days_to_event {
            if !d.is_finite() || d > cfg.max_days_to_event {
                return Err(Reject::TooEarly);
            }
        }
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

    // 3b. SPORTS away-team (`k_b`) divergence (C7): the team-B Kalshi book is a load-bearing hedge leg
    //     (PK fills YES@Kalshi-B), but step 3 only checks team-A vs pmus, so a stale/mislabeled team-B
    //     book (e.g. a doubleheader/duplicate-ticker misbind, or a one-sided book whose `mid()` collapses)
    //     was a blind spot on exactly the 2-outcome category where a bad join is most likely. The two
    //     single-team Kalshi YES prices must be mutually coherent with pmus: team-B implied ~= 1 - pmus_yes
    //     (since ka + kb ~= 1 and pm_yes ~= ka). Reject past the same cross-category ceiling. `None` k_b
    //     (weather/econ) skips this entirely.
    if let Some(kb) = q.k_b {
        if let (Some(kbm), Some(pm)) = (kb.mid(), q.pm.mid()) {
            let dd_cents = (kbm - (1.0 - pm)).abs() * 100.0;
            if dd_cents > cfg.mid_divergence_reject_cents {
                return Err(Reject::MidDivergence(dd_cents));
            }
        }
    }

    // 4. edge sign + opt-in floor (L11 / L15 — only ever filter on <=0 or the explicit floor)
    if edge.net <= 0.0 {
        return Err(Reject::NonPositiveEdge);
    }
    if edge.net * 100.0 < cfg.edge_floor_cents {
        return Err(Reject::BelowEdgeFloor);
    }

    // 4a. EDGE-RATE reservation floor (0014-H2): reserve scarce capital for high-VELOCITY arbs. A flat
    //     edge floor gets categories backwards — a 13¢ econ arb @ ~21d = 0.6¢/$-day is WORSE than a 3¢
    //     weather arb @ 1.2d = 2.5¢/$-day, yet the flat floor prefers the econ one. `edge_rate` is ALWAYS
    //     computed (returned in Approved so the live path can log it + the owner can calibrate the
    //     threshold against the real opportunity distribution); it only GATES when min_edge_rate_cpd > 0
    //     (opt-in, default 0 = OFF -> zero behavior change). Lock-days = the corrected days-to-grade model
    //     (decision 0017), NOT the frozen 0014-H2 backtest priors. Reservation-only: sizing is untouched.
    let edge_rate = edge.net * 100.0 / lock_days(q.cat, q.days_to_event); // ¢ per dollar-day
    if cfg.min_edge_rate_cpd > 0.0 && edge_rate < cfg.min_edge_rate_cpd {
        return Err(Reject::BelowEdgeRateFloor(edge_rate));
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
        edge_rate,
    })
}

// ====================================================================================================
// 3-LEG DUTCH-BOOK (World Cup) pre-trade gate — a PARALLEL of `evaluate`. The 2-leg `evaluate` is
// UNCHANGED. Same gate philosophy (cheap/structural rejects first, sizing last), adapted to a basket:
// the per-BASKET cost is `basket_cost` (sum of the 3 cheapest YES asks), and the fillable size is the
// MIN depth across the three outcomes' cheapest-venue ladders — all three legs must have depth to lock.
// ====================================================================================================

/// An approved 3-leg basket: the size to fire on each leg + the per-basket cash cost (`basket_cost`).
#[derive(Clone, Debug, PartialEq)]
pub struct ApprovedTriple {
    pub size: u32,
    pub cost_per: f64, // $ per locked basket = the dutch-book `basket_cost`
    pub net: f64,      // locked profit per $1 payout (the dutch-book net edge)
    pub edge_rate: f64, // booked edge ÷ lock-days (¢ per $-day) — same velocity metric as the 2-leg path
}

/// Run the full pre-trade gate for a 3-leg DUTCH-BOOK basket. `sig` is the `dutch_book` result (net edge +
/// basket cost); `affordable` = baskets the bankroll can fund at this basket cost. Mirrors `evaluate`'s
/// ordering + every shared gate (kill-switch, stream-pause, settlement identity, event-proximity, edge
/// sign + floor + rate, caps, depth/affordability/concurrency sizing) — WC is `Cat::Sports`, settlement
/// TAIL-clean (the per-basket void tail is already netted into `sig.net`). A basket fires ONLY when
/// `sig.net > 0` AND all three outcomes have depth (`q.depth` is the cross-outcome MIN — 0 if any leg is
/// empty). Crossed/stale books are rejected upstream in `dutch_book` (sets `no_arb`/`crossed`), so by the
/// time a positive `sig.net` reaches here the chosen books were non-crossed; this gate adds the
/// per-outcome STALENESS check the signal does not (a wedged-but-uncrossed book must not trade).
pub fn evaluate_triple(
    cfg: &Config,
    q: &SoccerTriple,
    sig: &crate::signal::DutchSignal,
    exp: &Exposure,
    affordable: u32,
) -> Result<ApprovedTriple, Reject> {
    // 0. global halts
    if cfg.kill_switch {
        return Err(Reject::KillSwitch);
    }
    if exp.stream_paused {
        return Err(Reject::StreamPaused);
    }

    // 1. settlement identity (invariant #1). WC is regulation-clean (settle_clean=true, the TAIL verdict);
    //    require_settle_clean still refuses a basket the discovery layer did NOT mark clean.
    let settle_ok = q.settle_clean || (cfg.assume_sports_settled);
    if cfg.require_settle_clean && !settle_ok {
        return Err(Reject::SettlementUnverified);
    }

    // 1b. event-proximity (capital velocity) — don't lock capital long before the game. NaN fails CLOSED
    //     (same W5 rule as `evaluate`). `None` stays dormant. (max_days_to_event <= 0 disables.)
    if cfg.max_days_to_event > 0.0 {
        if let Some(dte) = q.days_to_event {
            if !dte.is_finite() || dte > cfg.max_days_to_event {
                return Err(Reject::TooEarly);
            }
        }
    }

    // 2. per-outcome book sanity. Crossed books are already rejected in `dutch_book` (a crossed chosen
    //    book makes the signal no_arb), but STALENESS is a risk-layer gate: a wedged-but-uncrossed book on
    //    ANY of the three outcomes (either venue) ages out -> reject the whole basket (L13). The signal
    //    chose the cheaper venue per outcome, but a stale leg on the OTHER venue could still be the one we
    //    fire (the basket may mix venues), so check BOTH books of every outcome — the worst leg governs.
    for oc in &q.outcomes {
        for (b, venue) in [(&oc.q.pm, Venue::Pmus), (&oc.q.k, Venue::Kalshi)] {
            if b.crossed() {
                return Err(Reject::CrossedBook(venue));
            }
            if b.age_s > cfg.max_book_age_s {
                return Err(Reject::StaleBook(venue));
            }
        }
    }

    // 4. edge sign + opt-in floor (L11 / L15). `sig.net` already nets the per-basket void tail + fees.
    if sig.net <= 0.0 {
        return Err(Reject::NonPositiveEdge);
    }
    if sig.net * 100.0 < cfg.edge_floor_cents {
        return Err(Reject::BelowEdgeFloor);
    }

    // 4a. edge-RATE reservation floor (0014-H2) — reserve scarce capital for high-velocity arbs. WC is a
    //     near-dated game; `lock_days(Cat::Sports, days_to_event)` is the dynamic days-to-grade model.
    let edge_rate = sig.net * 100.0 / lock_days(Cat::Sports, q.days_to_event);
    if cfg.min_edge_rate_cpd > 0.0 && edge_rate < cfg.min_edge_rate_cpd {
        return Err(Reject::BelowEdgeRateFloor(edge_rate));
    }

    // 5. concurrency
    if exp.open_positions >= cfg.max_concurrent_positions {
        return Err(Reject::ConcurrencyCap);
    }

    // 6. sizing: min(displayed basket depth c2, per-pair contract cap, affordable, notional caps). The
    //    per-BASKET cost is `basket_cost` (NOT 1-edge — a basket pays $1 once but COSTS basket_cost, which
    //    differs from a 2-leg pair's `1 - net` only by the void-tail term; use the real basket cost).
    let cost_per = sig.basket_cost.max(0.01);
    let mut size = q.depth.c2.min(cfg.max_contracts_per_pair).min(affordable);

    let pair_room = cfg.max_notional_per_pair - exp.per_pair.get(&q.game).copied().unwrap_or(0.0);
    let clus_room = cfg.max_notional_per_cluster - exp.per_cluster.get(&q.cluster).copied().unwrap_or(0.0);
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

    // FAT-EDGE TOXICITY haircut — same blunt size-down above the knee as the 2-leg path (a fat basket edge
    // is as likely to be adversely-selected; never to 0 — L15).
    if sig.net * 100.0 >= cfg.fat_edge_knee_cents && cfg.fat_edge_size_factor < 1.0 {
        size = ((size as f64) * cfg.fat_edge_size_factor).floor().max(1.0) as u32;
    }

    if size < 1 {
        return Err(Reject::NoFillableSize);
    }
    Ok(ApprovedTriple { size, cost_per, net: sig.net, edge_rate })
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
            min_edge_rate_cpd: 0.0,
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
            postpone_poll_s: 60,
            auto_unwind: true,
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
    fn rejects_divergent_uncrossed_away_team_book() {
        // C7: a sports team-B (k_b) book that is FRESH + UNCROSSED but DIVERGENT from pmus (team-B implied
        // should be ~ 1 - pm_yes) is the bad-join/mislabel blind spot the crossed/stale gates miss. Here
        // pm mid = 0.55 -> team-B should imply ~0.45, but k_b mid is 0.05 -> 40c apart -> MidDivergence.
        let e = Edge { net: 0.03, dir: Dir::PK };
        let mut q = quote();
        q.cat = Cat::Sports;
        q.settle_clean = true;
        q.pm = Book { yes_bid: Some(0.54), yes_ask: Some(0.56), age_s: 0.1 }; // pm mid 0.55 -> B implied 0.45
        q.k = Book { yes_bid: Some(0.55), yes_ask: Some(0.57), age_s: 0.0 };  // team-A coherent w/ pm (no k-vs-pm trip)
        // team-B book fresh + uncrossed, but its mid (0.03) is 42c off the implied 0.45 -> > 40c -> rejected.
        q.k_b = Some(Book { yes_bid: Some(0.02), yes_ask: Some(0.04), age_s: 0.1 });
        match evaluate(&cfg(), &q, &e, &Exposure::new(), 1000) {
            Err(Reject::MidDivergence(d)) => assert!(d > cfg().mid_divergence_reject_cents),
            other => panic!("expected MidDivergence from the away-team book, got {:?}", other),
        }
        // a COHERENT team-B book (implied ~0.45) passes the C7 gate (other gates may apply, but not this).
        q.k_b = Some(Book { yes_bid: Some(0.44), yes_ask: Some(0.46), age_s: 0.1 });
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
    fn nan_days_to_event_fails_closed() {
        // W5: a present-but-NaN days_to_event (a bad date subtraction) must be REJECTED, not let through.
        // `NaN > max` is false in IEEE-754, so the old `map_or` passed it -> the capital-velocity gate
        // failed OPEN on exactly the input it can't trust. assume both reconciled so settlement doesn't gate.
        let e = Edge { net: 0.03, dir: Dir::PK };
        let mut c = cfg();
        c.assume_sports_settled = true;
        c.max_days_to_event = 2.0;
        let mut q = evt_quote(Cat::Sports, Some(f64::NAN));
        q.settle_clean = false; // relies on assume_sports_settled, isolating the proximity gate
        assert_eq!(evaluate(&c, &q, &e, &Exposure::new(), 1000), Err(Reject::TooEarly));
        // a finite in-window value still passes (the fix only rejects the non-finite case + the > max case).
        assert!(evaluate(&c, &evt_quote(Cat::Sports, Some(1.0)), &e, &Exposure::new(), 1000).is_ok());
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

    #[test]
    fn lock_days_uses_corrected_days_to_grade_priors() {
        // WEATHER: always the ~same-evening floor, whether days_to_event is None or a stale 0.0.
        assert_eq!(lock_days(Cat::Weather, None), LOCK_DAYS_WEATHER);
        assert_eq!(lock_days(Cat::Weather, Some(0.0)), LOCK_DAYS_WEATHER);
        // SPORTS: DYNAMIC from the live game date — the 2026-06-11 correction (was a flat passive 15d).
        assert_eq!(lock_days(Cat::Sports, Some(7.0)), 7.0);
        // ...floored, so a same-day game still locks ~to tonight's grade (never 0 -> never div-by-zero).
        assert_eq!(lock_days(Cat::Sports, Some(0.0)), LOCK_DAYS_SPORTS_FLOOR);
        // ECON: days_to_event is None today -> the fallback (correctly ranks econ last)...
        assert_eq!(lock_days(Cat::Econ, None), LOCK_DAYS_ECON_FALLBACK);
        // ...and a real release horizon would flow straight through (forward-compatible w/ a future calendar).
        assert_eq!(lock_days(Cat::Econ, Some(25.0)), 25.0);
        // NaN / negative collapse to the floor -> lock_days is ALWAYS finite & positive.
        for d in [Some(f64::NAN), Some(-3.0), Some(f64::INFINITY)] {
            let ld = lock_days(Cat::Sports, d);
            assert!(ld.is_finite() && ld > 0.0, "lock_days(Sports, {:?}) = {} must be finite & >0", d, ld);
        }
    }

    #[test]
    fn edge_rate_is_booked_edge_over_lock_days() {
        // a 3c weather arb @ 1.2d -> 2.5 c/$-day (computed + returned even with the gate OFF).
        let w = evaluate(&cfg(), &quote(), &Edge { net: 0.03, dir: Dir::PK }, &Exposure::new(), 1000).unwrap();
        assert!((w.edge_rate - 2.5).abs() < 1e-9, "weather edge_rate = {}", w.edge_rate);
        // a 13c econ arb @ the 21d fallback -> ~0.62 c/$-day: a FATTER edge that is a WORSE use of capital
        // than the 3c weather arb — exactly the inversion a flat edge floor gets backwards.
        let mut econ = quote();
        econ.cat = Cat::Econ;
        econ.settle_clean = true; // isolate from the settlement gate
        econ.days_to_event = None; // econ has no live release date -> fallback
        let e = evaluate(&cfg(), &econ, &Edge { net: 0.13, dir: Dir::PK }, &Exposure::new(), 1000).unwrap();
        assert!((e.edge_rate - 13.0 / 21.0).abs() < 1e-9, "econ edge_rate = {}", e.edge_rate);
        assert!(e.edge_rate < w.edge_rate, "13c econ must rank BELOW 3c weather on velocity");
    }

    #[test]
    fn edge_rate_floor_reserves_for_velocity_and_is_off_when_zero() {
        // the motivating pair: a fat-but-slow econ arb (0.62 c/$-day) vs a thin-but-fast weather arb (2.5).
        let mut econ = quote();
        econ.cat = Cat::Econ;
        econ.settle_clean = true;
        econ.days_to_event = None;
        let econ_edge = Edge { net: 0.13, dir: Dir::PK };

        // gate ON at 1.0 c/$-day: the fat-but-slow econ arb is RESERVED OUT (reported, never silent)...
        let mut c = cfg();
        c.min_edge_rate_cpd = 1.0;
        match evaluate(&c, &econ, &econ_edge, &Exposure::new(), 1000) {
            Err(Reject::BelowEdgeRateFloor(r)) => assert!((r - 13.0 / 21.0).abs() < 1e-9, "rate {}", r),
            other => panic!("expected BelowEdgeRateFloor, got {:?}", other),
        }
        // ...while the thin-but-FAST weather arb (2.5 > 1.0) still passes.
        assert!(evaluate(&c, &quote(), &Edge { net: 0.03, dir: Dir::PK }, &Exposure::new(), 1000).is_ok());

        // GATE OFF (min_edge_rate_cpd = 0.0): the SAME slow econ arb is approved -> the floor is a pure
        // opt-in skip (0 disables it). (The SHIPPED from_env default is 1.0 — enabled live 2026-06-13 —
        // but test configs keep it 0.0 to isolate the OTHER gates; see settlement/proximity tests.)
        assert!(evaluate(&cfg(), &econ, &econ_edge, &Exposure::new(), 1000).is_ok());
    }

    // ---- 3-LEG DUTCH-BOOK gate (evaluate_triple) -------------------------------------------------------

    use crate::signal::dutch_book;

    /// A WC triple quote with the three outcomes' books + a per-outcome depth, all settle-clean + fresh.
    /// `depths` are the per-outcome basket-leg depths (c2 buckets); the SoccerTriple carries the MIN.
    fn triple_quote(depths: [u32; 3]) -> SoccerTriple {
        let ocq = |tag: OutcomeTag, pma: f64, ka: f64| OutcomeQuote {
            tag,
            pm: Book { yes_bid: Some(pma - 0.02), yes_ask: Some(pma), age_s: 0.1 },
            k: Book { yes_bid: Some(ka - 0.02), yes_ask: Some(ka), age_s: 0.0 },
        };
        let min_d = depths.iter().copied().min().unwrap();
        SoccerTriple {
            game: "ger-cuw-2026-06-14".into(),
            outcomes: [
                TripleOutcome { tag: OutcomeTag::A, q: ocq(OutcomeTag::A, 0.42, 0.46) }, // A cheap pmus 0.42
                TripleOutcome { tag: OutcomeTag::D, q: ocq(OutcomeTag::D, 0.26, 0.20) }, // draw cheap Kalshi 0.20
                TripleOutcome { tag: OutcomeTag::B, q: ocq(OutcomeTag::B, 0.30, 0.35) }, // B cheap pmus 0.30
            ],
            cluster: "fwc-ger-cuw-2026-06-14".into(),
            settle_clean: true,
            depth: Depth { c2: min_d, c1: min_d, c0: min_d },
            days_to_event: Some(1.0),
        }
    }
    fn sig_of(q: &SoccerTriple) -> crate::signal::DutchSignal {
        dutch_book(&[q.outcomes[0].q, q.outcomes[1].q, q.outcomes[2].q], 0.005)
    }

    /// A clean WC basket (0.92 cost, net ~4c > the 2c floor) is APPROVED, sized at the MIN-of-3 depth.
    #[test]
    fn evaluate_triple_approves_and_sizes_to_min_depth() {
        let q = triple_quote([60, 30, 80]); // the THIN outcome (30) caps the basket
        let sig = sig_of(&q);
        assert!(!sig.no_arb, "the 0.92 basket locks");
        let a = evaluate_triple(&cfg(), &q, &sig, &Exposure::new(), 1000).unwrap();
        assert_eq!(a.size, 30, "basket size = min of the 3 outcomes' depth (the thin draw leg)");
        assert!((a.cost_per - sig.basket_cost).abs() < 1e-9, "cost_per is the basket cost");
        assert!(a.net > 0.02, "net clears the 2c floor");
    }

    /// If ANY outcome has ZERO depth, the basket cannot lock (the min-of-3 is 0) -> NoFillableSize. All
    /// three legs must have depth — a 2-of-3-deep basket is not a Dutch book.
    #[test]
    fn evaluate_triple_requires_all_three_to_have_depth() {
        let q = triple_quote([50, 0, 50]); // the draw leg has no depth
        let sig = sig_of(&q);
        assert_eq!(evaluate_triple(&cfg(), &q, &sig, &Exposure::new(), 1000), Err(Reject::NoFillableSize));
    }

    /// A non-positive / below-floor basket is rejected on edge sign + the floor (L11/L15), same as the
    /// 2-leg path. A basket summing to >= $1 nets <= 0 -> NonPositiveEdge.
    #[test]
    fn evaluate_triple_rejects_non_positive_and_below_floor() {
        // make every outcome expensive so the cheapest basket sums > $1 -> net <= 0. Set BOTH bid+ask
        // consistently (bid < ask) so no book is crossed (the book-sanity gate would fire first otherwise).
        let mut q = triple_quote([50, 50, 50]);
        for oc in q.outcomes.iter_mut() {
            oc.q.pm = Book { yes_bid: Some(0.38), yes_ask: Some(0.40), age_s: 0.1 };
            oc.q.k = Book { yes_bid: Some(0.40), yes_ask: Some(0.42), age_s: 0.0 };
        }
        let sig = sig_of(&q); // cheapest = pmus 0.40 each -> 1.20 -> negative
        assert_eq!(evaluate_triple(&cfg(), &q, &sig, &Exposure::new(), 1000), Err(Reject::NonPositiveEdge));
    }

    /// The shared gates fire on the basket: kill-switch, settlement-unverified (when not clean + not
    /// assumed), event-proximity (too early), and a stale outcome book.
    #[test]
    fn evaluate_triple_honors_shared_gates() {
        let q = triple_quote([50, 50, 50]);
        let sig = sig_of(&q);
        // kill-switch
        let mut c = cfg();
        c.kill_switch = true;
        assert_eq!(evaluate_triple(&c, &q, &sig, &Exposure::new(), 1000), Err(Reject::KillSwitch));
        // settlement unverified: settle_clean=false + not assumed -> rejected
        let mut q2 = triple_quote([50, 50, 50]);
        q2.settle_clean = false;
        assert_eq!(evaluate_triple(&cfg(), &q2, &sig_of(&q2), &Exposure::new(), 1000), Err(Reject::SettlementUnverified));
        // ...but assume_sports_settled lets it through
        let mut c3 = cfg();
        c3.assume_sports_settled = true;
        assert!(evaluate_triple(&c3, &q2, &sig_of(&q2), &Exposure::new(), 1000).is_ok());
        // event-proximity: 5 days out (> the 2d window) -> TooEarly
        let mut q3 = triple_quote([50, 50, 50]);
        q3.days_to_event = Some(5.0);
        assert_eq!(evaluate_triple(&cfg(), &q3, &sig_of(&q3), &Exposure::new(), 1000), Err(Reject::TooEarly));
        // a STALE outcome book (any of the 6) -> StaleBook
        let mut q4 = triple_quote([50, 50, 50]);
        q4.outcomes[1].q.k.age_s = 9.0; // the draw's Kalshi book wedged
        assert_eq!(evaluate_triple(&cfg(), &q4, &sig_of(&q4), &Exposure::new(), 1000), Err(Reject::StaleBook(Venue::Kalshi)));
    }

    /// The per-game notional cap binds the basket (cost_per = basket_cost): with $1/game and a ~0.92 basket
    /// only 1 basket fits per game, and a game already at its cap rejects with PairCap.
    #[test]
    fn evaluate_triple_caps_on_per_game_notional() {
        let q = triple_quote([100, 100, 100]);
        let sig = sig_of(&q);
        let mut c = cfg();
        c.max_notional_per_pair = 1.0; // $1 per game -> floor(1.0 / 0.92) = 1 basket
        let a = evaluate_triple(&c, &q, &sig, &Exposure::new(), 1000).unwrap();
        assert_eq!(a.size, 1, "the $1 per-game cap allows one ~0.92 basket");
        // the game already fully allocated -> PairCap.
        let mut exp = Exposure::new();
        exp.per_pair.insert(q.game.clone(), 1.0);
        assert_eq!(evaluate_triple(&c, &q, &sig, &exp, 1000), Err(Reject::PairCap));
    }
}
