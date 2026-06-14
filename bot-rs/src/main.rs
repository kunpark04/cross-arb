//! cross-arb live trading bot — entry point. **SAFE BY DEFAULT** (decision 0015):
//!   * dry-run unless `EXECUTION_MODE=live`
//!   * demo/sandbox venue unless `VENUE_ENV=prod` (+ an explicit informed-consent env for prod)
//!   * 1-contract / tiny-notional caps + global kill-switch
//!   * the read-write key is loaded from the owner's path at RUNTIME; never read/copied by Claude
//!
//! Live order submission is a deliberate OWNER action gated by the safe-by-default rails above — NOT
//! because the sandbox blocks it (verified 2026-06-11: this environment reaches both venues with auth
//! and placed+cancelled live 1¢ orders). See `bot-rs/README.md`.
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

    // C8: disabling the SETTLEMENT-CLEAN gate (the catastrophic both-legs-loss axis) on live+prod is the
    // single most consequential safety toggle, and unlike the prod gate it had NO informed-consent
    // backstop — one leftover env var silently removed the last structural guard against an un-reconciled
    // econ/sports pair trading. Mirror the prod gate: refuse to start unless a second explicit consent env
    // is set. The per-category `assume_*` flags remain the intended, safer way to enable a category.
    if cfg.is_live() && cfg.is_prod() && !cfg.require_settle_clean
        && std::env::var("CROSSARB_I_UNDERSTAND_NO_SETTLE_GATE").as_deref() != Ok("yes")
    {
        eprintln!("\nREFUSING TO START: REQUIRE_SETTLE_CLEAN=false on live+prod disables the settlement-identity");
        eprintln!("gate for ALL categories at once (the catastrophic both-legs-loss axis). Set");
        eprintln!("CROSSARB_I_UNDERSTAND_NO_SETTLE_GATE=yes to confirm, or prefer the per-category");
        eprintln!("ASSUME_SPORTS_SETTLED / ASSUME_ECON_SETTLED overrides (safe per-category control).");
        std::process::exit(2);
    }

    let backend: std::sync::Arc<dyn ExecutionBackend> = match cfg.mode {
        ExecutionMode::DryRun => std::sync::Arc::new(DryRunBackend),
        ExecutionMode::Live => std::sync::Arc::new(LiveBackend::new(&cfg)),
    };
    println!("execution backend : {}\n", backend.label());

    // STAGE-1 smoke vs STAGE-2 live loop. The smoke runs the real risk+exec spine on synthetic
    // snapshots (no network) and is the path of least resistance: it runs when `--smoke` is passed OR
    // when venue creds aren't available (Claude's sandbox / a fresh checkout). The live loop connects
    // both venue WS streams and runs the match->signal->risk->exec pipeline (decision 0015 gated rails).
    let force_smoke = std::env::args().any(|a| a == "--smoke");
    match (force_smoke, venue::VenueCreds::from_env()) {
        (false, Ok(creds)) => {
            run_live(&cfg, backend, std::sync::Arc::new(creds)).await;
        }
        (_, Err(why)) if !force_smoke => {
            println!("[startup] venue creds unavailable ({why}) -> running offline smoke instead.\n");
            smoke(&cfg, backend.as_ref());
        }
        _ => smoke(&cfg, backend.as_ref()),
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
        "edge-rate floor   : {:.2}c/$-day{}",
        cfg.min_edge_rate_cpd,
        if cfg.min_edge_rate_cpd > 0.0 { "  (velocity gate ON)" } else { "  (off)" }
    );
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
    if !cfg.require_settle_clean {
        // C8: the settlement-identity gate (the catastrophic axis) is OFF — make it impossible to miss.
        println!("*** SETTLE-CLEAN GATE DISABLED *** (REQUIRE_SETTLE_CLEAN=false) — every category trades UNVERIFIED");
    }
    // SCALE-IN/RE-ENTRY arming — loud when ANY knob departs from the safe default (both off, cap 1), so an
    // armed add-to-held config is impossible to miss (mirrors the settle-clean banner). Silent at defaults.
    if cfg.enable_scale_in || cfg.enable_reentry || cfg.max_positions_per_slug != 1 {
        println!(
            "*** ADD-TO-HELD ARMED *** scale_in={} reentry={} max_positions/slug={} add_tau_gain={:.1}c — a held slug can be ADDED to",
            cfg.enable_scale_in, cfg.enable_reentry, cfg.max_positions_per_slug, cfg.add_tau_gain * 100.0
        );
    }
    if cfg.kill_switch {
        println!("KILL SWITCH       : ENGAGED (CROSSARB_KILL) - no trading");
    }
    println!();
}

/// A co-listed pair the live loop tracks. Weather/econ are 1:1 (`kalshi_b = None`). MONEYLINE SPORTS is
/// 2-outcome: `kalshi` = team-A ticker (the team pmus lists as YES), `kalshi_b = Some(team-B ticker)` — BOTH
/// Kalshi books are subscribed and the 2-outcome `game_signal` needs both. A WORLD-CUP outcome is a BINARY
/// pair (`kalshi_b = None`, `soccer = true`) routed through the 1:1 `signal` path. Filled by `discovery`.
#[derive(Clone, Debug)]
struct LivePair {
    slug: String,         // pmus market slug
    kalshi: String,       // Kalshi ticker (team-A ticker for moneyline sports)
    kalshi_b: Option<String>, // MONEYLINE SPORTS: team-B (away) Kalshi ticker; None for weather/econ/WC
    cat: Cat,
    cluster: String,
    settle_clean: bool,
    /// WORLD-CUP per-outcome marker (see `discovery::Pair::soccer`): a binary `Cat::Sports` pair routed via
    /// `signal`. Lets the loop skip enrolling a WC pair in the MLB-only postponement poll (no statsapi source).
    soccer: bool,
    days_to_event: Option<f64>,
    /// pmus per-market order constraints (FIX C): price tick the pmus leg must be a multiple of, and the
    /// minimum order qty pmus accepts. `None` -> the leg builder leaves the price unquantized / skips the
    /// min-qty check. Kalshi is integer-cent + whole-share, so these only gate the pmus leg.
    pm_min_tick: Option<f64>,
    pm_min_qty: Option<f64>,
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
        LivePair { slug: p.slug, kalshi: p.kalshi, kalshi_b: p.kalshi_b, cat: p.cat, cluster: p.cluster, settle_clean: p.settle_clean, soccer: p.soccer, days_to_event: p.days_to_event, pm_min_tick: p.pm_min_tick, pm_min_qty: p.pm_min_qty }
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

/// Which kind of submission an outcome belongs to (Entry opens a position; Unwind flattens a held pair;
/// Recovery is the single-leg flatten of a naked leg from a half-filled entry — FIX A).
#[derive(Clone, Copy, Debug, PartialEq)]
enum SubmitKind {
    Entry,
    Unwind,
    Recovery,
}

/// WHAT holds a slug's in-flight `flattening` slot (W-1). The bare `HashSet<slug>` couldn't distinguish a
/// RECOVERY (the single-leg flatten of THIS slug's one naked leg) from an UNWIND (a postpone flatten that
/// targets only the FRONT held leg `legs[0]`). That conflation let an unwind-held slot make a concurrently
/// half-filled ADD's recovery short-circuit as "already covered" — but the unwind does NOT cover the add's
/// just-naked leg, so the add leg was silently abandoned. Recording the kind lets `recover_naked_leg` treat
/// ONLY a recovery-held slot as covering this naked leg; an unwind-held slot fails CLOSED (halt). Both kinds
/// still de-dup a second concurrent flatten on the slug (the slot-occupied check is kind-agnostic).
#[derive(Clone, Copy, Debug, PartialEq)]
enum FlatKind {
    Recovery,
    Unwind,
}

/// The result of a SPAWNED `submit_pair`, sent back to the event loop so ALL position/exposure bookkeeping
/// happens on the loop's own turn (never on the network task). Decouples the two-leg RTT from the
/// `select!` so the unwind arm stays hot while an entry is in flight (concurrency-core fix C4/C5/W14/W16).
struct SubmitOutcome {
    slug: String,
    kind: SubmitKind,
    ack: exec::PairAck,
    /// Entry only: the position to RECORD if both legs filled (else the reservation is released). `None`
    /// for an unwind (which REMOVES an existing position on a both-filled flatten).
    position: Option<Position>,
    /// Entry only: the pair record (the poll needs its league/date/abbrevs to match statsapi). `None` for unwind.
    pair: Option<LivePair>,
    /// Entry only: the per-contract cost the exposure reservation used (so a release decrements the exact amount).
    cost_per: f64,
    /// Entry only: the gated edge's net + direction, STORED on the appended `HeldLeg` so a later add can gate
    /// on `max(entry_net)+tau` and same-direction (design §1/§2). Ignored for unwind/recovery (0.0 / a dummy).
    entry_net: f64,
    entry_dir: Dir,
}

/// STAGE-2 live loop: discover the co-listed universe, connect both venue WS streams, maintain a book
/// per venue for each tracked pair, and on each COMPLETE dual-venue update (L5) build a `Quote`, run
/// `risk::evaluate`, and SPAWN the `submit_pair` (dry-run default). A periodic refresh task re-discovers
/// and applies in-place subscribe add/prune. Honors the kill-switch + the already-checked prod-consent gate.
///
/// The submission path is SPAWNED + ACKED (never inline): an approved entry / triggered unwind clones the
/// `Arc<backend>` into a `tokio::spawn`ed task that drives the two-leg POST and reports a `SubmitOutcome`
/// back over `outcome_rx`; a THIRD `select!` arm does the record/remove/exposure/naked-leg bookkeeping on
/// the loop's turn. So the event loop NEVER blocks on the network and the unwind arm cannot be starved by
/// an in-flight entry. Concurrent in-flight work is de-duplicated by `pending_entries`/`flattening`.
async fn run_live(cfg: &Config, backend: std::sync::Arc<dyn ExecutionBackend>, creds: std::sync::Arc<venue::VenueCreds>) {
    use std::collections::{HashMap, HashSet};
    use std::sync::atomic::{AtomicBool, Ordering};
    use std::sync::{Arc, Mutex};

    let http = reqwest::Client::builder().use_rustls_tls().build().unwrap_or_else(|_| reqwest::Client::new());

    // INITIAL DISCOVERY (PUBLIC, no-auth catalog pull). A degraded/empty first pass is not fatal — seed
    // with whatever discovery returns (possibly nothing) and let the refresh task fill in; the loop never
    // invents a universe to trade.
    let initial = match discovery::discover(&http).await {
        Ok(d) => {
            println!(
                "[discovery] {} weather + {} econ + {} sports + {} world-cup pairs tracked ({} total subscribable)",
                d.weather_pairs, d.econ_pairs, d.sports_pairs, d.soccer_pairs, d.pairs.len()
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
        let mut ps = lock(&pairs);
        let mut kt = lock(&k_tracked);
        let mut pt = lock(&pm_tracked);
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

    // HELD positions keyed by pmus slug -> `SlugPositions` (a Vec of stacked `HeldLeg`s + slug-level game
    // metadata/prev). The postponement poll reads these (MLB sports); the entry path APPENDS a leg on a
    // both-filled fill so exposure caps bind across the session and a void can flatten every leg. With the
    // default cap=1 there is at most one leg per slug (the one-position-per-slug bot). Shared with the poll.
    let positions: Arc<Mutex<HashMap<String, postpone::SlugPositions>>> = Arc::new(Mutex::new(HashMap::new()));

    // IN-FLIGHT de-dup (C5): a slug with a SPAWNED-but-unacked entry is in `pending_entries`; a slug with a
    // spawned-but-unacked flatten (unwind OR recovery) is a key in `flattening`, mapped to WHICH kind holds
    // it (W-1). The loop refuses a second entry/unwind for a slug already in-flight, so a burst of frames (or
    // the poll's per-cycle re-emit) can't double-fire; the recorded kind lets recovery tell an unwind-held
    // slot (which doesn't cover a freshly-naked add leg) apart from a recovery-held one.
    let mut pending_entries: HashSet<String> = HashSet::new();
    let mut flattening: HashMap<String, FlatKind> = HashMap::new();

    // SCALE-IN vs RE-ENTRY proxy (design §3): a slug is in `edge_live` while a same-direction qualifying arb
    // is currently present on it (inserted/removed each frame, below). At ADD time `edge_live.contains(slug)`
    // => SCALE-IN (base episode still OPEN), else RE-ENTRY (base held, its edge already closed). Conservative
    // + cheap; only ever consulted when a held slug yields a fresh approvable edge (the add path).
    let mut edge_live: HashSet<String> = HashSet::new();

    // RUNTIME halt (W14/C1): set TRUE by a naked-leg-on-live fail-close or a dead supervised task. Distinct
    // from the config kill-switch — this is tripped at runtime and blocks every NEW entry from here on.
    let halt = Arc::new(AtomicBool::new(false));

    // PER-PAIR Kalshi freshness (C3): a ticker is "fresh" once a book frame has arrived for it SINCE the
    // last Kalshi reconnect/seq-gap (which `clear()`s the shared book map). A pair only trades when EVERY
    // Kalshi ticker it uses is fresh — so a just-cleared book can't be traded against a never-cleared pmus
    // book on the first rebuilt frame. Cleared wholesale on Kalshi Reconnect/SeqGap.
    let mut k_fresh: HashSet<String> = HashSet::new();

    let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel::<venue::VenueEvent>();
    let (k_subs_tx, k_subs_rx) = tokio::sync::mpsc::unbounded_channel::<venue::SubUpdate>();
    let (pm_subs_tx, pm_subs_rx) = tokio::sync::mpsc::unbounded_channel::<venue::SubUpdate>();
    // the postponement-unwind channel: the poll detects a void and sends an UnwindRequest the loop fires.
    let (unwind_tx, mut unwind_rx) = tokio::sync::mpsc::unbounded_channel::<postpone::UnwindRequest>();
    // the SUBMISSION outcome channel: a spawned submit task reports its PairAck back here for bookkeeping.
    let (outcome_tx, mut outcome_rx) = tokio::sync::mpsc::unbounded_channel::<SubmitOutcome>();

    // SUPERVISED tasks (C1): a dead collector/refresh/poll must HALT trading (never trade a frozen book),
    // so we keep their JoinHandles and a `select!` arm treats any of them ending/panicking as FATAL.
    let mut k_handle = tokio::spawn(venue::kalshi_stream(creds.clone(), k_tracked.clone(), kalshi_books.clone(), tx.clone(), k_subs_rx));
    let mut pm_handle = tokio::spawn(venue::pmus_stream(creds.clone(), pm_tracked.clone(), tx.clone(), pm_subs_rx));
    let mut refresh_handle = tokio::spawn(refresh_loop(
        http.clone(),
        cfg.discovery_refresh_s,
        pairs.clone(),
        k_tracked.clone(),
        pm_tracked.clone(),
        kalshi_books.clone(),
        positions.clone(),
        k_subs_tx,
        pm_subs_tx,
    ));
    // ARM the live postponement-unwind trigger (gated on cfg.auto_unwind). statsapi is public/keyless; the
    // poll runs on the owner's droplet. Disengage with CROSSARB_NO_AUTO_UNWIND=1 (a manual-only fallback).
    let mut poll_handle = if cfg.auto_unwind {
        Some(tokio::spawn(postpone::poll_mlb_postponements(
            http.clone(),
            positions.clone(),
            unwind_tx.clone(),
            cfg.postpone_poll_s,
            cfg.kalshi_void_window_days,
        )))
    } else {
        None
    };
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

    // HEALTH HEARTBEAT: a 24/7 live bot must be observable. Every 20s log book/pair counts + the frame
    // count so a silently-wedged stream (task alive but delivering no frames — which the task supervisor
    // does NOT catch) is visible. `events` counts venue book frames processed since start.
    let mut heartbeat = tokio::time::interval(std::time::Duration::from_secs(20));
    heartbeat.tick().await; // consume the immediate first tick
    let mut events: u64 = 0;

    loop {
        // C1 (robustness): a supervised task can finish WHILE the loop is in its body — and the guarded
        // `select!` arms below (`if !is_finished()`) are then DISABLED, so that death would never wake the
        // select. Re-check at the TOP of every iteration: any finished collector/refresh/poll = FATAL halt
        // (a dead data source means a frozen book). The guarded arms still cover a death during an idle await.
        if k_handle.is_finished() || pm_handle.is_finished() || refresh_handle.is_finished()
            || poll_handle.as_ref().is_some_and(|h| h.is_finished())
        {
            halt.store(true, Ordering::Relaxed);
            eprintln!("[live] CRITICAL a supervised collector/refresh/poll task ended (loop-top check) — halting");
            break;
        }

        // Drive the venue book stream, the postponement-unwind channel, the submission-outcome channel,
        // AND the task supervisor — all co-equal. A void detection / a settled ack / a dead collector must
        // never wait behind a quiet book stream, so none of them is polled only between frames.
        let ev = tokio::select! {
            v = rx.recv() => match v {
                Some(ev) => ev,
                None => break, // both venue streams ended
            },
            u = unwind_rx.recv() => {
                if let Some(req) = u {
                    spawn_unwind(cfg, &backend, &positions, &kalshi_books, &pmus_books, &mut flattening, &outcome_tx, &req.slug);
                }
                continue;
            }
            o = outcome_rx.recv() => {
                if let Some(out) = o {
                    apply_outcome(&backend, &positions, &kalshi_books, &pmus_books, &mut exposure, &mut pending_entries, &mut flattening, &outcome_tx, &halt, out);
                }
                continue;
            }
            _ = heartbeat.tick() => {
                let (kb, pmb) = (lock(&kalshi_books).len(), pmus_books.len());
                let pn = lock(&pairs).by_slug.len();
                println!("[live] heartbeat: {pn} pairs, {kb} kalshi books, {pmb} pmus books, {events} frames, k_fresh={}, paused={}",
                         k_fresh.len(), exposure.stream_paused);
                continue;
            }
            // C1 SUPERVISOR: any collector/refresh/poll task ending (clean return OR panic) is FATAL — a
            // dead data source means a frozen book, so HALT and stop trading rather than trade stale data.
            r = &mut k_handle, if !k_handle.is_finished() => { supervise_fatal("kalshi_stream", r, &halt); break; }
            r = &mut pm_handle, if !pm_handle.is_finished() => { supervise_fatal("pmus_stream", r, &halt); break; }
            r = &mut refresh_handle, if !refresh_handle.is_finished() => { supervise_fatal("refresh_loop", r, &halt); break; }
            r = poll_opt(&mut poll_handle), if poll_handle.is_some() => { supervise_fatal("poll_mlb_postponements", r, &halt); break; }
        };
        events = events.wrapping_add(1); // a venue book frame fell through the select (heartbeat activity)
        // which pmus slug does this event touch? (book updates first, then evaluate on the complete state)
        let slug = match &ev {
            venue::VenueEvent::Kalshi { ticker } => {
                k_rebuild = false; // a Kalshi book frame -> its rebuild is flowing again
                k_fresh.insert(ticker.clone()); // C3: this ticker's book has a post-reconnect frame
                lock(&pairs).by_ticker.get(ticker).cloned()
            }
            venue::VenueEvent::Pmus { slug, bids, asks } => {
                pm_rebuild = false; // a pmus book frame -> its rebuild is flowing again
                if lock(&pm_tracked).contains(slug) {
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
                    Venue::Kalshi => {
                        k_rebuild = true;
                        k_fresh.clear(); // C3: the stream `clear()`s every Kalshi book on reconnect -> none fresh
                    }
                    Venue::Pmus => pm_rebuild = true,
                }
                exposure.stream_paused = k_rebuild || pm_rebuild;
                continue;
            }
            venue::VenueEvent::SeqGap { seq } => {
                println!("[live] Kalshi seq gap at {seq} — connection cycling; Kalshi entries paused");
                k_rebuild = true;
                k_fresh.clear(); // C3: the gap cycles the connection -> books re-snapshot -> none fresh yet
                exposure.stream_paused = true;
                continue;
            }
        };
        exposure.stream_paused = k_rebuild || pm_rebuild; // recompute after a (possibly) clearing frame
        let Some(slug) = slug else { continue }; // a Kalshi ticker we don't track
        // clone the pair record out so we don't hold the pairs lock across the Quote build / book locks
        // (the refresh task may be mutating the map concurrently).
        let Some(pair) = lock(&pairs).by_slug.get(&slug).cloned() else { continue };

        // C3 FRESHNESS GATE: require a post-reconnect frame for EVERY Kalshi ticker this pair uses before
        // building a Quote. This blocks trading a just-cleared Kalshi book (against a never-cleared pmus
        // book) until its snapshot rebuilds, while a 1:1 pair whose ticker just arrived trades correctly.
        if !pair.kalshi_tickers().iter().all(|t| k_fresh.contains(t)) {
            continue;
        }

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
            let kb = lock(&kalshi_books);
            let Some(ka_book) = kb.get(&pair.kalshi) else { continue }; // no Kalshi-A book yet -> incomplete
            let k = ka_book.touch();
            let Some(pmb) = pmus_books.get(&slug) else { continue }; // W17: guarded re-read, never unwrap
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

        // SCALE-IN/RE-ENTRY proxy bookkeeping (design §3): is a same-direction positive edge present on this
        // slug THIS frame? `was_live` reads the PRIOR-frame state (read-before-write) so a continuously-live
        // edge classifies an add as SCALE-IN, while a closed-then-reopened edge classifies as RE-ENTRY. We
        // snapshot the held legs once (their dirs/entry_nets gate the add) and recompute membership below.
        let held_legs: Vec<postpone::HeldLeg> = lock(&positions).get(&slug).map(|sp| sp.legs.clone()).unwrap_or_default();
        let same_dir_live = !held_legs.is_empty()
            && edge.net > 0.0
            && held_legs.iter().all(|l| l.entry_dir == edge.dir);
        let was_live = edge_live.contains(&slug);
        if same_dir_live {
            edge_live.insert(slug.clone());
        } else {
            edge_live.remove(&slug);
        }

        // RUNTIME-HALT + IN-FLIGHT de-dup, BEFORE the gate: don't even price a slug that is halted or already
        // has an entry/unwind in flight (C5 — a second concurrent entry would double-reserve). These three
        // clauses are UNCHANGED. The old 4th clause (`positions.contains_key` => block ALL re-entry) is
        // replaced by `qualifying_add` below: held slugs are no longer blanket-blocked, but an ADD must pass
        // the same-direction + tau-gain + flag + count-cap gate (and with the SAFE defaults — both flags off,
        // cap=1 — `qualifying_add` ALWAYS returns None for a held slug => `continue`, i.e. today's behavior).
        if halt.load(Ordering::Relaxed)
            || pending_entries.contains(&slug)
            || flattening.contains_key(&slug)
        {
            continue;
        }

        // HELD-SLUG ADD GATE: if the slug already has legs, this would be an add — gate it. `None` => block
        // (exactly the old `contains_key` continue). `Some(tag)` => a qualifying add; fall through to
        // `evaluate` so the notional/count caps then bound it (the held legs' contribution is already in the
        // exposure buckets, so per-pair/cluster/total room is what's LEFT). An UNHELD slug skips this (fresh
        // entry, zero change to the common case).
        let add_tag: Option<&'static str> = if held_legs.is_empty() {
            None
        } else {
            match qualifying_add(cfg, &held_legs, &edge, was_live) {
                Some(tag) => Some(tag),
                None => continue, // held but not a qualifying add -> block (== old one-position guard)
            }
        };

        if let Ok(a) = evaluate(cfg, &quote, &edge, &exposure, affordable(cfg, &edge, &exposure)) {
            // Build BOTH legs with venue-native market ids + per-leg LIMIT prices from the BOOKS (never
            // derived from the pair edge — that was a self-review CRITICAL). A missing book price (a
            // one-sided book) yields no legs -> skip rather than fire a naked leg.
            let Some(legs) = build_legs(&pair, &quote, edge.dir, a.size) else { continue };
            // W6: re-validate the edge from the ROUNDED leg prices (each leg rounds to a whole cent
            // independently, eroding up to +1c of cost). Skip the fire if the realized net fell under the
            // floor or the pair would cost >= 100c — the gated edge and the booked edge must agree.
            if !realized_edge_clears_floor(cfg, &legs) {
                continue;
            }
            // Record the velocity metric on the live order path (the owner calibrates MIN_EDGE_RATE_CPD
            // against this accruing distribution): every fired ENTRY logs its edge + edge_rate (¢/$-day). An
            // ADD additionally logs its scale-in|re-entry tag + the base vs add net so an armed add is
            // auditable (design §2). `add_tag` is None for a fresh entry (the common case).
            if let Some(tag) = add_tag {
                let base_net = held_legs.iter().map(|l| l.entry_net).fold(0.0_f64, f64::max);
                println!(
                    "[live] ADD({tag}) {slug}  base_net={:.1}c  add_net={:.1}c  size={}  @ {:.2}c/$-day  dir={:?}",
                    base_net * 100.0, edge.net * 100.0, a.size, a.edge_rate, edge.dir
                );
            } else {
                println!(
                    "[live] ENTRY {slug}  size={}  edge={:.1}c @ {:.2}c/$-day  dir={:?}",
                    a.size, edge.net * 100.0, a.edge_rate, edge.dir
                );
            }
            // RESERVE exposure NOW (on spawn), so concurrent in-flight entries can't over-allocate; the
            // outcome arm keeps the reservation on a both-filled fill (appends the leg) or releases it.
            let pos = position_from_intents(&slug, pair.cat, &pair.cluster, pair.pm_min_tick, &legs);
            reserve_exposure(&mut exposure, &pos, a.cost_per);
            pending_entries.insert(slug.clone());
            spawn_submit(&backend, &outcome_tx, SubmitKind::Entry, slug.clone(), legs, Some(pos), Some(pair.clone()), a.cost_per, edge.net, edge.dir);
        }
    }
    if halt.load(Ordering::Relaxed) {
        println!("[live] HALTED (kill-switch engaged at runtime) — loop exiting; no further entries.");
    } else {
        println!("[live] both venue streams ended — loop exiting.");
    }
}

/// Poison-tolerant `Mutex::lock` for the event-loop path (C1): a thread that panicked WHILE holding a lock
/// poisons it; `unwrap()` would then cascade-panic the loop. We recover the guard instead — one bad task
/// must not take down the trading loop. (The data behind the lock is plain book/exposure state; a partial
/// write is at worst a stale read the gates already tolerate, not a safety invariant.)
fn lock<T>(m: &std::sync::Mutex<T>) -> std::sync::MutexGuard<'_, T> {
    m.lock().unwrap_or_else(|e| e.into_inner())
}

/// Poll an `Option<JoinHandle>` inside `select!` only when it is `Some` (the `if poll_handle.is_some()`
/// guard gates this). `unwrap` is sound under that guard; `pending()` parks forever when the poll is
/// disabled so the arm never fires.
async fn poll_opt(h: &mut Option<tokio::task::JoinHandle<()>>) -> Result<(), tokio::task::JoinError> {
    match h {
        Some(handle) => handle.await,
        None => std::future::pending().await,
    }
}

/// A supervised collector/refresh/poll task ended — log CRITICAL + engage the runtime halt. Any termination
/// (clean return, error, or panic) of a data-source task means the book it feeds is now frozen, so the only
/// safe action is to STOP: trading on a frozen book is the failure this guard exists to prevent.
fn supervise_fatal(name: &str, res: Result<(), tokio::task::JoinError>, halt: &std::sync::atomic::AtomicBool) {
    halt.store(true, std::sync::atomic::Ordering::Relaxed);
    match res {
        Ok(()) => eprintln!("[live] CRITICAL supervised task '{name}' ENDED — halting (a dead data source means a frozen book)"),
        Err(e) => eprintln!("[live] CRITICAL supervised task '{name}' PANICKED ({e}) — halting"),
    }
}

/// SPAWN a `submit_pair` off a cloned `Arc<backend>` and report the `PairAck` back over `outcome_tx`. The
/// event-loop NEVER calls `submit_pair` inline (it would block the whole `select!` for the two-leg RTT);
/// this detaches the network I/O so the loop keeps draining the unwind / outcome arms.
#[allow(clippy::too_many_arguments)]
fn spawn_submit(
    backend: &std::sync::Arc<dyn ExecutionBackend>,
    outcome_tx: &tokio::sync::mpsc::UnboundedSender<SubmitOutcome>,
    kind: SubmitKind,
    slug: String,
    legs: [OrderIntent; 2],
    position: Option<Position>,
    pair: Option<LivePair>,
    cost_per: f64,
    entry_net: f64,
    entry_dir: Dir,
) {
    let backend = backend.clone();
    let outcome_tx = outcome_tx.clone();
    tokio::spawn(async move {
        // block_in_place requires the multi-thread runtime (#[tokio::main] full); the submit drives the two
        // signed POSTs concurrently inside it. Running it on a SPAWNED task means only this task parks, not
        // the event loop. catch_unwind GUARANTEES an outcome is reported even if `submit_pair` panics —
        // otherwise the slug stays stuck in pending_entries/flattening forever (never re-tradeable / never
        // re-flattenable). A panic maps to a failed pair, so the outcome arm releases the reservation + slug.
        let ack = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            backend.submit_pair(&legs[0], &legs[1])
        }))
        .unwrap_or_else(|_| {
            eprintln!("[live] CRITICAL submit task panicked for {slug} — reporting a failed pair (slug released)");
            exec::PairAck {
                a: Err(exec::ExecError::Rejected("submit panicked".into())),
                b: Err(exec::ExecError::Rejected("submit panicked".into())),
            }
        });
        let _ = outcome_tx.send(SubmitOutcome { slug, kind, ack, position, pair, cost_per, entry_net, entry_dir });
    });
}

