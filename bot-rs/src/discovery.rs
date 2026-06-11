//! Co-listed-pair DISCOVERY — the catalog-pull + slug/field PARSE that fills the live loop's `pairs`
//! list. Port of `bot/colisted_map.py::build_colisted_map` (the catalog side); the identity-critical
//! JOIN logic is NOT re-implemented here — it is reused verbatim from [`crate::matcher`] (the
//! no-false-positive invariant, L1, lives in one place).
//!
//! Split, like `venue.rs`, into a PURE layer and an I/O layer:
//!   - PURE (unit-tested against embedded sample catalog JSON, NO network): the slug parsers
//!     (`weather_city`, `econ_parse`, sports team/date extraction, Kalshi ticker→date/bounds/event) and
//!     `assemble`, which takes already-fetched catalog `Value`s and produces the `Vec<Pair>`. This is
//!     where a wrong bucket/inequality/date would manufacture a phantom pair, so it is the tested part.
//!   - I/O (`discover`, the owner's-droplet path): paginate both PUBLIC, no-auth catalogs (pmus
//!     `?closed=false&limit=500&offset=`, Kalshi `?series_ticker=&limit=&cursor=`) and hand the parsed
//!     JSON to `assemble`. NOT exercised by any test (no live HTTP on a test path).
//!
//! SCOPE: WEATHER + ECON are 1:1 (one pmus slug ↔ one Kalshi ticker) and SPORTS is 2-outcome (a pmus
//! game ↔ TWO single-team Kalshi tickers). All three are emitted as subscribable [`Pair`]s; sports
//! carries `kalshi_b = Some(team-B ticker)` so the live loop can read both Kalshi books and run the
//! 2-outcome `game_signal`. A live league with no Kalshi match is still COUNTED for the coverage audit
//! (so a new/unmapped league is never silently missed — L7).

use crate::matcher::{self, Ineq};
use crate::types::Cat;
use serde_json::Value;

const PM_MARKETS: &str = "https://gateway.polymarket.us/v1/markets";
// Unify on the bot's Kalshi host (CLAUDE.md: "unify Kalshi host"; venue.rs uses api.elections.kalshi.com).
const KALSHI_MARKETS: &str = "https://api.elections.kalshi.com/trade-api/v2/markets";
const PM_PAGE: usize = 500; // pmus offset page size (verified: page < limit = last page)
const PM_CATALOG_CAP: usize = 12_000; // safety cap; hitting it = TRUNCATED coverage (warn, don't loop forever)

const MON: [&str; 12] = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"];

/// One Kalshi weather bucket as `match_weather` consumes it: `(ticker, floor_strike, cap_strike)`.
type KBucket = (String, Option<i64>, Option<i64>);

/// A co-listed pair the live loop can subscribe + price. Weather/econ are 1:1 (one pmus slug <-> one
/// Kalshi ticker, `kalshi_b = None`). SPORTS is 2-outcome: `kalshi` = team-A ticker (the team pmus lists
/// as YES), `kalshi_b = Some(team-B ticker)` — both are subscribed and the game signal needs both.
#[derive(Clone, Debug, PartialEq)]
pub struct Pair {
    pub slug: String,        // pmus market slug (the WS subscribe key + book key)
    pub kalshi: String,      // the settlement-identical Kalshi ticker (team-A ticker for sports)
    pub kalshi_b: Option<String>, // SPORTS only: the team-B (away) Kalshi ticker; None for weather/econ
    pub cat: Cat,
    pub cluster: String,     // correlated-exposure key (city-date / family-period / game)
    pub settle_clean: bool,  // weather=true (empirically verified); econ/sports=false (recon open)
    pub days_to_event: Option<f64>,
}

/// What a discovery pass produces. `pairs` is the subscribable 1:1 set; the rest is the coverage report
/// (`build_colisted_map`'s report) so an unmapped category / misaligned bucket is LOUD, not silent (L7).
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Discovery {
    pub pairs: Vec<Pair>,
    pub weather_pairs: usize,
    pub econ_pairs: usize,
    pub sports_pairs: usize,                 // matched + emitted as 2-ticker subscribable Pairs (team A + B)
    pub weather_cities_unmapped: Vec<String>, // pmus lists these climate cities, WX map doesn't -> MISSED
    pub sports_leagues_unmapped: Vec<String>,
    pub weather_buckets_misaligned: usize,    // pmus weather buckets with no identical-bounds Kalshi twin
    pub econ_skipped: usize,                  // <= tails / == point buckets / >=T with no listed twin
    pub truncated: bool,                      // pmus catalog hit the page cap (coverage incomplete)
}

// ============================================================================================
// CONFIG MAPS (port of WX / LEAGUES / ECON — the "what to look for"; a coverage audit flags drift, L7)
// ============================================================================================

/// pmus climate-city token -> Kalshi weather series ticker (port of `WX`).
const WX: [(&str, &str); 5] = [
    ("sfo", "KXHIGHTSFO"),
    ("lax", "KXHIGHLAX"),
    ("nyc", "KXHIGHNY"),
    ("mia", "KXHIGHMIA"),
    ("mdw", "KXHIGHCHI"),
];

/// pmus macro-family slug-prefix -> (Kalshi series, family tag, print-grid step) (port of `ECON`).
/// `step` is the grid quantum for the settlement-identical twin (`floor = T - step`); `None` = Fed
/// categorical (no inequality). 0.1 for one-decimal prints (U-3/CPI/GDP), 1000 for payrolls (NFP).
const ECON: [(&str, &str, &str, Option<f64>); 5] = [
    ("cpic", "KXCPIYOY", "cpi", Some(0.1)),
    ("urc", "KXU3", "u3", Some(0.1)),
    ("nfpc", "KXPAYROLLS", "nfp", Some(1000.0)),
    ("gdpc", "KXGDP", "gdp", Some(0.1)),
    ("rdc", "KXFEDDECISION", "fed", None),
];

/// pmus sports-league token -> (Kalshi series, abbrev|surname join). The live 1:1 loop only SUBSCRIBES
/// weather+econ, but discovery still matches sports to COUNT coverage (L7). Only the abbrev leagues are
/// matched here (surname matching needs the player-name fuzzy join, ported separately if sports go 1:1).
const LEAGUES_ABBREV: [(&str, &str); 8] = [
    ("mlb", "KXMLBGAME"),
    ("wnba", "KXWNBAGAME"),
    ("nba", "KXNBAGAME"),
    ("nhl", "KXNHLGAME"),
    ("cs2", "KXCS2GAME"),
    ("lol", "KXLOLGAME"),
    ("valorant", "KXVALORANTGAME"),
    ("cod", "KXCODGAME"),
];

// ============================================================================================
// PURE SLUG / FIELD PARSERS  (port of colisted_map.py's regex helpers; tested offline)
// ============================================================================================

/// pmus weather slug -> city token. `tc-temp-<city>high-...` -> `<city>`. Port of `wcity`.
pub fn weather_city(slug: &str) -> Option<String> {
    let s = slug.to_ascii_lowercase();
    let after = s.strip_prefix("tc-temp-")?;
    let city = after.split("high").next()?;
    if city.is_empty() || city.contains('-') {
        return None;
    }
    Some(city.to_string())
}

