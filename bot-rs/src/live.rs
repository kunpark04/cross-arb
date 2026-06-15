use crate::bookkeeping::{apply_outcome, qualifying_add, reserve_exposure, spawn_submit, spawn_unwind};
use crate::config::Config;
use crate::exec::ExecutionBackend;
use crate::pair::{lock, FlatKind, LivePair, PairState, SubmitKind, SubmitOutcome};
use crate::pricing::{affordable, apply_second_leg_markup, build_legs, position_from_intents, realized_edge_clears_floor};
use crate::refresh::{refresh_loop, report_coverage};
use crate::risk::{evaluate, reject_label, Exposure};
use crate::types::*;
use crate::{book, discovery, exec_log, postpone, signal, venue};

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
pub(crate) async fn run_live(cfg: &Config, backend: std::sync::Arc<dyn ExecutionBackend>, creds: std::sync::Arc<venue::VenueCreds>) {
    use std::collections::{HashMap, HashSet};
    use std::sync::atomic::{AtomicBool, Ordering};
    use std::sync::{Arc, Mutex};

    let http = reqwest::Client::builder().use_rustls_tls().build().unwrap_or_else(|_| reqwest::Client::new());

    // Execution liveness, computed once from the backend label (DryRunBackend -> "dry-run"). Routes the exec
    // log to the LIVE file vs a separate `.dryrun` file so a dry-run never pollutes the real-money audit
    // trail (2026-06-15). Used for `book_snapshot` here; `apply_outcome`/`fire_outcome` derive it from the
    // same `backend.label()`.
    let live = backend.label() != "dry-run";

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
    // MONOTONIC per-slug entry-coid index (scale-in dedup fix): PEEKED at fire to build `xarb-{slug}-{idx}-{tag}`,
    // ADVANCED only on a both-filled commit (in apply_outcome), and NEVER reset — so an unwound-then-re-added slug
    // never reuses an index whose Kalshi coid could still be live (which would `409 already exists`). See the
    // advance site + the never-reset rationale in apply_outcome.
    let mut next_pos_index: HashMap<String, u32> = HashMap::new();

    // PER-SLUG ENTRY COOLDOWN (2026-06-15 incident): a slug stamps `Instant::now()` the moment an entry FIRES
    // and again on EVERY entry OUTCOME; a fresh entry on a slug is refused while `elapsed < entry_cooldown_s`.
    // This stops the churn where one slug fired the same arb repeatedly within seconds (FOK now makes a miss
    // terminal, but the cooldown is the belt to that suspenders). OFF (entry_cooldown_s=0) in tests + dry-run-
    // by-default behavior is unchanged. Pruned each heartbeat so the map can't grow unbounded.
    let mut cooldown: HashMap<String, std::time::Instant> = HashMap::new();

    // `CROSSARB_MAX_ENTRIES=N`: stop opening NEW entries after N have fired this run (recovery/unwind still run;
    // the held position settles normally). For a SAFE first live run set it to 1 -> the bot fires EXACTLY ONE
    // round-trip then holds, so an unattended/overnight arming yields a single reviewable trade. Absent = unlimited.
    let max_entries: Option<u32> = std::env::var("CROSSARB_MAX_ENTRIES").ok().and_then(|s| s.parse::<u32>().ok());
    let mut entries_fired: u32 = 0;

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

    // LATENCY of EVERYTHING (owner ask 2026-06-14): time the in-loop COMPUTE per venue frame — book apply +
    // freshness gate + Quote build + signal/depth + risk evaluate + (rare) build_legs/spawn. This was the one
    // pipeline stage we had only ASSUMED was ~0; now it's MEASURED. `proc_t0` is stamped when a venue frame
    // falls through the select and READ at the next loop-top, so every `continue` exit path is captured. The
    // order RTT (the dominant, network-bound stage) is already logged per leg in exec_log; the heartbeat
    // reports p50/p99/max of the per-frame compute (µ-seconds) over each 20s window, then clears.
    let mut proc_t0: Option<std::time::Instant> = None;
    let mut frame_lat_us: Vec<f64> = Vec::with_capacity(32768);
    // INSTRUMENTATION (0020 follow-up): per-20s-window count of which risk gate is BINDING (each `evaluate`
    // outcome → its label, plus "approved"), so the owner can read whether the recovery-cost / edge / velocity
    // floors are too strict. Logged + cleared each heartbeat.
    let mut gate_outcomes: HashMap<&'static str, u64> = HashMap::new();

    loop {
        // LATENCY: record the PREVIOUS venue-frame's full in-loop compute time (stamped at frame-receipt
        // below, read HERE so every `continue` exit path is timed). `None` after a heartbeat/unwind/outcome
        // tick (those arms `continue` without stamping), so only real frame processing enters the distribution.
        if let Some(t) = proc_t0.take() {
            frame_lat_us.push(t.elapsed().as_secs_f64() * 1e6);
        }
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
                    // re-stamp the per-slug cooldown on EVERY entry resolution (Some(slug) iff Entry) so a
                    // settled/aborted/recovered entry extends the window past a churn burst (2026-06-15).
                    if let Some(s) = apply_outcome(&backend, &positions, &kalshi_books, &pmus_books, &mut exposure, &mut pending_entries, &mut flattening, &mut next_pos_index, &outcome_tx, &halt, out) {
                        cooldown.insert(s, std::time::Instant::now());
                    }
                }
                continue;
            }
            _ = heartbeat.tick() => {
                let (kb, pmb) = (lock(&kalshi_books).len(), pmus_books.len());
                let pn = lock(&pairs).by_slug.len();
                // LATENCY: p50/p99/max (µs) of the per-frame in-loop compute over this 20s window, then clear.
                let (p50, p99, mx, n) = pct_summary(&mut frame_lat_us);
                println!("[live] heartbeat: {pn} pairs, {kb} kalshi books, {pmb} pmus books, {events} frames, k_fresh={}, paused={}, deployed=${:.2}, open={} | frame-compute us p50={p50:.1} p99={p99:.1} max={mx:.1} (n={n})",
                         k_fresh.len(), exposure.stream_paused, exposure.total, exposure.open_positions);
                // PRUNE expired per-slug cooldowns so the map can't grow unbounded (keep entries only while
                // they could still gate: 2× the cooldown, min 60s, well past `entry_cooldown_s`).
                cooldown.retain(|_, t| t.elapsed().as_secs() < cfg.entry_cooldown_s.saturating_mul(2).max(60));
                // GATE OUTCOMES this window — which gate is binding (is the 0020 recovery-cost gate too strict?).
                if !gate_outcomes.is_empty() {
                    let mut go: Vec<(&&'static str, &u64)> = gate_outcomes.iter().collect();
                    go.sort_by(|a, b| b.1.cmp(a.1));
                    let s = go.iter().map(|(k, v)| format!("{k}={v}")).collect::<Vec<_>>().join(" ");
                    println!("[live] gate outcomes (20s): {s}");
                    gate_outcomes.clear();
                }
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
        proc_t0 = Some(std::time::Instant::now()); // LATENCY: start timing THIS frame's in-loop compute
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
        // Checked field-by-field (team-A always; team-B iff present) so the per-frame path allocates no
        // `kalshi_tickers()` Vec — equivalent to "all tickers fresh".
        let all_fresh = k_fresh.contains(&pair.kalshi)
            && pair.kalshi_b.as_ref().is_none_or(|b| k_fresh.contains(b));
        if !all_fresh {
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
            // ENTRY COOLDOWN — FRESH ENTRIES ONLY (`held_legs.is_empty()`): the churn this stops was the same
            // FRESH arb re-firing. A held slug is an ADD, gated below by `qualifying_add` (+ the per-slug count
            // cap), so it must NOT be cooldown-blocked — a scale-in targets a ~1s line-lag dislocation that a
            // 30s cooldown would always miss. (At the safe default both add flags are off, so this is moot.)
            || (held_legs.is_empty() && cfg.entry_cooldown_s > 0 && cooldown.get(&slug).is_some_and(|t| t.elapsed().as_secs() < cfg.entry_cooldown_s))
        {
            continue;
        }
        // NB: CROSSARB_MAX_ENTRIES is checked AFTER `evaluate` (in the Ok arm) — not here — so the per-gate
        // rejection counter keeps tallying the FULL run, not just until the fire cap is hit. Firing still
        // stops at the cap; only the measurement continues.

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

        match evaluate(cfg, &quote, &edge, &exposure, affordable(cfg, &edge, &exposure)) {
            // INSTRUMENTATION (0020 follow-up): tally the BINDING gate so the owner can read whether the
            // recovery-cost / edge / velocity floors are too strict (logged + cleared each heartbeat).
            Err(reject) => {
                *gate_outcomes.entry(reject_label(&reject)).or_insert(0) += 1;
            }
            Ok(a) => {
                *gate_outcomes.entry("approved").or_insert(0) += 1;
                // CROSSARB_MAX_ENTRIES gates the FIRE (not the count): once the cap is hit, keep measuring
                // ("approved_capped" = the opportunity rate = arbs we left on the table) but open no more.
                if max_entries.is_some_and(|m| entries_fired >= m) {
                    *gate_outcomes.entry("approved_capped").or_insert(0) += 1;
                    continue;
                }
                // Build BOTH legs with venue-native market ids + per-leg LIMIT prices from the BOOKS (never
                // derived from the pair edge — that was a self-review CRITICAL). A missing book price (a
                // one-sided book) yields no legs -> skip rather than fire a naked leg. PEEK the MONOTONIC per-slug
                // index (NOT `held_legs.len()`, which reuses an index after an unwind's front-removal -> dedup-409
                // -> a fresh naked pmus leg + halt). It makes the entry coid unique per position so a
                // scale-in/re-entry add does NOT dedup-collide with a held coid, stays stable across a within-fire
                // retry (it advances only on a both-filled lock, in apply_outcome), and NEVER reuses a removed index.
                let pos_index = *next_pos_index.get(&pair.slug).unwrap_or(&0);
                let Some(mut legs) = build_legs(&pair, &quote, edge.dir, a.size, pos_index) else { continue };
                // W6: re-validate the edge from the ROUNDED leg prices (each leg rounds to a whole cent
                // independently, eroding up to +1c of cost). Skip the fire if the realized net fell under the
                // floor or the pair would cost >= 100c — the gated edge and the booked edge must agree.
                if !realized_edge_clears_floor(cfg, &legs) {
                    continue;
                }
                // 0020 follow-up: under pmus-first the Kalshi leg fires SECOND (after the pmus block) and a
                // passive limit MISSES when the price ticked during the wait (most edges are sub-second). Pay
                // up to the edge SURPLUS (capped, never below the floor) so it still locks; it re-checks the floor.
                let markup_c = if cfg.aggressive_second_leg { apply_second_leg_markup(cfg, &mut legs) } else { 0 };
                // Record the velocity metric on the live order path (the owner calibrates MIN_EDGE_RATE_CPD
                // against this accruing distribution): every fired ENTRY logs its edge + edge_rate (¢/$-day). An
                // ADD additionally logs its scale-in|re-entry tag + the base vs add net so an armed add is
                // auditable (design §2). `add_tag` is None for a fresh entry (the common case).
                // LATENCY (per-fire): frame-receipt -> this fire = the in-loop compute that produced the order. The
                // order RTT that follows is logged per leg in exec_log; decision + RTT = the full fire chain measured.
                let decision_us = proc_t0.map(|t| t.elapsed().as_secs_f64() * 1e6).unwrap_or(0.0);
                if let Some(tag) = add_tag {
                    let base_net = held_legs.iter().map(|l| l.entry_net).fold(0.0_f64, f64::max);
                    println!(
                        "[live] ADD({tag}) {slug}  base_net={:.1}c  add_net={:.1}c  size={}  @ {:.2}c/$-day  dir={:?}  decision={decision_us:.0}us  markup={markup_c}c",
                        base_net * 100.0, edge.net * 100.0, a.size, a.edge_rate, edge.dir
                    );
                } else {
                    println!(
                        "[live] ENTRY {slug}  size={}  edge={:.1}c @ {:.2}c/$-day  dir={:?}  decision={decision_us:.0}us  markup={markup_c}c",
                        a.size, edge.net * 100.0, a.edge_rate, edge.dir
                    );
                }
                // ENTRY book snapshot (0020 follow-up): record the books we fired against so a later naked-leg
                // recovery can be decomposed into spread-vs-move (the FILL prices are already in exec_log).
                exec_log::book_snapshot("entry", &slug, quote.pm.yes_bid, quote.pm.yes_ask, quote.k.yes_bid, quote.k.yes_ask, quote.depth.c2, live);
                // RESERVE exposure NOW (on spawn), so concurrent in-flight entries can't over-allocate; the
                // outcome arm keeps the reservation on a both-filled fill (appends the leg) or releases it.
                let pos = position_from_intents(&slug, pair.cat, &pair.cluster, pair.pm_min_tick, &legs);
                reserve_exposure(&mut exposure, &pos, a.cost_per);
                pending_entries.insert(slug.clone());
                // deref-clone the shared `Arc<LivePair>` into the owned `LivePair` the outcome carries (only on
                // the rare approved-fire path, never per frame); the poll reads its league/date/abbrevs later.
                spawn_submit(&backend, &outcome_tx, SubmitKind::Entry, slug.clone(), legs, Some(pos), Some((*pair).clone()), a.cost_per, edge.net, edge.dir);
                cooldown.insert(slug.clone(), std::time::Instant::now()); // 2026-06-15: start the per-slug entry cooldown at the fire
                entries_fired += 1;
                if max_entries == Some(entries_fired) {
                    println!("[live] CROSSARB_MAX_ENTRIES={entries_fired} reached — holding this position to settlement; NO new entries will open (recovery/unwind stay active).");
                }
            }
        }
    }
    if halt.load(Ordering::Relaxed) {
        println!("[live] HALTED (kill-switch engaged at runtime) — loop exiting; no further entries.");
    } else {
        println!("[live] both venue streams ended — loop exiting.");
    }
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

/// p50/p99/max (µs) of the accumulated per-frame compute latencies, then CLEAR the buffer (so each call
/// reports one 20s heartbeat window). Returns (p50, p99, max, n); empty -> all-zeros. Sorts in place — cheap
/// at the ~10^4 samples/window the loop produces, and it's off the per-frame hot path (heartbeat-only).
fn pct_summary(samples: &mut Vec<f64>) -> (f64, f64, f64, usize) {
    let n = samples.len();
    if n == 0 {
        return (0.0, 0.0, 0.0, 0);
    }
    samples.sort_unstable_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
    let p50 = samples[n / 2];
    let p99 = samples[(n * 99 / 100).min(n - 1)];
    let max = samples[n - 1];
    samples.clear();
    (p50, p99, max, n)
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn pct_summary_basic_and_clears() {
        // 1..=100 -> p50 at index 50 (value 51), p99 at index 99 (value 100), max 100.
        let mut s: Vec<f64> = (1..=100).map(|i| i as f64).collect();
        let (p50, p99, max, n) = pct_summary(&mut s);
        assert_eq!(n, 100);
        assert_eq!(p50, 51.0);
        assert_eq!(p99, 100.0);
        assert_eq!(max, 100.0);
        assert!(s.is_empty(), "buffer must be cleared so each call is one heartbeat window");
    }

    #[test]
    fn pct_summary_empty_is_zeros() {
        let mut s: Vec<f64> = vec![];
        assert_eq!(pct_summary(&mut s), (0.0, 0.0, 0.0, 0));
    }

    #[test]
    fn pct_summary_single_sample() {
        let mut s = vec![7.5];
        assert_eq!(pct_summary(&mut s), (7.5, 7.5, 7.5, 1));
    }
}
