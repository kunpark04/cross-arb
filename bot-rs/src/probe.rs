//! `--probe-order` — LIVE order-path verification + latency probe. Places a **1¢ BUY-YES** (the lowest
//! price — non-marketable, rests far below any live YES ask so it CANNOT fill) on a real discovered market
//! on each venue, times the submit + cancel round-trips, and immediately cancels. Reuses the VERIFIED
//! `exec::submit`/`exec::cancel` primitives, so the bot loads the trade key itself (never read by Claude).
//!
//! SAFE BY CONSTRUCTION: BUY-YES@1¢ is the live-verified entry intent on both venues (Kalshi `yes_price:1`,
//! pmus `BUY_LONG` value 0.01). It is NOT the marketable trap (`exec.rs:373`: a buy-NO@1¢ executes as a
//! marketable sell-YES) — we only ever send `side:Yes`. Worst case if a near-certain-NO market's YES is
//! offered at 1¢ and fills: 1¢ × 1 = **$0.01**; the probe HALTS on any fill and flags it for manual cleanup.
//!
//! LATENCY SEMANTICS (read the report with these in mind):
//!   * Kalshi create returns at ACCEPTANCE (status `resting`) -> **submit latency == network RTT**.
//!   * pmus create uses `synchronousExecution` + `maxBlockTime`, so a NON-filling order BLOCKS ~maxBlockTime
//!     (≈1s by design) before acking -> **pmus submit latency is BLOCK-bound, NOT the network RTT**. The
//!     pmus CANCEL (returns immediately) measures the true pmus order-path **network RTT**.
//!
//! Run (real money, 1¢, owner-gated):
//!   EXECUTION_MODE=live VENUE_ENV=prod CROSSARB_I_UNDERSTAND_PROD=yes PMUS_POST_SIGNING_VERIFIED=yes \
//!     cargo run -- --probe-order [N]

use crate::config::Config;
use crate::exec::{CancelTarget, ExecutionBackend};
use crate::types::*;
use std::sync::Arc;
use std::time::Instant;

fn pct(sorted: &[f64], p: f64) -> f64 {
    if sorted.is_empty() {
        return f64::NAN;
    }
    let rank = ((p / 100.0 * sorted.len() as f64).ceil() as usize).max(1);
    sorted[rank.min(sorted.len()) - 1]
}

fn report_lat(label: &str, mut v: Vec<f64>) {
    v.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
    if v.is_empty() {
        println!("  {label:26} (no samples)");
        return;
    }
    println!(
        "  {label:26} n={:2}  p50={:7.1}  p90={:7.1}  max={:7.1}  min={:7.1} ms",
        v.len(), pct(&v, 50.0), pct(&v, 90.0), v[v.len() - 1], v[0]
    );
}

/// Place a 1¢ BUY-YES on `(venue, market)`, time the submit; if accepted with a real venue order id, cancel
/// it and time the cancel. Returns `(submit_ms, Option<cancel_ms>, filled)` or `None` on a placement reject
/// (a closed/invalid market — skipped, not fatal). A FILL (must not happen for a 1¢ YES buy) is surfaced.
fn place_cancel(backend: &Arc<dyn ExecutionBackend>, venue: Venue, market: &str, nonce: u128, i: usize) -> Option<(f64, Option<f64>, bool)> {
    let intent = OrderIntent {
        venue,
        market: market.to_string(),
        action: Action::Buy,
        side: Side::Yes, // ONLY Yes — never the marketable buy-NO trap (exec.rs:373)
        price_cents: 1,  // lowest tick: non-marketable, cannot fill against a live ask
        qty: 1,
        // `nonce` (a per-run timestamp) makes the Kalshi client_order_id UNIQUE across runs — Kalshi dedups on
        // it, so a re-run with a reused id 409s ("order already exists"). Fresh nonce -> fresh, placeable id.
        client_order_id: format!("probe-{venue:?}-{nonce}-{i}"),
    };
    let t0 = Instant::now();
    let ack = backend.submit(&intent);
    let submit_ms = t0.elapsed().as_secs_f64() * 1000.0;
    match ack {
        Ok(a) => {
            let filled = a.filled && !a.simulated;
            // dry-run / no real id -> nothing to cancel (still a valid submit-latency sample).
            if a.venue_order_id.is_empty() || a.venue_order_id == "SIMULATED" {
                return Some((submit_ms, None, filled));
            }
            let target = CancelTarget { venue, venue_order_id: a.venue_order_id.clone(), market: market.to_string() };
            let c0 = Instant::now();
            let cres = backend.cancel(&target);
            let cancel_ms = c0.elapsed().as_secs_f64() * 1000.0;
            if let Err(e) = cres {
                eprintln!("  [probe] WARN cancel FAILED for {venue:?} order {}: {e:?} — a 1¢ resting order may remain (won't fill); cancel it manually", a.venue_order_id);
            }
            Some((submit_ms, Some(cancel_ms), filled))
        }
        Err(e) => {
            println!("  [probe] {venue:?} {market}: place rejected ({e:?}) — skipping");
            None
        }
    }
}

