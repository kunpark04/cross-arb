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

// The gateway catalog host (public, no-auth). SLUG IDENTITY VERIFIED LIVE (2026-06-11), not assumed: a
// `slug` discovered here is the SAME `marketSlug` the ORDER endpoint (`api.polymarket.us/v1/orders`,
// `exec::LiveBackend::pmus_base`) accepts — gateway-catalog slugs placed real BUY_LONG/BUY_SHORT orders.
const PM_MARKETS: &str = "https://gateway.polymarket.us/v1/markets";
// Unify on the bot's Kalshi host (CLAUDE.md: "unify Kalshi host"; venue.rs uses api.elections.kalshi.com).
const KALSHI_MARKETS: &str = "https://api.elections.kalshi.com/trade-api/v2/markets";
const PM_PAGE: usize = 500; // pmus offset page size (verified: page < limit = last page)
const PM_CATALOG_CAP: usize = 25_000; // safety cap; hitting it = TRUNCATED coverage (warn, don't loop forever).
// Raised 12k->25k 2026-06-13: live pmus catalog reached 15.3k (FIFA World Cup 2026 surge), so 12k was
// silently truncating ~3k markets including tracked-league games sorted into the tail.

const MON: [&str; 12] = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"];

/// One Kalshi weather bucket as `match_weather` consumes it: `(ticker, floor_strike, cap_strike)`.
type KBucket = (String, Option<i64>, Option<i64>);

/// A co-listed pair the live loop can subscribe + price. Weather/econ are 1:1 (one pmus slug <-> one
/// Kalshi ticker, `kalshi_b = None`). SPORTS moneyline is 2-outcome: `kalshi` = team-A ticker (the team
/// pmus lists as YES), `kalshi_b = Some(team-B ticker)` — both are subscribed and the game signal needs
/// both. A WORLD-CUP outcome is its OWN binary co-listed pair (kalshi_b = None, soccer = true), routed
/// through the weather/econ 1:1 path — a WC game's 3 outcomes are 3 such binaries, never a 3-leg basket.
#[derive(Clone, Debug, PartialEq)]
pub struct Pair {
    pub slug: String,        // pmus market slug (the WS subscribe key + book key)
    pub kalshi: String,      // the settlement-identical Kalshi ticker (team-A ticker for moneyline sports)
    pub kalshi_b: Option<String>, // moneyline SPORTS only: the team-B (away) Kalshi ticker; None for weather/econ/WC
    pub cat: Cat,
    pub cluster: String,     // correlated-exposure key (city-date / family-period / game)
    pub settle_clean: bool,  // weather=true (empirically verified); econ/moneyline-sports=false (recon open); WC=true (regulation-clean)
    /// WORLD-CUP per-outcome pair marker. Reuses `Cat::Sports` (so `lock_days`/divergence behave correctly
    /// for a near-dated game) but is a BINARY pair (kalshi_b=None, routed through `signal`, not `game_signal`).
    /// The flag carries the regulation-settlement basis and lets the live loop skip enrolling a WC pair in the
    /// MLB-only postponement poll (statsapi has no WC source). `false` for weather/econ/moneyline sports.
    pub soccer: bool,
    pub days_to_event: Option<f64>,
    /// pmus `orderPriceMinTickSize` (a number, e.g. 0.001) — the price tick a pmus ORDER must be a multiple
    /// of. `None` when the catalog omits it (the leg builder then keeps the whole-cent price unquantized).
    pub pm_min_tick: Option<f64>,
    /// pmus `minimumTradeQty` (can be < 1 or > 1) — the minimum order size pmus accepts. A configured qty
    /// below this would REJECT (leaving a naked leg), so the leg builder skips the fire. `None` = no minimum known.
    pub pm_min_qty: Option<f64>,
}

