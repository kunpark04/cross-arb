//! MLB postponement DETECTION — the live trigger that arms the `unwind` rule (stage 2).
//!
//! Faithful port of `scripts/probe_mlb_postpone.py` (`snap` + `unwind_trigger` + its `_selftest`). A
//! held cross-arb sports pair is riskless only while its legs offset; a postponement breaks that (Kalshi
//! voids to a fair price past its ~2-day window while pmus settles the real replay — `crate::unwind`). So
//! we POLL MLB statsapi, detect a postpone/suspend/cancel (or an officialDate slide that moved the game
//! out of Kalshi's window), and emit a `Postponement` the caller feeds to `unwind::should_unwind`.
//!
//! Split, like the rest of the crate, into a PURE detector (`snap`, `detect_postponement`, `days_between`
//! — unit-tested against the Python `_selftest` vectors, the PARITY GATE) and an I/O poll
//! (`poll_mlb_postponements` — the owner's-droplet path; NEVER exercised by a test, no live HTTP).
//!
//! ## The L3 trap (the load-bearing invariant)
//! On a postponement statsapi MOVES `officialDate` to the makeup date while `gameDate` keeps the original
//! datetime. So the reschedule gap MUST be measured from the pair's BOUND event date (the pmus slug date
//! == the Kalshi ticker date == the original game date), NEVER from `officialDate` — measuring from
//! `officialDate` computes makeup−makeup = 0 days and misses every unwind.

use crate::types::Position;
use crate::unwind::Postponement;
use serde_json::Value;
use std::collections::HashMap;
use std::sync::{Arc, Mutex};

/// statsapi `detailedState` values that indicate the settlement-divergence tail is live. Port of
/// `POSTPONE_STATES` — substring-matched against `detailedState` (e.g. "Cancelled: Rain" contains
/// "Cancelled").
const POSTPONE_STATES: [&str; 3] = ["Postponed", "Suspended", "Cancelled"];

/// The compact status snapshot the detector compares (port of `snap`'s return). Only the fields the
/// trigger reads; `reschedule_date`/`resume_date` fold the `*GameDate` aliases like the Python.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct GameStatus {
    pub detailed_state: String,
    pub official_date: Option<String>,
    pub game_date: Option<String>,
    pub reschedule_date: Option<String>,
    pub resume_date: Option<String>,
}

/// One statsapi schedule game entry -> the compact status snapshot. Port of `snap`:
/// `rescheduleDate = rescheduleDate || rescheduleGameDate`; `resumeDate = resumeDate || resumeGameDate`.
pub fn snap(game: &Value) -> GameStatus {
    let s = |k: &str| game.get(k).and_then(Value::as_str).map(str::to_string);
    let status_str =
        |k: &str| game.get("status").and_then(|st| st.get(k)).and_then(Value::as_str).map(str::to_string);
    GameStatus {
        detailed_state: status_str("detailedState").unwrap_or_default(),
        official_date: s("officialDate"),
        game_date: s("gameDate"),
        reschedule_date: s("rescheduleDate").or_else(|| s("rescheduleGameDate")),
        resume_date: s("resumeDate").or_else(|| s("resumeGameDate")),
    }
}

/// Whole-day difference `d1 - d0` from ISO `YYYY-MM-DD` (the leading 10 chars of a datetime are taken, so
/// `2026-06-15T17:10:00Z` works). `None` if either side is unparseable. Port of `_days`; dep-free civil-day
/// algebra (no chrono — the same `days_from_civil` approach `discovery` uses for `days_to_event`).
pub fn days_between(d0: &str, d1: &str) -> Option<i64> {
    Some(ymd_to_epoch_days(&d1[..d1.len().min(10)])? - ymd_to_epoch_days(&d0[..d0.len().min(10)])?)
}

