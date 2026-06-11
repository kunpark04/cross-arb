//! Execution backends.
//!
//! `DryRunBackend` is the DEFAULT and never touches the network — it logs the intent and returns a
//! simulated ack. `LiveBackend` builds the REAL venue order payload, but the signed HTTPS POST is the
//! one thing wired + run in the **owner's** environment (stage 2, with the read-write key): Claude's
//! sandbox blocks real-money submission, so `LiveBackend::submit` returns `TransportNotWired` here by
//! design. This is the deliberate seam from decision 0015 — the decision/gating logic is real and
//! testable; only the final send crosses into the owner's hands.

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
    TransportNotWired, // stage-2 signed POST not present in this build (by design)
    Rejected(String),  // venue rejected the order
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

pub trait ExecutionBackend {
    /// Fire BOTH legs of a hedged pair. The unit of execution is the PAIR — callers must NEVER
    /// serialize the legs: serial legging ~doubles effective latency (measured serial floor p50 148 ms
    /// vs ~86 ms concurrent). The LIVE backend fires them CONCURRENTLY over two warm, pre-authed
    /// connections (stage-2, `tokio::join!`); the dry-run backend logs both. This pair-shaped signature
    /// exists NOW so stage-2 can't accidentally harden a serial single-leg path — it's the single
    /// highest-leverage latency item from the rust review, and the only latency lever the code controls.
    fn submit_pair(&mut self, a: &OrderIntent, b: &OrderIntent) -> PairAck;
    fn cancel(&mut self, client_order_id: &str) -> Result<(), ExecError>;
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
    fn submit_pair(&mut self, a: &OrderIntent, b: &OrderIntent) -> PairAck {
        // both legs logged together — mirrors the concurrent live fire.
        PairAck {
            a: self.log_leg(a),
            b: self.log_leg(b),
        }
    }
    fn cancel(&mut self, _client_order_id: &str) -> Result<(), ExecError> {
        Ok(())
    }
    fn label(&self) -> &'static str {
        "dry-run"
    }
}

/// Real order path. Order CONSTRUCTION is real and unit-testable; the signed network POST is wired
/// in the owner's environment (stage 2) using the read-write key referenced by `kalshi_key_path`.
pub struct LiveBackend {
    pub venue_env: VenueEnv,
    pub kalshi_key_path: String,
}

impl LiveBackend {
    pub fn new(cfg: &Config) -> Self {
        LiveBackend {
            venue_env: cfg.venue_env,
            kalshi_key_path: cfg.kalshi_key_path.clone(),
        }
    }

    /// The Kalshi REST base for the configured venue env. Demo (sandbox) is the staged-rollout default.
    pub fn kalshi_base(&self) -> &'static str {
        match self.venue_env {
            VenueEnv::Demo => "https://demo-api.kalshi.co/trade-api/v2",
            VenueEnv::Prod => "https://api.elections.kalshi.com/trade-api/v2",
        }
    }

    /// Build the venue-native CreateOrder body (real). A limit order; the venue dedupes on
    /// `client_order_id` so a transport retry can't double-fire (idempotency).
    pub fn build_kalshi_payload(&self, intent: &OrderIntent) -> String {
        let side = match intent.side {
            Side::Yes => "yes",
            Side::No => "no",
        };
        format!(
            "{{\"action\":\"buy\",\"side\":\"{}\",\"ticker\":\"{}\",\"count\":{},\"type\":\"limit\",\"yes_price\":{},\"client_order_id\":\"{}\"}}",
            side, intent.market, intent.qty, intent.price_cents, intent.client_order_id
        )
    }
}

impl ExecutionBackend for LiveBackend {
    fn submit_pair(&mut self, a: &OrderIntent, b: &OrderIntent) -> PairAck {
        let _payload_a = self.build_kalshi_payload(a);
        let _payload_b = self.build_kalshi_payload(b);
        // STAGE 2 (owner env): fire BOTH legs CONCURRENTLY — `tokio::join!(post(a), post(b))` — each
        // RSA-PSS (Kalshi) / Ed25519 (pmus) signed over its own warm, pre-authed connection, returning
        // when both ack. This concurrency is the one latency lever the code owns (serial 148 ms ->
        // concurrent ~86 ms p50). Claude's sandbox blocks real submission, so both legs return
        // TransportNotWired here by design.
        PairAck {
            a: Err(ExecError::TransportNotWired),
            b: Err(ExecError::TransportNotWired),
        }
    }
    fn cancel(&mut self, _client_order_id: &str) -> Result<(), ExecError> {
        Err(ExecError::TransportNotWired)
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
        let mut bk = DryRunBackend;
        let a = OrderIntent {
            venue: Venue::Kalshi,
            market: "KXHIGHNY-26JUN11-T95".into(),
            side: Side::Yes,
            price_cents: 8,
            qty: 1,
            client_order_id: "leg-a".into(),
        };
        let b = OrderIntent {
            venue: Venue::Pmus,
            market: "tc-temp-nychigh-2026-06-11-gte95f".into(),
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
            leg_fill_timeout_ms: 500,
            require_settle_clean: true,
            kill_switch: false,
        };
        let mut bk = LiveBackend::new(&cfg);
        let intent = OrderIntent {
            venue: Venue::Kalshi,
            market: "KXHIGHNY-26JUN11-T95".into(),
            side: Side::No,
            price_cents: 14,
            qty: 1,
            client_order_id: "coid-1".into(),
        };
        let body = bk.build_kalshi_payload(&intent);
        assert!(body.contains("\"side\":\"no\"") && body.contains("\"count\":1"));
        assert!(bk.kalshi_base().contains("demo")); // sandbox default
        let r = bk.submit_pair(&intent, &intent);
        assert_eq!(r.a, Err(ExecError::TransportNotWired)); // never sends here
        assert!(!r.both_filled());
    }
}
