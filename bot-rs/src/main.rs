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
mod postpone;
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

/// A co-listed pair the live loop tracks. Weather/econ are 1:1 (`kalshi_b = None`). SPORTS is 2-outcome:
/// `kalshi` = team-A ticker (the team pmus lists as YES), `kalshi_b = Some(team-B ticker)` — BOTH Kalshi
/// books are subscribed and the 2-outcome `game_signal` needs both. Filled by `discovery`.
#[derive(Clone, Debug)]
struct LivePair {
    slug: String,         // pmus market slug
    kalshi: String,       // Kalshi ticker (team-A ticker for sports)
    kalshi_b: Option<String>, // SPORTS: team-B (away) Kalshi ticker; None for weather/econ
    cat: Cat,
    cluster: String,
    settle_clean: bool,
    days_to_event: Option<f64>,
}

impl LivePair {
    /// Every Kalshi ticker this pair subscribes (team-A always; team-B for sports). Drives `by_ticker`
    /// registration, `k_tracked`, and book freeing on prune — so a sports pair tracks BOTH books.
    fn kalshi_tickers(&self) -> Vec<String> {
        let mut v = vec![self.kalshi.clone()];
        if let Some(b) = &self.kalshi_b {
            v.push(b.clone());
        }
        v
    }
}

impl From<discovery::Pair> for LivePair {
    fn from(p: discovery::Pair) -> Self {
        LivePair { slug: p.slug, kalshi: p.kalshi, kalshi_b: p.kalshi_b, cat: p.cat, cluster: p.cluster, settle_clean: p.settle_clean, days_to_event: p.days_to_event }
    }
}

/// Shared, mutable pair state the event loop READS and the refresh task MUTATES (add new pairs / drop
/// settled). `by_slug` is the authoritative pair record; `by_ticker` indexes EVERY Kalshi ticker (both
/// teams for a sports pair) back to the pmus slug, so a frame on either Kalshi book finds its pair.
#[derive(Default)]
struct PairState {
    by_slug: std::collections::HashMap<String, LivePair>,
    by_ticker: std::collections::HashMap<String, String>, // Kalshi ticker -> pmus slug
}

impl PairState {
    fn insert(&mut self, p: LivePair) {
        for tk in p.kalshi_tickers() {
            self.by_ticker.insert(tk, p.slug.clone());
        }
        self.by_slug.insert(p.slug.clone(), p);
    }
    fn remove(&mut self, slug: &str) {
        if let Some(p) = self.by_slug.remove(slug) {
            for tk in p.kalshi_tickers() {
                self.by_ticker.remove(&tk);
            }
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
            let lp = LivePair::from(p);
            for tk in lp.kalshi_tickers() {
                kt.insert(tk); // sports registers BOTH team tickers
            }
            pt.insert(lp.slug.clone());
            ps.insert(lp);
        }
    }

    // book stores: Kalshi books are owned by the kalshi_stream (shared so the loop can read touches);
    // pmus books are rebuilt here from each frame's (bids, asks).
    let kalshi_books: Arc<Mutex<HashMap<String, book::KalshiBook>>> = Arc::new(Mutex::new(HashMap::new()));
    let mut pmus_books: HashMap<String, book::PmusBook> = HashMap::new();
    let mut prior_mid: HashMap<String, (Option<f64>, Option<f64>)> = HashMap::new(); // slug -> (pm_mid, k_mid) for led_by
    let mut exposure = Exposure::new();

    // HELD positions keyed by pmus slug — the postponement poll reads these (MLB sports), and the entry
    // path inserts into them on a both-filled fill so exposure caps bind across the session and a void can
    // be flattened. Shared with the spawned poll task.
    let positions: Arc<Mutex<HashMap<String, postpone::HeldPosition>>> = Arc::new(Mutex::new(HashMap::new()));

    let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel::<venue::VenueEvent>();
    let (k_subs_tx, k_subs_rx) = tokio::sync::mpsc::unbounded_channel::<venue::SubUpdate>();
    let (pm_subs_tx, pm_subs_rx) = tokio::sync::mpsc::unbounded_channel::<venue::SubUpdate>();
    // the postponement-unwind channel: the poll detects a void and sends an UnwindRequest the loop fires.
    let (unwind_tx, mut unwind_rx) = tokio::sync::mpsc::unbounded_channel::<postpone::UnwindRequest>();

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
    // ARM the live postponement-unwind trigger (gated on cfg.auto_unwind). statsapi is public/keyless; the
    // poll runs on the owner's droplet. Disengage with CROSSARB_NO_AUTO_UNWIND=1 (a manual-only fallback).
    if cfg.auto_unwind {
        tokio::spawn(postpone::poll_mlb_postponements(
            http.clone(),
            positions.clone(),
            unwind_tx.clone(),
            cfg.postpone_poll_s,
            cfg.kalshi_void_window_days,
        ));
    }
    drop(tx); // the spawned tasks hold their own senders; drop ours so rx closes if both ever end
    // KEEP `unwind_tx` alive for the loop's lifetime: if it were dropped while auto_unwind=false (no poll
    // task holds a clone), `unwind_rx` would close and its select arm would return `None` every poll ->
    // a busy-loop. Holding the sender keeps `recv()` PARKED when idle. The loop exits on the venue `rx`
    // closing (above), not this channel.
    let _unwind_tx_keepalive = unwind_tx;