/// `YYYY-MM-DD` -> days since the Unix epoch via Howard Hinnant's `days_from_civil` (dep-free, proleptic
/// Gregorian). Mirrors `discovery::ymd_to_epoch_days` (kept local so `postpone` has no cross-module dep on
/// a private fn).
fn ymd_to_epoch_days(ymd: &str) -> Option<i64> {
    let p: Vec<&str> = ymd.split('-').collect();
    if p.len() != 3 {
        return None;
    }
    let (y, m, d): (i64, i64, i64) = (p[0].parse().ok()?, p[1].parse().ok()?, p[2].parse().ok()?);
    if !(1..=12).contains(&m) || !(1..=31).contains(&d) {
        return None;
    }
    let y = y - i64::from(m <= 2);
    let era = if y >= 0 { y } else { y - 399 } / 400;
    let yoe = y - era * 400;
    let doy = (153 * (if m > 2 { m - 3 } else { m + 9 }) + 2) / 5 + d - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    Some(era * 146097 + doe - 719468)
}

/// Does `detailed_state` carry any POSTPONE_STATE substring? (port of the `any(s in cs ...)` test).
fn is_postpone(detailed_state: &str) -> bool {
    POSTPONE_STATES.iter().any(|s| detailed_state.contains(s))
}

/// Detect a postponement for ONE tracked game and build the `Postponement` the unwind rule consumes.
/// FAITHFUL port of `unwind_trigger`'s decision tree, SPLIT so the UNWIND/WATCH cutoff is delegated to
/// `unwind::should_unwind` (the caller composes `detect_postponement` then `should_unwind`):
///   - returns `Some(Postponement)` whenever a postpone/suspend/cancel state is present OR `officialDate`
///     moved without a status flip (the two cases that need an unwind/hold decision);
///   - returns `None` on the normal life-cycle (no postpone state, no officialDate move) — the caller
///     never calls `should_unwind`, so the pair is held.
///
/// `reschedule_in_days` carries the UNWIND-vs-WATCH distinction (`should_unwind` = unwind unless it's
/// `Some(d <= window)`):
///   - `None` on Cancelled (no makeup to confirm), no makeup/resume target, OR an unparseable gap
///     -> `should_unwind` => UNWIND;
///   - `Some(days_between(event_date, target))` otherwise (target = resume_date when Suspended, else
///     reschedule_date) -> `should_unwind` => UNWIND if `> window`, WATCH if `<= window`.
///
/// L3 (CRITICAL): the gap is measured from `event_date` (the BOUND pm slug date), never `officialDate`
/// (which moves to the makeup date -> makeup−makeup = 0d, missing every unwind). `event_date` empty/absent
/// falls back to `prev.game_date` then `cur.game_date` (the original datetime), mirroring the Python
/// `orig` chain. `prev = None` is first-sight (a game already postponed the first time we poll it).
pub fn detect_postponement(
    prev: Option<&GameStatus>,
    cur: &GameStatus,
    event_date: &str,
    market: &str,
) -> Option<Postponement> {
    let cs = cur.detailed_state.as_str();

    // `orig` = the bound event date, else the prior/current snapshot's original gameDate (the Python
    // `event_date or prev.gameDate[:10] or cur.gameDate[:10] or None`). Empty event_date is treated as
    // absent (the Python `or` chain skips a falsy "").
    let first10 = |s: Option<&String>| s.map(|d| d[..d.len().min(10)].to_string());
    let orig: Option<String> = if !event_date.is_empty() {
        Some(event_date.to_string())
    } else {
        first10(prev.and_then(|p| p.game_date.as_ref())).or_else(|| first10(cur.game_date.as_ref()))
    };

    if !is_postpone(cs) {
        // officialDate moved WITHOUT a status flip (a statsapi quirk that still slides the game): treat it
        // exactly like the Python — gap from `orig or prev.officialDate` to `cur.officialDate`. Requires
        // BOTH dates present (the `prev.and_then` is None when prev is None OR prev has no officialDate).
        if let (Some(prev_off), Some(cur_off)) =
            (prev.and_then(|p| p.official_date.as_ref()), cur.official_date.as_ref())
        {
            if prev_off != cur_off {
                let from = orig.clone().unwrap_or_else(|| prev_off.clone());
                let d = days_between(&from, cur_off);
                return Some(Postponement { market: market.to_string(), reschedule_in_days: d.map(|x| x as f64) });
            }
        }
        return None; // normal life-cycle -> no Postponement -> caller holds
    }

    // Cancelled: divergent void bases (Kalshi fair-price void vs pmus last-traded). No makeup to confirm
    // -> unconditional unwind (reschedule_in_days = None).
    if cs.contains("Cancelled") {
        return Some(Postponement { market: market.to_string(), reschedule_in_days: None });
    }

    // Suspended resumes; Postponed reschedules. No target yet -> unknown -> unwind (None).
    let target = if cs.contains("Suspended") { cur.resume_date.as_ref() } else { cur.reschedule_date.as_ref() };
    let Some(target) = target else {
        return Some(Postponement { market: market.to_string(), reschedule_in_days: None });
    };

    // L3: gap from the BOUND event date (orig), never officialDate. Unparseable orig/target -> None (unwind).
    let days = orig.as_deref().and_then(|o| days_between(o, target));
    Some(Postponement { market: market.to_string(), reschedule_in_days: days.map(|x| x as f64) })
}