/// RESERVE exposure for an entry the moment it is SPAWNED (not when it acks), so two concurrent in-flight
/// entries can't both size against the same free room and over-allocate (C5). The outcome arm then either
/// keeps the reservation (records the position on a both-filled fill) or releases it (any other outcome).
fn reserve_exposure(exposure: &mut Exposure, pos: &Position, cost_per: f64) {
    let notional = cost_per * pos.size as f64;
    *exposure.per_pair.entry(pos.market.clone()).or_insert(0.0) += notional;
    *exposure.per_cluster.entry(pos.cluster.clone()).or_insert(0.0) += notional;
    exposure.total += notional;
    exposure.open_positions += 1;
}

/// SUBTRACT the EXACT reservation `reserve_exposure` made for ONE position (`cost_per * size`, saturating at
/// 0), decrement `open_positions` by 1. This is the SINGLE inverse of `reserve_exposure` — design §1.3 / R1:
/// it UNIFIES the old `release_exposure` (an entry that didn't fully fill) and `decrement_exposure` (a tracked
/// position closing on unwind) so the two paths CANNOT drift. Both pass the SAME (pos, cost_per) the
/// reservation used, so the subtraction is exact whether the slug has one stacked position or several — it
/// removes only THIS position's contribution, never the whole per-pair bucket. The per-pair bucket reaches
/// ~0 only when the LAST position on the slug is subtracted (the caller drops the slug key then).
fn subtract_exposure(exposure: &mut Exposure, pos: &Position, cost_per: f64) {
    let notional = cost_per * pos.size as f64;
    if let Some(p) = exposure.per_pair.get_mut(&pos.market) {
        *p = (*p - notional).max(0.0);
    }
    if let Some(c) = exposure.per_cluster.get_mut(&pos.cluster) {
        *c = (*c - notional).max(0.0);
    }
    exposure.total = (exposure.total - notional).max(0.0);
    exposure.open_positions = exposure.open_positions.saturating_sub(1);
}

/// APPEND a freshly-filled position as a new `HeldLeg` on its slug (design §1). The exposure was already
/// RESERVED at spawn (`reserve_exposure`), so this does NOT touch exposure — it only records the leg +
/// stores its `cost_per`/`entry_net`/`entry_dir` (so a later per-leg unwind subtracts EXACTLY this leg — R1 —
/// and a later add gates on `max(entry_net)+tau` / same-direction). Metadata is set on the FIRST insert
/// (Vacant) only; an APPEND (Occupied) pushes the leg and leaves the slug-level metadata AND `prev` UNTOUCHED
/// — so the C6 "preserve the poll's accumulated `prev`" concern is now STRUCTURAL (prev is a slug property,
/// never re-derived per leg). For a SPORTS pair the poll needs league/date/abbrevs.
fn track_position(
    positions: &std::sync::Arc<std::sync::Mutex<std::collections::HashMap<String, postpone::SlugPositions>>>,
    pair: &LivePair,
    pos: Position,
    cost_per: f64,
    entry_net: f64,
    entry_dir: Dir,
) {
    let leg = postpone::HeldLeg { pos, cost_per, entry_net, entry_dir };
    let slug = leg.pos.market.clone();
    use std::collections::hash_map::Entry;
    match lock(positions).entry(slug) {
        // APPEND: prev + metadata are slug-level (one game) and shared by every add -> never touched here.
        Entry::Occupied(mut e) => {
            e.get_mut().legs.push(leg);
        }
        Entry::Vacant(e) => {
            // poll metadata is for the MLB postponement poll ONLY. A WORLD-CUP pair is `Cat::Sports` but has
            // no statsapi source (`soccer`), so it enrolls with EMPTY metadata (like weather/econ) -> the poll
            // skips it. Its tiny void/postpone tail is the noted follow-on, not handled by the MLB poll.
            let (league, date, team_a, team_b) = if pair.cat == Cat::Sports && !pair.soccer {
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
            e.insert(postpone::SlugPositions { league, date, team_a, team_b, prev: None, legs: vec![leg] });
        }
    }
}

/// HELD-SLUG ADD GATE (design §2/§3) — decide whether an approvable same-frame edge on an ALREADY-HELD slug
/// qualifies as an add, and if so which kind. Returns `Some("scale-in"|"re-entry")` to ALLOW (the caller then
/// runs `evaluate`, whose notional/count caps bound it) or `None` to BLOCK (== the old one-position guard's
/// `continue`). Gate — ALL must hold: (1) SAME-direction as every held leg (an opposite-direction bigger arb
/// is NOT an add — R6/Test 4); (2) `edge.net >= max(held entry_net) + add_tau_gain` (parity with the backtest
/// `add_events` trigger); (3) `held.len() < max_positions_per_slug` (the explicit count cap — cap=1 blocks the
/// first add); (4) the matching feature FLAG is ON (`was_live` => SCALE-IN needs `enable_scale_in`; else
/// RE-ENTRY needs `enable_reentry`).
///
/// SAFE BY DEFAULT: with both flags false AND cap=1, clause (3) (cap) and clause (4) (flag) BOTH fail, so this
/// ALWAYS returns `None` for a held slug — byte-identical to the one-position-per-slug bot.
fn qualifying_add(cfg: &Config, held: &[postpone::HeldLeg], edge: &Edge, was_live: bool) -> Option<&'static str> {
    if held.is_empty() {
        return None; // not held -> not an add (caller handles the fresh-entry path)
    }
    // 1. same direction as ALL held legs (the gate forbids opening an opposite-direction position on a slug).
    if !held.iter().all(|l| l.entry_dir == edge.dir) {
        return None;
    }
    // 2. strictly bigger than the best held entry by the tau-gain margin.
    let base_net = held.iter().map(|l| l.entry_net).fold(f64::NEG_INFINITY, f64::max);
    if edge.net < base_net + cfg.add_tau_gain {
        return None;
    }
    // 3. count cap (cap=1 => any held slug already at the cap => block).
    if (held.len() as u32) >= cfg.max_positions_per_slug {
        return None;
    }
    // 4. classify via the cross-frame proxy + require the matching flag.
    if was_live {
        cfg.enable_scale_in.then_some("scale-in")
    } else {
        cfg.enable_reentry.then_some("re-entry")
    }
}

