use crate::discovery;
use crate::exec;
use crate::types::*;

/// A co-listed pair the live loop tracks. Weather/econ are 1:1 (`kalshi_b = None`). MONEYLINE SPORTS is
/// 2-outcome: `kalshi` = team-A ticker (the team pmus lists as YES), `kalshi_b = Some(team-B ticker)` — BOTH
/// Kalshi books are subscribed and the 2-outcome `game_signal` needs both. A WORLD-CUP outcome is a BINARY
/// pair (`kalshi_b = None`, `soccer = true`) routed through the 1:1 `signal` path. Filled by `discovery`.
#[derive(Clone, Debug)]
pub(crate) struct LivePair {
    pub(crate) slug: String,         // pmus market slug
    pub(crate) kalshi: String,       // Kalshi ticker (team-A ticker for moneyline sports)
    pub(crate) kalshi_b: Option<String>, // MONEYLINE SPORTS: team-B (away) Kalshi ticker; None for weather/econ/WC
    pub(crate) cat: Cat,
    pub(crate) cluster: String,
    pub(crate) settle_clean: bool,
    /// WORLD-CUP per-outcome marker (see `discovery::Pair::soccer`): a binary `Cat::Sports` pair routed via
    /// `signal`. Lets the loop skip enrolling a WC pair in the MLB-only postponement poll (no statsapi source).
    pub(crate) soccer: bool,
    pub(crate) days_to_event: Option<f64>,
    /// pmus per-market order constraints (FIX C): price tick the pmus leg must be a multiple of, and the
    /// minimum order qty pmus accepts. `None` -> the leg builder leaves the price unquantized / skips the
    /// min-qty check. Kalshi is integer-cent + whole-share, so these only gate the pmus leg.
    pub(crate) pm_min_tick: Option<f64>,
    pub(crate) pm_min_qty: Option<f64>,
}

impl LivePair {
    /// Every Kalshi ticker this pair subscribes (team-A always; team-B for sports). Drives `by_ticker`
    /// registration, `k_tracked`, and book freeing on prune — so a sports pair tracks BOTH books.
    pub(crate) fn kalshi_tickers(&self) -> Vec<String> {
        let mut v = vec![self.kalshi.clone()];
        if let Some(b) = &self.kalshi_b {
            v.push(b.clone());
        }
        v
    }
}

impl From<discovery::Pair> for LivePair {
    fn from(p: discovery::Pair) -> Self {
        LivePair { slug: p.slug, kalshi: p.kalshi, kalshi_b: p.kalshi_b, cat: p.cat, cluster: p.cluster, settle_clean: p.settle_clean, soccer: p.soccer, days_to_event: p.days_to_event, pm_min_tick: p.pm_min_tick, pm_min_qty: p.pm_min_qty }
    }
}

/// Shared, mutable pair state the event loop READS and the refresh task MUTATES (add new pairs / drop
/// settled). `by_slug` is the authoritative pair record; `by_ticker` indexes EVERY Kalshi ticker (both
/// teams for a sports pair) back to the pmus slug, so a frame on either Kalshi book finds its pair.
#[derive(Default)]
pub(crate) struct PairState {
    // `Arc<LivePair>` so the event loop's per-frame `by_slug.get(&slug).cloned()` is a refcount bump, not a
    // deep clone of the record (several Strings) on every book frame. Pairs are immutable once inserted
    // (the refresh task inserts/removes whole records, never mutates a field), so sharing is sound.
    pub(crate) by_slug: std::collections::HashMap<String, std::sync::Arc<LivePair>>,
    pub(crate) by_ticker: std::collections::HashMap<String, String>, // Kalshi ticker -> pmus slug
}

impl PairState {
    pub(crate) fn insert(&mut self, p: LivePair) {
        for tk in p.kalshi_tickers() {
            self.by_ticker.insert(tk, p.slug.clone());
        }
        self.by_slug.insert(p.slug.clone(), std::sync::Arc::new(p));
    }
    pub(crate) fn remove(&mut self, slug: &str) {
        if let Some(p) = self.by_slug.remove(slug) {
            for tk in p.kalshi_tickers() {
                self.by_ticker.remove(&tk);
            }
        }
    }
}