// =====================================================================================================
// LIVE POLL  (the owner's-droplet path — statsapi is public/keyless; NOT on any test path, no live HTTP)
// =====================================================================================================

const STATS_API: &str = "https://statsapi.mlb.com/api/v1";

/// A held cross-arb position the poll tracks for postponements. `team_a`/`team_b` are the lowercase Kalshi
/// abbrevs (from the two Kalshi tickers) used to match the statsapi game; `prev` is last poll's snapshot
/// (None on first sight -> the Python first-sight `prev=None`). Non-MLB sports leave the match fields empty
/// (there is no statsapi source for them — logged once, never auto-unwound here).
#[derive(Clone, Debug)]
pub struct HeldPosition {
    pub pos: Position,
    pub league: String,
    pub date: String,
    pub team_a: String,
    pub team_b: String,
    pub prev: Option<GameStatus>,
}

/// A request to flatten a held position (sent by the poll, consumed by the main loop's unwind handler).
#[derive(Clone, Debug, PartialEq)]
pub struct UnwindRequest {
    pub slug: String,
}

/// Live MLB-postponement poll (stage-2, owner droplet). Caches `teams` (id->lowercase abbrev) once, then
/// every `poll_s`: snapshot the held MLB positions, group by event date, pull `schedule?date=<date>` per
/// date, match each game to a held position by its `{away,home}` abbrev pair, `snap` ->
/// `detect_postponement` -> `should_unwind`; on UNWIND send an `UnwindRequest`. Supervised: a bad pull
/// logs and continues (never kills the task). MLB-only — non-MLB held sports log once that there is no
/// auto-unwind source. NEVER called from a test (no live HTTP on a test path).
pub async fn poll_mlb_postponements(
    http: reqwest::Client,
    positions: Arc<Mutex<HashMap<String, HeldPosition>>>,
    unwind_tx: tokio::sync::mpsc::UnboundedSender<UnwindRequest>,
    poll_s: u64,
    kalshi_void_window_days: f64,
) {
    // teams map (id -> lowercase abbrev), cached once; retried each loop until it loads.
    let mut teams: HashMap<i64, String> = HashMap::new();
    let mut warned_non_mlb: std::collections::HashSet<String> = std::collections::HashSet::new();
    loop {
        tokio::time::sleep(std::time::Duration::from_secs(poll_s.max(1))).await;

        if teams.is_empty() {
            match get_json(&http, &format!("{STATS_API}/teams?sportId=1")).await {
                Ok(v) => teams = parse_teams(&v),
                Err(e) => {
                    println!("[postpone] teams pull failed ({e}); retrying next cycle");
                    continue;
                }
            }
        }

        // snapshot the held MLB sports positions (clone out; don't hold the lock across awaits). Non-MLB
        // held sports get a one-time "no auto-unwind source" log.
        let held: Vec<HeldPosition> = {
            let map = positions.lock().unwrap();
            let mut v = Vec::new();
            for hp in map.values() {
                // an EMPTY league = a position enrolled WITHOUT poll metadata (weather/econ never reach here
                // with cat==Sports; a WORLD-CUP `Cat::Sports` pair does, on purpose — it has no statsapi
                // source). Skip the "no source" warning for it: there is no league name to report.
                if hp.pos.cat == crate::types::Cat::Sports && !hp.league.is_empty() && hp.league != "mlb" && warned_non_mlb.insert(hp.league.clone()) {
                    println!("[postpone] no auto-unwind source for league {} (statsapi is MLB-only)", hp.league);
                }
                if hp.league == "mlb" && !hp.team_a.is_empty() && !hp.team_b.is_empty() {
                    v.push(hp.clone());
                }
            }
            v
        };
        if held.is_empty() {
            continue;
        }

        // one schedule pull per distinct event date covers all held games on that date.
        let mut dates: Vec<String> = held.iter().map(|h| h.date.clone()).collect();
        dates.sort();
        dates.dedup();
        let mut sched_by_date: HashMap<String, Value> = HashMap::new();
        for d in &dates {
            match get_json(&http, &format!("{STATS_API}/schedule?sportId=1&date={d}")).await {
                Ok(v) => {
                    sched_by_date.insert(d.clone(), v);
                }
                Err(e) => println!("[postpone] schedule pull {d} failed ({e}); skipping this date this cycle"),
            }
        }

        for h in &held {
            let Some(sched) = sched_by_date.get(&h.date) else { continue };
            let Some(game) = find_game(sched, &teams, &h.team_a, &h.team_b) else { continue };
            let cur = snap(game);
            let prev = {
                let map = positions.lock().unwrap();
                map.get(&h.pos.market).and_then(|hp| hp.prev.clone())
            };
            if let Some(p) = detect_postponement(prev.as_ref(), &cur, &h.date, &h.pos.market) {
                if crate::unwind::should_unwind(&p, kalshi_void_window_days) {
                    println!("[postpone] UNWIND {} (reschedule_in_days={:?})", h.pos.market, p.reschedule_in_days);
                    let _ = unwind_tx.send(UnwindRequest { slug: h.pos.market.clone() });
                }
            }
            // store cur as prev for the next cycle (only if the position is still held).
            if let Some(hp) = positions.lock().unwrap().get_mut(&h.pos.market) {
                hp.prev = Some(cur);
            }
        }
    }
}

