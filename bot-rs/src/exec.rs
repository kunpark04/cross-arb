//! Execution backends.
//!
//! `DryRunBackend` is the DEFAULT and never touches the network — it logs the intent and returns a
//! simulated ack. `LiveBackend` builds the REAL venue order payload AND fires the signed HTTPS POST.
//! Real-money submission is a deliberate OWNER action — **NOT** because a sandbox prevents it: the
//! 2026-06-11 verification proved this environment CAN reach both venues with auth (Kalshi demo+prod
//! signed reads, and a 1¢ pmus BUY_LONG/BUY_SHORT placed+cancelled live). The guardrail is therefore
//! the CODE — `EXECUTION_MODE` defaults to dry-run, prod needs explicit consent, and the pmus live leg
//! is gated behind `PMUS_POST_SIGNING_VERIFIED` — not an external wall. Decision 0015 seam: the
//! decision/gating logic is real and testable; the live send stays gated to `EXECUTION_MODE=live`.
//!
//! ## dyn-compatibility of the async transport (the design choice)
//! `ExecutionBackend` is used as `Box<dyn ExecutionBackend>`, and `submit_pair` is SYNCHRONOUS (an
//! async-fn-in-trait is not dyn-compatible without boxing the future, which would ripple through every
//! caller and the smoke path). So `LiveBackend` owns its OWN reqwest async `Client` and drives the two
//! signed POSTs CONCURRENTLY (`tokio::try_join!`) off the ambient tokio runtime: when called from
//! inside the `#[tokio::main]` live loop it uses `block_in_place` + the current `Handle` (the
//! multi-thread runtime from `features=["full"]`); outside a runtime (unit tests) it builds a tiny
//! current-thread runtime. The trait stays object-safe and the dry-run path is byte-for-byte unchanged.

use crate::config::{Config, VenueEnv};
use crate::types::*;

#[derive(Clone, Debug, PartialEq)]
pub struct Ack {
    pub client_order_id: String,
    pub venue_order_id: String,
    /// TRUE iff the FULL requested qty actually FILLED — a 2xx create is acceptance, NOT a fill. Set by
    /// parsing the venue body (`post_leg`): Kalshi reports fill synchronously (order `status`=executed +
    /// fill count >= count); pmus needs `synchronousExecution` and reports it via `cumQuantity` /
    /// `leavesQuantity==0` / summed `executions[].lastShares` / `state`=ORDER_STATE_FILLED (field names
    /// pinned to the OpenAPI orders-schema — see `pmus_order_filled`). A resting/working/0-fill leg is
    /// `false` — it is NOT a completed hedge leg. Simulated (dry-run) acks are `true` (unchanged).
    pub filled: bool,
    pub simulated: bool,
}

#[derive(Clone, Debug, PartialEq)]
pub enum ExecError {
    LiveDisabled,      // submit() reached in DryRun mode (caller bug)
    TransportNotWired, // could not drive the async transport (no runtime available) — by design in odd hosts
    KeysUnavailable,   // live POST attempted but signing keys aren't loaded (Claude sandbox / dry-run build)
    Rejected(String),  // venue rejected the order (non-2xx) — carries status + body head
    RateLimited,
    /// pmus-first abort sentinel (0020): the FAST (Kalshi) leg was DELIBERATELY not opened because the slow
    /// pmus hedge leg did not fill first. NOT a failure — no order was ever sent for this leg. The outcome
    /// handler cancels the resting GTC hedge and does NOT halt (a clean skip, not a naked leg). [L33]
    HedgeNotFilled,
}

/// What `cancel` needs to reach the right venue endpoint for a resting order: the venue, its
/// exchange-assigned order id (persisted from the ack), and — for pmus — the market slug its cancel body
/// requires. Kalshi cancels by order id in the URL path; pmus needs `{marketSlug}` in the body.
#[derive(Clone, Debug, PartialEq)]
pub struct CancelTarget {
    pub venue: Venue,
    pub venue_order_id: String,
    pub market: String, // pmus: the marketSlug (required in the cancel body); Kalshi: unused
}

/// Result of firing a hedged PAIR — one ack per leg. A one-legged result is the naked-leg risk the
/// (stage-2) unwind logic must handle.
#[derive(Clone, Debug, PartialEq)]
pub struct PairAck {
    pub a: Result<Ack, ExecError>,
    pub b: Result<Ack, ExecError>,
}
impl PairAck {
    /// A COMPLETE hedge requires BOTH legs to be `Ok` AND actually FILLED — an accepted-but-resting
    /// (working) leg is `Ok` yet `filled:false` and is NOT a hedge (one filled + one resting = a naked
    /// directional position once the resting leg is cancelled / never fills). The old `is_ok()`-only
    /// check treated an order-create 2xx as proof of fill — the core bug this fixes.
    pub fn both_filled(&self) -> bool {
        let leg_filled = |r: &Result<Ack, ExecError>| matches!(r, Ok(a) if a.filled);
        leg_filled(&self.a) && leg_filled(&self.b)
    }
}

/// `Send + Sync` so the live loop can hold the backend as `Arc<dyn ExecutionBackend>` and `tokio::spawn`
/// a submission task that clones the Arc — the event loop never blocks on the two-leg network RTT (the
/// concurrency-core fix). All methods take `&self`: `DryRunBackend` only logs and `LiveBackend` drives its
/// POSTs off `&self.http` (reqwest::Client is itself `Send + Sync` + cheaply cloneable), so neither needs
/// `&mut self`. Shared- access correctness is preserved because no method mutates backend state.
pub trait ExecutionBackend: Send + Sync {
    /// Fire BOTH legs of a hedged pair. The unit of execution is the PAIR — callers must NEVER
    /// serialize the legs: serial legging ~doubles effective latency (measured serial floor p50 161 ms
    /// vs ~86 ms concurrent). The LIVE backend fires them CONCURRENTLY over two warm, pre-authed
    /// connections (stage-2, `tokio::join!`); the dry-run backend logs both. This pair-shaped signature
    /// exists NOW so stage-2 can't accidentally harden a serial single-leg path — it's the single
    /// highest-leverage latency item from the rust review, and the only latency lever the code controls.
    fn submit_pair(&self, a: &OrderIntent, b: &OrderIntent) -> PairAck;
    /// Fire ONE leg on its own. The pair is the normal unit (`submit_pair`); this single-leg primitive
    /// exists for the NAKED-LEG RECOVERY (`main::recover_naked_leg`): when only one entry leg filled, the
    /// other is cancelled and the filled leg is FLATTENED with a single marketable SELL — there is no second
    /// leg to fire. Same per-leg signing/parse as one half of `submit_pair`.
    fn submit(&self, intent: &OrderIntent) -> Result<Ack, ExecError>;
    /// Cancel a resting order by its persisted venue order id (Kalshi `DELETE /portfolio/orders/{id}`;
    /// pmus `POST /v1/order/{id}/cancel` with a `{marketSlug}` body). Needs the `CancelTarget` (venue + id
    /// + slug) the ack persisted — a `client_order_id` alone can't reach either endpoint.
    fn cancel(&self, target: &CancelTarget) -> Result<(), ExecError>;
    fn label(&self) -> &'static str;
}

/// DEFAULT backend. Logs the intent, returns a simulated ack. NEVER sends.
pub struct DryRunBackend;

impl DryRunBackend {
    fn log_leg(&self, intent: &OrderIntent) -> Result<Ack, ExecError> {
        println!(
            "[DRY-RUN] would submit: {:?} {:?} {}x @ {}c  market={}  coid={}",
            intent.venue, intent.side, intent.qty, intent.price_cents, intent.market, intent.client_order_id
        );
        // log the simulated execution too ("ALL executions"): mode=dry-run, no real ack.
        crate::exec_log::order_submit(intent, "dry-run", None, "SIMULATED", true, 0.0, "dry-run (no order sent)");
        Ok(Ack {
            client_order_id: intent.client_order_id.clone(),
            venue_order_id: "SIMULATED".into(),
            filled: true, // dry-run treats every simulated leg as filled (unchanged behaviour)
            simulated: true,
        })
    }
}

impl ExecutionBackend for DryRunBackend {
    fn submit_pair(&self, a: &OrderIntent, b: &OrderIntent) -> PairAck {
        // both legs logged together — mirrors the concurrent live fire.
        PairAck {
            a: self.log_leg(a),
            b: self.log_leg(b),
        }
    }
    fn submit(&self, intent: &OrderIntent) -> Result<Ack, ExecError> {
        self.log_leg(intent) // single-leg recovery flatten: logs + returns a simulated filled ack
    }
    fn cancel(&self, target: &CancelTarget) -> Result<(), ExecError> {
        println!("[DRY-RUN] would cancel: {:?} order_id={} market={}", target.venue, target.venue_order_id, target.market);
        Ok(())
    }
    fn label(&self) -> &'static str {
        "dry-run"
    }
}