    // A venue WS reconnect/seq-gap pauses trading until THAT venue's books rebuild (never trade a
    // half-rebuilt book). Tracked PER VENUE: a pmus frame must not clear a Kalshi rebuild pause, and
    // vice-versa. `stream_paused` stays true while EITHER venue is mid-rebuild.
    let (mut k_rebuild, mut pm_rebuild) = (false, false);

    loop {
        // Drive BOTH the venue book stream AND the postponement-unwind channel. A void detection must not
        // wait behind a quiet book stream, so the unwind arm is co-equal (not polled only between frames).
        let ev = tokio::select! {
            v = rx.recv() => match v {
                Some(ev) => ev,
                None => break, // both venue streams ended
            },
            u = unwind_rx.recv() => {
                if let Some(req) = u {
                    handle_unwind(cfg, backend, &positions, &kalshi_books, &pmus_books, &mut exposure, &req.slug);
                }
                continue;
            }
        };
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
        // checks ALL legs (sports reads THREE books), so the worse (older) leg governs staleness.
        let pm = match pmus_books.get(&slug) {
            Some(b) => b.touch(),
            None => continue, // no pmus book yet -> incomplete, wait
        };
        // SPORTS (kalshi_b.is_some()) is 2-outcome: read pmus + Kalshi-A + Kalshi-B (ALL required) and use
        // the game signal/depth. Weather/econ is 1:1: pmus + the single Kalshi book + the binary signal.
        let (k, k_b, depth_dir, edge) = {
            let kb = kalshi_books.lock().unwrap();
            let Some(ka_book) = kb.get(&pair.kalshi) else { continue }; // no Kalshi-A book yet -> incomplete
            let k = ka_book.touch();
            let pmb = pmus_books.get(&slug).unwrap();
            match &pair.kalshi_b {
                Some(tb) => {
                    let Some(kb_book) = kb.get(tb) else { continue }; // no Kalshi-B book yet -> incomplete
                    let sig = signal::game_signal(pm.yes_bid, pm.yes_ask, k.yes_ask, kb_book.touch().yes_ask);
                    let depth = book::game_depth_at_edge(pmb, ka_book, kb_book, sig.edge.dir);
                    (k, Some(kb_book.touch()), depth, sig.edge)
                }
                None => {
                    let sig = signal::signal(&pm, &k);
                    let depth = book::depth_at_edge(ka_book, pmb, sig.edge.dir);
                    (k, None, depth, sig.edge)
                }
            }
        };

        // led_by: which venue's mid MOVED to open/realign this edge, vs the prior snapshot (H1 input).
        let led_by = led_by_from_prior(&mut prior_mid, &slug, &pm, &k);

        let quote = Quote {
            market: slug.clone(),
            cat: pair.cat,
            pm,
            k,
            k_b,
            depth: depth_dir,
            settle_clean: pair.settle_clean,
            cluster: pair.cluster.clone(),
            led_by,
            days_to_event: pair.days_to_event,
        };

        match evaluate(cfg, &quote, &edge, &exposure, affordable(cfg, &edge)) {
            Ok(a) => {
                // Build BOTH legs with venue-native market ids + per-leg LIMIT prices from the BOOKS (never
                // derived from the pair edge — that was a self-review CRITICAL). A missing book price (a
                // one-sided book) yields no legs -> skip rather than fire a naked leg.
                let Some(legs) = build_legs(&pair, &quote, edge.dir, a.size) else { continue };
                let ack = backend.submit_pair(&legs[0], &legs[1]);
                // On a both-filled fill: record the held position (so a void can be flattened) AND bump
                // exposure so the caps actually bind across the session. A partial/failed fill is NOT
                // tracked here — naked-leg handling is the leg-fill-timeout path (stage-2 legs.rs).
                if ack.both_filled() {
                    let pos = position_from_intents(&slug, pair.cat, &pair.cluster, &legs);
                    track_position(&positions, &pair, pos, &mut exposure, a.cost_per);
                }
            }
            Err(_r) => {} // rejected by a gate — silent in the live loop; transitions/metrics are stage-2
        }
    }
    println!("[live] both venue streams ended — loop exiting.");
}

/// Record a freshly-filled position + BUMP exposure so the pair/cluster/total/concurrency caps bind across
/// the session (without this, every fill looks like the first and the caps never engage). For a SPORTS
/// pair the postponement poll needs league/date/abbrevs: league = `pm_league(slug)`, date = `iso_date(slug)`,
/// team_a/team_b = the last dash-segment of each Kalshi ticker (`pair.kalshi`/`pair.kalshi_b`). Non-sports
/// leaves those empty (no postponement concept), so the poll ignores them.
fn track_position(
    positions: &std::sync::Arc<std::sync::Mutex<std::collections::HashMap<String, postpone::HeldPosition>>>,
    pair: &LivePair,
    pos: Position,
    exposure: &mut Exposure,
    cost_per: f64,
) {
    let notional = cost_per * pos.size as f64;
    *exposure.per_pair.entry(pos.market.clone()).or_insert(0.0) += notional;
    *exposure.per_cluster.entry(pos.cluster.clone()).or_insert(0.0) += notional;
    exposure.total += notional;
    exposure.open_positions += 1;

    let (league, date, team_a, team_b) = if pair.cat == Cat::Sports {
        let last_seg = |t: &str| t.rsplit('-').next().unwrap_or("").to_ascii_lowercase();
        (
            discovery::pm_league(&pair.slug).unwrap_or_default(),
            discovery::iso_date(&pair.slug).unwrap_or_default(),
            last_seg(&pair.kalshi),
            pair.kalshi_b.as_deref().map(last_seg).unwrap_or_default(),
        )
    } else {
        (String::new(), String::new(), String::new(), String::new())
    };
    let slug = pos.market.clone();
    positions.lock().unwrap().insert(
        slug,
        postpone::HeldPosition { pos, league, date, team_a, team_b, prev: None },
    );
}