/// pmus league token from a sports slug: the 2nd dash-segment (`aec-mlb-lad-pit-...` -> `mlb`). Port of `pmlg`.
pub(crate) fn pm_league(slug: &str) -> Option<String> {
    slug.split('-').nth(1).map(|s| s.to_ascii_lowercase())
}

/// Extract a `YYYY-MM-DD` date from a slug, if present.
pub(crate) fn iso_date(slug: &str) -> Option<String> {
    let b = slug.as_bytes();
    let mut i = 0;
    while i + 10 <= b.len() {
        if b[i].is_ascii_digit()
            && b[i + 1].is_ascii_digit()
            && b[i + 2].is_ascii_digit()
            && b[i + 3].is_ascii_digit()
            && b[i + 4] == b'-'
            && b[i + 5].is_ascii_digit()
            && b[i + 6].is_ascii_digit()
            && b[i + 7] == b'-'
            && b[i + 8].is_ascii_digit()
            && b[i + 9].is_ascii_digit()
        {
            return Some(slug[i..i + 10].to_string());
        }
        i += 1;
    }
    None
}

/// Kalshi ticker date token `-YYMMMDD-` -> ISO `20YY-MM-DD`. Port of `ktok_iso` applied to a ticker.
fn ktok_date(ticker: &str) -> Option<String> {
    let b = ticker.as_bytes();
    // find `-` then YY (2 digit) MMM (3 alpha) DD (2 digit).
    let mut i = 0;
    while i + 8 <= b.len() {
        if b[i] == b'-'
            && b[i + 1].is_ascii_digit()
            && b[i + 2].is_ascii_digit()
            && b[i + 3].is_ascii_uppercase()
            && b[i + 4].is_ascii_uppercase()
            && b[i + 5].is_ascii_uppercase()
            && b[i + 6].is_ascii_digit()
            && b[i + 7].is_ascii_digit()
        {
            let yy = &ticker[i + 1..i + 3];
            let mmm = &ticker[i + 3..i + 6];
            let dd = &ticker[i + 6..i + 8];
            let mon = MON.iter().position(|&m| m == mmm)? + 1;
            return Some(format!("20{yy}-{mon:02}-{dd}"));
        }
        i += 1;
    }
    None
}

/// Kalshi econ period token `-YYMMM[DD]-` -> the `period` key `econ_parse` produces. Port of
/// colisted_map.py's `-(\d{2}[A-Z]{3}\d{0,2})-`: `YYMMM` for U-3/CPI/NFP/Fed, `YYMMMDD` for GDP (whose
/// pmus period key carries the day too) — the trailing 0-2 day digits are INCLUDED so GDP joins.
fn k_econ_period(ticker: &str) -> Option<String> {
    let b = ticker.as_bytes();
    let mut i = 0;
    while i + 6 <= b.len() {
        if b[i] == b'-'
            && b[i + 1].is_ascii_digit()
            && b[i + 2].is_ascii_digit()
            && b[i + 3].is_ascii_uppercase()
            && b[i + 4].is_ascii_uppercase()
            && b[i + 5].is_ascii_uppercase()
        {
            // consume 0-2 trailing day digits (GDP's YYMMMDD), then REQUIRE the closing `-` (the regex is
            // `-(\d{2}[A-Z]{3}\d{0,2})-`): only accept this token if a `-` follows, else keep scanning.
            let mut end = i + 6;
            while end < b.len() && end < i + 8 && b[end].is_ascii_digit() {
                end += 1;
            }
            if end < b.len() && b[end] == b'-' {
                return Some(ticker[i + 1..end].to_string());
            }
        }
        i += 1;
    }
    None
}

/// Parsed pmus econ slug — port of `econ_parse`'s return. `period` is the Kalshi-token form (`26JUN`),
/// `thr` the threshold (None for Fed), `ineq` the orientation, `fed_label` the Fed token (else None).
#[derive(Clone, Debug, PartialEq)]
pub struct EconParse {
    pub family: String, // slug prefix: cpic/urc/nfpc/gdpc/rdc
    pub period: Option<String>,
    pub thr: Option<f64>,
    pub ineq: Ineq,
    pub fed_label: Option<String>,
}

fn emon(name3: &str) -> Option<usize> {
    MON.iter().position(|&m| m == name3.to_ascii_uppercase()).map(|i| i + 1)
}

/// Parse the threshold token enum (`4pt4` -> 4.4, `250k` -> 250000, `2pt0` -> 2.0). Port of `_enum`.
fn enum_val(tok: &str) -> Option<f64> {
    let mut t = tok.to_ascii_lowercase().replace("pct", "");
    t = t.trim().to_string();
    let mut mult = 1.0;
    if let Some(stripped) = t.strip_suffix('k') {
        mult = 1000.0;
        t = stripped.to_string();
    }
    t = t.replace("pt", ".");
    t.parse::<f64>().ok().map(|v| v * mult)
}

/// pmus econ slug -> `EconParse`, or `None` if the prefix isn't a tracked macro family. Port of
/// `econ_parse` (ineq tail `lte/gte` + `atl/atm`, `==` point bucket, Fed categorical, period decoding
/// incl. the U-3/NFP data-month-vs-release-year boundary).
pub fn econ_parse(slug: &str) -> Option<EconParse> {
    let s = slug.to_ascii_lowercase();
    let pre = s.split('-').next()?.to_string();
    ECON.iter().find(|(p, ..)| *p == pre)?; // reject any non-tracked macro family prefix
    let family = pre.clone();

    // Fed categorical: -<label> tail + a YYYY-MM-DD date.
    if pre == "rdc" {
        let label = ["maintains", "cut25bps", "cutgt25bps", "hike25bps", "hikegt25bps"]
            .iter()
            .find(|&&l| s.ends_with(&format!("-{l}")))
            .map(|l| l.to_string());
        let period = iso_date(&s).and_then(|d| {
            let p: Vec<&str> = d.split('-').collect();
            let mon: usize = p.get(1)?.parse().ok()?;
            Some(format!("{}{}", &p[0][2..], MON.get(mon - 1)?))
        });
        return Some(EconParse { family, period, thr: None, ineq: Ineq::Cat, fed_label: label });
    }

    // threshold tail: -lteXpct / -gteXpct, or -atlX / -atmX. Else a bare -Xpct = point bucket (==).
    let (ineq, thr) = if let Some((kind, raw)) = parse_ineq_tail(&s) {
        let ineq = if kind == "lte" { Ineq::Le } else { Ineq::Ge };
        (ineq, enum_val(&raw))
    } else if let Some(raw) = parse_point_tail(&s) {
        (Ineq::Eq, enum_val(&raw))
    } else {
        return None;
    };

    // period decoding per family.
    let period = match pre.as_str() {
        "cpic" => parse_cpi_period(&s),
        "gdpc" => iso_date(&s).and_then(|d| {
            let p: Vec<&str> = d.split('-').collect();
            let mon: usize = p.get(1)?.parse().ok()?;
            // GDP keys on the full date (YYMMMDD) in colisted_map; match that.
            Some(format!("{}{}{}", &p[0][2..], MON.get(mon - 1)?, p.get(2)?))
        }),
        _ => parse_release_period(&s), // urc / nfpc — data-month name + release-year boundary
    };
    Some(EconParse { family, period, thr, ineq, fed_label: None })
}