/// Apply a SPAWNED submission's outcome on the EVENT LOOP's turn (so all position/exposure mutation is
/// single-threaded). Entry: both-filled => record the position (keep the reservation); else => release the
/// reservation, then on a real one-leg-filled NAKED outcome attempt AUTO-RECOVERY (cancel the resting leg +
/// flatten the filled leg) before the halt backstop (FIX A). Unwind: both-filled => remove the position +
/// decrement exposure; else => the flatten itself left a naked leg -> fail-close (W14). Always clears the
/// slug's in-flight marker.
#[allow(clippy::too_many_arguments)]
fn apply_outcome(
    backend: &std::sync::Arc<dyn ExecutionBackend>,
    positions: &std::sync::Arc<std::sync::Mutex<std::collections::HashMap<String, postpone::SlugPositions>>>,
    kalshi_books: &std::sync::Arc<std::sync::Mutex<std::collections::HashMap<String, book::KalshiBook>>>,
    pmus_books: &std::collections::HashMap<String, book::PmusBook>,
    exposure: &mut Exposure,
    pending_entries: &mut std::collections::HashSet<String>,
    flattening: &mut std::collections::HashMap<String, FlatKind>,
    outcome_tx: &tokio::sync::mpsc::UnboundedSender<SubmitOutcome>,
    halt: &std::sync::atomic::AtomicBool,
    out: SubmitOutcome,
) {
    let both = out.ack.both_filled();
    match out.kind {
        SubmitKind::Entry => {
            pending_entries.remove(&out.slug);
            if both {
                if let (Some(mut pos), Some(pair)) = (out.position, out.pair) {
                    // persist each leg's exchange order id from its fill ack (positional: ack.a <-> legs[0],
                    // ack.b <-> legs[1]) so a later cancel/unwind can reach the right venue endpoint (FIX 3).
                    for (leg, ack) in pos.legs.iter_mut().zip([&out.ack.a, &out.ack.b]) {
                        if let Ok(a) = ack {
                            leg.venue_order_id = a.venue_order_id.clone();
                        }
                    }
                    // APPEND the new leg (exposure stays RESERVED — keep it). cost_per/entry_net/entry_dir are
                    // STORED on the HeldLeg so a later per-leg unwind subtracts EXACTLY this leg (R1) and a
                    // later add gates on max(entry_net)+tau / same-direction.
                    track_position(positions, &pair, pos, out.cost_per, out.entry_net, out.entry_dir);
                }
            } else {
                // subtract the EXACT reservation made at spawn (the entry did not fully fill) — the unified
                // inverse of reserve_exposure (R1), never the whole-bucket remove.
                if let Some(pos) = &out.position {
                    subtract_exposure(exposure, pos, out.cost_per);
                }
                // FIX A: a real one-leg-filled outcome is a NAKED directional leg. Try to AUTO-RECOVER
                // (cancel the resting leg + flatten the filled leg at a marketable book price). The halt
                // is the BACKSTOP only — used when there's no real naked leg (nothing to do) OR recovery
                // can't be priced/fired. Never records a hedge; the reservation is already released.
                if !recover_naked_leg(backend, kalshi_books, pmus_books, flattening, outcome_tx, &out.slug, &out.ack, out.position.as_ref()) {
                    naked_leg_failclose(&out.slug, SubmitKind::Entry, &out.ack, halt);
                }
            }
        }
        SubmitKind::Unwind => {
            flattening.remove(&out.slug); // a non-flat outcome lets the poll re-emit to retry
            if both {
                // POP the FRONT leg that was just flattened (spawn_unwind always targets legs[0], and
                // `flattening` serialized this slug so the Vec is unchanged since the spawn — the front is
                // exactly that leg). Subtract its OWN stored cost_per (the exact inverse, R1) — NEVER the
                // whole bucket. Drop the slug key only when its `legs` empties (R5 — else prune/W16 can't
                // fire). A still-non-empty slug keeps the poll re-emitting to flatten the next leg (§1.5).
                let mut drop_slug = false;
                if let Some(sp) = lock(positions).get_mut(&out.slug) {
                    if !sp.legs.is_empty() {
                        let removed = sp.legs.remove(0);
                        subtract_exposure(exposure, &removed.pos, removed.cost_per);
                    }
                    drop_slug = sp.legs.is_empty();
                }
                if drop_slug {
                    lock(positions).remove(&out.slug);
                    println!("[UNWIND] flattened {} (slug fully closed)", out.slug);
                } else {
                    println!("[UNWIND] flattened one leg of {} ({} leg(s) remain; poll re-emits)", out.slug, lock(positions).get(&out.slug).map(|sp| sp.legs.len()).unwrap_or(0));
                }
            } else {
                // a postpone unwind that HALF-filled (one leg sold, the other unfilled) is a NEW naked leg ->
                // halt; a both-failed unwind sold nothing (the pair is still hedged) so the poll re-emits.
                println!("[UNWIND] WARN {} did not fully flatten (one leg unfilled); poll re-emits", out.slug);
                naked_leg_failclose(&out.slug, SubmitKind::Unwind, &out.ack, halt);
            }
        }
        SubmitKind::Recovery => {
            // a naked-leg RECOVERY flatten (single SELL of the already-filled leg, fired by FIX A). The
            // outcome's leg `a` is that SELL; leg `b` is unused here.
            flattening.remove(&out.slug);
            if matches!(&out.ack.a, Ok(a) if a.filled) {
                println!("[RECOVER] flattened the naked leg on {} (filled)", out.slug);
            } else {
                // the SELL did NOT fill -> the originally-filled leg is STILL naked. This is the case the
                // simulated-sentinel approach would have slipped past `naked_filled_idx`; halt EXPLICITLY so
                // the unhedged directional leg surfaces for a manual flatten. (Dry-run SELLs fill -> the Ok
                // branch above, so this never trips in dry-run.)
                halt.store(true, std::sync::atomic::Ordering::Relaxed);
                eprintln!(
                    "[live] CRITICAL RECOVERY flatten of the naked leg on {} did NOT fill ({:?}) \
                     -> KILL-SWITCH engaged. The filled leg is STILL a directional position — flatten MANUALLY.",
                    out.slug, out.ack.a
                );
            }
        }
    }
}

/// Which leg of a non-both-filled pair is the NAKED one: returns `Some(0)` if leg `a` filled LIVE (not a
/// simulated dry-run ack) while `b` did not fill, `Some(1)` for the mirror, else `None` (no real naked leg
/// — both errored, both simulated, or both filled). "Did not fill" covers an `Err` AND an `Ok`-but-resting
/// (accepted, not filled) leg — a resting leg leaves the other filled leg just as naked as an errored one.
fn naked_filled_idx(ack: &exec::PairAck) -> Option<usize> {
    let live_filled = |r: &Result<exec::Ack, exec::ExecError>| matches!(r, Ok(a) if a.filled && !a.simulated);
    let not_filled = |r: &Result<exec::Ack, exec::ExecError>| !matches!(r, Ok(a) if a.filled);
    if live_filled(&ack.a) && not_filled(&ack.b) {
        Some(0)
    } else if live_filled(&ack.b) && not_filled(&ack.a) {
        Some(1)
    } else {
        None
    }
}

/// FIX A — NAKED-LEG AUTO-RECOVERY. On a one-leg-filled entry outcome: (1) if the UNFILLED leg is an
/// `Ok`-but-resting order with a venue order id, CANCEL it (spawned, best-effort); (2) FLATTEN the FILLED
/// leg with a single marketable SELL (best YES bid for a YES leg, `1 - YES ask` for a NO leg) read from
/// that leg's LIVE book, fired through the single-leg `submit` primitive on a spawned task; the SELL's
/// outcome routes back as a `SubmitKind::Recovery` so a SELL that ITSELF fails to fill re-trips the halt.
/// Does NOT record a hedge; the reservation was already released by the caller. Returns TRUE iff recovery
/// was LAUNCHED; FALSE (caller halts as the backstop) when there is no real naked leg OR the filled leg
/// cannot be priced (one-sided book) / its (venue, market, side) is unavailable. FAIL SAFE: any uncertainty
/// about the flatten price/route returns FALSE -> halt, never an un-flattened silent naked leg.
#[allow(clippy::too_many_arguments)]
fn recover_naked_leg(
    backend: &std::sync::Arc<dyn ExecutionBackend>,
    kalshi_books: &std::sync::Arc<std::sync::Mutex<std::collections::HashMap<String, book::KalshiBook>>>,
    pmus_books: &std::collections::HashMap<String, book::PmusBook>,
    flattening: &mut std::collections::HashMap<String, FlatKind>,
    outcome_tx: &tokio::sync::mpsc::UnboundedSender<SubmitOutcome>,
    slug: &str,
    ack: &exec::PairAck,
    position: Option<&Position>,
) -> bool {
    let Some(filled_idx) = naked_filled_idx(ack) else {
        return false; // no real (live) naked leg -> nothing to recover; caller's halt is a no-op anyway
    };
    // the held legs (with venue/market/side) we built the entry from — positional: legs[i] <-> ack {a,b}.
    let Some(pos) = position else { return false }; // no leg metadata -> can't price/route -> halt backstop
    let resting_idx = 1 - filled_idx;
    let filled_leg = &pos.legs[filled_idx];
    let resting_ack = if resting_idx == 0 { &ack.a } else { &ack.b };

    // W-1: the slug's flatten slot is occupied — by WHAT decides whether this naked leg is covered.
    //   * Recovery: a prior recovery for THIS slug's naked leg is already in flight -> genuinely covered ->
    //     return true (don't double-fire; the in-flight recovery flattens it).
    //   * Unwind: a postpone-unwind holds the slot, but it targets only the FRONT held leg (`legs[0]`), NOT
    //     this freshly-naked add leg. Treating it as "covered" would ABANDON the add leg silently (the W-1
    //     bug). FAIL CLOSED: return false so the caller engages the halt and the unhedged leg surfaces for a
    //     manual flatten. (We can't safely fire a second flatten here either — `flattening` is per-slug and a
    //     second SELL would race the unwind's pop of legs[0]; halt is the correct fail-safe.)
    match flattening.get(slug) {
        Some(FlatKind::Recovery) => return true, // this naked leg's recovery is already underway
        Some(FlatKind::Unwind) => return false,  // unwind covers a DIFFERENT leg -> NOT covered -> caller halts
        None => {}                               // slot free -> fire the recovery below
    }

    // PRICE the filled leg's marketable SELL from its LIVE book. Unpriceable (one-sided book / no book) ->
    // FALSE so the caller halts: we must NOT leave the filled leg silently naked. The flatten is a SELL, so
    // it FLOOR-quantizes to the leg's pmus tick (W2) — `Position` now carries `pm_min_tick` on the pmus leg —
    // before the cent floor, so a coarse-tick pmus market doesn't reject the recovery SELL.
    let book = match filled_leg.venue {
        Venue::Kalshi => lock(kalshi_books).get(&filled_leg.market).map(|b| b.touch()),
        Venue::Pmus => pmus_books.get(&filled_leg.market).map(|b| b.touch()),
    };
    let Some(exit) = book.and_then(|b| flatten_exit_cents(filled_leg, &b)) else {
        eprintln!("[live] CRITICAL NAKED LEG on {slug}: filled {:?} leg can't be priced for a flatten (one-sided book) -> halting", filled_leg.venue);
        return false;
    };

    // (1) CANCEL the resting leg if it is an accepted-but-resting order with a venue order id (an Err leg
    //     created no order to cancel). Best-effort + spawned: a cancel failure still leaves the FLATTEN as
    //     the real risk reducer, and a GTC resting order that never fills is harmless once we're flat.
    if let Ok(a) = resting_ack {
        if !a.filled && !a.venue_order_id.is_empty() {
            let target = exec::CancelTarget {
                venue: pos.legs[resting_idx].venue,
                venue_order_id: a.venue_order_id.clone(),
                market: pos.legs[resting_idx].market.clone(),
            };
            spawn_cancel(backend, slug, target);
        }
    }

    // (2) FLATTEN the filled leg: one marketable SELL of the EXACT (venue, market, side) held, fired on a
    //     spawned task via the single-leg `submit`. Mark the slug `flattening` so a burst can't double-fire.
    let sell = OrderIntent {
        venue: filled_leg.venue,
        market: filled_leg.market.clone(),
        action: Action::Sell,
        side: filled_leg.side,
        price_cents: exit,
        qty: pos.size,
        client_order_id: format!("recover-{slug}-{filled_idx}"),
    };
    eprintln!(
        "[live] CRITICAL NAKED LEG on {slug}: one live leg filled, the other did not -> AUTO-RECOVERING \
         (cancel resting leg + SELL {:?} {:?} {}x @ {}c to flatten). No hedge recorded.",
        sell.venue, sell.side, sell.qty, sell.price_cents
    );
    flattening.insert(slug.to_string(), FlatKind::Recovery);
    spawn_flatten(backend, outcome_tx, slug.to_string(), sell);
    true
}

/// SPAWN a single-leg recovery SELL (the flatten) off the cloned `Arc<backend>`, reporting it back as a
/// `SubmitKind::Recovery` outcome whose leg `a` is that SELL (leg `b` is an explicit UNUSED `Err` placeholder
/// — never a "filled" sentinel, so it can't be misread as a fill). The Recovery outcome arm halts iff the
/// SELL did NOT fill (the originally-filled leg is then still naked). catch_unwind guarantees an outcome so
/// the slug never stays stuck in `flattening`.
fn spawn_flatten(
    backend: &std::sync::Arc<dyn ExecutionBackend>,
    outcome_tx: &tokio::sync::mpsc::UnboundedSender<SubmitOutcome>,
    slug: String,
    sell: OrderIntent,
) {
    let backend = backend.clone();
    let outcome_tx = outcome_tx.clone();
    tokio::spawn(async move {
        let a = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| backend.submit(&sell)))
            .unwrap_or_else(|_| {
                eprintln!("[live] CRITICAL recovery flatten panicked for {slug} — reporting a failed SELL");
                Err(exec::ExecError::Rejected("flatten panicked".into()))
            });
        let ack = exec::PairAck {
            a,
            // leg b is UNUSED for a recovery (only leg a — the SELL — is read). An Err placeholder, never a
            // filled sentinel, so no path can mistake it for a fill.
            b: Err(exec::ExecError::Rejected("recovery has no second leg".into())),
        };
        let _ = outcome_tx.send(SubmitOutcome { slug, kind: SubmitKind::Recovery, ack, position: None, pair: None, cost_per: 0.0, entry_net: 0.0, entry_dir: Dir::PK });
    });
}

/// SPAWN a best-effort cancel of a resting leg off the cloned `Arc<backend>` (the recovery's cancel step).
/// Fire-and-log: the FLATTEN is the real risk reducer, so a cancel failure is logged, not fatal (a GTC
/// resting order that never fills is harmless once the filled leg is flat).
fn spawn_cancel(backend: &std::sync::Arc<dyn ExecutionBackend>, slug: &str, target: exec::CancelTarget) {
    let backend = backend.clone();
    let slug = slug.to_string();
    tokio::spawn(async move {
        let r = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| backend.cancel(&target)))
            .unwrap_or_else(|_| Err(exec::ExecError::Rejected("cancel panicked".into())));
        if let Err(e) = r {
            eprintln!("[live] WARN recovery cancel of the resting leg failed for {slug} ({e:?}) — the flatten still reduces the risk; a resting GTC order is harmless once flat");
        }
    });
}

/// W14 FAIL-CLOSE BACKSTOP: a non-both-filled outcome where exactly one leg actually FILLED LIVE while the
/// OTHER did not is a naked directional position. This is the BACKSTOP for when auto-recovery (FIX A) could
/// NOT be launched (no priceable book / no leg metadata) — log CRITICAL and ENGAGE the runtime halt (blocks
/// all new entries), keeping the filled leg visible for a MANUAL flatten. Dry-run acks are simulated +
/// filled -> `both_filled` is always true there, so this never trips in dry-run.
fn naked_leg_failclose(slug: &str, kind: SubmitKind, ack: &exec::PairAck, halt: &std::sync::atomic::AtomicBool) {
    if let Some(filled_idx) = naked_filled_idx(ack) {
        let filled = if filled_idx == 0 { &ack.a } else { &ack.b };
        halt.store(true, std::sync::atomic::Ordering::Relaxed);
        eprintln!(
            "[live] CRITICAL NAKED LEG on {kind:?} {slug}: one live leg filled, the other did not ({filled:?}) \
             -> KILL-SWITCH engaged (no new entries). Filled leg is a directional position — flatten MANUALLY."
        );
    }
}

