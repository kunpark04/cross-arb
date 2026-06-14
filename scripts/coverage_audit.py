"""scripts/coverage_audit.py — FULL-UNIVERSE cross-venue coverage audit (READ-ONLY, public data).

Answers ONE question with live numbers: how many candidate co-listed cross-venue arbs exist across the
ENTIRE polymarket.us catalog vs how many we currently TRACK — and where the real gap is (vs. where the
raw market-count gap is an illusion of props/futures that have no head-to-head Kalshi twin).

It is honest about the project's #1 rule (no false-positive joins): every count here is labelled
CONFIRMED-co-listed (Kalshi series cross-referenced + a structural game/threshold twin exists) vs
CANDIDATE-pending-rules-work (a Kalshi series plausibly co-lists it, but the game-level join + settlement
diff is not yet built). It never asserts an unverified join is a confirmed arb.

WHAT IT PRODUCES
  1. Per-category / per-league table: pmus count | Kalshi co-lists? | TRACKED? | addressable candidates |
     settlement-identity status. The 3 TRACKED categories get exact counts from build_colisted_map (run live).
  2. FIFA World Cup resolution: finds the Kalshi WC game series, sample-matches specific pmus fwc games by
     (teams,date), quotes concrete matched/unmatched examples + the settlement-source comparison.
  3. Sampled settlement-identity check: token-normalized pmus description station/source vs Kalshi
     rules_primary + settlement_sources.name, across categories.
  4. THE HEADLINE: total candidate co-listed universe vs tracked, as a multiple, split clean vs needs-work.

Reuses bot/colisted_map.py (pm_catalog, build_colisted_map, the matchers, WX/LEAGUES/ECON) — single source
of truth, exactly as scan_all.py does. Read-only: only catalog/series/market-LIST GETs (no books, no orders).
"""
import os, sys, re, json, time, collections
try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: pass
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bot"))
from colisted_map import (get, pm_catalog, build_colisted_map, surname, ktok_iso,
                          WX, LEAGUES, ECON, KAL)

KSERIES = "https://api.elections.kalshi.com/trade-api/v2/series"
OUT = os.path.join(os.path.dirname(__file__), "_data")
os.makedirs(OUT, exist_ok=True)

# ---------------------------------------------------------------------------------------------------
# pmus catalog helpers
# ---------------------------------------------------------------------------------------------------
def pm_seg2(slug):
    """polymarket.us league/family key = 2nd dash-segment of the slug (aec-MLB-..., atc-FWC-..., tc-TEMP-...)."""
    p = str(slug).split("-")
    return p[1] if len(p) > 1 else "?"

# pmus marketTypes that are a head-to-head / threshold OUTCOME we can in principle settlement-twin against a
# Kalshi binary. 'props/spreads/totals/futures' are NOT a clean same-number win/lose twin (different contract).
HEADTOHEAD_TYPES = {"moneyline", "drawable_outcome"}        # 2-way + 3-way (with TIE) game-winner
THRESHOLD_TYPES  = {"binary_threshold", "scalar"}           # econ-style >=/<= cutoffs (best-effort)

def pm_distinct_games(markets):
    """For head-to-head pmus markets, collapse the per-outcome rows to distinct GAMES (slug minus the trailing
    outcome token). A 3-way WC game lists 3 outcome-markets (…-ger / …-cuw / …-tie variants under one game)."""
    games = set()
    for m in markets:
        parts = str(m.get("slug")).split("-")
        games.add("-".join(parts[:-1]))                     # drop the per-outcome suffix
    return games

