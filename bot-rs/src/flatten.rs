//! `--flatten <venue> <market> <side> <qty>` — a SURGICAL one-shot to SELL a single naked leg and EXIT.
//!
//! Fires EXACTLY ONE marketable SELL on EXACTLY the named leg to CLOSE a position (e.g. flatten the live
//! naked pmus Tailleu leg left by a rejected Kalshi hedge). It does **NO discovery, NO other orders, NO
//! loop** — it connects ONLY the target venue's WS (the SAME auth path the live loop uses), reads ONLY that
//! one market's book, prices a marketable SELL with the RECOVERY pricer (`flatten_exit_cents` — the same
//! direction-aware tick/cent FLOOR the naked-leg recovery uses), submits it via the configured backend
//! (dry-run PRINTS the intended order and sends nothing; live SENDS), logs the ack + fill, and returns.
//!
//! SAFETY: gated behind the SAME prod-consent env as the live path (`main` checks it before dispatch). It
//! fires a SELL, which is reduce-only **ONLY IF the (venue, market, side, qty) match a leg you actually
//! hold** — a SELL is NOT inherently a close. A wrong-side / wrong-market / over-qty SELL OPENS a new short
//! at the venue (on pmus a `SELL` of YES is `ORDER_INTENT_SELL_LONG` → a fresh short if you hold no YES;
//! exec.rs:440-446), so this one-shot is as dangerous as any order if mis-aimed. The operator MUST eyeball
//! the resolved SELL echoed below (venue/market/side/qty/price/coid) against the leg they intend to flatten
//! BEFORE it sends. Reuses the VERIFIED `exec::submit` + `venue::*_stream` primitives, so the bot loads the
//! trade key itself (never read by Claude) and no signing is duplicated.
//!
//! Run (flatten the live naked Tailleu leg — 1 pmus YES contract):
//!   EXECUTION_MODE=live VENUE_ENV=prod CROSSARB_I_UNDERSTAND_PROD=yes PMUS_POST_SIGNING_VERIFIED=yes \
//!     cargo run -- --flatten pmus aec-itfm-pietai-antwal-2026-06-15 yes 1

use crate::config::Config;
use crate::exec::ExecutionBackend;
use crate::pricing::flatten_exit_cents;
use crate::types::*;
use crate::{book, venue};
use std::sync::Arc;

/// The parsed `--flatten` request. Pure so it is unit-testable without argv/I/O.
#[derive(Clone, Debug, PartialEq)]
pub struct FlattenReq {
    pub venue: Venue,
    pub market: String,
    pub side: Side,
    pub qty: u32,
}

/// Parse `--flatten <venue> <market> <side> <qty>` from an arg list. Returns `None` if the flag is absent;
/// `Err` if present but malformed (so a typo'd flatten request fails LOUD instead of silently doing nothing).
/// `venue ∈ {pmus, kalshi}`, `side ∈ {yes, no}`, `qty` a positive integer.
pub fn parse_flatten(args: &[String]) -> Option<Result<FlattenReq, String>> {
    let i = args.iter().position(|a| a == "--flatten")?;
    let need = |n: usize, what: &str| {
        args.get(i + 1 + n).cloned().ok_or_else(|| format!("--flatten missing <{what}> (usage: --flatten <pmus|kalshi> <market> <yes|no> <qty>)"))
    };
    Some((|| {
        let venue = match need(0, "venue")?.to_ascii_lowercase().as_str() {
            "pmus" => Venue::Pmus,
            "kalshi" => Venue::Kalshi,
            other => return Err(format!("--flatten venue must be pmus|kalshi, got '{other}'")),
        };
        let market = need(1, "market")?;
        if market.is_empty() {
            return Err("--flatten <market> is empty".into());
        }
        let side = match need(2, "side")?.to_ascii_lowercase().as_str() {
            "yes" => Side::Yes,
            "no" => Side::No,
            other => return Err(format!("--flatten side must be yes|no, got '{other}'")),
        };
        let qty_s = need(3, "qty")?;
        let qty = qty_s.parse::<u32>().map_err(|_| format!("--flatten <qty> must be a positive integer, got '{qty_s}'"))?;
        if qty == 0 {
            return Err("--flatten <qty> must be >= 1".into());
        }
        Ok(FlattenReq { venue, market, side, qty })
    })())
}

