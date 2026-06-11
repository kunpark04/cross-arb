//! Venue WebSocket clients — the live market-data transport (stage 2).
//!
//! Two async streams, ported from the validated Python collector:
//!   - [`kalshi_stream`]: RSA-PSS handshake to `wss://api.elections.kalshi.com/trade-api/ws/v2`,
//!     subscribe `orderbook_delta`, merge `orderbook_snapshot` + `orderbook_delta` frames into a
//!     [`book::KalshiBook`] per ticker, track the single-sid connection seq (a gap cycles the
//!     connection — no replay for missed deltas). Port of `bot/kalshi_book.py` + `kalshi_stream`.
//!   - [`pmus_stream`]: Ed25519 handshake to `wss://api.polymarket.us/v1/ws/markets`, subscribe
//!     `SUBSCRIPTION_TYPE_MARKET_DATA` (≤100 slugs/sub), parse each `marketData` frame into a
//!     [`book::PmusBook`]. Port of `probe_pmus_ws_auth.py` + `pmus_stream`.
//!
//! Both run a SUPERVISED reconnect loop with exponential backoff (1→30 s), mirroring `monitor.py`:
//! a clean close ends the read loop without raising, so without the loop the task would silently
//! return and the venue's book would freeze (a half-dead collector no alarm catches). Each stream
//! pushes a [`VenueEvent`] onto an mpsc channel the main loop consumes.
//!
//! The frame PARSERS are pure and unit-tested against embedded sample JSON (no live connection); the
//! connect/subscribe/reconnect loop is the only part that needs a live venue (the owner's droplet).

use crate::auth;
use crate::book::KalshiBook;
use crate::types::{Side, Venue};
use serde_json::Value;

/// One parsed venue update handed to the main loop: which venue + which market key changed. The book
/// itself lives in the per-venue book maps the streams own; the event just says "this market moved".
#[derive(Clone, Debug, PartialEq)]
pub enum VenueEvent {
    /// A Kalshi ticker's book changed (after snapshot or delta applied).
    Kalshi { ticker: String },
    /// A pmus slug's book changed (a new `marketData` frame arrived).
    Pmus {
        slug: String,
        bids: Vec<(f64, f64)>,
        asks: Vec<(f64, f64)>,
    },
    /// A venue WS reconnected (clean or dropped) — the main loop pauses trading on that book until it
    /// rebuilds (the book-init-phantom / rebuilding-book class; mirrors monitor.py's reconnect marker).
    Reconnect { venue: Venue, clean: bool },
    /// A Kalshi connection-level seq gap — missed deltas have no replay, so the connection cycles.
    SeqGap { seq: u64 },
}

// =====================================================================================================
// PURE FRAME PARSERS  (no I/O; unit-tested against embedded sample frames)
// =====================================================================================================

/// Parse a `[[price_dollars, qty_fp], ...]` ladder (Kalshi snapshot side / pmus bids|offers) where each
/// element is either `["0.67","100.00"]` (Kalshi fixed-point strings) or `{"px":{"value":".."},"qty":".."}`
/// (pmus money objects). Skips malformed levels rather than failing the whole frame. Returns `[(price, qty)]`.
fn parse_levels(v: &Value) -> Vec<(f64, f64)> {
    let arr = match v.as_array() {
        Some(a) => a,
        None => return Vec::new(),
    };
    let mut out = Vec::with_capacity(arr.len());
    for lvl in arr {
        // Kalshi shape: a 2-element array [price, qty] (numbers or numeric strings).
        if let Some(pair) = lvl.as_array() {
            if let (Some(p), Some(q)) = (pair.first().and_then(num), pair.get(1).and_then(num)) {
                out.push((p, q));
            }
            continue;
        }
        // pmus shape: {"px":{"value":".."},"qty":".."}.
        if let Some(p) = lvl.get("px").and_then(|px| px.get("value")).and_then(num) {
            if let Some(q) = lvl.get("qty").and_then(num) {
                out.push((p, q));
            }
        }
    }
    out
}

/// A JSON value that is a number OR a numeric string -> f64 (the venues mix both representations).
fn num(v: &Value) -> Option<f64> {
    match v {
        Value::Number(n) => n.as_f64(),
        Value::String(s) => s.trim().parse::<f64>().ok(),
        _ => None,
    }
}