# ---------------------------------------------------------------------------------------------------
# Kalshi series catalog -> a coarse "does Kalshi cover this pmus league/category?" cross-reference.
# We can't game-match every league here (that's the per-league matcher's job), so co-listing detection is:
#   (a) a KNOWN explicit series-ticker mapping (the strongest signal — these are confirmed), OR
#   (b) a keyword hit on the Kalshi series catalog (ticker/title) for that league/category (a CANDIDATE).
# ---------------------------------------------------------------------------------------------------
# Explicit pmus-seg2 -> Kalshi series ticker(s) that DEMONSTRABLY co-list the same head-to-head events.
# (mlb/nba/... already in LEAGUES; the additions below are the unmapped leagues this audit resolves.)
KAL_LEAGUE_SERIES = {
    # --- already tracked (from LEAGUES) ---
    **{lg: (ser,) for lg, (ser, _join) in LEAGUES.items()},
    # --- unmapped pmus leagues -> the Kalshi GAME series that co-lists them (verified present this run) ---
    "fwc":    ("KXWCGAME",),       # FIFA World Cup 2026 match-winner (3-way win/win/TIE)  ✅ co-listed
    "mls":    ("KXMLSGAME",),      # MLS game (pmus mls = futures only this run; flagged)
    "nfl":    ("KXNFLGAME",),      # NFL game (offseason: pmus nfl = futures only this run)
    "nba":    ("KXNBAGAME",),
}
# pmus categories -> Kalshi category(ies) for the coarse "is there ANY overlap" cross-reference.
KAL_CAT_FOR_PM = {
    "sports":   ("Sports",),
    "climate":  ("Climate and Weather", "World"),
    "macro":    ("Economics", "Financials"),
    "politics": ("Politics", "Elections"),
    "culture":  ("Entertainment", "Mentions", "Social", "Science and Technology"),
    "finance":  ("Financials", "Companies", "Crypto", "Commodities", "Economics"),
}

def kalshi_series_catalog():
    ks = get(KSERIES).get("series", [])
    return ks

def kalshi_open_count(series_ticker):
    """How many OPEN markets the Kalshi series has right now (paginated). 0 => the series exists but is dormant
    (e.g. NFL/MLS in the WC-season window) => NOT a live co-listing today."""
    n, cur = 0, None
    for _ in range(12):
        u = f"{KAL}?series_ticker={series_ticker}&status=open&limit=1000" + (f"&cursor={cur}" if cur else "")
        d = get(u); ms = d.get("markets", []); n += len(ms); cur = d.get("cursor")
        if not cur: break
        time.sleep(0.2)
    return n

# ---------------------------------------------------------------------------------------------------
# Settlement-identity token check (sample): does pmus description's source/station match Kalshi's
# rules_primary + settlement_sources.name on a token-normalized basis?
# ---------------------------------------------------------------------------------------------------
_SRC_CANON = {  # collapse equivalent provider names so "NWS"~"National Weather Service" don't false-mismatch
    "nws": "nws", "national weather service": "nws", "climatological report": "nws_cli",
    "noaa": "noaa", "national oceanic and atmospheric administration": "noaa",
    "bls": "bls", "bureau of labor statistics": "bls",
    "bea": "bea", "bureau of economic analysis": "bea",
    "fed": "fed", "federal reserve": "fed", "fomc": "fed",
    "espn": "espn", "fox sports": "foxsports", "fox": "foxsports",
    "mlb": "mlb", "major league baseball": "mlb", "statsapi": "mlb",
    # NB: pmus often writes "the relevant governing body or event official" (i.e. FIFA) while Kalshi names
    # "ESPN; Fox Sports" — DIFFERENT stated sources for the SAME real result; the token check will (correctly)
    # NOT pin a shared canonical provider, surfacing that as a settlement-identity item to verify, not assume.
    "fifa": "fifa", "governing body": "gov_body",
}
def src_tokens(text):
    t = str(text or "").lower()
    t = re.sub(r"[^a-z0-9 ]", " ", t)
    toks = set(w for w in t.split() if len(w) > 2)
    # also fold known multi-word provider phrases
    canon = set()
    for phrase, c in _SRC_CANON.items():
        if phrase in t: canon.add(c)
    return toks, canon

def settlement_source_match(pm_desc, k_rules, k_sources):
    """Return (verdict, shared_canonical_providers). verdict in {SAME, PARTIAL, DIFFERENT, INSUFFICIENT}.
    SAME      = the two sides share >=1 canonical provider AND no obviously conflicting one.
    PARTIAL   = some token overlap but no canonical provider pinned.
    Heuristic + sampled — flags where 'same source' is mechanically confirmable vs where it needs eyes."""
    pm_t, pm_c = src_tokens(pm_desc)
    k_t,  k_c  = src_tokens(str(k_rules) + " " + " ".join(k_sources))
    if not pm_t or not k_t: return "INSUFFICIENT", []
    shared_c = sorted(pm_c & k_c)
    if shared_c: return "SAME", shared_c
    shared = pm_t & k_t
    # meaningful shared content words (drop boilerplate)
    boiler = {"the","will","this","market","resolves","yes","scheduled","originally","outcome","event",
              "between","after","report","sourced","relevant","governing","body","official","price"}
    sig = sorted(shared - boiler)
    if len(sig) >= 2: return "PARTIAL", sig[:6]
    return "DIFFERENT", sig[:6]

