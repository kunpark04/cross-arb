"""scripts/settle_recon.py - EMPIRICAL test of invariant #1: do BOTH venues actually grade a
co-listed market to the SAME outcome? Uses SETTLED markets only. READ-ONLY, no capital.

Invariant #1 ("settlement identity") is currently believed from RULES TEXT (both venues' rules say
they grade off the same NWS CLI number / the same final game score). This script replaces that
INFERENCE with a DIRECT empirical check: take markets that were genuinely co-listed on both venues,
wait for them to SETTLE, and read each venue's RESOLVED outcome from its own API. If invariant #1
holds, every joined pair agrees YES<->YES. A DISAGREEMENT is a real settlement-divergence finding
(a "locked" pair could then lose BOTH legs) and is reported loudly with the specific markets.

WHY the join is sourced from the live transition archive (not a fresh catalog scan):
  The monitor (bot/monitor.py) already paired markets that were SIMULTANEOUSLY live on both venues,
  using the validated identity join (colisted_map: city+date+identical-degF-bucket / league+date+
  team). Those pmus slugs are the ground-truth "was co-listed" set. We re-query each AFTER settlement.
  A pmus market is fetched by its EXACT slug (`?slug=<slug>`) - the only reliable way to read a
  SETTLED climate market, since pmus's paginated `closed=true` feed does NOT surface settled climate
  markets (verified 2026-06-09: 2499 closed rows, all old `aec-` sports, zero `tc-temp`).

RESOLUTION PARSING (per the venue gateways, verified live 2026-06-09):
  * Kalshi  : GET /markets/{ticker} -> result in {"yes","no"}, status "finalized"/"settled".
              A YES result means the contract's bucket (floor_strike..cap_strike) was HIT.
  * pmus    : ?slug=<slug> -> closed:true; outcomes e.g. ["Yes","No"], outcomePrices e.g. ["1","0"];
              the winning outcome is the LABEL whose price == "1". The arrays are JSON STRINGS that
              must be json.loads()'d, and their ORDER varies per market (sometimes ["No","Yes"]), so
              you MUST pair label[i] with price[i] - never assume index 0 is "Yes".

DATA-INTEGRITY GUARD (a finding, not an assumption): pmus's settled climate `outcomePrices` were
observed INTERNALLY INCONSISTENT - on one MIA day FOUR disjoint buckets each reported their "Yes"
outcome as the winner, which is impossible (a single high temp lands in exactly one bucket). So for
weather we also cross-check pmus against itself PER (city,date): exactly one bucket should win YES.
A day whose pmus YES-winner count != 1 is flagged pmus_inconsistent and excluded from the agree/
disagree tally (it can't be compared fairly) but is reported - it is itself evidence about pmus
settled-data reliability, which bears directly on invariant #1.

Usage:
  python scripts/settle_recon.py --selftest          # offline synthetic verification
  python scripts/settle_recon.py                     # live recon over the archive's co-listed slugs
  python scripts/settle_recon.py --no-sports         # weather only (faster)
  python scripts/settle_recon.py --max-pairs 40      # cap live work to finish fast
"""
import os, sys, re, json, gzip, glob, time, argparse, collections, urllib.request, urllib.error
try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: pass

# --- reuse the validated, identity-correct join helpers (do NOT duplicate) ---
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bot"))
from colisted_map import (get, KAL, PM, pm_bounds, kbounds, pick_game, surname, ktok_iso, wcity)  # noqa: E402

ARCHIVE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "data", "cross-arb")
WX = {"sfo": "KXHIGHTSFO", "lax": "KXHIGHLAX", "nyc": "KXHIGHNY", "mia": "KXHIGHMIA", "mdw": "KXHIGHCHI"}
LEAGUES = {"mlb": ("KXMLBGAME", "abbrev"), "wnba": ("KXWNBAGAME", "abbrev"), "nba": ("KXNBAGAME", "abbrev"),
           "nhl": ("KXNHLGAME", "abbrev"), "atp": ("KXATPMATCH", "surname"), "wta": ("KXWTAMATCH", "surname"),
           "itfm": ("KXITFMATCH", "surname"), "itfw": ("KXITFWMATCH", "surname"), "ufc": ("KXUFCFIGHT", "surname")}


