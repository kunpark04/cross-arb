"""bot/colisted_map.py - build the co-listed {polymarket.us slug <-> Kalshi ticker} map by FULL
discovery, and AUDIT coverage so new markets are never silently missed. READ-ONLY (catalog reads only).

Freshness model (answers "does this miss new markets?"):
  * Every call pulls the ENTIRE polymarket.us closed=false catalog + the Kalshi series, and groups by
    date/event DYNAMICALLY - so new weather DAYS and new sports GAMES in known cities/leagues are picked
    up automatically. The monitor calls this on startup AND on the REST heartbeat to refresh (add new
    pairs, drop settled ones) - decision 0003's "coverage heartbeat".
  * The one thing discovery can't infer is a brand-new CATEGORY: a new weather city or a new sports
    league that isn't in WX / LEAGUES below. So build_colisted_map() also returns a COVERAGE REPORT that
    lists every climate city + sports league polymarket.us is currently listing and flags any we don't
    map. Unmapped => we'd miss it until WX/LEAGUES is extended -> the report makes that LOUD, not silent.

WX / LEAGUES / ECON mirror scripts/scan_all.py (the validated matcher). Keep them in sync; the coverage
audit below is the backstop that catches drift. Matching is identity-based (city+date+bucket; league+date+
abbrev|surname; econ family+period+threshold) - the no-false-positive invariant (lesson L1). ECON
(CPI/U-3/NFP/GDP/Fed) covers the cleanest US-legal subset; only SAME-orientation pairs are mapped
(settlement identity verified by scripts/verify_econ_settlement.py). No order books are fetched here (fast).
"""
import os, sys, re, json, time, urllib.request, urllib.error, collections, unicodedata
try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: pass

PM = "https://gateway.polymarket.us/v1/markets"
KAL = "https://api.elections.kalshi.com/trade-api/v2/markets"

# --- config mirrored from scripts/scan_all.py (keep in sync; coverage audit flags drift) ---
WX = {"sfo": "KXHIGHTSFO", "lax": "KXHIGHLAX", "nyc": "KXHIGHNY", "mia": "KXHIGHMIA", "mdw": "KXHIGHCHI"}
LEAGUES = {"mlb": ("KXMLBGAME", "abbrev"), "wnba": ("KXWNBAGAME", "abbrev"), "nba": ("KXNBAGAME", "abbrev"),
           "nhl": ("KXNHLGAME", "abbrev"), "cs2": ("KXCS2GAME", "abbrev"), "lol": ("KXLOLGAME", "abbrev"),
           "valorant": ("KXVALORANTGAME", "abbrev"), "atp": ("KXATPMATCH", "surname"),
           "wta": ("KXWTAMATCH", "surname"), "itfm": ("KXITFMATCH", "surname"),
           "itfw": ("KXITFWMATCH", "surname"), "ufc": ("KXUFCFIGHT", "surname")}

UA = {"User-Agent": "cross-arb/1.0", "Accept": "application/json"}
MON = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]

def get(url, tries=4):
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429 and i < tries - 1: time.sleep(3); continue
            return {"_err": e.code}
        except Exception as e:
            return {"_err": str(e)[:60]}
    return {}

def ktok_iso(t):
    m = re.match(r"(\d{2})([A-Z]{3})(\d{2})", t)
    return f"20{m.group(1)}-{MON.index(m.group(2)) + 1:02d}-{m.group(3)}" if m else None
def dnear(a, b):
    from datetime import date
    try:
        ya, ma, da = map(int, a.split("-")); yb, mb, db = map(int, b.split("-"))
        return abs((date(ya, ma, da) - date(yb, mb, db)).days) <= 1
    except Exception: return a == b
def surname(name):
    n = unicodedata.normalize("NFKD", str(name)).encode("ascii", "ignore").decode().lower()
    n = re.sub(r"[^a-z \-]", " ", n); t = [x for x in n.split() if x]; return t[-1] if t else ""