def k_market_rules(series_ticker, limit=3):
    """Pull a few open markets from a Kalshi series and return [(yes_sub_title, rules_primary)]."""
    d = get(f"{KAL}?series_ticker={series_ticker}&status=open&limit={limit}")
    return [(m.get("yes_sub_title"), str(m.get("rules_primary") or "")) for m in d.get("markets", [])[:limit]]

# ===================================================================================================
# MAIN
# ===================================================================================================
def main():
    t0 = time.time()
    print("=" * 104)
    print("CROSS-ARB FULL-UNIVERSE COVERAGE AUDIT  (read-only, live public data)")
    print("=" * 104)

    # ---- 1a. Pull both catalogs once ----
    print("\n[1/5] Pulling polymarket.us full catalog + Kalshi series catalog (live)...")
    allm = pm_catalog()
    ks   = kalshi_series_catalog()
    live = [m for m in allm if m.get("active") and not m.get("closed") and not m.get("archived")]
    print(f"      pmus catalog rows: {len(allm)}   (active&open: {len(live)})   Kalshi series: {len(ks)}")
    kseries_tickers = {s.get("ticker") for s in ks}
    kby_cat = collections.Counter(s.get("category") for s in ks)

    # ---- 1b. Run the REAL tracked-coverage builder (exact co-listed + settlement-qualified counts) ----
    print("\n[2/5] Running build_colisted_map() live — exact TRACKED co-listed pair counts (weather/sports/econ)...")
    colisted, rep = build_colisted_map()
    tracked_counts = rep["counts"]
    # tracked pairs -> distinct GAMES/DAYS for an apples-to-apples 'opportunity' count
    tracked_weather = tracked_counts["weather_pairs"]        # 1 pair = 1 bucket (an opportunity unit)
    tracked_sports  = tracked_counts["sports_pairs"]         # 1 pair = 1 game
    tracked_econ    = tracked_counts["econ_pairs"]
    if rep.get("fetch_errors"):
        print(f"      WARNING: {len(rep['fetch_errors'])} fetch errors — counts are a DEGRADED lower bound.")
    print(f"      TRACKED co-listed pairs: weather={tracked_weather}  sports={tracked_sports}  econ={tracked_econ}")
    print(f"      coverage report flags: weather_cities_UNMAPPED={rep['weather_cities_UNMAPPED']} "
          f"sports_leagues_UNMAPPED={rep['sports_leagues_UNMAPPED']} econ_fams_UNMAPPED={rep['econ_families_UNMAPPED']}")

    # ---- 2. Per-category / per-league universe table ----
    print("\n[3/5] Building per-category / per-league coverage table...")
    rows = []   # each: dict(category, key, pm_total, kalshi_colists, tracked, candidates, candidate_kind, settle_status)

    # --- SPORTS: break out by pmus league (seg2), with marketType-aware addressable candidate count ---
    sports = [m for m in live if m.get("category") == "sports"]
    by_league = collections.defaultdict(list)
    for m in sports: by_league[pm_seg2(m.get("slug"))].append(m)
    for lg in sorted(by_league, key=lambda k: -len(by_league[k])):
        ms = by_league[lg]
        mt = collections.Counter(m.get("marketType") for m in ms)
        h2h = [m for m in ms if m.get("marketType") in HEADTOHEAD_TYPES]
        n_games = len(pm_distinct_games(h2h)) if h2h else 0
        tracked = lg in LEAGUES
        kser = KAL_LEAGUE_SERIES.get(lg)
        # confirmed co-listing only if a known Kalshi GAME series for this league has OPEN markets right now
        kopen = kalshi_open_count(kser[0]) if kser and kser[0] in kseries_tickers else 0
        colists = "yes" if kopen > 0 else ("series-exists-dormant" if kser and kser[0] in kseries_tickers else "no")
        if tracked:
            cand_kind = "TRACKED"
            candidates = None        # exact count comes from build_colisted_map (sports total), not per-league here
        elif kopen > 0 and n_games > 0:
            cand_kind = "CANDIDATE (needs game-join + rules)"
            candidates = n_games
        elif n_games == 0 and kser:
            cand_kind = "no head-to-head pmus markets (futures/props only)"
            candidates = 0
        else:
            cand_kind = "no Kalshi game co-listing"
            candidates = 0
        rows.append(dict(category="sports", key=lg, pm_total=len(ms), kalshi_colists=colists,
                         kopen=kopen, tracked=tracked, candidates=candidates, candidate_kind=cand_kind,
                         marketTypes=dict(mt), n_h2h_games=n_games, kseries=(kser[0] if kser else None)))

    # --- WEATHER / ECON: tracked categories; report exact build_colisted_map counts at category level ---
    rows.append(dict(category="climate", key="(weather buckets)", pm_total=sum(1 for m in live if m.get("category")=="climate"),
                     kalshi_colists="yes", tracked=True, candidates=tracked_weather,
                     candidate_kind="TRACKED (exact, settlement-verified)", marketTypes={}, kseries="KXHIGH*"))
    rows.append(dict(category="macro", key="(econ thresholds)", pm_total=sum(1 for m in live if m.get("category")=="macro"),
                     kalshi_colists="yes", tracked=True, candidates=tracked_econ,
                     candidate_kind="TRACKED (exact, identical-twin join 0013)", marketTypes={}, kseries="KX(CPIYOY|U3|PAYROLLS|GDP|FEDDECISION)"))

    # --- UNMAPPED categories entirely: politics / culture / finance — is there ANY Kalshi cross-listing? ---
    for pmcat in ["politics", "culture", "finance"]:
        ms = [m for m in live if m.get("category") == pmcat]
        if not ms: continue
        kcats = KAL_CAT_FOR_PM.get(pmcat, ())
        kcat_n = sum(kby_cat.get(c, 0) for c in kcats)
        # These are NOT head-to-head; co-listing is event-by-event semantic (same election/award/print).
        # We can confirm Kalshi HAS the category, but not auto-join individual markets here -> CANDIDATE only.
        colists = "category-exists" if kcat_n > 0 else "no"
        rows.append(dict(category=pmcat, key=f"(all {pmcat})", pm_total=len(ms),
                         kalshi_colists=colists, tracked=False, candidates=len(ms),
                         candidate_kind=f"CANDIDATE — Kalshi has {kcat_n} {'/'.join(kcats)} series, "
                                        f"but per-market semantic join + rules NOT built",
                         marketTypes=dict(collections.Counter(m.get("marketType") for m in ms)), kseries=None))

    # ---- print the table ----
    print("\n" + "=" * 104)
    print("PER-CATEGORY / PER-LEAGUE COVERAGE TABLE")
    print("=" * 104)
    hdr = f"{'category':9} {'league/key':16} {'pmus':>6} {'K co-lists':>20} {'tracked':>7} {'cand':>6}  {'status'}"
    print(hdr); print("-" * 104)
    for r in sorted(rows, key=lambda r: (r["category"], -(r["pm_total"]))):
        cand = "n/a" if r["candidates"] is None else str(r["candidates"])
        print(f"{r['category']:9} {str(r['key'])[:16]:16} {r['pm_total']:>6} {str(r['kalshi_colists'])[:20]:>20} "
              f"{('Y' if r['tracked'] else '·'):>7} {cand:>6}  {r['candidate_kind']}")

    # ---- 3. FIFA WORLD CUP DEEP DIVE ----
    print("\n" + "=" * 104)
    print("FIFA WORLD CUP RESOLUTION  (highest-priority unmapped block)")
    print("=" * 104)
    fifa = fifa_worldcup_resolution(live, ks)

    # ---- 4. SAMPLED settlement-identity check across categories ----
    print("\n" + "=" * 104)
    print("SAMPLED SETTLEMENT-IDENTITY CHECK  (pmus description source/station  vs  Kalshi rules + sources)")
    print("=" * 104)
    sample_settlement_checks(live, ks, fifa)

    # ---- 5. HEADLINE GAP ----
    print("\n" + "=" * 104)
    print("HEADLINE: candidate co-listed universe vs what we track")
    print("=" * 104)
    headline(rows, tracked_weather, tracked_sports, tracked_econ, fifa)

    # ---- persist the machine-readable audit ----
    out = {"generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "pm_live": len(live), "kalshi_series": len(ks),
           "tracked": {"weather": tracked_weather, "sports": tracked_sports, "econ": tracked_econ},
           "rows": rows, "fifa": fifa, "coverage_report": rep}
    json.dump(out, open(os.path.join(OUT, "coverage_audit.json"), "w"), indent=1, default=str)
    print(f"\n[done in {time.time()-t0:.0f}s]  machine-readable -> scripts/_data/coverage_audit.json")