/// Apply a Kalshi `orderbook_snapshot.msg` to a fresh book. `msg` carries optional `yes_dollars_fp` /
/// `no_dollars_fp` side ladders (a side is absent when empty). Port of `KalshiBook.apply_snapshot`.
pub fn apply_kalshi_snapshot(book: &mut KalshiBook, msg: &Value) {
    let yes = msg.get("yes_dollars_fp").map(parse_levels).unwrap_or_default();
    let no = msg.get("no_dollars_fp").map(parse_levels).unwrap_or_default();
    book.apply_snapshot(&yes, &no);
}

/// Apply one Kalshi `orderbook_delta.msg` (`{side, price_dollars, delta_fp}`) to a book. Returns `false`
/// if the frame is malformed (missing/unparseable field) — the caller leaves the book unchanged.
/// Port of `KalshiBook.apply_delta`.
pub fn apply_kalshi_delta(book: &mut KalshiBook, msg: &Value) -> bool {
    let side = match msg.get("side").and_then(Value::as_str) {
        Some("yes") => Side::Yes,
        Some("no") => Side::No,
        _ => return false,
    };
    let price = match msg.get("price_dollars").and_then(num) {
        Some(p) => p,
        None => return false,
    };
    let delta = match msg.get("delta_fp").and_then(num) {
        Some(d) => d,
        None => return false,
    };
    book.apply_delta(side, price, delta);
    true
}

/// A price-indexed ladder: `[(price_dollars, qty)]` (the shared shape `book.rs` uses).
pub type Ladder = Vec<(f64, f64)>;

/// Extract `(bids, asks)` from a pmus `marketData` object (`{bids:[..], offers:[..]}`). pmus serves the
/// YES book directly: `bids` = YES bids, `offers` = YES asks. Port of the `pm_targets` frame handler.
pub fn parse_pmus_market_data(md: &Value) -> (Ladder, Ladder) {
    let bids = md.get("bids").map(parse_levels).unwrap_or_default();
    let asks = md.get("offers").map(parse_levels).unwrap_or_default();
    (bids, asks)
}

/// Connection-level monotonic-seq guard (port of `kalshi_book.py::SeqTracker`). `check` returns `false`
/// on a gap — the caller cycles the connection (Kalshi gives no replay for missed deltas).
#[derive(Default)]
pub struct SeqTracker {
    expected: Option<u64>,
}

impl SeqTracker {
    pub fn new() -> Self {
        SeqTracker::default()
    }
    pub fn check(&mut self, seq: u64) -> bool {
        let ok = self.expected.is_none_or(|e| seq == e);
        self.expected = Some(seq + 1);
        ok
    }
}

// =====================================================================================================
// SUBSCRIBE ENVELOPES  (the exact verified wire shapes; a typo = silent no-data, so they're tested)
// =====================================================================================================

/// The Kalshi `subscribe` frame for `orderbook_delta` over a set of tickers (verified live,
/// `probe_kalshi_ws.py`). `id=1` is the connect subscribe.
pub fn kalshi_subscribe(id: u64, tickers: &[String]) -> String {
    serde_json::json!({
        "id": id,
        "cmd": "subscribe",
        "params": {"channels": ["orderbook_delta"], "market_tickers": tickers}
    })
    .to_string()
}

/// One pmus `subscribe` shard for `SUBSCRIPTION_TYPE_MARKET_DATA` (camelCase envelope verified live,
/// `probe_pmus_ws_auth.py`). Caller shards the universe into ≤100-slug groups.
pub fn pmus_subscribe(request_id: &str, slugs: &[String]) -> String {
    serde_json::json!({
        "subscribe": {
            "requestId": request_id,
            "subscriptionType": "SUBSCRIPTION_TYPE_MARKET_DATA",
            "marketSlugs": slugs
        }
    })
    .to_string()
}

/// The Kalshi `update_subscription` frame — a NO-GAP in-place add/delete on the live sid (probe-verified
/// `probe_kalshi_ws.py --multisub`: control acks consume a seq slot, existing books stream uninterrupted,
/// only added tickers re-snapshot). Port of `monitor.py::update_sub_cmd`. `action` ∈ {add_markets,
/// delete_markets}; an unknown action returns `None` (the caller must not send a bogus frame).
pub fn kalshi_update_subscription(id: u64, sid: u64, tickers: &[String], action: &str) -> Option<String> {
    if action != "add_markets" && action != "delete_markets" {
        return None;
    }
    Some(
        serde_json::json!({
            "id": id,
            "cmd": "update_subscription",
            "params": {"sids": [sid], "market_tickers": tickers, "action": action}
        })
        .to_string(),
    )
}

