//! PnL accounting + fee model.
//!
//! The cross-arb PnL is **outcome-independent** by construction: buy YES on the cheap venue at
//! `cost_yes` and NO on the dear venue at `cost_no`; exactly one leg pays `$1`, so gross profit per
//! pair = `1 - cost_yes - cost_no` regardless of how the world resolves. `Pnl::book_pair` asserts that
//! self-verifying property (the same invariant `bot/ledger.py` proves).
//!
//! ⚠️ FEES — VERIFY BEFORE LIVE. The coefficients below match `research/fee-pin-2026-06-10.md` and the
//! venue audit, but `bot/ledger.py` remains the source of truth until a parity test asserts these Rust
//! fees reproduce its selftest vectors (a stage-2 task). Do NOT trade real money on the Rust fee path
//! until that parity check is green — a wrong fee silently turns a +EV arb into a loss (L10/L15).

/// Per-contract taker coefficient `f`: fee(dollars) = `f * p * (1-p)` per contract.
/// Kalshi taker 0.07 -> 1.75c at p=0.5; pmus taker 0.05 -> 1.25c at p=0.5 (fee-pin 2026-06-10).
pub const KALSHI_TAKER_COEF: f64 = 0.07;
pub const PMUS_TAKER_COEF: f64 = 0.05;
/// Kalshi maker coef 0.0175 — but $0 on the series with no maker fee (all 5 weather, esports, ITF,
/// UFC). pmus rebates makers. Maker path is stage-2 (the queued maker study); not used by the taker bot.
pub const KALSHI_MAKER_COEF: f64 = 0.0175;

const CEIL_EPS: f64 = 1e-9; // L10: kill float noise at a cent boundary so ceil() can't add a phantom cent

/// At-scale MARGINAL taker fee per contract (no ceil) — used for DETECTION/sizing so nothing that is
/// +EV at size is dropped by the n=1 ceil over-charge (L10/L15). `p` in dollars 0..1.
pub fn marginal_taker_fee(coef: f64, p: f64) -> f64 {
    coef * p * (1.0 - p)
}

/// Per-ORDER taker fee in CENTS, ceiled once for the whole order (size known at booking, L10).
pub fn order_taker_fee_cents(coef: f64, n: u32, p: f64) -> u32 {
    let cents = coef * (n as f64) * p * (1.0 - p) * 100.0;
    (cents - CEIL_EPS).ceil().max(0.0) as u32
}

/// Booked PnL ledger. All amounts in dollars.
#[derive(Default, Debug)]
pub struct Pnl {
    pub pairs: u32,
    pub gross: f64,    // sum of (1 - cost_yes - cost_no) * size
    pub fees: f64,     // sum of both legs' per-order fees
    pub deployed: f64, // capital put to work (cost basis)
}

impl Pnl {
    pub fn new() -> Self {
        Pnl::default()
    }

    /// Book one settled arb pair. Returns the NET profit booked. Panics if the pair isn't actually
    /// outcome-independent at the given prices (a guard against a mis-constructed hedge).
    pub fn book_pair(
        &mut self,
        cost_yes: f64,
        cost_no: f64,
        size: u32,
        yes_fee_cents: u32,
        no_fee_cents: u32,
    ) -> f64 {
        // Input sanity (the old `win_yes == win_no` assert was a tautology — both expressions are
        // algebraically identical, so it could never fire; rust code review caught it). The real guard
        // is that each leg cost is a valid probability; gross is then outcome-independent BY
        // CONSTRUCTION (exactly one leg pays $1, total cost is fixed).
        debug_assert!(
            (0.0..=1.0).contains(&cost_yes) && (0.0..=1.0).contains(&cost_no),
            "leg costs must be valid prices in [0,1]"
        );
        let n = size as f64;
        let gross = (1.0 - cost_yes - cost_no) * n;
        let fees = (yes_fee_cents + no_fee_cents) as f64 / 100.0;
        self.pairs += 1;
        self.gross += gross;
        self.fees += fees;
        self.deployed += (cost_yes + cost_no) * n;
        gross - fees
    }

    pub fn net(&self) -> f64 {
        self.gross - self.fees
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn kalshi_taker_is_175c_per_contract_at_mid() {
        // 100 contracts at p=0.5 -> $1.75 = 175c, ceiled once (L10: not 176).
        assert_eq!(order_taker_fee_cents(KALSHI_TAKER_COEF, 100, 0.5), 175);
        // marginal (per-contract, no ceil) = $0.0175
        assert!((marginal_taker_fee(KALSHI_TAKER_COEF, 0.5) - 0.0175).abs() < 1e-12);
    }

    #[test]
    fn pmus_taker_is_125c_per_contract_at_mid() {
        assert_eq!(order_taker_fee_cents(PMUS_TAKER_COEF, 100, 0.5), 125);
    }

    #[test]
    fn pnl_is_outcome_independent_and_nets_fees() {
        // buy YES @0.75 (pmus) + NO @0.14 (kalshi) = 0.89 cost -> 0.11 gross/contract.
        let mut p = Pnl::new();
        let yf = order_taker_fee_cents(PMUS_TAKER_COEF, 10, 0.75);
        let nf = order_taker_fee_cents(KALSHI_TAKER_COEF, 10, 0.14);
        let net = p.book_pair(0.75, 0.14, 10, yf, nf);
        assert!((p.gross - 1.10).abs() < 1e-9); // 0.11 * 10
        assert!(net < p.gross); // fees subtracted
        assert_eq!(p.pairs, 1);
    }
}