/// statsapi `teams` payload -> `{id: lowercase abbreviation}`.
fn parse_teams(v: &Value) -> HashMap<i64, String> {
    let mut m = HashMap::new();
    if let Some(arr) = v.get("teams").and_then(Value::as_array) {
        for t in arr {
            if let (Some(id), Some(ab)) = (
                t.get("id").and_then(Value::as_i64),
                t.get("abbreviation").and_then(Value::as_str),
            ) {
                m.insert(id, ab.to_ascii_lowercase());
            }
        }
    }
    m
}

/// Find the schedule game whose `{away_abbrev, home_abbrev}` == `{team_a, team_b}` of a held position
/// (orientation-free — the pm slug's A/B order need not match statsapi's away/home). Returns the raw game
/// `Value` for `snap`. `teams` resolves a team id -> lowercase abbrev.
fn find_game<'a>(sched: &'a Value, teams: &HashMap<i64, String>, team_a: &str, team_b: &str) -> Option<&'a Value> {
    let want: std::collections::HashSet<&str> = [team_a, team_b].into_iter().collect();
    let games = sched.get("dates").and_then(Value::as_array)?.iter().flat_map(|d| {
        d.get("games").and_then(Value::as_array).map(|a| a.as_slice()).unwrap_or(&[])
    });
    for g in games {
        let abbr = |side: &str| {
            g.get("teams")
                .and_then(|t| t.get(side))
                .and_then(|s| s.get("team"))
                .and_then(|t| t.get("id"))
                .and_then(Value::as_i64)
                .and_then(|id| teams.get(&id))
                .map(String::as_str)
        };
        if let (Some(away), Some(home)) = (abbr("away"), abbr("home")) {
            let got: std::collections::HashSet<&str> = [away, home].into_iter().collect();
            if got == want {
                return Some(g);
            }
        }
    }
    None
}