def fifa_worldcup_resolution(live, ks):
    """Does Kalshi co-list World Cup matches? Sample-match concrete pmus fwc games to KXWCGAME. Returns a dict
    with the counts + 2-3 concrete examples for the report."""
    # pmus side: the World Cup HEAD-TO-HEAD match-winner markets are seg2='fwc' + marketType drawable_outcome.
    sports = [m for m in live if m.get("category") == "sports"]
    fwc_all = [m for m in sports if pm_seg2(m.get("slug")) == "fwc"]
    fwc_h2h = [m for m in fwc_all if m.get("marketType") == "drawable_outcome" and m.get("gameStartTime")]
    fifa_fut = [m for m in sports if pm_seg2(m.get("slug")) == "fifa"]   # the OTHER block — futures/props
    mt_fwc = collections.Counter(m.get("marketType") for m in fwc_all)
    print(f"  pmus 'fwc' total markets={len(fwc_all)}  marketTypes={dict(mt_fwc)}")
    print(f"  pmus 'fwc' HEAD-TO-HEAD (drawable_outcome match-winner) markets={len(fwc_h2h)}")
    print(f"  pmus 'fifa' block (separate)={len(fifa_fut)}  marketTypes={dict(collections.Counter(m.get('marketType') for m in fifa_fut))}")
    print(f"    -> the 'fifa' block is FUTURES/PROPS (stage-of-elimination etc), NOT head-to-head games.")

    # Parse pmus fwc games: slug 'atc-fwc-ger-cuw-2026-06-14-ger' -> (date, {teamA,teamB}); collapse 3 outcomes/game
    def fwc_game_key(m):
        s = str(m.get("slug"))
        mt = re.match(r"[a-z]+-fwc-([a-z]{2,4})-([a-z]{2,4})-(\d{4}-\d{2}-\d{2})", s)
        if not mt: return None
        teams = frozenset([mt.group(1), mt.group(2)])
        return (mt.group(3), teams)
    pm_games = {}
    for m in fwc_h2h:
        k = fwc_game_key(m)
        if k: pm_games.setdefault(k, []).append(m)
    print(f"  pmus distinct World Cup GAMES (head-to-head): {len(pm_games)}")

    # Kalshi side: find the WC game series + enumerate its games.
    wc_series = [s.get("ticker") for s in ks
                 if re.search(r"world ?cup|worldcup", str(s.get("title","")).lower()) and "GAME" in str(s.get("ticker",""))]
    print(f"  Kalshi World-Cup GAME series found: {wc_series or 'NONE'}")
    kgames = {}   # (date_iso, frozenset(team_abbrev)) -> event_ticker
    kev_markets = {}   # event_ticker -> [markets] (keep the matched game's OWN rules, not market#0's)
    if "KXWCGAME" in {s.get("ticker") for s in ks}:
        mk, cur = [], None
        for _ in range(8):
            u = f"{KAL}?series_ticker=KXWCGAME&status=open&limit=1000" + (f"&cursor={cur}" if cur else "")
            d = get(u); mk += d.get("markets", []); cur = d.get("cursor")
            if not cur: break
            time.sleep(0.2)
        evs = collections.defaultdict(list)
        for m in mk: evs[m.get("event_ticker")].append(m)
        kev_markets = dict(evs)
        for ev, ms in evs.items():
            mt = re.match(r"KXWCGAME-(\d{2}[A-Z]{3}\d{2})([A-Z]{3})([A-Z]{3})", str(ev))
            if mt:
                kgames[(ktok_iso(mt.group(1)), frozenset([mt.group(2).lower(), mt.group(3).lower()]))] = ev
        print(f"  Kalshi KXWCGAME open events (games): {len(kgames)}   (each = 3-way win/win/TIE)")

    # pmus uses 3-letter team codes in the slug too; Kalshi uses 3-letter abbrevs. Try direct + a small alias map
    # for the few that differ (FIFA codes are mostly identical; alias only where the slugs demonstrably diverge).
    # We match on DATE first then look for an abbrev-set overlap (>=1 shared 3-letter code) — and REQUIRE both
    # teams to resolve before calling it a match (no-false-positive rule).
    def abbr3(x):  # normalize a team token to a 3-letter lower code for set compare
        return str(x)[:3].lower()
    matched, unmatched = [], []
    for (pdate, pteams), pmlist in sorted(pm_games.items()):
        pset = {abbr3(t) for t in pteams}
        # exact (date, team-set) — try equality then >=1 overlap on same date
        hit = None
        for (kdate, kteams), ev in kgames.items():
            if kdate == pdate and (pset == set(kteams) or len(pset & set(kteams)) >= 2):
                hit = (ev, kteams); break
        if hit: matched.append((pdate, pteams, pmlist, hit))
        else:   unmatched.append((pdate, pteams, pmlist))
    print(f"\n  WORLD-CUP SAMPLE MATCH (date + both-team-code join):")
    print(f"    pmus head-to-head games: {len(pm_games)}   Kalshi KXWCGAME games: {len(kgames)}")
    print(f"    MATCHED (same game on both venues): {len(matched)}   unmatched-this-window: {len(unmatched)}")

    # concrete examples (settlement source compared too) — uses the MATCHED event's OWN rules_primary
    ksrc = [x.get("name","") if isinstance(x,dict) else str(x)
            for s in ks if s.get("ticker")=="KXWCGAME" for x in (s.get("settlement_sources") or [])]
    ex = []
    for (pdate, pteams, pmlist, (ev, kteams)) in matched[:3]:
        pm0 = pmlist[0]
        k_rule = str((kev_markets.get(ev) or [{}])[0].get("rules_primary") or "")   # THIS game's rule
        verdict, shared = settlement_source_match(pm0.get("description"), k_rule, ksrc)
        ex.append(dict(date=pdate, pm_teams=sorted(pteams), pm_slug=pm0.get("slug"), kalshi_event=ev,
                       kalshi_teams=sorted(kteams), settle_verdict=verdict, shared_src=shared,
                       k_rule=k_rule[:160], pm_desc=str(pm0.get("description"))[:160], k_src=ksrc))
        print(f"\n    [MATCH] {pdate}  pmus{sorted(pteams)} <-> Kalshi {ev} {sorted(kteams)}")
        print(f"            pmus slug : {pm0.get('slug')}")
        print(f"            pmus desc : {str(pm0.get('description'))[:130]}")
        print(f"            Kalshi rule: {k_rule[:130]}")
        print(f"            Kalshi src : {ksrc}   |  pmus names: 'the relevant governing body or event official'")
        print(f"            settlement-source verdict: {verdict}  shared={shared}")
    for (pdate, pteams, pmlist) in unmatched[:2]:
        print(f"\n    [no-Kalshi-match this window] {pdate} pmus{sorted(pteams)}  slug={pmlist[0].get('slug')}")
        print(f"            (likely a later-round game Kalshi hasn't opened yet, or a team-code/date offset)")

    # SETTLEMENT-IDENTITY caveat (the load-bearing one for WC): the two venues NAME DIFFERENT SOURCES.
    #   pmus  -> "...Outcome sourced from the relevant governing body or event official..." (i.e. FIFA)
    #   Kalshi-> settlement_sources = ESPN; Fox Sports, and rules add "after 90 minutes plus stoppage".
    # For a normally-completed match these agree on the real result, but it is NOT a token-confirmable
    # SAME-source identity, and the "after 90 min + stoppage" wording vs a penalty-shootout/extra-time edge
    # case must be diffed before trusting the lock (same class of risk as the MLB void tail, 0001/sports brief).
    print(f"\n  SETTLEMENT-IDENTITY (World Cup): the venues NAME DIFFERENT SOURCES — pmus 'relevant governing")
    print(f"  body' (FIFA) vs Kalshi 'ESPN; Fox Sports'; both also need a 90-min-vs-extra-time/penalties diff.")
    print(f"  Agrees for a clean completed match, but is a CANDIDATE pending a rules diff, not a confirmed twin.")
    # STRUCTURAL caveat: pmus drawable_outcome is 3-way (win/lose/draw) and Kalshi KXWCGAME is ALSO 3-way
    # (team/team/TIE) — so a lock needs the 3-outcome basket, not the 2-leg YES/NO the current monitor tracks.
    print(f"\n  STRUCTURAL NOTE: both venues price soccer as a 3-WAY market (win/lose/DRAW). The current sports")
    print(f"  tracker is built for 2-way moneyline (YES+NO offset). World Cup needs a 3-outcome basket join")
    print(f"  (teamA/teamB/TIE on both sides) — addressable, but a new contract shape, not a drop-in league add.")

    return dict(fwc_total=len(fwc_all), fwc_h2h_markets=len(fwc_h2h), fwc_h2h_games=len(pm_games),
                fifa_block=len(fifa_fut), kalshi_wc_game_series=wc_series, kalshi_wc_games=len(kgames),
                matched_games=len(matched), unmatched_games=len(unmatched), examples=ex,
                marketTypes_fwc=dict(mt_fwc))