def smatch(a, b):
    """Surname join: exact, or one a prefix of the other differing by <=1 char (accent/truncation noise).
    The <=1 guard rejects DISTINCT players that merely share a prefix (martin~martinez, williams~williamson,
    mann~mannarino, koval~kovalenko) - the no-false-positive invariant (L1)."""
    if a == b: return True
    if len(a) < 4 or len(b) < 4: return False
    return (a.startswith(b) or b.startswith(a)) and abs(len(a) - len(b)) <= 1
def wcity(s): m = re.search(r"tc-temp-([a-z]+?)high", str(s)); return m.group(1) if m else None
def pm_lo(s):
    m = re.search(r"-lt(\d+)f", s); m2 = re.search(r"gte(\d+)", s)
    return (int(m.group(1)) - 100) if m else (int(m2.group(1)) if m2 else 0)
def pmlg(s): p = str(s).split("-"); return p[1] if len(p) > 1 else "?"

# --- WEATHER bucket boundary equality. The settlement-identity guard: pair pm[i] with kalshi[i] ONLY when
#     their (floor, cap) boundary NUMBERS are identical, never by sorted-index alone. The pm SLUG encodes the
#     inequality directly (gteXltY / ltYf / gteX) and equals Kalshi (floor_strike, cap_strike) when aligned
#     (verified live 2026-06-09: SFO gte64lt65f <-> floor_strike=64 cap_strike=65). A NUMBER mismatch means the
#     index-zip paired non-identical buckets (gte70 vs floor 68) -> would grade off different thresholds.
def pm_bounds(slug):
    """pm bucket -> INCLUSIVE (lo, hi) integer degF (None = open tail). gteXltY is the 2deg bucket [X,Y]
    (verified live: pm tiles 64-65/66-67/... matching Kalshi's '64 to 65' etc); gteX = [X,inf); ltY = (-inf,Y-1]."""
    s = str(slug).lower()
    m = re.search(r"gte(\d+)lt(\d+)", s)
    if m: return (int(m.group(1)), int(m.group(2)))         # gteXltY -> [X, Y] (Kalshi 'X to Y' middle bucket)
    m = re.search(r"-lt(\d+)f", s)
    if m: return (None, int(m.group(1)) - 1)                # ltY -> T < Y -> (-inf, Y-1]
    m = re.search(r"gte(\d+)", s)
    if m: return (int(m.group(1)), None)                    # gteX -> [X, inf)
    return (None, None)
def kbounds(m):
    """Kalshi bucket -> INCLUSIVE (lo, hi) degF (= nyc_align_check's VERIFIED convention): a MIDDLE bucket (both
    strikes) is [floor, cap]; a LOW tail (cap only) is (-inf, cap-1]; a HIGH tail (floor only) is [floor+1, inf).
    Tails encode the boundary exclusively, middles inclusively - confirmed live 2026-06-09 against yes_sub_title."""
    fls, cap = m.get("floor_strike"), m.get("cap_strike")
    if fls is None and cap is not None: return (None, cap - 1)
    if cap is None and fls is not None: return (fls + 1, None)
    return (fls, cap)

# --- SPORTS game-instance binding (testable; pulled out of build_colisted_map so it has an offline self-test).
def _match_game(pl, kA, kB, join):
    """In one Kalshi event's {team_key: ticker} dict, find the two DISTINCT tickers for teams A and B."""
    ks = list(pl.keys())
    mA = next((s for s in ks if (s == kA if join == "abbrev" else smatch(s, kA))), None)
    mB = next((s for s in ks if (s == kB if join == "abbrev" else smatch(s, kB))), None)
    return (pl, mA, mB) if (mA and mB and mA != mB) else None
def pick_game(kbydate, kA, kB, join, date, slug_dated):
    """Bind a pm game to its Kalshi event. The pm SLUG date (ET) == the Kalshi ticker date, so an EXACT-date
    match is correct and kills the adjacent-series wrong-game mispair. The +/-1-day window is used ONLY as a
    fallback when the slug carried no date, and ONLY if the match is GLOBALLY UNIQUE (else a series ambiguity
    -> refuse). gameStartTime[:10] (UTC) must NOT be used for the join - it is a day off for late ET games."""
    if date in kbydate:
        m = next((g for pl in kbydate[date] if (g := _match_game(pl, kA, kB, join))), None)
        if m: return m
    if not slug_dated:
        near = [g for kdt in kbydate if kdt and dnear(kdt, date)
                for pl in kbydate[kdt] if (g := _match_game(pl, kA, kB, join))]
        if len(near) == 1: return near[0]
    return None