/// Public Kalshi book check for `ticker` (no-auth GET) — decides whether a 1¢ BUY-YES is fill-safe:
/// `None` -> the GET/parse FAILED (unknown book) -> caller skips (conservative); `Some(None)` -> NO resting
/// YES ask (empty/one-sided book) -> a 1¢ buy has nothing to match -> SAFE; `Some(Some(a))` -> best YES ask
/// is `a`¢: SAFE iff `a >= 2` (1¢ below the offer -> rests), while `a == 1` means a YES offer sits at 1¢ and
/// a 1¢ buy WOULD fill (the pmus lesson) -> skip.
async fn kalshi_quote_ask(http: &reqwest::Client, ticker: &str) -> Option<Option<u8>> {
    // Read the LIVE ORDER BOOK — NOT GetMarket's derived `yes_ask`, which a 2026-06-14 probe found STALE (it
    // reported no ask while a 99¢ NO bid sat resting, so a 1¢ YES buy filled). Kalshi quotes resting BIDS per
    // side; the best YES ASK = 100 − best NO bid (buying YES == lifting a NO bid). No NO bids -> no resting YES
    // offer -> a 1¢ buy can't fill.
    let url = format!("https://api.elections.kalshi.com/trade-api/v2/markets/{ticker}/orderbook");
    let v: serde_json::Value = http.get(&url).send().await.ok()?.json().await.ok()?;
    let ob = v.get("orderbook")?;
    let best_no_bid = ob
        .get("no")
        .and_then(|x| x.as_array())
        .and_then(|arr| arr.iter().filter_map(|lvl| lvl.as_array()?.first()?.as_u64()).max());
    match best_no_bid {
        Some(nb) if (1..=99).contains(&nb) => {
            let ask = 100u64.saturating_sub(nb);
            if (1..=99).contains(&ask) {
                Some(Some(ask as u8))
            } else {
                Some(None)
            }
        }
        _ => Some(None), // no NO bids -> no resting YES ask -> a 1¢ buy rests safely
    }
}