/// Which kind of submission an outcome belongs to (Entry opens a position; Unwind flattens a held pair;
/// Recovery is the single-leg flatten of a naked leg from a half-filled entry — FIX A).
#[derive(Clone, Copy, Debug, PartialEq)]
pub(crate) enum SubmitKind {
    Entry,
    Unwind,
    Recovery,
}

/// WHAT holds a slug's in-flight `flattening` slot (W-1). The bare `HashSet<slug>` couldn't distinguish a
/// RECOVERY (the single-leg flatten of THIS slug's one naked leg) from an UNWIND (a postpone flatten that
/// targets only the FRONT held leg `legs[0]`). That conflation let an unwind-held slot make a concurrently
/// half-filled ADD's recovery short-circuit as "already covered" — but the unwind does NOT cover the add's
/// just-naked leg, so the add leg was silently abandoned. Recording the kind lets `recover_naked_leg` treat
/// ONLY a recovery-held slot as covering this naked leg; an unwind-held slot fails CLOSED (halt). Both kinds
/// still de-dup a second concurrent flatten on the slug (the slot-occupied check is kind-agnostic).
#[derive(Clone, Copy, Debug, PartialEq)]
pub(crate) enum FlatKind {
    Recovery,
    Unwind,
}

/// The result of a SPAWNED `submit_pair`, sent back to the event loop so ALL position/exposure bookkeeping
/// happens on the loop's own turn (never on the network task). Decouples the two-leg RTT from the
/// `select!` so the unwind arm stays hot while an entry is in flight (concurrency-core fix C4/C5/W14/W16).
pub(crate) struct SubmitOutcome {
    pub(crate) slug: String,
    pub(crate) kind: SubmitKind,
    pub(crate) ack: exec::PairAck,
    /// Entry only: the position to RECORD if both legs filled (else the reservation is released). `None`
    /// for an unwind (which REMOVES an existing position on a both-filled flatten).
    pub(crate) position: Option<Position>,
    /// Entry only: the pair record (the poll needs its league/date/abbrevs to match statsapi). `None` for unwind.
    pub(crate) pair: Option<LivePair>,
    /// Entry only: the per-contract cost the exposure reservation used (so a release decrements the exact amount).
    pub(crate) cost_per: f64,
    /// Entry only: the gated edge's net + direction, STORED on the appended `HeldLeg` so a later add can gate
    /// on `max(entry_net)+tau` and same-direction (design §1/§2). Ignored for unwind/recovery (0.0 / a dummy).
    pub(crate) entry_net: f64,
    pub(crate) entry_dir: Dir,
}

/// Poison-tolerant `Mutex::lock` for the event-loop path (C1): a thread that panicked WHILE holding a lock
/// poisons it; `unwrap()` would then cascade-panic the loop. We recover the guard instead — one bad task
/// must not take down the trading loop. (The data behind the lock is plain book/exposure state; a partial
/// write is at worst a stale read the gates already tolerate, not a safety invariant.)
pub(crate) fn lock<T>(m: &std::sync::Mutex<T>) -> std::sync::MutexGuard<'_, T> {
    m.lock().unwrap_or_else(|e| e.into_inner())
}

/// Every Kalshi ticker a freshly-discovered pair subscribes (team-A always; team-B for sports). Mirrors
/// `LivePair::kalshi_tickers` for the pre-`LivePair` `discovery::Pair` the refresh task iterates.
pub(crate) fn pair_tickers(p: &discovery::Pair) -> Vec<String> {
    let mut v = vec![p.kalshi.clone()];
    if let Some(b) = &p.kalshi_b {
        v.push(b.clone());
    }
    v
}

#[cfg(test)]
mod tests {
    use super::*;