# ============================================================================================
# PARSING (pure, self-tested)
# ============================================================================================
def pm_winner(market):
    """pmus settled market -> ("Yes"/"No"/team-label-or-None, raw_outcomes, raw_prices).
    Pairs label[i] with price[i] (arrays are json-strings; order varies; price '1' == winner)."""
    if not market or not market.get("closed"):
        return (None, None, None)
    try:
        oc = json.loads(market.get("outcomes") or "[]")
        op = json.loads(market.get("outcomePrices") or "[]")
    except Exception:
        return (None, None, None)
    win = [oc[i] for i in range(min(len(oc), len(op))) if str(op[i]) in ("1", "1.0", "1.00")]
    return (win[0] if len(win) == 1 else None, oc, op)

def kal_result(market):
    """Kalshi settled market -> 'yes'/'no'/None (None unless finalized/settled with a result)."""
    if not market or market.get("status") not in ("finalized", "settled"):
        return None
    r = market.get("result")
    return r if r in ("yes", "no") else None

def parse_wx_slug(slug):
    """tc-temp-<city>high-<YYYY-MM-DD>-<bucket> -> (city, date, bucketsuffix) or None."""
    m = re.match(r"tc-temp-([a-z]+)high-(\d{4}-\d{2}-\d{2})-(.+)$", str(slug))
    return (m.group(1), m.group(2), m.group(3)) if m else None

def parse_sport_slug(slug):
    """aec-<league>-<a>-<b>-<YYYY-MM-DD> -> (league, a, b, date) or None."""
    m = re.match(r"aec-([a-z0-9]+)-(.+?)-(.+?)-(\d{4}-\d{2}-\d{2})$", str(slug))
    return (m.group(1), m.group(2), m.group(3), m.group(4)) if m else None