/// Handle a postponement `UnwindRequest`: look up the held position, price each leg's marketable EXIT from
/// the live books (SELL YES -> that book's best yes_bid; SELL NO -> 1 - yes_ask), and if BOTH price, fire
/// the two SELLs; on a both-filled ack remove the position + decrement exposure. A one-sided book (a leg
/// can't be priced) logs a WARN and leaves the position (the poll re-emits; the idempotent `unwind-…` coids
/// stop a double-flatten). REDUCE-ONLY: this fires even under the kill-switch — flattening a void REDUCES
/// risk — and the dry-run backend only LOGS, so it's safe by default.
fn handle_unwind(
    cfg: &Config,
    backend: &mut dyn ExecutionBackend,
    positions: &std::sync::Arc<std::sync::Mutex<std::collections::HashMap<String, postpone::HeldPosition>>>,
    kalshi_books: &std::sync::Arc<std::sync::Mutex<std::collections::HashMap<String, book::KalshiBook>>>,
    pmus_books: &std::collections::HashMap<String, book::PmusBook>,
    exposure: &mut Exposure,
    slug: &str,
) {
    let Some(hp) = positions.lock().unwrap().get(slug).cloned() else { return }; // already flattened / gone
    if cfg.kill_switch {
        println!("[UNWIND] kill-switch engaged but flattening (reduce-only) {slug}");
    }
    // price each leg's exit from the venue book it sits on (a SELL never blocks on a fresh entry edge).
    let exits = unwind_exit_cents(&hp.pos, |leg| match leg.venue {
        Venue::Kalshi => kalshi_books.lock().unwrap().get(&leg.market).map(|b| b.touch()),
        Venue::Pmus => pmus_books.get(&leg.market).map(|b| b.touch()),
    });
    let Some(exits) = exits else {
        println!("[UNWIND] WARN one-sided book — cannot price both legs of {slug}; holding (poll re-emits)");
        return;
    };
    let orders = unwind::unwind_orders(&hp.pos, exits);
    let ack = backend.submit_pair(&orders[0], &orders[1]);
    if ack.both_filled() {
        if let Some(removed) = positions.lock().unwrap().remove(slug) {
            decrement_exposure(exposure, &removed.pos);
        }
        println!("[UNWIND] flattened {slug}");
    } else {
        println!("[UNWIND] WARN {slug} did not fully flatten (one leg unfilled); poll re-emits");
    }
}

