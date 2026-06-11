//! Co-listed market matcher — given a pmus slug, find its settlement-IDENTICAL Kalshi twin and the
//! metadata needed to build a [`Quote`] (`cat`, `cluster`, `settle_clean`, `days_to_event`).
//!
//! Port of the identity-critical JOIN logic in `bot/colisted_map.py` (the no-false-positive invariant,
//! L1). The three families each have a different identity key — getting the INEQUALITY/BOUNDS semantics
//! wrong silently manufactures phantom edges (L17 weather, L21 econ), so those are ported verbatim and
//! pinned with tests, including the exact off-by-one the project was bitten by:
//!   - WEATHER: pair on IDENTICAL canonical INCLUSIVE `(lo, hi)` degF bounds, never a sorted-index zip.
//!     pmus tails encode the boundary exclusively; Kalshi tails too (`floor+1`/`cap-1`); middles are
//!     inclusive `[floor, cap]`. Canonicalize BOTH to `[lo, hi]` before comparing.
//!   - ECON: pmus `>= T` (inclusive) ↔ Kalshi `Above (T - grid_step)` (strict). The twin floor is
//!     `T - step`, NOT `T` (pairing `floor == T` is off by one bucket = the phantom P(print==T) edge).
//!   - SPORTS: bind on `(league, date, team-abbrev)`; `void_clean=false` for ALL leagues (only a
//!     game that completes on schedule settles identically).
//!
//! Settlement-identity status (`settle_clean`) reflects the project's EMPIRICAL findings: weather is
//! verified clean; sports/econ are rules-verified-only (recon still open), so they map to `false`.

use crate::types::Cat;

/// What a successful co-listing yields: the Kalshi side + everything needed to build a `Quote`.
#[derive(Clone, Debug, PartialEq)]
pub struct ColistedMatch {
    pub cat: Cat,
    /// Kalshi ticker (sports carries TWO — see `kalshi_b`).
    pub kalshi: String,
    /// Second Kalshi ticker for the away team (sports moneyline only).
    pub kalshi_b: Option<String>,
    /// Correlated-exposure cluster key (city-date for weather, game for sports, family-period for econ).
    pub cluster: String,
    /// Settlement identity EMPIRICALLY verified (weather=true; sports/econ rules-only -> false).
    pub settle_clean: bool,
    /// Days until the settlement EVENT, when computable from the slug date and a reference date.
    pub days_to_event: Option<f64>,
}

// =====================================================================================================
// WEATHER — bucket boundary-equality join (port of pm_bounds / kbounds / pair_weather_date).
// =====================================================================================================

/// Inclusive integer degF bounds `(lo, hi)`; `None` = open tail.
pub type Bounds = (Option<i64>, Option<i64>);

/// pmus weather bucket slug -> canonical INCLUSIVE `(lo, hi)`. `gteXltY` = `[X, Y]` (a 2°-wide bucket);
/// `ltY` = `(-inf, Y-1]`; `gteX` = `[X, inf)`. Returns `(None, None)` when no bucket pattern matches.
/// Port of `pm_bounds`.
pub fn pm_bounds(slug: &str) -> Bounds {
    let s = slug.to_ascii_lowercase();
    if let Some((x, y)) = re_two(&s, "gte", "lt") {
        return (Some(x), Some(y)); // gteXltY -> [X, Y]
    }
    if let Some(y) = re_one_after(&s, "-lt", "f") {
        return (None, Some(y - 1)); // ltY -> (-inf, Y-1]
    }
    if let Some(x) = re_one(&s, "gte") {
        return (Some(x), None); // gteX -> [X, inf)
    }
    (None, None)
}

/// Kalshi (floor_strike, cap_strike) -> canonical INCLUSIVE `(lo, hi)`. A LOW tail (cap only) is
/// `(-inf, cap-1]`; a HIGH tail (floor only) is `[floor+1, inf)`; a middle (both) is `[floor, cap]`.
/// Port of `kbounds`.
pub fn k_bounds(floor: Option<i64>, cap: Option<i64>) -> Bounds {
    match (floor, cap) {
        (None, Some(c)) => (None, Some(c - 1)),
        (Some(f), None) => (Some(f + 1), None),
        (f, c) => (f, c),
    }
}