/// Real order path. Order CONSTRUCTION is real and unit-testable; the signed network POST runs in the
/// owner's environment using the read-write key referenced by `kalshi_key_path`. Signing keys are
/// loaded lazily from env at construction — when absent (Claude's sandbox, or a dry-run-only build)
/// the live POST returns a typed `KeysUnavailable` error instead of sending.
pub struct LiveBackend {
    pub venue_env: VenueEnv,
    pub kalshi_key_path: String,
    /// pmus `maxBlockTime` (seconds, min 1) for `synchronousExecution` — derived from `leg_fill_timeout_ms`
    /// so the create call blocks until the order resolves and the response can report a real fill state.
    pmus_max_block_s: u64,
    keys: Option<TransportKeys>,
    http: reqwest::Client,
}

/// The loaded signing material for the live POSTs (owner env). Absent in the sandbox / dry-run builds.
struct TransportKeys {
    kalshi_access_key: String,
    kalshi_rsa: rsa::RsaPrivateKey,
    pmus_access_key: String,
    pmus_ed25519: ed25519_dalek::SigningKey,
}

const KALSHI_ORDERS_PATH: &str = "/trade-api/v2/portfolio/orders";

/// Whether the owner has explicitly confirmed the pmus POST-body signing scheme against the live/demo
/// endpoint. Default OFF: until set, a pmus LIVE leg is REFUSED (not fired possibly-mis-signed, which
/// would systematically leg out the hedge — see `build_pmus_payload`). Self-contained env read (NOT a
/// config field) so the gate is a deliberate operator unlock, independent of the rest of the config.
fn pmus_post_signing_verified() -> bool {
    matches!(
        std::env::var("PMUS_POST_SIGNING_VERIFIED").ok().as_deref(),
        Some("yes") | Some("1") | Some("true")
    )
}

/// Read a numeric JSON value that the venue may encode as a number OR a string (Kalshi returned
/// `fill_count` as the STRING `"0.00"` live; pmus shares are strings). `None` if absent/unparseable.
fn num_or_str(v: &serde_json::Value) -> Option<f64> {
    match v {
        serde_json::Value::Number(n) => n.as_f64(),
        serde_json::Value::String(s) => s.trim().parse::<f64>().ok(),
        _ => None,
    }
}

/// Did the KALSHI order FILL the full requested `qty`? Judged on the FILLED QUANTITY (the ground truth).
/// CURRENT Kalshi API field: **`fill_count_fp`** — a fixed-point STRING, e.g. `"1.00"` (older shape used
/// `fill_count`/`filled_count`, kept as fallbacks). FILLED iff the filled count `>= qty`. FAIL-SAFE: an
/// absent/0 count -> `false`, so a `resting` (marketable-miss) order or an unrecognized body is NEVER read as
/// a hedge leg (the silent-naked-leg axis — a fabricated "filled" leaves a real naked position).
///
/// ⚠️ HISTORY (2026-06-14): this previously read only `fill_count` AND hard-required a `status` field. A LIVE
/// probe caught the bot returning `parsed_filled=false` on a REAL `{"fill_count_fp":"1.00","status":"executed",
/// "remaining_count_fp":"0.00"}` fill — the venue had RENAMED the count field, so EVERY Kalshi fill was being
/// missed (→ the bot would treat a filled leg as unfilled → release/recover → silent naked position). The
/// create-response unit test used a fictional `fill_count` fixture, so it never caught the rename. Now
/// quantity-based on the real field, and the test is pinned to the captured live shape.
fn kalshi_order_filled(v: &serde_json::Value, qty: u32) -> bool {
    let order = v.get("order").unwrap_or(v); // tolerate both {order:{..}} and a flat {..}
    let fill_count = order
        .get("fill_count_fp")            // CURRENT API: filled qty, fixed-point STRING ("1.00")
        .or_else(|| order.get("fill_count")) // legacy field names (back-compat)
        .or_else(|| order.get("filled_count"))
        .and_then(num_or_str)
        .unwrap_or(0.0);
    fill_count >= qty as f64
}

/// Extract the exchange order id from a CreateOrder ack: Kalshi nests it at `order.order_id` (live-verified
/// 2026-06-11/-14), pmus puts it TOP-LEVEL at `id` (camel `orderId` tolerated). Empty when absent (an error
/// body or a renamed field) — surfaced only later when a cancel/recovery has no id to target, so it is pinned
/// to a captured real body in tests (closing the F1 audit gap; the same false-green class as [L32]).
fn parse_venue_order_id(v: &serde_json::Value) -> String {
    v.get("order")
        .and_then(|o| o.get("order_id"))
        .or_else(|| v.get("orderId"))
        .or_else(|| v.get("id"))
        .and_then(|x| x.as_str())
        .unwrap_or("")
        .to_string()
}

/// Did the PMUS order FILL the full requested `qty`? With `synchronousExecution` (set in
/// `build_pmus_payload`) the create blocks until the order resolves. A bare 2xx WITHOUT fill evidence is
/// acceptance only ("accepted" != "filled"). FILLED iff ANY of these prove the full requested qty filled:
/// (1) `cumQuantity >= qty` (cumulative filled qty); (2) `leavesQuantity == 0 AND cumQuantity > 0` (nothing
/// left unfilled, and something did fill); (3) summed `executions[].lastShares` (or `shares`/`quantity`)
/// `>= qty`; (4) a terminal `ORDER_STATE_FILLED` `state` (not `PARTIALLY_FILLED`).
/// Otherwise (the accept-only `{id,intent,...}` body, or any ambiguity) -> false (FAIL SAFE — absence of
/// fill evidence must NEVER be read as a fill on the money path; a spurious "not filled" only trips a halt,
/// a fabricated "filled" leaves a real naked hedge). A top-level `{"order":{..}}` wrapper is unwrapped first.
///
/// DOC NOTE (OpenAPI orders-schema.json, docs.polymarket.us, 2026-06-11): authoritative field names PINNED.
/// `Order` carries `cumQuantity`/`leavesQuantity`/`quantity` (all number/double) + `state` (enum:
/// `ORDER_STATE_FILLED`/`ORDER_STATE_PARTIALLY_FILLED`/…); `GetOrderResponse` wraps it as `{"order":{..}}`.
/// The synchronous `CreateOrderResponse` is `{"id":string,"executions":[Execution]}` with `executions`
/// present "if synchronous execution was requested" — each `Execution.lastShares` is a STRING (live-verified
/// 2026-06-11: `BUY_LONG`/`BUY_SHORT` created on `/v1/orders` returned `{id,executions:[{lastShares,lastPx}]}`).
/// (The schema does NOT define `remainingQty` or `orderStatus`; `leavesQuantity`/`state` are the real names.)
fn pmus_order_filled(v: &serde_json::Value, qty: u32) -> bool {
    let q = qty as f64;
    let v = v.get("order").unwrap_or(v); // tolerate the GetOrderResponse `{"order":{..}}` wrapper
    // 1) explicit cumulative filled qty (Order.cumQuantity).
    if let Some(cum) = v.get("cumQuantity").and_then(num_or_str) {
        if cum >= q {
            return true;
        }
    }
    // 2) leavesQuantity == 0 AND cumQuantity > 0: nothing left unfilled and at least some fill occurred
    //    (a brand-new resting order has cumQuantity 0, so the cumQuantity>0 guard keeps acceptance != fill).
    //    `remainingQty` is accepted as a defensive alias (not in the schema, but harmless if a body carries it).
    if let Some(leaves) = v.get("leavesQuantity").or_else(|| v.get("remainingQty")).and_then(num_or_str) {
        let cum = v.get("cumQuantity").and_then(num_or_str).unwrap_or(0.0);
        if leaves <= 0.0 && cum > 0.0 && cum >= q {
            return true;
        }
    }
    // 3) sum the executions' shares (the synchronous CreateOrderResponse array; `lastShares` is the schema
    //    field — a STRING — with `shares`/`quantity` tolerated as aliases, number or string).
    if let Some(execs) = v.get("executions").and_then(|e| e.as_array()) {
        if !execs.is_empty() {
            let total: f64 = execs
                .iter()
                .filter_map(|e| {
                    e.get("lastShares")
                        .or_else(|| e.get("shares"))
                        .or_else(|| e.get("quantity"))
                        .and_then(num_or_str)
                })
                .sum();
            if total >= q {
                return true;
            }
        }
    }
    // 4) a terminal FILLED `state` (the schema enum field), if the body carries one. `ORDER_STATE_FILLED`
    //    contains "FILL" and not "PARTIAL"; `ORDER_STATE_PARTIALLY_FILLED` contains "PARTIAL" -> not a full fill.
    let state = v
        .get("state")
        .or_else(|| v.get("orderStatus"))
        .or_else(|| v.get("status"))
        .and_then(|s| s.as_str())
        .unwrap_or("")
        .to_ascii_uppercase();
    state.contains("FILL") && !state.contains("PARTIAL")
}

impl LiveBackend {
    pub fn new(cfg: &Config) -> Self {
        let keys = Self::load_keys(cfg); // None when env/keys absent -> live POST returns a typed error
        LiveBackend {
            venue_env: cfg.venue_env,
            kalshi_key_path: cfg.kalshi_key_path.clone(),
            // ms -> whole seconds, floor, min 1s: a sub-second timeout would round to 0 and tell pmus not
            // to block at all (defeating synchronousExecution); 1s is the documented minimum useful block.
            pmus_max_block_s: (cfg.leg_fill_timeout_ms / 1000).max(1),
            keys,
            http: reqwest::Client::builder()
                .use_rustls_tls()
                .build()
                .unwrap_or_else(|_| reqwest::Client::new()),
        }
    }