# --- ECON (macro) co-listing: pmus binary threshold/categorical <-> Kalshi cumulative "Above T" / categorical.
#     VERIFIED 2026-06-09 (scripts/verify_econ_settlement.py): same family/period/threshold + same govt source
#     (BLS/BEA/Fed). Only SAME-ORIENTATION pairs are co-listed: pmus ">=T" YES == Kalshi "Above T" YES, and Fed
#     categorical label==label (the pmus /book is YES-oriented regardless of the outcomes-array order, verified
#     pm book mid ~ Kalshi YES mid). SKIPPED+flagged: pmus "<=T" tails (pmus-YES = Kalshi-NO, opposite) and
#     "exactly X%" POINT buckets (no cumulative Kalshi twin). Residual: a print EXACTLY on T resolves >= vs >
#     oppositely (narrow, like the weather downward-correction).
ECON = {"cpic": ("KXCPIYOY", "cpi"), "urc": ("KXU3", "u3"), "nfpc": ("KXPAYROLLS", "nfp"),
        "gdpc": ("KXGDP", "gdp"), "rdc": ("KXFEDDECISION", "fed")}
_EMON = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}
_FEDLBL = {"maintains": "fed maintains rate", "hike25bps": "hike 25bps", "hikegt25bps": "hike >25bps",
           "cut25bps": "cut 25bps", "cutgt25bps": "cut >25bps"}
def _enum(tok):
    t = tok.lower().replace("pct", "").strip(); mult = 1
    if t.endswith("k"): mult = 1000; t = t[:-1]
    t = t.replace("pt", ".")
    try: return float(t) * mult
    except ValueError: return None