def sample_settlement_checks(live, ks, fifa):
    """A handful of co-listed pairs across categories: is 'same source / same station' mechanically confirmable?"""
    kseries = {s.get("ticker"): s for s in ks}
    def ksrc(tk):
        s = kseries.get(tk) or {}
        return [x.get("name","") if isinstance(x,dict) else str(x) for x in (s.get("settlement_sources") or [])]
    checks = []

    # weather: a co-listed city — pmus climate description vs Kalshi KXHIGH* rules+sources
    clim = [m for m in live if m.get("category") == "climate"]
    if clim:
        pm0 = clim[0]
        kr = k_market_rules("KXHIGHNY", limit=1) or k_market_rules("KXHIGHMIA", limit=1)
        v, sh = settlement_source_match(pm0.get("description"), kr[0][1] if kr else "", ksrc("KXHIGHNY"))
        checks.append(("weather/NYC-high", v, sh, str(pm0.get("description"))[:90], (kr[0][1][:90] if kr else "")))

    # econ: CPI — pmus macro description vs Kalshi KXCPIYOY
    macro = [m for m in live if m.get("category") == "macro" and str(m.get("slug","")).startswith("cpic")]
    if macro:
        pm0 = macro[0]
        kr = k_market_rules("KXCPIYOY", limit=1)
        v, sh = settlement_source_match(pm0.get("description"), kr[0][1] if kr else "", ksrc("KXCPIYOY"))
        checks.append(("econ/CPI-YoY", v, sh, str(pm0.get("description"))[:90], (kr[0][1][:90] if kr else "")))

    # sports MLB: pmus moneyline description vs Kalshi KXMLBGAME
    mlb = [m for m in live if m.get("category")=="sports" and pm_seg2(m.get("slug"))=="mlb"
           and m.get("marketType")=="moneyline"]
    if mlb:
        pm0 = mlb[0]
        kr = k_market_rules("KXMLBGAME", limit=1)
        v, sh = settlement_source_match(pm0.get("description"), kr[0][1] if kr else "", ksrc("KXMLBGAME"))
        checks.append(("sports/MLB-game", v, sh, str(pm0.get("description"))[:90], (kr[0][1][:90] if kr else "")))

    # World Cup (from fifa deep-dive examples)
    for ex in (fifa.get("examples") or [])[:1]:
        checks.append((f"sports/WorldCup {ex['date']}", ex["settle_verdict"], ex["shared_src"], "(see FIFA section)", "(KXWCGAME)"))

    print(f"  {'pair':22} {'verdict':12} {'shared canonical/sig tokens'}")
    print("  " + "-" * 96)
    for name, v, sh, pmd, kr in checks:
        print(f"  {name:22} {v:12} {sh}")
    print("\n  Interpretation: SAME = a shared canonical provider (NWS/BLS/BEA/Fed/ESPN) is pinned on BOTH sides")
    print("  (mechanically confirmable). PARTIAL/DIFFERENT/INSUFFICIENT = the description wording diverges enough")
    print("  that a token check can't certify identity -> needs the per-category settlement brief (the manual")
    print("  verify_*.py path). NOTE: a SAME token verdict still does NOT certify bucket-boundary / void-tail")
    print("  identity — that is the deeper settlement-identity work (decisions 0001/0013, the verify_* scripts).")
    return checks