/// GET -> parsed JSON (text-first so a non-JSON error page is a clean error, not a panic). PUBLIC,
/// keyless statsapi endpoint — NO auth headers. Mirrors `discovery::fetch_json`.
async fn get_json(http: &reqwest::Client, url: &str) -> Result<Value, String> {
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
    use crate::unwind::should_unwind;

    const WINDOW: f64 = 2.0;

    /// Compose detect_postponement -> should_unwind into the Python `unwind_trigger`'s 3-valued action so
    /// the parity tests read like the Python asserts. "UNWIND" / "WATCH" / "NONE".
    fn action(prev: Option<&GameStatus>, cur: &GameStatus, event_date: &str) -> &'static str {
        match detect_postponement(prev, cur, event_date, "m") {
            Some(p) => {
                if should_unwind(&p, WINDOW) {
                    "UNWIND"
                } else {
                    "WATCH"
                }
            }
            None => "NONE",
        }
    }

    /// The base "Scheduled" snapshot the Python `_selftest` builds on (gameDate keeps the original
    /// datetime; officialDate starts equal to the date).
    fn base() -> GameStatus {
        GameStatus {
            detailed_state: "Scheduled".into(),
            official_date: Some("2026-06-10".into()),
            game_date: Some("2026-06-10T22:40:00Z".into()),
            reschedule_date: None,
            resume_date: None,
        }
    }
    fn with(state: &str, official: Option<&str>, reschedule: Option<&str>, resume: Option<&str>) -> GameStatus {
        GameStatus {
            detailed_state: state.into(),
            official_date: official.map(str::to_string),
            game_date: Some("2026-06-10T22:40:00Z".into()),
            reschedule_date: reschedule.map(str::to_string),
            resume_date: resume.map(str::to_string),
        }
    }

    // ===== PARITY GATE: every Python `_selftest` vector, same expected action =====================

    /// Postponed, makeup 5 days out (the 2d–2wk both-legs-loss gap) -> UNWIND; the gap is measured from
    /// the BOUND event date (no event_date arg here -> falls to gameDate = 2026-06-10), and the makeup is
    /// 2026-06-15 -> 5 days -> > window -> UNWIND. ALSO asserts reschedule_in_days = 5 explicitly (L3).
    #[test]
    fn postponed_makeup_5d_out_unwinds() {
        let cur = with("Postponed", Some("2026-06-15"), Some("2026-06-15T17:10:00Z"), None);
        let p = detect_postponement(Some(&base()), &cur, "", "m").unwrap();
        assert_eq!(p.reschedule_in_days, Some(5.0), "gap from event_date 06-10, NOT officialDate 06-15");
        assert_eq!(action(Some(&base()), &cur, ""), "UNWIND");
    }

    /// The LIVE TB@NYY shape: makeup MONTHS out, officialDate ALREADY MOVED to the makeup date. An
    /// officialDate-based gap would compute makeup−makeup = 0d and WATCH (the L3 trap) — measuring from the
    /// bound event date gives 104 days -> UNWIND. This is the single most important regression.
    #[test]
    fn live_officialdate_already_moved_104d_unwinds() {
        let cur = with("Postponed", Some("2026-09-22"), Some("2026-09-22T17:05:00Z"), None);
        let p = detect_postponement(Some(&base()), &cur, "", "m").unwrap();
        assert_eq!(p.reschedule_in_days, Some(104.0), "L3: 06-10 -> 09-22 = 104d, NOT officialDate-based 0d");
        assert_eq!(action(Some(&base()), &cur, ""), "UNWIND");
    }

    /// Postponed, makeup NEXT DAY (classic split-doubleheader makeup) -> inside the window -> WATCH.
    #[test]
    fn postponed_makeup_next_day_watches() {
        let cur = with("Postponed", Some("2026-06-11"), Some("2026-06-11T17:10:00Z"), None);
        assert_eq!(action(Some(&base()), &cur, ""), "WATCH");
    }

    /// Postponed, NO makeup date yet -> unknown -> conservative UNWIND (reschedule_in_days = None).
    #[test]
    fn postponed_no_makeup_date_unwinds() {
        let cur = with("Postponed", None, None, None);
        let p = detect_postponement(Some(&base()), &cur, "", "m").unwrap();
        assert_eq!(p.reschedule_in_days, None);
        assert_eq!(action(Some(&base()), &cur, ""), "UNWIND");
    }

    /// Suspended, resumes next day -> both venues settle the completed game -> WATCH.
    #[test]
    fn suspended_resumes_next_day_watches() {
        let cur = with("Suspended", None, None, Some("2026-06-11T17:00:00Z"));
        assert_eq!(action(Some(&base()), &cur, ""), "WATCH");
    }

    /// Cancelled outright -> divergent void bases -> UNWIND (no target needed; reschedule_in_days = None).
    #[test]
    fn cancelled_unwinds() {
        let cur = with("Cancelled: Rain", None, None, None);
        let p = detect_postponement(Some(&base()), &cur, "", "m").unwrap();
        assert_eq!(p.reschedule_in_days, None);
        assert_eq!(action(Some(&base()), &cur, ""), "UNWIND");
    }

    /// The normal life-cycle states -> no Postponement at all (None) -> the caller holds.
    #[test]
    fn normal_lifecycle_is_none() {
        for st in ["Scheduled", "Pre-Game", "Warmup", "In Progress", "Final"] {
            let cur = with(st, Some("2026-06-10"), None, None);
            assert_eq!(action(Some(&base()), &cur, ""), "NONE", "state {st}");
            assert!(detect_postponement(Some(&base()), &cur, "", "m").is_none(), "state {st}");
        }
    }

    /// First sighting ALREADY postponed (prev = None): the event_date comes from the BOUND pair (pm slug
    /// date) -> UNWIND.
    #[test]
    fn first_sight_postponed_with_event_date_unwinds() {
        let cur = with("Postponed", Some("2026-06-20"), Some("2026-06-20T17:00:00Z"), None);
        let p = detect_postponement(None, &cur, "2026-06-10", "m").unwrap();
        assert_eq!(p.reschedule_in_days, Some(10.0)); // 06-10 -> 06-20
        assert_eq!(action(None, &cur, "2026-06-10"), "UNWIND");
    }

    /// ... and WITHOUT event_date the postponed entry's own gameDate (the original) still catches it.
    #[test]
    fn first_sight_postponed_without_event_date_unwinds() {
        let cur = with("Postponed", Some("2026-06-20"), Some("2026-06-20T17:00:00Z"), None);
        assert_eq!(action(None, &cur, ""), "UNWIND"); // gameDate 06-10 -> makeup 06-20 = 10d
    }

    /// officialDate slid ONE day without a status flip (statsapi quirk) -> inside window -> WATCH.
    #[test]
    fn officialdate_slid_one_day_watches() {
        let cur = with("Scheduled", Some("2026-06-11"), None, None);
        assert_eq!(action(Some(&base()), &cur, ""), "WATCH");
    }

    /// officialDate slid a WEEK without a status flip -> outside window -> UNWIND.
    #[test]
    fn officialdate_slid_a_week_unwinds() {
        let cur = with("Scheduled", Some("2026-06-17"), None, None);
        assert_eq!(action(Some(&base()), &cur, ""), "UNWIND");
    }

    // ===== days_between + snap + find_game ========================================================

    /// `days_between` on ISO dates (incl. a datetime suffix that gets trimmed to 10 chars) + a known anchor.
    #[test]
    fn days_between_whole_days() {
        assert_eq!(days_between("2026-06-10", "2026-06-15"), Some(5));
        assert_eq!(days_between("2026-06-10T22:40:00Z", "2026-09-22T17:05:00Z"), Some(104));
        assert_eq!(days_between("2026-06-15", "2026-06-10"), Some(-5)); // signed
        assert_eq!(days_between("bad", "2026-06-10"), None);
        assert_eq!(days_between("2026-06-10", "also-bad"), None);
    }

    /// `snap` folds the `*GameDate` aliases (rescheduleGameDate -> reschedule_date) and reads the nested
    /// status.detailedState, exactly like the Python `snap`.
    #[test]
    fn snap_reads_status_and_folds_aliases() {
        let g: Value = serde_json::from_str(
            r#"{"gamePk":1,"officialDate":"2026-06-15","gameDate":"2026-06-10T22:40:00Z",
                "status":{"detailedState":"Postponed","reason":"Rain"},
                "rescheduleGameDate":"2026-06-15T17:10:00Z"}"#,
        )
        .unwrap();
        let s = snap(&g);
        assert_eq!(s.detailed_state, "Postponed");
        assert_eq!(s.reschedule_date.as_deref(), Some("2026-06-15T17:10:00Z")); // GameDate alias folded
        assert_eq!(s.official_date.as_deref(), Some("2026-06-15"));
        assert_eq!(s.game_date.as_deref(), Some("2026-06-10T22:40:00Z"));
        // a plain rescheduleDate wins when present (the primary, not the alias).
        let g2: Value = serde_json::from_str(
            r#"{"status":{"detailedState":"Suspended"},"resumeDate":"2026-06-11T17:00:00Z"}"#,
        )
        .unwrap();
        let s2 = snap(&g2);
        assert_eq!(s2.resume_date.as_deref(), Some("2026-06-11T17:00:00Z"));
        assert_eq!(s2.reschedule_date, None);
    }

    /// `find_game` matches a held position's abbrev pair against an embedded `schedule?date=` sample,
    /// orientation-free (the held pair lists LAD/PIT; the schedule has away=PIT, home=LAD -> still matches),
    /// and resolves abbrevs via the teams id map. A non-matching abbrev pair returns None.
    #[test]
    fn find_game_matches_abbrev_pair_orientation_free() {
        // teams id->abbrev: 119 = LAD, 134 = PIT, 147 = NYY.
        let teams: HashMap<i64, String> =
            [(119, "lad"), (134, "pit"), (147, "nyy")].into_iter().map(|(i, a)| (i, a.to_string())).collect();
        let sched: Value = serde_json::from_str(
            r#"{"dates":[{"games":[
                {"gamePk":1,"officialDate":"2026-06-16","gameDate":"2026-06-16T20:00:00Z",
                 "status":{"detailedState":"Postponed"},"rescheduleDate":"2026-06-21T17:10:00Z",
                 "teams":{"away":{"team":{"id":134}},"home":{"team":{"id":119}}}},
                {"gamePk":2,"status":{"detailedState":"Scheduled"},
                 "teams":{"away":{"team":{"id":147}},"home":{"team":{"id":119}}}}
            ]}]}"#,
        )
        .unwrap();
        // held LAD/PIT -> matches game 1 even though schedule lists away=PIT, home=LAD.
        let g = find_game(&sched, &teams, "lad", "pit").unwrap();
        assert_eq!(snap(g).detailed_state, "Postponed");
        // the composed detector on that game (event_date = the bound slug date 06-16) -> makeup 06-21 = 5d
        // -> UNWIND.
        let p = detect_postponement(None, &snap(g), "2026-06-16", "aec-mlb-lad-pit-2026-06-16").unwrap();
        assert_eq!(p.reschedule_in_days, Some(5.0));
        assert!(should_unwind(&p, WINDOW));
        // a pair not on the schedule (PIT/NYY) -> no match.
        assert!(find_game(&sched, &teams, "pit", "nyy").is_none());
    }

    /// `parse_teams` builds the id->lowercase-abbrev map and skips entries missing id/abbreviation.
    #[test]
    fn parse_teams_lowercases_and_skips_incomplete() {
        let v: Value = serde_json::from_str(
            r#"{"teams":[{"id":119,"abbreviation":"LAD"},{"id":134,"abbreviation":"PIT"},{"abbreviation":"XXX"},{"id":99}]}"#,
        )
        .unwrap();
        let m = parse_teams(&v);
        assert_eq!(m.get(&119).map(String::as_str), Some("lad"));
        assert_eq!(m.get(&134).map(String::as_str), Some("pit"));
        assert_eq!(m.len(), 2); // the id-less and abbrev-less entries are skipped
    }
}