/// `-(lte|gte)<tok>pct` or `-(atl|atm)<tok>` tail -> (kind, token). kind is "lte"/"gte"/"atl"/"atm".
fn parse_ineq_tail(s: &str) -> Option<(String, String)> {
    for kind in ["lte", "gte"] {
        if let Some(rest) = s.rsplit_once(&format!("-{kind}")) {
            let tail = rest.1;
            if let Some(tok) = tail.strip_suffix("pct") {
                if !tok.is_empty() && tok.chars().all(|c| c.is_ascii_digit() || c == 'p' || c == 't') {
                    return Some((kind.to_string(), tok.to_string()));
                }
            }
        }
    }
    for kind in ["atl", "atm"] {
        if let Some((_, tail)) = s.rsplit_once(&format!("-{kind}")) {
            if !tail.is_empty()
                && tail.chars().all(|c| c.is_ascii_digit() || c == 'p' || c == 't' || c == 'k')
            {
                // map atl/atm -> ">=" (both are cumulative "at least"); kind label only needs Ge here.
                return Some(("gte".to_string(), tail.to_string()));
            }
        }
    }
    None
}

/// A bare `-<tok>pct` tail (a point bucket "CPI YoY = X.X%") -> the token. Distinct from `-gteXpct`.
fn parse_point_tail(s: &str) -> Option<String> {
    let (_, tail) = s.rsplit_once('-')?;
    let tok = tail.strip_suffix("pct")?;
    if !tok.is_empty() && tok.chars().all(|c| c.is_ascii_digit() || c == 'p' || c == 't') {
        Some(tok.to_string())
    } else {
        None
    }
}

/// CPI period: `...mayYYYYyoy...` -> `YYMMM`. Port of the cpic branch.
fn parse_cpi_period(s: &str) -> Option<String> {
    // find a month name immediately followed by 4 digits then "yoy".
    for (mi, m) in MON.iter().enumerate() {
        let ml = m.to_ascii_lowercase();
        if let Some(p) = s.find(&ml) {
            let after = &s[p + ml.len()..];
            // tolerate the full month spelling: skip alpha until digits.
            let digits_start = after.find(|c: char| c.is_ascii_digit())?;
            let inter = &after[..digits_start];
            if !inter.chars().all(|c| c.is_ascii_alphabetic()) {
                continue;
            }
            let rest = &after[digits_start..];
            if rest.len() >= 4 && rest.as_bytes()[..4].iter().all(|b| b.is_ascii_digit()) {
                let yr = &rest[..4];
                if rest[4..].starts_with("yoy") {
                    return Some(format!("{}{}", &yr[2..], MON[mi]));
                }
            }
        }
    }
    None
}

/// urc/nfpc period: a `-<monthname>-` token + a release date `YYYY-MM-DD`; data-year = release-year - 1
/// when data-month > release-month (Dec data releases in Jan). Port of the urc/nfpc branch.
fn parse_release_period(s: &str) -> Option<String> {
    // data-month name token between dashes.
    let dmon = s
        .split('-')
        .find_map(|seg| emon(&seg.chars().take(3).collect::<String>()).map(|m| (m, seg)))
        .map(|(m, _)| m)?;
    let date = iso_date(s)?;
    let p: Vec<&str> = date.split('-').collect();
    let rel_year: i32 = p[0].parse().ok()?;
    let rel_mon: usize = p[1].parse().ok()?;
    let data_year = if dmon > rel_mon { rel_year - 1 } else { rel_year };
    Some(format!("{:02}{}", data_year % 100, MON[dmon - 1]))
}

/// `YYYY-MM-DD` -> days since the Unix epoch (1970-01-01), via Howard Hinnant's `days_from_civil`
/// (dep-free, no chrono). Used only for the sports `days_to_event = slug_date - today`. Returns `None`
/// on a malformed date.
fn ymd_to_epoch_days(ymd: &str) -> Option<i64> {
    let p: Vec<&str> = ymd.split('-').collect();
    if p.len() != 3 {
        return None;
    }
    let (y, m, d): (i64, i64, i64) = (p[0].parse().ok()?, p[1].parse().ok()?, p[2].parse().ok()?);
    if !(1..=12).contains(&m) || !(1..=31).contains(&d) {
        return None;
    }
    // days_from_civil: y' = y - (m <= 2); era/year-of-era/day-of-year algebra (proleptic Gregorian).
    let y = y - i64::from(m <= 2);
    let era = if y >= 0 { y } else { y - 399 } / 400;
    let yoe = y - era * 400; // [0, 399]
    let doy = (153 * (if m > 2 { m - 3 } else { m + 9 }) + 2) / 5 + d - 1; // [0, 365]
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy; // [0, 146096]
    Some(era * 146097 + doe - 719468)
}

/// Bind a pmus game (abbrevs A, B) to ONE Kalshi event's `{abbrev: ticker}` set, returning the two
/// DISTINCT tickers `(ticker_a, ticker_b)`. FAITHFUL port of `colisted_map.py::pick_game` (lines
/// 139-158): the slug ET date == the Kalshi ticker date, so an EXACT-date match is correct and kills the
/// adjacent-series wrong-game mispair. The ±1-day window is the fallback ONLY when the slug carried no
/// date (`slug_dated=false`) AND the match is GLOBALLY UNIQUE. `used` (shared across one league's pass)
/// marks each bound Kalshi event by index so two same-day same-team games don't both bind the first event
/// (the DOUBLEHEADER guard). `kbydate` maps a date -> the league's `[{abbrev: ticker}]` event dicts.
fn pick_game(
    kbydate: &std::collections::HashMap<String, Vec<std::collections::HashMap<String, String>>>,
    ka: &str,
    kb: &str,
    date: &str,
    slug_dated: bool,
    used: &mut std::collections::HashSet<(String, usize)>,
) -> Option<(String, String)> {
    // exact-date arm: first un-used event on `date` whose two abbrevs resolve to two DISTINCT tickers.
    if let Some(events) = kbydate.get(date) {
        for (i, ev) in events.iter().enumerate() {
            if used.contains(&(date.to_string(), i)) {
                continue;
            }
            if let Some(pair) = resolve_two(ev, ka, kb) {
                used.insert((date.to_string(), i));
                return Some(pair);
            }
        }
    }
    // ±1-day fallback: ONLY when the slug was undated AND the match is GLOBALLY UNIQUE across all dates.
    if !slug_dated {
        let mut hits: Vec<(String, usize, (String, String))> = Vec::new();
        for (kdate, events) in kbydate {
            if !kdate.is_empty() && dnear(kdate, date) {
                for (i, ev) in events.iter().enumerate() {
                    if used.contains(&(kdate.clone(), i)) {
                        continue;
                    }
                    if let Some(pair) = resolve_two(ev, ka, kb) {
                        hits.push((kdate.clone(), i, pair));
                    }
                }
            }
        }
        if hits.len() == 1 {
            let (kdate, i, pair) = hits.into_iter().next().unwrap();
            used.insert((kdate, i));
            return Some(pair);
        }
    }
    None
}

/// In one Kalshi event's `{abbrev: ticker}` dict, resolve A and B to two DISTINCT tickers (the
/// no-false-positive invariant). Port of `_match_game`'s abbrev arm.
fn resolve_two(ev: &std::collections::HashMap<String, String>, ka: &str, kb: &str) -> Option<(String, String)> {
    let ta = ev.get(ka)?;
    let tb = ev.get(kb)?;
    if ta != tb {
        Some((ta.clone(), tb.clone()))
    } else {
        None
    }
}

