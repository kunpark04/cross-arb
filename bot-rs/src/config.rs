//! Bot configuration — **SAFE BY DEFAULT**. Loaded from the environment. Nothing in this file reads,
//! copies, or logs the key material; the read-write key is referenced by PATH and loaded at runtime in
//! the owner's environment (decision 0015 / 0007 least-privilege spirit preserved).

use std::env;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ExecutionMode {
    DryRun,
    Live,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum VenueEnv {
    Demo, // sandbox
    Prod, // real money
}

#[derive(Clone, Debug)]
pub struct Config {
    pub mode: ExecutionMode, // DEFAULT DryRun
    pub venue_env: VenueEnv, // DEFAULT Demo (sandbox even when live)
    pub kalshi_key_path: String, // external path; loaded in OWNER env, never by Claude
    pub pmus_env_path: String,

    // --- risk caps (defaults are the STAGED-ROLLOUT floor: 1 contract, tiny notional) ---
    pub edge_floor_cents: f64,        // 0014 pre-registered floor = 2.0c
    pub max_contracts_per_pair: u32,  // DEFAULT 1
    pub max_notional_per_pair: f64,   // $
    pub max_notional_per_cluster: f64,
    pub max_total_notional: f64,
    pub max_concurrent_positions: u32,

    // --- execution-time guards (the learned edge cases) ---
    pub max_book_age_s: f64,              // L13 staleness
    pub mid_divergence_reject_cents: f64, // L1 bad-join / stale guard (cross-category)
    pub econ_twin_max_divergence_cents: f64, // TIGHTER bound for settlement-identical econ twins
    pub fat_edge_knee_cents: f64,         // edges above this are adversely-selected (~66% toxic, die ~0.5s)
    pub fat_edge_size_factor: f64,        // size multiplier above the knee (1.0 = OFF, to test speed-capture)
    pub skip_dear_led_weather: bool,      // H1 toxicity-direction gate: skip dear-led WEATHER edges (~79% toxic)
    pub assume_sports_settled: bool,      // owner override: treat SPORTS as settlement-reconciled (else gated)
    pub assume_econ_settled: bool,        // owner override: treat ECON as settlement-reconciled (else gated)
    pub max_days_to_event: f64,           // event-proximity gate: skip arbs >this many days before settlement (<=0 = off)
    pub kalshi_void_window_days: f64,     // postponement-unwind: Kalshi voids if reschedule is past this (~2d)
    pub leg_fill_timeout_ms: u64,         // unwind leg A if leg B isn't filled in time
    pub require_settle_clean: bool,       // only trade settlement-verified pairs (econ/sports)
    pub discovery_refresh_s: u64,         // periodic re-discovery interval (monitor.py heartbeat = 300s)

    pub kill_switch: bool, // CROSSARB_KILL=1 -> halt everything
}

impl Config {
    pub fn from_env() -> Config {
        Config {
            mode: match env::var("EXECUTION_MODE").as_deref() {
                Ok("live") | Ok("LIVE") => ExecutionMode::Live,
                _ => ExecutionMode::DryRun, // DEFAULT: never live unless explicitly asked
            },
            venue_env: match env::var("VENUE_ENV").as_deref() {
                Ok("prod") | Ok("PROD") => VenueEnv::Prod,
                _ => VenueEnv::Demo, // DEFAULT: sandbox before production
            },
            kalshi_key_path: env::var("KALSHI_RW_KEY_PATH").unwrap_or_default(),
            pmus_env_path: env::var("PMUS_ENV_PATH").unwrap_or_default(),

            edge_floor_cents: env_f64("EDGE_FLOOR_CENTS", 2.0),
            max_contracts_per_pair: env_u32("MAX_CONTRACTS_PER_PAIR", 1),
            max_notional_per_pair: env_f64("MAX_NOTIONAL_PER_PAIR", 1.0),
            max_notional_per_cluster: env_f64("MAX_NOTIONAL_PER_CLUSTER", 5.0),
            max_total_notional: env_f64("MAX_TOTAL_NOTIONAL", 20.0),
            max_concurrent_positions: env_u32("MAX_CONCURRENT_POSITIONS", 5),

            max_book_age_s: env_f64("MAX_BOOK_AGE_S", 5.0),
            mid_divergence_reject_cents: env_f64("MID_DIVERGENCE_REJECT_CENTS", 40.0),
            econ_twin_max_divergence_cents: env_f64("ECON_TWIN_MAX_DIVERGENCE_CENTS", 15.0),
            fat_edge_knee_cents: env_f64("FAT_EDGE_KNEE_CENTS", 6.0),
            fat_edge_size_factor: env_f64("FAT_EDGE_SIZE_FACTOR", 0.5),
            skip_dear_led_weather: env_bool("SKIP_DEAR_LED_WEATHER", true),
            assume_sports_settled: env_bool("ASSUME_SPORTS_SETTLED", false),
            assume_econ_settled: env_bool("ASSUME_ECON_SETTLED", false),
            max_days_to_event: env_f64("MAX_DAYS_TO_EVENT", 2.0),
            kalshi_void_window_days: env_f64("KALSHI_VOID_WINDOW_DAYS", 2.0),
            leg_fill_timeout_ms: env_u64("LEG_FILL_TIMEOUT_MS", 500),
            require_settle_clean: env_bool("REQUIRE_SETTLE_CLEAN", true),
            discovery_refresh_s: env_u64("DISCOVERY_REFRESH_S", 300),

            kill_switch: env_bool("CROSSARB_KILL", false),
        }
    }

    pub fn is_live(&self) -> bool {
        self.mode == ExecutionMode::Live
    }
    pub fn is_prod(&self) -> bool {
        self.venue_env == VenueEnv::Prod
    }

    /// TEST-ONLY staged-rollout defaults built WITHOUT reading the environment (the real `from_env`
    /// reads process env, which is shared mutable state across parallel tests). Mirrors the per-module
    /// inline literals so cross-module tests (book/discovery) don't re-spell all fields.
    #[cfg(test)]
    pub fn test_default() -> Config {
        Config {
            mode: ExecutionMode::DryRun,
            venue_env: VenueEnv::Demo,
            kalshi_key_path: String::new(),
            pmus_env_path: String::new(),
            edge_floor_cents: 2.0,
            max_contracts_per_pair: 100,
            max_notional_per_pair: 1000.0,
            max_notional_per_cluster: 1000.0,
            max_total_notional: 1000.0,
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
            leg_fill_timeout_ms: 500,
            require_settle_clean: true,
            discovery_refresh_s: 300,
            kill_switch: false,
        }
    }
}

fn env_f64(k: &str, d: f64) -> f64 {
    env::var(k).ok().and_then(|v| v.parse().ok()).unwrap_or(d)
}
fn env_u32(k: &str, d: u32) -> u32 {
    env::var(k).ok().and_then(|v| v.parse().ok()).unwrap_or(d)
}
fn env_u64(k: &str, d: u64) -> u64 {
    env::var(k).ok().and_then(|v| v.parse().ok()).unwrap_or(d)
}
fn env_bool(k: &str, d: bool) -> bool {
    match env::var(k).as_deref() {
        Ok("1") | Ok("true") | Ok("TRUE") => true,
        Ok("0") | Ok("false") | Ok("FALSE") => false,
        _ => d,
    }
}