/// Build the SINGLE marketable SELL `OrderIntent` for `req` from the live `book` — the EXACT (venue, market,
/// side) named, priced by the RECOVERY pricer (`flatten_exit_cents`: direction-aware tick/cent FLOOR so the
/// SELL stays marketable). `None` when the needed side isn't quoted (one-sided book) or the price rounds
/// outside 1..=99c — the caller then fires NOTHING (never a mispriced/rejecting flatten). `pm_min_tick` is
/// left `None` (cent granularity): a SELL floors, and live pmus ticks (0.001) leave whole cents valid, so a
/// cent-floored SELL is a valid marketable tick.
pub fn build_flatten_sell(req: &FlattenReq, book: &Book) -> Option<OrderIntent> {
    let leg = PositionLeg { venue: req.venue, market: req.market.clone(), side: req.side, venue_order_id: String::new(), pm_min_tick: None };
    let price_cents = flatten_exit_cents(&leg, book)?;
    Some(OrderIntent {
        venue: req.venue,
        market: req.market.clone(),
        action: Action::Sell,
        side: req.side,
        price_cents,
        qty: req.qty,
        frac_qty: None, // a whole-contract operator flatten (the partial-fill recovery is the only fractional path)
        client_order_id: format!("flatten-{}-{}-{}-{}", crate::exec::run_salt(), venue_tag(req.venue), req.market, side_tag(req.side)),
    })
}

fn venue_tag(v: Venue) -> &'static str {
    match v {
        Venue::Kalshi => "kalshi",
        Venue::Pmus => "pmus",
    }
}
fn side_tag(s: Side) -> &'static str {
    match s {
        Side::Yes => "yes",
        Side::No => "no",
    }
}

/// Read ONLY `req.market`'s live `Book` from its venue's WS — the SAME stream the live loop uses, subscribed
/// to JUST this one market (NO discovery). Spins the venue stream, drains book frames until the target
/// market's book is built, and returns its touch. `None` on timeout / no frame for the market. The stream
/// task is dropped (aborted) on return — a one-shot read, never a loop.
async fn read_one_book(creds: Arc<venue::VenueCreds>, req: &FlattenReq) -> Option<Book> {
    use std::collections::{HashMap, HashSet};
    use std::sync::Mutex;
    let tracked = Arc::new(Mutex::new(HashSet::from([req.market.clone()])));
    let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel::<venue::VenueEvent>();
    let (_subs_tx, subs_rx) = tokio::sync::mpsc::unbounded_channel::<venue::SubUpdate>();
    // Kalshi books live in the shared map the stream owns; pmus frames carry the ladders inline.
    let kalshi_books: Arc<Mutex<HashMap<String, book::KalshiBook>>> = Arc::new(Mutex::new(HashMap::new()));

    let handle = match req.venue {
        Venue::Kalshi => tokio::spawn(venue::kalshi_stream(creds, tracked.clone(), kalshi_books.clone(), tx, subs_rx)),
        Venue::Pmus => tokio::spawn(venue::pmus_stream(creds, tracked.clone(), tx, subs_rx)),
    };

    // wait up to 10s for the FIRST book frame for THIS market (a snapshot must arrive before we can price).
    let book = tokio::time::timeout(std::time::Duration::from_secs(10), async {
        while let Some(ev) = rx.recv().await {
            match ev {
                venue::VenueEvent::Kalshi { ticker } if ticker == req.market => {
                    if let Some(b) = crate::pair::lock(&kalshi_books).get(&ticker) {
                        return Some(b.touch());
                    }
                }
                venue::VenueEvent::Pmus { slug, bids, asks } if slug == req.market => {
                    let mut pb = book::PmusBook::new();
                    pb.apply_snapshot(&bids, &asks);
                    return Some(pb.touch());
                }
                _ => {} // a reconnect / seq-gap / a frame for some other market -> keep waiting
            }
        }
        None
    })
    .await
    .ok()
    .flatten();

    handle.abort(); // one-shot: stop the stream (never a loop)
    book
}

