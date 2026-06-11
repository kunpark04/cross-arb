//! cross-arb live trading bot — entry point. **SAFE BY DEFAULT** (decision 0015):
//!   * dry-run unless `EXECUTION_MODE=live`
//!   * demo/sandbox venue unless `VENUE_ENV=prod` (+ an explicit informed-consent env for prod)
//!   * 1-contract / tiny-notional caps + global kill-switch
//!   * the read-write key is loaded from the owner's path at RUNTIME; never read/copied by Claude
//! Live order submission runs in the OWNER's environment (Claude's sandbox blocks real submission).
//! See `bot-rs/README.md`.
#![allow(dead_code)] // stage-1 spine: several domain fields/variants are wired in stage 2 (venue I/O)

mod auth;
mod config;
mod exec;
mod ledger;
mod risk;
mod types;
mod unwind;

use config::{Config, ExecutionMode, VenueEnv};
use exec::{DryRunBackend, ExecutionBackend, LiveBackend};
use risk::{evaluate, Exposure};
use types::*;

fn main() {
    dotenvy::dotenv().ok(); // load bot-rs/.env (key paths + safety vars) if present
    let cfg = Config::from_env();
    banner(&cfg);

    // Hard gate: live + production requires an explicit informed-consent env, else refuse to start.
    if cfg.is_live() && cfg.is_prod() && std::env::var("CROSSARB_I_UNDERSTAND_PROD").is_err() {
        eprintln!("\nREFUSING TO START: EXECUTION_MODE=live + VENUE_ENV=prod requires");
        eprintln!("CROSSARB_I_UNDERSTAND_PROD=yes. Staged rollout (decision 0015):");
        eprintln!("  dry-run  ->  demo sandbox  ->  1-contract prod  ->  scale (only after 0014 validates).");
        std::process::exit(2);
    }

    let mut backend: Box<dyn ExecutionBackend> = match cfg.mode {
        ExecutionMode::DryRun => Box::new(DryRunBackend),
        ExecutionMode::Live => Box::new(LiveBackend::new(&cfg)),
    };
    println!("execution backend : {}\n", backend.label());

    // STAGE 2 (owner env): connect pmus + Kalshi WS (auth), maintain books, run the
    //   match -> signal -> risk -> exec -> leg-sequence/unwind loop.
    // STAGE 1 (here): run synthetic complete dual-venue snapshots through the real risk+exec spine.
    smoke(&cfg, backend.as_mut());
}

fn banner(cfg: &Config) {
    println!("============================================================");
    println!(" cross-arb live bot   (decision 0015 - SAFE BY DEFAULT)");
    println!("============================================================");
    let mode_note = if matches!(cfg.mode, ExecutionMode::DryRun) {
        "   (no orders sent; EXECUTION_MODE=live to arm)"
    } else {
        ""
    };
    println!("execution mode    : {:?}{}", cfg.mode, mode_note);
    let venue_note = if matches!(cfg.venue_env, VenueEnv::Prod) {
        "   *** REAL MONEY ***"
    } else {
        "   (sandbox)"
    };
    println!("venue env         : {:?}{}", cfg.venue_env, venue_note);
    println!("edge floor        : {:.1}c", cfg.edge_floor_cents);
    println!(
        "caps              : {} ctr/pair, ${:.0}/pair, ${:.0}/cluster, ${:.0} total, {} concurrent",
        cfg.max_contracts_per_pair,
        cfg.max_notional_per_pair,
        cfg.max_notional_per_cluster,
        cfg.max_total_notional,
        cfg.max_concurrent_positions
    );
    println!(
        "guards            : age<={}s, mid-div<={}c, leg-fill<={}ms, settle-clean={}",
        cfg.max_book_age_s, cfg.mid_divergence_reject_cents, cfg.leg_fill_timeout_ms, cfg.require_settle_clean
    );
    if cfg.kill_switch {
        println!("KILL SWITCH       : ENGAGED (CROSSARB_KILL) - no trading");
    }
    println!();
}