/// A live subscribe-set mutation handed to a running stream over its control channel: new keys to
/// subscribe + settled keys to drop. Mirrors `monitor.py`'s heartbeat add/prune. For Kalshi these become
/// `update_subscription` add/delete on the live sid; for pmus, an `add` is a new subscribe shard and a
/// `del` is a local no-op (pmus has no documented unsubscribe — a settled market simply stops streaming
/// and the main loop already ignores untracked slugs).
#[derive(Clone, Debug, Default, PartialEq)]
pub struct SubUpdate {
    pub add: Vec<String>,
    pub del: Vec<String>,
}

// =====================================================================================================
// LIVE ASYNC STREAMS  (connect + subscribe + supervised reconnect; the owner's-droplet path)
// =====================================================================================================

pub const KALSHI_WS: &str = "wss://api.elections.kalshi.com/trade-api/ws/v2";
pub const KALSHI_WS_PATH: &str = "/trade-api/ws/v2";
pub const PMUS_WS: &str = "wss://api.polymarket.us/v1/ws/markets";
pub const PMUS_WS_PATH: &str = "/v1/ws/markets";
const PMUS_SHARD: usize = 100; // ≤100 slugs per pmus subscription (verified limit)

/// Loaded venue credentials + signing keys for the live streams/transport (owner env only).
pub struct VenueCreds {
    pub kalshi_access_key: String,
    pub kalshi_rsa: rsa::RsaPrivateKey,
    pub pmus_access_key: String,
    pub pmus_ed25519: ed25519_dalek::SigningKey,
}

impl VenueCreds {
    /// Load both venues' keys from the env + the external key paths (never embedded). The Kalshi RSA key
    /// is read from `KALSHI_RW_KEY_PATH` at runtime; the pmus Ed25519 seed from `PMUS_SECRET` (base64).
    /// Returns a clear error string if anything is missing — the live loop refuses to start without it.
    pub fn from_env() -> Result<VenueCreds, String> {
        let kalshi_access_key =
            std::env::var("KALSHI_ACCESS_KEY").map_err(|_| "KALSHI_ACCESS_KEY not set".to_string())?;
        let key_path =
            std::env::var("KALSHI_RW_KEY_PATH").map_err(|_| "KALSHI_RW_KEY_PATH not set".to_string())?;
        let pem = std::fs::read_to_string(&key_path)
            .map_err(|e| format!("read Kalshi key {key_path}: {e}"))?;
        let kalshi_rsa = auth::kalshi_key_from_pem(&pem)?;
        let pmus_access_key =
            std::env::var("PMUS_ACCESS_KEY").map_err(|_| "PMUS_ACCESS_KEY not set".to_string())?;
        let pmus_secret =
            std::env::var("PMUS_SECRET").map_err(|_| "PMUS_SECRET not set".to_string())?;
        let pmus_ed25519 = auth::pmus_key_from_secret_b64(&pmus_secret)?;
        Ok(VenueCreds {
            kalshi_access_key,
            kalshi_rsa,
            pmus_access_key,
            pmus_ed25519,
        })
    }
}

fn now_ms() -> u128 {
    // A pre-epoch clock can't sign a valid handshake (the venue rejects the stale timestamp); fail LOUD
    // rather than fall back to `0` and 401 forever with no indication why (a wrong clock is unrecoverable).
    use std::time::{SystemTime, UNIX_EPOCH};
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("system clock before UNIX epoch — cannot sign")
        .as_millis()
}