def headline(rows, tw, ts, te, fifa):
    # TRACKED opportunity units (pairs from build_colisted_map)
    tracked_total = tw + ts + te

    # CANDIDATE co-listed universe = TRACKED  +  confirmed-co-listed-but-untracked  +  needs-rules-work.
    # Keep the buckets explicit and honest (the project's #1 rule).
    confirmed_untracked = 0      # a Kalshi game series is OPEN + pmus has head-to-head games, just not joined yet
    needs_rules = 0              # category exists on Kalshi but per-market join not built (politics/culture/finance)
    confirmed_lines = []
    needs_lines = []
    for r in rows:
        if r["tracked"]: continue
        c = r["candidates"] or 0
        if c == 0: continue
        if str(r["kalshi_colists"]).startswith("yes") and "CANDIDATE (needs game-join" in r["candidate_kind"]:
            confirmed_untracked += c
            confirmed_lines.append(f"{r['category']}/{r['key']}: {c} games (Kalshi {r.get('kseries')})")
        else:
            needs_rules += c
            needs_lines.append(f"{r['category']}/{r['key']}: {c} markets ({r['candidate_kind'][:60]})")

    candidate_total = tracked_total + confirmed_untracked + needs_rules

    print(f"\n  WHAT WE TRACK NOW (exact, settlement-qualified pairs from build_colisted_map):")
    print(f"     weather buckets : {tw}")
    print(f"     sports games    : {ts}")
    print(f"     econ thresholds : {te}")
    print(f"     -------------------------")
    print(f"     TRACKED TOTAL   : {tracked_total} co-listed opportunity-units")

    print(f"\n  CONFIRMED co-listed but NOT yet tracked (Kalshi series open + pmus head-to-head games exist):")
    for l in confirmed_lines: print(f"     + {l}")
    if not confirmed_lines: print("     (none)")
    print(f"     subtotal (settlement-PLAUSIBLE, needs game-join + rules diff): {confirmed_untracked}")

    print(f"\n  CANDIDATE — Kalshi has the category but per-market join + rules NOT built (treat as unverified):")
    for l in needs_lines: print(f"     ? {l}")
    if not needs_lines: print("     (none)")
    print(f"     subtotal (needs real rules work — NOT confirmed arbs): {needs_rules}")

    print(f"\n  " + "=" * 90)
    print(f"  HEADLINE NUMBERS")
    print(f"  " + "=" * 90)
    if tracked_total:
        mult_conf = (tracked_total + confirmed_untracked) / tracked_total
        mult_all  = candidate_total / tracked_total
        print(f"  We TRACK {tracked_total} co-listed opportunity-units.")
        print(f"  Adding the CONFIRMED-co-listed-untracked block (World Cup etc): {tracked_total + confirmed_untracked} "
              f"  -> {mult_conf:.2f}x ({(mult_conf-1)*100:+.0f}%) more, settlement-PLAUSIBLE.")
        print(f"  Adding ALL candidates incl. needs-rules categories: {candidate_total}  -> {mult_all:.2f}x "
              f"({(mult_all-1)*100:+.0f}%) — but the needs-rules block is UNVERIFIED, not confirmed arbs.")
    print(f"\n  HONEST READ:")
    print(f"   * The big raw pmus counts (fwc ~6k, fifa ~1.5k, pga/f1/nfl) are MOSTLY props/futures/spreads that")
    print(f"     have NO clean head-to-head Kalshi twin — they are NOT addressable cross-venue locks.")
    print(f"   * The one large genuinely-co-listed UNTRACKED block is the FIFA WORLD CUP: pmus head-to-head")
    print(f"     games={fifa['fwc_h2h_games']} matched to Kalshi KXWCGAME games={fifa['kalshi_wc_games']} "
          f"(matched {fifa['matched_games']}).")
    print(f"     BUT it is a 3-WAY market (win/lose/DRAW) — a new contract shape, not a drop-in 2-way league.")
    print(f"   * politics/culture/finance: Kalshi HAS these categories, so co-listings plausibly exist, but each")
    print(f"     needs a per-market semantic join + settlement-rules diff before any count is a real arb.")
    print(f"   * NOTHING here is a confirmed arb beyond what we track until the game-join + rules work is done.")


if __name__ == "__main__":
    main()