    /// `LivePair` carries discovery's settle_clean + the two-ticker sports shape through unchanged so the
    /// risk gate / book wiring is fed the right values per category.
    #[test]
    fn livepair_from_discovery_preserves_settle_clean_and_tickers() {
        let wx = discovery::Pair { slug: "tc-temp-x-2026-06-11-gte95f".into(), kalshi: "K".into(), kalshi_b: None, cat: Cat::Weather, cluster: "x".into(), settle_clean: true, soccer: false, days_to_event: Some(0.0), pm_min_tick: None, pm_min_qty: None };
        let ec = discovery::Pair { slug: "urc-x".into(), kalshi: "K2".into(), kalshi_b: None, cat: Cat::Econ, cluster: "u3-26JUN".into(), settle_clean: false, soccer: false, days_to_event: None, pm_min_tick: None, pm_min_qty: None };
        let sp = discovery::Pair { slug: "aec-mlb-lad-pit-2026-06-16".into(), kalshi: "K-LAD".into(), kalshi_b: Some("K-PIT".into()), cat: Cat::Sports, cluster: "mlb-2026-06-16".into(), settle_clean: false, soccer: false, days_to_event: Some(1.0), pm_min_tick: None, pm_min_qty: None };
        // a WORLD-CUP discovery Pair is binary (kalshi_b=None) + soccer=true + settle_clean=true; it carries
        // those through to LivePair unchanged so the loop routes it binary and trades it (regulation-clean).
        let wc = discovery::Pair { slug: "atc-fwc-ger-cuw-2026-06-14-ger".into(), kalshi: "KXWCGAME-26JUN14GERCUW-GER".into(), kalshi_b: None, cat: Cat::Sports, cluster: "fwc-ger-cuw-2026-06-14".into(), settle_clean: true, soccer: true, days_to_event: Some(1.0), pm_min_tick: None, pm_min_qty: None };
        assert!(LivePair::from(wx).settle_clean);
        assert!(!LivePair::from(ec).settle_clean);
        // the WC pair routes BINARY (kalshi_b=None) yet is flagged soccer + settle_clean.
        let wc_lp = LivePair::from(wc);
        assert!(wc_lp.kalshi_b.is_none() && wc_lp.soccer && wc_lp.settle_clean);
        assert_eq!(wc_lp.kalshi_tickers(), vec!["KXWCGAME-26JUN14GERCUW-GER".to_string()], "WC subscribes ONE Kalshi ticker per outcome");
        // a sports LivePair subscribes BOTH team tickers (kalshi_tickers / pair_tickers parity).
        let sp_lp = LivePair::from(sp.clone());
        assert_eq!(sp_lp.kalshi_tickers(), vec!["K-LAD".to_string(), "K-PIT".to_string()]);
        assert_eq!(pair_tickers(&sp), vec!["K-LAD".to_string(), "K-PIT".to_string()]);
    }

    /// PairState registers BOTH sports tickers in `by_ticker` -> slug, and `remove` frees both (so a
    /// frame on either team's Kalshi book finds the pair, and a pruned pair leaves no dangling index).
    #[test]
    fn pairstate_indexes_and_frees_both_sports_tickers() {
        let mut ps = PairState::default();
        ps.insert(LivePair { slug: "aec-mlb-lad-pit-2026-06-16".into(), kalshi: "K-LAD".into(), kalshi_b: Some("K-PIT".into()), cat: Cat::Sports, cluster: "mlb-2026-06-16".into(), settle_clean: false, soccer: false, days_to_event: Some(1.0), pm_min_tick: None, pm_min_qty: None });
        assert_eq!(ps.by_ticker.get("K-LAD").map(String::as_str), Some("aec-mlb-lad-pit-2026-06-16"));
        assert_eq!(ps.by_ticker.get("K-PIT").map(String::as_str), Some("aec-mlb-lad-pit-2026-06-16"));
        ps.remove("aec-mlb-lad-pit-2026-06-16");
        assert!(!ps.by_ticker.contains_key("K-LAD") && !ps.by_ticker.contains_key("K-PIT"));
        assert!(ps.by_slug.is_empty());
    }
}
