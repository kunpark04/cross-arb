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

/// Resolve the exec-log file path for the run MODE (2026-06-15: keep dry-run records OUT of the live exec
/// log so a paper run can't pollute the real-money audit trail). LIVE -> `CROSSARB_EXEC_LOG` (default
/// `executions.jsonl`), UNCHANGED. DRY-RUN -> `CROSSARB_EXEC_LOG_DRYRUN` if set, else the live path with
/// `.dryrun` inserted before the extension (`executions.jsonl` -> `executions.dryrun.jsonl`; a no-extension
/// path appends `.dryrun`).
fn log_path(live: bool) -> String {
    let live_path = std::env::var("CROSSARB_EXEC_LOG").unwrap_or_else(|_| "executions.jsonl".to_string());
    if live {
        return live_path;
    }
    if let Ok(p) = std::env::var("CROSSARB_EXEC_LOG_DRYRUN") {
        return p;
    }
    match live_path.rfind('.') {
        // insert `.dryrun` before the extension; `rfind('.')` after the last path separator avoids a dotted dir.
        Some(i) if i > live_path.rfind(['/', '\\']).map_or(0, |s| s + 1) => format!("{}.dryrun{}", &live_path[..i], &live_path[i..]),
        _ => format!("{live_path}.dryrun"),
    }
}

/// Append one event as a JSON line (we stamp `ts_ms`). Best-effort + serialized; never panics. A no-op under
/// `cargo test` (`cfg!(test)`) so the dry-run paths exercised by tests never write a file / pollute the tree.
/// `live` selects the destination file (`log_path`): dry-run records go to a SEPARATE `.dryrun` log.
pub fn record(mut entry: serde_json::Value, live: bool) {
    if cfg!(test) {
        return;
    }
    if let Some(o) = entry.as_object_mut() {
        o.insert("ts_ms".to_string(), serde_json::json!(now_ms()));
    }
    let path = log_path(live);
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
        // pmus FILL bodies (executions[].order.marketMetadata) run several KB; capture enough to keep the
        // fill evidence (cumQuantity / executions[].lastShares) intact for forensics.
        "raw": raw.chars().take(6000).collect::<String>(),
    }), mode == "live"); // dry-run records route to the separate `.dryrun` log (2026-06-15)
}

/// Standard ORDER-CANCEL record. `ok` = the cancel succeeded (2xx, not rate-limited). Only the LIVE/recovery
/// path cancels real orders, so this always logs to the live exec log.
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
    }), true);
}

/// ENTRY / RECOVERY book snapshot (0020 follow-up) — the bid/ask we fired against + the take-side depth, so a
/// later naked-leg recovery decomposes into SPREAD (entry bid↔ask) vs MOVE (entry mid → the recovery fill
/// price already captured in the submit records). `phase` = "entry" | "recovery". `live` routes a dry-run
/// snapshot to the `.dryrun` log (2026-06-15). No-op under `cargo test`.
#[allow(clippy::too_many_arguments)]
pub fn book_snapshot(phase: &str, market: &str, pm_bid: Option<f64>, pm_ask: Option<f64>, k_bid: Option<f64>, k_ask: Option<f64>, depth_c2: u32, live: bool) {
    record(serde_json::json!({
        "event": "book",
        "phase": phase,
        "market": market,
        "pm_bid": pm_bid,
        "pm_ask": pm_ask,
        "k_bid": k_bid,
        "k_ask": k_ask,
        "depth_c2": depth_c2,
    }), live);
}

/// Per-fire OUTCOME (0020 follow-up instrumentation) — one machine-readable record of how an ENTRY resolved:
/// "lock" (both legs filled), "abort_clean" (pmus hedge didn't fill → cancelled, no position), "abort_ambiguous"
/// (hedge err → halt), or "recover" (one leg naked → flattened; `detail` = the naked leg's venue). So a run's
/// fire distribution (lock vs abort vs recover, the second-leg miss rate) is one `jq` away, not a hand-join
/// across the per-leg submit records. `live` routes a dry-run outcome to the `.dryrun` log (2026-06-15).
/// No-op under `cargo test`.
pub fn fire_outcome(slug: &str, result: &str, detail: &str, live: bool) {
    record(serde_json::json!({
        "event": "fire_outcome",
        "market": slug,
        "result": result,
        "detail": detail,
    }), live);
}

#[cfg(test)]
mod tests {
    use super::log_path;

    /// 2026-06-15: the dry-run exec log is a SEPARATE file so a paper run can't pollute the real-money audit
    /// trail. LIVE path is UNCHANGED; dry-run derives `.dryrun` before the extension; `CROSSARB_EXEC_LOG_DRYRUN`
    /// overrides; a no-extension path appends `.dryrun`. Env is process-global, so save/restore both vars and
    /// run all cases in one test (no parallel race).
    #[test]
    fn log_path_separates_dry_run() {
        let prev_live = std::env::var("CROSSARB_EXEC_LOG").ok();
        let prev_dry = std::env::var("CROSSARB_EXEC_LOG_DRYRUN").ok();
        std::env::remove_var("CROSSARB_EXEC_LOG_DRYRUN");

        // default live path UNCHANGED; dry-run inserts `.dryrun` before the extension.
        std::env::set_var("CROSSARB_EXEC_LOG", "executions.jsonl");
        assert_eq!(log_path(true), "executions.jsonl", "live path is unchanged");
        assert_eq!(log_path(false), "executions.dryrun.jsonl", "dry-run derives the .dryrun file");

        // a configured live path keeps its directory; the suffix lands before the last extension.
        std::env::set_var("CROSSARB_EXEC_LOG", "/var/log/cross-arb/exec.jsonl");
        assert_eq!(log_path(false), "/var/log/cross-arb/exec.dryrun.jsonl");

        // a no-extension live path -> append `.dryrun` (no false extension split on a dotted directory).
        std::env::set_var("CROSSARB_EXEC_LOG", "execlog");
        assert_eq!(log_path(false), "execlog.dryrun");
        std::env::set_var("CROSSARB_EXEC_LOG", "/var/log/cross.arb/execlog");
        assert_eq!(log_path(false), "/var/log/cross.arb/execlog.dryrun", "a dotted DIR must not be split");

        // the explicit dry-run override wins outright.
        std::env::set_var("CROSSARB_EXEC_LOG", "executions.jsonl");
        std::env::set_var("CROSSARB_EXEC_LOG_DRYRUN", "/tmp/paper.jsonl");
        assert_eq!(log_path(false), "/tmp/paper.jsonl", "CROSSARB_EXEC_LOG_DRYRUN overrides the derived path");
        assert_eq!(log_path(true), "executions.jsonl", "...but the live path still ignores the dry-run override");

        match prev_live { Some(v) => std::env::set_var("CROSSARB_EXEC_LOG", v), None => std::env::remove_var("CROSSARB_EXEC_LOG") }
        match prev_dry { Some(v) => std::env::set_var("CROSSARB_EXEC_LOG_DRYRUN", v), None => std::env::remove_var("CROSSARB_EXEC_LOG_DRYRUN") }
    }
}
