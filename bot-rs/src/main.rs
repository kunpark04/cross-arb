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
mod bookkeeping;
mod config;
mod discovery;
mod exec;
mod exec_log;
mod flatten;
mod ledger;
mod live;
mod matcher;
mod pair;
mod postpone;
mod pricing;
mod probe;
mod refresh;
mod risk;
mod signal;
mod smoke;
mod types;
mod unwind;
mod venue;

#[cfg(test)]
mod test_support;

use config::{Config, ExecutionMode, VenueEnv};
use exec::{DryRunBackend, ExecutionBackend, LiveBackend};
use live::run_live;
use smoke::smoke;

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

    // `--flatten <venue> <market> <side> <qty>`: SURGICAL one-shot — read ONLY that market's book and fire
    // EXACTLY ONE marketable SELL to close the named leg, then exit. NO discovery, NO loop, NO other orders.
    // Same prod/settle consent gates above; a real order only when EXECUTION_MODE=live (dry-run prints + sends
    // nothing). Used to flatten a live naked leg the bot couldn't auto-recover.
    if let Some(parsed) = flatten::parse_flatten(&std::env::args().collect::<Vec<_>>()) {
        match parsed {
            Ok(req) => flatten::run(&cfg, backend, req).await,
            Err(why) => {
                eprintln!("[flatten] {why}");
                std::process::exit(2);
            }
        }
        return;
    }

    // `--probe-no`: LIVE verification of the Kalshi NO-leg `no_price` write-mapping (audit's top pre-arming
    // risk) — a 1¢ BUY-NO on a fully-empty book. Same consent gates; real order only when EXECUTION_MODE=live.
    if std::env::args().any(|a| a == "--probe-no") {
        probe::verify_kalshi_no_mapping(&cfg, backend).await;
        return;
    }
    // `--probe-order [N]`: LIVE order-path verification + latency probe (1¢ BUY-YES place+cancel). Gated by
    // the SAME prod/settle consent checks above; uses the configured backend (real orders only when
    // EXECUTION_MODE=live). Runs instead of the loop/smoke and exits.
    if let Some(n) = probe_order_iters() {
        probe::run(&cfg, backend, n).await;
        return;
    }

    // STAGE-1 smoke vs STAGE-2 live loop. The smoke runs the real risk+exec spine on synthetic
    // snapshots (no network) and is the path of least resistance: it runs when `--smoke` is passed OR
    // when venue creds aren't available (Claude's sandbox / a fresh checkout). The live loop connects
    // both venue WS streams and runs the match->signal->risk->exec pipeline (decision 0015 gated rails).
    let force_smoke = std::env::args().any(|a| a == "--smoke");
    match (force_smoke, venue::VenueCreds::from_env()) {
        (false, Ok(creds)) => {
            let creds = std::sync::Arc::new(creds);
            // `--duration N`: a BOUNDED verification run — connect WS + populate books + detect arbs for N
            // seconds, then exit GRACEFULLY (flushes output, drops the streams). Without it, the loop runs
            // until the venue streams end / the kill-switch (the normal 24/7 mode).
            match run_duration_secs() {
                Some(secs) => {
                    println!("[startup] BOUNDED run: live loop for {secs}s then exit (verification).\n");
                    let _ = tokio::time::timeout(std::time::Duration::from_secs(secs), run_live(&cfg, backend, creds)).await;
                    println!("\n[live] {secs}s elapsed — verification run complete, exiting.");
                }
                None => run_live(&cfg, backend, creds).await,
            }
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

/// Parse `--probe-order [N]` from argv. `Some(N)` (default 8) when the flag is present, else `None`.
/// N is the max place+cancel iterations for the order-path latency probe (`probe::run`).
fn probe_order_iters() -> Option<usize> {
    let args: Vec<String> = std::env::args().collect();
    let i = args.iter().position(|a| a == "--probe-order")?;
    Some(args.get(i + 1).and_then(|s| s.parse::<usize>().ok()).unwrap_or(8))
}

/// Parse `--duration <secs>` from argv — run the live loop for a BOUNDED time then exit gracefully (a
/// verification run: connect WS, populate books, detect arbs for N seconds). `None` -> run until the streams
/// end / kill-switch (the normal 24/7 mode).
fn run_duration_secs() -> Option<u64> {
    let args: Vec<String> = std::env::args().collect();
    let i = args.iter().position(|a| a == "--duration")?;
    args.get(i + 1).and_then(|s| s.parse::<u64>().ok())
}
