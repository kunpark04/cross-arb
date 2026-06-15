use crate::config::Config;
use crate::exec::ExecutionBackend;
use crate::pair::LivePair;
use crate::pricing::{apply_second_leg_markup, build_legs};
use crate::risk::{evaluate, Exposure};
use crate::types::*;
use crate::{postpone, unwind};

/// Fire the hedged pair from an already-built pair of legs (the backend fires both concurrently). Thin
/// wrapper kept for the OFFLINE smoke path (synchronous, no spawn); the live loop SPAWNS `submit_pair` off
/// an `Arc<backend>` so the network RTT never blocks its `select!` (see `spawn_submit`).
fn fire_legs(backend: &dyn ExecutionBackend, legs: &[OrderIntent; 2]) {
    let _ = backend.submit_pair(&legs[0], &legs[1], true); // smoke: dry-run, pmus-first default
}

/// Stage-1 smoke: prove the risk+exec spine behaves on real-shaped snapshots (weather/econ/sports).
pub(crate) fn smoke(cfg: &Config, backend: &dyn ExecutionBackend) {
    // a 1:1 LivePair (weather/econ) builder for the smoke (kalshi_b = None).
    let lp = |slug: &str, kalshi: &str, cat: Cat, cluster: &str| LivePair {
        slug: slug.into(), kalshi: kalshi.into(), kalshi_b: None, cat, cluster: cluster.into(),
        settle_clean: false, soccer: false, days_to_event: None, pm_min_tick: None, pm_min_qty: None,
    };

    // (1) the live U-3 >=4.2 gap (pmus YES 0.75 / Kalshi YES 0.86) — but ECON, settlement NOT yet
    //     empirically verified (the cumulative-twin recon is 2026-07-02), so the gate MUST refuse it.
    let econ_pair = lp("urc-us-seasonadj-gte-june-2026-07-02-atl4pt2", "KXU3-26JUN-T4.1", Cat::Econ, "u3-2026-07-02");
    let econ = Quote {
        market: econ_pair.slug.clone(),
        cat: Cat::Econ,
        pm: Book { yes_bid: Some(0.60), yes_ask: Some(0.75), age_s: 1.0 },
        k: Book { yes_bid: Some(0.86), yes_ask: Some(0.87), age_s: 0.0 },
        k_b: None,
        depth: Depth { c2: 100, c1: 100, c0: 100 },
        settle_clean: false,
        cluster: "u3-2026-07-02".into(),
        led_by: None,
        days_to_event: None,
        fire_pmus_first: true,
    };
    println!("[smoke] econ U-3 9c gap, settlement NOT yet verified:");
    report(cfg, &econ_pair, &econ, Edge { net: 0.09, dir: Dir::PK }, backend);

    // (2) a clean, settlement-verified weather arb above the 2c floor, cheap-led (benign) -> approved.
    let wx_pair = lp("tc-temp-nychigh-2026-06-11-gte95f", "KXHIGHNY-26JUN11-T95", Cat::Weather, "nychigh-2026-06-11");
    let wx = Quote {
        market: wx_pair.slug.clone(),
        cat: Cat::Weather,
        pm: Book { yes_bid: Some(0.05), yes_ask: Some(0.07), age_s: 0.2 },
        k: Book { yes_bid: Some(0.10), yes_ask: Some(0.11), age_s: 0.0 },
        k_b: None,
        depth: Depth { c2: 40, c1: 50, c0: 60 },
        settle_clean: true,
        cluster: "nychigh-2026-06-11".into(),
        led_by: Some(Venue::Pmus), // cheap venue (dir PK) led -> benign
        days_to_event: None,
        fire_pmus_first: true,
    };
    println!("[smoke] weather arb, verified, 3c edge, cheap-led (benign):");
    report(cfg, &wx_pair, &wx, Edge { net: 0.03, dir: Dir::PK }, backend);

    // (3) same weather arb but DEAR-led -> the H1 toxicity-direction gate rejects it (weather-only).
    let mut wx_toxic = wx.clone();
    wx_toxic.led_by = Some(Venue::Kalshi); // dear venue (dir PK) led -> ~79% toxic
    println!("[smoke] weather arb, verified, 3c edge, DEAR-led (toxic):");
    report(cfg, &wx_pair, &wx_toxic, Edge { net: 0.03, dir: Dir::PK }, backend);

    // (4) SPORTS (2-outcome): pmus YES = LAD (team A); Kalshi-A = LAD ticker, Kalshi-B = PIT ticker. The
    //     game-proximity gate skips it 5 days out, takes it 1 day out. The PK legs are YES@pmus + YES@Kalshi-B.
    let mut sc = cfg.clone();
    sc.assume_sports_settled = true;
    let sport_pair = LivePair {
        slug: "aec-mlb-lad-pit-2026-06-16".into(),
        kalshi: "KXMLBGAME-26JUN16-LAD".into(),
        kalshi_b: Some("KXMLBGAME-26JUN16-PIT".into()),
        cat: Cat::Sports,
        cluster: "mlb-2026-06-16".into(),
        settle_clean: false,
        soccer: false,
        days_to_event: Some(5.0),
        pm_min_tick: None,
        pm_min_qty: None,
    };
    let sport = Quote {
        market: sport_pair.slug.clone(),
        cat: Cat::Sports,
        pm: Book { yes_bid: Some(0.54), yes_ask: Some(0.55), age_s: 0.3 }, // pmus YES = back LAD
        k: Book { yes_bid: Some(0.56), yes_ask: Some(0.58), age_s: 0.0 },  // Kalshi-A (LAD) book
        k_b: Some(Book { yes_bid: Some(0.40), yes_ask: Some(0.42), age_s: 0.0 }), // Kalshi-B (PIT) book
        depth: Depth { c2: 200, c1: 220, c0: 250 },
        settle_clean: false, // not actually reconciled — assumed via config
        cluster: "mlb-2026-06-16".into(),
        led_by: None,
        days_to_event: Some(5.0),
        fire_pmus_first: true,
    };
    println!("[smoke] sports arb (assumed-settled), 5 days pre-game -> event-proximity gate:");
    report(&sc, &sport_pair, &sport, Edge { net: 0.03, dir: Dir::PK }, backend);
    let mut sport_soon = sport.clone();
    sport_soon.days_to_event = Some(1.0);
    let mut sport_pair_soon = sport_pair.clone();
    sport_pair_soon.days_to_event = Some(1.0);
    println!("[smoke] sports arb (assumed-settled), 1 day pre-game -> within window:");
    report(&sc, &sport_pair_soon, &sport_soon, Edge { net: 0.03, dir: Dir::PK }, backend);

    // (4b) WORLD CUP (per-outcome BINARY): one outcome ("Germany wins") is its OWN binary co-listed pair —
    //      pmus YES (atc-fwc-ger-cuw-…-ger) + Kalshi NO (KXWCGAME-…-GER). kalshi_b=None + soccer=true, so it
    //      flows through the BINARY signal path (buy YES cheap + NO dear), NOT game_signal. settle_clean=true
    //      (regulation-clean), so the gate trades it on edge alone — 1 day pre-game (within the proximity
    //      window). This is exactly the weather/econ 1:1 shape, proving WC reuses that path unchanged.
    let wc_pair = LivePair {
        slug: "atc-fwc-ger-cuw-2026-06-14-ger".into(),
        kalshi: "KXWCGAME-26JUN14GERCUW-GER".into(),
        kalshi_b: None, // per-outcome BINARY -> routed via `signal`, never `game_signal`
        cat: Cat::Sports,
        cluster: "fwc-ger-cuw-2026-06-14".into(),
        settle_clean: true, // regulation-clean (the void tail is far below a tradeable edge)
        soccer: true,
        days_to_event: Some(1.0),
        pm_min_tick: None,
        pm_min_qty: None,
    };
    let wc = Quote {
        market: wc_pair.slug.clone(),
        cat: Cat::Sports,
        pm: Book { yes_bid: Some(0.40), yes_ask: Some(0.42), age_s: 0.3 }, // pmus YES (back "Germany wins") cheap
        k: Book { yes_bid: Some(0.45), yes_ask: Some(0.46), age_s: 0.0 },  // Kalshi YES dearer -> buy NO on Kalshi
        k_b: None, // BINARY: no away-team book (the whole point — WC is NOT the 2-team game model)
        depth: Depth { c2: 80, c1: 90, c0: 100 },
        settle_clean: true,
        cluster: wc_pair.cluster.clone(),
        led_by: None,
        days_to_event: Some(1.0),
        fire_pmus_first: true,
    };
    println!("[smoke] world-cup outcome arb (BINARY: pmus YES + Kalshi NO on one outcome), 1 day pre-game:");
    report(&sc, &wc_pair, &wc, Edge { net: 0.03, dir: Dir::PK }, backend);

    // (5) postponement unwind, LIVE PATH on a SYNTHETIC schedule (no network): a held MLB pair (dir PK:
    //     YES@pmus + YES@Kalshi-B) + a `snap`'d "Postponed, makeup 5d out" schedule game -> the DETECTOR
    //     fires -> should_unwind true -> print the two SELL unwind orders. This is the real stage-2 trigger
    //     composition (detect_postponement -> should_unwind -> unwind_orders), exercised offline.
    let held = crate::types::Position {
        market: "aec-mlb-lad-pit-2026-06-16".into(), cat: Cat::Sports,
        legs: [
            crate::types::PositionLeg { venue: Venue::Pmus, market: "aec-mlb-lad-pit-2026-06-16".into(), side: Side::Yes, ..Default::default() },
            crate::types::PositionLeg { venue: Venue::Kalshi, market: "KXMLBGAME-26JUN16-PIT".into(), side: Side::Yes, ..Default::default() },
        ],
        size: 10,
        cluster: "mlb-2026-06-16".into(),
    };
    // a synthetic statsapi schedule game: LAD@PIT on 2026-06-16, POSTPONED with a makeup 5 days out (the
    // 2d–2wk both-legs-loss gap). officialDate already moved to the makeup date (the live shape) — the
    // detector measures the gap from the BOUND event date (06-16), NOT officialDate (the L3 trap).
    let game: serde_json::Value = serde_json::from_str(
        r#"{"gamePk":1,"officialDate":"2026-06-21","gameDate":"2026-06-16T20:00:00Z",
            "status":{"detailedState":"Postponed","reason":"Rain"},
            "rescheduleDate":"2026-06-21T17:10:00Z",
            "teams":{"away":{"team":{"id":134}},"home":{"team":{"id":119}}}}"#,
    )
    .unwrap();
    let cur = postpone::snap(&game);
    println!("[smoke] postponement unwind LIVE path (snap -> detect -> should_unwind), makeup 5d out:");
    match postpone::detect_postponement(None, &cur, "2026-06-16", &held.market) {
        Some(p) if unwind::should_unwind(&p, cfg.kalshi_void_window_days) => {
            println!("  detected postponement: reschedule_in_days={:?} -> UNWIND (> {}d window)", p.reschedule_in_days, cfg.kalshi_void_window_days);
            // exit prices: stage-2 reads the live bids; here 1c placeholders (the smoke proves the firing).
            for o in &unwind::unwind_orders(&held, [1, 1]) {
                println!("  UNWIND {:?} {:?} {:?} {}x  market={}", o.action, o.venue, o.side, o.qty, o.market);
            }
        }
        _ => println!("  (no unwind — unexpected for this synthetic postponement)"),
    }
    println!();
}

fn report(cfg: &Config, pair: &LivePair, q: &Quote, edge: Edge, backend: &dyn ExecutionBackend) {
    match evaluate(cfg, q, &edge, &Exposure::new(), 1000) {
        Ok(a) => {
            println!("  APPROVED size={}  (edge {:.1}c @ {:.2}c/$-day, dir {:?})", a.size, edge.net * 100.0, a.edge_rate, edge.dir);
            // build both legs from the BOOKS (same unified path the live loop uses) and fire. The offline
            // smoke is always a fresh single position -> pos_index 0 (`xarb-{slug}-0-{tag}`).
            match build_legs(pair, q, edge.dir, a.size, 0) {
                Some(mut legs) => {
                    // mirror the live loop: pay up the SECOND (Kalshi) leg by the capped edge surplus (0020).
                    if cfg.aggressive_second_leg {
                        apply_second_leg_markup(cfg, &mut legs, true); // smoke: pmus-first default
                    }
                    fire_legs(backend, &legs);
                }
                None => println!("  (no two-sided book to price both legs)"),
            }
        }
        Err(r) => println!("  REJECTED: {:?}", r),
    }
    println!();
}