/// Pair one pmus weather bucket to its Kalshi twin: a match exists ONLY when canonical bounds are
/// identical AND not the open `(None, None)` no-pattern sentinel (settlement identity). Returns the
/// matching Kalshi ticker, or `None` (flagged, not paired). Port of `pair_weather_date`'s per-bucket
/// rule — the caller supplies the date's Kalshi buckets as `(ticker, floor, cap)`.
pub fn match_weather(
    pm_slug: &str,
    k_buckets: &[(String, Option<i64>, Option<i64>)],
    city: &str,
    date: &str,
) -> Option<ColistedMatch> {
    let b = pm_bounds(pm_slug);
    if b == (None, None) {
        return None;
    }
    for (ticker, floor, cap) in k_buckets {
        if k_bounds(*floor, *cap) == b {
            return Some(ColistedMatch {
                cat: Cat::Weather,
                kalshi: ticker.clone(),
                kalshi_b: None,
                cluster: format!("{city}-{date}"),
                settle_clean: true, // weather settlement identity is empirically verified
                days_to_event: Some(0.0), // weather settles same-day (~6pm high-lock)
            });
        }
    }
    None
}

// =====================================================================================================
// ECON — grid-step twin join (port of econ_twin / the `>=` branch of econ_colisted).
// =====================================================================================================

/// Kalshi floor_strike of the settlement-IDENTICAL twin of a pmus `>= thr` market: on the print grid,
/// `>= T` == `> T-step`, and Kalshi "Above F" is strict, so the twin has floor `F = T - step`.
/// Port of `econ_twin` (with the same 6dp float-safe rounding).
pub fn econ_twin(thr: f64, step: f64) -> f64 {
    round6(thr - step)
}

/// pmus econ inequality, as parsed from the slug tail (port of `econ_parse`'s `ineq`).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Ineq {
    Ge, // ">=" cumulative (the only orientation with an identical Kalshi twin)
    Le, // "<=" tail — pmus-YES == Kalshi-NO, OPPOSITE orientation (skipped)
    Eq, // "== X" point bucket — no cumulative Kalshi twin (skipped)
    Cat, // categorical (Fed) — label==label, no inequality
}

/// Match a pmus econ market to its Kalshi twin. Only a `>= T` whose `T - step` twin is listed (or a Fed
/// categorical whose label is listed) co-lists; `<=`/`==` are skipped (returns `None`). `k_floors` maps
/// a listed Kalshi `floor_strike` -> ticker; `k_labels` maps a Fed yes_sub_title -> ticker.
/// Port of the `>=` / `cat` branches of `econ_colisted`.
pub fn match_econ(
    ineq: Ineq,
    thr: Option<f64>,
    step: f64,
    fed_label: Option<&str>,
    period: &str,
    family: &str,
    k_floors: &[(f64, String)],
    k_labels: &[(String, String)],
) -> Option<ColistedMatch> {
    let cluster = format!("{family}-{period}");
    match ineq {
        Ineq::Ge => {
            let twin = econ_twin(thr?, step);
            for (floor, ticker) in k_floors {
                if (floor - twin).abs() < 1e-9 {
                    return Some(ColistedMatch {
                        cat: Cat::Econ,
                        kalshi: ticker.clone(),
                        kalshi_b: None,
                        cluster,
                        settle_clean: false, // econ recon still open (rules-verified only)
                        days_to_event: None,
                    });
                }
            }
            None // twin not listed -> NOT co-listed
        }
        Ineq::Cat => {
            let want = fed_canonical_label(fed_label?)?;
            for (label, ticker) in k_labels {
                if label.eq_ignore_ascii_case(want) {
                    return Some(ColistedMatch {
                        cat: Cat::Econ,
                        kalshi: ticker.clone(),
                        kalshi_b: None,
                        cluster,
                        settle_clean: false,
                        days_to_event: None,
                    });
                }
            }
            None
        }
        Ineq::Le | Ineq::Eq => None, // opposite orientation / no cumulative twin -> skip (flagged upstream)
    }
}

/// pmus Fed token -> the Kalshi `yes_sub_title` it must match (port of `_FEDLBL`).
fn fed_canonical_label(tok: &str) -> Option<&'static str> {
    Some(match tok {
        "maintains" => "fed maintains rate",
        "hike25bps" => "hike 25bps",
        "hikegt25bps" => "hike >25bps",
        "cut25bps" => "cut 25bps",
        "cutgt25bps" => "cut >25bps",
        _ => return None,
    })
}

// =====================================================================================================
// SPORTS — (league, date, team-abbrev) binding (port of _match_game's abbrev arm).
// =====================================================================================================

/// Bind a pmus game (two team abbreviations) to ONE Kalshi event's `{abbrev: ticker}` set. Returns the
/// two DISTINCT tickers `(team_a, team_b)` only when both teams resolve to different tickers (the
/// no-false-positive invariant). Port of `_match_game(join="abbrev")`.
pub fn match_sports_abbrev(
    abbrev_a: &str,
    abbrev_b: &str,
    league: &str,
    date: &str,
    k_event: &[(String, String)], // (abbrev, ticker) within ONE Kalshi event
    days_to_event: Option<f64>,
) -> Option<ColistedMatch> {
    let a = abbrev_a.to_ascii_lowercase();
    let b = abbrev_b.to_ascii_lowercase();
    let ta = k_event.iter().find(|(ab, _)| *ab == a).map(|(_, t)| t.clone());
    let tb = k_event.iter().find(|(ab, _)| *ab == b).map(|(_, t)| t.clone());
    match (ta, tb) {
        (Some(ta), Some(tb)) if ta != tb => Some(ColistedMatch {
            cat: Cat::Sports,
            kalshi: ta,
            kalshi_b: Some(tb),
            cluster: format!("{league}-{date}"),
            settle_clean: false, // only a game that COMPLETES on schedule settles identically (void tail)
            days_to_event,
        }),
        _ => None,
    }
}

