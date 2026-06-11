//! cross-arb live trading bot — entry point. **SAFE BY DEFAULT** (decision 0015):
//!   * dry-run unless `EXECUTION_MODE=live`
//!   * demo/sandbox venue unless `VENUE_ENV=prod` (+ an explicit informed-consent env for prod)
//!   * 1-contract / tiny-notional caps + global kill-switch
//!   * the read-write key is loaded from the owner's path at RUNTIME; never read/copied by Claude
//! Live order submission runs in the OWNER's environment (Claude's sandbox blocks real submission).
//! See `bot-rs/README.md`.
#![allow(dead_code)] // stage-1 spine: several domain fields/variants are wired in stage 2 (venue I/O)

mod auth;
mod book;
mod config;
mod discovery;
mod exec;
mod ledger;
mod matcher;
mod risk;
mod signal;
mod types;
mod unwind;
mod venue;

use config::{Config, ExecutionMode, VenueEnv};
use exec::{DryRunBackend, ExecutionBackend, LiveBackend};
use risk::{evaluate, Exposure};
use types::*;

#[tokio::main]
async fn main() {
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

    // STAGE-1 smoke vs STAGE-2 live loop. The smoke runs the real risk+exec spine on synthetic
    // snapshots (no network) and is the path of least resistance: it runs when `--smoke` is passed OR
    // when venue creds aren't available (Claude's sandbox / a fresh checkout). The live loop connects
    // both venue WS streams and runs the match->signal->risk->exec pipeline (decision 0015 gated rails).
    let force_smoke = std::env::args().any(|a| a == "--smoke");
    match (force_smoke, venue::VenueCreds::from_env()) {
        (false, Ok(creds)) => {
            run_live(&cfg, backend.as_mut(), std::sync::Arc::new(creds)).await;
        }
        (_, Err(why)) if !force_smoke => {
            println!("[startup] venue creds unavailable ({why}) -> running offline smoke instead.\n");
            smoke(&cfg, backend.as_mut());
        }
        _ => smoke(&cfg, backend.as_mut()),
    }
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

/// A co-listed pair the live loop tracks: the pmus slug + its settlement-identical Kalshi twin (1:1).
/// Filled by `discovery` (the colisted_map.py port). Sports' two-ticker shape is out of this 1:1 loop's
/// scope — discovery counts it but does not emit it here (see `discovery` module docs).
#[derive(Clone, Debug)]
struct LivePair {
    slug: String,         // pmus market slug
    kalshi: String,       // Kalshi ticker (the 1:1 twin)
    cat: Cat,
    cluster: String,
    settle_clean: bool,
    days_to_event: Option<f64>,
}

impl From<discovery::Pair> for LivePair {
    fn from(p: discovery::Pair) -> Self {
        LivePair { slug: p.slug, kalshi: p.kalshi, cat: p.cat, cluster: p.cluster, settle_clean: p.settle_clean, days_to_event: p.days_to_event }
    }
}

/// Shared, mutable pair state the event loop READS and the refresh task MUTATES (add new pairs / drop
/// settled). `by_slug` is the authoritative pair record; `by_ticker` indexes the Kalshi side.
#[derive(Default)]
struct PairState {
    by_slug: std::collections::HashMap<String, LivePair>,
    by_ticker: std::collections::HashMap<String, String>, // Kalshi ticker -> pmus slug
}

impl PairState {
    fn insert(&mut self, p: LivePair) {
        self.by_ticker.insert(p.kalshi.clone(), p.slug.clone());
        self.by_slug.insert(p.slug.clone(), p);
    }
    fn remove(&mut self, slug: &str) {
        if let Some(p) = self.by_slug.remove(slug) {
            self.by_ticker.remove(&p.kalshi);
        }
    }
}

/// STAGE-2 live loop: discover the co-listed universe, connect both venue WS streams, maintain a book
/// per venue for each tracked pair, and on each COMPLETE dual-venue update (L5) build a `Quote`, run
/// `risk::evaluate`, and `submit_pair` (dry-run default). A periodic refresh task re-discovers and
/// applies in-place subscribe add/prune. Honors the kill-switch + the already-checked prod-consent gate.
async fn run_live(cfg: &Config, backend: &mut dyn ExecutionBackend, creds: std::sync::Arc<venue::VenueCreds>) {
    use std::collections::{HashMap, HashSet};
    use std::sync::{Arc, Mutex};

    let http = reqwest::Client::builder().use_rustls_tls().build().unwrap_or_else(|_| reqwest::Client::new());

    // INITIAL DISCOVERY (PUBLIC, no-auth catalog pull). A degraded/empty first pass is not fatal — seed
    // with whatever discovery returns (possibly nothing) and let the refresh task fill in; the loop never
    // invents a universe to trade.
    let initial = match discovery::discover(&http).await {
        Ok(d) => {
            println!(
                "[discovery] {} weather + {} econ pairs ({} sports matched, not subscribed in the 1:1 loop)",
                d.weather_pairs, d.econ_pairs, d.sports_pairs
            );
            report_coverage(&d);
            d.pairs
        }
        Err(e) => {
            println!("[discovery] initial pull failed ({e}) — starting empty; refresh will retry.");
            Vec::new()
        }
    };

    let pairs: Arc<Mutex<PairState>> = Arc::new(Mutex::new(PairState::default()));
    let k_tracked: Arc<Mutex<HashSet<String>>> = Arc::new(Mutex::new(HashSet::new()));
    let pm_tracked: Arc<Mutex<HashSet<String>>> = Arc::new(Mutex::new(HashSet::new()));
    {
        let mut ps = pairs.lock().unwrap();
        let mut kt = k_tracked.lock().unwrap();
        let mut pt = pm_tracked.lock().unwrap();
        for p in initial {
            kt.insert(p.kalshi.clone());
            pt.insert(p.slug.clone());
            ps.insert(LivePair::from(p));
        }
    }

    // book stores: Kalshi books are owned by the kalshi_stream (shared so the loop can read touches);
    // pmus books are rebuilt here from each frame's (bids, asks).
    let kalshi_books: Arc<Mutex<HashMap<String, book::KalshiBook>>> = Arc::new(Mutex::new(HashMap::new()));
    let mut pmus_books: HashMap<String, book::PmusBook> = HashMap::new();
    let mut prior_mid: HashMap<String, (Option<f64>, Option<f64>)> = HashMap::new(); // slug -> (pm_mid, k_mid) for led_by
    let mut exposure = Exposure::new();

    let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel::<venue::VenueEvent>();
    let (k_subs_tx, k_subs_rx) = tokio::sync::mpsc::unbounded_channel::<venue::SubUpdate>();
    let (pm_subs_tx, pm_subs_rx) = tokio::sync::mpsc::unbounded_channel::<venue::SubUpdate>();

    tokio::spawn(venue::kalshi_stream(creds.clone(), k_tracked.clone(), kalshi_books.clone(), tx.clone(), k_subs_rx));
    tokio::spawn(venue::pmus_stream(creds.clone(), pm_tracked.clone(), tx.clone(), pm_subs_rx));
    tokio::spawn(refresh_loop(
        http.clone(),
        cfg.discovery_refresh_s,
        pairs.clone(),
        k_tracked.clone(),
        pm_tracked.clone(),
        kalshi_books.clone(),
        k_subs_tx,
        pm_subs_tx,
    ));
    drop(tx); // the spawned tasks hold their own senders; drop ours so rx closes if both ever end

    // A venue WS reconnect/seq-gap pauses trading until THAT venue's books rebuild (never trade a
    // half-rebuilt book). Tracked PER VENUE: a pmus frame must not clear a Kalshi rebuild pause, and
    // vice-versa. `stream_paused` stays true while EITHER venue is mid-rebuild.
    let (mut k_rebuild, mut pm_rebuild) = (false, false);

    while let Some(ev) = rx.recv().await {
        // which pmus slug does this event touch? (book updates first, then evaluate on the complete state)
        let slug = match &ev {
            venue::VenueEvent::Kalshi { ticker } => {
                k_rebuild = false; // a Kalshi book frame -> its rebuild is flowing again
                pairs.lock().unwrap().by_ticker.get(ticker).cloned()
            }
            venue::VenueEvent::Pmus { slug, bids, asks } => {
                pm_rebuild = false; // a pmus book frame -> its rebuild is flowing again
                if pm_tracked.lock().unwrap().contains(slug) {
                    pmus_books.entry(slug.clone()).or_default().apply_snapshot(bids, asks);
                    Some(slug.clone())
                } else {
                    pmus_books.remove(slug); // a settled/pruned slug still streaming -> free its book (L20)
                    None
                }
            }
            venue::VenueEvent::Reconnect { venue: v, clean } => {
                println!("[live] {v:?} reconnect (clean={clean}) — pausing entries until its books rebuild");
                match v {
                    Venue::Kalshi => k_rebuild = true,
                    Venue::Pmus => pm_rebuild = true,
                }
                exposure.stream_paused = k_rebuild || pm_rebuild;
                continue;
            }
            venue::VenueEvent::SeqGap { seq } => {
                println!("[live] Kalshi seq gap at {seq} — connection cycling; Kalshi entries paused");
                k_rebuild = true;
                exposure.stream_paused = true;
                continue;
            }
        };
        exposure.stream_paused = k_rebuild || pm_rebuild; // recompute after a (possibly) clearing frame
        let Some(slug) = slug else { continue }; // a Kalshi ticker we don't track
        // clone the pair record out so we don't hold the pairs lock across the Quote build / book locks
        // (the refresh task may be mutating the map concurrently).
        let Some(pair) = pairs.lock().unwrap().by_slug.get(&slug).cloned() else { continue };

        // build the COMPLETE dual-venue Quote (L5: classify on both venues' current state, not one frame).
        // Each touch carries its book's REAL staleness `age` (seconds since its last applied frame), so a
        // wedged stream ages its leg out and `risk::evaluate` fires `Reject::StaleBook` (L13). The gate
        // checks BOTH legs, so the worse (older) leg governs the pair's staleness.
        let pm = match pmus_books.get(&slug) {
            Some(b) => b.touch(),
            None => continue, // no pmus book yet -> incomplete, wait
        };
        let (k, depth_dir, edge) = {
            let kb = kalshi_books.lock().unwrap();
            let Some(kbook) = kb.get(&pair.kalshi) else { continue }; // no Kalshi book yet -> incomplete
            let k = kbook.touch();
            let pmb = pmus_books.get(&slug).unwrap();
            let sig = signal::signal(&pm, &k);
            let depth = book::depth_at_edge(kbook, pmb, sig.edge.dir);
            (k, depth, sig.edge)
        };

        // led_by: which venue's mid MOVED to open/realign this edge, vs the prior snapshot (H1 input).
        let led_by = led_by_from_prior(&mut prior_mid, &slug, &pm, &k);

        let quote = Quote {
            market: slug.clone(),
            cat: pair.cat,
            pm,
            k,
            depth: depth_dir,
            settle_clean: pair.settle_clean,
            cluster: pair.cluster.clone(),
            led_by,
            days_to_event: pair.days_to_event,
        };

        match evaluate(cfg, &quote, &edge, &exposure, affordable(cfg, &edge)) {
            Ok(a) => {
                // Per-leg LIMIT prices from the books — NOT derived from the pair edge. The YES leg pays
                // the CHEAP venue's YES ask; the NO leg pays the DEAR venue's NO ask (= 1 - its YES bid).
                // (A prior version used (1-edge) as the YES price, which set the NO limit to ~edge cents —
                // far below the real NO ask, so the NO leg would never fill and the hedge would leg out.)
                let (yes_ask, no_ask) = leg_prices(&quote, edge.dir);
                let (Some(yc), Some(nc)) = (cents(yes_ask), cents(no_ask)) else { continue };
                fire_pair(backend, &quote, &edge, a.size, yc, nc);
            }
            Err(_r) => {} // rejected by a gate — silent in the live loop; transitions/metrics are stage-2
        }
    }
    println!("[live] both venue streams ended — loop exiting.");
}

/// Log the discovery coverage report LOUDLY (L7): an unmapped category / misaligned bucket / truncated
/// catalog must never pass silently — a human decides whether to extend the config.
fn report_coverage(d: &discovery::Discovery) {
    if !d.weather_cities_unmapped.is_empty() {
        println!("[coverage] UNMAPPED climate cities (MISSED until added to discovery::WX): {:?}", d.weather_cities_unmapped);
    }
    if !d.sports_leagues_unmapped.is_empty() {
        println!("[coverage] UNMAPPED sports leagues: {:?}", d.sports_leagues_unmapped);
    }
    if d.weather_buckets_misaligned > 0 {
        println!("[coverage] {} weather buckets had no identical-bounds Kalshi twin (NOT paired)", d.weather_buckets_misaligned);
    }
    if d.truncated {
        println!("[coverage] WARNING pmus catalog hit the page cap — coverage INCOMPLETE");
    }
}

/// Set-diff the CURRENT subscribed keys against a FRESH discovery's keys -> the in-place `SubUpdate`
/// (add = fresh-not-current; del = current-not-fresh). Pure; the prune debounce is applied separately so
/// a transient discovery blip can't drop a live market (see `prune_step`).
fn diff_targets(current: &std::collections::HashSet<String>, fresh: &std::collections::HashSet<String>, to_prune: &std::collections::HashSet<String>) -> venue::SubUpdate {
    let add = fresh.iter().filter(|k| !current.contains(*k)).cloned().collect();
    let del = to_prune.iter().filter(|k| current.contains(*k)).cloned().collect();
    venue::SubUpdate { add, del }
}

/// 2-miss prune debounce (port of `monitor.py::prune_decision`): a tracked slug absent from `current`
/// discovery for `threshold` consecutive refreshes is settled -> prune. A reappearance resets its count,
/// so an API hiccup / pagination blip doesn't tear down a still-live market. Mutates `absent`.
fn prune_step(
    tracked: &std::collections::HashSet<String>,
    current: &std::collections::HashSet<String>,
    absent: &mut std::collections::HashMap<String, u32>,
    threshold: u32,
) -> std::collections::HashSet<String> {
    let mut to_prune = std::collections::HashSet::new();
    for slug in tracked {
        if current.contains(slug) {
            absent.insert(slug.clone(), 0);
        } else {
            let c = absent.entry(slug.clone()).or_insert(0);
            *c += 1;
            if *c >= threshold {
                to_prune.insert(slug.clone());
            }
        }
    }
    to_prune
}

/// PERIODIC RE-DISCOVERY (mirrors `monitor.py::rest_heartbeat`): every `refresh_s` re-pull both catalogs,
/// add newly-listed pairs (no-gap in-place subscribe), and prune settled ones (2-miss debounce). A
/// DEGRADED pull (error) is skipped entirely — never prune on a failed pull (a fetch error makes live
/// markets look settled; monitor.py H4). Supervised: one bad cycle logs and continues, never kills the task.
#[allow(clippy::too_many_arguments)]
async fn refresh_loop(
    http: reqwest::Client,
    refresh_s: u64,
    pairs: std::sync::Arc<std::sync::Mutex<PairState>>,
    k_tracked: std::sync::Arc<std::sync::Mutex<std::collections::HashSet<String>>>,
    pm_tracked: std::sync::Arc<std::sync::Mutex<std::collections::HashSet<String>>>,
    kalshi_books: std::sync::Arc<std::sync::Mutex<std::collections::HashMap<String, book::KalshiBook>>>,
    k_subs: tokio::sync::mpsc::UnboundedSender<venue::SubUpdate>,
    pm_subs: tokio::sync::mpsc::UnboundedSender<venue::SubUpdate>,
) {
    use std::collections::{HashMap, HashSet};
    let mut absent: HashMap<String, u32> = HashMap::new(); // pmus slug -> consecutive-miss count
    loop {
        tokio::time::sleep(std::time::Duration::from_secs(refresh_s.max(1))).await;
        let fresh = match discovery::discover(&http).await {
            Ok(d) => d,
            Err(e) => {
                println!("[refresh] discovery DEGRADED ({e}) — keeping current set, no prune (H4)");
                continue;
            }
        };
        report_coverage(&fresh);

        // fresh keys by venue.
        let fresh_slugs: HashSet<String> = fresh.pairs.iter().map(|p| p.slug.clone()).collect();
        let fresh_tickers: HashSet<String> = fresh.pairs.iter().map(|p| p.kalshi.clone()).collect();

        // snapshot the PRE-refresh subscribed set (slugs + each pair's Kalshi ticker) before mutating.
        let (pre_slugs, slug_to_ticker): (HashSet<String>, HashMap<String, String>) = {
            let ps = pairs.lock().unwrap();
            (
                ps.by_slug.keys().cloned().collect(),
                ps.by_slug.iter().map(|(s, p)| (s.clone(), p.kalshi.clone())).collect(),
            )
        };
        let pre_tickers: HashSet<String> = slug_to_ticker.values().cloned().collect();

        // PRUNE debounce (keyed on the pmus slug = the pair identity); pruned slugs -> their Kalshi tickers.
        let prune_slugs = prune_step(&pre_slugs, &fresh_slugs, &mut absent, 2);
        let prune_tickers: HashSet<String> = prune_slugs.iter().filter_map(|s| slug_to_ticker.get(s).cloned()).collect();

        // the in-place WIRE updates: add = fresh keys not already subscribed; del = the pruned keys. Pure
        // set-diff (the stream tolerates a re-add as a harmless no-gap merge, but we send the minimal set).
        let k_update = diff_targets(&pre_tickers, &fresh_tickers, &prune_tickers);
        let pm_update = diff_targets(&pre_slugs, &fresh_slugs, &prune_slugs);

        // apply to the shared pair map + tracked sets (streams re-subscribe `tracked` on reconnect, so
        // mutate it before dispatching so a reconnect-during-refresh stays consistent).
        let mut added = 0usize;
        {
            let mut ps = pairs.lock().unwrap();
            let mut kt = k_tracked.lock().unwrap();
            let mut pt = pm_tracked.lock().unwrap();
            for p in &fresh.pairs {
                if !ps.by_slug.contains_key(&p.slug) {
                    kt.insert(p.kalshi.clone());
                    pt.insert(p.slug.clone());
                    ps.insert(LivePair::from(p.clone()));
                    added += 1;
                }
            }
            for s in &prune_slugs {
                ps.remove(s);
                pt.remove(s);
                absent.remove(s);
            }
            for tk in &prune_tickers {
                kt.remove(tk);
            }
        }
        // free settled Kalshi books so memory stays FLAT over a multi-week run (L20), not only on the next
        // reconnect-clear. (pmus books are freed in the event loop when an untracked frame arrives.)
        if !prune_tickers.is_empty() {
            let mut kb = kalshi_books.lock().unwrap();
            for tk in &prune_tickers {
                kb.remove(tk);
            }
        }

        // dispatch the wire updates (pmus `del` is a local-only no-op — see pmus_stream docs).
        if !k_update.add.is_empty() || !k_update.del.is_empty() {
            let _ = k_subs.send(k_update);
        }
        if !pm_update.add.is_empty() {
            let _ = pm_subs.send(venue::SubUpdate { add: pm_update.add, del: Vec::new() });
        }
        if added > 0 || !prune_slugs.is_empty() {
            println!(
                "[refresh] +{added} pairs, -{} settled ({} weather + {} econ pairs live)",
                prune_slugs.len(), fresh.weather_pairs, fresh.econ_pairs
            );
        }
    }
}

/// Contracts the bankroll can fund at this pair price — the `affordable` arg to `risk::evaluate`. Derived
/// from the remaining total-notional room / per-pair cost (the gate then applies all the finer caps).
fn affordable(cfg: &Config, edge: &Edge) -> u32 {
    let cost_per = (1.0 - edge.net).max(0.1);
    (cfg.max_total_notional / cost_per).floor().max(0.0) as u32
}

/// Determine which venue led the current edge by comparing each venue's mid to the prior snapshot: the
/// venue whose mid moved MORE since last time is the mover. `None` on the first sighting (no prior).
fn led_by_from_prior(
    prior: &mut std::collections::HashMap<String, (Option<f64>, Option<f64>)>,
    slug: &str,
    pm: &Book,
    k: &Book,
) -> Option<Venue> {
    let (pm_mid, k_mid) = (pm.mid(), k.mid());
    let led = match prior.get(slug) {
        Some((Some(prev_pm), Some(prev_k))) => match (pm_mid, k_mid) {
            (Some(npm), Some(nk)) => {
                let dpm = (npm - prev_pm).abs();
                let dk = (nk - prev_k).abs();
                if dpm > dk {
                    Some(Venue::Pmus)
                } else if dk > dpm {
                    Some(Venue::Kalshi)
                } else {
                    None
                }
            }
            _ => None,
        },
        _ => None, // first sighting
    };
    prior.insert(slug.to_string(), (pm_mid, k_mid));
    led
}

/// The two per-leg LIMIT prices (dollars) for the hedge in direction `dir`: the YES leg buys at the
/// CHEAP venue's YES ask; the NO leg buys at the DEAR venue's NO ask (= 1 - that venue's YES bid). These
/// come from the live touches, never from the pair edge (the edge is YES_ask + NO_ask, not either leg).
fn leg_prices(q: &Quote, dir: Dir) -> (Option<f64>, Option<f64>) {
    let (cheap, dear) = match dir {
        Dir::PK => (&q.pm, &q.k), // YES@pmus + NO@Kalshi
        Dir::KP => (&q.k, &q.pm), // YES@Kalshi + NO@pmus
    };
    let yes_ask = cheap.yes_ask;
    let no_ask = dear.yes_bid.map(|b| (1.0 - b).clamp(0.01, 0.99));
    (yes_ask, no_ask)
}

/// Dollars (0..1) -> a valid integer venue tick price in 1..=99 cents, or None if non-finite/out of range.
fn cents(price: Option<f64>) -> Option<u8> {
    let p = price?;
    if !p.is_finite() {
        return None;
    }
    let c = (p * 100.0).round();
    if (1.0..=99.0).contains(&c) {
        Some(c as u8)
    } else {
        None
    }
}

/// Fire the hedged pair: buy YES on the cheap venue at `yes_price_cents` + NO on the dear venue at
/// `no_price_cents`, as ONE pair (the backend fires both legs concurrently). Shared by smoke + live loop.
fn fire_pair(backend: &mut dyn ExecutionBackend, q: &Quote, edge: &Edge, size: u32, yes_price_cents: u8, no_price_cents: u8) {
    let (yes_venue, no_venue) = match edge.dir {
        Dir::PK => (Venue::Pmus, Venue::Kalshi),
        Dir::KP => (Venue::Kalshi, Venue::Pmus),
    };
    let leg_yes = OrderIntent {
        venue: yes_venue,
        market: q.market.clone(),
        action: Action::Buy,
        side: Side::Yes,
        price_cents: yes_price_cents,
        qty: size,
        client_order_id: format!("xarb-{}-Y", q.market),
    };
    let leg_no = OrderIntent {
        venue: no_venue,
        market: q.market.clone(),
        action: Action::Buy,
        side: Side::No,
        price_cents: no_price_cents,
        qty: size,
        client_order_id: format!("xarb-{}-N", q.market),
    };
    let _ = backend.submit_pair(&leg_yes, &leg_no);
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
    report(cfg, &econ, Edge { net: 0.09, dir: Dir::PK }, backend);

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
    report(cfg, &wx, Edge { net: 0.03, dir: Dir::PK }, backend);

    // (3) same weather arb but DEAR-led -> the H1 toxicity-direction gate rejects it (weather-only).
    let mut wx_toxic = wx.clone();
    wx_toxic.led_by = Some(Venue::Kalshi); // dear venue (dir PK) led -> ~79% toxic
    println!("[smoke] weather arb, verified, 3c edge, DEAR-led (toxic):");
    report(cfg, &wx_toxic, Edge { net: 0.03, dir: Dir::PK }, backend);

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
    report(&sc, &sport, Edge { net: 0.03, dir: Dir::PK }, backend);
    let mut sport_soon = sport.clone();
    sport_soon.days_to_event = Some(1.0);
    println!("[smoke] sports arb (assumed-settled), 1 day pre-game -> within window:");
    report(&sc, &sport_soon, Edge { net: 0.03, dir: Dir::PK }, backend);

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

fn report(cfg: &Config, q: &Quote, edge: Edge, backend: &mut dyn ExecutionBackend) {
    match evaluate(cfg, q, &edge, &Exposure::new(), 1000) {
        Ok(a) => {
            println!("  APPROVED size={}  (edge {:.1}c, dir {:?})", a.size, edge.net * 100.0, edge.dir);
            // per-leg limit prices from the quote's books (same correct path the live loop uses).
            let (yes_ask, no_ask) = leg_prices(q, edge.dir);
            match (cents(yes_ask), cents(no_ask)) {
                (Some(yc), Some(nc)) => fire_pair(backend, q, &edge, a.size, yc, nc),
                _ => println!("  (no two-sided book to price both legs)"),
            }
        }
        Err(r) => println!("  REJECTED: {:?}", r),
    }
    println!();
}

#[cfg(test)]
mod tests {
    use super::*;

    fn q_pk() -> Quote {
        // dir PK: cheap = pmus (YES ask 0.07), dear = Kalshi (YES bid 0.10 -> NO ask 0.90).
        Quote {
            market: "tc-temp-nychigh-2026-06-11-gte95f".into(),
            cat: Cat::Weather,
            pm: Book { yes_bid: Some(0.05), yes_ask: Some(0.07), age_s: 0.0 },
            k: Book { yes_bid: Some(0.10), yes_ask: Some(0.11), age_s: 0.0 },
            depth: Depth { c2: 40, c1: 50, c0: 60 },
            settle_clean: true,
            cluster: "nychigh-2026-06-11".into(),
            led_by: None,
            days_to_event: None,
        }
    }

    /// REGRESSION GUARD for the self-review CRITICAL: leg prices come from the BOOKS (YES = cheap
    /// venue's YES ask; NO = 1 - dear venue's YES bid), NOT from the pair edge. The buggy version set
    /// YES = (1-edge) ~= 0.97 and NO ~= edge, so the NO leg's limit sat far below its real ask and could
    /// never fill -> a naked YES leg. Here YES must be 7c and NO must be 90c (sum = pair cost = 1-edge).
    #[test]
    fn leg_prices_come_from_books_not_edge() {
        let q = q_pk();
        let (yes_ask, no_ask) = leg_prices(&q, Dir::PK);
        assert_eq!(cents(yes_ask), Some(7)); // pmus YES ask 0.07 — the cheap YES leg
        assert_eq!(cents(no_ask), Some(90)); // 1 - Kalshi YES bid 0.10 — the dear NO leg
        // KP flips which venue is cheap/dear: YES = Kalshi ask 0.11; NO = 1 - pmus YES bid 0.05 = 0.95.
        let (yk, nk) = leg_prices(&q, Dir::KP);
        assert_eq!(cents(yk), Some(11));
        assert_eq!(cents(nk), Some(95));
    }

    /// `cents` rounds to the nearest tick and rejects prices that round outside the 1..=99 range or are
    /// non-finite/absent (0.5c rounds UP to a valid 1c tick; 0.4c rounds to 0c -> rejected).
    #[test]
    fn cents_validates_tick_range() {
        assert_eq!(cents(Some(0.075)), Some(8)); // 7.5c rounds to 8c
        assert_eq!(cents(Some(0.005)), Some(1)); // 0.5c rounds up to the 1c floor tick
        assert_eq!(cents(Some(0.004)), None); // 0.4c rounds to 0c -> below the valid range
        assert_eq!(cents(Some(0.0)), None); // free -> not a tradeable tick
        assert_eq!(cents(Some(1.0)), None); // 100c -> out of range
        assert_eq!(cents(None), None);
        assert_eq!(cents(Some(f64::NAN)), None);
    }

    /// A one-sided book (no dear-venue YES bid) yields no NO-leg price -> the loop skips (no naked fire).
    #[test]
    fn one_sided_book_blocks_the_no_leg_price() {
        let mut q = q_pk();
        q.k.yes_bid = None; // no Kalshi bid -> can't price the NO@Kalshi leg
        let (yes_ask, no_ask) = leg_prices(&q, Dir::PK);
        assert_eq!(cents(yes_ask), Some(7));
        assert_eq!(cents(no_ask), None); // -> the live loop `continue`s instead of firing a naked leg
    }

    use std::collections::{HashMap, HashSet};
    fn set(items: &[&str]) -> HashSet<String> {
        items.iter().map(|s| s.to_string()).collect()
    }

    /// The refresh diff: add = fresh-not-current, del = the pruned keys. Pure set-diff.
    #[test]
    fn diff_targets_adds_new_and_deletes_pruned() {
        let current = set(&["A", "B", "C"]);
        let fresh = set(&["B", "C", "D"]); // A gone, D new
        let to_prune = set(&["A"]); // A debounced out
        let u = diff_targets(&current, &fresh, &to_prune);
        assert_eq!(u.add, vec!["D".to_string()]);
        assert_eq!(u.del, vec!["A".to_string()]);
        // a key still in fresh is never deleted even if it appears in to_prune (defensive: prune wins
        // only on keys actually absent from fresh, which the debounce already guarantees).
        let u2 = diff_targets(&current, &fresh, &HashSet::new());
        assert!(u2.del.is_empty() && u2.add == vec!["D".to_string()]);
    }

    /// The 2-miss prune debounce (monitor.py parity): missing ONCE holds; missing TWICE prunes; a
    /// reappearance resets the miss count.
    #[test]
    fn prune_step_debounces_two_misses() {
        let tracked = set(&["a", "b", "c"]);
        let mut absent: HashMap<String, u32> = HashMap::new();
        // round 1: b,c missing once -> hold (only a is present).
        assert_eq!(prune_step(&tracked, &set(&["a"]), &mut absent, 2), HashSet::new());
        // round 2: still missing -> prune both.
        assert_eq!(prune_step(&tracked, &set(&["a"]), &mut absent, 2), set(&["b", "c"]));
        // a reappearance resets the counter (b back -> not pruned next miss).
        let mut absent2: HashMap<String, u32> = HashMap::new();
        prune_step(&tracked, &set(&["a", "c"]), &mut absent2, 2); // b missing once
        prune_step(&tracked, &set(&["a", "b", "c"]), &mut absent2, 2); // b back -> reset
        assert_eq!(prune_step(&tracked, &set(&["a", "c"]), &mut absent2, 2), HashSet::new()); // b missing once again -> hold
    }

    /// `LivePair` carries discovery's settle_clean through unchanged (weather true, econ false) so the
    /// risk gate's settlement-identity check is fed the right value per category.
    #[test]
    fn livepair_from_discovery_preserves_settle_clean() {
        let wx = discovery::Pair { slug: "tc-temp-x-2026-06-11-gte95f".into(), kalshi: "K".into(), cat: Cat::Weather, cluster: "x".into(), settle_clean: true, days_to_event: Some(0.0) };
        let ec = discovery::Pair { slug: "urc-x".into(), kalshi: "K2".into(), cat: Cat::Econ, cluster: "u3-26JUN".into(), settle_clean: false, days_to_event: None };
        assert!(LivePair::from(wx).settle_clean);
        assert!(!LivePair::from(ec).settle_clean);
    }
}