/// Decrement exposure when a tracked position is closed (mirror of the bump in `track_position`), so caps
/// re-open for new entries. Reconstructs the notional from cost_per×size is not available post-fill, so we
/// remove the recorded per-pair notional directly (the per-pair bucket holds exactly this position's
/// contribution — one position per pmus slug).
fn decrement_exposure(exposure: &mut Exposure, pos: &Position) {
    if let Some(n) = exposure.per_pair.remove(&pos.market) {
        exposure.total = (exposure.total - n).max(0.0);
        if let Some(c) = exposure.per_cluster.get_mut(&pos.cluster) {
            *c = (*c - n).max(0.0);
        }
    }
    exposure.open_positions = exposure.open_positions.saturating_sub(1);
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

/// Every Kalshi ticker a freshly-discovered pair subscribes (team-A always; team-B for sports). Mirrors
/// `LivePair::kalshi_tickers` for the pre-`LivePair` `discovery::Pair` the refresh task iterates.
fn pair_tickers(p: &discovery::Pair) -> Vec<String> {
    let mut v = vec![p.kalshi.clone()];
    if let Some(b) = &p.kalshi_b {
        v.push(b.clone());
    }
    v
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

        // fresh keys by venue. A sports pair contributes BOTH Kalshi tickers (team A + B).
        let fresh_slugs: HashSet<String> = fresh.pairs.iter().map(|p| p.slug.clone()).collect();
        let fresh_tickers: HashSet<String> = fresh.pairs.iter().flat_map(pair_tickers).collect();

        // snapshot the PRE-refresh subscribed set (slugs + each pair's Kalshi ticker(s)) before mutating.
        let (pre_slugs, slug_to_tickers): (HashSet<String>, HashMap<String, Vec<String>>) = {
            let ps = pairs.lock().unwrap();
            (
                ps.by_slug.keys().cloned().collect(),
                ps.by_slug.iter().map(|(s, p)| (s.clone(), p.kalshi_tickers())).collect(),
            )
        };
        let pre_tickers: HashSet<String> = slug_to_tickers.values().flatten().cloned().collect();

        // PRUNE debounce (keyed on the pmus slug = the pair identity); pruned slugs -> their Kalshi
        // tickers (both teams for a sports pair).
        let prune_slugs = prune_step(&pre_slugs, &fresh_slugs, &mut absent, 2);
        let prune_tickers: HashSet<String> = prune_slugs.iter().filter_map(|s| slug_to_tickers.get(s)).flatten().cloned().collect();

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
                    for tk in pair_tickers(p) {
                        kt.insert(tk); // sports adds BOTH team tickers
                    }
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

/// A single planned leg before pricing-to-tick: the venue, the VENUE-NATIVE market id (Kalshi ticker for
/// a Kalshi leg, pmus slug for a pmus leg — the leg-market fix), the side, and the limit price in dollars
/// read from the BOOKS (never the pair edge). `tag` ('A'/'B') makes the two client_order_ids distinct.
struct PlannedLeg {
    venue: Venue,
    market: String,
    side: Side,
    price: f64,
    tag: char,
}

/// Plan both hedge legs for `dir`, with each leg's market id VENUE-NATIVE and each limit price read from
/// the quote's BOOKS. This is the single unified builder (it replaced the old weather-only `leg_prices` +
/// `fire_pair`, which sent the pmus SLUG as the Kalshi leg's ticker — a wrong-ticker live order).
///   - weather/econ (1:1): PK = YES@pmus(slug) + NO@Kalshi(ticker); KP = YES@Kalshi(ticker) + NO@pmus(slug).
///   - sports (2-outcome): PK = YES@pmus(slug) + YES@Kalshi-B(ticker_b); KP = YES@Kalshi-A(ticker_a) + NO@pmus(slug).
///
/// Prices (monitor.py game_edge / signal): a YES leg pays that book's YES ask; a NO@pmus leg pays
/// `1 - pm_bid`; a NO@Kalshi leg pays `1 - k_bid`. Returns `None` if any leg lacks a book price (one-sided
/// book) -> the caller skips rather than fire a naked leg.
fn plan_legs(pair: &LivePair, q: &Quote, dir: Dir) -> Option<[PlannedLeg; 2]> {
    let slug = pair.slug.clone();
    let ka = pair.kalshi.clone();
    let no_at = |b: &Book| b.yes_bid.map(|bid| 1.0 - bid); // NO ask = 1 - that book's YES bid
    match (pair.kalshi_b.as_ref(), dir) {
        // ---- SPORTS ----
        (Some(kb_ticker), Dir::PK) => {
            // back A@pmus (YES@pmus slug) + B@Kalshi (YES@Kalshi-B ticker).
            let pm_ask = q.pm.yes_ask?;
            let kb_ask = q.k_b.as_ref().and_then(|b| b.yes_ask)?;
            Some([
                PlannedLeg { venue: Venue::Pmus, market: slug, side: Side::Yes, price: pm_ask, tag: 'A' },
                PlannedLeg { venue: Venue::Kalshi, market: kb_ticker.clone(), side: Side::Yes, price: kb_ask, tag: 'B' },
            ])
        }
        (Some(_), Dir::KP) => {
            // back A@Kalshi (YES@Kalshi-A ticker) + B@pmus (NO@pmus slug = 1 - pm_bid).
            let ka_ask = q.k.yes_ask?;
            let pm_no = no_at(&q.pm)?;
            Some([
                PlannedLeg { venue: Venue::Kalshi, market: ka, side: Side::Yes, price: ka_ask, tag: 'A' },
                PlannedLeg { venue: Venue::Pmus, market: slug, side: Side::No, price: pm_no, tag: 'B' },
            ])
        }
        // ---- WEATHER / ECON (1:1) ----
        (None, Dir::PK) => {
            // YES@pmus(slug) + NO@Kalshi(ticker = 1 - Kalshi YES bid).
            let pm_ask = q.pm.yes_ask?;
            let k_no = no_at(&q.k)?;
            Some([
                PlannedLeg { venue: Venue::Pmus, market: slug, side: Side::Yes, price: pm_ask, tag: 'A' },
                PlannedLeg { venue: Venue::Kalshi, market: ka, side: Side::No, price: k_no, tag: 'B' },
            ])
        }
        (None, Dir::KP) => {
            // YES@Kalshi(ticker) + NO@pmus(slug = 1 - pm_bid).
            let k_ask = q.k.yes_ask?;
            let pm_no = no_at(&q.pm)?;
            Some([
                PlannedLeg { venue: Venue::Kalshi, market: ka, side: Side::Yes, price: k_ask, tag: 'A' },
                PlannedLeg { venue: Venue::Pmus, market: slug, side: Side::No, price: pm_no, tag: 'B' },
            ])
        }
    }
}

/// Plan + price-to-tick both legs into `OrderIntent`s ready to submit. `None` if any leg can't be priced
/// (one-sided book) or rounds outside the 1..=99c venue tick range. The two legs share the pmus slug
/// (the pair identity) in their client_order_id so retries dedupe per pair-leg.
fn build_legs(pair: &LivePair, q: &Quote, dir: Dir, size: u32) -> Option<[OrderIntent; 2]> {
    let planned = plan_legs(pair, q, dir)?;
    let mut out: Vec<OrderIntent> = Vec::with_capacity(2);
    for leg in planned {
        let pc = cents(Some(leg.price))?;
        out.push(OrderIntent {
            venue: leg.venue,
            market: leg.market,
            action: Action::Buy,
            side: leg.side,
            price_cents: pc,
            qty: size,
            client_order_id: format!("xarb-{}-{}", pair.slug, leg.tag),
        });
    }
    Some([out.remove(0), out.remove(0)])
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

/// Fire the hedged pair from an already-built pair of legs (the backend fires both concurrently). Thin
/// wrapper kept for the smoke path; the live loop calls `submit_pair` directly off `build_legs`.
fn fire_legs(backend: &mut dyn ExecutionBackend, legs: &[OrderIntent; 2]) {
    let _ = backend.submit_pair(&legs[0], &legs[1]);
}

/// The HELD position the live loop derives from a both-filled entry: build a `Position` straight from the
/// two entry `OrderIntent`s (each leg = its venue/market/side; the pair `market` = the pmus slug). The two
/// intents are the exact legs we now own, so the unwind SELLs back the same (venue, market, side).
fn position_from_intents(slug: &str, cat: Cat, cluster: &str, legs: &[OrderIntent; 2]) -> Position {
    Position {
        market: slug.to_string(),
        cat,
        legs: std::array::from_fn(|i| PositionLeg {
            venue: legs[i].venue,
            market: legs[i].market.clone(),
            side: legs[i].side,
        }),
        size: legs[0].qty,
        cluster: cluster.to_string(),
    }
}

/// Price ONE held leg's marketable EXIT (a SELL) from the live book of the venue it sits on: a SELL YES
/// leg lifts that book's best `yes_bid`; a SELL NO leg unwinds at `1 - yes_ask` (selling NO = buying YES
/// back, which pays the YES ask -> the NO sale nets `1 - yes_ask`). `None` when the needed side isn't
/// quoted (a one-sided book) -> the caller leaves the position and the poll re-emits.
fn exit_price(leg: &PositionLeg, book: &Book) -> Option<f64> {
    match leg.side {
        Side::Yes => book.yes_bid,
        Side::No => book.yes_ask.map(|a| 1.0 - a),
    }
}

/// Price BOTH legs of a held position to exit `cents`, reading each leg's book from `book_of` (the live
/// per-venue books). `None` if EITHER leg can't be priced or rounds outside the venue tick — the caller
/// then logs a WARN and holds (the poll re-emits; the idempotent `unwind-…` coids prevent a double-flatten).
fn unwind_exit_cents<F>(pos: &Position, book_of: F) -> Option<[u8; 2]>
where
    F: Fn(&PositionLeg) -> Option<Book>,
{
    let mut out = [0u8; 2];
    for (i, leg) in pos.legs.iter().enumerate() {
        let book = book_of(leg)?;
        out[i] = cents(exit_price(leg, &book))?;
    }
    Some(out)
}

/// Stage-1 smoke: prove the risk+exec spine behaves on real-shaped snapshots (weather/econ/sports).
fn smoke(cfg: &Config, backend: &mut dyn ExecutionBackend) {
    // a 1:1 LivePair (weather/econ) builder for the smoke (kalshi_b = None).
    let lp = |slug: &str, kalshi: &str, cat: Cat, cluster: &str| LivePair {
        slug: slug.into(), kalshi: kalshi.into(), kalshi_b: None, cat, cluster: cluster.into(),
        settle_clean: false, days_to_event: None,
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
        days_to_event: Some(5.0),
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
    };
    println!("[smoke] sports arb (assumed-settled), 5 days pre-game -> event-proximity gate:");
    report(&sc, &sport_pair, &sport, Edge { net: 0.03, dir: Dir::PK }, backend);
    let mut sport_soon = sport.clone();
    sport_soon.days_to_event = Some(1.0);
    let mut sport_pair_soon = sport_pair.clone();
    sport_pair_soon.days_to_event = Some(1.0);
    println!("[smoke] sports arb (assumed-settled), 1 day pre-game -> within window:");
    report(&sc, &sport_pair_soon, &sport_soon, Edge { net: 0.03, dir: Dir::PK }, backend);

    // (5) postponement unwind, LIVE PATH on a SYNTHETIC schedule (no network): a held MLB pair (dir PK:
    //     YES@pmus + YES@Kalshi-B) + a `snap`'d "Postponed, makeup 5d out" schedule game -> the DETECTOR
    //     fires -> should_unwind true -> print the two SELL unwind orders. This is the real stage-2 trigger
    //     composition (detect_postponement -> should_unwind -> unwind_orders), exercised offline.
    let held = types::Position {
        market: "aec-mlb-lad-pit-2026-06-16".into(), cat: Cat::Sports,
        legs: [
            types::PositionLeg { venue: Venue::Pmus, market: "aec-mlb-lad-pit-2026-06-16".into(), side: Side::Yes },
            types::PositionLeg { venue: Venue::Kalshi, market: "KXMLBGAME-26JUN16-PIT".into(), side: Side::Yes },
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

fn report(cfg: &Config, pair: &LivePair, q: &Quote, edge: Edge, backend: &mut dyn ExecutionBackend) {
    match evaluate(cfg, q, &edge, &Exposure::new(), 1000) {
        Ok(a) => {
            println!("  APPROVED size={}  (edge {:.1}c, dir {:?})", a.size, edge.net * 100.0, edge.dir);
            // build both legs from the BOOKS (same unified path the live loop uses) and fire.
            match build_legs(pair, q, edge.dir, a.size) {
                Some(legs) => fire_legs(backend, &legs),
                None => println!("  (no two-sided book to price both legs)"),
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
            k_b: None,
            depth: Depth { c2: 40, c1: 50, c0: 60 },
            settle_clean: true,
            cluster: "nychigh-2026-06-11".into(),
            led_by: None,
            days_to_event: None,
        }
    }
    fn wx_pair() -> LivePair {
        LivePair {
            slug: "tc-temp-nychigh-2026-06-11-gte95f".into(),
            kalshi: "KXHIGHNY-26JUN11-T95".into(),
            kalshi_b: None,
            cat: Cat::Weather,
            cluster: "nychigh-2026-06-11".into(),
            settle_clean: true,
            days_to_event: None,
        }
    }

    /// REGRESSION GUARD for the self-review CRITICAL: leg prices come from the BOOKS (YES = cheap
    /// venue's YES ask; NO = 1 - dear venue's YES bid), NOT from the pair edge. The buggy version set
    /// YES = (1-edge) ~= 0.97 and NO ~= edge, so the NO leg's limit sat far below its real ask and could
    /// never fill -> a naked YES leg. Here YES must be 7c and NO must be 90c (sum = pair cost = 1-edge).
    /// ALSO pins the leg-market FIX: the Kalshi leg carries the Kalshi TICKER, never the pmus slug.
    #[test]
    fn leg_prices_come_from_books_not_edge() {
        let (pair, q) = (wx_pair(), q_pk());
        let pk = build_legs(&pair, &q, Dir::PK, 1).unwrap();
        // leg A = YES@pmus(slug) @ 7c (pmus YES ask 0.07); leg B = NO@Kalshi(ticker) @ 90c (1 - 0.10).
        assert_eq!((pk[0].venue, pk[0].side, pk[0].price_cents), (Venue::Pmus, Side::Yes, 7));
        assert_eq!(pk[0].market, "tc-temp-nychigh-2026-06-11-gte95f"); // pmus leg -> slug
        assert_eq!((pk[1].venue, pk[1].side, pk[1].price_cents), (Venue::Kalshi, Side::No, 90));
        assert_eq!(pk[1].market, "KXHIGHNY-26JUN11-T95"); // LEG-MARKET FIX: Kalshi leg -> the TICKER, not the slug
        // KP flips cheap/dear: YES@Kalshi(ticker) 0.11; NO@pmus(slug) = 1 - 0.05 = 0.95.
        let kp = build_legs(&pair, &q, Dir::KP, 1).unwrap();
        assert_eq!((kp[0].venue, kp[0].side, kp[0].price_cents), (Venue::Kalshi, Side::Yes, 11));
        assert_eq!(kp[0].market, "KXHIGHNY-26JUN11-T95");
        assert_eq!((kp[1].venue, kp[1].side, kp[1].price_cents), (Venue::Pmus, Side::No, 95));
        assert_eq!(kp[1].market, "tc-temp-nychigh-2026-06-11-gte95f");
    }

    /// SPORTS leg construction: PK = "YES@pmus(slug) and YES@Kalshi-B(ticker_b)"; KP = "YES@Kalshi-A
    /// (ticker_a) and NO@pmus(slug)". Both Kalshi legs carry their own TICKER (the leg-market fix), and
    /// the sports PK second leg is a YES on the AWAY team's book (kb ask), not a NO leg.
    #[test]
    fn sports_legs_use_venue_native_tickers_and_correct_sides() {
        let pair = LivePair {
            slug: "aec-mlb-lad-pit-2026-06-16".into(),
            kalshi: "KXMLBGAME-26JUN16-LAD".into(),
            kalshi_b: Some("KXMLBGAME-26JUN16-PIT".into()),
            cat: Cat::Sports,
            cluster: "mlb-2026-06-16".into(),
            settle_clean: false,
            days_to_event: Some(1.0),
        };
        let q = Quote {
            market: pair.slug.clone(),
            cat: Cat::Sports,
            pm: Book { yes_bid: Some(0.54), yes_ask: Some(0.55), age_s: 0.0 }, // back A@pmus pays 0.55
            k: Book { yes_bid: Some(0.56), yes_ask: Some(0.58), age_s: 0.0 },  // Kalshi-A (LAD) ask 0.58
            k_b: Some(Book { yes_bid: Some(0.40), yes_ask: Some(0.42), age_s: 0.0 }), // Kalshi-B (PIT) ask 0.42
            depth: Depth { c2: 50, c1: 50, c0: 50 },
            settle_clean: false,
            cluster: "mlb-2026-06-16".into(),
            led_by: None,
            days_to_event: Some(1.0),
        };
        // PK: leg A = YES@pmus(slug) @ pm_ask 55c; leg B = YES@Kalshi-B(PIT ticker) @ kB_ask 42c.
        let pk = build_legs(&pair, &q, Dir::PK, 3).unwrap();
        assert_eq!((pk[0].venue, pk[0].side, pk[0].price_cents), (Venue::Pmus, Side::Yes, 55));
        assert_eq!(pk[0].market, "aec-mlb-lad-pit-2026-06-16");
        assert_eq!((pk[1].venue, pk[1].side, pk[1].price_cents), (Venue::Kalshi, Side::Yes, 42));
        assert_eq!(pk[1].market, "KXMLBGAME-26JUN16-PIT", "PK leg2 = YES on the AWAY team's Kalshi TICKER");
        assert!(pk[0].qty == 3 && pk[1].qty == 3);
        // KP: leg A = YES@Kalshi-A(LAD ticker) @ kA_ask 58c; leg B = NO@pmus(slug) @ 1-pm_bid = 46c.
        let kp = build_legs(&pair, &q, Dir::KP, 3).unwrap();
        assert_eq!((kp[0].venue, kp[0].side, kp[0].price_cents), (Venue::Kalshi, Side::Yes, 58));
        assert_eq!(kp[0].market, "KXMLBGAME-26JUN16-LAD", "KP leg1 = YES on the HOME team's Kalshi TICKER");
        assert_eq!((kp[1].venue, kp[1].side, kp[1].price_cents), (Venue::Pmus, Side::No, 46));
        assert_eq!(kp[1].market, "aec-mlb-lad-pit-2026-06-16");
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

    /// A one-sided book (no dear-venue YES bid) yields no NO-leg price -> `build_legs` returns None and
    /// the live loop skips (no naked fire). Pins the "skip rather than leg out" invariant.
    #[test]
    fn one_sided_book_blocks_leg_construction() {
        let (pair, mut q) = (wx_pair(), q_pk());
        q.k.yes_bid = None; // no Kalshi bid -> can't price the NO@Kalshi leg (dir PK)
        assert!(build_legs(&pair, &q, Dir::PK, 1).is_none()); // -> the live loop `continue`s, no naked leg
        // the OTHER direction (KP needs Kalshi YES ask + pmus YES bid) still prices -> two legs.
        assert!(build_legs(&pair, &q, Dir::KP, 1).is_some());
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

    /// `LivePair` carries discovery's settle_clean + the two-ticker sports shape through unchanged so the
    /// risk gate / book wiring is fed the right values per category.
    #[test]
    fn livepair_from_discovery_preserves_settle_clean_and_tickers() {
        let wx = discovery::Pair { slug: "tc-temp-x-2026-06-11-gte95f".into(), kalshi: "K".into(), kalshi_b: None, cat: Cat::Weather, cluster: "x".into(), settle_clean: true, days_to_event: Some(0.0) };
        let ec = discovery::Pair { slug: "urc-x".into(), kalshi: "K2".into(), kalshi_b: None, cat: Cat::Econ, cluster: "u3-26JUN".into(), settle_clean: false, days_to_event: None };
        let sp = discovery::Pair { slug: "aec-mlb-lad-pit-2026-06-16".into(), kalshi: "K-LAD".into(), kalshi_b: Some("K-PIT".into()), cat: Cat::Sports, cluster: "mlb-2026-06-16".into(), settle_clean: false, days_to_event: Some(1.0) };
        assert!(LivePair::from(wx).settle_clean);
        assert!(!LivePair::from(ec).settle_clean);
        // a sports LivePair subscribes BOTH team tickers (kalshi_tickers / pair_tickers parity).
        let sp_lp = LivePair::from(sp.clone());
        assert_eq!(sp_lp.kalshi_tickers(), vec!["K-LAD".to_string(), "K-PIT".to_string()]);
        assert_eq!(pair_tickers(&sp), vec!["K-LAD".to_string(), "K-PIT".to_string()]);
    }

    /// `position_from_intents` builds the held Position straight from the two entry OrderIntents: each leg
    /// = that intent's venue/market/side, the pair `market` = the slug, size = the intent qty. The unwind
    /// then SELLs back the exact (venue, market, side) — so this round-trips a sports PK fill (YES@pmus +
    /// YES@Kalshi-B) into a Position whose two legs are both YES, on the right venues/tickers.
    #[test]
    fn position_from_intents_records_exact_legs() {
        let legs = [
            OrderIntent { venue: Venue::Pmus, market: "aec-mlb-lad-pit-2026-06-16".into(), action: Action::Buy, side: Side::Yes, price_cents: 55, qty: 7, client_order_id: "xarb-…-A".into() },
            OrderIntent { venue: Venue::Kalshi, market: "KXMLBGAME-26JUN16-PIT".into(), action: Action::Buy, side: Side::Yes, price_cents: 42, qty: 7, client_order_id: "xarb-…-B".into() },
        ];
        let pos = position_from_intents("aec-mlb-lad-pit-2026-06-16", Cat::Sports, "mlb-2026-06-16", &legs);
        assert_eq!(pos.market, "aec-mlb-lad-pit-2026-06-16"); // pair identity = the pmus slug
        assert_eq!(pos.size, 7);
        assert_eq!(pos.cluster, "mlb-2026-06-16");
        assert_eq!(pos.legs[0], PositionLeg { venue: Venue::Pmus, market: "aec-mlb-lad-pit-2026-06-16".into(), side: Side::Yes });
        assert_eq!(pos.legs[1], PositionLeg { venue: Venue::Kalshi, market: "KXMLBGAME-26JUN16-PIT".into(), side: Side::Yes });
        // the unwind SELLs back the EXACT legs (venue/market/side), priced at the supplied exit cents.
        let u = unwind::unwind_orders(&pos, [98, 55]);
        assert_eq!((u[0].action, u[0].venue, u[0].side, u[0].market.as_str()), (Action::Sell, Venue::Pmus, Side::Yes, "aec-mlb-lad-pit-2026-06-16"));
        assert_eq!((u[1].action, u[1].venue, u[1].side, u[1].market.as_str()), (Action::Sell, Venue::Kalshi, Side::Yes, "KXMLBGAME-26JUN16-PIT"));
    }

    /// The exit-pricing helper: a SELL YES leg lifts that book's best `yes_bid`; a SELL NO leg nets
    /// `1 - yes_ask` (selling NO = buying YES back at the ask). A missing needed side -> None (one-sided
    /// book) so `unwind_exit_cents` declines to price the pair and the caller holds.
    #[test]
    fn exit_pricing_yes_takes_bid_no_takes_one_minus_ask() {
        let yes_leg = PositionLeg { venue: Venue::Pmus, market: "s".into(), side: Side::Yes };
        let no_leg = PositionLeg { venue: Venue::Kalshi, market: "K".into(), side: Side::No };
        let book = Book { yes_bid: Some(0.98), yes_ask: Some(0.99), age_s: 0.0 };
        assert_eq!(exit_price(&yes_leg, &book), Some(0.98)); // SELL YES -> hit the YES bid
        assert_eq!(exit_price(&no_leg, &book), Some(1.0 - 0.99)); // SELL NO -> 1 - YES ask = 0.01
        // a YES leg with no bid -> None; a NO leg with no ask -> None (one-sided book).
        assert_eq!(exit_price(&yes_leg, &Book { yes_bid: None, yes_ask: Some(0.99), age_s: 0.0 }), None);
        assert_eq!(exit_price(&no_leg, &Book { yes_bid: Some(0.98), yes_ask: None, age_s: 0.0 }), None);

        // unwind_exit_cents prices BOTH legs (here a YES@pmus + NO@Kalshi weather pair) from their books.
        let pos = Position {
            market: "tc-temp-nychigh-2026-06-11-gte95f".into(), cat: Cat::Weather,
            legs: [yes_leg.clone(), no_leg.clone()], size: 1, cluster: "c".into(),
        };
        // YES@pmus bid 0.98 -> 98c; NO@Kalshi 1 - ask(0.11) = 0.89 -> 89c.
        let pm = Book { yes_bid: Some(0.98), yes_ask: Some(0.99), age_s: 0.0 };
        let k = Book { yes_bid: Some(0.10), yes_ask: Some(0.11), age_s: 0.0 };
        let cents = unwind_exit_cents(&pos, |leg| if leg.venue == Venue::Pmus { Some(pm) } else { Some(k) }).unwrap();
        assert_eq!(cents, [98, 89]);
        // one leg's book missing the needed side -> the whole pair declines to price (hold).
        assert!(unwind_exit_cents(&pos, |leg| if leg.venue == Venue::Pmus { Some(Book { yes_bid: None, yes_ask: Some(0.99), age_s: 0.0 }) } else { Some(k) }).is_none());
    }

    /// `track_position` then `decrement_exposure` round-trips exposure to zero (caps bind on entry, re-open
    /// on flatten), and a SPORTS pair derives league/date/abbrevs for the poll while non-sports leaves them
    /// empty.
    #[test]
    fn track_and_decrement_exposure_round_trips() {
        use std::sync::{Arc, Mutex};
        let positions = Arc::new(Mutex::new(std::collections::HashMap::new()));
        let mut exp = Exposure::new();
        let pair = LivePair {
            slug: "aec-mlb-lad-pit-2026-06-16".into(),
            kalshi: "KXMLBGAME-26JUN16-LAD".into(),
            kalshi_b: Some("KXMLBGAME-26JUN16-PIT".into()),
            cat: Cat::Sports, cluster: "mlb-2026-06-16".into(), settle_clean: false, days_to_event: Some(1.0),
        };
        let legs = [
            OrderIntent { venue: Venue::Pmus, market: pair.slug.clone(), action: Action::Buy, side: Side::Yes, price_cents: 55, qty: 4, client_order_id: "a".into() },
            OrderIntent { venue: Venue::Kalshi, market: "KXMLBGAME-26JUN16-PIT".into(), action: Action::Buy, side: Side::Yes, price_cents: 42, qty: 4, client_order_id: "b".into() },
        ];
        let pos = position_from_intents(&pair.slug, pair.cat, &pair.cluster, &legs);
        track_position(&positions, &pair, pos, &mut exp, 0.97);
        // exposure bumped by cost_per×size = 0.97×4 = 3.88 across pair/cluster/total; one open position.
        assert!((exp.total - 3.88).abs() < 1e-9);
        assert!((exp.per_pair["aec-mlb-lad-pit-2026-06-16"] - 3.88).abs() < 1e-9);
        assert!((exp.per_cluster["mlb-2026-06-16"] - 3.88).abs() < 1e-9);
        assert_eq!(exp.open_positions, 1);
        // the held position carries the poll's match fields (league/date/abbrevs from the slug + tickers).
        let hp = positions.lock().unwrap().get(&pair.slug).cloned().unwrap();
        assert_eq!((hp.league.as_str(), hp.date.as_str(), hp.team_a.as_str(), hp.team_b.as_str()), ("mlb", "2026-06-16", "lad", "pit"));
        // flatten -> exposure back to zero, position gone.
        decrement_exposure(&mut exp, &hp.pos);
        assert!(exp.total.abs() < 1e-9 && exp.open_positions == 0);
        assert!(!exp.per_pair.contains_key("aec-mlb-lad-pit-2026-06-16"));
    }

    /// PairState registers BOTH sports tickers in `by_ticker` -> slug, and `remove` frees both (so a
    /// frame on either team's Kalshi book finds the pair, and a pruned pair leaves no dangling index).
    #[test]
    fn pairstate_indexes_and_frees_both_sports_tickers() {
        let mut ps = PairState::default();
        ps.insert(LivePair { slug: "aec-mlb-lad-pit-2026-06-16".into(), kalshi: "K-LAD".into(), kalshi_b: Some("K-PIT".into()), cat: Cat::Sports, cluster: "mlb-2026-06-16".into(), settle_clean: false, days_to_event: Some(1.0) });
        assert_eq!(ps.by_ticker.get("K-LAD").map(String::as_str), Some("aec-mlb-lad-pit-2026-06-16"));
        assert_eq!(ps.by_ticker.get("K-PIT").map(String::as_str), Some("aec-mlb-lad-pit-2026-06-16"));
        ps.remove("aec-mlb-lad-pit-2026-06-16");
        assert!(!ps.by_ticker.contains_key("K-LAD") && !ps.by_ticker.contains_key("K-PIT"));
        assert!(ps.by_slug.is_empty());
    }
}