/// `|a - b| <= 1 day` on `YYYY-MM-DD` dates (port of `dnear`); a parse failure falls back to `a == b`.
fn dnear(a: &str, b: &str) -> bool {
    match (ymd_to_epoch_days(a), ymd_to_epoch_days(b)) {
        (Some(x), Some(y)) => (x - y).abs() <= 1,
        _ => a == b,
    }
}

// ============================================================================================
// ASSEMBLE  (pure: already-fetched catalogs -> Discovery, reusing matcher.rs joins)
// ============================================================================================

/// Build the `Discovery` from already-fetched catalog JSON: the pmus `closed=false` market list, and a
/// closure that returns a Kalshi series' markets (so the offline test can stub it; the live `discover`
/// hands it the real per-series pulls). `truncated` flags a pmus catalog that hit the page cap.
/// `today_epoch_days` is today's date as days-since-epoch (the live `discover` computes it from the
/// system clock; `None` -> sports `days_to_event` is `None`, so the event-proximity gate stays dormant).
///
/// Reuses [`matcher`] for every join (no identity logic re-implemented here). WEATHER + ECON become 1:1
/// `Pair`s; SPORTS becomes a 2-ticker `Pair` (team A + B) via `pick_game` (exact-date binding + a
/// doubleheader `used`-set; ±1-day fallback only when the slug is undated AND globally unique).
pub fn assemble<F>(pm_markets: &[Value], truncated: bool, today_epoch_days: Option<i64>, mut kalshi_series: F) -> Discovery
where
    F: FnMut(&str) -> Vec<Value>,
{
    let mut d = Discovery { truncated, ..Default::default() };

    // ---- WEATHER: per (city, date), join pmus buckets to Kalshi buckets on identical inclusive bounds.
    let clim: Vec<&Value> = pm_markets.iter().filter(|m| field_str(m, "category") == Some("climate".into())).collect();
    let pm_cities: std::collections::BTreeSet<String> =
        clim.iter().filter_map(|m| field_str(m, "slug").and_then(|s| weather_city(&s))).collect();
    for (city, kser) in WX {
        let pmc: Vec<&Value> = clim.iter().filter(|m| field_str(m, "slug").and_then(|s| weather_city(&s)).as_deref() == Some(city)).copied().collect();
        if pmc.is_empty() {
            continue;
        }
        let kmarkets = kalshi_series(kser);
        // Kalshi buckets grouped by ISO date.
        let mut kby: std::collections::HashMap<String, Vec<KBucket>> = std::collections::HashMap::new();
        for m in &kmarkets {
            let Some(tk) = field_str(m, "ticker") else { continue };
            let Some(date) = ktok_date(&tk) else { continue };
            kby.entry(date).or_default().push((tk, field_i64(m, "floor_strike"), field_i64(m, "cap_strike")));
        }
        for pm in pmc {
            let Some(slug) = field_str(pm, "slug") else { continue };
            let Some(date) = iso_date(&slug) else { continue };
            let Some(kbuckets) = kby.get(&date) else { continue };
            match matcher::match_weather(&slug, kbuckets, city, &date) {
                Some(m) => {
                    d.pairs.push(Pair {
                        slug,
                        kalshi: m.kalshi,
                        kalshi_b: None, // weather is 1:1
                        cat: Cat::Weather,
                        cluster: m.cluster,
                        settle_clean: m.settle_clean,
                        days_to_event: m.days_to_event,
                    });
                    d.weather_pairs += 1;
                }
                None => d.weather_buckets_misaligned += 1, // no identical-bounds twin -> NOT paired (flag)
            }
        }
    }
    d.weather_cities_unmapped = pm_cities.iter().filter(|c| !WX.iter().any(|(w, _)| *w == c.as_str())).cloned().collect();

    // ---- ECON: pmus '>=T' <-> Kalshi 'Above T-step' twin (+ Fed categorical). matcher::match_econ.
    let macro_m: Vec<&Value> = pm_markets.iter().filter(|m| field_str(m, "category") == Some("macro".into())).collect();
    for (pre, kser, family, step) in ECON {
        let group: Vec<&Value> = macro_m.iter().filter(|m| field_str(m, "slug").is_some_and(|s| s.to_ascii_lowercase().starts_with(pre))).copied().collect();
        if group.is_empty() {
            continue;
        }
        let kmarkets = kalshi_series(kser);
        // (period -> [(floor, ticker)]) and (period -> [(yes_sub_title, ticker)]) for the matcher.
        let mut k_floors: std::collections::HashMap<String, Vec<(f64, String)>> = std::collections::HashMap::new();
        let mut k_labels: std::collections::HashMap<String, Vec<(String, String)>> = std::collections::HashMap::new();
        for m in &kmarkets {
            let Some(tk) = field_str(m, "ticker") else { continue };
            let per = k_econ_period(&tk).unwrap_or_default();
            if let Some(f) = field_f64(m, "floor_strike") {
                k_floors.entry(per.clone()).or_default().push((f, tk.clone()));
            }
            let lab = field_str(m, "yes_sub_title").unwrap_or_default().to_ascii_lowercase();
            k_labels.entry(per).or_default().push((lab, tk));
        }
        for pm in group {
            let Some(slug) = field_str(pm, "slug") else { continue };
            let Some(p) = econ_parse(&slug) else { continue };
            let per = p.period.clone().unwrap_or_default();
            let empty_f: Vec<(f64, String)> = Vec::new();
            let empty_l: Vec<(String, String)> = Vec::new();
            let floors = k_floors.get(&per).unwrap_or(&empty_f);
            let labels = k_labels.get(&per).unwrap_or(&empty_l);
            match matcher::match_econ(p.ineq, p.thr, step.unwrap_or(0.0), p.fed_label.as_deref(), &per, family, floors, labels) {
                Some(m) => {
                    d.pairs.push(Pair {
                        slug,
                        kalshi: m.kalshi,
                        kalshi_b: None, // econ is 1:1
                        cat: Cat::Econ,
                        cluster: m.cluster,
                        settle_clean: m.settle_clean,
                        days_to_event: m.days_to_event,
                    });
                    d.econ_pairs += 1;
                }
                None => d.econ_skipped += 1, // <= / == / no-listed-twin -> not co-listed (flag)
            }
        }
    }

    // ---- SPORTS (abbrev leagues): emitted as 2-ticker subscribable Pairs via pick_game (exact-date
    //      binding + doubleheader used-set; ±1-day fallback only when the slug is undated AND globally
    //      unique). pmus YES = team A; Kalshi-A is the same team (`kalshi`), Kalshi-B is `kalshi_b`.
    let sports_m: Vec<&Value> = pm_markets
        .iter()
        .filter(|m| field_str(m, "category") == Some("sports".into()) && field_str(m, "marketType") == Some("moneyline".into()) && m.get("gameStartTime").is_some())
        .collect();
    let pm_leagues: std::collections::BTreeSet<String> = sports_m.iter().filter_map(|m| field_str(m, "slug").and_then(|s| pm_league(&s))).collect();
    for (league, kser) in LEAGUES_ABBREV {
        let group: Vec<&Value> = sports_m.iter().filter(|m| field_str(m, "slug").and_then(|s| pm_league(&s)).as_deref() == Some(league)).copied().collect();
        if group.is_empty() {
            continue;
        }
        let kmarkets = kalshi_series(kser);
        // group Kalshi markets by EVENT -> {abbrev: ticker}, recording each event's date (from the ticker);
        // then bucket events by date for pick_game's exact-date binding (colisted_map.py kbydate).
        let mut by_event: std::collections::HashMap<String, std::collections::HashMap<String, String>> = std::collections::HashMap::new();
        let mut event_date: std::collections::HashMap<String, String> = std::collections::HashMap::new();
        for m in &kmarkets {
            let Some(tk) = field_str(m, "ticker") else { continue };
            let Some(ev) = field_str(m, "event_ticker") else { continue };
            event_date.entry(ev.clone()).or_insert_with(|| ktok_date(&tk).unwrap_or_default());
            let abbrev = tk.rsplit('-').next().unwrap_or("").to_ascii_lowercase();
            if !abbrev.is_empty() {
                by_event.entry(ev).or_default().insert(abbrev, tk);
            }
        }
        let mut kbydate: std::collections::HashMap<String, Vec<std::collections::HashMap<String, String>>> = std::collections::HashMap::new();
        for (ev, dict) in by_event {
            let date = event_date.get(&ev).cloned().unwrap_or_default();
            kbydate.entry(date).or_default().push(dict);
        }
        // one used-set per league-pass: a bound Kalshi event can't bind a second pm game (doubleheaders).
        let mut used: std::collections::HashSet<(String, usize)> = std::collections::HashSet::new();
        for pm in group {
            let Some(slug) = field_str(pm, "slug") else { continue };
            let Some((a, b)) = sports_abbrevs(pm) else { continue };
            // pm slug ET date == Kalshi ticker date (exact join); undated slug -> the ±1 fallback path.
            let slug_date = iso_date(&slug);
            let slug_dated = slug_date.is_some();
            let date = slug_date.unwrap_or_default();
            let Some((ta, tb)) = pick_game(&kbydate, &a, &b, &date, slug_dated, &mut used) else { continue };
            // days_to_event = slug_date - today (in days); None when either date is unknown -> gate dormant.
            let days_to_event = match (today_epoch_days, ymd_to_epoch_days(&date)) {
                (Some(today), Some(game)) => Some((game - today) as f64),
                _ => None,
            };
            d.pairs.push(Pair {
                slug,
                kalshi: ta,
                kalshi_b: Some(tb),
                cat: Cat::Sports,
                cluster: format!("{league}-{date}"),
                settle_clean: false, // only a game that COMPLETES on schedule settles identically (void tail)
                days_to_event,
            });
            d.sports_pairs += 1;
        }
    }
    d.sports_leagues_unmapped = pm_leagues.iter().filter(|l| !LEAGUES_ABBREV.iter().any(|(x, _)| *x == l.as_str())).cloned().collect();

    d
}