// ---- small slug helpers (no regex dep; the patterns here are simple fixed-token scans) ----------------

fn round6(x: f64) -> f64 {
    (x * 1.0e6).round() / 1.0e6
}

/// Parse the run of ASCII digits starting at byte index `start`. Returns `(value, end_index)`.
fn digits_at(s: &str, start: usize) -> Option<(i64, usize)> {
    let bytes = s.as_bytes();
    let mut i = start;
    while i < bytes.len() && bytes[i].is_ascii_digit() {
        i += 1;
    }
    if i == start {
        return None;
    }
    s[start..i].parse::<i64>().ok().map(|v| (v, i))
}

/// `gteXltY` style: find `t1` immediately followed by digits, then `t2` immediately followed by digits.
fn re_two(s: &str, t1: &str, t2: &str) -> Option<(i64, i64)> {
    let p1 = s.find(t1)?;
    let (x, after_x) = digits_at(s, p1 + t1.len())?;
    if !s[after_x..].starts_with(t2) {
        return None;
    }
    let (y, _) = digits_at(s, after_x + t2.len())?;
    Some((x, y))
}

/// `gteX` style: first occurrence of `t` followed by digits.
fn re_one(s: &str, t: &str) -> Option<i64> {
    let mut from = 0;
    while let Some(rel) = s[from..].find(t) {
        let p = from + rel;
        if let Some((v, _)) = digits_at(s, p + t.len()) {
            return Some(v);
        }
        from = p + t.len();
    }
    None
}