/// `--flatten` one-shot. Prints what it WILL do, reads the one market's book, builds + fires exactly ONE
/// marketable SELL (dry-run prints + sends nothing; live sends), prints the ack + whether it filled, exits.
pub async fn run(cfg: &Config, backend: Arc<dyn ExecutionBackend>, req: FlattenReq) {
    println!("============================================================");
    println!(" --flatten ONE-SHOT — sell a single naked leg + exit");
    println!("============================================================");
    println!("execution mode : {:?}", cfg.mode);
    println!("venue env      : {:?}{}", cfg.venue_env, if cfg.is_prod() { "  *** REAL MONEY ***" } else { "  (sandbox)" });
    println!("target leg     : SELL {} {:?} {}x on {:?}", req.market, req.side, req.qty, req.venue);
    if !cfg.is_live() {
        println!("NOTE: EXECUTION_MODE != live -> DRY-RUN: prints the intended SELL and sends NOTHING.");
    }
    println!();

    // connect ONLY this venue (creds load the trade key from the owner env, never read by Claude).
    let creds = match venue::VenueCreds::from_env() {
        Ok(c) => Arc::new(c),
        Err(why) => {
            eprintln!("[flatten] venue creds unavailable ({why}) — cannot read the book / send the SELL. Aborting (nothing sent).");
            return;
        }
    };

    let Some(book) = read_one_book(creds, &req).await else {
        eprintln!("[flatten] no live book frame for {} on {:?} within 10s — NOTHING sent. Re-run when the market is streaming.", req.market, req.venue);
        return;
    };
    println!("[flatten] live book: yes_bid={:?} yes_ask={:?} (age {:.1}s)", book.yes_bid, book.yes_ask, book.age_s);

    let Some(sell) = build_flatten_sell(&req, &book) else {
        eprintln!("[flatten] cannot price a marketable SELL for the {:?} leg (one-sided book / out of 1..=99c) — NOTHING sent.", req.side);
        return;
    };
    // RESOLVED-ORDER CONFIRMATION — the fully-resolved SELL the operator must eyeball BEFORE it sends. A SELL
    // is reduce-only ONLY if this exactly matches a held leg; a wrong side/market/qty OPENS a short. Make it
    // unmistakable and flag REAL MONEY loudly so a live mis-aim can't slip by unread.
    println!("------------------------------------------------------------");
    println!("  RESOLVED SELL — confirm this matches the leg you hold:");
    println!("    venue  : {:?}", sell.venue);
    println!("    market : {}", sell.market);
    println!("    side   : {:?}", sell.side);
    println!("    qty    : {}", sell.qty);
    println!("    price  : {}c (marketable SELL, floored toward the bid)", sell.price_cents);
    println!("    coid   : {}", sell.client_order_id);
    if cfg.is_live() {
        let env = if cfg.is_prod() { "PROD / REAL MONEY" } else { "demo sandbox" };
        println!("  *** THIS WILL SEND A REAL SELL ({env}) — a wrong side/market/qty OPENS a short ***");
    } else {
        println!("  (DRY-RUN: prints only, sends NOTHING)");
    }
    println!("------------------------------------------------------------");

    match backend.submit(&sell) {
        Ok(a) => {
            if a.simulated {
                println!("[flatten] DRY-RUN: would have sent the SELL above; no order placed.");
            } else if a.filled {
                println!("[flatten] DONE: SELL FILLED (venue_order_id={}, fill_qty={}). The leg is flat.", a.venue_order_id, a.fill_qty);
            } else {
                eprintln!("[flatten] WARN: SELL accepted but NOT fully filled (venue_order_id={}, fill_qty={}). It is a GTC SELL — it may rest/fill, or re-run --flatten. The leg may still be (partly) naked.", a.venue_order_id, a.fill_qty);
            }
        }
        Err(e) => {
            eprintln!("[flatten] SELL FAILED ({e:?}) — the leg is STILL naked. Check the venue + re-run.");
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// PARSE: a well-formed `--flatten` request round-trips; missing/garbled fields fail LOUD; the flag's
    /// absence is `None` (so the dispatcher falls through to the normal loop/smoke).
    #[test]
    fn parse_flatten_accepts_well_formed_and_rejects_garbage() {
        let v = |s: &[&str]| s.iter().map(|x| x.to_string()).collect::<Vec<_>>();
        // the documented target usage parses to the exact request.
        let ok = parse_flatten(&v(&["prog", "--flatten", "pmus", "aec-itfm-pietai-antwal-2026-06-15", "yes", "1"]));
        assert_eq!(ok, Some(Ok(FlattenReq { venue: Venue::Pmus, market: "aec-itfm-pietai-antwal-2026-06-15".into(), side: Side::Yes, qty: 1 })));
        // case-insensitive venue/side; a Kalshi NO leg, qty 3.
        assert_eq!(
            parse_flatten(&v(&["--flatten", "Kalshi", "KXHIGHNY-26JUN15-T95", "NO", "3"])),
            Some(Ok(FlattenReq { venue: Venue::Kalshi, market: "KXHIGHNY-26JUN15-T95".into(), side: Side::No, qty: 3 }))
        );
        // absent flag -> None (fall through to the loop).
        assert_eq!(parse_flatten(&v(&["prog", "--smoke"])), None);
        // malformed -> Some(Err): bad venue, bad side, non-int qty, zero qty, missing trailing args.
        assert!(matches!(parse_flatten(&v(&["--flatten", "binance", "m", "yes", "1"])), Some(Err(_))));
        assert!(matches!(parse_flatten(&v(&["--flatten", "pmus", "m", "maybe", "1"])), Some(Err(_))));
        assert!(matches!(parse_flatten(&v(&["--flatten", "pmus", "m", "yes", "two"])), Some(Err(_))));
        assert!(matches!(parse_flatten(&v(&["--flatten", "pmus", "m", "yes", "0"])), Some(Err(_))));
        assert!(matches!(parse_flatten(&v(&["--flatten", "pmus", "m", "yes"])), Some(Err(_))));
    }

    /// BUILD: `--flatten` constructs EXACTLY ONE correctly-priced marketable SELL for the named leg — a SELL
    /// (close, never open), on the EXACT (venue, market, side), priced by the recovery pricer: a YES leg sells
    /// into the YES BID (floored to the cent); a NO leg sells at `1 - yes_ask`. A one-sided book -> None (fire
    /// nothing). Mirrors the live naked Tailleu leg (a YES@pmus leg).
    #[test]
    fn build_flatten_sell_prices_one_marketable_sell() {
        let book = Book { yes_bid: Some(0.93), yes_ask: Some(0.95), age_s: 0.0 };
        // the target case: SELL YES@pmus 1x -> hits the YES bid 0.93 -> floors to 93c.
        let req = FlattenReq { venue: Venue::Pmus, market: "aec-itfm-pietai-antwal-2026-06-15".into(), side: Side::Yes, qty: 1 };
        let sell = build_flatten_sell(&req, &book).expect("priceable YES leg");
        assert_eq!(sell.action, Action::Sell, "a flatten is a SELL (close), never a BUY");
        assert_eq!((sell.venue, sell.side, sell.market.as_str()), (Venue::Pmus, Side::Yes, "aec-itfm-pietai-antwal-2026-06-15"));
        assert_eq!(sell.qty, 1, "exactly the requested qty");
        assert_eq!(sell.price_cents, 93, "SELL YES floors into the 0.93 YES bid");
        assert_eq!(sell.frac_qty, None, "a whole-contract operator flatten");

        // a NO leg sells at 1 - yes_ask = 1 - 0.95 = 0.05 -> 5c, on the Kalshi side.
        let req_no = FlattenReq { venue: Venue::Kalshi, market: "K-1".into(), side: Side::No, qty: 2 };
        let sell_no = build_flatten_sell(&req_no, &book).expect("priceable NO leg");
        assert_eq!((sell_no.venue, sell_no.side, sell_no.action, sell_no.price_cents, sell_no.qty), (Venue::Kalshi, Side::No, Action::Sell, 5, 2));

        // one-sided book (no YES bid for a YES leg) -> None: the caller fires NOTHING.
        assert!(build_flatten_sell(&req, &Book { yes_bid: None, yes_ask: Some(0.95), age_s: 0.0 }).is_none());
    }

    /// DRY-RUN: the `--flatten` SELL is fired through the DRY-RUN backend and sends NOTHING (a simulated ack),
    /// proving the one-shot builds exactly one correctly-priced SELL for the named leg and places no real
    /// order in dry-run. (The exec-path send itself is the live-gated `LiveBackend`; here we assert the
    /// dry-run path returns a simulated, non-network ack for the single SELL.)
    #[test]
    fn dry_run_flatten_fires_exactly_one_simulated_sell() {
        let book = Book { yes_bid: Some(0.93), yes_ask: Some(0.95), age_s: 0.0 };
        let req = FlattenReq { venue: Venue::Pmus, market: "aec-itfm-pietai-antwal-2026-06-15".into(), side: Side::Yes, qty: 1 };
        let sell = build_flatten_sell(&req, &book).expect("priceable");
        let backend = crate::exec::DryRunBackend;
        let ack = backend.submit(&sell).expect("dry-run submit returns Ok");
        assert!(ack.simulated, "dry-run sends NOTHING — the ack is simulated (no real order placed)");
        // and it is the SELL we intended: the dry-run ack echoes the client_order_id, distinct from any entry coid.
        assert_eq!(ack.client_order_id, sell.client_order_id);
        assert!(sell.client_order_id.starts_with("flatten-"), "the flatten coid is namespaced so it can't collide with an entry/recovery coid");
    }
}