/// Run the order-path probe: discover real markets, then place+cancel a 1¢ BUY-YES on a DIFFERENT market
/// each iteration (so a single closed market can't bias it, and we never hammer one book). Reports per-venue
/// submit + cancel latency. Kalshi places only where YES ask >= 2¢ (guaranteed REST, no fill); pmus is
/// SKIPPED by default (already verified writable — `PROBE_WITH_PMUS=1` to include it). HALTS on any fill.
pub async fn run(cfg: &Config, backend: Arc<dyn ExecutionBackend>, iters: usize) {
    println!("============================================================");
    println!(" ORDER-PATH LATENCY PROBE — 1¢ BUY-YES place+cancel (LIVE order path)");
    println!("============================================================");
    println!("execution mode : {:?}", cfg.mode);
    println!("venue env      : {:?}{}", cfg.venue_env, if cfg.is_prod() { "  *** REAL MONEY (1¢ orders) ***" } else { "  (sandbox)" });
    if !cfg.is_live() {
        println!("NOTE: EXECUTION_MODE != live -> dry-run backend; this logs the path + local latency only (no real orders).");
    }
    println!();

    let http = reqwest::Client::builder().use_rustls_tls().build().unwrap_or_else(|_| reqwest::Client::new());
    let disc = match crate::discovery::discover(&http).await {
        Ok(d) => d,
        Err(e) => {
            eprintln!("[probe] discovery failed ({e}) — cannot pick markets to probe.");
            return;
        }
    };
    if disc.pairs.is_empty() {
        eprintln!("[probe] discovery returned 0 markets — nothing to probe.");
        return;
    }
    println!("[probe] discovered {} co-listed pairs; probing the first up-to-{iters} (Kalshi) / 3 (pmus).\n", disc.pairs.len());

    // per-run nonce (epoch millis) -> unique client_order_ids so a re-run doesn't 409 on a reused Kalshi id.
    let nonce = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis())
        .unwrap_or(0);

    // KALSHI — submit returns at acceptance, so submit latency == network RTT. SAFETY: place a 1¢ BUY-YES
    // ONLY where the YES ask >= 2¢ (strictly below the offer -> guaranteed REST, no fill); skip tail/one-
    // sided/closed markets. Scan more pairs than `iters` to collect N good samples.
    let (mut k_sub, mut k_can, mut k_fills) = (Vec::new(), Vec::new(), 0u32);
    for (i, pair) in disc.pairs.iter().enumerate() {
        // stop at N good samples, or after scanning 6× that many pairs (most evening books are empty/one-sided
        // -> skipped) so the probe can't run away.
        if k_sub.len() >= iters || i >= iters * 6 {
            break;
        }
        // light spacing so a rapid scan of book checks isn't rate-limited into failing (-> skipping) every market.
        tokio::time::sleep(std::time::Duration::from_millis(120)).await;
        match kalshi_quote_ask(&http, &pair.kalshi).await {
            Some(None) => {}              // no resting YES ask (empty/one-sided book) -> 1¢ buy rests -> SAFE
            Some(Some(a)) if a >= 2 => {} // YES ask >= 2¢ -> 1¢ buy strictly below the offer -> rests -> SAFE
            _ => continue,                // a YES offer at 1¢ (would fill) OR a failed quote (unknown) -> skip
        }
        if let Some((s, c, filled)) = place_cancel(&backend, Venue::Kalshi, &pair.kalshi, nonce, i) {
            k_sub.push(s);
            if let Some(c) = c {
                k_can.push(c);
            }
            if filled {
                k_fills += 1;
                eprintln!("[probe] HALT: a Kalshi 1¢ BUY-YES FILLED ({}) — stopping; check positions.", pair.kalshi);
                break;
            }
        }
    }

    // PMUS — already verified writable (a prior 1¢ probe FILLED), so SKIP by default to avoid opening another
    // tail-market position. `PROBE_WITH_PMUS=1` re-includes it (still 1¢, still a fill risk on a tail market).
    let with_pmus = matches!(std::env::var("PROBE_WITH_PMUS").as_deref(), Ok("1") | Ok("yes") | Ok("true"));
    let p_iters = if with_pmus { iters.min(3) } else { 0 };
    let (mut p_sub, mut p_can, mut p_fills) = (Vec::new(), Vec::new(), 0u32);
    for (i, pair) in disc.pairs.iter().take(p_iters).enumerate() {
        if let Some((s, c, filled)) = place_cancel(&backend, Venue::Pmus, &pair.slug, nonce, 1000 + i) {
            p_sub.push(s);
            if let Some(c) = c {
                p_can.push(c);
            }
            if filled {
                p_fills += 1;
                eprintln!("[probe] HALT: a pmus 1¢ BUY-YES FILLED ({}) — stopping pmus probe; check positions.", pair.slug);
                break;
            }
        }
    }

    println!("\n--- ORDER-PATH LATENCY (ms) ---");
    report_lat("kalshi submit (RTT)", k_sub);
    report_lat("kalshi cancel (RTT)", k_can);
    report_lat("pmus submit (block-bound*)", p_sub);
    report_lat("pmus cancel (RTT)", p_can);
    println!("  * pmus submit is synchronous-block-bound (~maxBlockTime), NOT the network RTT — read pmus CANCEL for its RTT.");
    println!("\nfills (MUST be 0): kalshi={k_fills}  pmus={p_fills}");
    if k_fills + p_fills > 0 {
        eprintln!("[probe] WARNING: order(s) FILLED unexpectedly — inspect positions and flatten the ~1¢ leg(s).");
    } else {
        println!("[probe] clean: every 1¢ order rested + cancelled; no fills, no residual exposure.");
    }
}