/// Prepare + SPAWN a postponement unwind for ONE held leg on the slug (design §1.5 — v1 flattens
/// one-at-a-time, FRONT leg first; the poll's per-cycle re-emit picks up the next leg over later cycles). A
/// postponement voids the whole GAME, so EVERY leg on the slug must eventually flatten. This fires the FRONT
/// `HeldLeg`'s two SELLs (priced from the live books); the Unwind outcome arm pops that front leg + subtracts
/// its EXACT `cost_per` (never the whole bucket). Dedupes against an in-flight flatten (`flattening`). A
/// one-sided book (a leg can't be priced) logs a WARN and clears nothing so the next re-emit retries.
/// REDUCE-ONLY: fires even under the kill-switch (flattening a void REDUCES risk); dry-run only LOGS.
#[allow(clippy::too_many_arguments)]
fn spawn_unwind(
    cfg: &Config,
    backend: &std::sync::Arc<dyn ExecutionBackend>,
    positions: &std::sync::Arc<std::sync::Mutex<std::collections::HashMap<String, postpone::SlugPositions>>>,
    kalshi_books: &std::sync::Arc<std::sync::Mutex<std::collections::HashMap<String, book::KalshiBook>>>,
    pmus_books: &std::collections::HashMap<String, book::PmusBook>,
    flattening: &mut std::collections::HashMap<String, FlatKind>,
    outcome_tx: &tokio::sync::mpsc::UnboundedSender<SubmitOutcome>,
    slug: &str,
) {
    if flattening.contains_key(slug) {
        return; // a flatten (unwind OR recovery) for this slug is already in flight -> don't double-fire (C5)
    }
    // the FRONT held leg on this slug (one-at-a-time, §1.5). `None`/empty -> already flattened / gone.
    let front = lock(positions).get(slug).and_then(|sp| sp.legs.first().cloned());
    let Some(front) = front else { return };
    if cfg.kill_switch {
        println!("[UNWIND] kill-switch engaged but flattening (reduce-only) {slug}");
    }
    // price each leg's exit from the venue book it sits on (a SELL never blocks on a fresh entry edge).
    let exits = unwind_exit_cents(&front.pos, |leg| match leg.venue {
        Venue::Kalshi => lock(kalshi_books).get(&leg.market).map(|b| b.touch()),
        Venue::Pmus => pmus_books.get(&leg.market).map(|b| b.touch()),
    });
    let Some(exits) = exits else {
        println!("[UNWIND] WARN one-sided book — cannot price both legs of {slug}; holding (poll re-emits)");
        return; // not marked flattening -> the poll's re-emit retries once a book is two-sided
    };
    let orders = unwind::unwind_orders(&front.pos, exits);
    flattening.insert(slug.to_string(), FlatKind::Unwind);
    // carry the front leg's pos + cost_per so the outcome arm subtracts EXACTLY this leg (defensive; the arm
    // pops the front and uses the popped leg's OWN stored cost_per — the exact-release guarantee).
    spawn_submit(backend, outcome_tx, SubmitKind::Unwind, slug.to_string(), orders, Some(front.pos), None, front.cost_per, 0.0, Dir::PK);
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
    if !d.soccer_leagues_unmapped.is_empty() {
        println!("[coverage] UNMAPPED soccer (drawable-outcome) leagues: {:?}", d.soccer_leagues_unmapped);
    }
    for (slug, why) in &d.soccer_unbound {
        // LOUD, never silent: a WC game whose codes mismatch AND whose names don't agree -> would be missed.
        println!("[wc-unbound] no Kalshi bind (code+name both failed): {slug}  ({why})");
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
    positions: std::sync::Arc<std::sync::Mutex<std::collections::HashMap<String, postpone::SlugPositions>>>,
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

        // C9: a TRUNCATED catalog (pmus paging hit the cap) means an absent slug may just be off the cut
        // page, not settled — treat it like a degraded pull FOR PRUNING ONLY: still apply the adds, but skip
        // the prune step this cycle so a live market isn't torn down because the catalog was incomplete.
        let prune_ok = !fresh.truncated;
        if !prune_ok {
            println!("[refresh] pmus catalog TRUNCATED — no prune this cycle");
        }

        // fresh keys by venue. A sports pair contributes BOTH Kalshi tickers (team A + B).
        let fresh_slugs: HashSet<String> = fresh.pairs.iter().map(|p| p.slug.clone()).collect();
        let fresh_tickers: HashSet<String> = fresh.pairs.iter().flat_map(pair_tickers).collect();

        // snapshot the PRE-refresh subscribed set (slugs + each pair's Kalshi ticker(s)) before mutating.
        let (pre_slugs, slug_to_tickers): (HashSet<String>, HashMap<String, Vec<String>>) = {
            let ps = lock(&pairs);
            (
                ps.by_slug.keys().cloned().collect(),
                ps.by_slug.iter().map(|(s, p)| (s.clone(), p.kalshi_tickers())).collect(),
            )
        };
        let pre_tickers: HashSet<String> = slug_to_tickers.values().flatten().cloned().collect();

        // PRUNE debounce (keyed on the pmus slug = the pair identity). W16: never prune a slug with any open
        // held leg — it must stay subscribed/flattenable until closed (else its Kalshi book is freed and the
        // unwind can never price the exit -> an un-flattenable held position). A slug is "held" iff it has a
        // map entry (R5: the key is dropped only when its last `HeldLeg` is flattened), so `keys()` is exactly
        // the held set. A genuinely-settled held slug still accrues misses in `absent` (the debounce runs), so
        // once its last leg closes the next cycle prunes it immediately. C9: on a TRUNCATED catalog, skip the
        // debounce entirely (an off-page slug must not count as a miss) — still apply the adds below.
        let held: HashSet<String> = lock(&positions).keys().cloned().collect();
        let prune_slugs: HashSet<String> = if prune_ok {
            prune_step(&pre_slugs, &fresh_slugs, &mut absent, 2)
                .into_iter()
                .filter(|s| !held.contains(s))
                .collect()
        } else {
            HashSet::new()
        };
        let prune_tickers: HashSet<String> = prune_slugs.iter().filter_map(|s| slug_to_tickers.get(s)).flatten().cloned().collect();

        // the in-place WIRE updates: add = fresh keys not already subscribed; del = the pruned keys. Pure
        // set-diff (the stream tolerates a re-add as a harmless no-gap merge, but we send the minimal set).
        let k_update = diff_targets(&pre_tickers, &fresh_tickers, &prune_tickers);
        let pm_update = diff_targets(&pre_slugs, &fresh_slugs, &prune_slugs);

        // apply to the shared pair map + tracked sets (streams re-subscribe `tracked` on reconnect, so
        // mutate it before dispatching so a reconnect-during-refresh stays consistent).
        let mut added = 0usize;
        {
            let mut ps = lock(&pairs);
            let mut kt = lock(&k_tracked);
            let mut pt = lock(&pm_tracked);
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
            let mut kb = lock(&kalshi_books);
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
/// from the REMAINING total-notional room (W4: subtract already-open `exposure.total`, matching the
/// docstring "the bankroll can fund" — the raw cap ignored open positions and double-counted once any
/// position was open; only the gate's `tot_cap` happened to subtract it). The gate still applies all the
/// finer caps; `cost_per` mirrors the gate's reconstruction (`(1-edge.net).max(0.1)`).
fn affordable(cfg: &Config, edge: &Edge, exposure: &Exposure) -> u32 {
    let cost_per = (1.0 - edge.net).max(0.1);
    let room = (cfg.max_total_notional - exposure.total).max(0.0);
    (room / cost_per).floor().max(0.0) as u32
}

/// W6: recompute the REALIZED net edge from the two ROUNDED leg prices and confirm it still clears the
/// edge floor (and the pair costs < 100c). `build_legs` rounds each leg's price to a whole cent
/// independently, so the paid cost (`a_cents + b_cents`) can drift up to +1c above the cost implied by the
/// gated `edge.net`; without this the booked edge can be materially thinner than the gated edge. The fee
/// model mirrors `signal`/`game_signal` exactly: each leg pays its venue's at-scale MARGINAL taker fee at
/// that leg's PAID price (the same per-leg `venue_fee(leg_price)` those functions sum), so this is the
/// honest "size and price are consistent" check the gate splits across two functions.
fn realized_edge_clears_floor(cfg: &Config, legs: &[OrderIntent; 2]) -> bool {
    use crate::ledger::{marginal_taker_fee, KALSHI_TAKER_COEF, PMUS_TAKER_COEF};
    let leg_fee = |leg: &OrderIntent| {
        let p = leg.price_cents as f64 / 100.0;
        if !(0.0 < p && p < 1.0) {
            return 0.0;
        }
        let coef = match leg.venue {
            Venue::Kalshi => KALSHI_TAKER_COEF,
            Venue::Pmus => PMUS_TAKER_COEF,
        };
        marginal_taker_fee(coef, p)
    };
    let cost = (legs[0].price_cents as f64 + legs[1].price_cents as f64) / 100.0;
    if cost >= 1.0 {
        return false; // a >= $1 pair pays more than the $1 payout — never book it
    }
    let realized_net = round4((1.0 - cost) - leg_fee(&legs[0]) - leg_fee(&legs[1]));
    realized_net * 100.0 >= cfg.edge_floor_cents
}

/// 4dp round, matching `signal::round4` / ledger.py — keeps the realized-edge re-check bit-consistent with
/// the edge the gate approved.
fn round4(x: f64) -> f64 {
    (x * 1.0e4).round() / 1.0e4
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
///
/// FIX C — per-market pmus constraints: a pmus leg is (1) SKIPPED (whole pair -> None) if `size` is below
/// the market's `minimumTradeQty` (a sub-min order would REJECT, leaving the OTHER leg naked — fail safe),
/// and (2) QUANTIZED to the market's `orderPriceMinTickSize` if the whole-cent price isn't already a valid
/// multiple (a coarser-than-cent tick; finer ticks like 0.001 leave whole cents unchanged). Kalshi is
/// integer-cent + whole-share, so its legs are untouched.
fn build_legs(pair: &LivePair, q: &Quote, dir: Dir, size: u32) -> Option<[OrderIntent; 2]> {
    let planned = plan_legs(pair, q, dir)?;
    let mut out: Vec<OrderIntent> = Vec::with_capacity(2);
    for leg in planned {
        let price = if leg.venue == Venue::Pmus {
            // a pmus order below the market's minimumTradeQty would reject -> skip the WHOLE pair (a fired
            // single leg with the other rejected is the naked-leg case this gate prevents).
            if let Some(min_qty) = pair.pm_min_qty {
                if (size as f64) < min_qty {
                    return None;
                }
            }
            // quantize UP (entries are BUYs) to the market's price tick so it stays marketable (W1).
            quantize_to_tick(leg.price, pair.pm_min_tick, Action::Buy)
        } else {
            leg.price // Kalshi: integer-cent, no per-market tick
        };
        // entries are BUYs -> ceil to the cent (limit >= touch, still crosses); W1.
        let pc = cents(Some(price), Action::Buy)?;
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

/// Quantize a price (dollars) to a valid multiple of `tick` (dollars) TOWARD-MARKETABLE for `action`: a
/// BUY ceils (limit >= the touch, still lifts the offer), a SELL floors (limit <= the touch, still hits the
/// bid). `None`/non-positive/non-finite tick -> the price unchanged. Used for the pmus per-market
/// `orderPriceMinTickSize` (FIX C/W1): when the tick is coarser than a cent (e.g. 0.05) a whole-cent price
/// like 0.07 snaps UP to 0.10 for a BUY (DOWN to 0.05 for a SELL); a finer tick (0.001) leaves whole cents
/// unchanged. Nearest-rounding could move a marketable order to a RESTING limit (W1) — direction-aware
/// rounding keeps it crossing. Since `OrderIntent.price_cents` is whole cents, sub-cent ticks are then
/// honored only to cent granularity (W1 FLAG: a bounded <=0.5c precision cost per pmus leg, not a safety bug).
fn quantize_to_tick(price: f64, tick: Option<f64>, action: Action) -> f64 {
    match tick {
        // EPS snaps a value already within ~1e-6 ticks of a boundary ONTO it before the directional
        // ceil/floor, so float noise (e.g. 0.10/0.05 = 1.9999999998) can't push an exact multiple a whole
        // tick the wrong way (the L10 cent-boundary class). 1e-6 << half a tick, so it never crosses a real one.
        Some(t) if t.is_finite() && t > 0.0 => match action {
            Action::Buy => (price / t - TICK_EPS).ceil() * t,
            Action::Sell => (price / t + TICK_EPS).floor() * t,
        },
        _ => price,
    }
}

/// Tolerance (in tick/cent multiples) for snapping a near-boundary value onto the boundary before a
/// directional ceil/floor — guards the L10 float-noise-at-a-cent-boundary class without crossing a real tick.
const TICK_EPS: f64 = 1e-6;

/// Dollars (0..1) -> a valid integer-cent venue tick price in 1..=99c, rounded TOWARD-MARKETABLE for
/// `action`: a BUY ceils to the cent (limit >= the touch so it still crosses), a SELL floors (limit <= the
/// touch). NEAREST-rounding a marketable BUY down (or a SELL up) yields a RESTING limit -> the leg rests ->
/// the sibling goes naked -> recovery/halt (W1). Kalshi touches are already whole cents so ceil/floor is a
/// no-op there; this matters for pmus sub-cent book prices. The BUY ceil is safe because
/// `realized_edge_clears_floor` re-checks the ceil'd cost against the edge floor before firing. `None` if
/// non-finite or the rounded cent is out of 1..=99.
fn cents(price: Option<f64>, action: Action) -> Option<u8> {
    let p = price?;
    if !p.is_finite() {
        return None;
    }
    // EPS snaps a price already within ~1e-6c of a cent onto it before the directional ceil/floor, so a
    // whole-cent touch like 0.07 (0.07*100 = 7.0000000000000001) doesn't ceil up to 8c (the L10 class).
    let cx = p * 100.0;
    let c = match action {
        Action::Buy => (cx - TICK_EPS).ceil(),
        Action::Sell => (cx + TICK_EPS).floor(),
    };
    if (1.0..=99.0).contains(&c) {
        Some(c as u8)
    } else {
        None
    }
}

/// Fire the hedged pair from an already-built pair of legs (the backend fires both concurrently). Thin
/// wrapper kept for the OFFLINE smoke path (synchronous, no spawn); the live loop SPAWNS `submit_pair` off
/// an `Arc<backend>` so the network RTT never blocks its `select!` (see `spawn_submit`).
fn fire_legs(backend: &dyn ExecutionBackend, legs: &[OrderIntent; 2]) {
    let _ = backend.submit_pair(&legs[0], &legs[1]);
}

/// The HELD position the live loop derives from a both-filled entry: build a `Position` straight from the
/// two entry `OrderIntent`s (each leg = its venue/market/side; the pair `market` = the pmus slug). The two
/// intents are the exact legs we now own, so the unwind SELLs back the same (venue, market, side). `pm_tick`
/// (the pair's pmus `orderPriceMinTickSize`) is recorded ONLY on the pmus leg (FIX W2) so a later recovery
/// SELL can floor-quantize its flatten price to a valid tick; Kalshi legs carry `None`.
fn position_from_intents(slug: &str, cat: Cat, cluster: &str, pm_tick: Option<f64>, legs: &[OrderIntent; 2]) -> Position {
    Position {
        market: slug.to_string(),
        cat,
        legs: std::array::from_fn(|i| PositionLeg {
            venue: legs[i].venue,
            market: legs[i].market.clone(),
            side: legs[i].side,
            venue_order_id: String::new(), // filled from the fill ack in `apply_outcome` before tracking
            pm_min_tick: if legs[i].venue == Venue::Pmus { pm_tick } else { None },
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

/// Price a held leg's RECOVERY-flatten SELL to a valid integer-cent tick (W1/W2): take the marketable exit
/// from `book`, FLOOR-quantize it to this leg's pmus `pm_min_tick` (a SELL floors — limit <= touch stays
/// marketable; W2), then FLOOR to the cent. `None` when the leg can't be priced (one-sided book) OR the
/// floor lands outside 1..=99c -> the caller halts rather than fire a rejecting/mispriced flatten. A
/// coarse-tick pmus market would otherwise reject a whole-cent SELL and bounce the recovery to the halt.
fn flatten_exit_cents(leg: &PositionLeg, book: &Book) -> Option<u8> {
    let p = quantize_to_tick(exit_price(leg, book)?, leg.pm_min_tick, Action::Sell);
    cents(Some(p), Action::Sell)
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
        // route through flatten_exit_cents so the postpone-unwind SELL gets the SAME pmus tick FLOOR (W2)
        // the recovery SELL has — a coarse-tick pmus market would otherwise reject a whole-cent unwind.
        out[i] = flatten_exit_cents(leg, &book)?;
    }
    Some(out)
}

/// Stage-1 smoke: prove the risk+exec spine behaves on real-shaped snapshots (weather/econ/sports).
fn smoke(cfg: &Config, backend: &dyn ExecutionBackend) {
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
    };
    println!("[smoke] world-cup outcome arb (BINARY: pmus YES + Kalshi NO on one outcome), 1 day pre-game:");
    report(&sc, &wc_pair, &wc, Edge { net: 0.03, dir: Dir::PK }, backend);

    // (5) postponement unwind, LIVE PATH on a SYNTHETIC schedule (no network): a held MLB pair (dir PK:
    //     YES@pmus + YES@Kalshi-B) + a `snap`'d "Postponed, makeup 5d out" schedule game -> the DETECTOR
    //     fires -> should_unwind true -> print the two SELL unwind orders. This is the real stage-2 trigger
    //     composition (detect_postponement -> should_unwind -> unwind_orders), exercised offline.
    let held = types::Position {
        market: "aec-mlb-lad-pit-2026-06-16".into(), cat: Cat::Sports,
        legs: [
            types::PositionLeg { venue: Venue::Pmus, market: "aec-mlb-lad-pit-2026-06-16".into(), side: Side::Yes, ..Default::default() },
            types::PositionLeg { venue: Venue::Kalshi, market: "KXMLBGAME-26JUN16-PIT".into(), side: Side::Yes, ..Default::default() },
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
            soccer: false,
            days_to_event: None,
            pm_min_tick: None,
            pm_min_qty: None,
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

    /// A WORLD-CUP outcome LivePair (kalshi_b=None, soccer=true). It builds a clean 1:1 cluster and a
    /// BINARY pair builder for the smoke/test.
    fn wc_pair() -> LivePair {
        LivePair {
            slug: "atc-fwc-ger-cuw-2026-06-14-ger".into(),
            kalshi: "KXWCGAME-26JUN14GERCUW-GER".into(),
            kalshi_b: None, // per-outcome BINARY
            cat: Cat::Sports,
            cluster: "fwc-ger-cuw-2026-06-14".into(),
            settle_clean: true,
            soccer: true,
            days_to_event: Some(1.0),
            pm_min_tick: None,
            pm_min_qty: None,
        }
    }

    /// ROUTING (the money-path self-review item a): a WORLD-CUP outcome pair MUST take the BINARY `signal`
    /// arm, NEVER `game_signal`. The live loop routes on `kalshi_b.is_some()` (Some -> game_signal; None ->
    /// signal), so a WC pair (kalshi_b=None) is structurally guaranteed the binary arm — assert that, AND
    /// prove it via the LEG SHAPE: `build_legs` on a WC pair produces the 1:1 binary shape (YES@pmus(slug) +
    /// NO@Kalshi(ticker) for PK; YES@Kalshi + NO@pmus for KP), which ONLY the `(None, dir)` arms of plan_legs
    /// emit. A `game_signal`/2-team pair would instead make a YES@Kalshi-B leg — the mis-hedge this prevents.
    #[test]
    fn world_cup_pair_routes_through_binary_signal_not_game_signal() {
        let pair = wc_pair();
        // the exact routing predicate the live loop uses (main::run_live): None -> binary `signal` arm.
        assert!(pair.kalshi_b.is_none(), "a WC pair has no kalshi_b -> the loop takes the binary signal arm");
        assert!(pair.soccer, "and it is flagged soccer (regulation-settlement basis)");
        // dir PK: pmus YES ask 0.42 (back 'Germany wins' cheap on pmus) + Kalshi NO = 1 - Kalshi YES bid 0.45.
        let q = Quote {
            market: pair.slug.clone(),
            cat: Cat::Sports,
            pm: Book { yes_bid: Some(0.40), yes_ask: Some(0.42), age_s: 0.0 },
            k: Book { yes_bid: Some(0.45), yes_ask: Some(0.46), age_s: 0.0 },
            k_b: None, // BINARY: there is NO away-team book — the structural proof WC isn't the 2-team model
            depth: Depth { c2: 50, c1: 50, c0: 50 },
            settle_clean: true,
            cluster: pair.cluster.clone(),
            led_by: None,
            days_to_event: Some(1.0),
        };
        // PK legs = YES@pmus(slug) @ 42c + NO@Kalshi(ticker) @ (1-0.45)=55c — the weather/econ 1:1 shape.
        let pk = build_legs(&pair, &q, Dir::PK, 1).unwrap();
        assert_eq!((pk[0].venue, pk[0].side, pk[0].price_cents), (Venue::Pmus, Side::Yes, 42));
        assert_eq!(pk[0].market, "atc-fwc-ger-cuw-2026-06-14-ger", "the pmus leg uses the outcome SLUG");
        assert_eq!((pk[1].venue, pk[1].side, pk[1].price_cents), (Venue::Kalshi, Side::No, 55));
        assert_eq!(pk[1].market, "KXWCGAME-26JUN14GERCUW-GER", "the Kalshi leg uses the outcome TICKER (binary), not a team-B ticker");
        // the per-outcome legs LOCK (self-review item b): YES on the cheap venue + NO on the dear venue, on
        // the SAME outcome (same slug/ticker pair) — so it pays $1 whichever way THIS outcome resolves.
        assert!(pk[0].side == Side::Yes && pk[1].side == Side::No, "YES@one venue + NO@other on the same outcome");
        // KP flips: YES@Kalshi(ticker) 46c + NO@pmus(slug) = 1 - 0.40 = 60c. Still the 1:1 binary shape.
        let kp = build_legs(&pair, &q, Dir::KP, 1).unwrap();
        assert_eq!((kp[0].venue, kp[0].side, kp[0].market.as_str()), (Venue::Kalshi, Side::Yes, "KXWCGAME-26JUN14GERCUW-GER"));
        assert_eq!((kp[1].venue, kp[1].side, kp[1].market.as_str()), (Venue::Pmus, Side::No, "atc-fwc-ger-cuw-2026-06-14-ger"));
    }

    /// `track_position` does NOT enroll a WORLD-CUP pair in the MLB postponement poll (no statsapi WC
    /// source): a WC `Cat::Sports` pair gets EMPTY poll metadata (like weather/econ), while a moneyline MLB
    /// pair still derives league/date/abbrevs. (The held position is still recorded for exposure/dedup.)
    #[test]
    fn track_position_skips_mlb_poll_for_world_cup() {
        use std::sync::{Arc, Mutex};
        let positions = Arc::new(Mutex::new(std::collections::HashMap::new()));
        let pair = wc_pair();
        let legs = [
            OrderIntent { venue: Venue::Pmus, market: pair.slug.clone(), action: Action::Buy, side: Side::Yes, price_cents: 42, qty: 1, client_order_id: "a".into() },
            OrderIntent { venue: Venue::Kalshi, market: pair.kalshi.clone(), action: Action::Buy, side: Side::No, price_cents: 55, qty: 1, client_order_id: "b".into() },
        ];
        let pos = position_from_intents(&pair.slug, pair.cat, &pair.cluster, pair.pm_min_tick, &legs);
        track_position(&positions, &pair, pos, 0.97, 0.03, Dir::PK);
        let sp = positions.lock().unwrap().get(&pair.slug).cloned().expect("WC position still recorded (exposure/dedup)");
        // EMPTY poll metadata -> the MLB poll's `league=="mlb"` filter skips it (no wrong unwind / no warning).
        assert_eq!((sp.league.as_str(), sp.date.as_str(), sp.team_a.as_str(), sp.team_b.as_str()), ("", "", "", ""),
            "a WC pair enrolls with EMPTY MLB-poll metadata (it has no statsapi source)");
        assert_eq!(sp.legs.len(), 1, "one leg recorded");
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
            soccer: false,
            days_to_event: Some(1.0),
            pm_min_tick: None,
            pm_min_qty: None,
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

    /// `cents` rounds TOWARD-MARKETABLE (W1): a BUY ceils to the cent (limit >= touch -> still crosses), a
    /// SELL floors (limit <= touch). It rejects prices that round outside 1..=99 or are non-finite/absent.
    /// The whole-cent Kalshi-touch cases (exact multiples) are unchanged by direction.
    #[test]
    fn cents_rounds_toward_marketable() {
        // W1 core: a BUY at a sub-cent book price ceils UP (0.074 -> 8c); a SELL floors DOWN (0.076 -> 7c)
        // so each stays marketable. Nearest-rounding would have rested the BUY at 7c / the SELL at 8c.
        assert_eq!(cents(Some(0.074), Action::Buy), Some(8)); // BUY ceils 7.4c -> 8c (still lifts the offer)
        assert_eq!(cents(Some(0.076), Action::Sell), Some(7)); // SELL floors 7.6c -> 7c (still hits the bid)
        // whole-cent (Kalshi) touches are exact multiples -> direction is a no-op.
        assert_eq!(cents(Some(0.07), Action::Buy), Some(7));
        assert_eq!(cents(Some(0.07), Action::Sell), Some(7));
        assert_eq!(cents(Some(0.90), Action::Buy), Some(90));
        // a BUY at <0.5c still ceils to the 1c floor tick; a SELL at <1c floors to 0c -> rejected.
        assert_eq!(cents(Some(0.004), Action::Buy), Some(1)); // BUY ceils up to the 1c floor tick
        assert_eq!(cents(Some(0.004), Action::Sell), None); // SELL floors to 0c -> below the valid range
        assert_eq!(cents(Some(0.0), Action::Buy), None); // free -> not a tradeable tick
        assert_eq!(cents(Some(0.995), Action::Buy), None); // BUY ceils 99.5c -> 100c -> out of range
        assert_eq!(cents(Some(0.995), Action::Sell), Some(99)); // SELL floors 99.5c -> 99c -> valid
        assert_eq!(cents(Some(1.0), Action::Buy), None); // 100c -> out of range
        assert_eq!(cents(None, Action::Buy), None);
        assert_eq!(cents(Some(f64::NAN), Action::Buy), None);
    }

    /// FIX C/W1 — per-market pmus tick + min-size in the leg builder. (1) a configured size BELOW the pmus
    /// `minimumTradeQty` skips the WHOLE pair (sub-min would reject -> naked leg). (2) a coarse pmus price
    /// tick quantizes the pmus leg's BUY price UP to a valid multiple (W1 toward-marketable); a fine tick
    /// (0.001) is a no-op. Kalshi legs are never quantized. `quantize_to_tick` direction is checked directly.
    #[test]
    fn pmus_min_qty_skips_and_tick_quantizes_the_pmus_leg() {
        // quantize_to_tick toward-marketable: a BUY at 0.07 on a 0.05 tick ceils UP to 0.10; a SELL floors to
        // 0.05. A finer tick (0.001) and None/0 ticks leave the price unchanged either direction.
        assert!((quantize_to_tick(0.07, Some(0.05), Action::Buy) - 0.10).abs() < 1e-9); // BUY ceils up
        assert!((quantize_to_tick(0.07, Some(0.05), Action::Sell) - 0.05).abs() < 1e-9); // SELL floors down
        assert!((quantize_to_tick(0.10, Some(0.05), Action::Buy) - 0.10).abs() < 1e-9); // exact multiple: no-op
        assert!((quantize_to_tick(0.07, Some(0.001), Action::Buy) - 0.07).abs() < 1e-9); // finer tick: whole cent unchanged
        assert!((quantize_to_tick(0.07, None, Action::Buy) - 0.07).abs() < 1e-9); // no tick known -> unchanged
        assert!((quantize_to_tick(0.07, Some(0.0), Action::Sell) - 0.07).abs() < 1e-9); // non-positive tick ignored

        // (1) min-qty skip: pmus minimumTradeQty = 2, configured size 1 -> the pmus leg is sub-min -> None.
        let mut pair = wx_pair();
        pair.pm_min_qty = Some(2.0);
        assert!(build_legs(&pair, &q_pk(), Dir::PK, 1).is_none(), "size below pmus minimumTradeQty -> skip the pair");
        assert!(build_legs(&pair, &q_pk(), Dir::PK, 2).is_some(), "size at the minimum is allowed");

        // (2) tick quantization: pmus tick 0.05; dir PK leg A = YES@pmus (a BUY) @ pm_ask 0.07 -> ceils UP to
        // 0.10 = 10c (stays marketable; nearest-rounding to 5c would have rested it below the 7c offer).
        let mut pair2 = wx_pair();
        pair2.pm_min_tick = Some(0.05);
        let pk = build_legs(&pair2, &q_pk(), Dir::PK, 1).unwrap();
        assert_eq!((pk[0].venue, pk[0].price_cents), (Venue::Pmus, 10), "pmus BUY leg ceils UP to the 0.05 tick (marketable)");
        // the Kalshi NO leg (1 - 0.10 = 0.90) is NOT quantized by the pmus tick -> stays 90c.
        assert_eq!((pk[1].venue, pk[1].price_cents), (Venue::Kalshi, 90), "Kalshi leg is integer-cent, untouched");
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
        let wx = discovery::Pair { slug: "tc-temp-x-2026-06-11-gte95f".into(), kalshi: "K".into(), kalshi_b: None, cat: Cat::Weather, cluster: "x".into(), settle_clean: true, soccer: false, days_to_event: Some(0.0), pm_min_tick: None, pm_min_qty: None };
        let ec = discovery::Pair { slug: "urc-x".into(), kalshi: "K2".into(), kalshi_b: None, cat: Cat::Econ, cluster: "u3-26JUN".into(), settle_clean: false, soccer: false, days_to_event: None, pm_min_tick: None, pm_min_qty: None };
        let sp = discovery::Pair { slug: "aec-mlb-lad-pit-2026-06-16".into(), kalshi: "K-LAD".into(), kalshi_b: Some("K-PIT".into()), cat: Cat::Sports, cluster: "mlb-2026-06-16".into(), settle_clean: false, soccer: false, days_to_event: Some(1.0), pm_min_tick: None, pm_min_qty: None };
        // a WORLD-CUP discovery Pair is binary (kalshi_b=None) + soccer=true + settle_clean=true; it carries
        // those through to LivePair unchanged so the loop routes it binary and trades it (regulation-clean).
        let wc = discovery::Pair { slug: "atc-fwc-ger-cuw-2026-06-14-ger".into(), kalshi: "KXWCGAME-26JUN14GERCUW-GER".into(), kalshi_b: None, cat: Cat::Sports, cluster: "fwc-ger-cuw-2026-06-14".into(), settle_clean: true, soccer: true, days_to_event: Some(1.0), pm_min_tick: None, pm_min_qty: None };
        assert!(LivePair::from(wx).settle_clean);
        assert!(!LivePair::from(ec).settle_clean);
        // the WC pair routes BINARY (kalshi_b=None) yet is flagged soccer + settle_clean.
        let wc_lp = LivePair::from(wc);
        assert!(wc_lp.kalshi_b.is_none() && wc_lp.soccer && wc_lp.settle_clean);
        assert_eq!(wc_lp.kalshi_tickers(), vec!["KXWCGAME-26JUN14GERCUW-GER".to_string()], "WC subscribes ONE Kalshi ticker per outcome");
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
        let pos = position_from_intents("aec-mlb-lad-pit-2026-06-16", Cat::Sports, "mlb-2026-06-16", None, &legs);
        assert_eq!(pos.market, "aec-mlb-lad-pit-2026-06-16"); // pair identity = the pmus slug
        assert_eq!(pos.size, 7);
        assert_eq!(pos.cluster, "mlb-2026-06-16");
        assert_eq!(pos.legs[0], PositionLeg { venue: Venue::Pmus, market: "aec-mlb-lad-pit-2026-06-16".into(), side: Side::Yes, ..Default::default() });
        assert_eq!(pos.legs[1], PositionLeg { venue: Venue::Kalshi, market: "KXMLBGAME-26JUN16-PIT".into(), side: Side::Yes, ..Default::default() });
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
        let yes_leg = PositionLeg { venue: Venue::Pmus, market: "s".into(), side: Side::Yes, ..Default::default() };
        let no_leg = PositionLeg { venue: Venue::Kalshi, market: "K".into(), side: Side::No, ..Default::default() };
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

    /// `track_position` then `subtract_exposure` (the unified inverse, R1) round-trips exposure to zero (caps
    /// bind on entry, re-open on flatten), and a SPORTS pair derives league/date/abbrevs for the poll while
    /// non-sports leaves them empty.
    #[test]
    fn reserve_track_and_decrement_exposure_round_trips() {
        use std::sync::{Arc, Mutex};
        let positions = Arc::new(Mutex::new(std::collections::HashMap::new()));
        let mut exp = Exposure::new();
        let pair = LivePair {
            slug: "aec-mlb-lad-pit-2026-06-16".into(),
            kalshi: "KXMLBGAME-26JUN16-LAD".into(),
            kalshi_b: Some("KXMLBGAME-26JUN16-PIT".into()),
            cat: Cat::Sports, cluster: "mlb-2026-06-16".into(), settle_clean: false, soccer: false, days_to_event: Some(1.0),
            pm_min_tick: None, pm_min_qty: None,
        };
        let legs = [
            OrderIntent { venue: Venue::Pmus, market: pair.slug.clone(), action: Action::Buy, side: Side::Yes, price_cents: 55, qty: 4, client_order_id: "a".into() },
            OrderIntent { venue: Venue::Kalshi, market: "KXMLBGAME-26JUN16-PIT".into(), action: Action::Buy, side: Side::Yes, price_cents: 42, qty: 4, client_order_id: "b".into() },
        ];
        let pos = position_from_intents(&pair.slug, pair.cat, &pair.cluster, pair.pm_min_tick, &legs);
        // RESERVE at spawn (exposure bumps) then APPEND the leg on both-filled (exposure stays reserved).
        reserve_exposure(&mut exp, &pos, 0.97);
        track_position(&positions, &pair, pos, 0.97, 0.03, Dir::PK);
        // exposure reserved by cost_per×size = 0.97×4 = 3.88 across pair/cluster/total; one open position.
        assert!((exp.total - 3.88).abs() < 1e-9);
        assert!((exp.per_pair["aec-mlb-lad-pit-2026-06-16"] - 3.88).abs() < 1e-9);
        assert!((exp.per_cluster["mlb-2026-06-16"] - 3.88).abs() < 1e-9);
        assert_eq!(exp.open_positions, 1);
        // the held slug carries the poll's match fields (league/date/abbrevs from the slug + tickers).
        let sp = positions.lock().unwrap().get(&pair.slug).cloned().unwrap();
        assert_eq!((sp.league.as_str(), sp.date.as_str(), sp.team_a.as_str(), sp.team_b.as_str()), ("mlb", "2026-06-16", "lad", "pit"));
        assert_eq!(sp.legs.len(), 1);
        // flatten the (only) leg with its STORED cost_per (the exact inverse, R1) -> exposure back to zero.
        // Post-R1, per_pair is value-subtracted to ~0 (NOT key-removed; the bucket key is dropped lazily with
        // the slug). The asserted SEMANTICS (round-trips to zero) are unchanged; the shape of the last check
        // is the per-pair VALUE, not key-absence.
        let leg0 = sp.legs[0].clone();
        subtract_exposure(&mut exp, &leg0.pos, leg0.cost_per);
        assert!(exp.total.abs() < 1e-9 && exp.open_positions == 0);
        assert!(exp.per_pair.get("aec-mlb-lad-pit-2026-06-16").copied().unwrap_or(0.0).abs() < 1e-9);
    }

    /// C6: re-tracking a slug that already has a HeldPosition PRESERVES the poll's accumulated `prev`
    /// (status history) instead of clobbering it to `None` — the officialDate-slide detection needs `prev`.
    #[test]
    fn track_position_preserves_prev_on_retrack() {
        use std::sync::{Arc, Mutex};
        let positions = Arc::new(Mutex::new(std::collections::HashMap::new()));
        let pair = LivePair {
            slug: "aec-mlb-lad-pit-2026-06-16".into(),
            kalshi: "KXMLBGAME-26JUN16-LAD".into(),
            kalshi_b: Some("KXMLBGAME-26JUN16-PIT".into()),
            cat: Cat::Sports, cluster: "mlb-2026-06-16".into(), settle_clean: false, soccer: false, days_to_event: Some(1.0),
            pm_min_tick: None, pm_min_qty: None,
        };
        let legs = [
            OrderIntent { venue: Venue::Pmus, market: pair.slug.clone(), action: Action::Buy, side: Side::Yes, price_cents: 55, qty: 4, client_order_id: "a".into() },
            OrderIntent { venue: Venue::Kalshi, market: "KXMLBGAME-26JUN16-PIT".into(), action: Action::Buy, side: Side::Yes, price_cents: 42, qty: 4, client_order_id: "b".into() },
        ];
        let pos = position_from_intents(&pair.slug, pair.cat, &pair.cluster, pair.pm_min_tick, &legs);
        track_position(&positions, &pair, pos.clone(), 0.97, 0.03, Dir::PK);
        // the poll has since accumulated a status snapshot on this slug.
        let snapshot = postpone::GameStatus { detailed_state: "Scheduled".into(), official_date: Some("2026-06-16".into()), ..Default::default() };
        positions.lock().unwrap().get_mut(&pair.slug).unwrap().prev = Some(snapshot.clone());
        // an ADD (scale-in/re-entry) APPENDS a second leg on the SAME slug — `prev` is slug-level so it must
        // survive (it was clobbered to None pre-C6; now the preservation is STRUCTURAL — append never touches
        // prev). The append also stacks a 2nd leg (the multi-position shape).
        track_position(&positions, &pair, pos, 0.96, 0.05, Dir::PK);
        let sp = positions.lock().unwrap().get(&pair.slug).cloned().unwrap();
        assert_eq!(sp.prev, Some(snapshot), "prev survives an append (slug-level, never re-derived per leg)");
        assert_eq!(sp.legs.len(), 2, "the add stacked a second leg");
    }

    /// W6: when rounding each leg to a whole cent pushes the realized net under the floor, the fire is
    /// SKIPPED. A pair whose two book legs round to 49c + 49c = 98c gross 2c, minus marginal fees, nets
    /// under the 2c floor -> rejected; a clearly-fat pair (cheap legs) clears it.
    #[test]
    fn realized_edge_recheck_skips_a_rounded_under_floor_pair() {
        let cfg = crate::config::Config::test_default(); // edge_floor_cents = 2.0
        let leg = |v: Venue, c: u8| OrderIntent { venue: v, market: "m".into(), action: Action::Buy, side: Side::Yes, price_cents: c, qty: 1, client_order_id: "x".into() };
        // 49 + 49 = 98c -> gross 2c, but marginal taker fees on both legs eat it below the 2c floor -> SKIP.
        assert!(!realized_edge_clears_floor(&cfg, &[leg(Venue::Pmus, 49), leg(Venue::Kalshi, 49)]));
        // 5 + 90 = 95c -> gross 5c, fees on the cheap+dear legs leave well over 2c -> FIRE.
        assert!(realized_edge_clears_floor(&cfg, &[leg(Venue::Pmus, 5), leg(Venue::Kalshi, 90)]));
        // a >= $1 pair (51 + 50 = 101c) can never be booked -> SKIP.
        assert!(!realized_edge_clears_floor(&cfg, &[leg(Venue::Pmus, 51), leg(Venue::Kalshi, 50)]));
    }

    /// W4: `affordable` subtracts already-open `exposure.total` from the total-notional cap (the bankroll
    /// truly fundable), not the raw cap. With half the cap deployed, affordable halves.
    #[test]
    fn affordable_subtracts_open_exposure() {
        let cfg = crate::config::Config::test_default(); // max_total_notional = 1000
        let e = Edge { net: 0.0, dir: Dir::PK }; // cost_per = (1-0).max(0.1) = 1.0 -> affordable == room
        let mut exp = Exposure::new();
        assert_eq!(affordable(&cfg, &e, &exp), 1000); // nothing open -> full room
        exp.total = 600.0;
        assert_eq!(affordable(&cfg, &e, &exp), 400); // 600 deployed -> 400 room (raw cap would say 1000)
        exp.total = 1200.0; // over-deployed -> clamps at 0, never negative
        assert_eq!(affordable(&cfg, &e, &exp), 0);
    }

    /// PairState registers BOTH sports tickers in `by_ticker` -> slug, and `remove` frees both (so a
    /// frame on either team's Kalshi book finds the pair, and a pruned pair leaves no dangling index).
    #[test]
    fn pairstate_indexes_and_frees_both_sports_tickers() {
        let mut ps = PairState::default();
        ps.insert(LivePair { slug: "aec-mlb-lad-pit-2026-06-16".into(), kalshi: "K-LAD".into(), kalshi_b: Some("K-PIT".into()), cat: Cat::Sports, cluster: "mlb-2026-06-16".into(), settle_clean: false, soccer: false, days_to_event: Some(1.0), pm_min_tick: None, pm_min_qty: None });
        assert_eq!(ps.by_ticker.get("K-LAD").map(String::as_str), Some("aec-mlb-lad-pit-2026-06-16"));
        assert_eq!(ps.by_ticker.get("K-PIT").map(String::as_str), Some("aec-mlb-lad-pit-2026-06-16"));
        ps.remove("aec-mlb-lad-pit-2026-06-16");
        assert!(!ps.by_ticker.contains_key("K-LAD") && !ps.by_ticker.contains_key("K-PIT"));
        assert!(ps.by_slug.is_empty());
    }

    use std::sync::atomic::{AtomicBool, Ordering};

    fn sim_ack(coid: &str) -> Result<exec::Ack, exec::ExecError> {
        Ok(exec::Ack { client_order_id: coid.into(), venue_order_id: "SIMULATED".into(), filled: true, simulated: true })
    }
    fn live_ack(coid: &str) -> Result<exec::Ack, exec::ExecError> {
        Ok(exec::Ack { client_order_id: coid.into(), venue_order_id: "v1".into(), filled: true, simulated: false })
    }
    fn wx_entry_pair() -> (LivePair, Position, f64) {
        let pair = LivePair {
            slug: "tc-temp-nychigh-2026-06-11-gte95f".into(),
            kalshi: "KXHIGHNY-26JUN11-T95".into(),
            kalshi_b: None, cat: Cat::Weather, cluster: "nychigh-2026-06-11".into(), settle_clean: true, soccer: false, days_to_event: None,
            pm_min_tick: None, pm_min_qty: None,
        };
        let legs = [
            OrderIntent { venue: Venue::Pmus, market: pair.slug.clone(), action: Action::Buy, side: Side::Yes, price_cents: 7, qty: 3, client_order_id: "xarb-…-A".into() },
            OrderIntent { venue: Venue::Kalshi, market: pair.kalshi.clone(), action: Action::Buy, side: Side::No, price_cents: 90, qty: 3, client_order_id: "xarb-…-B".into() },
        ];
        (pair.clone(), position_from_intents(&pair.slug, pair.cat, &pair.cluster, pair.pm_min_tick, &legs), 0.97)
    }

    /// A test harness for `apply_outcome` that supplies the FIX-A recovery args (backend + books + outcome
    /// channel). `kalshi_books`/`pmus_books` are empty by default (no priceable flatten -> recovery declines
    /// and the halt backstop runs) unless a test pre-populates them. Returns the `outcome_rx` so a test can
    /// assert whether a recovery SELL was actually spawned.
    #[allow(clippy::too_many_arguments)]
    fn run_apply(
        backend: &std::sync::Arc<dyn ExecutionBackend>,
        positions: &std::sync::Arc<std::sync::Mutex<std::collections::HashMap<String, postpone::SlugPositions>>>,
        kalshi_books: &std::sync::Arc<std::sync::Mutex<std::collections::HashMap<String, book::KalshiBook>>>,
        pmus_books: &std::collections::HashMap<String, book::PmusBook>,
        exp: &mut Exposure,
        pending: &mut std::collections::HashSet<String>,
        flat: &mut std::collections::HashMap<String, FlatKind>,
        halt: &AtomicBool,
        out: SubmitOutcome,
    ) -> tokio::sync::mpsc::UnboundedReceiver<SubmitOutcome> {
        let (tx, rx) = tokio::sync::mpsc::unbounded_channel::<SubmitOutcome>();
        apply_outcome(backend, positions, kalshi_books, pmus_books, exp, pending, flat, &tx, halt, out);
        rx
    }

    fn dry_backend() -> std::sync::Arc<dyn ExecutionBackend> {
        std::sync::Arc::new(exec::DryRunBackend)
    }
    fn empty_kbooks() -> std::sync::Arc<std::sync::Mutex<std::collections::HashMap<String, book::KalshiBook>>> {
        std::sync::Arc::new(std::sync::Mutex::new(std::collections::HashMap::new()))
    }

    /// CORE: a BOTH-FILLED entry outcome RECORDS the position and KEEPS the spawn reservation (exposure
    /// unchanged from the reserve), and clears the slug's `pending_entries` marker.
    #[test]
    fn outcome_entry_both_filled_records_and_keeps_reservation() {
        use std::sync::{Arc, Mutex};
        let positions = Arc::new(Mutex::new(std::collections::HashMap::new()));
        let mut exp = Exposure::new();
        let mut pending: std::collections::HashSet<String> = std::collections::HashSet::new();
        let mut flat: std::collections::HashMap<String, FlatKind> = std::collections::HashMap::new();
        let halt = AtomicBool::new(false);
        let (pair, pos, cp) = wx_entry_pair();
        let slug = pos.market.clone();
        // SPAWN-time bookkeeping: reserve + mark pending.
        reserve_exposure(&mut exp, &pos, cp);
        pending.insert(slug.clone());
        let reserved_total = exp.total;
        let out = SubmitOutcome { slug: slug.clone(), kind: SubmitKind::Entry, ack: exec::PairAck { a: sim_ack("a"), b: sim_ack("b") }, position: Some(pos), pair: Some(pair), cost_per: cp, entry_net: 0.03, entry_dir: Dir::PK };
        run_apply(&dry_backend(), &positions, &empty_kbooks(), &std::collections::HashMap::new(), &mut exp, &mut pending, &mut flat, &halt, out);
        assert!((exp.total - reserved_total).abs() < 1e-9, "both-filled keeps the reservation");
        assert!(positions.lock().unwrap().contains_key(&slug), "position recorded");
        assert!(!pending.contains(&slug), "pending marker cleared");
        assert!(!halt.load(Ordering::Relaxed), "a clean simulated fill never halts");
    }

    /// CORE: a NON-both-filled entry outcome RELEASES the exact reservation (exposure back to zero) and
    /// clears `pending_entries`. A SIMULATED partial (dry-run can't produce one, but a transport error can
    /// in live with keys absent) does NOT trip the naked-leg halt because neither leg actually filled live.
    #[test]
    fn outcome_entry_failed_releases_reservation() {
        use std::sync::{Arc, Mutex};
        let positions = Arc::new(Mutex::new(std::collections::HashMap::new()));
        let mut exp = Exposure::new();
        let mut pending: std::collections::HashSet<String> = std::collections::HashSet::new();
        let mut flat: std::collections::HashMap<String, FlatKind> = std::collections::HashMap::new();
        let halt = AtomicBool::new(false);
        let (pair, pos, cp) = wx_entry_pair();
        let slug = pos.market.clone();
        reserve_exposure(&mut exp, &pos, cp);
        pending.insert(slug.clone());
        // both legs errored (e.g. KeysUnavailable) -> not both_filled, no live fill -> release, no halt.
        let out = SubmitOutcome { slug: slug.clone(), kind: SubmitKind::Entry, ack: exec::PairAck { a: Err(exec::ExecError::KeysUnavailable), b: Err(exec::ExecError::KeysUnavailable) }, position: Some(pos), pair: Some(pair), cost_per: cp, entry_net: 0.03, entry_dir: Dir::PK };
        run_apply(&dry_backend(), &positions, &empty_kbooks(), &std::collections::HashMap::new(), &mut exp, &mut pending, &mut flat, &halt, out);
        assert!(exp.total.abs() < 1e-9 && exp.open_positions == 0, "reservation released exactly");
        assert!(!positions.lock().unwrap().contains_key(&slug), "no position recorded on a failed entry");
        assert!(!pending.contains(&slug));
        assert!(!halt.load(Ordering::Relaxed), "no LIVE leg filled -> no naked-leg halt");
    }

    /// W14 FAIL-CLOSE BACKSTOP: an entry where ONE leg filled LIVE and the other errored is naked. When the
    /// filled leg CANNOT be priced for a flatten (no live book here), auto-recovery (FIX A) declines and the
    /// halt backstop engages (blocks all new entries) + the reservation is still released.
    #[test]
    fn outcome_naked_live_leg_engages_halt_when_recovery_unpriceable() {
        use std::sync::{Arc, Mutex};
        let positions = Arc::new(Mutex::new(std::collections::HashMap::new()));
        let mut exp = Exposure::new();
        let mut pending: std::collections::HashSet<String> = std::collections::HashSet::new();
        let mut flat: std::collections::HashMap<String, FlatKind> = std::collections::HashMap::new();
        let halt = AtomicBool::new(false);
        let (pair, pos, cp) = wx_entry_pair();
        let slug = pos.market.clone();
        reserve_exposure(&mut exp, &pos, cp);
        pending.insert(slug.clone());
        // leg A filled LIVE, leg B rate-limited -> NAKED. No book is available -> recovery can't price the
        // flatten -> the halt backstop runs (this is the FAIL-SAFE: never leave the leg silently naked).
        let out = SubmitOutcome { slug: slug.clone(), kind: SubmitKind::Entry, ack: exec::PairAck { a: live_ack("a"), b: Err(exec::ExecError::RateLimited) }, position: Some(pos), pair: Some(pair), cost_per: cp, entry_net: 0.03, entry_dir: Dir::PK };
        run_apply(&dry_backend(), &positions, &empty_kbooks(), &std::collections::HashMap::new(), &mut exp, &mut pending, &mut flat, &halt, out);
        assert!(halt.load(Ordering::Relaxed), "an unpriceable naked LIVE leg must engage the kill-switch backstop");
        assert!(exp.total.abs() < 1e-9, "the entry reservation is still released");
        assert!(!pending.contains(&slug));
        // a SIMULATED-only partial does NOT halt (dry-run safety): one simulated ok + one error.
        let halt2 = AtomicBool::new(false);
        naked_leg_failclose("s", SubmitKind::Entry, &exec::PairAck { a: sim_ack("a"), b: Err(exec::ExecError::RateLimited) }, &halt2);
        assert!(!halt2.load(Ordering::Relaxed), "a simulated partial is not a real naked leg");
        assert_eq!(naked_filled_idx(&exec::PairAck { a: sim_ack("a"), b: Err(exec::ExecError::RateLimited) }), None, "a simulated leg is not a live naked leg");
    }

    /// CORE: a BOTH-FILLED unwind outcome REMOVES the held position + decrements exposure + clears the
    /// `flattening` marker (so the slug is fully closed).
    #[test]
    fn outcome_unwind_both_filled_removes_position() {
        use std::sync::{Arc, Mutex};
        let positions = Arc::new(Mutex::new(std::collections::HashMap::new()));
        let mut exp = Exposure::new();
        let mut pending: std::collections::HashSet<String> = std::collections::HashSet::new();
        let mut flat: std::collections::HashMap<String, FlatKind> = std::collections::HashMap::new();
        let halt = AtomicBool::new(false);
        let (pair, pos, cp) = wx_entry_pair();
        let slug = pos.market.clone();
        // an open, recorded position with its reservation, now mid-flatten.
        reserve_exposure(&mut exp, &pos, cp);
        track_position(&positions, &pair, pos, cp, 0.03, Dir::PK);
        flat.insert(slug.clone(), FlatKind::Unwind);
        let out = SubmitOutcome { slug: slug.clone(), kind: SubmitKind::Unwind, ack: exec::PairAck { a: sim_ack("u0"), b: sim_ack("u1") }, position: None, pair: None, cost_per: 0.0, entry_net: 0.0, entry_dir: Dir::PK };
        run_apply(&dry_backend(), &positions, &empty_kbooks(), &std::collections::HashMap::new(), &mut exp, &mut pending, &mut flat, &halt, out);
        assert!(!positions.lock().unwrap().contains_key(&slug), "position removed on flatten");
        assert!(exp.total.abs() < 1e-9 && exp.open_positions == 0, "exposure decremented on flatten");
        assert!(!flat.contains_key(&slug), "flattening marker cleared");
    }

    /// FIX 3: a both-filled entry PERSISTS each leg's exchange order id (from its fill ack, positionally:
    /// ack.a -> legs[0], ack.b -> legs[1]) onto the tracked position, so a later cancel/unwind can build a
    /// `CancelTarget` and reach the right venue endpoint. Pre-fix the id was parsed then dropped.
    #[test]
    fn outcome_entry_persists_venue_order_ids_on_legs() {
        use std::sync::{Arc, Mutex};
        let positions = Arc::new(Mutex::new(std::collections::HashMap::new()));
        let mut exp = Exposure::new();
        let mut pending: std::collections::HashSet<String> = std::collections::HashSet::new();
        let mut flat: std::collections::HashMap<String, FlatKind> = std::collections::HashMap::new();
        let halt = AtomicBool::new(false);
        let (pair, pos, cp) = wx_entry_pair();
        let slug = pos.market.clone();
        reserve_exposure(&mut exp, &pos, cp);
        pending.insert(slug.clone());
        // both legs filled live with DISTINCT venue order ids.
        let a = Ok(exec::Ack { client_order_id: "A".into(), venue_order_id: "PM-ORD-1".into(), filled: true, simulated: false });
        let b = Ok(exec::Ack { client_order_id: "B".into(), venue_order_id: "K-ORD-2".into(), filled: true, simulated: false });
        let out = SubmitOutcome { slug: slug.clone(), kind: SubmitKind::Entry, ack: exec::PairAck { a, b }, position: Some(pos), pair: Some(pair), cost_per: cp, entry_net: 0.03, entry_dir: Dir::PK };
        run_apply(&dry_backend(), &positions, &empty_kbooks(), &std::collections::HashMap::new(), &mut exp, &mut pending, &mut flat, &halt, out);
        let sp = positions.lock().unwrap().get(&slug).cloned().expect("position recorded");
        // the single appended leg's two PositionLegs: leg 0 = pmus (PM-ORD-1), leg 1 = Kalshi (K-ORD-2) —
        // ids persisted onto the held legs (FIX 3).
        let held = &sp.legs[0].pos;
        assert_eq!(held.legs[0].venue_order_id, "PM-ORD-1");
        assert_eq!(held.legs[1].venue_order_id, "K-ORD-2");
        assert!(!halt.load(Ordering::Relaxed), "a clean both-filled live entry does not halt");
    }

    /// FIX 1 end-to-end: an entry where one leg FILLED live and the other came back `Ok` but RESTING
    /// (accepted, not filled) is NOT a hedge — `apply_outcome` must NOT record a position, must release the
    /// reservation. The filled leg is naked; with no priceable book here, the FIX-A recovery declines and the
    /// halt backstop engages. (This is the exact case the pre-fix `is_ok()`-only `both_filled` mis-recorded.)
    #[test]
    fn outcome_entry_one_resting_leg_is_naked_not_a_hedge() {
        use std::sync::{Arc, Mutex};
        let positions = Arc::new(Mutex::new(std::collections::HashMap::new()));
        let mut exp = Exposure::new();
        let mut pending: std::collections::HashSet<String> = std::collections::HashSet::new();
        let mut flat: std::collections::HashMap<String, FlatKind> = std::collections::HashMap::new();
        let halt = AtomicBool::new(false);
        let (pair, pos, cp) = wx_entry_pair();
        let slug = pos.market.clone();
        reserve_exposure(&mut exp, &pos, cp);
        pending.insert(slug.clone());
        // leg A filled live; leg B ACCEPTED but resting (Ok, filled:false) -> not both-filled -> naked.
        let a = Ok(exec::Ack { client_order_id: "A".into(), venue_order_id: "K-1".into(), filled: true, simulated: false });
        let b = Ok(exec::Ack { client_order_id: "B".into(), venue_order_id: "PM-2".into(), filled: false, simulated: false });
        let out = SubmitOutcome { slug: slug.clone(), kind: SubmitKind::Entry, ack: exec::PairAck { a, b }, position: Some(pos), pair: Some(pair), cost_per: cp, entry_net: 0.03, entry_dir: Dir::PK };
        run_apply(&dry_backend(), &positions, &empty_kbooks(), &std::collections::HashMap::new(), &mut exp, &mut pending, &mut flat, &halt, out);
        assert!(!positions.lock().unwrap().contains_key(&slug), "a one-resting-leg entry is NOT recorded as a hedge");
        assert!(exp.total.abs() < 1e-9 && exp.open_positions == 0, "reservation released");
        assert!(halt.load(Ordering::Relaxed), "the filled leg is naked + unpriceable -> halt backstop engaged");
        assert!(!pending.contains(&slug));
    }

    /// FIX A — NAKED-LEG AUTO-RECOVERY (the task's required test): a one-leg-filled entry where the FILLED
    /// leg CAN be priced from a live book triggers recovery instead of a bare halt — a CANCEL of the resting
    /// leg + a SELL of the filled leg are spawned, NO position is recorded, NO held hedge, and the halt is
    /// NOT engaged (the position is being flattened, not left naked). Leg A = YES@pmus filled live; leg B =
    /// NO@Kalshi resting. The pmus book quotes a YES bid, so the filled YES@pmus leg flattens at that bid.
    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn outcome_naked_leg_recovers_with_cancel_and_sell() {
        use std::sync::{Arc, Mutex};
        let positions = Arc::new(Mutex::new(std::collections::HashMap::new()));
        let mut exp = Exposure::new();
        let mut pending: std::collections::HashSet<String> = std::collections::HashSet::new();
        let mut flat: std::collections::HashMap<String, FlatKind> = std::collections::HashMap::new();
        let halt = AtomicBool::new(false);
        let (pair, pos, cp) = wx_entry_pair(); // leg0 = YES@pmus(slug); leg1 = NO@Kalshi(ticker)
        let slug = pos.market.clone();
        reserve_exposure(&mut exp, &pos, cp);
        pending.insert(slug.clone());
        // the pmus book for the FILLED leg's market quotes a YES bid (0.06) so the flatten SELL can be priced.
        let mut pmus_books: std::collections::HashMap<String, book::PmusBook> = std::collections::HashMap::new();
        let mut pb = book::PmusBook::new();
        pb.apply_snapshot(&[(0.06, 500.0)], &[(0.08, 500.0)]);
        pmus_books.insert(slug.clone(), pb);
        // leg A (pmus) filled LIVE; leg B (Kalshi) resting with a venue order id (so it gets cancelled).
        let a = Ok(exec::Ack { client_order_id: "A".into(), venue_order_id: "PM-1".into(), filled: true, simulated: false });
        let b = Ok(exec::Ack { client_order_id: "B".into(), venue_order_id: "K-2".into(), filled: false, simulated: false });
        let out = SubmitOutcome { slug: slug.clone(), kind: SubmitKind::Entry, ack: exec::PairAck { a, b }, position: Some(pos), pair: Some(pair), cost_per: cp, entry_net: 0.03, entry_dir: Dir::PK };
        let mut rx = run_apply(&dry_backend(), &positions, &empty_kbooks(), &pmus_books, &mut exp, &mut pending, &mut flat, &halt, out);
        // RECOVERY launched: no held hedge recorded, reservation released, NOT a bare halt, slug marked flattening.
        assert!(!positions.lock().unwrap().contains_key(&slug), "recovery records NO hedge");
        assert!(exp.total.abs() < 1e-9, "the entry reservation is released");
        assert!(!halt.load(Ordering::Relaxed), "recovery flattens -> does NOT engage the halt backstop");
        assert!(flat.contains_key(&slug), "the slug is marked flattening (dedup against a double-fire)");
        assert!(!pending.contains(&slug), "the entry in-flight marker is cleared");
        // the spawned flatten reports a RECOVERY outcome (the dry-run SELL fills): drain it to confirm a SELL
        // was actually fired (dry-run `submit` returns a simulated filled ack -> the recovery completes).
        let recovered = tokio::time::timeout(std::time::Duration::from_secs(2), rx.recv()).await.expect("recovery outcome timed out").expect("an outcome was sent");
        assert_eq!(recovered.kind, SubmitKind::Recovery, "the flatten routes back as a Recovery outcome");
        assert!(matches!(&recovered.ack.a, Ok(a) if a.filled), "leg a is the SELL and the dry-run flatten fills");
    }

    /// FIX W2 — the recovery-flatten SELL FLOOR-quantizes to the held pmus leg's coarse `orderPriceMinTickSize`
    /// so a coarse-tick pmus market doesn't REJECT it (which would bounce the recovery to a needless halt). A
    /// SELL floors (limit <= touch -> still hits the bid). Directly on the pure pricer + end-to-end on the
    /// recovery path: a YES@pmus leg with a 0.05 tick, book YES bid 0.93 -> floors to 0.90 = 90c (not 93c).
    #[test]
    fn flatten_sell_floors_to_the_pmus_tick() {
        // a pmus YES leg carrying a coarse 0.05 tick; book best YES bid 0.93 (not a 0.05 multiple).
        let coarse = PositionLeg { venue: Venue::Pmus, market: "tc-temp-nychigh-2026-06-11-gte95f".into(), side: Side::Yes, pm_min_tick: Some(0.05), ..Default::default() };
        let book = Book { yes_bid: Some(0.93), yes_ask: Some(0.95), age_s: 0.0 };
        // SELL floors 0.93 -> the 0.05 tick 0.90 -> 90c (a 93c SELL would reject on a 0.05-tick market).
        assert_eq!(flatten_exit_cents(&coarse, &book), Some(90), "the recovery SELL floors to a valid coarse tick");
        // a NO@pmus leg on the same tick: exit = 1 - yes_ask(0.95) = 0.05 -> floors to the 0.05 tick = 5c.
        let coarse_no = PositionLeg { side: Side::No, ..coarse.clone() };
        assert_eq!(flatten_exit_cents(&coarse_no, &book), Some(5), "NO-leg flatten also floors to the tick");
        // no pmus tick (Kalshi/weather/econ leg) -> just the cent floor, unchanged: 0.93 -> 93c.
        let fine = PositionLeg { pm_min_tick: None, ..coarse.clone() };
        assert_eq!(flatten_exit_cents(&fine, &book), Some(93), "no tick -> cent-granularity floor, no tick snap");
        // a one-sided book (no YES bid for a YES leg) -> None so the caller halts rather than misprice.
        assert_eq!(flatten_exit_cents(&coarse, &Book { yes_bid: None, yes_ask: Some(0.95), age_s: 0.0 }), None);
    }

    /// FIX W2 end-to-end: a one-leg-filled entry whose FILLED leg is a coarse-tick pmus market still RECOVERS
    /// (the floored SELL is a valid tick, so the flatten is priceable and recovery launches) — it does NOT
    /// fall through to the halt backstop the way an un-quantized whole-cent SELL would on a coarse market.
    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn coarse_tick_pmus_leg_still_recovers() {
        use std::sync::{Arc, Mutex};
        let positions = Arc::new(Mutex::new(std::collections::HashMap::new()));
        let mut exp = Exposure::new();
        let mut pending: std::collections::HashSet<String> = std::collections::HashSet::new();
        let mut flat: std::collections::HashMap<String, FlatKind> = std::collections::HashMap::new();
        let halt = AtomicBool::new(false);
        // a weather PK pair whose pmus market has a coarse 0.05 tick -> the held pmus leg carries it (W2).
        let pair = LivePair {
            slug: "tc-temp-nychigh-2026-06-11-gte95f".into(),
            kalshi: "KXHIGHNY-26JUN11-T95".into(),
            kalshi_b: None, cat: Cat::Weather, cluster: "nychigh-2026-06-11".into(), settle_clean: true, soccer: false, days_to_event: None,
            pm_min_tick: Some(0.05), pm_min_qty: None,
        };
        let legs = [
            OrderIntent { venue: Venue::Pmus, market: pair.slug.clone(), action: Action::Buy, side: Side::Yes, price_cents: 10, qty: 2, client_order_id: "xarb-…-A".into() },
            OrderIntent { venue: Venue::Kalshi, market: pair.kalshi.clone(), action: Action::Buy, side: Side::No, price_cents: 88, qty: 2, client_order_id: "xarb-…-B".into() },
        ];
        let pos = position_from_intents(&pair.slug, pair.cat, &pair.cluster, pair.pm_min_tick, &legs);
        // the tick is recorded ONLY on the pmus leg (W2); the Kalshi leg carries None.
        assert_eq!(pos.legs[0].pm_min_tick, Some(0.05), "pmus leg carries the tick");
        assert_eq!(pos.legs[1].pm_min_tick, None, "Kalshi leg carries no pmus tick");
        let slug = pos.market.clone();
        reserve_exposure(&mut exp, &pos, 0.97);
        pending.insert(slug.clone());
        // the pmus book for the FILLED leg quotes a YES bid of 0.93 (not a 0.05 multiple) -> the flatten SELL
        // floors to 0.90 = 90c (a valid tick) and recovery launches; un-quantized it would have been 93c.
        let mut pmus_books: std::collections::HashMap<String, book::PmusBook> = std::collections::HashMap::new();
        let mut pb = book::PmusBook::new();
        pb.apply_snapshot(&[(0.93, 500.0)], &[(0.95, 500.0)]);
        pmus_books.insert(slug.clone(), pb);
        let a = Ok(exec::Ack { client_order_id: "A".into(), venue_order_id: "PM-1".into(), filled: true, simulated: false });
        let b = Ok(exec::Ack { client_order_id: "B".into(), venue_order_id: "K-2".into(), filled: false, simulated: false });
        let out = SubmitOutcome { slug: slug.clone(), kind: SubmitKind::Entry, ack: exec::PairAck { a, b }, position: Some(pos), pair: Some(pair), cost_per: 0.97, entry_net: 0.03, entry_dir: Dir::PK };
        let mut rx = run_apply(&dry_backend(), &positions, &empty_kbooks(), &pmus_books, &mut exp, &mut pending, &mut flat, &halt, out);
        // RECOVERY launched (not the halt backstop): the coarse-tick SELL was priceable.
        assert!(!halt.load(Ordering::Relaxed), "a coarse-tick pmus leg recovers -> does NOT halt");
        assert!(flat.contains_key(&slug), "recovery launched (slug marked flattening)");
        let recovered = tokio::time::timeout(std::time::Duration::from_secs(2), rx.recv()).await.expect("recovery outcome timed out").expect("an outcome was sent");
        assert_eq!(recovered.kind, SubmitKind::Recovery, "the floored flatten fires and routes back as Recovery");
    }

    /// FIX A — the FAILED-recovery safety net (the self-review CRITICAL): when the recovery flatten SELL
    /// itself does NOT fill, the originally-filled leg is STILL naked, so the `Recovery` outcome MUST engage
    /// the halt. (The earlier simulated-sentinel design slipped this past `naked_filled_idx` with no halt.)
    #[test]
    fn recovery_flatten_that_does_not_fill_engages_halt() {
        use std::sync::{Arc, Mutex};
        let positions: Arc<Mutex<std::collections::HashMap<String, postpone::SlugPositions>>> = Arc::new(Mutex::new(std::collections::HashMap::new()));
        let mut exp = Exposure::new();
        let mut pending: std::collections::HashSet<String> = std::collections::HashSet::new();
        let mut flat: std::collections::HashMap<String, FlatKind> = std::collections::HashMap::new();
        let halt = AtomicBool::new(false);
        flat.insert("s".to_string(), FlatKind::Recovery); // a recovery flatten is in flight for this slug
        // the recovery SELL came back rate-limited (did NOT fill); leg b is the unused Err placeholder.
        let ack = exec::PairAck { a: Err(exec::ExecError::RateLimited), b: Err(exec::ExecError::Rejected("recovery has no second leg".into())) };
        let out = SubmitOutcome { slug: "s".into(), kind: SubmitKind::Recovery, ack, position: None, pair: None, cost_per: 0.0, entry_net: 0.0, entry_dir: Dir::PK };
        run_apply(&dry_backend(), &positions, &empty_kbooks(), &std::collections::HashMap::new(), &mut exp, &mut pending, &mut flat, &halt, out);
        assert!(halt.load(Ordering::Relaxed), "a recovery SELL that did not fill leaves a naked leg -> halt");
        assert!(!flat.contains_key("s"), "the flattening marker is cleared either way");
        // and a recovery SELL that DID fill clears cleanly without halting.
        let halt2 = AtomicBool::new(false);
        let mut flat2: std::collections::HashMap<String, FlatKind> = std::collections::HashMap::new();
        flat2.insert("s".to_string(), FlatKind::Recovery);
        let ok = exec::PairAck { a: Ok(exec::Ack { client_order_id: "r".into(), venue_order_id: "v".into(), filled: true, simulated: false }), b: Err(exec::ExecError::Rejected("recovery has no second leg".into())) };
        let out2 = SubmitOutcome { slug: "s".into(), kind: SubmitKind::Recovery, ack: ok, position: None, pair: None, cost_per: 0.0, entry_net: 0.0, entry_dir: Dir::PK };
        run_apply(&dry_backend(), &positions, &empty_kbooks(), &std::collections::HashMap::new(), &mut exp, &mut pending, &mut flat2, &halt2, out2);
        assert!(!halt2.load(Ordering::Relaxed), "a filled recovery SELL clears without halting");
    }

    /// `naked_filled_idx` pinpoints the live-filled leg: leg-a live-filled + b unfilled -> Some(0); the
    /// mirror -> Some(1); both filled / both errored / a simulated fill -> None (no real naked leg).
    #[test]
    fn naked_filled_idx_identifies_the_live_leg() {
        assert_eq!(naked_filled_idx(&exec::PairAck { a: live_ack("a"), b: Err(exec::ExecError::RateLimited) }), Some(0));
        assert_eq!(naked_filled_idx(&exec::PairAck { a: Err(exec::ExecError::RateLimited), b: live_ack("b") }), Some(1));
        // a resting (Ok, filled:false) other leg still leaves the filled leg naked.
        let resting = Ok(exec::Ack { client_order_id: "r".into(), venue_order_id: "v".into(), filled: false, simulated: false });
        assert_eq!(naked_filled_idx(&exec::PairAck { a: live_ack("a"), b: resting }), Some(0));
        // both filled -> no naked leg; both errored -> none; a simulated fill is not a LIVE naked leg.
        assert_eq!(naked_filled_idx(&exec::PairAck { a: live_ack("a"), b: live_ack("b") }), None);
        assert_eq!(naked_filled_idx(&exec::PairAck { a: Err(exec::ExecError::RateLimited), b: Err(exec::ExecError::RateLimited) }), None);
        assert_eq!(naked_filled_idx(&exec::PairAck { a: sim_ack("a"), b: Err(exec::ExecError::RateLimited) }), None);
    }

    // ====================================================================================================
    // SCALE-IN + RE-ENTRY (multi-position-per-slug) — design tasks/scale-in-reentry-design.md §6 (10 tests)
    // ====================================================================================================

    /// Build a `SlugPositions` with `n` held legs on one slug, each leg reserving `cost_per*size`, recording
    /// `entry_net`/`entry_dir` — and bump `exp` by the SAME reservation each leg (mirrors the spawn-time
    /// reserve so the map and the exposure buckets agree, the real invariant). Returns the slug.
    fn seed_legs(positions: &std::sync::Arc<std::sync::Mutex<std::collections::HashMap<String, postpone::SlugPositions>>>, exp: &mut Exposure, pair: &LivePair, costs_nets: &[(f64, f64)], dir: Dir) -> String {
        let mut slug = String::new();
        for (i, &(cost_per, entry_net)) in costs_nets.iter().enumerate() {
            let legs = [
                OrderIntent { venue: Venue::Pmus, market: pair.slug.clone(), action: Action::Buy, side: Side::Yes, price_cents: 7, qty: 3, client_order_id: format!("a{i}") },
                OrderIntent { venue: Venue::Kalshi, market: pair.kalshi.clone(), action: Action::Buy, side: Side::No, price_cents: 90, qty: 3, client_order_id: format!("b{i}") },
            ];
            let pos = position_from_intents(&pair.slug, pair.cat, &pair.cluster, pair.pm_min_tick, &legs);
            reserve_exposure(exp, &pos, cost_per);
            track_position(positions, pair, pos, cost_per, entry_net, dir);
            slug = pair.slug.clone();
        }
        slug
    }

    /// A both-filled Unwind outcome for `slug` (the bookkeeping the poll's re-emit drives one-at-a-time).
    fn unwind_both_filled(slug: &str) -> SubmitOutcome {
        SubmitOutcome { slug: slug.into(), kind: SubmitKind::Unwind, ack: exec::PairAck { a: sim_ack("u0"), b: sim_ack("u1") }, position: None, pair: None, cost_per: 0.0, entry_net: 0.0, entry_dir: Dir::PK }
    }

    /// TEST 1 (THE DESYNC REGRESSION — design §6.1 / R1): two positions A+B stacked on ONE slug. Decrement A
    /// (the FRONT leg, via the real Unwind outcome path) -> the per-pair/cluster/total buckets equal exactly
    /// B's contribution (NOT zero — the old whole-bucket `remove` wiped BOTH), open=1, the slug is KEPT.
    /// Decrement B -> buckets ~0, open=0, the slug KEY is gone (R5). This is the exact desync the
    /// one-position guard used to prevent; it MUST be airtight.
    #[test]
    fn test1_multi_position_exact_reserve_and_release_no_desync() {
        use std::sync::{Arc, Mutex};
        let positions = Arc::new(Mutex::new(std::collections::HashMap::new()));
        let mut exp = Exposure::new();
        let mut pending: std::collections::HashSet<String> = std::collections::HashSet::new();
        let mut flat: std::collections::HashMap<String, FlatKind> = std::collections::HashMap::new();
        let halt = AtomicBool::new(false);
        let pair = wx_pair();
        // A: cost_per 0.90, B: cost_per 0.95 (DISTINCT so a desync — subtracting the wrong amount — is
        // detectable). size = 3 each (from seed_legs). A is the FRONT leg (appended first).
        let (ca, cb, size) = (0.90_f64, 0.95_f64, 3.0_f64);
        let slug = seed_legs(&positions, &mut exp, &pair, &[(ca, 0.03), (cb, 0.05)], Dir::PK);
        // reserved = A + B on every bucket; two open positions.
        let want_a = ca * size;
        let want_b = cb * size;
        assert!((exp.per_pair[&slug] - (want_a + want_b)).abs() < 1e-9, "per_pair = A + B reserved");
        assert!((exp.per_cluster[&pair.cluster] - (want_a + want_b)).abs() < 1e-9);
        assert!((exp.total - (want_a + want_b)).abs() < 1e-9);
        assert_eq!(exp.open_positions, 2);
        assert_eq!(positions.lock().unwrap().get(&slug).unwrap().legs.len(), 2);

        // DECREMENT A (front leg) via the real both-filled Unwind outcome arm.
        flat.insert(slug.clone(), FlatKind::Unwind);
        run_apply(&dry_backend(), &positions, &empty_kbooks(), &std::collections::HashMap::new(), &mut exp, &mut pending, &mut flat, &halt, unwind_both_filled(&slug));
        // buckets now equal EXACTLY B's contribution (the desync would have wiped the whole bucket to 0).
        assert!((exp.per_pair[&slug] - want_b).abs() < 1e-9, "after A: per_pair == B exactly (NOT 0 — the old desync)");
        assert!((exp.per_cluster[&pair.cluster] - want_b).abs() < 1e-9, "after A: per_cluster == B");
        assert!((exp.total - want_b).abs() < 1e-9, "after A: total == B");
        assert_eq!(exp.open_positions, 1, "one position still open");
        assert!(positions.lock().unwrap().contains_key(&slug), "slug KEPT while B remains (R5)");
        assert_eq!(positions.lock().unwrap().get(&slug).unwrap().legs.len(), 1, "B is the remaining leg");

        // DECREMENT B (now the front leg) -> everything to ~0, slug key gone.
        flat.insert(slug.clone(), FlatKind::Unwind);
        run_apply(&dry_backend(), &positions, &empty_kbooks(), &std::collections::HashMap::new(), &mut exp, &mut pending, &mut flat, &halt, unwind_both_filled(&slug));
        assert!(exp.per_pair.get(&slug).copied().unwrap_or(0.0).abs() < 1e-9, "after B: per_pair ~0");
        assert!(exp.per_cluster.get(&pair.cluster).copied().unwrap_or(0.0).abs() < 1e-9, "after B: per_cluster ~0");
        assert!(exp.total.abs() < 1e-9, "after B: total ~0");
        assert_eq!(exp.open_positions, 0, "no positions open");
        assert!(!positions.lock().unwrap().contains_key(&slug), "slug KEY gone once legs empty (R5)");
    }

    /// TEST 2 (design §6.2): `track_position` APPENDS a leg + PRESERVES the shared `prev`. (The C6
    /// preservation is now structural; this pins it for the append path explicitly.)
    #[test]
    fn test2_track_position_appends_and_preserves_prev() {
        use std::sync::{Arc, Mutex};
        let positions = Arc::new(Mutex::new(std::collections::HashMap::new()));
        let mut exp = Exposure::new();
        let pair = wx_pair();
        seed_legs(&positions, &mut exp, &pair, &[(0.90, 0.03)], Dir::PK);
        // the poll accumulates a prev snapshot on the slug.
        let snap = postpone::GameStatus { detailed_state: "Scheduled".into(), official_date: Some("2026-06-11".into()), ..Default::default() };
        positions.lock().unwrap().get_mut(&pair.slug).unwrap().prev = Some(snap.clone());
        // a second add APPENDS (now 2 legs) and leaves prev untouched.
        seed_legs(&positions, &mut exp, &pair, &[(0.95, 0.05)], Dir::PK);
        let sp = positions.lock().unwrap().get(&pair.slug).cloned().unwrap();
        assert_eq!(sp.legs.len(), 2, "the second add appended a leg");
        assert_eq!(sp.prev, Some(snap), "prev (slug-level) survived the append");
    }

    /// A config with the add knobs set (else everything stays at the safe defaults).
    fn add_cfg(scale_in: bool, reentry: bool, cap: u32) -> Config {
        let mut c = crate::config::Config::test_default();
        c.enable_scale_in = scale_in;
        c.enable_reentry = reentry;
        c.max_positions_per_slug = cap;
        c.add_tau_gain = 0.01;
        c
    }
    fn held_leg(entry_net: f64, dir: Dir) -> postpone::HeldLeg {
        let leg = OrderIntent { venue: Venue::Pmus, market: "s".into(), action: Action::Buy, side: Side::Yes, price_cents: 7, qty: 3, client_order_id: "x".into() };
        let pos = position_from_intents("s", Cat::Weather, "c", None, &[leg.clone(), OrderIntent { venue: Venue::Kalshi, market: "K".into(), side: Side::No, ..leg }]);
        postpone::HeldLeg { pos, cost_per: 0.9, entry_net, entry_dir: dir }
    }

    /// TEST 3 (design §6.3): SCALE-IN vs RE-ENTRY classification, each flag independently admits/blocks.
    /// `was_live` (the §3 proxy) selects which flag gates the add.
    #[test]
    fn test3_scalein_vs_reentry_each_flag_gates_independently() {
        let held = [held_leg(0.03, Dir::PK)];
        let bigger = Edge { net: 0.06, dir: Dir::PK }; // > 0.03 + 0.01 tau -> qualifies on the net gate
        // edge LIVE (was_live=true) -> SCALE-IN: needs enable_scale_in.
        assert_eq!(qualifying_add(&add_cfg(true, false, 2), &held, &bigger, true), Some("scale-in"));
        assert_eq!(qualifying_add(&add_cfg(false, true, 2), &held, &bigger, true), None, "scale-in blocked when only re-entry is armed");
        // edge CLOSED (was_live=false) -> RE-ENTRY: needs enable_reentry.
        assert_eq!(qualifying_add(&add_cfg(false, true, 2), &held, &bigger, false), Some("re-entry"));
        assert_eq!(qualifying_add(&add_cfg(true, false, 2), &held, &bigger, false), None, "re-entry blocked when only scale-in is armed");
        // both armed -> the proxy picks the kind.
        assert_eq!(qualifying_add(&add_cfg(true, true, 2), &held, &bigger, true), Some("scale-in"));
        assert_eq!(qualifying_add(&add_cfg(true, true, 2), &held, &bigger, false), Some("re-entry"));
    }

    /// TEST 4 (design §6.4 / R6): the add_tau_gain gate + the same-direction requirement. An opposite-dir
    /// bigger arb is NOT an add (the gate forbids opening an opposite-direction position on a held slug).
    #[test]
    fn test4_tau_gain_and_same_direction_required() {
        let c = add_cfg(true, true, 2); // both armed + cap 2 so only the net/dir gates can block
        let held = [held_leg(0.03, Dir::PK)];
        // below tau: net 0.035 < 0.03 + 0.01 -> BLOCK (not enough gain over the base).
        assert_eq!(qualifying_add(&c, &held, &Edge { net: 0.035, dir: Dir::PK }, true), None, "add must beat base by >= tau");
        // exactly at tau boundary: 0.04 == 0.03 + 0.01 -> ADMIT (>= is inclusive).
        assert_eq!(qualifying_add(&c, &held, &Edge { net: 0.04, dir: Dir::PK }, true), Some("scale-in"), "net == base + tau passes (>=)");
        // OPPOSITE direction, even much bigger -> BLOCK (R6: never open an opposite-direction position).
        assert_eq!(qualifying_add(&c, &held, &Edge { net: 0.20, dir: Dir::KP }, true), None, "opposite-direction bigger arb is NOT an add");
        // gate must compare against the MAX held entry_net: two held legs (0.03, 0.06) -> base 0.06.
        let two = [held_leg(0.03, Dir::PK), held_leg(0.06, Dir::PK)];
        assert_eq!(qualifying_add(&add_cfg(true, true, 3), &two, &Edge { net: 0.065, dir: Dir::PK }, true), None, "must beat MAX(entry_net)=0.06 by tau, 0.065 < 0.07");
        assert_eq!(qualifying_add(&add_cfg(true, true, 3), &two, &Edge { net: 0.07, dir: Dir::PK }, true), Some("scale-in"));
    }

    /// TEST 5 (design §6.5): max_positions_per_slug binds. cap=1 => the FIRST add is blocked (default
    /// behavior); cap=2 => a 3rd add (when 2 are held) is blocked.
    #[test]
    fn test5_max_positions_per_slug_cap_binds() {
        let bigger = Edge { net: 0.10, dir: Dir::PK };
        let one = [held_leg(0.03, Dir::PK)];
        // cap=1, one held -> at the cap -> BLOCK (this is exactly the one-position-per-slug default).
        assert_eq!(qualifying_add(&add_cfg(true, true, 1), &one, &bigger, true), None, "cap=1 blocks the first add");
        // cap=2, one held -> room -> ADMIT.
        assert_eq!(qualifying_add(&add_cfg(true, true, 2), &one, &bigger, true), Some("scale-in"));
        // cap=2, TWO held -> at the cap -> BLOCK the 3rd.
        let two = [held_leg(0.03, Dir::PK), held_leg(0.05, Dir::PK)];
        assert_eq!(qualifying_add(&add_cfg(true, true, 2), &two, &bigger, true), None, "cap=2 blocks the 3rd add");
    }

    /// TEST 6 (design §6.6): notional caps bind ACROSS positions — pos#1 using the full per-pair room makes
    /// an otherwise-qualifying add `Reject::PairCap` in `evaluate` (the held leg's contribution is already in
    /// the per_pair bucket, so `pair_room` is what's LEFT). The cap is the real concentration bound the guard
    /// never enforced. (Cluster/total bind the same way — same `evaluate` arithmetic.)
    #[test]
    fn test6_notional_caps_bind_across_positions() {
        use crate::risk::{evaluate, Exposure, Reject};
        let mut c = add_cfg(true, true, 5);
        c.max_notional_per_pair = 1.0; // tiny per-pair cap
        c.max_contracts_per_pair = 100;
        let q = q_pk(); // a clean weather arb, dir PK, settle_clean
        let edge = Edge { net: 0.06, dir: Dir::PK };
        // pos#1 already consumed the whole per-pair cap (1.0) -> the qualifying add has zero pair_room.
        let mut exp = Exposure::new();
        exp.per_pair.insert(q.market.clone(), 1.0); // full per-pair notional in use
        exp.open_positions = 1;
        assert_eq!(evaluate(&c, &q, &edge, &exp, 1000), Err(Reject::PairCap), "per-pair cap binds the SUM across stacked positions");
        // and the cluster cap binds identically (fresh exp, cluster full).
        let mut exp2 = Exposure::new();
        exp2.per_cluster.insert(q.cluster.clone(), c.max_notional_per_cluster);
        exp2.open_positions = 1;
        assert_eq!(evaluate(&c, &q, &edge, &exp2, 1000), Err(Reject::ClusterCap), "per-cluster cap binds across positions");
    }

    /// TEST 7 (design §6.7 / §1.4): a half-filled ADD is recovered from its OWN outcome, and the
    /// already-held base position's exposure is left INTACT. The add's spawn-reservation is released exactly
    /// (subtract this attempt's `cost_per*size`), the add's own naked leg is recovered/halted — the base
    /// leg's contribution and the base leg itself are untouched.
    #[test]
    fn test7_recovery_picks_own_leg_base_position_intact() {
        use std::sync::{Arc, Mutex};
        let positions = Arc::new(Mutex::new(std::collections::HashMap::new()));
        let mut exp = Exposure::new();
        let mut pending: std::collections::HashSet<String> = std::collections::HashSet::new();
        let mut flat: std::collections::HashMap<String, FlatKind> = std::collections::HashMap::new();
        let halt = AtomicBool::new(false);
        let pair = wx_pair();
        // a base position is HELD (reserved 0.90*3) and recorded as one leg.
        let slug = seed_legs(&positions, &mut exp, &pair, &[(0.90, 0.03)], Dir::PK);
        let base_total = exp.total;
        let base_pair = exp.per_pair[&slug];
        assert_eq!(exp.open_positions, 1);
        // the ADD is spawn-reserved (0.95*3) on top, then comes back HALF-FILLED (leg A live, leg B errored)
        // -> NOT both_filled -> release the ADD's exact reservation; the add's naked leg can't be priced (no
        // book) -> halt backstop. Crucially this touches ONLY the add's reservation, not the base's.
        let add_legs = [
            OrderIntent { venue: Venue::Pmus, market: pair.slug.clone(), action: Action::Buy, side: Side::Yes, price_cents: 7, qty: 3, client_order_id: "a2".into() },
            OrderIntent { venue: Venue::Kalshi, market: pair.kalshi.clone(), action: Action::Buy, side: Side::No, price_cents: 90, qty: 3, client_order_id: "b2".into() },
        ];
        let add_pos = position_from_intents(&pair.slug, pair.cat, &pair.cluster, pair.pm_min_tick, &add_legs);
        reserve_exposure(&mut exp, &add_pos, 0.95);
        pending.insert(slug.clone());
        assert_eq!(exp.open_positions, 2, "base + the in-flight add reserved");
        let out = SubmitOutcome { slug: slug.clone(), kind: SubmitKind::Entry, ack: exec::PairAck { a: live_ack("a2"), b: Err(exec::ExecError::RateLimited) }, position: Some(add_pos), pair: Some(pair.clone()), cost_per: 0.95, entry_net: 0.06, entry_dir: Dir::PK };
        run_apply(&dry_backend(), &positions, &empty_kbooks(), &std::collections::HashMap::new(), &mut exp, &mut pending, &mut flat, &halt, out);
        // the ADD's reservation is released EXACTLY -> exposure is back to the BASE-only amount (intact).
        assert!((exp.total - base_total).abs() < 1e-9, "base exposure intact: only the add's reservation released");
        assert!((exp.per_pair[&slug] - base_pair).abs() < 1e-9, "base per-pair contribution untouched");
        assert_eq!(exp.open_positions, 1, "back to one open position (the base)");
        // the base leg itself is STILL HELD (the failed add never recorded a leg; recovery touched its own leg).
        assert_eq!(positions.lock().unwrap().get(&slug).unwrap().legs.len(), 1, "the held BASE leg is intact");
        assert!(halt.load(Ordering::Relaxed), "the add's unpriceable naked leg engaged the halt backstop");
    }

    /// TEST 8 (design §6.8): a postpone unwinds ALL positions over re-emit cycles, each removed + decremented
    /// individually, and the slug key drops only after the LAST leg flattens. (Models the poll's per-cycle
    /// re-emit: spawn the front, the outcome pops it, repeat.) Three stacked legs here.
    #[test]
    fn test8_postpone_unwinds_all_positions_over_re_emit_cycles() {
        use std::sync::{Arc, Mutex};
        let positions = Arc::new(Mutex::new(std::collections::HashMap::new()));
        let mut exp = Exposure::new();
        let mut pending: std::collections::HashSet<String> = std::collections::HashSet::new();
        let mut flat: std::collections::HashMap<String, FlatKind> = std::collections::HashMap::new();
        let halt = AtomicBool::new(false);
        let pair = wx_pair();
        let slug = seed_legs(&positions, &mut exp, &pair, &[(0.90, 0.03), (0.92, 0.05), (0.95, 0.08)], Dir::PK);
        assert_eq!(positions.lock().unwrap().get(&slug).unwrap().legs.len(), 3);
        assert_eq!(exp.open_positions, 3);
        // three re-emit cycles, each flattens ONE front leg.
        for remaining in (0..3).rev() {
            flat.insert(slug.clone(), FlatKind::Unwind);
            run_apply(&dry_backend(), &positions, &empty_kbooks(), &std::collections::HashMap::new(), &mut exp, &mut pending, &mut flat, &halt, unwind_both_filled(&slug));
            assert_eq!(exp.open_positions, remaining, "one position closed per cycle");
            let still_held = positions.lock().unwrap().contains_key(&slug);
            if remaining > 0 {
                assert!(still_held, "slug kept while {remaining} leg(s) remain");
                assert_eq!(positions.lock().unwrap().get(&slug).unwrap().legs.len() as u32, remaining);
            } else {
                assert!(!still_held, "slug key gone after the last leg flattens (R5)");
            }
        }
        assert!(exp.total.abs() < 1e-9, "all exposure decremented exactly to zero");
        assert!(!halt.load(Ordering::Relaxed), "a clean serial flatten never halts");
    }

    /// TEST 9 (design §6.9): dry-run is honored for an armed-flags add — `apply_outcome` of a both-filled
    /// dry-run entry while a position is already held APPENDS the leg (no network: the DryRunBackend only
    /// simulates), exposure stays reserved, no halt. The arming changes WHICH adds the loop admits; it never
    /// changes the execution backend.
    #[test]
    fn test9_dry_run_honored_for_armed_add() {
        use std::sync::{Arc, Mutex};
        let positions = Arc::new(Mutex::new(std::collections::HashMap::new()));
        let mut exp = Exposure::new();
        let mut pending: std::collections::HashSet<String> = std::collections::HashSet::new();
        let mut flat: std::collections::HashMap<String, FlatKind> = std::collections::HashMap::new();
        let halt = AtomicBool::new(false);
        let pair = wx_pair();
        // one held leg already.
        seed_legs(&positions, &mut exp, &pair, &[(0.90, 0.03)], Dir::PK);
        let reserved_after_one = exp.total;
        // the ADD's both-filled entry outcome (dry-run simulated fills) -> APPEND, keep the reservation.
        let legs = [
            OrderIntent { venue: Venue::Pmus, market: pair.slug.clone(), action: Action::Buy, side: Side::Yes, price_cents: 7, qty: 3, client_order_id: "a2".into() },
            OrderIntent { venue: Venue::Kalshi, market: pair.kalshi.clone(), action: Action::Buy, side: Side::No, price_cents: 90, qty: 3, client_order_id: "b2".into() },
        ];
        let pos = position_from_intents(&pair.slug, pair.cat, &pair.cluster, pair.pm_min_tick, &legs);
        reserve_exposure(&mut exp, &pos, 0.95); // spawn-reserve the add
        pending.insert(pair.slug.clone());
        let reserved_after_two = exp.total;
        let out = SubmitOutcome { slug: pair.slug.clone(), kind: SubmitKind::Entry, ack: exec::PairAck { a: sim_ack("a2"), b: sim_ack("b2") }, position: Some(pos), pair: Some(pair.clone()), cost_per: 0.95, entry_net: 0.06, entry_dir: Dir::PK };
        run_apply(&dry_backend(), &positions, &empty_kbooks(), &std::collections::HashMap::new(), &mut exp, &mut pending, &mut flat, &halt, out);
        assert_eq!(positions.lock().unwrap().get(&pair.slug).unwrap().legs.len(), 2, "the dry-run add appended a 2nd leg");
        assert!((exp.total - reserved_after_two).abs() < 1e-9, "both-filled keeps the add reservation (no release)");
        assert!(reserved_after_two > reserved_after_one, "the add added exposure");
        assert!(!halt.load(Ordering::Relaxed), "a clean simulated add never halts (dry-run safe)");
    }

    /// TEST 10 (design §6.10 + §5.4): SINGLE-POSITION / SAFE-DEFAULT regression. With the SHIPPED defaults
    /// (both flags FALSE, cap 1), `qualifying_add` ALWAYS returns None for a held slug — so the loop's add
    /// path always `continue`s and behavior is byte-identical to the one-position-per-slug bot. (The full
    /// ~136-test baseline staying green is the rest of this regression.)
    #[test]
    fn test10_safe_defaults_admit_zero_adds() {
        let def = crate::config::Config::test_default(); // enable_scale_in=false, enable_reentry=false, cap=1
        assert!(!def.enable_scale_in && !def.enable_reentry && def.max_positions_per_slug == 1, "shipped defaults are safe");
        let held = [held_leg(0.03, Dir::PK)];
        // a hugely-bigger same-direction arb, edge live OR closed -> STILL blocked at the defaults.
        let huge = Edge { net: 0.50, dir: Dir::PK };
        assert_eq!(qualifying_add(&def, &held, &huge, true), None, "defaults: SCALE-IN candidate blocked (flag off + cap 1)");
        assert_eq!(qualifying_add(&def, &held, &huge, false), None, "defaults: RE-ENTRY candidate blocked (flag off + cap 1)");
        // the from_env defaults match (the real shipped config, not just test_default).
        // (env is process-global; we assert the constants the from_env literals use instead of mutating env.)
        assert_eq!(qualifying_add(&add_cfg(false, false, 1), &held, &huge, true), None);
    }

    /// W-1 REGRESSION (the independent review's R4-A repro, ASSERTION FLIPPED to halt==true). An armed
    /// scale-in/re-entry can have an ADD in flight on a held slug when a postponement fires. `spawn_unwind`
    /// marks the slug `flattening` (an UNWIND of the BASE leg `legs[0]`). If the ADD then lands HALF-FILLED,
    /// `recover_naked_leg` USED to see `flattening.contains(slug)` and return true ("already covered") — but
    /// the in-flight UNWIND covers `legs[0]`, NOT the add's freshly-naked leg, so that add leg was abandoned
    /// as a SILENT live unhedged position (the W-1 bug: spawned_recovery=false, halted=false). The fix records
    /// WHICH kind holds the slot; an UNWIND-held slot is NOT "covered" for a recovery -> FAIL CLOSED (halt).
    /// The invariant this guards: a filled leg is NEVER left without EITHER a fired recovery OR a halt.
    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn w1_halffilled_add_while_unwind_in_flight_fails_closed() {
        use std::sync::{Arc, Mutex};
        let positions = Arc::new(Mutex::new(std::collections::HashMap::new()));
        let mut exp = Exposure::new();
        let mut pending: std::collections::HashSet<String> = std::collections::HashSet::new();
        let mut flat: std::collections::HashMap<String, FlatKind> = std::collections::HashMap::new();
        let halt = AtomicBool::new(false);
        let pair = wx_pair();
        // a BASE leg is held; a postpone-unwind of it is already IN FLIGHT (slug marked Unwind, targeting
        // legs[0]). This is exactly the state `spawn_unwind` leaves once it fires the base leg's two SELLs.
        let slug = seed_legs(&positions, &mut exp, &pair, &[(0.90, 0.03)], Dir::PK);
        flat.insert(slug.clone(), FlatKind::Unwind);
        // the in-flight ADD is spawn-reserved on top, then lands HALF-FILLED (leg A live, leg B errored).
        let add_legs = [
            OrderIntent { venue: Venue::Pmus, market: pair.slug.clone(), action: Action::Buy, side: Side::Yes, price_cents: 7, qty: 3, client_order_id: "add-a".into() },
            OrderIntent { venue: Venue::Kalshi, market: pair.kalshi.clone(), action: Action::Buy, side: Side::No, price_cents: 90, qty: 3, client_order_id: "add-b".into() },
        ];
        let add_pos = position_from_intents(&pair.slug, pair.cat, &pair.cluster, pair.pm_min_tick, &add_legs);
        reserve_exposure(&mut exp, &add_pos, 0.95);
        pending.insert(slug.clone());
        // CRUCIAL: the pmus book for the add's filled leg IS priceable (a YES bid), so the add leg COULD be
        // flattened in isolation. The bot must STILL halt — a recovery here would race the unwind's pop of
        // legs[0] through the single per-slug flatten slot, so fail-closed is the only safe outcome.
        let mut pmus_books: std::collections::HashMap<String, book::PmusBook> = std::collections::HashMap::new();
        let mut pb = book::PmusBook::new();
        pb.apply_snapshot(&[(0.06, 500.0)], &[(0.08, 500.0)]);
        pmus_books.insert(slug.clone(), pb);
        let out = SubmitOutcome { slug: slug.clone(), kind: SubmitKind::Entry, ack: exec::PairAck { a: live_ack("add-a"), b: Err(exec::ExecError::RateLimited) }, position: Some(add_pos), pair: Some(pair.clone()), cost_per: 0.95, entry_net: 0.06, entry_dir: Dir::PK };
        let mut rx = run_apply(&dry_backend(), &positions, &empty_kbooks(), &pmus_books, &mut exp, &mut pending, &mut flat, &halt, out);
        // THE FIX: the add's naked leg is NOT silently abandoned — the bot FAIL-CLOSES (halt). (Pre-fix this
        // was halt==false, the abandon.) The add's reservation is still released; the base position + its
        // in-flight unwind slot are untouched (the unwind still owns legs[0]); no SECOND flatten was spawned.
        assert!(halt.load(Ordering::Relaxed), "W-1: a half-filled add racing an unwind must FAIL-CLOSE, never silently abandon");
        assert!((exp.total - 0.90 * 3.0).abs() < 1e-9, "the add's reservation is released; the base reservation is intact");
        assert_eq!(exp.open_positions, 1, "back to the one held BASE position");
        assert_eq!(positions.lock().unwrap().get(&slug).unwrap().legs.len(), 1, "the base leg is intact (the add never recorded a leg)");
        assert_eq!(flat.get(&slug), Some(&FlatKind::Unwind), "the in-flight UNWIND still owns the flatten slot (recovery did NOT clobber it)");
        assert!(rx.try_recv().is_err(), "no recovery flatten was spawned (no competing SELL races the unwind's legs[0] pop)");

        // SAFE BRANCH 1: a slot held by a prior RECOVERY for THIS slug's naked leg DOES short-circuit (the
        // genuine "already covered" — the in-flight recovery flattens this very leg; don't double-fire).
        let mut flat_rec: std::collections::HashMap<String, FlatKind> = std::collections::HashMap::new();
        flat_rec.insert(slug.clone(), FlatKind::Recovery);
        let (tx2, _rx2) = tokio::sync::mpsc::unbounded_channel::<SubmitOutcome>();
        let covered = recover_naked_leg(&dry_backend(), &empty_kbooks(), &pmus_books, &mut flat_rec, &tx2,
            &slug, &exec::PairAck { a: live_ack("x"), b: Err(exec::ExecError::RateLimited) }, Some(&position_from_intents(&pair.slug, pair.cat, &pair.cluster, pair.pm_min_tick, &add_legs)));
        assert!(covered, "a recovery-held slot is genuinely covered -> recover_naked_leg returns true (no double-fire, no halt)");

        // SAFE BRANCH 2 (mirror of the bug, isolated): an UNWIND-held slot returns FALSE so the caller halts.
        let mut flat_unw: std::collections::HashMap<String, FlatKind> = std::collections::HashMap::new();
        flat_unw.insert(slug.clone(), FlatKind::Unwind);
        let (tx3, _rx3) = tokio::sync::mpsc::unbounded_channel::<SubmitOutcome>();
        let covered_unw = recover_naked_leg(&dry_backend(), &empty_kbooks(), &pmus_books, &mut flat_unw, &tx3,
            &slug, &exec::PairAck { a: live_ack("x"), b: Err(exec::ExecError::RateLimited) }, Some(&position_from_intents(&pair.slug, pair.cat, &pair.cluster, pair.pm_min_tick, &add_legs)));
        assert!(!covered_unw, "an unwind-held slot does NOT cover a fresh naked leg -> false -> caller fail-closes");
    }
}
