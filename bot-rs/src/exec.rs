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

pub trait ExecutionBackend {
    fn submit(&mut self, intent: &OrderIntent) -> Result<Ack, ExecError>;
    fn cancel(&mut self, client_order_id: &str) -> Result<(), ExecError>;
    fn label(&self) -> &'static str;
}

/// DEFAULT backend. Logs the intent, returns a simulated ack. NEVER sends.
pub struct DryRunBackend;

impl ExecutionBackend for DryRunBackend {
    fn submit(&mut self, intent: &OrderIntent) -> Result<Ack, ExecError> {
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
    fn submit(&mut self, intent: &OrderIntent) -> Result<Ack, ExecError> {
        let _payload = self.build_kalshi_payload(intent);
        // STAGE 2 (owner env): RSA-PSS sign "{ts}POST{path}{body}" with the read-write key at
        // `kalshi_key_path`, HTTPS POST `_payload` to `{kalshi_base()}/portfolio/orders`, parse the
        // ack. pmus uses the Ed25519 signing scheme + its own order endpoint. Claude's sandbox blocks
        // real submission, so this is intentionally not executed here.
        Err(ExecError::TransportNotWired)
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
    fn dry_run_never_errors_and_simulates() {
        let mut b = DryRunBackend;
        let intent = OrderIntent {
            venue: Venue::Kalshi,
            market: "KXHIGHNY-26JUN11-T95".into(),
            side: Side::Yes,
            price_cents: 8,
            qty: 1,
            client_order_id: "abc".into(),
        };
        let ack = b.submit(&intent).unwrap();
        assert!(ack.simulated);
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
            leg_fill_timeout_ms: 500,
            require_settle_clean: true,
            kill_switch: false,
        };
        let mut b = LiveBackend::new(&cfg);
        let intent = OrderIntent {
            venue: Venue::Kalshi,
            market: "KXHIGHNY-26JUN11-T95".into(),
            side: Side::No,
            price_cents: 14,
            qty: 1,
            client_order_id: "coid-1".into(),
        };
        let body = b.build_kalshi_payload(&intent);
        assert!(body.contains("\"side\":\"no\"") && body.contains("\"count\":1"));
        assert!(b.kalshi_base().contains("demo")); // sandbox default
        assert_eq!(b.submit(&intent), Err(ExecError::TransportNotWired)); // never sends here
    }
}