/// pmus moneyline game -> the two team abbreviations `(A, B)` from `marketSides[].team.abbreviation`,
/// where A is the LONG-named side (the side with a truthy `long` field) and B is the other — matching
/// colisted_map.py's `lo = next((s for s in sides if s.get("long")), sides[0]); ot = the other`. The A/B
/// ORDER decides which Kalshi ticker is `kalshi` vs `kalshi_b`, so it must follow the Python exactly
/// (pmus YES = team A; Kalshi-A must be the same team). Only sides whose `team.name` is present count
/// (the Python's `(s.get("team") or {}).get("name")` filter).
fn sports_abbrevs(m: &Value) -> Option<(String, String)> {
    let sides: Vec<&Value> = m
        .get("marketSides")?
        .as_array()?
        .iter()
        .filter(|s| s.get("team").and_then(|t| t.get("name")).and_then(Value::as_str).is_some())
        .collect();
    if sides.len() < 2 {
        return None;
    }
    // long-named side first (truthy `long`), else the first side; the other is whichever is not it.
    let lo_idx = sides.iter().position(|s| is_truthy(s.get("long"))).unwrap_or(0);
    let ot_idx = (0..sides.len()).find(|&i| i != lo_idx)?;
    let abbrev = |s: &Value| s.get("team").and_then(|t| t.get("abbreviation")).and_then(Value::as_str).map(|x| x.to_ascii_lowercase());
    Some((abbrev(sides[lo_idx])?, abbrev(sides[ot_idx])?))
}

/// Python truthiness of a JSON value for the `s.get("long")` test: present + not null/false/empty/0.
fn is_truthy(v: Option<&Value>) -> bool {
    match v {
        None | Some(Value::Null) => false,
        Some(Value::Bool(b)) => *b,
        Some(Value::String(s)) => !s.is_empty(),
        Some(Value::Number(n)) => n.as_f64().map(|f| f != 0.0).unwrap_or(true),
        Some(_) => true,
    }
}

// ---- small JSON field accessors (a number OR numeric string -> the typed value) ------------------

fn field_str(m: &Value, key: &str) -> Option<String> {
    m.get(key).and_then(Value::as_str).map(str::to_string)
}
fn field_f64(m: &Value, key: &str) -> Option<f64> {
    match m.get(key) {
        Some(Value::Number(n)) => n.as_f64(),
        Some(Value::String(s)) => s.trim().parse().ok(),
        _ => None,
    }
}
fn field_i64(m: &Value, key: &str) -> Option<i64> {
    field_f64(m, key).map(|f| f.round() as i64)
}

// ============================================================================================
// I/O LAYER  (live catalog pull — the owner's-droplet path; NOT on any test path)
// ============================================================================================

/// Run a full live discovery: PUBLIC, no-auth catalog pulls on both venues, then `assemble`. Returns a
/// clear error string on a failed/degraded pull (the caller treats a degraded pass as "do not prune",
/// like monitor.py's `fetch_errors`). NEVER called from a test (no live HTTP on a test path).
pub async fn discover(http: &reqwest::Client) -> Result<Discovery, String> {
    let (pm_markets, truncated) = pull_pmus(http).await?;
    // The Kalshi series pull is async, but `assemble` takes a sync closure; pre-fetch each needed series
    // into a map first, then hand `assemble` a lookup over it (so the pure assembler stays sync + tested).
    let mut series_cache: std::collections::HashMap<String, Vec<Value>> = std::collections::HashMap::new();
    let needed: Vec<&str> = WX.iter().map(|(_, k)| *k)
        .chain(ECON.iter().map(|(_, k, ..)| *k))
        .chain(LEAGUES_ABBREV.iter().map(|(_, k)| *k))
        .collect();
    for kser in needed {
        let markets = pull_kalshi_series(http, kser).await?;
        series_cache.insert(kser.to_string(), markets);
    }
    // today as days-since-epoch (UTC), dep-free: system seconds / 86400. Feeds sports days_to_event.
    let today = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .ok()
        .map(|d| (d.as_secs() / 86_400) as i64);
    Ok(assemble(&pm_markets, truncated, today, |kser| series_cache.get(kser).cloned().unwrap_or_default()))
}

/// Paginate the pmus `closed=false` catalog by `offset` (page < limit = last; the page cap guards a
/// runaway). Returns `(markets, truncated)`.
async fn pull_pmus(http: &reqwest::Client) -> Result<(Vec<Value>, bool), String> {
    let mut all = Vec::new();
    let mut offset = 0usize;
    loop {
        let url = format!("{PM_MARKETS}?closed=false&limit={PM_PAGE}&offset={offset}");
        let page = fetch_markets(http, &url, "markets").await?;
        let n = page.len();
        all.extend(page);
        if n < PM_PAGE {
            return Ok((all, false));
        }
        if all.len() > PM_CATALOG_CAP {
            return Ok((all, true)); // TRUNCATED -> coverage incomplete (caller warns)
        }
        offset += PM_PAGE;
    }
}

