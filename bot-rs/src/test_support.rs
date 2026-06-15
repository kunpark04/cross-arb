use crate::pair::LivePair;
use crate::types::*;

pub(crate) fn q_pk() -> Quote {
    // dir PK: cheap = pmus (YES ask 0.07), dear = Kalshi (YES bid 0.10 -> NO ask 0.90).
    Quote {
        market: "tc-temp-nychigh-2026-06-11-gte95f".into(),
        cat: Cat::Weather,
        pm: Book { yes_bid: Some(0.05), yes_ask: Some(0.07), age_s: 0.0 },
        k: Book { yes_bid: Some(0.10), yes_ask: Some(0.11), age_s: 0.0 },
        k_b: None,
        depth: Depth { c2: 40, c1: 50, c0: 60 },
        settle_clean: true,
        cluster: "nychigh-2026-06-11".into(),
        led_by: None,
        days_to_event: None,
        fire_pmus_first: true,
    }
}
pub(crate) fn wx_pair() -> LivePair {
    LivePair {
        slug: "tc-temp-nychigh-2026-06-11-gte95f".into(),
        kalshi: "KXHIGHNY-26JUN11-T95".into(),
        kalshi_b: None,
        cat: Cat::Weather,
        cluster: "nychigh-2026-06-11".into(),
        settle_clean: true,
        soccer: false,
        days_to_event: None,
        pm_min_tick: None,
        pm_min_qty: None,
    }
}

/// A WORLD-CUP outcome LivePair (kalshi_b=None, soccer=true). It builds a clean 1:1 cluster and a
/// BINARY pair builder for the smoke/test.
pub(crate) fn wc_pair() -> LivePair {
    LivePair {
        slug: "atc-fwc-ger-cuw-2026-06-14-ger".into(),
        kalshi: "KXWCGAME-26JUN14GERCUW-GER".into(),
        kalshi_b: None, // per-outcome BINARY
        cat: Cat::Sports,
        cluster: "fwc-ger-cuw-2026-06-14".into(),
        settle_clean: true,
        soccer: true,
        days_to_event: Some(1.0),
        pm_min_tick: None,
        pm_min_qty: None,
    }
}