/// Stage-1 smoke: prove the risk+exec spine behaves on two real-shaped snapshots.
fn smoke(cfg: &Config, backend: &mut dyn ExecutionBackend) {
    // (1) the live U-3 >=4.2 gap (pmus YES 0.75 / Kalshi YES 0.86) — but ECON, settlement NOT yet
    //     empirically verified (the cumulative-twin recon is 2026-07-02), so the gate MUST refuse it.
    let econ = Quote {
        market: "urc-us-seasonadj-gte-june-2026-07-02-atl4pt2".into(),
        cat: Cat::Econ,
        pm: Book { yes_bid: Some(0.60), yes_ask: Some(0.75), age_s: 1.0 },
        k: Book { yes_bid: Some(0.86), yes_ask: Some(0.87), age_s: 0.0 },
        depth: Depth { c2: 100, c1: 100, c0: 100 },
        settle_clean: false,
        cluster: "u3-2026-07-02".into(),
        led_by: None,
        days_to_event: None,
    };
    println!("[smoke] econ U-3 9c gap, settlement NOT yet verified:");
    report(cfg, &econ, Edge { net: 0.09, dir: Dir::PK }, 75, backend);

    // (2) a clean, settlement-verified weather arb above the 2c floor, cheap-led (benign) -> approved.
    let wx = Quote {
        market: "tc-temp-nychigh-2026-06-11-gte95f".into(),
        cat: Cat::Weather,
        pm: Book { yes_bid: Some(0.05), yes_ask: Some(0.07), age_s: 0.2 },
        k: Book { yes_bid: Some(0.10), yes_ask: Some(0.11), age_s: 0.0 },
        depth: Depth { c2: 40, c1: 50, c0: 60 },
        settle_clean: true,
        cluster: "nychigh-2026-06-11".into(),
        led_by: Some(Venue::Pmus), // cheap venue (dir PK) led -> benign
        days_to_event: None,
    };
    println!("[smoke] weather arb, verified, 3c edge, cheap-led (benign):");
    report(cfg, &wx, Edge { net: 0.03, dir: Dir::PK }, 7, backend);

    // (3) same weather arb but DEAR-led -> the H1 toxicity-direction gate rejects it (weather-only).
    let mut wx_toxic = wx.clone();
    wx_toxic.led_by = Some(Venue::Kalshi); // dear venue (dir PK) led -> ~79% toxic
    println!("[smoke] weather arb, verified, 3c edge, DEAR-led (toxic):");
    report(cfg, &wx_toxic, Edge { net: 0.03, dir: Dir::PK }, 7, backend);

    // (4) SPORTS, owner-assumed reconciled: the game-proximity gate skips it 5 days out, takes it 1 day out.
    let mut sc = cfg.clone();
    sc.assume_sports_settled = true;
    let sport = Quote {
        market: "aec-mlb-lad-pit-2026-06-16".into(),
        cat: Cat::Sports,
        pm: Book { yes_bid: Some(0.54), yes_ask: Some(0.55), age_s: 0.3 },
        k: Book { yes_bid: Some(0.58), yes_ask: Some(0.59), age_s: 0.0 },
        depth: Depth { c2: 200, c1: 220, c0: 250 },
        settle_clean: false, // not actually reconciled — assumed via config
        cluster: "mlb-lad-pit-2026-06-16".into(),
        led_by: None,
        days_to_event: Some(5.0),
    };
    println!("[smoke] sports arb (assumed-settled), 5 days pre-game -> event-proximity gate:");
    report(&sc, &sport, Edge { net: 0.03, dir: Dir::PK }, 55, backend);
    let mut sport_soon = sport.clone();
    sport_soon.days_to_event = Some(1.0);
    println!("[smoke] sports arb (assumed-settled), 1 day pre-game -> within window:");
    report(&sc, &sport_soon, Edge { net: 0.03, dir: Dir::PK }, 55, backend);

    // (5) postponement unwind: a held MLB pair + a postponement with an unknown/late reschedule ->
    //     flatten BOTH legs (SELL) before Kalshi voids. (Stage-2 wires live statsapi detection.)
    let held = types::Position {
        market: "aec-mlb-lad-pit-2026-06-16".into(), cat: Cat::Sports,
        yes_venue: Venue::Pmus, no_venue: Venue::Kalshi, size: 10,
        cluster: "mlb-lad-pit-2026-06-16".into(),
    };
    let postponed = vec![unwind::Postponement {
        market: held.market.clone(), reschedule_in_days: None, // makeup unknown -> Kalshi will void
    }];
    println!("[smoke] postponement unwind (held MLB pair, makeup unknown):");
    for pair in unwind::postponement_unwinds(&[held], &postponed, cfg.kalshi_void_window_days) {
        for o in &pair {
            println!("  UNWIND {:?} {:?} {:?} {}x  market={}", o.action, o.venue, o.side, o.qty, o.market);
        }
    }
    println!();
}

fn report(cfg: &Config, q: &Quote, edge: Edge, buy_price_cents: u8, backend: &mut dyn ExecutionBackend) {
    match evaluate(cfg, q, &edge, &Exposure::new(), 1000) {
        Ok(a) => {
            println!("  APPROVED size={}  (edge {:.1}c, dir {:?})", a.size, edge.net * 100.0, edge.dir);
            // fire BOTH legs of the hedge as a PAIR: buy YES on the cheap venue, buy NO on the dear one.
            let (yes_venue, no_venue) = match edge.dir {
                Dir::PK => (Venue::Pmus, Venue::Kalshi),
                Dir::KP => (Venue::Kalshi, Venue::Pmus),
            };
            let leg_yes = OrderIntent {
                venue: yes_venue, market: q.market.clone(), action: Action::Buy, side: Side::Yes,
                price_cents: buy_price_cents, qty: a.size,
                client_order_id: format!("smoke-{}-Y", q.market),
            };
            let leg_no = OrderIntent {
                venue: no_venue, market: q.market.clone(), action: Action::Buy, side: Side::No,
                price_cents: 100u8.saturating_sub(buy_price_cents).max(1), qty: a.size,
                client_order_id: format!("smoke-{}-N", q.market),
            };
            let _ = backend.submit_pair(&leg_yes, &leg_no);
        }
        Err(r) => println!("  REJECTED: {:?}", r),
    }
    println!();
}