/// Build the tungstenite handshake request with signed auth headers (`IntoClientRequest`).
fn client_request(
    url: &str,
    headers: [(&'static str, String); 3],
) -> Result<tokio_tungstenite::tungstenite::ClientRequestBuilder, String> {
    use tokio_tungstenite::tungstenite::http::Uri;
    use tokio_tungstenite::tungstenite::ClientRequestBuilder;
    let uri: Uri = url.parse().map_err(|e| format!("bad ws uri {url}: {e}"))?;
    let mut b = ClientRequestBuilder::new(uri);
    for (k, v) in headers {
        b = b.with_header(k, v);
    }
    Ok(b)
}

/// Kalshi WS stream: connect, subscribe the tickers, merge snapshot+delta frames into the shared book
/// map, push a `VenueEvent::Kalshi{ticker}` per updated book. A seq gap pushes `SeqGap` and cycles the
/// connection. Supervised reconnect with backoff. Runs forever (discovery keeps the set non-empty).
/// `books` is shared (the main loop reads it to build Quotes); this task is its only writer.
///
/// `subs` is the live discovery channel: each `SubUpdate` is applied IN PLACE via `update_subscription`
/// (no-gap add/delete on the captured sid — `monitor.py` cadence), so a newly-discovered weather day is
/// subscribed without cycling the connection and a settled market is dropped. `tracked` (shared with the
/// main loop) is the authoritative current ticker set: a reconnect re-subscribes IT (adds made during the
/// last connection survive the reconnect, mirroring monitor.py re-subscribing the CURRENT targets).
#[allow(clippy::await_holding_lock)] // book lock is a std Mutex held only across in-memory merges, never .await
pub async fn kalshi_stream(
    creds: std::sync::Arc<VenueCreds>,
    tracked: std::sync::Arc<std::sync::Mutex<std::collections::HashSet<String>>>,
    books: std::sync::Arc<std::sync::Mutex<std::collections::HashMap<String, KalshiBook>>>,
    tx: tokio::sync::mpsc::UnboundedSender<VenueEvent>,
    mut subs: tokio::sync::mpsc::UnboundedReceiver<SubUpdate>,
) {
    use futures_util::{SinkExt, StreamExt};
    use tokio_tungstenite::tungstenite::Message;
    let mut backoff = 1u64;
    let mut cmd_id = 1u64; // monotonic WS command id (id=1 is the connect subscribe)
    loop {
        let ts = now_ms();
        let hdrs = auth::kalshi_headers(&creds.kalshi_rsa, &creds.kalshi_access_key, ts, "GET", KALSHI_WS_PATH);
        let req = match client_request(KALSHI_WS, hdrs) {
            Ok(r) => r,
            Err(e) => {
                eprintln!("[kalshi] bad request: {e}");
                return;
            }
        };
        let mut seq = SeqTracker::new();
        let mut sid: Option<u64> = None; // captured from the subscribed/ok ack -> targets update_subscription
        let mut sid_warned = false; // one-shot guard for the "ack had no sid" WARN (per connection)
        let mut clean;
        match tokio_tungstenite::connect_async(req).await {
            Ok((mut ws, _resp)) => {
                // fresh connection -> rebuild every book from snapshots (clear stale state). Re-subscribe
                // the CURRENT tracked set (includes anything discovery added on the prior connection).
                books.lock().unwrap().clear();
                let current: Vec<String> = tracked.lock().unwrap().iter().cloned().collect();
                let sub = kalshi_subscribe(cmd_id, &current);
                cmd_id += 1;
                if ws.send(Message::text(sub)).await.is_err() {
                    eprintln!("[kalshi] subscribe send failed; reconnecting");
                } else {
                    backoff = 1; // connected + subscribed OK -> reset backoff
                    clean = true;
                    'read: loop {
                        // FAIR select (no `biased`): a busy frame stream must not starve discovery sub
                        // updates (tokio randomizes poll order each iteration). Adds/deletes apply between
                        // frames; the WS lib still drains pings/pongs.
                        let item = tokio::select! {
                            frame = ws.next() => match frame {
                                Some(f) => f,
                                None => break 'read, // stream ended
                            },
                            upd = subs.recv() => {
                                match upd {
                                    Some(u) => {
                                        if apply_kalshi_sub_update(&mut ws, sid, &mut cmd_id, &u).await {
                                            clean = false; // sid-unknown or a failed control send -> cycle
                                            break 'read;
                                        }
                                        continue 'read;
                                    }
                                    None => continue 'read, // discovery channel closed -> keep streaming
                                }
                            }
                        };
                        let raw = match item {
                            Ok(Message::Text(t)) => t.to_string(),
                            Ok(Message::Binary(b)) => String::from_utf8_lossy(&b).into_owned(),
                            Ok(Message::Close(_)) => break 'read,
                            Ok(_) => continue, // ping/pong handled by the lib
                            Err(e) => {
                                eprintln!("[kalshi] read error: {e}");
                                clean = false;
                                break 'read;
                            }
                        };
                        let o: Value = match serde_json::from_str(&raw) {
                            Ok(v) => v,
                            Err(_) => continue,
                        };
                        if let Some(s) = o.get("seq").and_then(Value::as_u64) {
                            if !seq.check(s) {
                                let _ = tx.send(VenueEvent::SeqGap { seq: s });
                                eprintln!("[kalshi] seq gap at {s} -> cycling connection (no replay)");
                                clean = false;
                                break 'read;
                            }
                        }
                        let typ = o.get("type").and_then(Value::as_str).unwrap_or("");
                        let msg = o.get("msg").cloned().unwrap_or(Value::Null);
                        // capture the sid from the subscribed/ok ack — update_subscription targets it.
                        if sid.is_none() && matches!(typ, "subscribed" | "ok") {
                            sid = msg.get("sid").and_then(Value::as_u64);
                            // ack seen but no parseable sid -> the no-gap add path is disabled for this
                            // connection (a sub-update will force a reconnect). Operators must know. Warn
                            // once (the flag stops it spamming on every subsequent `ok`).
                            if sid.is_none() && !sid_warned {
                                eprintln!("[kalshi] {typ} ack carried no parseable sid -> no-gap adds DISABLED for this connection (adds will force a reconnect)");
                                sid_warned = true;
                            }
                        }
                        let ticker = msg.get("market_ticker").and_then(Value::as_str).map(str::to_string);
                        match typ {
                            "orderbook_snapshot" => {
                                if let Some(tk) = ticker {
                                    let mut bk = books.lock().unwrap();
                                    let b = bk.entry(tk.clone()).or_default();
                                    *b = KalshiBook::new(); // snapshot REPLACES the book (no stale carryover)
                                    apply_kalshi_snapshot(b, &msg);
                                    drop(bk);
                                    let _ = tx.send(VenueEvent::Kalshi { ticker: tk });
                                }
                            }
                            "orderbook_delta" => {
                                if let Some(tk) = ticker {
                                    let mut bk = books.lock().unwrap();
                                    if let Some(b) = bk.get_mut(&tk) {
                                        if apply_kalshi_delta(b, &msg) {
                                            drop(bk);
                                            let _ = tx.send(VenueEvent::Kalshi { ticker: tk });
                                        }
                                    }
                                }
                            }
                            _ => {} // "subscribed" ack, "ok", "error" — no book change
                        }
                    }
                    let _ = tx.send(VenueEvent::Reconnect { venue: Venue::Kalshi, clean });
                }
            }
            Err(e) => {
                eprintln!("[kalshi] connect failed: {e}; reconnect in {backoff}s");
                let _ = tx.send(VenueEvent::Reconnect { venue: Venue::Kalshi, clean: false });
            }
        }
        tokio::time::sleep(std::time::Duration::from_secs(backoff)).await;
        backoff = (backoff * 2).min(30);
    }
}

/// Apply a discovery `SubUpdate` to the live Kalshi connection: `update_subscription` add/delete on the
/// captured `sid` (no-gap). Returns `true` when the caller should FORCE A RECONNECT — either the sid isn't
/// known yet (so the add can't target it; `tracked` already holds the new keys, so cycling re-subscribes
/// them promptly instead of dropping the add until an unrelated reconnect), or a control `ws.send` failed
/// (a half-broken socket — don't keep streaming on it). Matches monitor.py's "sid not known -> cycle".
async fn apply_kalshi_sub_update<S>(ws: &mut S, sid: Option<u64>, cmd_id: &mut u64, u: &SubUpdate) -> bool
where
    S: futures_util::SinkExt<tokio_tungstenite::tungstenite::Message> + Unpin,
{
    use tokio_tungstenite::tungstenite::Message;
    let Some(sid) = sid else {
        // ack not yet seen -> can't target update_subscription. Force a reconnect so the (already-updated)
        // tracked set is re-subscribed now, rather than silently dropping the add until the next cycle.
        eprintln!("[kalshi] sub-update before sid known -> forcing reconnect to subscribe the tracked set");
        return true;
    };
    for (keys, action) in [(&u.add, "add_markets"), (&u.del, "delete_markets")] {
        if keys.is_empty() {
            continue;
        }
        if let Some(frame) = kalshi_update_subscription(*cmd_id, sid, keys, action) {
            *cmd_id += 1;
            if ws.send(Message::text(frame)).await.is_err() {
                // a failed control send means the socket is suspect — reconnect rather than leave the
                // add/delete silently lost while the read loop spins on a half-dead connection.
                eprintln!("[kalshi] sub-update {action} send failed -> forcing reconnect");
                return true;
            }
        }
    }
    false
}

/// pmus WS stream: connect, subscribe the slugs (sharded ≤100), parse each `marketData` frame and push
/// `VenueEvent::Pmus{slug, bids, asks}` (the main loop owns the `PmusBook` map). Supervised reconnect.
///
/// `subs` is the live discovery channel: an `add` is sent as a new subscribe shard (the live-verified
/// add pattern from `monitor.py`); a `del` is a LOCAL no-op (pmus has no documented unsubscribe — a
/// settled market stops streaming and the main loop already ignores untracked slugs, and `tracked` is
/// pruned so a reconnect doesn't re-subscribe it). A reconnect re-subscribes the CURRENT `tracked` set.
pub async fn pmus_stream(
    creds: std::sync::Arc<VenueCreds>,
    tracked: std::sync::Arc<std::sync::Mutex<std::collections::HashSet<String>>>,
    tx: tokio::sync::mpsc::UnboundedSender<VenueEvent>,
    mut subs: tokio::sync::mpsc::UnboundedReceiver<SubUpdate>,
) {
    use futures_util::{SinkExt, StreamExt};
    use tokio_tungstenite::tungstenite::Message;
    let mut backoff = 1u64;
    let mut add_seq = 0u64; // unique requestId suffix for in-place add shards
    loop {
        let ts = now_ms();
        let hdrs = auth::pmus_headers(&creds.pmus_ed25519, &creds.pmus_access_key, ts, "GET", PMUS_WS_PATH);
        let req = match client_request(PMUS_WS, hdrs) {
            Ok(r) => r,
            Err(e) => {
                eprintln!("[pmus] bad request: {e}");
                return;
            }
        };
        let mut clean = true;
        match tokio_tungstenite::connect_async(req).await {
            Ok((mut ws, _resp)) => {
                let current: Vec<String> = tracked.lock().unwrap().iter().cloned().collect();
                let mut send_ok = true;
                for (i, shard) in current.chunks(PMUS_SHARD).enumerate() {
                    let sub = pmus_subscribe(&format!("md-{i}"), shard);
                    if ws.send(Message::text(sub)).await.is_err() {
                        eprintln!("[pmus] subscribe send failed; reconnecting");
                        send_ok = false;
                        break;
                    }
                }
                if send_ok {
                    backoff = 1;
                    'read: loop {
                        // FAIR select (no `biased`) — same rationale as kalshi_stream.
                        let item = tokio::select! {
                            frame = ws.next() => match frame {
                                Some(f) => f,
                                None => break 'read,
                            },
                            upd = subs.recv() => {
                                if let Some(u) = upd {
                                    for shard in u.add.chunks(PMUS_SHARD) {
                                        add_seq += 1;
                                        let sub = pmus_subscribe(&format!("md-add-{add_seq}"), shard);
                                        let _ = ws.send(Message::text(sub)).await;
                                    }
                                    // u.del: no wire action (see fn docs).
                                }
                                continue 'read;
                            }
                        };
                        let raw = match item {
                            Ok(Message::Text(t)) => t.to_string(),
                            Ok(Message::Binary(b)) => String::from_utf8_lossy(&b).into_owned(),
                            Ok(Message::Close(_)) => break 'read,
                            Ok(_) => continue,
                            Err(e) => {
                                eprintln!("[pmus] read error: {e}");
                                clean = false;
                                break 'read;
                            }
                        };
                        let o: Value = match serde_json::from_str(&raw) {
                            Ok(v) => v,
                            Err(_) => continue,
                        };
                        // frames carry marketData (camelCase verified; snake_case tolerated as a fallback).
                        let md = o.get("marketData").or_else(|| o.get("market_data"));
                        if let Some(md) = md {
                            if let Some(slug) = md
                                .get("marketSlug")
                                .or_else(|| md.get("market_slug"))
                                .and_then(Value::as_str)
                            {
                                let (bids, asks) = parse_pmus_market_data(md);
                                let _ = tx.send(VenueEvent::Pmus {
                                    slug: slug.to_string(),
                                    bids,
                                    asks,
                                });
                            }
                        }
                    }
                    let _ = tx.send(VenueEvent::Reconnect { venue: Venue::Pmus, clean });
                }
            }
            Err(e) => {
                eprintln!("[pmus] connect failed: {e}; reconnect in {backoff}s");
                let _ = tx.send(VenueEvent::Reconnect { venue: Venue::Pmus, clean: false });
            }
        }
        tokio::time::sleep(std::time::Duration::from_secs(backoff)).await;
        backoff = (backoff * 2).min(30);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Kalshi snapshot frame -> correct book (the exact verified frame shape: yes/no dollars-fp ladders).
    /// Mirrors `kalshi_book.py::_selftest`'s snapshot vector through the JSON parser.
    #[test]
    fn kalshi_snapshot_frame_builds_book() {
        let frame: Value = serde_json::from_str(
            r#"{"type":"orderbook_snapshot","seq":1,"sid":7,
                "msg":{"market_ticker":"KXHIGHNY-26JUN11-T95","market_id":"m1",
                       "yes_dollars_fp":[["0.67","100.00"],["0.66","50.00"]],
                       "no_dollars_fp":[["0.31","40.00"]]}}"#,
        )
        .unwrap();
        let mut b = KalshiBook::new();
        apply_kalshi_snapshot(&mut b, &frame["msg"]);
        // YES bid 0.67; YES ask = 1 - best NO bid 0.31 = 0.69.
        assert_eq!(b.best(), (Some(0.67), Some(0.69)));
        assert_eq!(b.yes_bid_ladder()[0], (0.67, 100.0));
    }

    /// Kalshi delta frame -> correct merge onto the snapshot book (add a level, then empty it).
    #[test]
    fn kalshi_delta_frame_merges() {
        let snap: Value = serde_json::from_str(
            r#"{"msg":{"market_ticker":"T","yes_dollars_fp":[["0.67","100.00"]],"no_dollars_fp":[["0.31","40.00"]]}}"#,
        )
        .unwrap();
        let mut b = KalshiBook::new();
        apply_kalshi_snapshot(&mut b, &snap["msg"]);
        // a better YES bid at 0.68.
        let d1: Value = serde_json::from_str(r#"{"side":"yes","price_dollars":"0.68","delta_fp":"30.00"}"#).unwrap();
        assert!(apply_kalshi_delta(&mut b, &d1));
        assert_eq!(b.best().0, Some(0.68));
        // empty it -> back to 0.67.
        let d2: Value = serde_json::from_str(r#"{"side":"yes","price_dollars":"0.68","delta_fp":"-30.00"}"#).unwrap();
        assert!(apply_kalshi_delta(&mut b, &d2));
        assert_eq!(b.best().0, Some(0.67));
        // a better NO bid 0.33 -> YES ask = 0.67.
        let d3: Value = serde_json::from_str(r#"{"side":"no","price_dollars":"0.33","delta_fp":"10.00"}"#).unwrap();
        assert!(apply_kalshi_delta(&mut b, &d3));
        assert_eq!(b.best().1, Some(0.67));
        // a malformed delta (missing delta_fp) is rejected, book unchanged.
        let bad: Value = serde_json::from_str(r#"{"side":"no","price_dollars":"0.40"}"#).unwrap();
        assert!(!apply_kalshi_delta(&mut b, &bad));
        assert_eq!(b.best().1, Some(0.67));
    }

    /// pmus `marketData` frame -> (bids, asks) in the money-object shape from the verified probe.
    #[test]
    fn pmus_market_data_frame_parses() {
        let frame: Value = serde_json::from_str(
            r#"{"marketData":{"marketSlug":"tc-temp-nychigh-2026-06-11-gte95f","state":"MARKET_STATE_OPEN",
                "bids":[{"px":{"value":"0.0500","currency":"USD"},"qty":"300"},
                        {"px":{"value":"0.0400","currency":"USD"},"qty":"120"}],
                "offers":[{"px":{"value":"0.0700","currency":"USD"},"qty":"90"}]}}"#,
        )
        .unwrap();
        let md = &frame["marketData"];
        let (bids, asks) = parse_pmus_market_data(md);
        assert_eq!(bids, vec![(0.05, 300.0), (0.04, 120.0)]);
        assert_eq!(asks, vec![(0.07, 90.0)]);
        // feed it through PmusBook to confirm the touch matches (best bid 0.05 / best ask 0.07).
        let mut pb = crate::book::PmusBook::new();
        pb.apply_snapshot(&bids, &asks);
        assert_eq!(pb.best(), (Some(0.05), Some(0.07)));
    }

    /// An empty/absent side (Kalshi sends no zero-qty levels; a side key may be missing) parses to empty.
    #[test]
    fn missing_side_is_empty_not_error() {
        let frame: Value =
            serde_json::from_str(r#"{"msg":{"market_ticker":"T","yes_dollars_fp":[["0.10","5.00"]]}}"#).unwrap();
        let mut b = KalshiBook::new();
        apply_kalshi_snapshot(&mut b, &frame["msg"]);
        assert_eq!(b.best(), (Some(0.10), None)); // YES bid present, no NO side -> no YES ask
    }

    /// SeqTracker: contiguous passes, a gap fails once then resyncs (port of the Python seq test).
    #[test]
    fn seq_tracker_flags_gap() {
        let mut st = SeqTracker::new();
        assert!(st.check(1) && st.check(2) && st.check(3));
        assert!(!st.check(7)); // expected 4 -> gap
        assert!(st.check(8)); // resyncs from the gap
    }

    /// The subscribe envelopes match the verified wire shapes (a typo = silent no-data live).
    #[test]
    fn subscribe_envelopes_match_verified_shape() {
        let k: Value = serde_json::from_str(&kalshi_subscribe(1, &["A".into(), "B".into()])).unwrap();
        assert_eq!(k["cmd"], "subscribe");
        assert_eq!(k["params"]["channels"][0], "orderbook_delta");
        assert_eq!(k["params"]["market_tickers"][1], "B");
        let p: Value = serde_json::from_str(&pmus_subscribe("md-0", &["s1".into()])).unwrap();
        assert_eq!(p["subscribe"]["subscriptionType"], "SUBSCRIPTION_TYPE_MARKET_DATA");
        assert_eq!(p["subscribe"]["requestId"], "md-0");
        assert_eq!(p["subscribe"]["marketSlugs"][0], "s1");
    }

    /// A `subscribed` ack / control frame carries no `msg.market_ticker` book change — parses to no-op.
    #[test]
    fn control_frame_is_noop() {
        let ack: Value = serde_json::from_str(r#"{"type":"subscribed","seq":1,"msg":{"sid":7}}"#).unwrap();
        // the snapshot/delta parsers only act on their own `type`; a `subscribed` frame has neither.
        assert_eq!(ack["type"], "subscribed");
        assert!(ack["msg"].get("market_ticker").is_none());
        // the sid is captured from this ack -> targets update_subscription (the stream reads msg.sid).
        assert_eq!(ack["msg"]["sid"], 7);
    }

    /// `update_subscription` add/delete envelope matches the probe-verified wire shape (monitor.py
    /// update_sub_cmd selftest); a bogus action yields None so no malformed frame is ever sent.
    #[test]
    fn update_subscription_envelope_and_bad_action() {
        let add: Value = serde_json::from_str(&kalshi_update_subscription(7, 3, &["T1".into(), "T2".into()], "add_markets").unwrap()).unwrap();
        assert_eq!(add["cmd"], "update_subscription");
        assert_eq!(add["id"], 7);
        assert_eq!(add["params"]["sids"][0], 3);
        assert_eq!(add["params"]["market_tickers"][1], "T2");
        assert_eq!(add["params"]["action"], "add_markets");
        let del: Value = serde_json::from_str(&kalshi_update_subscription(8, 3, &["T1".into()], "delete_markets").unwrap()).unwrap();
        assert_eq!(del["params"]["action"], "delete_markets");
        assert!(kalshi_update_subscription(9, 3, &["T1".into()], "remove_markets").is_none()); // bogus -> no frame
    }

    /// SubUpdate diffing helper the refresh task uses (current vs fresh -> add/del). A pure set-diff.
    #[test]
    fn sub_update_default_is_empty() {
        let u = SubUpdate::default();
        assert!(u.add.is_empty() && u.del.is_empty());
    }
}
