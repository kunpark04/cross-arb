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
    pub simulated: bool,
}

#[derive(Clone, Debug, PartialEq)]
pub enum ExecError {
    LiveDisabled,      // submit() reached in DryRun mode (caller bug)
    TransportNotWired, // could not drive the async transport (no runtime available) — by design in odd hosts
    KeysUnavailable,   // live POST attempted but signing keys aren't loaded (Claude sandbox / dry-run build)
    Rejected(String),  // venue rejected the order (non-2xx) — carries status + body head
    RateLimited,
}

/// Result of firing a hedged PAIR — one ack per leg. A one-legged result is the naked-leg risk the
/// (stage-2) unwind logic must handle.
#[derive(Clone, Debug, PartialEq)]
pub struct PairAck {
    pub a: Result<Ack, ExecError>,
    pub b: Result<Ack, ExecError>,
}
impl PairAck {
    pub fn both_filled(&self) -> bool {
        self.a.is_ok() && self.b.is_ok()
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
    fn cancel(&self, client_order_id: &str) -> Result<(), ExecError>;
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
        Ok(Ack {
            client_order_id: intent.client_order_id.clone(),
            venue_order_id: "SIMULATED".into(),
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
    fn cancel(&self, _client_order_id: &str) -> Result<(), ExecError> {
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

impl LiveBackend {
    pub fn new(cfg: &Config) -> Self {
        let keys = Self::load_keys(cfg); // None when env/keys absent -> live POST returns a typed error
        LiveBackend {
            venue_env: cfg.venue_env,
            kalshi_key_path: cfg.kalshi_key_path.clone(),
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
    pub fn pmus_base(&self) -> &'static str {
        "https://api.polymarket.us"
    }

    /// Build the venue-native CreateOrder body (real). A limit order; the venue dedupes on
    /// `client_order_id` so a transport retry can't double-fire (idempotency). Built with `serde_json`
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
        serde_json::json!({
            "action": action,
            "side": side,
            "ticker": intent.market,
            "count": intent.qty,
            "type": "limit",
            "yes_price": intent.price_cents,
            "client_order_id": intent.client_order_id,
        })
        .to_string()
    }

    /// Build a pmus CreateOrder body — the VERIFIED shape for `POST /v1/orders`
    /// (docs.polymarket.us/api-reference/orders/create-order). LIVE-VERIFIED against the real venue
    /// 2026-06-11: a 1¢ `BUY_LONG` and a 1¢ `BUY_SHORT` each placed (`[200] {"id":..}`) + cancelled.
    /// (The prior `{slug,action,side,size,price}` @ `/v1/portfolio/orders` was a guess and 404'd.)
    /// `(action, side)` maps to the `OrderIntent` enum on the SAME `marketSlug`: Buy-YES=`BUY_LONG`,
    /// Buy-NO=`BUY_SHORT` (both live-verified — the bot's two ENTRY directions); Sell-YES=`SELL_LONG`,
    /// Sell-NO=`SELL_SHORT` (doc-derived, used only by the unwind path — verify before relying on them).
    /// Built with `serde_json` (never `format!`) so a `"`/`\` in the venue slug/coid is escaped, not spliced.
    pub fn build_pmus_payload(&self, intent: &OrderIntent) -> String {
        let order_intent = match (intent.action, intent.side) {
            (Action::Buy, Side::Yes) => "ORDER_INTENT_BUY_LONG",   // live-verified
            (Action::Buy, Side::No) => "ORDER_INTENT_BUY_SHORT",   // live-verified
            (Action::Sell, Side::Yes) => "ORDER_INTENT_SELL_LONG", // doc-derived (unwind only)
            (Action::Sell, Side::No) => "ORDER_INTENT_SELL_SHORT", // doc-derived (unwind only)
        };
        // price is a 2dp dollar STRING inside the {value,currency} Amount object; quantity a bare number.
        let value = format!("{:.2}", (intent.price_cents as f64) / 100.0);
        serde_json::json!({
            "marketSlug": intent.market,
            "intent": order_intent,
            "type": "ORDER_TYPE_LIMIT",
            "price": {"value": value, "currency": "USD"},
            "quantity": intent.qty,
            "tif": "TIME_IN_FORCE_GOOD_TILL_CANCEL",
            "clientOrderId": intent.client_order_id,
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
                // VERIFIED endpoint (was the guessed `/v1/portfolio/orders`, which 404'd). Cancel (stage-2
                // wiring) is POST `/v1/order/{orderId}/cancel` with a `{marketSlug}` body (also verified).
                let path = "/v1/orders";
                let h = crate::auth::pmus_headers(&keys.pmus_ed25519, &keys.pmus_access_key, ts, "POST", path);
                (format!("{}{}", self.pmus_base(), path), self.build_pmus_payload(intent), h)
            }
        };
        let mut req = self.http.post(&url).header("Content-Type", "application/json");
        for (k, v) in hdrs {
            req = req.header(k, v);
        }
        let resp = req.body(body).send().await.map_err(|e| {
            if e.is_timeout() {
                ExecError::RateLimited
            } else {
                ExecError::Rejected(format!("transport: {e}"))
            }
        })?;
        let status = resp.status();
        let text = resp.text().await.unwrap_or_default();
        if status == reqwest::StatusCode::TOO_MANY_REQUESTS {
            return Err(ExecError::RateLimited);
        }
        if !status.is_success() {
            return Err(ExecError::Rejected(format!("{} {}", status.as_u16(), text.chars().take(160).collect::<String>())));
        }
        // parse the venue order id out of the ack — VERIFIED live 2026-06-11 (Kalshi: {"order":{"order_id":..}}
        // parsed from a real placed order; pmus: top-level {"id":..} from a real BUY_LONG/BUY_SHORT).
        let v: serde_json::Value = serde_json::from_str(&text).unwrap_or(serde_json::Value::Null);
        let venue_order_id = v
            .get("order")
            .and_then(|o| o.get("order_id"))
            .or_else(|| v.get("orderId"))
            .or_else(|| v.get("id"))
            .and_then(|x| x.as_str())
            .unwrap_or("")
            .to_string();
        Ok(Ack {
            client_order_id: intent.client_order_id.clone(),
            venue_order_id,
            simulated: false,
        })
    }

    /// Drive the two concurrent signed POSTs to completion, returning when BOTH ack (or error). Runs the
    /// async join off the ambient tokio runtime (block_in_place when inside one; a tiny current-thread
    /// runtime otherwise) — keeping `submit_pair` synchronous + the trait dyn-compatible.
    fn run_pair(&self, a: &OrderIntent, b: &OrderIntent) -> PairAck {
        let fut = async {
            // CONCURRENT — never serial: serial legging ~doubles effective latency (161ms vs ~86ms p50).
            let (ra, rb) = tokio::join!(self.post_leg(a), self.post_leg(b));
            PairAck { a: ra, b: rb }
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
    fn cancel(&self, client_order_id: &str) -> Result<(), ExecError> {
        // Cancel endpoints VERIFIED live 2026-06-11: Kalshi `DELETE /trade-api/v2/portfolio/orders/{order_id}`
        // (200) and pmus `POST /v1/order/{orderId}/cancel` with a `{marketSlug}` body (200). What's still
        // unbuilt is the venue-order-id TRACKING (the ack's id isn't yet stored per position), so this
        // refuses loudly rather than pretend — wiring the id store is the remaining cancel work.
        let _ = client_order_id;
        Err(ExecError::Rejected("cancel needs venue order_id (stage-2 leg tracking)".into()))
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
            kalshi_void_window_days: 2.0,
            postpone_poll_s: 60,
            auto_unwind: true,
            leg_fill_timeout_ms: 500,
            require_settle_clean: true,
            discovery_refresh_s: 300,
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
            kalshi_void_window_days: 2.0,
            postpone_poll_s: 60,
            auto_unwind: true,
            leg_fill_timeout_ms: 500,
            require_settle_clean: true,
            discovery_refresh_s: 300,
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
        assert_eq!(v["price"]["value"], "0.07");
        assert_eq!(v["price"]["currency"], "USD");
        // Buy+No maps to BUY_SHORT (the other live-verified ENTRY direction); Sell maps to the SELL_* intents.
        let no = OrderIntent { side: Side::No, ..intent.clone() };
        assert_eq!(serde_json::from_str::<serde_json::Value>(&bk.build_pmus_payload(&no)).unwrap()["intent"], "ORDER_INTENT_BUY_SHORT");
        let sell = OrderIntent { action: Action::Sell, side: Side::Yes, ..intent.clone() };
        assert_eq!(serde_json::from_str::<serde_json::Value>(&bk.build_pmus_payload(&sell)).unwrap()["intent"], "ORDER_INTENT_SELL_LONG");
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
            kalshi_void_window_days: 2.0,
            postpone_poll_s: 60,
            auto_unwind: true,
            leg_fill_timeout_ms: 500,
            require_settle_clean: true,
            discovery_refresh_s: 300,
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
}