/// Pull one Kalshi series' OPEN markets, paginated by `cursor` (empty/absent cursor = last page).
async fn pull_kalshi_series(http: &reqwest::Client, series: &str) -> Result<Vec<Value>, String> {
    let mut all = Vec::new();
    let mut cursor: Option<String> = None;
    loop {
        let mut url = format!("{KALSHI_MARKETS}?series_ticker={series}&status=open&limit=1000");
        if let Some(c) = &cursor {
            url.push_str(&format!("&cursor={c}"));
        }
        let v = fetch_json(http, &url).await?;
        if let Some(arr) = v.get("markets").and_then(Value::as_array) {
            all.extend(arr.iter().cloned());
        }
        cursor = v.get("cursor").and_then(Value::as_str).filter(|c| !c.is_empty()).map(str::to_string);
        if cursor.is_none() {
            return Ok(all);
        }
    }
}

/// GET a catalog page and return its `key` array (`markets`). A non-2xx or non-JSON body is an error
/// (mirrors colisted_map.py's degraded-pass model — the caller skips pruning on a degraded pass).
async fn fetch_markets(http: &reqwest::Client, url: &str, key: &str) -> Result<Vec<Value>, String> {
    let v = fetch_json(http, url).await?;
    Ok(v.get(key).and_then(Value::as_array).cloned().unwrap_or_default())
}

/// GET -> parsed JSON `Value`. Reads the body as text first (like `exec.rs`) so a non-JSON error page
/// yields a clean error string rather than a decode panic. PUBLIC endpoint — NO auth headers.
async fn fetch_json(http: &reqwest::Client, url: &str) -> Result<Value, String> {
    let resp = http
        .get(url)
        .header("User-Agent", "cross-arb/1.0")
        .header("Accept", "application/json")
        .send()
        .await
        .map_err(|e| format!("GET {}: {e}", url.split('?').next().unwrap_or(url)))?;
    let status = resp.status();
    let text = resp.text().await.unwrap_or_default();
    if !status.is_success() {
        return Err(format!("GET {} -> {}", url.split('?').next().unwrap_or(url), status.as_u16()));
    }
    serde_json::from_str(&text).map_err(|e| format!("parse {}: {e}", url.split('?').next().unwrap_or(url)))
}

#[cfg(test)]
mod tests {
    use super::*;

    // ---- PURE PARSERS ----------------------------------------------------------------------------
    #[test]
    fn weather_city_parses_and_rejects_non_weather() {
        assert_eq!(weather_city("tc-temp-sfohigh-2026-06-09-gte64lt65f").as_deref(), Some("sfo"));
        assert_eq!(weather_city("tc-temp-nychigh-2026-06-11-gte95f").as_deref(), Some("nyc"));
        assert_eq!(weather_city("aec-mlb-lad-pit-2026-06-09"), None);
        assert_eq!(weather_city("urc-us-seasonadj-gte-june-2026-07-02-atl4pt4"), None);
    }

    #[test]
    fn iso_and_ticker_dates() {
        assert_eq!(iso_date("tc-temp-laxhigh-2026-06-09-gte73").as_deref(), Some("2026-06-09"));
        assert_eq!(iso_date("no-date-here"), None);
        assert_eq!(ktok_date("KXHIGHNY-26JUN11-T95").as_deref(), Some("2026-06-11"));
        assert_eq!(ktok_date("KXMLBGAME-26JUN16-LAD").as_deref(), Some("2026-06-16"));
    }

    #[test]
    fn econ_parse_matches_python_vectors() {
        // GDP: atl tail -> Ge, full-date period.
        let g = econ_parse("gdpc-us-saa-q2-2026-07-30-atl2pt0").unwrap();
        assert_eq!((g.ineq, g.thr, g.period.as_deref()), (Ineq::Ge, Some(2.0), Some("26JUL30")));
        // CPI lte tail -> Le, YYMMM period.
        let c = econ_parse("cpic-uscpi-may2026yoy-2026-06-10-lte3pt7pct").unwrap();
        assert_eq!((c.ineq, c.thr, c.period.as_deref()), (Ineq::Le, Some(3.7), Some("26MAY")));
        // CPI bare point bucket -> Eq.
        assert_eq!(econ_parse("cpic-uscpi-may2026yoy-2026-06-10-3pt8pct").unwrap().ineq, Ineq::Eq);
        // NFP: atl + 1000-grid threshold, release-period.
        let n = econ_parse("nfpc-uschange-gte-june-2026-07-02-atl250k").unwrap();
        assert_eq!((n.ineq, n.thr, n.period.as_deref()), (Ineq::Ge, Some(250000.0), Some("26JUN")));
        // Fed categorical.
        let f = econ_parse("rdc-usfed-fomc-2026-06-17-maintains").unwrap();
        assert_eq!((f.ineq, f.fed_label.as_deref(), f.period.as_deref()), (Ineq::Cat, Some("maintains"), Some("26JUN")));
        // year boundary: Dec data released next Jan -> prior data-year (26DEC not 27DEC).
        assert_eq!(econ_parse("urc-us-seasonadj-gte-december-2027-01-08-atl4pt4").unwrap().period.as_deref(), Some("26DEC"));
        assert_eq!(econ_parse("urc-us-seasonadj-gte-june-2026-07-02-atl4pt4").unwrap().period.as_deref(), Some("26JUN"));
        // non-econ -> None.
        assert!(econ_parse("tc-temp-laxhigh-2026-06-09-gte73").is_none());
        assert!(econ_parse("aec-mlb-x-y-2026-06-10").is_none());
    }