    /// Load both venues' signing keys from env + the external key path. Returns `None` (not an error) if
    /// anything is missing — the backend still constructs (so `build_*_payload` stays testable) but the
    /// live POST refuses to send. The Kalshi RSA key is read from the owner's external path at runtime.
    fn load_keys(cfg: &Config) -> Option<TransportKeys> {
        let kalshi_access_key = std::env::var("KALSHI_ACCESS_KEY").ok()?;
        let pem = std::fs::read_to_string(&cfg.kalshi_key_path).ok()?;
        let kalshi_rsa = crate::auth::kalshi_key_from_pem(&pem).ok()?;
        let pmus_access_key = std::env::var("PMUS_ACCESS_KEY").ok()?;
        let pmus_secret = std::env::var("PMUS_SECRET").ok()?;
        let pmus_ed25519 = crate::auth::pmus_key_from_secret_b64(&pmus_secret).ok()?;
        Some(TransportKeys {
            kalshi_access_key,
            kalshi_rsa,
            pmus_access_key,
            pmus_ed25519,
        })
    }

    /// The Kalshi REST base for the configured venue env. Demo (sandbox) is the staged-rollout default.
    pub fn kalshi_base(&self) -> &'static str {
        match self.venue_env {
            VenueEnv::Demo => "https://demo-api.kalshi.co/trade-api/v2",
            VenueEnv::Prod => "https://api.elections.kalshi.com/trade-api/v2",
        }
    }

    /// The pmus authenticated REST base (orders host). Demo/prod share the same host on pmus's retail
    /// surface; the sandbox/prod distinction is the Kalshi side (pmus has no documented demo host).
    ///
    /// SLUG IDENTITY — VERIFIED LIVE (2026-06-11), not assumed: the `marketSlug` discovery pulls from the
    /// GATEWAY catalog (`gateway.polymarket.us/v1/markets`, see `discovery::PM_MARKETS`) is the SAME slug the
    /// ORDER endpoint here (`api.polymarket.us/v1/orders`) accepts — gateway-catalog slugs placed real
    /// `BUY_LONG`/`BUY_SHORT` orders on `api.polymarket.us`. So a discovered slug is order-routable as-is.
    pub fn pmus_base(&self) -> &'static str {
        "https://api.polymarket.us"
    }

    /// Build the Kalshi CreateOrder body (real). A limit order; Kalshi dedupes on `client_order_id` (a
    /// REAL idempotency token — a transport retry can't double-fire this leg). (pmus has NO such token —
    /// see `build_pmus_payload`.) Built with `serde_json`
    /// (never `format!`) so a `"`/`\`/control char in the venue-supplied `market`/`client_order_id` is
    /// escaped, not spliced raw into the body — a malformed/injected order would otherwise 400 (naked leg)
    /// or alter `count`/price. Numbers stay numbers (`json!` preserves the bare `count`/`yes_price`).
    pub fn build_kalshi_payload(&self, intent: &OrderIntent) -> String {
        let side = match intent.side {
            Side::Yes => "yes",
            Side::No => "no",
        };
        let action = match intent.action {
            Action::Buy => "buy",
            Action::Sell => "sell", // an unwind closes the leg we hold
        };
        // Kalshi's CreateOrder takes `yes_price` OR `no_price` (cents) — the field MATCHING the order side.
        // `build_legs` hands a NO leg the NO price, so a `side:no` order must use `no_price` (sending
        // `yes_price` on a no-order is the same class of bug the pmus NO leg had — caught live). BOTH sides are
        // now LIVE-VERIFIED: YES (2026-06-11 placed/cancelled) and NO (2026-06-14 — a 1¢ BUY-NO on an empty book
        // was recorded by Kalshi as `outcome_side:"no"` + `no_price_dollars:"0.0100"`, rested + cancelled). Holds.
        let price_key = match intent.side {
            Side::Yes => "yes_price",
            Side::No => "no_price",
        };
        let mut body = serde_json::json!({
            "action": action,
            "side": side,
            "ticker": intent.market,
            "count": intent.qty,
            "type": "limit",
            "client_order_id": intent.client_order_id,
        });
        body[price_key] = serde_json::json!(intent.price_cents);
        // FILL-OR-KILL on entry BUYs (2026-06-15 incident fix). A resting GTC limit that the bot reads as
        // "not filled" at one instant can FILL SECONDS LATER -> an untracked naked position (the incident's
        // root cause). FOK forces the venue to fill the whole clip IMMEDIATELY or KILL it, so "not filled"
        // becomes TERMINAL: no late fill, no cancel race. Recovery/unwind SELLs deliberately stay GTC — they
        // flatten a KNOWN leg, and a rested SELL at worst triggers a needless halt, never a naked position.
        // API-DOC-VERIFIED (docs.kalshi.com, 2026-06-15): /trade-api/v2/portfolio/orders takes time_in_force as
        // an OPTIONAL enum {fill_or_kill, good_till_canceled, immediate_or_cancel} — "fill_or_kill" is valid, and
        // an UNKNOWN value 400s (it is NOT silently rested as GTC), so this can't degrade into the late-fill race.
        // Optional ⇒ the SELL path (no field) defaults to GTC, as intended. LIVE-VERIFIED ACCEPTED 2026-06-15
        // (ITF arb: a fill_or_kill BUY YES @25¢ filled + owner-reconciled); kill-on-miss = documented FOK
        // semantics, not yet directly observed (both legs filled — the first leg-MISS confirms it).
        if matches!(intent.action, Action::Buy) {
            body["time_in_force"] = serde_json::json!("fill_or_kill");
        }
        body.to_string()
    }

    /// Build a pmus CreateOrder body for `POST /v1/orders`.
    /// `(action, side)` maps to the `OrderIntent` enum on the SAME `marketSlug`: Buy-YES=`BUY_LONG`,
    /// Buy-NO=`BUY_SHORT`, Sell-YES=`SELL_LONG`, Sell-NO=`SELL_SHORT`.
    ///
    /// ⚠️ NO IDEMPOTENCY KEY — pmus's CreateOrder has no `clientOrderId` field (it is silently dropped),
    /// and pmus order creation is NOT idempotent. So we DON'T send one (it would be dead weight that
    /// falsely implies dedupe), and a pmus timeout/RateLimited is "unknown" — reconcile via
    /// positions/open-orders before any resend, never blindly retry. (Kalshi's `client_order_id` IS real.)
    ///
    /// ⚠️ SYNCHRONOUS EXECUTION — `synchronousExecution:true` + `maxBlockTime` make the create BLOCK until
    /// the order fills/rejects/cancels/expires, so `post_leg` can judge a real fill from the response
    /// rather than treating bare acceptance as a fill (a 2xx without `synchronousExecution` returns only
    /// `{id}` and the fill arrives later on the order stream — "accepted" != "filled").
    ///
    /// ⚠️ PRICE IS ALWAYS IN **YES** TERMS — the critical thing live-testing caught (2026-06-11): pmus
    /// runs `BUY_SHORT`/`SELL_SHORT` as a SELL/BUY of YES under the hood (`order.side = ORDER_SIDE_SELL`
    /// for a `BUY_SHORT`), and the `price` is the YES price. A probe "buy NO @ 1¢" was executed as a
    /// marketable "sell YES @ 1¢", filled at the ~54¢ YES bid, and opened an unintended short. So a NO
    /// leg's price must be the YES-EQUIVALENT = `1 − (NO price)`; sending the raw NO price fills at the
    /// wrong price/side. `build_legs` hands a NO leg the NO price (1 − book YES bid), so convert here.
    /// Built with `serde_json` (never `format!`) so a `"`/`\` in the venue slug/coid is escaped, not spliced.
    pub fn build_pmus_payload(&self, intent: &OrderIntent) -> String {
        let order_intent = match (intent.action, intent.side) {
            (Action::Buy, Side::Yes) => "ORDER_INTENT_BUY_LONG",
            (Action::Buy, Side::No) => "ORDER_INTENT_BUY_SHORT",
            (Action::Sell, Side::Yes) => "ORDER_INTENT_SELL_LONG",
            (Action::Sell, Side::No) => "ORDER_INTENT_SELL_SHORT",
        };
        // YES-denominated price: a YES leg's price_cents is already YES; a NO leg's price_cents is the NO
        // price, whose YES equivalent is 100 − price_cents. (Tick is 0.001; cent granularity is within it.)
        let yes_cents = match intent.side {
            Side::Yes => intent.price_cents,
            Side::No => 100u8.saturating_sub(intent.price_cents),
        };
        let value = format!("{:.2}", (yes_cents as f64) / 100.0);
        // FILL-OR-KILL on entry BUYs (2026-06-15 incident fix — same rationale as the Kalshi leg). A GTC pmus
        // order RESTS after the synchronousExecution block expires and can fill LATER, after the bot read it
        // "not filled" -> an untracked naked position. FOK kills an unfilled clip at the venue (still inside the
        // block, which still returns a real fill verdict), so "not filled" is TERMINAL. Recovery/unwind SELLs
        // (flattening a KNOWN leg) stay GTC.
        // LIVE-VERIFIED ACCEPTED (2026-06-15, ITF arb aec-itfw-alepui-irifet): a FOK BUY NO @70¢ with this enum
        // FILLED (filled:true) + owner-reconciled — so `TIME_IN_FORCE_FILL_OR_KILL` is venue-accepted (NOT a
        // 400-reject) and a fillable FOK fills. NOT yet directly observed: the kill-on-NO-fill path (an unfillable
        // FOK BUY -> 2xx-no-fill -> Ok(filled:false), the "clean miss" recovery assumes) — both legs filled this
        // fire, so the first leg-MISS confirms it (no demo to force it, see [[no-demo-verify-live-only]]). SAFE-FAIL
        // meanwhile: pmus-first fires this leg FIRST, so any enum/transport failure -> Err -> the entry ABORTS
        // (no Kalshi leg, no naked position) — an availability stop, never a safety risk. [L32]
        let tif = match intent.action {
            Action::Buy => "TIME_IN_FORCE_FILL_OR_KILL",
            Action::Sell => "TIME_IN_FORCE_GOOD_TILL_CANCEL",
        };
        serde_json::json!({
            "marketSlug": intent.market,
            "intent": order_intent,
            "type": "ORDER_TYPE_LIMIT",
            "price": {"value": value, "currency": "USD"},
            "quantity": intent.qty,
            "tif": tif,
            // block until the order resolves so the response carries a real fill state (no clientOrderId:
            // pmus has no idempotency token — see the doc above). maxBlockTime is a STRING per the schema.
            "synchronousExecution": true,
            "maxBlockTime": self.pmus_max_block_s.to_string(),
        })
        .to_string()
    }

    /// Send ONE signed leg to its venue and parse the ack. Async; the two legs are joined concurrently
    /// by `submit_pair`. A Kalshi leg signs RSA-PSS over `{ts}POST{path}`; a pmus leg signs Ed25519 over
    /// the same canonical string (body-less `{ts}POST{path}` signing VERIFIED live for POST 2026-06-11).
    async fn post_leg(&self, intent: &OrderIntent) -> Result<Ack, ExecError> {
        let keys = self.keys.as_ref().ok_or(ExecError::KeysUnavailable)?;
        let ts = crate::auth::now_ms_for_sign();
        let (url, body, hdrs): (String, String, [(&'static str, String); 3]) = match intent.venue {
            Venue::Kalshi => {
                let h = crate::auth::kalshi_headers(
                    &keys.kalshi_rsa,
                    &keys.kalshi_access_key,
                    ts,
                    "POST",
                    KALSHI_ORDERS_PATH,
                );
                (format!("{}/portfolio/orders", self.kalshi_base()), self.build_kalshi_payload(intent), h)
            }
            Venue::Pmus => {
                // GATE (default OFF): a deliberate pmus-LIVE opt-in. The original reason — "POST-body
                // signing unverified" — is now RESOLVED: live control test 2026-06-11 proved a BAD sig
                // 401s while a body-less `{ts}POST{path}` sig passes auth on POSTs (auth precedes routing),
                // and a 1¢ BUY_LONG + BUY_SHORT each placed+cancelled on `/v1/orders`. So signing + both
                // ENTRY intents + the endpoint are verified; the gate remains as the explicit pmus-live
                // arming switch (the SELL_* unwind intents are still doc-only, and the edge is unvalidated).
                if !pmus_post_signing_verified() {
                    return Err(ExecError::Rejected(
                        "pmus live leg gated — set PMUS_POST_SIGNING_VERIFIED=yes to arm (signing+endpoint verified 2026-06-11)".into(),
                    ));
                }
                // VERIFIED endpoint (was the guessed `/v1/portfolio/orders`, which 404'd). Cancel is POST
                // `/v1/order/{orderId}/cancel` with a `{marketSlug}` body (also verified) — see `cancel_one`.
                let path = "/v1/orders";
                let h = crate::auth::pmus_headers(&keys.pmus_ed25519, &keys.pmus_access_key, ts, "POST", path);
                (format!("{}{}", self.pmus_base(), path), self.build_pmus_payload(intent), h)
            }
        };
        let mut req = self.http.post(&url).header("Content-Type", "application/json");
        for (k, v) in hdrs {
            req = req.header(k, v);
        }
        let t0 = std::time::Instant::now();
        let resp = match req.body(body).send().await {
            Ok(r) => r,
            Err(e) => {
                let err = if e.is_timeout() { ExecError::RateLimited } else { ExecError::Rejected(format!("transport: {e}")) };
                // a transport failure IS an execution attempt — log it too (no http/body).
                crate::exec_log::order_submit(intent, "live", None, "", false, t0.elapsed().as_secs_f64() * 1000.0, &format!("{err:?}"));
                return Err(err);
            }
        };
        let status = resp.status();
        let text = resp.text().await.unwrap_or_default();
        let latency_ms = t0.elapsed().as_secs_f64() * 1000.0;
        // Parse the ack — HARMLESS on a non-2xx error body (no `order`/fill fields -> "" / false). We parse +
        // LOG *before* the status-gate returns, so a reject/429 is recorded too, with its raw body.
        // venue_order_id: VERIFIED live (Kalshi `{"order":{"order_id":..}}`; pmus top-level `{"id":..}`).
        let v: serde_json::Value = serde_json::from_str(&text).unwrap_or(serde_json::Value::Null);
        let venue_order_id = parse_venue_order_id(&v);
        // FILLED vs merely ACCEPTED: a 2xx create is acceptance, not a fill (the core real-money bug). Read the
        // venue body for the FULL requested qty actually filling — Kalshi synchronously (`fill_count_fp`); pmus
        // with `synchronousExecution`. A non-fill (resting/0-exec) is NOT a hedge leg -> `filled:false`.
        let filled = match intent.venue {
            Venue::Kalshi => kalshi_order_filled(&v, intent.qty),
            Venue::Pmus => pmus_order_filled(&v, intent.qty),
        };
        // EXECUTION LOG (always-on, exec_log.rs): one line per submit — success OR reject — with the RAW ack
        // next to the parsed `filled`/`venue_order_id`, so a parser-vs-reality drift (the fill_count_fp class)
        // is visible by eye/jq ([L32]).
        crate::exec_log::order_submit(intent, "live", Some(status.as_u16()), &venue_order_id, filled, latency_ms, &text);
        // DIAGNOSTIC (env-gated console echo, default OFF): PROBE_LOG_RAW=1 also mirrors the raw ack to stderr.
        if std::env::var("PROBE_LOG_RAW").as_deref() == Ok("1") {
            eprintln!(
                "[raw] {:?} http={} parsed_filled={} body={}",
                intent.venue, status.as_u16(), filled,
                text.chars().take(400).collect::<String>()
            );
        }
        if status == reqwest::StatusCode::TOO_MANY_REQUESTS {
            return Err(ExecError::RateLimited);
        }
        if !status.is_success() {
            return Err(ExecError::Rejected(format!("{} {}", status.as_u16(), text.chars().take(160).collect::<String>())));
        }
        Ok(Ack {
            client_order_id: intent.client_order_id.clone(),
            venue_order_id,
            filled,
            simulated: false,
        })
    }

    /// Fire the two signed POSTs **pmus-first and SERIALLY** (decision 0020). The pmus leg is the slow,
    /// thin, uncertain one — it BLOCKS for a real fill verdict (`synchronousExecution`); only if it actually
    /// FILLED do we open the fast Kalshi leg. So the fast leg is NEVER left naked while the pmus hedge
    /// resolves: if pmus does not fill, the Kalshi leg is never sent (the `HedgeNotFilled` sentinel) and the
    /// outcome handler cancels the resting GTC pmus order — zero naked exposure. The PairAck preserves the
    /// a/b SLOTS (ack.a ↔ leg a) regardless of fire order, so all positional bookkeeping is unchanged.
    ///
    /// This REVERSES the pre-0020 concurrent `tokio::join!` (~86ms p50). Serial is ~1 block-second slower,
    /// but the first live arb proved concurrent fire leaves the fast leg exposed for the WHOLE pmus block:
    /// Kalshi filled @86¢, pmus never filled (1.5s block), and flattening the naked Kalshi leg cost ~9¢ on a
    /// thin book. Latency is network-bound regardless; the forgone fast-evaporating edges were phantom (the
    /// hedge wasn't executable). [L33]
    fn run_pair(&self, a: &OrderIntent, b: &OrderIntent) -> PairAck {
        // identify the pmus (blocking) leg; fire it first, open the OTHER leg only if it FILLED. A real arb
        // always has exactly one pmus + one Kalshi leg; if neither is pmus (never, defensively), b-first
        // serial is still naked-leg-free.
        let a_is_pmus = a.venue == Venue::Pmus;
        let fut = async {
            let leg_filled = |r: &Result<Ack, ExecError>| matches!(r, Ok(ack) if ack.filled);
            if a_is_pmus {
                let ra = self.post_leg(a).await;
                let rb = if leg_filled(&ra) { self.post_leg(b).await } else { Err(ExecError::HedgeNotFilled) };
                PairAck { a: ra, b: rb }
            } else {
                let rb = self.post_leg(b).await;
                let ra = if leg_filled(&rb) { self.post_leg(a).await } else { Err(ExecError::HedgeNotFilled) };
                PairAck { a: ra, b: rb }
            }
        };
        match tokio::runtime::Handle::try_current() {
            Ok(handle) => tokio::task::block_in_place(|| handle.block_on(fut)),
            Err(_) => match tokio::runtime::Builder::new_current_thread().enable_all().build() {
                Ok(rt) => rt.block_on(fut),
                Err(_) => PairAck {
                    a: Err(ExecError::TransportNotWired),
                    b: Err(ExecError::TransportNotWired),
                },
            },
        }
    }

    /// Drive ONE signed POST to completion off the ambient runtime (same dyn-compat pattern as `run_pair`)
    /// — the single-leg recovery flatten. No fabricated concurrency: it's intrinsically one order.
    fn run_one(&self, intent: &OrderIntent) -> Result<Ack, ExecError> {
        let fut = self.post_leg(intent);
        match tokio::runtime::Handle::try_current() {
            Ok(handle) => tokio::task::block_in_place(|| handle.block_on(fut)),
            Err(_) => match tokio::runtime::Builder::new_current_thread().enable_all().build() {
                Ok(rt) => rt.block_on(fut),
                Err(_) => Err(ExecError::TransportNotWired),
            },
        }
    }

    /// The cancel HTTP request line + body for `target` — PURE (no signing, no I/O) so it is unit-testable
    /// without keys. Kalshi: `DELETE /trade-api/v2/portfolio/orders/{id}`, empty body. pmus: `POST
    /// /v1/order/{id}/cancel` with a `{"marketSlug":..}` body (the marketSlug is REQUIRED by the schema and
    /// escaped by `serde_json`). Returns the signing `path` too (Kalshi signs the id-in-path; pmus signs
    /// POST+path, body-less, same scheme as create). Venue order ids are opaque tokens without `/`/`?`.
    fn cancel_request(&self, target: &CancelTarget) -> (reqwest::Method, String, String, String) {
        match target.venue {
            Venue::Kalshi => {
                let path = format!("{KALSHI_ORDERS_PATH}/{}", target.venue_order_id);
                let url = format!("{}/portfolio/orders/{}", self.kalshi_base(), target.venue_order_id);
                (reqwest::Method::DELETE, url, String::new(), path)
            }
            Venue::Pmus => {
                let path = format!("/v1/order/{}/cancel", target.venue_order_id);
                let url = format!("{}{}", self.pmus_base(), path);
                let body = serde_json::json!({ "marketSlug": target.market }).to_string();
                (reqwest::Method::POST, url, body, path)
            }
        }
    }

    /// Sign the cancel request for `target` -> (METHOD, url, body, headers). Wraps `cancel_request` with the
    /// per-venue signature over `{ts}{METHOD}{path}` (RSA-PSS for Kalshi, Ed25519 for pmus).
    fn build_cancel(&self, keys: &TransportKeys, target: &CancelTarget)
        -> (reqwest::Method, String, String, [(&'static str, String); 3])
    {
        let ts = crate::auth::now_ms_for_sign();
        let (method, url, body, path) = self.cancel_request(target);
        let hdrs = match target.venue {
            Venue::Kalshi => crate::auth::kalshi_headers(&keys.kalshi_rsa, &keys.kalshi_access_key, ts, method.as_str(), &path),
            Venue::Pmus => crate::auth::pmus_headers(&keys.pmus_ed25519, &keys.pmus_access_key, ts, method.as_str(), &path),
        };
        (method, url, body, hdrs)
    }

    /// Send ONE signed cancel and map the status to a result (2xx => Ok). pmus's cancel response is empty
    /// on success; Kalshi returns the canceled order — we only need the HTTP status either way.
    async fn cancel_one(&self, target: &CancelTarget) -> Result<(), ExecError> {
        let keys = self.keys.as_ref().ok_or(ExecError::KeysUnavailable)?;
        if target.venue == Venue::Pmus && !pmus_post_signing_verified() {
            return Err(ExecError::Rejected("pmus cancel gated — set PMUS_POST_SIGNING_VERIFIED=yes to arm".into()));
        }
        if target.venue_order_id.is_empty() {
            return Err(ExecError::Rejected("cancel: no venue order id persisted for this leg".into()));
        }
        let (method, url, body, hdrs) = self.build_cancel(keys, target);
        let mut req = self.http.request(method, &url).header("Content-Type", "application/json");
        for (k, v) in hdrs {
            req = req.header(k, v);
        }
        let venue = format!("{:?}", target.venue);
        let t0 = std::time::Instant::now();
        let resp = match req.body(body).send().await {
            Ok(r) => r,
            Err(e) => {
                let err = if e.is_timeout() { ExecError::RateLimited } else { ExecError::Rejected(format!("transport: {e}")) };
                crate::exec_log::order_cancel(&venue, &target.market, &target.venue_order_id, None, false, t0.elapsed().as_secs_f64() * 1000.0, &format!("{err:?}"));
                return Err(err);
            }
        };
        let status = resp.status();
        let text = resp.text().await.unwrap_or_default();
        let latency_ms = t0.elapsed().as_secs_f64() * 1000.0;
        // EXECUTION LOG: every cancel, with its raw response (a 404 not_found means the order wasn't resting —
        // already filled/gone; the probe's filled-then-404 was exactly this).
        crate::exec_log::order_cancel(&venue, &target.market, &target.venue_order_id, Some(status.as_u16()), status.is_success(), latency_ms, &text);
        if status == reqwest::StatusCode::TOO_MANY_REQUESTS {
            return Err(ExecError::RateLimited);
        }
        if !status.is_success() {
            return Err(ExecError::Rejected(format!("{} {}", status.as_u16(), text.chars().take(160).collect::<String>())));
        }
        Ok(())
    }

    /// Drive one cancel to completion off the ambient runtime (same dyn-compat pattern as `run_pair`).
    fn run_cancel(&self, target: &CancelTarget) -> Result<(), ExecError> {
        let fut = self.cancel_one(target);
        match tokio::runtime::Handle::try_current() {
            Ok(handle) => tokio::task::block_in_place(|| handle.block_on(fut)),
            Err(_) => match tokio::runtime::Builder::new_current_thread().enable_all().build() {
                Ok(rt) => rt.block_on(fut),
                Err(_) => Err(ExecError::TransportNotWired),
            },
        }
    }
}

impl ExecutionBackend for LiveBackend {
    fn submit_pair(&self, a: &OrderIntent, b: &OrderIntent) -> PairAck {
        // Fire BOTH legs CONCURRENTLY (tokio::join! inside run_pair) — each RSA-PSS (Kalshi) / Ed25519
        // (pmus) signed. Concurrency is the one latency lever the code owns (serial ~161 ms p50 ->
        // concurrent ~86 ms). Returns KeysUnavailable per-leg when the signing keys aren't loaded
        // (a dry-run/no-creds build) — that absence, plus the dry-run default, is what keeps real money
        // unsent (NOT a sandbox: the environment can reach the venues — verified live).
        if self.keys.is_none() {
            return PairAck {
                a: Err(ExecError::KeysUnavailable),
                b: Err(ExecError::KeysUnavailable),
            };
        }
        self.run_pair(a, b)
    }
    fn submit(&self, intent: &OrderIntent) -> Result<Ack, ExecError> {
        // single-leg recovery flatten. KeysUnavailable when keys aren't loaded (dry-run/no-creds build) —
        // same gate as submit_pair, so a sandbox build never sends and the recovery falls back to halt.
        if self.keys.is_none() {
            return Err(ExecError::KeysUnavailable);
        }
        self.run_one(intent)
    }
    fn cancel(&self, target: &CancelTarget) -> Result<(), ExecError> {
        // Cancel endpoints VERIFIED live 2026-06-11: Kalshi `DELETE /trade-api/v2/portfolio/orders/{order_id}`
        // (200) and pmus `POST /v1/order/{orderId}/cancel` with a `{marketSlug}` body (200). The venue order
        // id is now PERSISTED on the held position's legs (`PositionLeg::venue_order_id`, set in
        // `main::apply_outcome`), so this is functional given a `CancelTarget`. KeysUnavailable when keys
        // aren't loaded (dry-run/no-creds build). TODO(stage-2 loop wiring): the leg-fill-timeout that fires
        // this on a resting leg — cancel the resting leg, then unwind/flatten the filled one — is NOT yet in
        // the event loop (a naked live leg still fail-closes + halts in `apply_outcome`); see `naked_leg_failclose`.
        if self.keys.is_none() {
            return Err(ExecError::KeysUnavailable);
        }
        self.run_cancel(target)
    }
    fn label(&self) -> &'static str {
        match self.venue_env {
            VenueEnv::Demo => "live:demo-sandbox",
            VenueEnv::Prod => "live:PRODUCTION",
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn dry_run_fires_both_legs_simulated() {
        let bk = DryRunBackend;
        let a = OrderIntent {
            venue: Venue::Kalshi,
            market: "KXHIGHNY-26JUN11-T95".into(),
            action: Action::Buy,
            side: Side::Yes,
            price_cents: 8,
            qty: 1,
            client_order_id: "leg-a".into(),
        };
        let b = OrderIntent {
            venue: Venue::Pmus,
            market: "tc-temp-nychigh-2026-06-11-gte95f".into(),
            action: Action::Buy,
            side: Side::No,
            price_cents: 90,
            qty: 1,
            client_order_id: "leg-b".into(),
        };
        let r = bk.submit_pair(&a, &b);
        assert!(r.both_filled() && r.a.unwrap().simulated);
    }

    #[test]
    fn live_payload_is_well_formed_but_does_not_send() {
        let cfg = Config {
            mode: crate::config::ExecutionMode::Live,
            venue_env: VenueEnv::Demo,
            kalshi_key_path: "/owner/path/kalshi-readwrite.pem".into(),
            pmus_env_path: String::new(),
            edge_floor_cents: 2.0,
            min_edge_rate_cpd: 0.0,
            max_contracts_per_pair: 1,
            max_notional_per_pair: 1.0,
            max_notional_per_cluster: 5.0,
            max_total_notional: 20.0,
            max_concurrent_positions: 5,
            max_book_age_s: 5.0,
            mid_divergence_reject_cents: 40.0,
            econ_twin_max_divergence_cents: 15.0,
            fat_edge_knee_cents: 6.0,
            fat_edge_size_factor: 0.5,
            skip_dear_led_weather: true,
            assume_sports_settled: false,
            assume_econ_settled: false,
            max_days_to_event: 2.0,
            max_recovery_spread_ratio: 0.0,
            aggressive_second_leg: false,
            kalshi_void_window_days: 2.0,
            postpone_poll_s: 60,
            auto_unwind: true,
            leg_fill_timeout_ms: 500,
            entry_cooldown_s: 0,
            require_settle_clean: true,
            discovery_refresh_s: 300,
            enable_scale_in: false,
            enable_reentry: false,
            add_tau_gain: 0.01,
            max_positions_per_slug: 1,
            kill_switch: false,
        };
        let bk = LiveBackend::new(&cfg);
        let intent = OrderIntent {
            venue: Venue::Kalshi,
            market: "KXHIGHNY-26JUN11-T95".into(),
            action: Action::Buy,
            side: Side::No,
            price_cents: 14,
            qty: 1,
            client_order_id: "coid-1".into(),
        };
        let body = bk.build_kalshi_payload(&intent);
        assert!(body.contains("\"action\":\"buy\"") && body.contains("\"side\":\"no\"") && body.contains("\"count\":1"));
        // a NO leg prices via `no_price` (its own side), NOT `yes_price` — the pmus-class side-pricing fix.
        assert!(body.contains("\"no_price\":14") && !body.contains("yes_price"), "NO leg must use no_price: {body}");
        // FOK on entry BUYs (2026-06-15 incident fix): a kill-on-no-fill makes "not filled" TERMINAL — no
        // late fill of a resting GTC limit -> no untracked naked position.
        assert!(body.contains("\"time_in_force\":\"fill_or_kill\""), "entry BUY must be FOK: {body}");
        // a SELL (recovery/unwind flatten of a KNOWN leg) deliberately stays GTC — no FOK field.
        let sell = OrderIntent { action: Action::Sell, ..intent.clone() };
        let sbody = bk.build_kalshi_payload(&sell);
        assert!(!sbody.contains("time_in_force"), "a SELL flatten stays GTC (no FOK): {sbody}");
        assert!(bk.kalshi_base().contains("demo")); // sandbox default
        // no signing keys loaded in the sandbox (KALSHI_RW_KEY_PATH empty) -> both legs refuse to send.
        let r = bk.submit_pair(&intent, &intent);
        assert_eq!(r.a, Err(ExecError::KeysUnavailable)); // never sends without keys
        assert!(!r.both_filled());
    }

    #[test]
    fn pmus_payload_is_well_formed() {
        let cfg = Config {
            mode: crate::config::ExecutionMode::Live,
            venue_env: VenueEnv::Demo,
            kalshi_key_path: String::new(),
            pmus_env_path: String::new(),
            edge_floor_cents: 2.0,
            min_edge_rate_cpd: 0.0,
            max_contracts_per_pair: 1,
            max_notional_per_pair: 1.0,
            max_notional_per_cluster: 5.0,
            max_total_notional: 20.0,
            max_concurrent_positions: 5,
            max_book_age_s: 5.0,
            mid_divergence_reject_cents: 40.0,
            econ_twin_max_divergence_cents: 15.0,
            fat_edge_knee_cents: 6.0,
            fat_edge_size_factor: 0.5,
            skip_dear_led_weather: true,
            assume_sports_settled: false,
            assume_econ_settled: false,
            max_days_to_event: 2.0,
            max_recovery_spread_ratio: 0.0,
            aggressive_second_leg: false,
            kalshi_void_window_days: 2.0,
            postpone_poll_s: 60,
            auto_unwind: true,
            leg_fill_timeout_ms: 500,
            entry_cooldown_s: 0,
            require_settle_clean: true,
            discovery_refresh_s: 300,
            enable_scale_in: false,
            enable_reentry: false,
            add_tau_gain: 0.01,
            max_positions_per_slug: 1,
            kill_switch: false,
        };
        let bk = LiveBackend::new(&cfg);
        let intent = OrderIntent {
            venue: Venue::Pmus,
            market: "tc-temp-nychigh-2026-06-11-gte95f".into(),
            action: Action::Buy,
            side: Side::Yes,
            price_cents: 7,
            qty: 2,
            client_order_id: "coid-pm".into(),
        };
        // VERIFIED `/v1/orders` shape (live-verified 2026-06-11): marketSlug + intent + Amount price.
        let v: serde_json::Value = serde_json::from_str(&bk.build_pmus_payload(&intent)).unwrap();
        assert_eq!(v["marketSlug"], "tc-temp-nychigh-2026-06-11-gte95f");
        assert_eq!(v["intent"], "ORDER_INTENT_BUY_LONG"); // Buy+Yes
        assert_eq!(v["type"], "ORDER_TYPE_LIMIT");
        assert_eq!(v["quantity"], 2);
        // FIX 1: synchronousExecution makes the create BLOCK so the response can report a real fill state.
        assert_eq!(v["synchronousExecution"], true, "pmus create must request synchronous execution");
        // maxBlockTime is a STRING per the schema; leg_fill_timeout_ms=500 -> floor to 0s -> min 1s.
        assert_eq!(v["maxBlockTime"], "1", "sub-1s timeout floors up to the 1s minimum block");
        // FIX 2: pmus has NO idempotency key — the payload must NOT carry a clientOrderId (silently dropped).
        assert!(v.get("clientOrderId").is_none(), "pmus payload must not send a clientOrderId: {v}");
        // FOK on entry BUYs (2026-06-15 incident fix): an unfilled clip is killed at the venue inside the
        // synchronousExecution block -> "not filled" is TERMINAL, no resting GTC order fills later unhedged.
        assert_eq!(v["tif"], "TIME_IN_FORCE_FILL_OR_KILL", "pmus entry BUY must be FOK");
        assert_eq!(v["price"]["value"], "0.07"); // YES leg: price is the YES price as-is
        assert_eq!(v["price"]["currency"], "USD");
        // Buy+No -> BUY_SHORT, and CRITICALLY the price is the YES-EQUIVALENT (1 - NO price): a NO leg with
        // price_cents=7 (buy NO at 7c) must send YES price 0.93, NOT 0.07. Sending 0.07 was the bug that
        // executed as a marketable sell-YES @ 7c and opened an unintended short live (2026-06-11).
        let no = OrderIntent { side: Side::No, ..intent.clone() };
        let nv: serde_json::Value = serde_json::from_str(&bk.build_pmus_payload(&no)).unwrap();
        assert_eq!(nv["intent"], "ORDER_INTENT_BUY_SHORT");
        assert_eq!(nv["price"]["value"], "0.93"); // 1 - 0.07 (YES-denominated) — the fix
        assert_eq!(nv["tif"], "TIME_IN_FORCE_FILL_OR_KILL", "a NO entry BUY is also FOK");
        let sell = OrderIntent { action: Action::Sell, side: Side::Yes, ..intent.clone() };
        let sv: serde_json::Value = serde_json::from_str(&bk.build_pmus_payload(&sell)).unwrap();
        assert_eq!(sv["intent"], "ORDER_INTENT_SELL_LONG");
        // a SELL (recovery/unwind flatten of a KNOWN leg) deliberately stays GTC, not FOK.
        assert_eq!(sv["tif"], "TIME_IN_FORCE_GOOD_TILL_CANCEL", "a SELL flatten stays GTC");
        // Sell+No (SELL_SHORT) is also YES-denominated: a NO sell at price_cents=7 -> YES 0.93.
        let sn = OrderIntent { action: Action::Sell, side: Side::No, ..intent.clone() };
        let snv: serde_json::Value = serde_json::from_str(&bk.build_pmus_payload(&sn)).unwrap();
        assert_eq!((snv["intent"].as_str(), snv["price"]["value"].as_str()), (Some("ORDER_INTENT_SELL_SHORT"), Some("0.93")));
        assert_eq!(snv["tif"], "TIME_IN_FORCE_GOOD_TILL_CANCEL", "a NO SELL flatten stays GTC");
    }

    /// CRITICAL C2 regression: a venue-supplied `market`/`coid` containing a `"` (or `\`) must NOT malform
    /// or inject the order body — `serde_json` escapes it and the result re-parses to the LITERAL string
    /// (the old `format!` splice produced invalid JSON / an altered count). Checked on BOTH venue builders.
    #[test]
    fn payload_escapes_quotes_in_untrusted_fields() {
        let cfg = Config {
            mode: crate::config::ExecutionMode::Live,
            venue_env: VenueEnv::Demo,
            kalshi_key_path: String::new(),
            pmus_env_path: String::new(),
            edge_floor_cents: 2.0,
            min_edge_rate_cpd: 0.0,
            max_contracts_per_pair: 1,
            max_notional_per_pair: 1.0,
            max_notional_per_cluster: 5.0,
            max_total_notional: 20.0,
            max_concurrent_positions: 5,
            max_book_age_s: 5.0,
            mid_divergence_reject_cents: 40.0,
            econ_twin_max_divergence_cents: 15.0,
            fat_edge_knee_cents: 6.0,
            fat_edge_size_factor: 0.5,
            skip_dear_led_weather: true,
            assume_sports_settled: false,
            assume_econ_settled: false,
            max_days_to_event: 2.0,
            max_recovery_spread_ratio: 0.0,
            aggressive_second_leg: false,
            kalshi_void_window_days: 2.0,
            postpone_poll_s: 60,
            auto_unwind: true,
            leg_fill_timeout_ms: 500,
            entry_cooldown_s: 0,
            require_settle_clean: true,
            discovery_refresh_s: 300,
            enable_scale_in: false,
            enable_reentry: false,
            add_tau_gain: 0.01,
            max_positions_per_slug: 1,
            kill_switch: false,
        };
        let bk = LiveBackend::new(&cfg);
        // an adversarial slug trying to inject a second field via an unescaped quote + backslash.
        let evil = r#"x","count":9999,"x":"\"#;
        let intent = OrderIntent {
            venue: Venue::Kalshi,
            market: evil.into(),
            action: Action::Buy,
            side: Side::Yes,
            price_cents: 8,
            qty: 1,
            client_order_id: evil.into(),
        };
        // Kalshi: body re-parses, ticker == the literal evil string (quote escaped, no injected count).
        let kbody = bk.build_kalshi_payload(&intent);
        let kv: serde_json::Value = serde_json::from_str(&kbody).expect("kalshi body is valid JSON");
        assert_eq!(kv["ticker"], evil);
        assert_eq!(kv["count"], 1); // the injected "count":9999 did NOT take effect
        assert_eq!(kv["client_order_id"], evil);
        // pmus: same — marketSlug re-parses to the literal evil string, quantity is the real qty.
        let pintent = OrderIntent { venue: Venue::Pmus, ..intent };
        let pbody = bk.build_pmus_payload(&pintent);
        let pv: serde_json::Value = serde_json::from_str(&pbody).expect("pmus body is valid JSON");
        assert_eq!(pv["marketSlug"], evil);
        assert_eq!(pv["quantity"], 1);
    }

    /// WARN D: the pmus LIVE leg is gated behind `PMUS_POST_SIGNING_VERIFIED` (default OFF). Until it's
    /// set to yes/1/true, `pmus_post_signing_verified()` is false so `post_leg` refuses the pmus order
    /// (rather than fire a possibly-mis-signed body that legs out the hedge). All in one test: env-var
    /// state is process-global, so toggling + restoring here avoids racing a parallel test.
    #[test]
    fn pmus_live_leg_gated_behind_signing_env() {
        let prev = std::env::var("PMUS_POST_SIGNING_VERIFIED").ok();
        std::env::remove_var("PMUS_POST_SIGNING_VERIFIED");
        assert!(!pmus_post_signing_verified(), "default OFF -> pmus leg refused");
        std::env::set_var("PMUS_POST_SIGNING_VERIFIED", "yes");
        assert!(pmus_post_signing_verified(), "explicit yes -> unlocked");
        std::env::set_var("PMUS_POST_SIGNING_VERIFIED", "1");
        assert!(pmus_post_signing_verified(), "1 also unlocks");
        std::env::set_var("PMUS_POST_SIGNING_VERIFIED", "no");
        assert!(!pmus_post_signing_verified(), "any other value stays gated");
        // restore the prior process env so a parallel test sees what it expected.
        match prev {
            Some(v) => std::env::set_var("PMUS_POST_SIGNING_VERIFIED", v),
            None => std::env::remove_var("PMUS_POST_SIGNING_VERIFIED"),
        }
    }

    fn ack(filled: bool, simulated: bool) -> Result<Ack, ExecError> {
        Ok(Ack { client_order_id: "c".into(), venue_order_id: "v".into(), filled, simulated })
    }

    /// FIX 1: a Kalshi `resting` (marketable-miss) order is ACCEPTED, not filled -> `filled:false`; an
    /// `executed` order with fill_count >= count is `filled:true`. fill_count comes back as a STRING live
    /// (`"0.00"`), so the parser must read number-or-string. A 0-fill executed body is still not-filled.
    #[test]
    fn kalshi_resting_is_not_filled_executed_is() {
        // REAL current shape (captured LIVE 2026-06-14) — a RESTING order: fill_count_fp "0.00" -> not filled.
        let resting: serde_json::Value = serde_json::from_str(
            r#"{"order":{"status":"resting","fill_count_fp":"0.00","initial_count_fp":"1.00","remaining_count_fp":"1.00"}}"#).unwrap();
        assert!(!kalshi_order_filled(&resting, 1), "a resting order (fill_count_fp 0) is acceptance, not a fill");
        // REAL current shape — an EXECUTED full fill: fill_count_fp "1.00". This is the EXACT body the OLD code
        // (which read only `fill_count`) misread as not-filled live -> the silent-naked-leg bug. Now -> filled.
        let executed: serde_json::Value = serde_json::from_str(
            r#"{"order":{"status":"executed","fill_count_fp":"1.00","initial_count_fp":"1.00","remaining_count_fp":"0.00"}}"#).unwrap();
        assert!(kalshi_order_filled(&executed, 1), "executed + fill_count_fp >= qty is a fill (the RENAMED field)");
        // partial: fill_count_fp 1 but 3 requested -> NOT a full fill.
        let partial: serde_json::Value = serde_json::from_str(r#"{"order":{"fill_count_fp":"1.00"}}"#).unwrap();
        assert!(!kalshi_order_filled(&partial, 3), "fill_count_fp below the requested qty is a partial, not full");
        // LEGACY back-compat: the older `fill_count` field still parses (string or numeric; flat body tolerated).
        let legacy: serde_json::Value = serde_json::from_str(r#"{"order":{"status":"executed","fill_count":"1"}}"#).unwrap();
        assert!(kalshi_order_filled(&legacy, 1), "legacy fill_count field still works (back-compat)");
        let flat: serde_json::Value = serde_json::from_str(r#"{"fill_count":2}"#).unwrap();
        assert!(kalshi_order_filled(&flat, 2), "flat body + numeric legacy fill_count");
    }

    /// F1 (audit gap, [L32] class): `venue_order_id` extraction pinned to the CAPTURED real shapes — Kalshi
    /// nests at `order.order_id`, pmus is top-level `id`. Live-verified by the probe's cancel working; now
    /// unit-pinned so a rename can't silently empty the id (-> a skipped cancel / un-targetable naked-leg recovery).
    #[test]
    fn venue_order_id_from_real_shapes() {
        // REAL Kalshi CreateOrder body (captured live 2026-06-14).
        let k: serde_json::Value = serde_json::from_str(r#"{"order":{"order_id":"40feecd5-3c83-4259-a623-8aa053e1a40b","status":"resting","fill_count_fp":"0.00"}}"#).unwrap();
        assert_eq!(parse_venue_order_id(&k), "40feecd5-3c83-4259-a623-8aa053e1a40b");
        // REAL pmus synchronous CreateOrderResponse: top-level {"id":..,"executions":[..]}.
        let p: serde_json::Value = serde_json::from_str(r#"{"id":"pm-ord-9","executions":[{"lastShares":"1","lastPx":"0.01"}]}"#).unwrap();
        assert_eq!(parse_venue_order_id(&p), "pm-ord-9");
        // camelCase `orderId` tolerated; an error/absent body -> empty (never crashes; surfaces at cancel-time).
        assert_eq!(parse_venue_order_id(&serde_json::json!({"orderId": "x"})), "x");
        assert_eq!(parse_venue_order_id(&serde_json::json!({"error": {"code": "bad"}})), "");
    }

    /// FIX B: pmus fill-detection pinned to the OpenAPI schema field names (orders-schema.json, 2026-06-11).
    /// REALISTIC bodies: the synchronous `CreateOrderResponse` (`{id,executions:[{lastShares,lastPx}]}`) and
    /// the `Order` object (`cumQuantity`/`leavesQuantity`/`state`, wrapped `{"order":{..}}` by GetOrderResponse).
    /// Fully-filled -> true; accepted-but-0 -> false; partial (`cumQuantity < qty`) -> false.
    #[test]
    fn pmus_accepted_without_executions_is_not_filled() {
        // the documented accept-only CreateOrderResponse — id only, no executions -> not filled (core bug guard).
        let accepted: serde_json::Value = serde_json::from_str(r#"{"id":"o1","intent":"ORDER_INTENT_BUY_LONG","outcomeSide":"OUTCOME_SIDE_YES","action":"ORDER_ACTION_BUY"}"#).unwrap();
        assert!(!pmus_order_filled(&accepted, 2), "a bare accept body is not a fill");
        // an explicitly empty executions array is also not a fill.
        let empty_exec: serde_json::Value = serde_json::from_str(r#"{"id":"o1","executions":[]}"#).unwrap();
        assert!(!pmus_order_filled(&empty_exec, 1), "empty executions[] is not a fill");
        // 1) cumQuantity >= requested -> filled (number, per the schema's double type).
        let cum: serde_json::Value = serde_json::from_str(r#"{"id":"o1","cumQuantity":2}"#).unwrap();
        assert!(pmus_order_filled(&cum, 2));
        assert!(!pmus_order_filled(&cum, 3), "cumQuantity below requested is a partial, not full");
        // 2) leavesQuantity == 0 AND cumQuantity > 0 -> filled (the schema's remaining-qty field).
        let leaves: serde_json::Value = serde_json::from_str(r#"{"id":"o1","quantity":2,"cumQuantity":2,"leavesQuantity":0}"#).unwrap();
        assert!(pmus_order_filled(&leaves, 2));
        // leavesQuantity 0 but cumQuantity 0 (a never-filled state) is NOT a fill — acceptance != fill.
        let leaves0: serde_json::Value = serde_json::from_str(r#"{"id":"o1","cumQuantity":0,"leavesQuantity":0}"#).unwrap();
        assert!(!pmus_order_filled(&leaves0, 1), "leaves=0 with cum=0 is not a fill");
        // 3) summed executions.lastShares (STRING per the schema) >= requested -> filled (the live create shape).
        let execs: serde_json::Value = serde_json::from_str(r#"{"id":"o1","executions":[{"lastShares":"1","lastPx":{"value":"0.07","currency":"USD"}},{"lastShares":"1","lastPx":{"value":"0.07","currency":"USD"}}]}"#).unwrap();
        assert!(pmus_order_filled(&execs, 2));
        assert!(!pmus_order_filled(&execs, 3), "summed lastShares below requested is a partial");
        // 4) terminal state ORDER_STATE_FILLED -> filled; ORDER_STATE_PARTIALLY_FILLED -> not a full fill.
        let fill: serde_json::Value = serde_json::from_str(r#"{"id":"o1","state":"ORDER_STATE_FILLED"}"#).unwrap();
        assert!(pmus_order_filled(&fill, 5));
        let partial: serde_json::Value = serde_json::from_str(r#"{"id":"o1","state":"ORDER_STATE_PARTIALLY_FILLED","cumQuantity":2}"#).unwrap();
        assert!(!pmus_order_filled(&partial, 5), "a partial fill is not the full requested qty");
        // the GetOrderResponse `{"order":{..}}` wrapper is unwrapped (an Order with a full cumQuantity -> filled).
        let wrapped: serde_json::Value = serde_json::from_str(r#"{"order":{"cumQuantity":2,"leavesQuantity":0,"state":"ORDER_STATE_FILLED"}}"#).unwrap();
        assert!(pmus_order_filled(&wrapped, 2), "the {{order:..}} wrapper must be unwrapped");
        // remainingQty is still accepted as a defensive alias (a body that happens to carry it).
        let rem: serde_json::Value = serde_json::from_str(r#"{"id":"o1","cumQuantity":2,"remainingQty":0}"#).unwrap();
        assert!(pmus_order_filled(&rem, 2));
    }

    /// W2 (audit gap, [L32] class): pmus fill detection pinned to the EXACT shape captured from a REAL 1¢
    /// BUY_LONG fill (live 2026-06-14). The synchronous CreateOrderResponse carries TWO executions — a NEW
    /// acceptance record (`lastShares:"0.0000"`, nested `order.state ORDER_STATE_NEW`, cumQuantity 0) AND the
    /// actual FILL (`lastShares:"1.0000"`, `order.state ORDER_STATE_FILLED`, cumQuantity 1). `pmus_order_filled`
    /// SUMS the executions' `lastShares` (0.0000 + 1.0000 = 1 >= qty) -> filled. A resting order returns ONLY the
    /// NEW execution (sum 0) -> not filled (fail-safe, no false positive). Shares are 4-decimal STRINGS.
    #[test]
    fn pmus_order_filled_real_captured_fill() {
        let real_fill: serde_json::Value = serde_json::from_str(
            r#"{"id":"AN42KH5W23FV","executions":[
                {"id":"e1","lastShares":"0.0000","lastPx":{"value":"0.0000","currency":"USD"},"order":{"cumQuantity":0,"leavesQuantity":1,"state":"ORDER_STATE_NEW"}},
                {"id":"e2","lastShares":"1.0000","lastPx":{"value":"0.0100","currency":"USD"},"order":{"cumQuantity":1,"leavesQuantity":0,"state":"ORDER_STATE_FILLED"}}
            ]}"#).unwrap();
        assert!(pmus_order_filled(&real_fill, 1), "captured 2-execution NEW+FILLED body (lastShares 0+1=1) IS a fill");
        assert!(!pmus_order_filled(&real_fill, 2), "...but not for qty 2 (only 1 share filled) — fail-safe partial");
        // a RESTING order returns ONLY the NEW execution (lastShares 0) -> sum 0 -> NOT filled (no false positive).
        let resting_only: serde_json::Value = serde_json::from_str(
            r#"{"id":"o","executions":[{"id":"e1","lastShares":"0.0000","order":{"cumQuantity":0,"leavesQuantity":1,"state":"ORDER_STATE_NEW"}}]}"#).unwrap();
        assert!(!pmus_order_filled(&resting_only, 1), "a NEW-only (lastShares 0) body is acceptance, not a fill");
    }

    /// FIX 1: `both_filled()` requires BOTH legs Ok AND filled. One Ok-but-resting (accepted, not filled)
    /// leg means NOT both-filled — the old `is_ok()`-only check wrongly called this a complete hedge.
    #[test]
    fn both_filled_requires_both_legs_actually_filled() {
        assert!(PairAck { a: ack(true, false), b: ack(true, false) }.both_filled(), "both filled -> hedge");
        assert!(!PairAck { a: ack(true, false), b: ack(false, false) }.both_filled(), "one resting leg -> NOT a hedge");
        assert!(!PairAck { a: ack(false, false), b: ack(false, false) }.both_filled(), "both resting -> not filled");
        assert!(!PairAck { a: ack(true, false), b: Err(ExecError::RateLimited) }.both_filled(), "one errored leg -> not filled");
    }

    /// FIX 3: `cancel` builds the right per-venue request — Kalshi DELETEs the order by id in the URL path
    /// (empty body); pmus POSTs `/v1/order/{id}/cancel` with the REQUIRED `{marketSlug}` body. The signing
    /// path mirrors the URL path so the signature covers the real request line.
    #[test]
    fn cancel_builds_correct_per_venue_request() {
        let bk = LiveBackend::new(&crate::config::Config::test_default()); // Demo, no keys
        // Kalshi: DELETE …/portfolio/orders/{id}, no body; signing path carries the id.
        let kt = CancelTarget { venue: Venue::Kalshi, venue_order_id: "ORD-123".into(), market: String::new() };
        let (m, url, body, path) = bk.cancel_request(&kt);
        assert_eq!(m, reqwest::Method::DELETE);
        assert!(url.ends_with("/portfolio/orders/ORD-123"), "Kalshi cancels by id in the URL path: {url}");
        assert!(url.contains("demo"), "uses the configured (demo) venue base");
        assert!(body.is_empty(), "Kalshi cancel has no body");
        assert_eq!(path, "/trade-api/v2/portfolio/orders/ORD-123");
        // pmus: POST /v1/order/{id}/cancel with the marketSlug body required by the schema.
        let pt = CancelTarget { venue: Venue::Pmus, venue_order_id: "pm-9".into(), market: "tc-temp-nychigh-2026-06-11-gte95f".into() };
        let (m, url, body, path) = bk.cancel_request(&pt);
        assert_eq!(m, reqwest::Method::POST);
        assert!(url.ends_with("/v1/order/pm-9/cancel"), "pmus cancel endpoint: {url}");
        let bv: serde_json::Value = serde_json::from_str(&body).unwrap();
        assert_eq!(bv["marketSlug"], "tc-temp-nychigh-2026-06-11-gte95f", "pmus cancel body must carry the marketSlug");
        assert_eq!(path, "/v1/order/pm-9/cancel");
        // no keys loaded in the sandbox -> the actual cancel refuses to send (never silently no-ops).
        assert_eq!(bk.cancel(&kt), Err(ExecError::KeysUnavailable));
    }
}