/// `-ltY` followed by digits then a literal `suffix` (e.g. `f`): the bracketed-tail form `-lt66f`.
fn re_one_after(s: &str, t: &str, suffix: &str) -> Option<i64> {
    let mut from = 0;
    while let Some(rel) = s[from..].find(t) {
        let p = from + rel;
        if let Some((v, end)) = digits_at(s, p + t.len()) {
            if s[end..].starts_with(suffix) {
                return Some(v);
            }
        }
        from = p + t.len();
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;

    // ---- WEATHER: the L17 canonicalization, both venues to inclusive [lo,hi] -------------------------
    #[test]
    fn weather_bounds_canonicalize_both_venues() {
        // middle '64 to 65' — both inclusive.
        assert_eq!(pm_bounds("tc-temp-sfohigh-2026-06-09-gte64lt65f"), (Some(64), Some(65)));
        assert_eq!(k_bounds(Some(64), Some(65)), (Some(64), Some(65)));
        // low tail '63 or below'.
        assert_eq!(pm_bounds("tc-temp-sfohigh-2026-06-09-lt64f"), (None, Some(63)));
        assert_eq!(k_bounds(None, Some(64)), (None, Some(63)));
        // high tail '72 or above'.
        assert_eq!(pm_bounds("tc-temp-sfohigh-2026-06-09-gte72f"), (Some(72), None));
        assert_eq!(k_bounds(Some(71), None), (Some(72), None));
        // shifted -> would mispair (must NOT be equal).
        assert_ne!(pm_bounds("x-gte70lt72f"), k_bounds(Some(68), Some(70)));
        // adversarial digit widths + precedence (gteXltY must win over gteX), checked vs colisted_map.py:
        assert_eq!(pm_bounds("x-gte100lt101f"), (Some(100), Some(101))); // 3-digit
        assert_eq!(pm_bounds("tc-temp-x-gte9lt10f"), (Some(9), Some(10))); // 1- then 2-digit
        assert_eq!(pm_bounds("tc-temp-x-gte72f"), (Some(72), None)); // gteX tail, no lt
        assert_eq!(pm_bounds("aec-mlb-lad-pit-2026-06-09"), (None, None)); // non-weather -> no bucket
    }

    #[test]
    fn weather_matches_identical_bounds_not_index() {
        // offset listing: pmus lists gte64lt65f; Kalshi has an extra low bucket -> still finds the twin.
        let k = vec![
            ("K60".into(), Some(60), Some(61)),
            ("K62".into(), Some(62), Some(63)),
            ("K64".into(), Some(64), Some(65)),
        ];
        let m = match_weather("tc-temp-x-2026-06-09-gte64lt65f", &k, "sfo", "2026-06-09").unwrap();
        assert_eq!(m.kalshi, "K64");
        assert_eq!(m.cat, Cat::Weather);
        assert!(m.settle_clean);
        assert_eq!(m.cluster, "sfo-2026-06-09");
        // a genuinely-missing twin is NOT paired.
        assert!(match_weather("tc-temp-x-2026-06-09-gte70lt71f", &k, "sfo", "2026-06-09").is_none());
    }

    // ---- ECON: the L21 off-by-one twin (floor = T - step, NOT T) -------------------------------------
    #[test]
    fn econ_twin_is_floor_minus_step() {
        assert!((econ_twin(4.4, 0.1) - 4.3).abs() < 1e-9); // U-3 >=4.4 ↔ Above 4.3
        assert!((econ_twin(250000.0, 1000.0) - 249000.0).abs() < 1e-9); // NFP >=250k ↔ Above 249k
        assert!((econ_twin(2.0, 0.1) - 1.9).abs() < 1e-9); // GDP, float-safe
        assert!((econ_twin(4.0, 0.1) - 3.9).abs() < 1e-9);
    }

    #[test]
    fn econ_ge_pairs_the_identical_twin_only() {
        // pmus >=4.4 must bind Kalshi floor 4.3 (twin), NOT floor 4.4 (the phantom off-by-one pair).
        let floors = vec![(4.3_f64, "K-U3-ABOVE43".to_string()), (4.4, "K-U3-ABOVE44".to_string())];
        let m = match_econ(Ineq::Ge, Some(4.4), 0.1, None, "26JUN", "u3", &floors, &[]).unwrap();
        assert_eq!(m.kalshi, "K-U3-ABOVE43", "must bind T-step twin, never floor==T (L21)");
        assert_eq!(m.cat, Cat::Econ);
        assert!(!m.settle_clean); // econ recon still open
        // no listed twin -> not co-listed.
        let only44 = vec![(4.4_f64, "K-U3-ABOVE44".to_string())];
        assert!(match_econ(Ineq::Ge, Some(4.4), 0.1, None, "26JUN", "u3", &only44, &[]).is_none());
    }

    #[test]
    fn econ_le_and_eq_are_skipped() {
        // <= tail (opposite orientation) and == point bucket never co-list.
        assert!(match_econ(Ineq::Le, Some(3.7), 0.1, None, "26MAY", "cpi", &[(3.6, "x".into())], &[]).is_none());
        assert!(match_econ(Ineq::Eq, Some(3.8), 0.1, None, "26MAY", "cpi", &[(3.8, "x".into())], &[]).is_none());
    }

    #[test]
    fn econ_fed_categorical_matches_label() {
        let labels = vec![("fed maintains rate".to_string(), "K-FED-MAINT".to_string())];
        let m = match_econ(Ineq::Cat, None, 0.0, Some("maintains"), "26JUN", "fed", &[], &labels).unwrap();
        assert_eq!(m.kalshi, "K-FED-MAINT");
        assert!(match_econ(Ineq::Cat, None, 0.0, Some("cut25bps"), "26JUN", "fed", &[], &labels).is_none());
    }

    // ---- SPORTS: abbrev binding to two distinct tickers ----------------------------------------------
    #[test]
    fn sports_binds_two_distinct_tickers() {
        let ev = vec![("lad".to_string(), "K-LAD".to_string()), ("pit".to_string(), "K-PIT".to_string())];
        let m = match_sports_abbrev("LAD", "PIT", "mlb", "2026-06-16", &ev, Some(1.0)).unwrap();
        assert_eq!(m.kalshi, "K-LAD");
        assert_eq!(m.kalshi_b.as_deref(), Some("K-PIT"));
        assert_eq!(m.cat, Cat::Sports);
        assert!(!m.settle_clean); // void tail -> never marked clean
        assert_eq!(m.cluster, "mlb-2026-06-16");
        assert_eq!(m.days_to_event, Some(1.0));
        // a team that doesn't resolve -> no false pair.
        assert!(match_sports_abbrev("lad", "sea", "mlb", "2026-06-16", &ev, None).is_none());
    }

    /// A known LIVE pair from the project findings: MLB lad-pit line-lag case (the depth-and-edge proof).
    #[test]
    fn sports_known_live_lad_pit() {
        let ev = vec![("lad".to_string(), "KXMLBGAME-26JUN16-LAD".to_string()),
                      ("pit".to_string(), "KXMLBGAME-26JUN16-PIT".to_string())];
        let m = match_sports_abbrev("lad", "pit", "mlb", "2026-06-16", &ev, Some(0.5)).unwrap();
        assert_eq!(m.kalshi, "KXMLBGAME-26JUN16-LAD");
        assert_eq!(m.kalshi_b.as_deref(), Some("KXMLBGAME-26JUN16-PIT"));
    }
}