def econ_parse(slug):
    """pmus econ slug -> {fam, period(Kalshi token), thr, ineq, label} or None. ineq in '>=','<=','==','cat'."""
    s = str(slug).lower(); pre = s.split("-")[0]
    if pre not in ECON: return None
    if pre == "rdc":
        m = re.search(r"-(maintains|cut25bps|cutgt25bps|hike25bps|hikegt25bps)$", s)
        dm = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
        return {"fam": pre, "period": (f"{dm.group(1)[2:]}{MON[int(dm.group(2))-1]}" if dm else None),
                "thr": None, "ineq": "cat", "label": (m.group(1) if m else None)}
    tail = re.search(r"-(lte|gte)([0-9pt]+)pct$", s) or re.search(r"-(atl|atm)([0-9ptk]+)$", s)
    if tail:
        ineq = "<=" if tail.group(1) == "lte" else ">="; thr = _enum(tail.group(2))
    else:
        pt = re.search(r"-([0-9pt]+)pct$", s)
        if not pt: return None
        ineq = "=="; thr = _enum(pt.group(1))
    if pre == "cpic":
        mm = re.search(r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*?(\d{4})yoy", s)
        per = f"{mm.group(2)[2:]}{MON[_EMON[mm.group(1)[:3]]-1]}" if mm else None
    elif pre == "gdpc":
        dm = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
        per = f"{dm.group(1)[2:]}{MON[int(dm.group(2))-1]}{dm.group(3)}" if dm else None
    else:
        mm = re.search(r"-(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*-", s); ym = re.search(r"(\d{4})-\d{2}-\d{2}", s)
        per = f"{ym.group(1)[2:]}{MON[_EMON[mm.group(1)[:3]]-1]}" if (mm and ym) else None
    return {"fam": pre, "period": per, "thr": thr, "ineq": ineq, "label": None}
def econ_colisted(allm):
    """Full econ discovery -> (entries, flags). Only SAME-orientation pairs (>=threshold + Fed categorical)."""
    macro = [m for m in allm if m.get("category") == "macro"]
    bypre = collections.defaultdict(list)
    for m in macro: bypre[str(m.get("slug", "")).split("-")[0]].append(m)
    out, flags = [], collections.Counter()
    for pre, (kser, fam) in ECON.items():
        if not bypre.get(pre): continue
        kd = get(f"{KAL}?series_ticker={kser}&limit=400"); time.sleep(0.25)
        kby, klab = collections.defaultdict(dict), collections.defaultdict(dict)
        for m in kd.get("markets", []):
            tk = str(m.get("ticker", "")); pm_ = re.search(r"-(\d{2}[A-Z]{3}\d{0,2})-", tk)
            per = pm_.group(1) if pm_ else None
            kby[per][m.get("floor_strike")] = tk; klab[per][str(m.get("yes_sub_title", "")).lower()] = tk
        for x in bypre[pre]:
            p = econ_parse(x.get("slug"))
            if not p: continue
            if p["ineq"] == "cat":
                tk = klab.get(p["period"], {}).get(_FEDLBL.get(p["label"]))
                if tk: out.append({"cat": "econ", "family": fam, "period": p["period"],
                                   "slug": str(x.get("slug")), "kalshi": tk, "outcome": p["label"]})
                else: flags["fed_nomatch"] += 1
            elif p["ineq"] == ">=":
                tk = kby.get(p["period"], {}).get(p["thr"])
                if tk: out.append({"cat": "econ", "family": fam, "period": p["period"], "thr": p["thr"],
                                   "slug": str(x.get("slug")), "kalshi": tk})
                else: flags["ge_nomatch"] += 1
            elif p["ineq"] == "<=": flags["le_skip_OPPOSITE_orientation"] += 1   # pmus-YES = Kalshi-NO (not paired)
            else: flags["point_bucket_skip"] += 1                               # no cumulative Kalshi twin
    return out, dict(flags)

def pm_catalog():
    allm, off = [], 0
    while True:
        d = get(f"{PM}?closed=false&limit=500&offset={off}")
        pg = d.get("markets", []); allm += pg
        if len(pg) < 500 or len(allm) > 12000: break
        off += 500
    return allm

def build_colisted_map():
    """Full discovery -> ({weather:[...], sports:[...]}, coverage_report). No books fetched (fast)."""
    allm = pm_catalog()
    weather, sports, bucket_misaligned = [], [], []   # bucket_misaligned: non-identical degF ranges, NOT paired

    # ---- WEATHER (pm slug <-> kalshi ticker, 1:1 per bucket) ----
    clim = [m for m in allm if m.get("category") == "climate"]
    pm_cities = {wcity(m.get("slug")) for m in clim if wcity(m.get("slug"))}
    for city, kser in WX.items():
        pmc = [m for m in clim if wcity(m.get("slug")) == city]
        if not pmc: continue
        bydate = collections.defaultdict(list)
        for m in pmc:
            dm = re.search(r"(\d{4}-\d{2}-\d{2})", str(m.get("slug"))); bydate[dm.group(1) if dm else "?"].append(m)
        kd = get(f"{KAL}?series_ticker={kser}&limit=1000"); time.sleep(0.25)
        kby = collections.defaultdict(list)
        for m in kd.get("markets", []):
            dm = re.search(r"-(\d{2}[A-Z]{3}\d{2})", str(m.get("ticker")))
            if dm: kby[ktok_iso(dm.group(1))].append(m)
        for date in sorted(set(bydate) & set(kby)):
            pm = sorted(bydate[date], key=lambda m: pm_lo(str(m.get("slug")).lower()))
            kb = sorted(kby[date], key=lambda m: (m.get("floor_strike") if m.get("floor_strike") is not None else -999))
            if len(pm) != len(kb):                   # bucket COUNT differs -> index-zip is unsafe; flag loudly
                bucket_misaligned.append({"city": city, "date": date, "count": (len(pm), len(kb))})
            for i in range(min(len(pm), len(kb))):
                p_b, k_b = pm_bounds(pm[i].get("slug")), kbounds(kb[i])
                if p_b != k_b:                       # NON-identical (floor,cap) numbers -> different thresholds;
                    bucket_misaligned.append({"city": city, "date": date, "pm": p_b, "kalshi": k_b,
                                              "slug": str(pm[i].get("slug"))})   # do NOT pair (inv #1/#2)
                    continue
                weather.append({"cat": "weather", "city": city, "date": date,
                                "slug": str(pm[i].get("slug")), "kalshi": kb[i].get("ticker"),
                                "bucket": kb[i].get("yes_sub_title"), "bounds": k_b})

    # ---- SPORTS moneyline (pm game slug <-> TWO kalshi tickers, one per team) ----
    pmg = collections.defaultdict(list)
    for x in allm:
        if x.get("category") != "sports" or x.get("marketType") != "moneyline" or not x.get("gameStartTime"): continue
        pmg[pmlg(x.get("slug"))].append(x)
    pm_leagues = set(pmg)
    for L, (series, join) in LEAGUES.items():
        if not pmg.get(L): continue
        kd = get(f"{KAL}?series_ticker={series}&status=open&limit=1000"); time.sleep(0.3)
        byev, evd = collections.defaultdict(dict), {}
        for m in kd.get("markets", []):
            ev = m.get("event_ticker"); tk = str(m.get("ticker", ""))
            key = tk.split("-")[-1].lower() if join == "abbrev" else surname(m.get("yes_sub_title"))
            dm = re.search(r"-(\d{2}[A-Z]{3}\d{2})", tk); evd[ev] = ktok_iso(dm.group(1)) if dm else None
            if key: byev[ev][key] = tk
        kbydate = collections.defaultdict(list)
        for ev, pl in byev.items(): kbydate[evd.get(ev)].append(pl)
        for x in pmg[L]:
            sides = [s for s in (x.get("marketSides") or []) if (s.get("team") or {}).get("name")]
            if len(sides) < 2: continue
            lo = next((s for s in sides if s.get("long")), sides[0]); ot = next((s for s in sides if s is not lo), sides[1])
            if join == "abbrev":
                kA = (lo.get("team") or {}).get("abbreviation", "").lower(); kB = (ot.get("team") or {}).get("abbreviation", "").lower()
            else:
                kA = surname((lo.get("team") or {}).get("name")); kB = surname((ot.get("team") or {}).get("name"))
            sm = re.search(r"(\d{4}-\d{2}-\d{2})", str(x.get("slug")))   # pm slug ET date == Kalshi ticker date (exact join)
            date = sm.group(1) if sm else str(x.get("gameStartTime"))[:10]
            found = pick_game(kbydate, kA, kB, join, date, slug_dated=sm is not None)
            if not found: continue
            pl, mA, mB = found
            # void_clean=False for ALL leagues: settlement is identical only for a game that COMPLETES on schedule;
            # the postpone/void tail DIVERGES (MLB 2d-vs-2wk reschedule window etc) -> the bot must not hold a pair
            # through a postponement / size into an un-vetted void path. See research/sports-settlement-verification.md.
            sports.append({"cat": "sports", "league": L, "date": date, "slug": str(x.get("slug")),
                           "kalshi_a": pl[mA], "kalshi_b": pl[mB], "void_clean": False,
                           "teamA": (lo.get("team") or {}).get("name"), "teamB": (ot.get("team") or {}).get("name")})

    # ---- ECON (macro): pmus threshold/categorical <-> Kalshi cumulative/categorical, SAME-orientation only ----
    econ, econ_flags = econ_colisted(allm)
    pm_macro_fams = {str(m.get("slug", "")).split("-")[0] for m in allm if m.get("category") == "macro"}

    report = {
        "weather_cities_mapped": sorted(c for c in pm_cities if c in WX),
        "weather_cities_UNMAPPED": sorted(c for c in pm_cities if c not in WX),
        "sports_leagues_mapped": sorted(l for l in pm_leagues if l in LEAGUES),
        "sports_leagues_UNMAPPED": sorted(l for l in pm_leagues if l not in LEAGUES),
        "weather_bucket_MISALIGNED": bucket_misaligned,   # non-identical degF ranges -> NOT paired (settlement-identity)
        "econ_families_mapped": sorted(f for f in pm_macro_fams if f in ECON),
        "econ_families_UNMAPPED": sorted(f for f in pm_macro_fams if f not in ECON),
        "econ_SKIPPED": econ_flags,                       # <=tails (opposite orient) + point-buckets + no-match
        "counts": {"weather_pairs": len(weather), "sports_pairs": len(sports), "econ_pairs": len(econ)},
    }
    return {"weather": weather, "sports": sports, "econ": econ}, report

def weather_monitor_map(colisted):
    """The 1:1 {slug: ticker} dict the current monitor consumes (weather subset; sports needs the
    2-outcome tracker). Plug straight into bot/monitor.py run_live(market_map=...)."""
    return {e["slug"]: e["kalshi"] for e in colisted["weather"]}


# ============================================================================================
# SELF-TEST  (no network) - the identity-critical join logic that enforces invariant #2.
# ============================================================================================
def _selftest():
    print("colisted_map self-test (offline)")
    # --- pure helpers (accent-strip built with chr() so the source stays pure-ASCII) ---
    assert surname("Jos" + chr(0xe9) + " Ram" + chr(0xed) + "rez") == "ramirez"        # NFKD accent strip
    assert surname("Felix Auger-Aliassime") == "auger-aliassime"                       # whitespace split keeps hyphen
    assert ktok_iso("26JUN08") == "2026-06-08"                                         # ticker token is YY MMM DD
    assert wcity("tc-temp-laxhigh-2026-06-09-gte73") == "lax" and pmlg("aec-mlb-lad-pit-2026-06-09") == "mlb"
    assert dnear("2026-06-08", "2026-06-09") and not dnear("2026-06-08", "2026-06-10")
    assert pm_lo("...-gte73") == 73 and pm_lo("...-lt66f") == -34   # gte -> N ; lt -> N-100 (sort sentinel)
    # --- smatch: exact + <=1-char prefix OK; distinct-player prefixes REJECTED (L1) ---
    assert smatch("aliassime", "aliassime") and smatch("johnson", "johnsen") is False  # 2-char tail diff (non-prefix)
    assert smatch("ramirez", "ramire")                                                  # 1-char truncation OK
    for a, b in [("martin", "martinez"), ("williams", "williamson"), ("mann", "mannarino"), ("koval", "kovalenko")]:
        assert not smatch(a, b), f"smatch must reject distinct players {a}~{b}"
    # --- C4: weather bucket boundary equality, both canonicalized to inclusive [lo,hi] (live-verified SFO map) ---
    assert pm_bounds("tc-temp-sfohigh-2026-06-09-gte64lt65f") == kbounds({"floor_strike": 64, "cap_strike": 65}) == (64, 65)  # middle '64 to 65'
    assert pm_bounds("tc-temp-sfohigh-2026-06-09-lt64f") == kbounds({"floor_strike": None, "cap_strike": 64}) == (None, 63)   # low tail '63 or below'
    assert pm_bounds("tc-temp-sfohigh-2026-06-09-gte72f") == kbounds({"floor_strike": 71, "cap_strike": None}) == (72, None)  # high tail '72 or above'
    assert pm_bounds("x-gte70lt72f") != kbounds({"floor_strike": 68, "cap_strike": 70})   # shifted -> FLAGGED (would mispair)
    # --- C2: sports game binding pins the EXACT date, never an adjacent-series game ---
    d8 = {"phi": "K-PHI-08", "tor": "K-TOR-08"}; d9 = {"phi": "K-PHI-09", "tor": "K-TOR-09"}
    kbydate = {"2026-06-08": [d8], "2026-06-09": [d9]}                 # same matchup on consecutive days (a series)
    g8 = pick_game(kbydate, "phi", "tor", "abbrev", "2026-06-08", slug_dated=True)
    g9 = pick_game(kbydate, "phi", "tor", "abbrev", "2026-06-09", slug_dated=True)
    assert g8 and g8[0] is d8 and g9 and g9[0] is d9, "each pm game must bind to its OWN-date Kalshi event"
    # no slug date -> +/-1 fallback: a series ambiguity (date absent, BOTH neighbors match) must REFUSE, not guess
    d7 = {"phi": "K-PHI-07", "tor": "K-TOR-07"}
    assert pick_game({"2026-06-07": [d7], "2026-06-09": [d9]}, "phi", "tor", "abbrev", "2026-06-08", slug_dated=False) is None
    assert pick_game({"2026-06-09": [d9]}, "phi", "tor", "abbrev", "2026-06-08", slug_dated=False)[0] is d9  # unique +/-1 OK
    # surname join still resolves two distinct players to two distinct tickers
    pl = {"djokovic": "K-DJO", "alcaraz": "K-ALC"}
    assert pick_game({"2026-06-09": [pl]}, "djokovic", "alcaraz", "surname", "2026-06-09", slug_dated=True)[1:] == ("djokovic", "alcaraz")
    # --- ECON: pmus econ slug -> (family, period, threshold, inequality); only >= + Fed-cat are co-listed ---
    assert econ_parse("gdpc-us-saa-q2-2026-07-30-atl2pt0") == {"fam": "gdpc", "period": "26JUL30", "thr": 2.0, "ineq": ">=", "label": None}
    assert econ_parse("cpic-uscpi-may2026yoy-2026-06-10-lte3pt7pct") == {"fam": "cpic", "period": "26MAY", "thr": 3.7, "ineq": "<=", "label": None}
    assert econ_parse("cpic-uscpi-may2026yoy-2026-06-10-3pt8pct")["ineq"] == "=="   # point bucket -> not co-listed
    nfp = econ_parse("nfpc-uschange-gte-june-2026-07-02-atl250k")
    assert nfp["thr"] == 250000.0 and nfp["period"] == "26JUN" and nfp["ineq"] == ">="
    fed = econ_parse("rdc-usfed-fomc-2026-06-17-maintains")
    assert fed["ineq"] == "cat" and fed["label"] == "maintains" and fed["period"] == "26JUN"
    assert econ_parse("aec-mlb-x-y-2026-06-10") is None and econ_parse("tc-temp-laxhigh-2026-06-09-gte73") is None
    print("OK - helpers, smatch L1-collision rejection, C4 bucket boundary, C2 exact-date game binding, econ parse")


if __name__ == "__main__":
    if "--live" not in sys.argv:
        _selftest(); sys.exit(0)
    print("full co-listed discovery (read-only)...\n")
    colisted, rep = build_colisted_map()
    print(f"weather pairs: {rep['counts']['weather_pairs']}   sports pairs: {rep['counts']['sports_pairs']}   econ pairs: {rep['counts']['econ_pairs']}\n")
    print(f"weather cities mapped:   {rep['weather_cities_mapped']}")
    print(f"sports leagues mapped:   {rep['sports_leagues_mapped']}")
    print(f"econ families mapped:    {rep['econ_families_mapped']}   (skipped: {rep['econ_SKIPPED']})")
    if colisted["econ"]:
        print("  econ pairs (sample):")
        for e in colisted["econ"][:8]:
            print(f"    {e.get('family')} {e.get('period')} {e.get('thr', e.get('outcome'))}  {e['slug']} <-> {e['kalshi']}")
    um_c, um_l = rep["weather_cities_UNMAPPED"], rep["sports_leagues_UNMAPPED"]
    if um_c or um_l:
        print("\n!!! COVERAGE GAP - polymarket.us lists these, but WX/LEAGUES does NOT map them (would be MISSED):")
        if um_c: print(f"    weather cities : {um_c}")
        if um_l: print(f"    sports leagues : {um_l}  (extend LEAGUES in colisted_map.py + scan_all.py to cover)")
    else:
        print("\nOK - every polymarket.us climate city + sports league is mapped (no coverage gap).")
    if rep["weather_bucket_MISALIGNED"]:
        print(f"\n!!! BUCKET MISALIGNMENT - {len(rep['weather_bucket_MISALIGNED'])} weather buckets NOT paired "
              f"(non-identical degF ranges / count mismatch -> settlement-identity guard):")
        for b in rep["weather_bucket_MISALIGNED"][:8]: print(f"    {b}")
    print(f"\nmonitor-ready weather map (1:1 slug->ticker): {len(weather_monitor_map(colisted))} markets")