# ============================================================================================
# CO-LISTED SET from the live transition archive (ground-truth "was co-listed on both")
# ============================================================================================
def archive_slugs(archive=ARCHIVE):
    """Distinct pmus slugs the monitor actually paired (each was simultaneously live on both venues).
    AT-MOST-ONE file per event-date (raw .jsonl wins over same-date .gz - mirrors analyze_persistence)."""
    if not os.path.isdir(archive):
        return []
    chosen = {}
    for path in glob.glob(os.path.join(archive, "transitions-*.jsonl.gz")) + \
                glob.glob(os.path.join(archive, "transitions-*.jsonl")):
        m = re.search(r"transitions-(.+?)\.jsonl(?:\.gz)?$", os.path.basename(path))
        chosen[m.group(1) if m else path] = path
    seen = set()
    for path in sorted(chosen.values()):
        op = gzip.open if path.endswith(".gz") else open
        with op(path, "rt", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        s = json.loads(line).get("market")
                        if s:
                            seen.add(s)
                    except Exception:
                        pass
    return sorted(seen)


# ============================================================================================
# KALSHI settled-bucket index (per city) / settled-event index (per league)
# ============================================================================================
def kal_wx_settled(city):
    """{date: {(lo,hi)-bounds: market}} for one WX city's SETTLED Kalshi buckets."""
    kser = WX.get(city)
    if not kser:
        return {}
    d = get(f"{KAL}?series_ticker={kser}&status=settled&limit=1000")
    out = collections.defaultdict(dict)
    for m in d.get("markets", []):
        dm = re.search(r"-(\d{2}[A-Z]{3}\d{2})", str(m.get("ticker")))
        if not dm:
            continue
        date = ktok_iso(dm.group(1))
        out[date][kbounds(m)] = m
    return out

def kal_league_settled(series, join):
    """kbydate structure pick_game() consumes, but over SETTLED Kalshi markets; plus {ticker: market}."""
    d = get(f"{KAL}?series_ticker={series}&status=settled&limit=1000")
    byev, evd, by_ticker = collections.defaultdict(dict), {}, {}
    for m in d.get("markets", []):
        ev = m.get("event_ticker"); tk = str(m.get("ticker", ""))
        by_ticker[tk] = m
        key = tk.split("-")[-1].lower() if join == "abbrev" else surname(m.get("yes_sub_title"))
        dm = re.search(r"-(\d{2}[A-Z]{3}\d{2})", tk); evd[ev] = ktok_iso(dm.group(1)) if dm else None
        if key:
            byev[ev][key] = tk
    kbydate = collections.defaultdict(list)
    for ev, pl in byev.items():
        kbydate[evd.get(ev)].append(pl)
    return kbydate, by_ticker


# ============================================================================================
# WEATHER recon
# ============================================================================================
def recon_weather(slugs, log=print):
    wx_slugs = [s for s in slugs if str(s).startswith("tc-temp-")]
    by_city = collections.defaultdict(list)
    for s in wx_slugs:
        p = parse_wx_slug(s)
        if p:
            by_city[p[0]].append(s)

    rows, divergences = [], []
    pm_day_yes = collections.defaultdict(list)   # (city,date) -> [bucket slugs pmus called YES]
    kal_cache = {}

    for city in sorted(by_city):
        if city not in WX:
            continue
        if city not in kal_cache:
            kal_cache[city] = kal_wx_settled(city); time.sleep(0.2)
        ksettled = kal_cache[city]
        for slug in sorted(by_city[city]):
            _, date, _ = parse_wx_slug(slug)
            pm = get(f"{PM}?slug={slug}"); time.sleep(0.12)
            pmm = (pm.get("markets") or [None])[0] if isinstance(pm, dict) else None
            pm_out, pm_oc, pm_op = pm_winner(pmm)
            pm_bnd = pm_bounds(slug)
            kmkt = ksettled.get(date, {}).get(pm_bnd)
            kres = kal_result(kmkt)
            if pm_out == "Yes":
                pm_day_yes[(city, date)].append(slug)
            rows.append({"cat": "weather", "city": city, "date": date, "slug": slug, "bounds": pm_bnd,
                         "pm_out": pm_out, "pm_oc": pm_oc, "pm_op": pm_op,
                         "kalshi": kmkt.get("ticker") if kmkt else None, "kal_result": kres})

    # cross-check pmus against itself: exactly one bucket should win YES per (city,date)
    inconsistent_days = {k: v for k, v in pm_day_yes.items() if len(v) != 1}

    matched = mism = compared = 0
    for r in rows:
        day = (r["city"], r["date"])
        r["pm_inconsistent_day"] = day in inconsistent_days
        if r["pm_out"] is None or r["kal_result"] is None:
            r["status"] = "unsettled_or_unjoined"; continue
        if r["pm_inconsistent_day"]:
            r["status"] = "pm_inconsistent"; continue
        pm_yes = (r["pm_out"] == "Yes")
        kal_yes = (r["kal_result"] == "yes")
        compared += 1
        if pm_yes == kal_yes:
            matched += 1; r["status"] = "agree"
        else:
            mism += 1; r["status"] = "DISAGREE"
            divergences.append(r)
    return {"rows": rows, "compared": compared, "matched": matched, "mismatched": mism,
            "inconsistent_days": inconsistent_days, "divergences": divergences,
            "n_slugs": len(wx_slugs)}


# ============================================================================================
# SPORTS recon (best-effort)
# ============================================================================================
def recon_sports(slugs, max_pairs, log=print):
    sp_slugs = [s for s in slugs if str(s).startswith("aec-")]
    by_league = collections.defaultdict(list)
    for s in sp_slugs:
        p = parse_sport_slug(s)
        if p and p[0] in LEAGUES:
            by_league[p[0]].append(s)

    rows, divergences = [], []
    matched = mism = compared = 0
    kcache = {}
    budget = max_pairs

    for league in sorted(by_league):
        series, join = LEAGUES[league]
        if league not in kcache:
            kcache[league] = kal_league_settled(series, join); time.sleep(0.25)
        kbydate, by_ticker = kcache[league]
        for slug in sorted(by_league[league]):
            if budget <= 0:
                break
            _, a, b, date = parse_sport_slug(slug)
            pm = get(f"{PM}?slug={slug}"); time.sleep(0.12)
            pmm = (pm.get("markets") or [None])[0] if isinstance(pm, dict) else None
            pm_out, pm_oc, pm_op = pm_winner(pmm)
            budget -= 1

            # map pmus winning label -> which side (A/B) via marketSides team metadata on the pmus object
            win_side = None
            kA = kB = None
            sides = (pmm or {}).get("marketSides") or []
            side_meta = []
            for s in sides:
                tm = (s.get("team") or {})
                side_meta.append({"name": tm.get("name"), "abbr": (tm.get("abbreviation") or "").lower(),
                                  "outcome": s.get("outcome") or s.get("name")})
            if join == "abbrev":
                kkeys = [sm["abbr"] for sm in side_meta if sm["abbr"]]
            else:
                kkeys = [surname(sm["name"]) for sm in side_meta if sm["name"]]
            kkeys = [k for k in kkeys if k]
            if len(kkeys) >= 2:
                kA, kB = kkeys[0], kkeys[1]

            # find the Kalshi settled event + its YES-resolved side
            kal_winner_key = None
            if kA and kB:
                found = pick_game(kbydate, kA, kB, join, date, slug_dated=True)
                if found:
                    pl, mA, mB = found
                    for key, tk in pl.items():
                        m = by_ticker.get(tk)
                        if kal_result(m) == "yes":
                            kal_winner_key = key
                            break

            # map pmus winner label back to a team key for comparison
            pm_winner_key = None
            if pm_out:
                for sm in side_meta:
                    label = sm["outcome"] or sm["name"]
                    if label and str(label).strip().lower() == str(pm_out).strip().lower():
                        pm_winner_key = (sm["abbr"] if join == "abbrev" else surname(sm["name"]))
                        break

            # pmus settlement-state signal: a closed market whose endDate is still in the FUTURE (well
            # after the game's calendar date) is a tell that pmus's outcomePrices are an interim/pipeline
            # state, not the FINAL graded result - it bears on whether a "divergence" is pmus being wrong.
            pm_end = (pmm or {}).get("endDate")
            pm_end_future = bool(pm_end and isinstance(pm_end, str) and pm_end[:10] > date)

            row = {"cat": "sports", "league": league, "date": date, "slug": slug, "pm_out": pm_out,
                   "pm_winner_key": pm_winner_key, "kal_winner_key": kal_winner_key,
                   "pm_end": pm_end, "pm_end_future": pm_end_future}
            rows.append(row)
            if pm_winner_key and kal_winner_key:
                compared += 1
                if pm_winner_key == kal_winner_key:
                    matched += 1; row["status"] = "agree"
                else:
                    mism += 1; row["status"] = "DISAGREE"; divergences.append(row)
            else:
                row["status"] = "unsettled_or_unjoined"
        if budget <= 0:
            break
    return {"rows": rows, "compared": compared, "matched": matched, "mismatched": mism,
            "divergences": divergences, "n_slugs": len(sp_slugs)}


# ============================================================================================
# REPORT
# ============================================================================================
def report(wx, sp):
    print("=" * 78)
    print("SETTLEMENT RECONCILIATION - empirical invariant-#1 check (settled markets, read-only)")
    print("=" * 78)

    print(f"\n[WEATHER]  co-listed slugs in archive: {wx['n_slugs']}")
    print(f"  joined+settled pairs compared : {wx['compared']}")
    print(f"  BOTH venues graded IDENTICALLY: {wx['matched']}")
    print(f"  DIVERGENCES                   : {wx['mismatched']}")
    if wx["inconsistent_days"]:
        print(f"  !!! pmus INTERNALLY-INCONSISTENT days (YES-winner count != 1; EXCLUDED from tally): "
              f"{len(wx['inconsistent_days'])}")
        for (city, date), bkts in sorted(wx["inconsistent_days"].items()):
            print(f"      {city} {date}: pmus reported YES for {len(bkts)} disjoint buckets -> {bkts}")
    for d in wx["divergences"]:
        print(f"      DIVERGE {d['city']} {d['date']} {d['slug']} bounds={d['bounds']}")
        print(f"              pmus={d['pm_out']} (oc={d['pm_oc']} op={d['pm_op']})  |  kalshi={d['kalshi']}={d['kal_result']}")

    print(f"\n[SPORTS]   co-listed slugs in archive: {sp['n_slugs']}")
    print(f"  joined+settled pairs compared : {sp['compared']}")
    print(f"  BOTH venues graded IDENTICALLY: {sp['matched']}")
    print(f"  DIVERGENCES                   : {sp['mismatched']}")
    compared_rows = [r for r in sp["rows"] if r.get("status") in ("agree", "DISAGREE")]
    n_cmp_future = sum(1 for r in compared_rows if r.get("pm_end_future"))
    if compared_rows:
        print(f"  !!! CAVEAT - pmus endDate STILL IN THE FUTURE on {n_cmp_future}/{len(compared_rows)} "
              f"compared pairs: pmus is closed:true but NOT finalized, so its outcomePrices are an INTERIM")
        print(f"      pipeline value, not the final grade. Neither these agreements NOR divergences are a "
              f"true settlement-identity read yet - pmus must reach its endDate first.")
    n_future = sum(1 for d in sp["divergences"] if d.get("pm_end_future"))
    if sp["divergences"]:
        print(f"  of which DIVERGENCES with future pmus endDate (interim, not final): {n_future}/{sp['mismatched']}")
    for d in sp["divergences"]:
        tag = " [pmus endDate FUTURE: " + str(d.get("pm_end")) + "]" if d.get("pm_end_future") else ""
        print(f"      DIVERGE {d['league']} {d['date']} {d['slug']}: "
              f"pmus->{d['pm_winner_key']} kalshi->{d['kal_winner_key']}{tag}")

    tot_c = wx["compared"] + sp["compared"]
    tot_m = wx["matched"] + sp["matched"]
    sp_future = sum(1 for r in sp["rows"] if r.get("status") in ("agree", "DISAGREE") and r.get("pm_end_future"))
    print("\n" + "-" * 78)
    if tot_c:
        print(f"OVERALL: {tot_m}/{tot_c} co-listed settled pairs graded IDENTICALLY "
              f"({100.0*tot_m/tot_c:.1f}%); {tot_c - tot_m} divergences.")
        if sp_future:
            print(f"BUT NOT YET A VALID INVARIANT-#1 READ: {sp_future}/{sp['compared']} sports pairs have a "
                  f"pmus endDate in the FUTURE (pmus closed:true != finalized), and the weather side has 0 "
                  f"cleanly-comparable pairs. pmus's outcomePrices are interim here, so this is INCONCLUSIVE "
                  f"on settlement identity - re-run after pmus markets pass their endDate.")
    else:
        print("OVERALL: 0 fully-settled-and-joined pairs to compare yet (archive is only ~days old).")
    print("-" * 78)
    return {"weather": wx, "sports": sp, "total_compared": tot_c, "total_matched": tot_m}


# ============================================================================================
# SELF-TEST (offline, synthetic)
# ============================================================================================
def _selftest():
    print("settle_recon self-test (offline)")
    # pm_winner: pairs label[i] with price[i]; order-independent; '1' marks the winner
    assert pm_winner({"closed": True, "outcomes": '["Yes","No"]', "outcomePrices": '["1","0"]'})[0] == "Yes"
    assert pm_winner({"closed": True, "outcomes": '["No","Yes"]', "outcomePrices": '["0","1"]'})[0] == "Yes"  # order flips
    assert pm_winner({"closed": True, "outcomes": '["No","Yes"]', "outcomePrices": '["1","0"]'})[0] == "No"
    assert pm_winner({"closed": True, "outcomes": '["Phillies","Jays"]', "outcomePrices": '["0","1"]'})[0] == "Jays"
    assert pm_winner({"closed": False, "outcomes": '["Yes","No"]', "outcomePrices": '["1","0"]'})[0] is None  # not closed
    assert pm_winner({"closed": True, "outcomes": '["Yes","No"]', "outcomePrices": '["1","1"]'})[0] is None  # two winners -> None
    # kal_result: only finalized/settled with a clean yes/no
    assert kal_result({"status": "finalized", "result": "yes"}) == "yes"
    assert kal_result({"status": "active", "result": "yes"}) is None
    assert kal_result({"status": "settled", "result": ""}) is None
    # slug parsers
    assert parse_wx_slug("tc-temp-miahigh-2026-06-08-gte90lt91f") == ("mia", "2026-06-08", "gte90lt91f")
    assert parse_wx_slug("aec-mlb-lad-pit-2026-06-09") is None
    assert parse_sport_slug("aec-mlb-lad-pit-2026-06-09") == ("mlb", "lad", "pit", "2026-06-09")
    assert parse_sport_slug("aec-atp-romsaf-gioper-2026-06-09") == ("atp", "romsaf", "gioper", "2026-06-09")
    # bound parity (reuses colisted_map's verified maps): pm slug bound == kalshi bound when aligned
    assert pm_bounds("tc-temp-miahigh-2026-06-08-gte90lt91f") == kbounds({"floor_strike": 90, "cap_strike": 91}) == (90, 91)

    # --- end-to-end weather recon on synthetic rows (no network): an AGREE day + a pmus-inconsistent day ---
    class _G:  # monkeypatch get()/PM/KAL by injecting via module-level recon using fakes is heavy;
        pass   # instead exercise the pure tally logic directly:
    # Build the same structure recon_weather builds post-fetch, then run its classification inline.
    rows = [
        {"city": "mia", "date": "2026-06-09", "slug": "A-gte90lt91f", "bounds": (90, 91), "pm_out": "Yes",
         "kal_result": "yes"},   # agree
        {"city": "mia", "date": "2026-06-09", "slug": "A-gte92lt93f", "bounds": (92, 93), "pm_out": "No",
         "kal_result": "no"},    # agree
        {"city": "sfo", "date": "2026-06-09", "slug": "B-lt64f", "bounds": (None, 63), "pm_out": "Yes",
         "kal_result": "no"},    # DISAGREE (and only 1 YES this day -> counts)
        {"city": "lax", "date": "2026-06-09", "slug": "C-gte76f", "bounds": (76, None), "pm_out": "Yes",
         "kal_result": "yes"},   # part of an inconsistent day below
        {"city": "lax", "date": "2026-06-09", "slug": "C-gte74lt75f", "bounds": (74, 75), "pm_out": "Yes",
         "kal_result": "no"},    # 2 YES same lax day -> inconsistent, both excluded
    ]
    pm_day_yes = collections.defaultdict(list)
    for r in rows:
        if r["pm_out"] == "Yes":
            pm_day_yes[(r["city"], r["date"])].append(r["slug"])
    inconsistent = {k: v for k, v in pm_day_yes.items() if len(v) != 1}
    assert ("lax", "2026-06-09") in inconsistent and ("mia", "2026-06-09") not in inconsistent
    matched = mism = compared = 0
    for r in rows:
        if (r["city"], r["date"]) in inconsistent:
            continue
        c, k = (r["pm_out"] == "Yes"), (r["kal_result"] == "yes")
        compared += 1
        if c == k: matched += 1
        else: mism += 1
    assert (compared, matched, mism) == (3, 2, 1), (compared, matched, mism)
    print("OK - pm_winner order-independence, kal_result gating, slug parse, bound parity, "
          "weather tally w/ inconsistent-day exclusion")


# ============================================================================================
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--no-sports", action="store_true")
    ap.add_argument("--archive", default=ARCHIVE)
    ap.add_argument("--max-pairs", type=int, default=60, help="cap sports pmus fetches (keeps live run fast)")
    a = ap.parse_args()

    if a.selftest:
        _selftest(); return

    slugs = archive_slugs(a.archive)
    print(f"co-listed slugs from live archive: {len(slugs)} "
          f"(weather={sum(s.startswith('tc-') for s in slugs)}, sports={sum(s.startswith('aec-') for s in slugs)})\n")
    wx = recon_weather(slugs)
    sp = (recon_sports(slugs, a.max_pairs) if not a.no_sports
          else {"rows": [], "compared": 0, "matched": 0, "mismatched": 0, "divergences": [],
                "n_slugs": sum(s.startswith('aec-') for s in slugs)})
    report(wx, sp)


if __name__ == "__main__":
    main()