/// What a discovery pass produces. `pairs` is the subscribable 1:1 set; the rest is the coverage report
/// (`build_colisted_map`'s report) so an unmapped category / misaligned bucket is LOUD, not silent (L7).
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Discovery {
    pub pairs: Vec<Pair>,
    pub weather_pairs: usize,
    pub econ_pairs: usize,
    pub sports_pairs: usize,                 // moneyline: matched + emitted as 2-ticker subscribable Pairs (team A + B)
    pub soccer_pairs: usize,                 // WORLD CUP: per-outcome BINARY pairs (3 per bound game)
    pub weather_cities_unmapped: Vec<String>, // pmus lists these climate cities, WX map doesn't -> MISSED
    pub sports_leagues_unmapped: Vec<String>,
    pub soccer_leagues_unmapped: Vec<String>, // pmus lists these drawable-outcome leagues, SOCCER3 doesn't -> MISSED
    pub soccer_unbound: Vec<(String, String)>, // WC games binding by NEITHER exact code NOR name: (slug, why) -> LOUD miss
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

/// SOCCER 3-way (World Cup), kept SEPARATE from `LEAGUES_ABBREV` so the moneyline path is untouched. A WC
/// game is NOT a 2-team complementary market: pmus lists each outcome as its OWN binary
/// (`atc-fwc-<a>-<b>-<date>-<a|b|draw>`, `marketType="drawable_outcome"`) and so does Kalshi
/// (`KXWCGAME-…-<A>|-TIE|-<B>`). Each outcome is a clean binary co-listed pair, emitted PER-OUTCOME and
/// routed through the weather/econ 1:1 path. Only `fwc` (live match-winners) this build (port of `SOCCER3`).
const SOCCER3: [(&str, &str); 1] = [("fwc", "KXWCGAME")];

/// Canonical country key for the WC name-fallback join — port of `colisted_map.py::_norm_country`: fold the
/// Latin-1 accented letters to ASCII, lowercase, strip to `[a-z0-9]`. `"IR Iran"`->`"iriran"`,
/// `"Türkiye"`->`"turkiye"`, `"Côte d'Ivoire"`->`"cotedivoire"`. EXACT equality on this key is the join (no
/// substring/fuzzy — two DISTINCT countries must never share a key; verified for the full 48-country WC
/// field). Empty for an empty/missing name (a missing name must produce a non-matchable key). The accent
/// fold (vs Python's NFKD) keeps the Türkiye/Côte-d'Ivoire class name-equal across venues WITHOUT a unicode
/// crate — though those bind by exact CODE today, so the fold is belt-and-suspenders for a future mismatch.
fn norm_country(name: &str) -> String {
    name.chars()
        .map(fold_accent)
        .flat_map(|c| c.to_lowercase())
        .filter(|c| c.is_ascii_alphanumeric())
        .collect()
}

/// Fold a single Latin-1 accented letter to its base ASCII letter (the accents that appear in country
/// names: acute/grave/circumflex/diaeresis/tilde/ring/cedilla/slash). Non-accented chars pass through; a
/// non-ASCII char with no fold here is dropped by `norm_country`'s ascii-alphanumeric filter (matching
/// Python's `encode("ascii","ignore")`).
fn fold_accent(c: char) -> char {
    match c {
        'À'..='Å' | 'à'..='å' => 'a',
        'Ç' | 'ç' => 'c',
        'È'..='Ë' | 'è'..='ë' => 'e',
        'Ì'..='Ï' | 'ì'..='ï' => 'i',
        'Ñ' | 'ñ' => 'n',
        'Ò'..='Ö' | 'Ø' | 'ò'..='ö' | 'ø' => 'o',
        'Ù'..='Ü' | 'ù'..='ü' => 'u',
        'Ý' | 'ý' | 'ÿ' => 'y',
        other => other,
    }
}

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

/// A parsed World-Cup outcome slug `atc-fwc-<a>-<b>-<YYYY>-<MM>-<DD>-<outcome>`: `(a, b, date, outcome)`,
/// where `outcome` is the LAST segment ∈ {`a`, `b`, `draw`}. Port of `soc_parts`. `None` for any non-WC
/// slug (wrong prefix / too few segments) so a moneyline or weather slug never enters the soccer branch.
///
/// NOTE on the pmus YES side: discovery only MAPS slug<->ticker (it never reads catalog prices — like the
/// weather/econ branches). The L23 YES-orientation is honored where the price is actually read: the LIVE
/// pmus WS book is per-SLUG YES-oriented (`venue::parse_pmus_market_data`: `bids`=YES bids, `offers`=YES
/// asks for the subscribed outcome slug), so a WC outcome's YES book needs no `marketSides` Yes-side read.
fn soc_parts(slug: &str) -> Option<(String, String, String, String)> {
    let p: Vec<&str> = slug.split('-').collect();
    if p.len() < 8 || p[0] != "atc" || p[1] != "fwc" {
        return None;
    }
    let date = format!("{}-{}-{}", p[4], p[5], p[6]);
    Some((p[2].to_string(), p[3].to_string(), date, p[p.len() - 1].to_string()))
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

/// CPI period: `...mayYYYYyoy...` -> `YYMMM`. Port of the cpic branch's `re.search((month)[a-z]*?(\d{4})yoy)`
/// — LEFTMOST match in the string (NOT calendar order): on two `monthYYYYyoy` tokens, the textually-first
/// wins (the old Jan→Dec scan returned the calendar-earliest, diverging from Python).
fn parse_cpi_period(s: &str) -> Option<String> {
    // a month token that begins at byte `p`, then (optional trailing alpha) 4 digits + "yoy" -> the year.
    let match_at = |p: usize, mi: usize| -> Option<String> {
        let ml = MON[mi].to_ascii_lowercase();
        let after = s.get(p + ml.len()..)?;
        let digits_start = after.find(|c: char| c.is_ascii_digit())?;
        if !after[..digits_start].chars().all(|c| c.is_ascii_alphabetic()) {
            return None;
        }
        let rest = &after[digits_start..];
        if rest.len() >= 4 && rest.as_bytes()[..4].iter().all(|b| b.is_ascii_digit()) && rest[4..].starts_with("yoy") {
            return Some(format!("{}{}", &rest[2..4], MON[mi]));
        }
        None
    };
    // pick the LEFTMOST validating month-token start (min byte position), matching re.search. Scan ALL
    // occurrences of each month (not just the first) so a non-validating earlier hit can't mask a valid
    // later one of the same month.
    let mut best: Option<(usize, String)> = None;
    for (mi, m) in MON.iter().enumerate() {
        let ml = m.to_ascii_lowercase();
        let mut from = 0;
        while let Some(rel) = s[from..].find(&ml) {
            let p = from + rel;
            if let Some(period) = match_at(p, mi) {
                if best.as_ref().is_none_or(|(bp, _)| p < *bp) {
                    best = Some((p, period));
                }
                break; // earliest occurrence of THIS month that validates is its best candidate
            }
            from = p + ml.len();
        }
    }
    best.map(|(_, period)| period)
}

/// urc/nfpc period: a `-<monthname>-` token + a release date `YYYY-MM-DD`; data-year = release-year - 1
/// when data-month > release-month (Dec data releases in Jan). Port of the urc/nfpc branch.
fn parse_release_period(s: &str) -> Option<String> {
    // data-month name token between dashes. The whole segment must be PURELY ALPHABETIC (Python's
    // `-(month)[a-z]*-` left/right alpha boundary): a digit-bearing decoy like `jun2`/`may1adj` is NOT a
    // month token, so the scan skips it to the real `-june-` (the bare 3-char-prefix test mis-read it).
    let dmon = s
        .split('-')
        .filter(|seg| seg.chars().all(|c| c.is_ascii_alphabetic()))
        .find_map(|seg| emon(&seg.chars().take(3).collect::<String>()))?;
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

/// Bind a WORLD-CUP game (Kalshi abbrevs A, B already aliased) to ONE `KXWCGAME` event's `{suffix: ticker}`
/// set on its EXACT date, returning the THREE tickers `(team_a, team_b, tie)`. Soccer-3way analogue of
/// `pick_game`: the pmus slug always carries the ET date (`soc_parts` requires it), so the bind is exact-date
/// only — no ±1 fallback needed. Requires all three of A, B and a `"tie"` suffix to resolve to DISTINCT
/// tickers (a partial WC event -> skip, never a partial bind; L1). `used` marks each bound event so two pmus
/// games on the same date can't both grab the first event (the doubleheader guard; WC has none, but the guard
/// is free correctness and mirrors the moneyline path). FAITHFUL to `soccer3_emit`'s `pick_game` + `pl["tie"]`.
fn pick_wc_game(
    kbydate: &std::collections::HashMap<String, Vec<std::collections::HashMap<String, String>>>,
    ka: &str,
    kb: &str,
    date: &str,
    used: &mut std::collections::HashSet<(String, usize)>,
) -> Option<(String, String, String)> {
    let events = kbydate.get(date)?;
    for (i, ev) in events.iter().enumerate() {
        if used.contains(&(date.to_string(), i)) {
            continue;
        }
        // need team-A, team-B AND a TIE suffix, all DISTINCT (no false/partial bind).
        if let (Some((ta, tb)), Some(tie)) = (resolve_two(ev, ka, kb), ev.get("tie")) {
            if ta != *tie && tb != *tie {
                used.insert((date.to_string(), i));
                return Some((ta, tb, tie.clone()));
            }
        }
    }
    None
}

/// The full country name of a pmus WC outcome market: `marketSides[].team.name` (BOTH the Yes and No side
/// of a `-<code>` market carry the SAME team, so the first non-empty wins). Empty for a `-draw` market
/// (team is null) or any market without a named team. Port of `colisted_map.py::pm_team_name`.
fn pm_wc_team_name(m: &Value) -> String {
    m.get("marketSides")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .find_map(|s| s.get("team").and_then(|t| t.get("name")).and_then(Value::as_str).filter(|n| !n.is_empty()))
        .unwrap_or("")
        .to_string()
}

/// Resolve the pmus codes `(a, b)` to the Kalshi event suffixes to bind. PRIMARY: the exact 3-letter code
/// (FIFA codes are unique -> safe). FALLBACK (the residue where venues use different codes, e.g. pmus `irn`
/// / Kalshi `iri`): the NORMALIZED full team NAME — pmus `marketSides[].team.name` vs Kalshi `yes_sub_title`,
/// EXACT-equal after `norm_country` (no substring/fuzzy, L1). Each returned code is the exact code if it
/// appears on SOME Kalshi event for `date`, else the name-matched suffix, else the original code (so
/// `pick_wc_game` then fails -> the game is reported unbound, never falsely bound). Port of `_wc_resolve_codes`.
fn wc_resolve_codes(
    a: &str,
    b: &str,
    pa: &Value,
    pb: &Value,
    knames_for_date: &[std::collections::HashMap<String, String>],
) -> (String, String) {
    let mut sufs: std::collections::HashSet<&str> = std::collections::HashSet::new();
    let mut name2suf: std::collections::HashMap<String, &str> = std::collections::HashMap::new();
    for ev in knames_for_date {
        for (suf, nm) in ev {
            sufs.insert(suf.as_str());
            let nz = norm_country(nm);
            if !nz.is_empty() {
                name2suf.entry(nz).or_insert(suf.as_str()); // first wins (mirrors Python setdefault)
            }
        }
    }
    let resolve = |code: &str, pm_mkt: &Value| -> String {
        if sufs.contains(code) {
            return code.to_string(); // exact code present -> primary path
        }
        // else exact-normalized-name fallback; neither -> keep the code (pick_wc_game then fails -> unbound).
        name2suf.get(&norm_country(&pm_wc_team_name(pm_mkt))).map(|s| s.to_string()).unwrap_or_else(|| code.to_string())
    };
    (resolve(a, pa), resolve(b, pb))
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
                    let (pm_min_tick, pm_min_qty) = pm_order_constraints(pm);
                    d.pairs.push(Pair {
                        slug,
                        kalshi: m.kalshi,
                        kalshi_b: None, // weather is 1:1
                        cat: Cat::Weather,
                        cluster: m.cluster,
                        settle_clean: m.settle_clean,
                        soccer: false,
                        days_to_event: m.days_to_event,
                        pm_min_tick,
                        pm_min_qty,
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
            let q = matcher::EconQuery {
                ineq: p.ineq,
                thr: p.thr,
                step: step.unwrap_or(0.0),
                fed_label: p.fed_label.as_deref(),
                period: &per,
                family,
            };
            match matcher::match_econ(&q, floors, labels) {
                Some(m) => {
                    let (pm_min_tick, pm_min_qty) = pm_order_constraints(pm);
                    d.pairs.push(Pair {
                        slug,
                        kalshi: m.kalshi,
                        kalshi_b: None, // econ is 1:1
                        cat: Cat::Econ,
                        cluster: m.cluster,
                        settle_clean: m.settle_clean,
                        soccer: false,
                        days_to_event: m.days_to_event,
                        pm_min_tick,
                        pm_min_qty,
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
        // DETERMINISM: `by_event` is a HashMap (random iteration order), so push events sorted by
        // event_ticker. Otherwise each date's event INDEX `i` (the `used`-set key in pick_game) is random
        // per process, and a same-team doubleheader could bind game-1 on one re-discovery and game-2 on the
        // next — swapping a held position's Kalshi tickers. Sorted push gives a stable index across passes.
        let mut events: Vec<(String, std::collections::HashMap<String, String>)> = by_event.into_iter().collect();
        events.sort_by(|a, b| a.0.cmp(&b.0));
        for (ev, dict) in events {
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
            let (pm_min_tick, pm_min_qty) = pm_order_constraints(pm);
            d.pairs.push(Pair {
                slug,
                kalshi: ta,
                kalshi_b: Some(tb),
                cat: Cat::Sports,
                cluster: format!("{league}-{date}"),
                settle_clean: false, // only a game that COMPLETES on schedule settles identically (void tail)
                soccer: false, // moneyline sports, not World Cup
                days_to_event,
                pm_min_tick,
                pm_min_qty,
            });
            d.sports_pairs += 1;
        }
    }
    d.sports_leagues_unmapped = pm_leagues.iter().filter(|l| !LEAGUES_ABBREV.iter().any(|(x, _)| *x == l.as_str())).cloned().collect();

    // ---- SOCCER 3-way (World Cup): each of the 3 outcomes is its OWN binary on BOTH venues -> emit each as
    //      a PER-OUTCOME BINARY Pair (kalshi_b=None, soccer=true), routed through the weather/econ 1:1 signal
    //      path (NOT the 2-team game_signal). pmus is 3 sibling slugs grouped per (a,b,date); Kalshi is the
    //      KXWCGAME event's 3 tickers (team A/B + TIE). Reuses the exact-date + used-set bind (pick_wc_game);
    //      the country code is matched EXACT-first, then by normalized full team NAME (team.name vs
    //      yes_sub_title) for the venue-code-mismatch residue — a game that binds by NEITHER is reported in
    //      `soccer_unbound` (LOUD), never silently missed. Port of the colisted_map soccer3 branch.
    //      group pmus drawable-outcome WC markets: (a,b,date) -> {outcome_token: market}.
    let mut pm_soc: std::collections::HashMap<(String, String, String), std::collections::HashMap<String, &Value>> =
        std::collections::HashMap::new();
    let mut soc_leagues: std::collections::BTreeSet<String> = std::collections::BTreeSet::new();
    for m in pm_markets {
        if field_str(m, "category").as_deref() != Some("sports") || field_str(m, "marketType").as_deref() != Some("drawable_outcome") {
            continue;
        }
        let Some(slug) = field_str(m, "slug") else { continue };
        // league token = the slug's 2nd segment (`atc-fwc-...` -> `fwc`); record it for the coverage audit.
        if let Some(l) = pm_league(&slug) {
            soc_leagues.insert(l);
        }
        if let Some((a, b, date, outcome)) = soc_parts(&slug) {
            pm_soc.entry((a, b, date)).or_default().insert(outcome, m);
        }
    }
    for (league, kser) in SOCCER3 {
        // `pm_soc` only ever holds `atc-fwc-` games (soc_parts requires the fwc prefix), so a non-empty map
        // means the one configured league (`fwc`) is live. Mirrors the Python `if not pm_soc: continue`.
        if pm_soc.is_empty() {
            continue;
        }
        let kmarkets = kalshi_series(kser);
        // group Kalshi KXWCGAME markets by EVENT -> {suffix: ticker} (suffix = country code or `tie`) AND a
        // PARALLEL {suffix: yes_sub_title} name map (the name-fallback bridge), recording each event's date.
        let mut by_event: std::collections::HashMap<String, std::collections::HashMap<String, String>> = std::collections::HashMap::new();
        let mut by_name: std::collections::HashMap<String, std::collections::HashMap<String, String>> = std::collections::HashMap::new();
        let mut event_date: std::collections::HashMap<String, String> = std::collections::HashMap::new();
        for m in &kmarkets {
            let Some(tk) = field_str(m, "ticker") else { continue };
            let Some(ev) = field_str(m, "event_ticker") else { continue };
            event_date.entry(ev.clone()).or_insert_with(|| ktok_date(&tk).unwrap_or_default());
            let suffix = tk.rsplit('-').next().unwrap_or("").to_ascii_lowercase();
            if !suffix.is_empty() {
                let name = field_str(m, "yes_sub_title").unwrap_or_default();
                by_event.entry(ev.clone()).or_default().insert(suffix.clone(), tk);
                by_name.entry(ev).or_default().insert(suffix, name);
            }
        }
        // DETERMINISM (mirrors the moneyline path): events come out of a HashMap (random order), so push
        // sorted by event_ticker -> a stable per-date event INDEX for the used-set across re-discovery passes.
        // kbydate (suffix->ticker) and kbydate_names (suffix->name) are pushed in the SAME order -> aligned.
        let mut kbydate: std::collections::HashMap<String, Vec<std::collections::HashMap<String, String>>> = std::collections::HashMap::new();
        let mut kbydate_names: std::collections::HashMap<String, Vec<std::collections::HashMap<String, String>>> = std::collections::HashMap::new();
        let mut event_tickers: Vec<String> = by_event.keys().cloned().collect();
        event_tickers.sort();
        for ev in event_tickers {
            let date = event_date.get(&ev).cloned().unwrap_or_default();
            kbydate.entry(date.clone()).or_default().push(by_event.remove(&ev).unwrap_or_default());
            kbydate_names.entry(date).or_default().push(by_name.remove(&ev).unwrap_or_default());
        }
        // one used-set per league pass: a bound Kalshi event can't bind a second pm game (doubleheader guard).
        let mut used: std::collections::HashSet<(String, usize)> = std::collections::HashSet::new();
        // bind games in a STABLE (sorted-key) order — `pm_soc` is a HashMap (random iteration), so without a
        // deterministic order the used-set could bind two same-date games to swapped events across passes.
        let mut game_keys: Vec<&(String, String, String)> = pm_soc.keys().collect();
        game_keys.sort();
        let no_names: Vec<std::collections::HashMap<String, String>> = Vec::new();
        for key in game_keys {
            let (a, b, date) = key;
            let outs = &pm_soc[key];
            // need all 3 sibling outcomes (team A / team B / draw) before binding — no partial game (L1).
            let (Some(pa), Some(pb), Some(pdraw)) = (outs.get(a.as_str()), outs.get(b.as_str()), outs.get("draw")) else {
                continue;
            };
            // resolve codes EXACT-first then by NAME (supersedes the old alias table); a code+name double-miss
            // keeps the original code so pick_wc_game fails -> reported unbound below.
            let knames_for_date = kbydate_names.get(date).unwrap_or(&no_names);
            let (ka, kb) = wc_resolve_codes(a, b, pa, pb, knames_for_date);
            let Some((ta, tb, tie)) = pick_wc_game(&kbydate, &ka, &kb, date, &mut used) else {
                // LOUD miss: bound by neither exact code nor name -> record the pmus slug + the unmatched names.
                d.soccer_unbound.push((
                    field_str(pa, "slug").unwrap_or_default(),
                    format!("{a}={:?}/{b}={:?} date={date}", pm_wc_team_name(pa), pm_wc_team_name(pb)),
                ));
                continue;
            };
            // days_to_event = game date - today (in days); None when either date is unknown -> gate dormant.
            let days_to_event = match (today_epoch_days, ymd_to_epoch_days(date)) {
                (Some(today), Some(game)) => Some((game - today) as f64),
                _ => None,
            };
            let cluster = format!("{league}-{a}-{b}-{date}"); // per-GAME correlated-exposure cluster (3 outcomes)
            // emit each outcome as its OWN binary Pair: -a<->team-A ticker, -b<->team-B ticker, -draw<->TIE.
            for (pm_mkt, ticker) in [(pa, ta), (pb, tb), (pdraw, tie)] {
                let Some(slug) = field_str(pm_mkt, "slug") else { continue };
                let (pm_min_tick, pm_min_qty) = pm_order_constraints(pm_mkt);
                d.pairs.push(Pair {
                    slug,
                    kalshi: ticker,
                    kalshi_b: None, // WC is per-outcome BINARY (1:1), like weather/econ
                    cat: Cat::Sports, // reuse Sports: correct lock_days/divergence; binary via kalshi_b=None
                    cluster: cluster.clone(),
                    // regulation-clean on both venues (the small priceable void tail is far below a tradeable
                    // edge) -> the bot trades it via the existing edge floor + settle_clean, mirroring the
                    // Python TAIL verdict. settle_clean=true passes the risk gate's invariant-#1 clause.
                    settle_clean: true,
                    soccer: true,
                    days_to_event,
                    pm_min_tick,
                    pm_min_qty,
                });
                d.soccer_pairs += 1;
            }
        }
    }
    d.soccer_leagues_unmapped = soc_leagues.iter().filter(|l| !SOCCER3.iter().any(|(x, _)| *x == l.as_str())).cloned().collect();

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

/// The pmus market object's order constraints: `(orderPriceMinTickSize, minimumTradeQty)` — both a number
/// (the catalog sends `orderPriceMinTickSize` e.g. 0.001, and `minimumTradeQty` which can be <1 or >1). A
/// missing/non-positive value -> `None` (the leg builder then leaves the price unquantized / skips the
/// min-qty check). Threaded onto `Pair` -> `LivePair` so the leg builder can quantize + size-check a pmus leg.
fn pm_order_constraints(m: &Value) -> (Option<f64>, Option<f64>) {
    let pos = |v: Option<f64>| v.filter(|x| x.is_finite() && *x > 0.0);
    (pos(field_f64(m, "orderPriceMinTickSize")), pos(field_f64(m, "minimumTradeQty")))
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
        .chain(SOCCER3.iter().map(|(_, k)| *k)) // WC: KXWCGAME — was MISSING, so the soccer branch saw an
                                                 // empty cache and bound 0 (live: Kalshi lists 186 WC markets)
        .collect();
    for (i, kser) in needed.iter().enumerate() {
        let markets = pull_kalshi_series(http, kser).await?;
        series_cache.insert(kser.to_string(), markets);
        // inter-series pacing (ports colisted_map.py's `time.sleep(0.25)`) — a burst across all ~18 series
        // with no gap trips Kalshi's rate limit (429); the per-call retry above is the backstop.
        if i + 1 < needed.len() {
            tokio::time::sleep(std::time::Duration::from_millis(250)).await;
        }
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

/// Pull one Kalshi series' OPEN markets, paginated by `cursor` (empty/absent cursor = last page). Bounded
/// by a page cap + a stuck-cursor break (`next == cursor`) + a row cap, mirroring the pmus `PM_CATALOG_CAP`
/// — a buggy endpoint returning the SAME non-empty cursor every page would otherwise loop forever and grow
/// `all` without bound (hang + OOM on the 24/7 droplet, blocking the refresh task). 50×1000 has headroom.
async fn pull_kalshi_series(http: &reqwest::Client, series: &str) -> Result<Vec<Value>, String> {
    let mut all = Vec::new();
    let mut cursor: Option<String> = None;
    let mut pages = 0usize;
    loop {
        let mut url = format!("{KALSHI_MARKETS}?series_ticker={series}&status=open&limit=1000");
        if let Some(c) = &cursor {
            url.push_str(&format!("&cursor={c}"));
        }
        let v = fetch_json(http, &url).await?;
        if let Some(arr) = v.get("markets").and_then(Value::as_array) {
            all.extend(arr.iter().cloned());
        }
        let next = v.get("cursor").and_then(Value::as_str).filter(|c| !c.is_empty()).map(str::to_string);
        pages += 1;
        if cursor_loop_done(&next, &cursor, pages, all.len()) {
            return Ok(all);
        }
        cursor = next;
    }
}

/// Termination predicate for the Kalshi cursor pagination: stop on an empty/absent cursor (last page),
/// a self-referential cursor (`next == prev`, a stuck endpoint), the page cap, or the row cap — so the
/// loop can never spin forever / OOM. Pure, so it's unit-testable without live HTTP.
fn cursor_loop_done(next: &Option<String>, prev: &Option<String>, pages: usize, rows: usize) -> bool {
    next.is_none() || next == prev || pages > 50 || rows > 50_000
}

/// GET a catalog page and return its `key` array (`markets`). A non-2xx or non-JSON body is an error
/// (mirrors colisted_map.py's degraded-pass model — the caller skips pruning on a degraded pass).
async fn fetch_markets(http: &reqwest::Client, url: &str, key: &str) -> Result<Vec<Value>, String> {
    let v = fetch_json(http, url).await?;
    Ok(v.get(key).and_then(Value::as_array).cloned().unwrap_or_default())
}

/// GET -> parsed JSON `Value`, with RETRY on 429 + transient 5xx. VERIFIED necessary live (2026-06-11): a
/// cold-start discovery burst across the ~18 tracked Kalshi series 429s on the first pull without it, and
/// the `?` propagation then aborts the WHOLE discovery (loop starts empty). Ports `colisted_map.py::get`
/// (`tries=4`; retry only {429,500,502,503,504} + transport errors; backoff `1.5*(i+1)` s; a 4xx that
/// isn't 429 is permanent). Reads the body as text first so a non-JSON error page is a clean error, not a
/// decode panic. PUBLIC endpoint — NO auth headers.
async fn fetch_json(http: &reqwest::Client, url: &str) -> Result<Value, String> {
    let short = |u: &str| u.split('?').next().unwrap_or(u).to_string();
    let tries = 4u32;
    let mut last = String::new();
    for attempt in 0..tries {
        match http
            .get(url)
            .header("User-Agent", "cross-arb/1.0")
            .header("Accept", "application/json")
            .send()
            .await
        {
            Ok(resp) => {
                let status = resp.status();
                let text = resp.text().await.unwrap_or_default();
                if status.is_success() {
                    return serde_json::from_str(&text).map_err(|e| format!("parse {}: {e}", short(url)));
                }
                let code = status.as_u16();
                last = format!("GET {} -> {}", short(url), code);
                if !matches!(code, 429 | 500 | 502 | 503 | 504) || attempt == tries - 1 {
                    return Err(last); // permanent (non-429 4xx) or out of retries
                }
            }
            Err(e) => {
                last = format!("GET {}: {e}", short(url));
                if attempt == tries - 1 {
                    return Err(last);
                }
            }
        }
        tokio::time::sleep(std::time::Duration::from_millis(1500 * (attempt as u64 + 1))).await;
    }
    Err(last)
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
        // TWO monthYYYYyoy tokens: Python re.search picks the LEFTMOST in the string (25SEP), NOT the
        // calendar-earliest (the old Jan->Dec scan returned 26MAR). Regression for the leftmost-match fix.
        let two = econ_parse("cpic-sep2025yoy-mar2026yoy-2026-06-10-lte3pt8pct").unwrap();
        assert_eq!(two.period.as_deref(), Some("25SEP"), "leftmost monthYYYYyoy wins, not calendar order");
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
        // DIGIT-BEARING decoy month segment must be SKIPPED (Python's -(month)[a-z]*- needs a pure-alpha
        // token): `jun2`/`may1adj` are not months -> the real `-july-` wins (was a Rust-only 26JUN/26MAY).
        assert_eq!(econ_parse("urc-jun2-gte-july-2026-08-02-atl4pt4").unwrap().period.as_deref(), Some("26JUL"));
        assert_eq!(econ_parse("urc-may1adj-gte-july-2026-08-02-atl4pt4").unwrap().period.as_deref(), Some("26JUL"));
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
        // (filter `!soccer`: a moneyline sports pair is `Cat::Sports` too, but this catalog has no WC.)
        assert_eq!(d.sports_pairs, 1);
        assert_eq!(d.soccer_pairs, 0, "no World Cup markets in this catalog");
        let sp: Vec<&Pair> = d.pairs.iter().filter(|p| p.cat == Cat::Sports && !p.soccer).collect();
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

    /// FIX C: the pmus market object's `orderPriceMinTickSize` + `minimumTradeQty` are parsed onto the Pair
    /// (threaded to the leg builder so a pmus leg can be quantized + size-checked). A market omitting them
    /// yields `None` (leg builder leaves the price unquantized / skips the min-qty check). Checked on weather.
    #[test]
    fn assemble_threads_pmus_order_constraints_onto_pair() {
        // a weather bucket WITH order constraints (tick 0.001, min qty 5) + one WITHOUT them.
        let pm_cat = vec![
            pm("tc-temp-sfohigh-2026-06-09-gte64lt65f", "climate", r#""orderPriceMinTickSize":0.001,"minimumTradeQty":5"#),
            pm("tc-temp-sfohigh-2026-06-09-gte72f", "climate", ""),
        ];
        fn stub(series: &str) -> Vec<Value> {
            if series == "KXHIGHTSFO" {
                vec![
                    serde_json::from_str(r#"{"ticker":"KXHIGHTSFO-26JUN09-B64","floor_strike":64,"cap_strike":65,"yes_sub_title":"64 to 65"}"#).unwrap(),
                    serde_json::from_str(r#"{"ticker":"KXHIGHTSFO-26JUN09-T72","floor_strike":71,"yes_sub_title":"72 or above"}"#).unwrap(),
                ]
            } else {
                vec![]
            }
        }
        let d = assemble(&pm_cat, false, None, stub);
        let with = d.pairs.iter().find(|p| p.slug.ends_with("gte64lt65f")).expect("constrained bucket paired");
        assert_eq!(with.pm_min_tick, Some(0.001), "orderPriceMinTickSize parsed");
        assert_eq!(with.pm_min_qty, Some(5.0), "minimumTradeQty parsed (can be >1)");
        let without = d.pairs.iter().find(|p| p.slug.ends_with("gte72f")).expect("plain bucket paired");
        assert_eq!((without.pm_min_tick, without.pm_min_qty), (None, None), "absent constraints -> None");
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

    /// Stuck-cursor / page-cap termination for the Kalshi pagination (the pure predicate). A self-
    /// referential cursor (`next == prev`), an absent cursor, the page cap, and the row cap all stop the
    /// loop; only genuine forward progress (a NEW non-empty cursor under the caps) continues.
    #[test]
    fn cursor_loop_terminates_on_stuck_or_caps() {
        let c = |s: &str| Some(s.to_string());
        // forward progress: new cursor, under caps -> keep going.
        assert!(!cursor_loop_done(&c("p2"), &c("p1"), 1, 10));
        assert!(!cursor_loop_done(&c("p2"), &None, 1, 10)); // first page -> next page
        // STUCK: the endpoint returns the SAME non-empty cursor -> stop (would loop forever otherwise).
        assert!(cursor_loop_done(&c("p1"), &c("p1"), 5, 100));
        // last page: empty/absent cursor -> stop.
        assert!(cursor_loop_done(&None, &c("p9"), 3, 100));
        // page cap + row cap -> stop even with a fresh cursor.
        assert!(cursor_loop_done(&c("pN"), &c("pM"), 51, 100));
        assert!(cursor_loop_done(&c("pN"), &c("pM"), 2, 50_001));
    }

    /// DETERMINISM (doubleheader): two re-discovery passes must bind the SAME Kalshi tickers to the same
    /// pm games. The Kalshi events come out of a HashMap (random iteration), so without the event_ticker
    /// sort a same-team DH could swap game-1/game-2 across passes under a live position. Run assemble many
    /// times; every pass must produce byte-identical sports bindings.
    #[test]
    fn sports_doubleheader_binding_is_deterministic_across_passes() {
        // two pm MLB games, same teams + date (a doubleheader); distinct slugs so both are tracked.
        let pm_cat = vec![
            pm(
                "aec-mlb-lad-pit-2026-06-16-g1",
                "sports",
                r#""marketType":"moneyline","gameStartTime":"2026-06-16T18:00:00Z","marketSides":[{"long":true,"team":{"name":"Los Angeles Dodgers","abbreviation":"LAD"}},{"long":false,"team":{"name":"Pittsburgh Pirates","abbreviation":"PIT"}}]"#,
            ),
            pm(
                "aec-mlb-lad-pit-2026-06-16-g2",
                "sports",
                r#""marketType":"moneyline","gameStartTime":"2026-06-16T21:00:00Z","marketSides":[{"long":true,"team":{"name":"Los Angeles Dodgers","abbreviation":"LAD"}},{"long":false,"team":{"name":"Pittsburgh Pirates","abbreviation":"PIT"}}]"#,
            ),
        ];
        // two Kalshi events on the SAME date, same team abbrevs, DISTINCT event tickers + full tickers.
        fn dh_stub(series: &str) -> Vec<Value> {
            if series == "KXMLBGAME" {
                vec![
                    serde_json::from_str(r#"{"ticker":"KXMLBGAME-26JUN16LADPITG2-LAD","event_ticker":"KXMLBGAME-26JUN16LADPITG2","yes_sub_title":"Los Angeles Dodgers"}"#).unwrap(),
                    serde_json::from_str(r#"{"ticker":"KXMLBGAME-26JUN16LADPITG2-PIT","event_ticker":"KXMLBGAME-26JUN16LADPITG2","yes_sub_title":"Pittsburgh Pirates"}"#).unwrap(),
                    serde_json::from_str(r#"{"ticker":"KXMLBGAME-26JUN16LADPITG1-LAD","event_ticker":"KXMLBGAME-26JUN16LADPITG1","yes_sub_title":"Los Angeles Dodgers"}"#).unwrap(),
                    serde_json::from_str(r#"{"ticker":"KXMLBGAME-26JUN16LADPITG1-PIT","event_ticker":"KXMLBGAME-26JUN16LADPITG1","yes_sub_title":"Pittsburgh Pirates"}"#).unwrap(),
                ]
            } else {
                vec![]
            }
        }
        // the canonical binding (sorted by event_ticker: G1 < G2): g1 slug -> G1 event, g2 slug -> G2 event.
        let bindings = |d: &Discovery| -> Vec<(String, String, Option<String>)> {
            let mut v: Vec<(String, String, Option<String>)> = d
                .pairs
                .iter()
                .filter(|p| p.cat == Cat::Sports)
                .map(|p| (p.slug.clone(), p.kalshi.clone(), p.kalshi_b.clone()))
                .collect();
            v.sort();
            v
        };
        let first = bindings(&assemble(&pm_cat, false, None, dh_stub));
        assert_eq!(first.len(), 2, "both doubleheader games bind");
        // many passes — each builds a fresh HashMap with a fresh random seed; all must agree with `first`.
        for _ in 0..50 {
            assert_eq!(bindings(&assemble(&pm_cat, false, None, dh_stub)), first, "DH binding must be stable across re-discovery");
        }
        // and the binding is the event_ticker-sorted one (G1 ticker for the first slug after sort).
        let g1 = first.iter().find(|(s, ..)| s.ends_with("-g1")).unwrap();
        assert_eq!(g1.1, "KXMLBGAME-26JUN16LADPITG1-LAD");
        assert_eq!(g1.2.as_deref(), Some("KXMLBGAME-26JUN16LADPITG1-PIT"));
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

    // ---- SOCCER 3-way (World Cup): per-outcome BINARY emission, alias join, L23 YES-read, no false join ----

    /// `soc_parts` parses an `atc-fwc-<a>-<b>-<date>-<outcome>` slug into (a, b, date, outcome) and rejects
    /// any non-WC slug (so a moneyline / weather slug never enters the soccer branch).
    #[test]
    fn soc_parts_parses_wc_and_rejects_others() {
        assert_eq!(
            soc_parts("atc-fwc-ger-cuw-2026-06-14-ger"),
            Some(("ger".into(), "cuw".into(), "2026-06-14".into(), "ger".into()))
        );
        assert_eq!(soc_parts("atc-fwc-ger-cuw-2026-06-14-draw").map(|t| t.3), Some("draw".into()));
        assert_eq!(soc_parts("atc-fwc-irn-nzl-2026-06-15-irn").map(|t| (t.0, t.3)), Some(("irn".into(), "irn".into())));
        // non-WC slugs -> None.
        assert!(soc_parts("aec-mlb-min-tex-2026-06-16").is_none());
        assert!(soc_parts("tc-temp-sfohigh-2026-06-09-gte64lt65f").is_none());
        assert!(soc_parts("atc-fwc-ger").is_none()); // too few segments
    }

    /// The WC Kalshi stub: one KXWCGAME event per game with three tickers (team A / team B / TIE).
    fn wc_kalshi_stub(series: &str) -> Vec<Value> {
        let raw = match series {
            "KXWCGAME" => vec![
                // GER vs CUW on 26JUN14 (exact-abbrev join).
                r#"{"ticker":"KXWCGAME-26JUN14GERCUW-GER","event_ticker":"KXWCGAME-26JUN14GERCUW","yes_sub_title":"Germany"}"#,
                r#"{"ticker":"KXWCGAME-26JUN14GERCUW-CUW","event_ticker":"KXWCGAME-26JUN14GERCUW","yes_sub_title":"Curacao"}"#,
                r#"{"ticker":"KXWCGAME-26JUN14GERCUW-TIE","event_ticker":"KXWCGAME-26JUN14GERCUW","yes_sub_title":"Draw"}"#,
                // IRI vs NZL on 26JUN15 — Kalshi uses `iri`; pmus uses `irn` (the alias case).
                r#"{"ticker":"KXWCGAME-26JUN15IRINZL-IRI","event_ticker":"KXWCGAME-26JUN15IRINZL","yes_sub_title":"IR Iran"}"#,
                r#"{"ticker":"KXWCGAME-26JUN15IRINZL-NZL","event_ticker":"KXWCGAME-26JUN15IRINZL","yes_sub_title":"New Zealand"}"#,
                r#"{"ticker":"KXWCGAME-26JUN15IRINZL-TIE","event_ticker":"KXWCGAME-26JUN15IRINZL","yes_sub_title":"Draw"}"#,
            ],
            _ => vec![],
        };
        raw.into_iter().map(|s| serde_json::from_str(s).unwrap()).collect()
    }

    /// A complete WC game (3 pmus sibling slugs) -> exactly 3 PER-OUTCOME BINARY Pairs, each with ONE Kalshi
    /// ticker (kalshi_b=None) + soccer=true + settle_clean=true + Cat::Sports. draw maps to the event TIE
    /// ticker. This is the core of the WC discovery branch: per-outcome binary, NOT a 2-team game pair.
    #[test]
    fn soccer3_emits_three_per_outcome_binary_pairs() {
        let pm_cat = vec![
            pm("atc-fwc-ger-cuw-2026-06-14-ger", "sports", r#""marketType":"drawable_outcome""#),
            pm("atc-fwc-ger-cuw-2026-06-14-cuw", "sports", r#""marketType":"drawable_outcome""#),
            pm("atc-fwc-ger-cuw-2026-06-14-draw", "sports", r#""marketType":"drawable_outcome""#),
        ];
        // today 2026-06-13; game 2026-06-14 -> days_to_event = 1.
        let today = ymd_to_epoch_days("2026-06-13");
        let d = assemble(&pm_cat, false, today, wc_kalshi_stub);
        assert_eq!(d.soccer_pairs, 3, "3 siblings -> 3 per-outcome binary pairs");
        let wc: Vec<&Pair> = d.pairs.iter().filter(|p| p.soccer).collect();
        assert_eq!(wc.len(), 3);
        // EVERY WC pair is BINARY (kalshi_b=None), soccer, settle_clean, Cat::Sports, with the per-game cluster.
        for p in &wc {
            assert!(p.kalshi_b.is_none(), "each WC outcome is a BINARY pair (no kalshi_b)");
            assert!(p.soccer && p.settle_clean && p.cat == Cat::Sports);
            assert_eq!(p.cluster, "fwc-ger-cuw-2026-06-14");
            assert_eq!(p.days_to_event, Some(1.0));
        }
        // the three outcomes map to the three event tickers: -ger<->GER, -cuw<->CUW, -draw<->TIE.
        let by_slug = |suf: &str| wc.iter().find(|p| p.slug.ends_with(suf)).unwrap().kalshi.as_str();
        assert_eq!(by_slug("-ger"), "KXWCGAME-26JUN14GERCUW-GER");
        assert_eq!(by_slug("-cuw"), "KXWCGAME-26JUN14GERCUW-CUW");
        assert_eq!(by_slug("-draw"), "KXWCGAME-26JUN14GERCUW-TIE", "draw <-> TIE");
        // moneyline sports is untouched: no moneyline markets here -> no 2-ticker sports pairs.
        assert_eq!(d.sports_pairs, 0, "the soccer branch must not emit moneyline 2-ticker pairs");
        assert!(d.pairs.iter().all(|p| p.kalshi_b.is_none()), "WC pairs never carry a kalshi_b");
    }

    /// A pmus WC outcome market carrying team.name (the name-fallback bridge); `-draw` carries no team.
    fn wc_pm(slug: &str, team_name: Option<&str>) -> Value {
        let sides = match team_name {
            Some(n) => format!(r#"[{{"description":"Yes","team":{{"name":"{n}"}}}},{{"description":"No","team":{{"name":"{n}"}}}}]"#),
            None => r#"[{"description":"Yes","team":null},{"description":"No","team":null}]"#.to_string(),
        };
        pm(slug, "sports", &format!(r#""marketType":"drawable_outcome","marketSides":{sides}"#))
    }

    /// NAME-FALLBACK join (supersedes the removed alias table): pmus code `irn` is ABSENT from the Kalshi
    /// event (which uses `iri`), so the exact-code path fails; the name fallback ("IR Iran"=="IR Iran")
    /// binds it. NOT fuzzy — exact normalized-name equality. The three outcomes emit, with -irn<->IRI.
    #[test]
    fn soccer3_name_fallback_binds_irn_to_iri() {
        let pm_cat = vec![
            wc_pm("atc-fwc-irn-nzl-2026-06-15-irn", Some("IR Iran")),
            wc_pm("atc-fwc-irn-nzl-2026-06-15-nzl", Some("New Zealand")),
            wc_pm("atc-fwc-irn-nzl-2026-06-15-draw", None),
        ];
        let d = assemble(&pm_cat, false, ymd_to_epoch_days("2026-06-13"), wc_kalshi_stub);
        assert_eq!(d.soccer_pairs, 3, "name fallback irn->iri must bind all 3 outcomes");
        assert!(d.soccer_unbound.is_empty(), "a name-matched game is not unbound");
        let irn = d.pairs.iter().find(|p| p.slug.ends_with("-irn")).unwrap();
        assert_eq!(irn.kalshi, "KXWCGAME-26JUN15IRINZL-IRI", "irn binds the IRI ticker by NAME, not a hardcoded alias");
        let draw = d.pairs.iter().find(|p| p.slug.ends_with("2026-06-15-draw")).unwrap();
        assert_eq!(draw.kalshi, "KXWCGAME-26JUN15IRINZL-TIE");
    }

    /// `norm_country`: NFKD-equivalent accent fold + lowercase + strip non-alphanumeric. The live code-
    /// mismatch countries are name-IDENTICAL across venues; two DISTINCT countries (incl. the South/North
    /// Korea trap the task flagged) must NOT collide -> the L1 no-false-join invariant holds without fuzzy.
    #[test]
    fn norm_country_folds_accents_and_never_overcollapses() {
        assert_eq!(norm_country("IR Iran"), "iriran");
        assert_eq!(norm_country("Türkiye"), "turkiye", "ü folds to u (matches Kalshi 'Turkiye')");
        assert_eq!(norm_country("Côte d'Ivoire"), "cotedivoire");
        assert_eq!(norm_country("New Zealand"), "newzealand");
        assert_eq!(norm_country(""), "");
        // L1: distinct countries must never share a normalized key.
        assert_ne!(norm_country("South Korea"), norm_country("North Korea"));
        assert_ne!(norm_country("Korea Republic"), norm_country("Korea DPR"));
        assert_ne!(norm_country("Congo DR"), norm_country("Congo"));
    }

    /// NO FALSE JOIN + LOUD MISS + NO PARTIAL BIND (L1): a code whose name ALSO matches no Kalshi country
    /// emits NOTHING and is REPORTED in `soccer_unbound` (no fuzzy fallback); the South/North Korea trap
    /// must NOT bind; and a game missing a sibling outcome / the Kalshi TIE emits NOTHING (all-3-or-skip).
    #[test]
    fn soccer3_no_false_join_loud_miss_and_no_partial_bind() {
        // (1) unknown code `xxx` with a name ("Atlantis") in no Kalshi event -> code+name both fail -> UNBOUND.
        let bad = vec![
            wc_pm("atc-fwc-xxx-nzl-2026-06-15-xxx", Some("Atlantis")),
            wc_pm("atc-fwc-xxx-nzl-2026-06-15-nzl", Some("New Zealand")),
            wc_pm("atc-fwc-xxx-nzl-2026-06-15-draw", None),
        ];
        let d_bad = assemble(&bad, false, None, wc_kalshi_stub);
        assert_eq!(d_bad.soccer_pairs, 0, "no false join on an unknown code+name");
        assert_eq!(d_bad.soccer_unbound.len(), 1, "the code+name miss is REPORTED unbound, not silent");
        assert_eq!(d_bad.soccer_unbound[0].0, "atc-fwc-xxx-nzl-2026-06-15-xxx");
        // (2) L1 OVER-COLLAPSE: pmus North Korea must NOT bind a Kalshi event listing only South Korea (kor).
        fn kor_stub(series: &str) -> Vec<Value> {
            if series == "KXWCGAME" {
                vec![
                    serde_json::from_str(r#"{"ticker":"KXWCGAME-26JUN16KORNZL-KOR","event_ticker":"KXWCGAME-26JUN16KORNZL","yes_sub_title":"South Korea"}"#).unwrap(),
                    serde_json::from_str(r#"{"ticker":"KXWCGAME-26JUN16KORNZL-NZL","event_ticker":"KXWCGAME-26JUN16KORNZL","yes_sub_title":"New Zealand"}"#).unwrap(),
                    serde_json::from_str(r#"{"ticker":"KXWCGAME-26JUN16KORNZL-TIE","event_ticker":"KXWCGAME-26JUN16KORNZL","yes_sub_title":"Tie"}"#).unwrap(),
                ]
            } else {
                vec![]
            }
        }
        let nk = vec![
            wc_pm("atc-fwc-prk-nzl-2026-06-16-prk", Some("North Korea")),
            wc_pm("atc-fwc-prk-nzl-2026-06-16-nzl", Some("New Zealand")),
            wc_pm("atc-fwc-prk-nzl-2026-06-16-draw", None),
        ];
        let d_nk = assemble(&nk, false, None, kor_stub);
        assert_eq!(d_nk.soccer_pairs, 0, "L1: North Korea must NOT bind a South-Korea-only event");
        assert_eq!(d_nk.soccer_unbound.len(), 1, "the over-collapse miss is reported unbound");
        // (3) incomplete game: only 2 of 3 sibling outcomes (no draw) -> emit nothing AND not reported unbound.
        let partial = vec![
            wc_pm("atc-fwc-ger-cuw-2026-06-14-ger", Some("Germany")),
            wc_pm("atc-fwc-ger-cuw-2026-06-14-cuw", Some("Curacao")),
        ];
        let d_partial = assemble(&partial, false, None, wc_kalshi_stub);
        assert_eq!(d_partial.soccer_pairs, 0, "incomplete game (no draw) -> skip");
        assert!(d_partial.soccer_unbound.is_empty(), "an incomplete game is not reported unbound (need all 3 first)");
        // (4) Kalshi event has no TIE ticker -> no bind (need team A + B + TIE) -> reported unbound.
        fn no_tie_stub(series: &str) -> Vec<Value> {
            if series == "KXWCGAME" {
                vec![
                    serde_json::from_str(r#"{"ticker":"KXWCGAME-26JUN14GERCUW-GER","event_ticker":"KXWCGAME-26JUN14GERCUW","yes_sub_title":"Germany"}"#).unwrap(),
                    serde_json::from_str(r#"{"ticker":"KXWCGAME-26JUN14GERCUW-CUW","event_ticker":"KXWCGAME-26JUN14GERCUW","yes_sub_title":"Curacao"}"#).unwrap(),
                ]
            } else {
                vec![]
            }
        }
        let full = vec![
            wc_pm("atc-fwc-ger-cuw-2026-06-14-ger", Some("Germany")),
            wc_pm("atc-fwc-ger-cuw-2026-06-14-cuw", Some("Curacao")),
            wc_pm("atc-fwc-ger-cuw-2026-06-14-draw", None),
        ];
        assert_eq!(assemble(&full, false, None, no_tie_stub).soccer_pairs, 0, "no Kalshi TIE -> no bind");
    }

    /// COVERAGE: a pmus drawable-outcome slug for an UNMAPPED soccer league (not `fwc`) is reported in
    /// `soccer_leagues_unmapped` (L7) — never silently missed. `fwc` is mapped, so it is NOT reported.
    #[test]
    fn soccer3_unmapped_league_is_reported() {
        // `fifa` is a drawable-outcome soccer league NOT in SOCCER3 (futures/offseason, deferred).
        let pm_cat = vec![
            pm("atc-fwc-ger-cuw-2026-06-14-ger", "sports", r#""marketType":"drawable_outcome""#),
            pm("atc-fifa-x-y-2026-07-01-x", "sports", r#""marketType":"drawable_outcome""#),
        ];
        let d = assemble(&pm_cat, false, None, wc_kalshi_stub);
        assert_eq!(d.soccer_leagues_unmapped, vec!["fifa".to_string()], "an unmapped drawable-outcome league is reported");
    }
}