    // ---- ASSEMBLE: the embedded sample catalog (markets that SHOULD and should NOT join) ----------
    fn pm(slug: &str, category: &str, extra: &str) -> Value {
        let body = if extra.is_empty() {
            format!(r#"{{"slug":"{slug}","category":"{category}"}}"#)
        } else {
            format!(r#"{{"slug":"{slug}","category":"{category}",{extra}}}"#)
        };
        serde_json::from_str(&body).unwrap()
    }

    fn sample_pm_catalog() -> Vec<Value> {
        vec![
            // WEATHER: two pmus SFO buckets — gte64lt65f (middle) + gte72f (high tail).
            pm("tc-temp-sfohigh-2026-06-09-gte64lt65f", "climate", ""),
            pm("tc-temp-sfohigh-2026-06-09-gte72f", "climate", ""),
            // a SFO bucket with NO identical Kalshi twin -> must be flagged misaligned, not paired.
            pm("tc-temp-sfohigh-2026-06-09-gte80lt81f", "climate", ""),
            // an UNMAPPED city (philadelphia not in WX) -> coverage flag.
            pm("tc-temp-phlhigh-2026-06-09-gte64lt65f", "climate", ""),
            // ECON: U-3 >=4.4 (twin Above 4.3 listed -> pair) + CPI point bucket (== -> skipped).
            pm("urc-us-seasonadj-gte-june-2026-07-02-atl4pt4", "macro", ""),
            pm("cpic-uscpi-may2026yoy-2026-06-10-3pt8pct", "macro", ""),
            // ECON: U-3 <=4.0 (opposite orientation -> skipped).
            pm("urc-us-seasonadj-lte-june-2026-07-02-lte4pt0pct", "macro", ""),
            // SPORTS: an MLB moneyline with two team sides. PIT is listed FIRST but LAD carries `long`
            // (the long-named side), so pick_game's A = LAD, B = PIT -> kalshi = LAD ticker, kalshi_b = PIT.
            // This proves the long-team-first ordering (not array order) decides A/B, matching the Python.
            pm(
                "aec-mlb-lad-pit-2026-06-16",
                "sports",
                r#""marketType":"moneyline","gameStartTime":"2026-06-16T20:00:00Z","marketSides":[{"long":false,"team":{"name":"Pittsburgh Pirates","abbreviation":"PIT"}},{"long":true,"team":{"name":"Los Angeles Dodgers","abbreviation":"LAD"}}]"#,
            ),
        ]
    }

    /// The Kalshi-series stub: returns embedded sample Kalshi markets per series ticker.
    fn kalshi_stub(series: &str) -> Vec<Value> {
        let raw = match series {
            "KXHIGHTSFO" => vec![
                // middle 64-65 (twin of gte64lt65f) + an extra low bucket pmus doesn't list (offset!).
                r#"{"ticker":"KXHIGHTSFO-26JUN09-B62","floor_strike":62,"cap_strike":63,"yes_sub_title":"62 to 63"}"#,
                r#"{"ticker":"KXHIGHTSFO-26JUN09-B64","floor_strike":64,"cap_strike":65,"yes_sub_title":"64 to 65"}"#,
                // high tail 72-or-above -> Kalshi floor_strike 71 (kbounds [72, inf)) twin of gte72f.
                r#"{"ticker":"KXHIGHTSFO-26JUN09-T72","floor_strike":71,"yes_sub_title":"72 or above"}"#,
            ],
            "KXU3" => vec![
                // the IDENTICAL twin of pmus >=4.4 is floor 4.3; floor 4.4 is the off-by-one phantom.
                r#"{"ticker":"KXU3-26JUN-T4.3","floor_strike":4.3,"yes_sub_title":"Above 4.3%"}"#,
                r#"{"ticker":"KXU3-26JUN-T4.4","floor_strike":4.4,"yes_sub_title":"Above 4.4%"}"#,
            ],
            "KXMLBGAME" => vec![
                // one MLB event, two team tickers (the abbrev-binding case: LAD vs PIT on 26JUN16).
                r#"{"ticker":"KXMLBGAME-26JUN16-LAD","event_ticker":"KXMLBGAME-26JUN16LADPIT","yes_sub_title":"Los Angeles Dodgers"}"#,
                r#"{"ticker":"KXMLBGAME-26JUN16-PIT","event_ticker":"KXMLBGAME-26JUN16LADPIT","yes_sub_title":"Pittsburgh Pirates"}"#,
            ],
            _ => vec![],
        };
        raw.into_iter().map(|s| serde_json::from_str(s).unwrap()).collect()
    }

    #[test]
    fn assemble_joins_the_right_pairs_and_no_false_joins() {
        // today = 2026-06-11 in epoch-days; the MLB game is 2026-06-16 -> days_to_event = 5.
        let today = ymd_to_epoch_days("2026-06-11");
        let d = assemble(&sample_pm_catalog(), false, today, kalshi_stub);

        // WEATHER: gte64lt65f -> B64, gte72f -> T72. gte80lt81f has no twin -> misaligned.
        let wx: Vec<&Pair> = d.pairs.iter().filter(|p| p.cat == Cat::Weather).collect();
        assert_eq!(wx.len(), 2, "exactly the two bounds-identical weather buckets pair");
        let b64 = wx.iter().find(|p| p.slug.ends_with("gte64lt65f")).unwrap();
        assert_eq!(b64.kalshi, "KXHIGHTSFO-26JUN09-B64"); // bounds-dict join finds it despite the offset
        assert!(b64.settle_clean); // weather is empirically settlement-clean
        assert_eq!(b64.cluster, "sfo-2026-06-09");
        let t72 = wx.iter().find(|p| p.slug.ends_with("gte72f")).unwrap();
        assert_eq!(t72.kalshi, "KXHIGHTSFO-26JUN09-T72"); // high-tail canonicalization [72, inf)
        assert_eq!(d.weather_buckets_misaligned, 1); // gte80lt81f -> flagged, NOT paired

        // ECON: U-3 >=4.4 binds the T-step twin (floor 4.3), NOT floor==4.4 (the L21 phantom).
        let econ: Vec<&Pair> = d.pairs.iter().filter(|p| p.cat == Cat::Econ).collect();
        assert_eq!(econ.len(), 1, "only the >=T-with-listed-twin pair");
        assert_eq!(econ[0].kalshi, "KXU3-26JUN-T4.3", "must bind T-step twin, never floor==T (L21)");
        assert!(!econ[0].settle_clean); // econ recon still open
        assert_eq!(econ[0].cluster, "u3-26JUN");
        assert_eq!(d.econ_skipped, 2); // the == point bucket + the <= tail are both skipped

        // SPORTS: the MLB game is now emitted as a 2-ticker Pair (team A = LAD, team B = PIT). pmus lists
        // LAD as the long-named side -> kalshi = LAD ticker, kalshi_b = PIT ticker; days_to_event = 5.
        assert_eq!(d.sports_pairs, 1);
        let sp: Vec<&Pair> = d.pairs.iter().filter(|p| p.cat == Cat::Sports).collect();
        assert_eq!(sp.len(), 1, "sports is emitted as a subscribable 2-ticker Pair");
        assert_eq!(sp[0].kalshi, "KXMLBGAME-26JUN16-LAD");
        assert_eq!(sp[0].kalshi_b.as_deref(), Some("KXMLBGAME-26JUN16-PIT"));
        assert_eq!(sp[0].cluster, "mlb-2026-06-16");
        assert!(!sp[0].settle_clean); // void tail -> never marked clean
        assert_eq!(sp[0].days_to_event, Some(5.0)); // 2026-06-16 minus 2026-06-11

        // COVERAGE: philadelphia is the unmapped climate city.
        assert_eq!(d.weather_cities_unmapped, vec!["phl".to_string()]);
        assert!(!d.truncated);
    }

    /// GDP keys on the FULL-DATE period (YYMMMDD) on BOTH sides — the Kalshi period parser must keep the
    /// trailing day digits (the `\d{0,2}` in the python regex) or GDP silently never joins. Regression
    /// guard for the self-review CRITICAL.
    #[test]
    fn econ_gdp_full_date_period_joins() {
        assert_eq!(k_econ_period("KXGDP-26JUL30-T1.9").as_deref(), Some("26JUL30")); // keeps the day
        assert_eq!(k_econ_period("KXU3-26JUN-T4.3").as_deref(), Some("26JUN")); // no day -> YYMMM
        fn stub(series: &str) -> Vec<Value> {
            if series == "KXGDP" {
                // twin of pmus >=2.0 (step 0.1) is floor 1.9; the period token carries the day.
                vec![serde_json::from_str(r#"{"ticker":"KXGDP-26JUL30-T1.9","floor_strike":1.9,"yes_sub_title":"Above 1.9%"}"#).unwrap()]
            } else {
                vec![]
            }
        }
        let cat = vec![pm("gdpc-us-saa-q2-2026-07-30-atl2pt0", "macro", "")];
        let d = assemble(&cat, false, None, stub);
        assert_eq!(d.econ_pairs, 1, "GDP must join on the full-date period");
        assert_eq!(d.pairs[0].kalshi, "KXGDP-26JUL30-T1.9");
        assert_eq!(d.pairs[0].cluster, "gdp-26JUL30");
    }

    // ---- SPORTS pick_game: exact-date binding + doubleheader used-set + ±1 fallback -------------------
    fn ev(pairs: &[(&str, &str)]) -> std::collections::HashMap<String, String> {
        pairs.iter().map(|(a, t)| (a.to_string(), t.to_string())).collect()
    }

    /// Exact-date binding: a pm game binds the Kalshi event on its OWN date, never an adjacent-date event
    /// for the same teams (the adjacent-series wrong-game mispair pick_game exists to kill).
    #[test]
    fn pick_game_binds_exact_date_not_adjacent() {
        let mut kbydate = std::collections::HashMap::new();
        kbydate.insert("2026-06-16".to_string(), vec![ev(&[("lad", "K-LAD-16"), ("pit", "K-PIT-16")])]);
        kbydate.insert("2026-06-17".to_string(), vec![ev(&[("lad", "K-LAD-17"), ("pit", "K-PIT-17")])]);
        let mut used = std::collections::HashSet::new();
        // dated slug on the 16th binds the 16th event, NOT the 17th.
        assert_eq!(pick_game(&kbydate, "lad", "pit", "2026-06-16", true, &mut used), Some(("K-LAD-16".into(), "K-PIT-16".into())));
        // a dated slug whose date has no event does NOT fall back (slug_dated=true) even though ±1 exists.
        let mut used2 = std::collections::HashSet::new();
        assert_eq!(pick_game(&kbydate, "lad", "pit", "2026-06-18", true, &mut used2), None);
    }

    /// DOUBLEHEADER guard: two same-day same-team pm games must bind TWO DISTINCT Kalshi events, not both
    /// the first. The shared `used` set makes the second pick skip the already-bound event.
    #[test]
    fn pick_game_doubleheader_used_set_binds_distinct_events() {
        let mut kbydate = std::collections::HashMap::new();
        kbydate.insert(
            "2026-06-16".to_string(),
            vec![ev(&[("lad", "K-LAD-G1"), ("pit", "K-PIT-G1")]), ev(&[("lad", "K-LAD-G2"), ("pit", "K-PIT-G2")])],
        );
        let mut used = std::collections::HashSet::new();
        let g1 = pick_game(&kbydate, "lad", "pit", "2026-06-16", true, &mut used).unwrap();
        let g2 = pick_game(&kbydate, "lad", "pit", "2026-06-16", true, &mut used).unwrap();
        assert_ne!(g1, g2, "two games on the same day must bind DIFFERENT Kalshi events (no double-bind)");
        assert_eq!(g1, ("K-LAD-G1".into(), "K-PIT-G1".into()));
        assert_eq!(g2, ("K-LAD-G2".into(), "K-PIT-G2".into()));
        // a third pm game finds no remaining event -> None (not a re-bind of an already-used event).
        assert_eq!(pick_game(&kbydate, "lad", "pit", "2026-06-16", true, &mut used), None);
    }

    /// ±1-day fallback fires ONLY when the slug is undated AND the match is globally unique. The fallback
    /// is reached only when the EXACT date has no event (the exact arm runs first, unconditionally — the
    /// Python order); an ambiguous (>1 candidate) undated match then refuses rather than guess.
    #[test]
    fn pick_game_undated_fallback_only_when_unique() {
        // exact date "2026-06-15" has NO event; the only candidate is the adjacent 16th -> fallback binds.
        let mut kbydate = std::collections::HashMap::new();
        kbydate.insert("2026-06-16".to_string(), vec![ev(&[("lad", "K-LAD-16"), ("pit", "K-PIT-16")])]);
        let mut used = std::collections::HashSet::new();
        assert_eq!(pick_game(&kbydate, "lad", "pit", "2026-06-15", false, &mut used), Some(("K-LAD-16".into(), "K-PIT-16".into())));
        // exact date "2026-06-17" has NO event but TWO same-team candidates within ±1 (16th + 18th) ->
        // ambiguous -> refuse (the exact arm finds nothing, so the fallback's uniqueness check governs).
        let mut amb = std::collections::HashMap::new();
        amb.insert("2026-06-16".to_string(), vec![ev(&[("lad", "K-LAD-16"), ("pit", "K-PIT-16")])]);
        amb.insert("2026-06-18".to_string(), vec![ev(&[("lad", "K-LAD-18"), ("pit", "K-PIT-18")])]);
        let mut used2 = std::collections::HashSet::new();
        assert_eq!(pick_game(&amb, "lad", "pit", "2026-06-17", false, &mut used2), None, "ambiguous ±1 match must refuse");
        // a DATED slug never falls back even with a unique ±1 neighbour (slug_dated=true short-circuits).
        let mut used3 = std::collections::HashSet::new();
        assert_eq!(pick_game(&kbydate, "lad", "pit", "2026-06-15", true, &mut used3), None);
    }

    /// The civil-days epoch conversion (dep-free) against known anchors + a date arithmetic round-trip.
    #[test]
    fn ymd_epoch_days_known_anchors() {
        assert_eq!(ymd_to_epoch_days("1970-01-01"), Some(0));
        assert_eq!(ymd_to_epoch_days("1970-01-02"), Some(1));
        assert_eq!(ymd_to_epoch_days("2000-01-01"), Some(10957)); // 30y incl. leap days
        // a 5-day forward difference (the assemble test's days_to_event).
        let a = ymd_to_epoch_days("2026-06-11").unwrap();
        let b = ymd_to_epoch_days("2026-06-16").unwrap();
        assert_eq!(b - a, 5);
        // across a month boundary, and a leap-day.
        assert_eq!(ymd_to_epoch_days("2026-03-01").unwrap() - ymd_to_epoch_days("2026-02-28").unwrap(), 1);
        assert_eq!(ymd_to_epoch_days("2024-03-01").unwrap() - ymd_to_epoch_days("2024-02-28").unwrap(), 2); // 2024 leap
        assert_eq!(ymd_to_epoch_days("bad-date"), None);
    }

    /// A no-twin econ market (>=T whose T-step floor isn't listed) is skipped, not falsely paired.
    #[test]
    fn econ_ge_without_listed_twin_is_skipped() {
        // only floor 4.4 listed; pmus >=4.4 needs floor 4.3 -> no pair.
        fn stub(series: &str) -> Vec<Value> {
            if series == "KXU3" {
                vec![serde_json::from_str(r#"{"ticker":"KXU3-26JUN-T4.4","floor_strike":4.4,"yes_sub_title":"Above 4.4%"}"#).unwrap()]
            } else {
                vec![]
            }
        }
        let cat = vec![pm("urc-us-seasonadj-gte-june-2026-07-02-atl4pt4", "macro", "")];
        let d = assemble(&cat, false, None, stub);
        assert_eq!(d.econ_pairs, 0);
        assert_eq!(d.econ_skipped, 1);
    }
}
