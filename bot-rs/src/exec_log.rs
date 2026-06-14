//! Persistent, append-only EXECUTION LOG — one JSON line per order submit / cancel (LIVE or dry-run), with
//! the RAW venue response, the parsed interpretation, and the round-trip latency. This is the forensic audit
//! trail for the money path: the 2026-06-14 fill-detection bug (the bot misreading a real Kalshi fill because
//! the venue renamed `fill_count` -> `fill_count_fp`) was INVISIBLE until we dumped the raw ack. This makes that
//! raw record PERMANENT for EVERY execution — so the next venue drift (renamed field, async fill, wrong side) is
//! one `grep` away, not one naked position away. Pairs with the standing rule [L32]: pin the parser to reality.
//!
//! Output: a JSONL file at `CROSSARB_EXEC_LOG` (default `executions.jsonl`, relative to the run cwd). Each line
//! carries the parsed `filled`/`venue_order_id` ALONGSIDE the `raw` body, so a parser-vs-reality discrepancy
//! (e.g. `raw` shows `fill_count_fp:"1.00"` but `filled:false`) is visible by eye and `jq`-greppable.
//!
//! Never panics and never blocks the trade: a log open/write failure logs to stderr and is swallowed — the
//! EXECUTION must never fail because its LOG did. Concurrent legs (the two-leg `tokio::join!`) are serialized by
//! a process-global lock so lines never interleave.

use crate::types::OrderIntent;
use std::io::Write;
use std::sync::{Mutex, OnceLock};
use std::time::{SystemTime, UNIX_EPOCH};

fn write_lock() -> &'static Mutex<()> {
    static L: OnceLock<Mutex<()>> = OnceLock::new();
    L.get_or_init(|| Mutex::new(()))
}

fn now_ms() -> u128 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_millis()).unwrap_or(0)
}

fn round1(x: f64) -> f64 {
    (x * 10.0).round() / 10.0
}

/// Append one event as a JSON line (we stamp `ts_ms`). Best-effort + serialized; never panics. A no-op under
/// `cargo test` (`cfg!(test)`) so the dry-run paths exercised by tests never write a file / pollute the tree.
pub fn record(mut entry: serde_json::Value) {
    if cfg!(test) {
        return;
    }
    if let Some(o) = entry.as_object_mut() {
        o.insert("ts_ms".to_string(), serde_json::json!(now_ms()));
    }
    let path = std::env::var("CROSSARB_EXEC_LOG").unwrap_or_else(|_| "executions.jsonl".to_string());
    let line = format!("{entry}\n");
    let _guard = write_lock().lock().unwrap_or_else(|e| e.into_inner());
    match std::fs::OpenOptions::new().create(true).append(true).open(&path) {
        Ok(mut f) => {
            if let Err(e) = f.write_all(line.as_bytes()) {
                eprintln!("[exec_log] WARN write failed ({path}): {e}");
            }
        }
        Err(e) => eprintln!("[exec_log] WARN open failed ({path}): {e}"),
    }
}

/// Standard ORDER-SUBMIT record (LIVE or dry-run). `http`/`raw`/`latency` are the venue response (None/empty/0
/// for dry-run). `filled`/`venue_order_id` are the bot's PARSE of `raw` — logged next to `raw` so a
/// parser-vs-reality mismatch is visible.
#[allow(clippy::too_many_arguments)]
pub fn order_submit(
    intent: &OrderIntent,
    mode: &str,
    http: Option<u16>,
    venue_order_id: &str,
    filled: bool,
    latency_ms: f64,
    raw: &str,
) {
    record(serde_json::json!({
        "event": "submit",
        "mode": mode,
        "venue": format!("{:?}", intent.venue),
        "market": intent.market,
        "action": format!("{:?}", intent.action),
        "side": format!("{:?}", intent.side),
        "price_cents": intent.price_cents,
        "qty": intent.qty,
        "client_order_id": intent.client_order_id,
        "http_status": http,
        "venue_order_id": venue_order_id,
        "filled": filled,
        "latency_ms": round1(latency_ms),
        "raw": raw.chars().take(2000).collect::<String>(),
    }));
}

/// Standard ORDER-CANCEL record. `ok` = the cancel succeeded (2xx, not rate-limited).
pub fn order_cancel(venue: &str, market: &str, venue_order_id: &str, http: Option<u16>, ok: bool, latency_ms: f64, raw: &str) {
    record(serde_json::json!({
        "event": "cancel",
        "mode": "live",
        "venue": venue,
        "market": market,
        "venue_order_id": venue_order_id,
        "http_status": http,
        "ok": ok,
        "latency_ms": round1(latency_ms),
        "raw": raw.chars().take(1000).collect::<String>(),
    }));
}
