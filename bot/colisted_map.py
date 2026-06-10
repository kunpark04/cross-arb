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

scan_all.py IMPORTS this module's matchers (single source of truth; the coverage audit below is the
backstop that catches config drift). Matching is identity-based (city+date+IDENTICAL bucket bounds;
league+date+abbrev|surname, one Kalshi event bound at most once; econ family+period+GRID-STEP twin) -
the no-false-positive invariant (lesson L1). ECON (CPI/U-3/NFP/GDP/Fed) covers the cleanest US-legal
subset; only SAME-orientation pairs whose IDENTICAL Kalshi twin exists are mapped (decision 0013 -
pmus ">=T" is inclusive, Kalshi "Above T" is STRICT, so the twin is floor_strike = T - print-grid-step;
pairing floor==T is off by one bucket and manufactures a phantom "edge" = the market-priced P(print==T)).
No order books are fetched here (fast).
"""
import os, sys, re, json, time, urllib.request, urllib.error, collections, unicodedata
try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: pass

PM = "https://gateway.polymarket.us/v1/markets"
KAL = "https://api.elections.kalshi.com/trade-api/v2/markets"

# --- config (scan_all.py imports these; coverage audit flags drift) ---
WX = {"sfo": "KXHIGHTSFO", "lax": "KXHIGHLAX", "nyc": "KXHIGHNY", "mia": "KXHIGHMIA", "mdw": "KXHIGHCHI"}
LEAGUES = {"mlb": ("KXMLBGAME", "abbrev"), "wnba": ("KXWNBAGAME", "abbrev"), "nba": ("KXNBAGAME", "abbrev"),
           "nhl": ("KXNHLGAME", "abbrev"), "cs2": ("KXCS2GAME", "abbrev"), "lol": ("KXLOLGAME", "abbrev"),
           "valorant": ("KXVALORANTGAME", "abbrev"), "cod": ("KXCODGAME", "abbrev"),
           "atp": ("KXATPMATCH", "surname"),
           "wta": ("KXWTAMATCH", "surname"), "itfm": ("KXITFMATCH", "surname"),
           "itfw": ("KXITFWMATCH", "surname"), "ufc": ("KXUFCFIGHT", "surname")}

UA = {"User-Agent": "cross-arb/1.0", "Accept": "application/json"}
MON = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
PM_CATALOG_CAP = 12000   # safety cap on catalog pagination; hitting it = TRUNCATED coverage (warn loudly)

def get(url, tries=4, errs=None):
    """GET -> parsed JSON. Retries 429 + transient errors (5xx, timeouts) with backoff. On final failure
    returns {"_err": ...} AND appends to `errs` when given — build_colisted_map reports those as
    fetch_errors so the monitor can treat the discovery pass as DEGRADED (skip pruning) instead of
    tearing down still-live markets that merely failed to fetch (review H4)."""
    err = None
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            err = e.code
            if e.code not in (429, 500, 502, 503, 504) or i == tries - 1: break
        except Exception as e:
            err = str(e)[:60]
            if i == tries - 1: break
        time.sleep(1.5 * (i + 1))
    if errs is not None:
        errs.append({"url": url.split("?")[0][-60:], "err": err})
    return {"_err": err}

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

def pair_weather_date(pm_list, k_list):
    """Pair one date's pm buckets to Kalshi buckets by IDENTICAL canonical inclusive (lo,hi) BOUNDS — a
    dict join, never a sorted-index zip (an offset listing under index-zip mispairs or kills the whole
    date even when every correct twin exists). Returns (pairs, flags): pairs = [(pm_market, k_market,
    bounds)]; flags = pm buckets with no bounds-identical Kalshi twin (NOT paired — settlement identity)."""
    kby_b = {}
    for k in k_list:
        kby_b.setdefault(kbounds(k), k)            # first wins; duplicate Kalshi bounds shouldn't exist
    pairs, flags = [], []
    for p in pm_list:
        b = pm_bounds(p.get("slug"))
        k = kby_b.get(b)
        if k is None or b == (None, None):
            flags.append({"pm": b, "slug": str(p.get("slug"))})
        else:
            pairs.append((p, k, b))
    return pairs, flags

# --- SPORTS game-instance binding (testable; pulled out of build_colisted_map so it has an offline self-test).
def _match_game(pl, kA, kB, join):
    """In one Kalshi event's {team_key: ticker} dict, find the two DISTINCT tickers for teams A and B."""
    ks = list(pl.keys())
    mA = next((s for s in ks if (s == kA if join == "abbrev" else smatch(s, kA))), None)
    mB = next((s for s in ks if (s == kB if join == "abbrev" else smatch(s, kB))), None)
    return (pl, mA, mB) if (mA and mB and mA != mB) else None
def pick_game(kbydate, kA, kB, join, date, slug_dated, used=None):
    """Bind a pm game to its Kalshi event. The pm SLUG date (ET) == the Kalshi ticker date, so an EXACT-date
    match is correct and kills the adjacent-series wrong-game mispair. The +/-1-day window is used ONLY as a
    fallback when the slug carried no date, and ONLY if the match is GLOBALLY UNIQUE (else a series ambiguity
    -> refuse). gameStartTime[:10] (UTC) must NOT be used for the join - it is a day off for late ET games.
    `used` (optional set, shared across one league's binding pass) tracks ALREADY-BOUND Kalshi events by
    object id — the DOUBLEHEADER guard: same teams + same date = two Kalshi events; without it both pm
    games bind the FIRST event and one tracks the wrong game's books (an L1-class false pair)."""
    def _ok(pl): return used is None or id(pl) not in used
    def _take(g):
        if g is not None and used is not None: used.add(id(g[0]))
        return g
    if date in kbydate:
        m = next((g for pl in kbydate[date] if _ok(pl) and (g := _match_game(pl, kA, kB, join))), None)
        if m: return _take(m)
    if not slug_dated:
        near = [g for kdt in kbydate if kdt and dnear(kdt, date)
                for pl in kbydate[kdt] if _ok(pl) and (g := _match_game(pl, kA, kB, join))]
        if len(near) == 1: return _take(near[0])
    return None

# --- ECON (macro) co-listing: pmus binary threshold/categorical <-> Kalshi cumulative / categorical.
#     SETTLEMENT IDENTITY (decision 0013, verified live 2026-06-10 against both venues' rules text):
#       pmus  ">= T"   is INCLUSIVE  ("...is at least 4.6%...")
#       Kalshi "Above T" is STRICT > (strike_type="greater": "...is above 4.6%...")
#     On the print grid (BLS/BEA report one decimal for U-3/CPI/GDP; payrolls in 1000s), ">= T" is
#     IDENTICAL to "> T-step" — so the settlement-identical Kalshi twin of a pmus ">= T" market has
#     floor_strike = T - step. Pairing floor == T (the pre-0013 join) was OFF BY ONE BUCKET: a print
#     landing exactly on T settles the venues OPPOSITELY, and the cross-venue gap on such a pair is the
#     market-priced P(print == T) (~17c observed on an at-the-money U-3 pair), i.e. a PHANTOM "edge"
#     that loses both legs when the modal print lands. No listed twin -> NOT co-listed (flagged).
#     Fed is a 5-way categorical (label==label, no inequality) — unchanged. The pmus /book is
#     YES-oriented regardless of the outcomes-array order (verified pm book mid ~ Kalshi YES mid).
#     SKIPPED+flagged: pmus "<=T" tails (pmus-YES = Kalshi-NO, opposite orientation), "exactly X%"
#     POINT buckets (no cumulative twin), and ">=T" with no listed T-step twin.
ECON = {"cpic": ("KXCPIYOY", "cpi", 0.1), "urc": ("KXU3", "u3", 0.1), "nfpc": ("KXPAYROLLS", "nfp", 1000),
        "gdpc": ("KXGDP", "gdp", 0.1), "rdc": ("KXFEDDECISION", "fed", None)}
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
        # urc/nfpc: data MONTH is a name token, the YEAR comes from the RELEASE date — December data
        # releases in January of the NEXT year, so when data-month > release-month the data year is
        # release-year - 1 (else the Dec pair would silently never match Kalshi's 26DEC ticker).
        mm = re.search(r"-(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*-", s)
        ym = re.search(r"(\d{4})-(\d{2})-\d{2}", s)
        if mm and ym:
            dmon = _EMON[mm.group(1)[:3]]
            yr = int(ym.group(1)) - (1 if dmon > int(ym.group(2)) else 0)
            per = f"{yr % 100:02d}{MON[dmon - 1]}"
        else:
            per = None
    return {"fam": pre, "period": per, "thr": thr, "ineq": ineq, "label": None}
def econ_twin(thr, step):
    """Kalshi floor_strike of the settlement-IDENTICAL twin of a pmus '>= thr' market: on the print grid,
    '>= T' == '> T-step', and Kalshi 'Above F' is strict, so the twin has floor F = T - step (0013)."""
    return round(thr - step, 6)

def econ_colisted(allm, errs=None):
    """Full econ discovery -> (entries, flags). Only SAME-orientation pairs whose settlement-IDENTICAL
    Kalshi twin is listed: pmus '>= T' <-> Kalshi 'Above T-step' (econ_twin), + Fed categorical."""
    macro = [m for m in allm if m.get("category") == "macro"]
    bypre = collections.defaultdict(list)
    for m in macro: bypre[str(m.get("slug", "")).split("-")[0]].append(m)
    out, flags = [], collections.Counter()
    for pre, (kser, fam, step) in ECON.items():
        if not bypre.get(pre): continue
        kd = get(f"{KAL}?series_ticker={kser}&limit=400", errs=errs); time.sleep(0.25)
        kby, klab = collections.defaultdict(dict), collections.defaultdict(dict)
        for m in kd.get("markets", []):
            tk = str(m.get("ticker", "")); pm_ = re.search(r"-(\d{2}[A-Z]{3}\d{0,2})-", tk)
            per = pm_.group(1) if pm_ else None
            if m.get("floor_strike") is not None:
                kby[per][round(float(m["floor_strike"]), 6)] = tk
            klab[per][str(m.get("yes_sub_title", "")).lower()] = tk
        for x in bypre[pre]:
            p = econ_parse(x.get("slug"))
            if not p: continue
            if p["ineq"] == "cat":
                tk = klab.get(p["period"], {}).get(_FEDLBL.get(p["label"]))
                if tk: out.append({"cat": "econ", "family": fam, "period": p["period"],
                                   "slug": str(x.get("slug")), "kalshi": tk, "outcome": p["label"]})
                else: flags["fed_nomatch"] += 1
            elif p["ineq"] == ">=":
                twin = econ_twin(p["thr"], step)               # identical twin, NOT floor == T (0013)
                tk = kby.get(p["period"], {}).get(twin)
                if tk: out.append({"cat": "econ", "family": fam, "period": p["period"], "thr": p["thr"],
                                   "k_strike": twin, "slug": str(x.get("slug")), "kalshi": tk})
                else: flags["ge_no_identical_twin"] += 1       # twin not listed -> NOT co-listed
            elif p["ineq"] == "<=": flags["le_skip_OPPOSITE_orientation"] += 1   # pmus-YES = Kalshi-NO (not paired)
            else: flags["point_bucket_skip"] += 1                               # no cumulative Kalshi twin
    return out, dict(flags)

def pm_catalog(errs=None):
    allm, off = [], 0
    while True:
        d = get(f"{PM}?closed=false&limit=500&offset={off}", errs=errs)
        pg = d.get("markets", []); allm += pg
        if len(pg) < 500: break
        if len(allm) > PM_CATALOG_CAP:               # safety cap -> TRUNCATED catalog = silent coverage loss
            print(f"WARNING: pm_catalog hit the {PM_CATALOG_CAP}-row cap — catalog TRUNCATED "
                  f"(raise PM_CATALOG_CAP; coverage is incomplete)", file=sys.stderr)
            break
        off += 500
    return allm

def build_colisted_map():
    """Full discovery -> ({weather:[...], sports:[...], econ:[...]}, coverage_report). No books fetched
    (fast). report['fetch_errors'] lists every failed venue pull — a NON-EMPTY list means this pass is
    DEGRADED (missing markets are fetch failures, not settlements; the monitor must not prune on it)."""
    errs = []
    allm = pm_catalog(errs=errs)
    weather, sports, bucket_misaligned = [], [], []   # bucket_misaligned: pm buckets w/o an identical-bounds twin

    # ---- WEATHER (pm slug <-> kalshi ticker, 1:1 per bucket, joined on IDENTICAL canonical bounds) ----
    clim = [m for m in allm if m.get("category") == "climate"]
    pm_cities = {wcity(m.get("slug")) for m in clim if wcity(m.get("slug"))}
    for city, kser in WX.items():
        pmc = [m for m in clim if wcity(m.get("slug")) == city]
        if not pmc: continue
        bydate = collections.defaultdict(list)
        for m in pmc:
            dm = re.search(r"(\d{4}-\d{2}-\d{2})", str(m.get("slug"))); bydate[dm.group(1) if dm else "?"].append(m)
        kd = get(f"{KAL}?series_ticker={kser}&limit=1000", errs=errs); time.sleep(0.25)
        kby = collections.defaultdict(list)
        for m in kd.get("markets", []):
            dm = re.search(r"-(\d{2}[A-Z]{3}\d{2})", str(m.get("ticker")))
            if dm: kby[ktok_iso(dm.group(1))].append(m)
        for date in sorted(set(bydate) & set(kby)):
            pairs, flags = pair_weather_date(bydate[date], kby[date])   # bounds-dict join, never index-zip
            for f in flags:
                bucket_misaligned.append({"city": city, "date": date, **f})
            for p, k, b in pairs:
                weather.append({"cat": "weather", "city": city, "date": date,
                                "slug": str(p.get("slug")), "kalshi": k.get("ticker"),
                                "bucket": k.get("yes_sub_title"), "bounds": b})

    # ---- SPORTS moneyline (pm game slug <-> TWO kalshi tickers, one per team) ----
    pmg = collections.defaultdict(list)
    for x in allm:
        if x.get("category") != "sports" or x.get("marketType") != "moneyline" or not x.get("gameStartTime"): continue
        pmg[pmlg(x.get("slug"))].append(x)
    pm_leagues = set(pmg)
    for L, (series, join) in LEAGUES.items():
        if not pmg.get(L): continue
        kd = get(f"{KAL}?series_ticker={series}&status=open&limit=1000", errs=errs); time.sleep(0.3)
        byev, evd = collections.defaultdict(dict), {}
        for m in kd.get("markets", []):
            ev = m.get("event_ticker"); tk = str(m.get("ticker", ""))
            key = tk.split("-")[-1].lower() if join == "abbrev" else surname(m.get("yes_sub_title"))
            dm = re.search(r"-(\d{2}[A-Z]{3}\d{2})", tk); evd[ev] = ktok_iso(dm.group(1)) if dm else None
            if key: byev[ev][key] = tk
        kbydate = collections.defaultdict(list)
        for ev, pl in byev.items(): kbydate[evd.get(ev)].append(pl)
        used = set()                                  # one Kalshi event binds AT MOST one pm game (doubleheaders)
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
            found = pick_game(kbydate, kA, kB, join, date, slug_dated=sm is not None, used=used)
            if not found: continue
            pl, mA, mB = found
            # void_clean=False for ALL leagues: settlement is identical only for a game that COMPLETES on schedule;
            # the postpone/void tail DIVERGES (MLB 2d-vs-2wk reschedule window etc) -> the bot must not hold a pair
            # through a postponement / size into an un-vetted void path. See research/sports-settlement-verification.md.
            sports.append({"cat": "sports", "league": L, "date": date, "slug": str(x.get("slug")),
                           "kalshi_a": pl[mA], "kalshi_b": pl[mB], "void_clean": False,
                           "teamA": (lo.get("team") or {}).get("name"), "teamB": (ot.get("team") or {}).get("name")})

    # ---- ECON (macro): pmus '>=T' <-> the IDENTICAL Kalshi 'Above T-step' twin + Fed categorical (0013) ----
    econ, econ_flags = econ_colisted(allm, errs=errs)
    pm_macro_fams = {str(m.get("slug", "")).split("-")[0] for m in allm if m.get("category") == "macro"}

    report = {
        "weather_cities_mapped": sorted(c for c in pm_cities if c in WX),
        "weather_cities_UNMAPPED": sorted(c for c in pm_cities if c not in WX),
        "sports_leagues_mapped": sorted(l for l in pm_leagues if l in LEAGUES),
        "sports_leagues_UNMAPPED": sorted(l for l in pm_leagues if l not in LEAGUES),
        "weather_bucket_MISALIGNED": bucket_misaligned,   # pm buckets w/o an identical-bounds Kalshi twin (NOT paired)
        "econ_families_mapped": sorted(f for f in pm_macro_fams if f in ECON),
        "econ_families_UNMAPPED": sorted(f for f in pm_macro_fams if f not in ECON),
        "econ_SKIPPED": econ_flags,                       # <=tails (opposite orient) + point-buckets + no listed twin
        "fetch_errors": errs,                             # non-empty = DEGRADED discovery pass (do NOT prune on it)
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
    # pair_weather_date: BOUNDS-dict join, never index-zip — an OFFSET listing still finds the true twins
    pm_off = [{"slug": "tc-temp-x-2026-06-09-gte62lt63f"}, {"slug": "tc-temp-x-2026-06-09-gte64lt65f"}]
    k_off = [{"ticker": "K60", "floor_strike": 60, "cap_strike": 61},      # extra low bucket pm doesn't list
             {"ticker": "K62", "floor_strike": 62, "cap_strike": 63},
             {"ticker": "K64", "floor_strike": 64, "cap_strike": 65}]
    prs, flg = pair_weather_date(pm_off, k_off)
    assert [(p["slug"][-10:], k["ticker"]) for p, k, _ in prs] == [("gte62lt63f", "K62"), ("gte64lt65f", "K64")], prs
    assert flg == [], flg                                                  # offset != misalignment under a bounds join
    prs2, flg2 = pair_weather_date([{"slug": "tc-temp-x-2026-06-09-gte70lt71f"}], k_off)
    assert prs2 == [] and len(flg2) == 1, (prs2, flg2)                     # genuinely missing twin -> flagged, NOT paired
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
    # DOUBLEHEADER guard: same teams + same date = TWO Kalshi events; with a shared `used` set the two pm
    # games bind DISTINCT events (and a third attempt refuses) instead of both grabbing the first one (L1).
    g1 = {"sea": "K-SEA-G1", "bal": "K-BAL-G1"}; g2 = {"sea": "K-SEA-G2", "bal": "K-BAL-G2"}
    dh = {"2026-06-10": [g1, g2]}; used = set()
    b1 = pick_game(dh, "sea", "bal", "abbrev", "2026-06-10", slug_dated=True, used=used)
    b2 = pick_game(dh, "sea", "bal", "abbrev", "2026-06-10", slug_dated=True, used=used)
    b3 = pick_game(dh, "sea", "bal", "abbrev", "2026-06-10", slug_dated=True, used=used)
    assert b1[0] is g1 and b2[0] is g2 and b3 is None, "doubleheader must bind distinct events then refuse"
    # --- ECON: parse + the settlement-IDENTICAL twin join (0013: pmus '>=T' <-> Kalshi 'Above T-step') ---
    assert econ_parse("gdpc-us-saa-q2-2026-07-30-atl2pt0") == {"fam": "gdpc", "period": "26JUL30", "thr": 2.0, "ineq": ">=", "label": None}
    assert econ_parse("cpic-uscpi-may2026yoy-2026-06-10-lte3pt7pct") == {"fam": "cpic", "period": "26MAY", "thr": 3.7, "ineq": "<=", "label": None}
    assert econ_parse("cpic-uscpi-may2026yoy-2026-06-10-3pt8pct")["ineq"] == "=="   # point bucket -> not co-listed
    nfp = econ_parse("nfpc-uschange-gte-june-2026-07-02-atl250k")
    assert nfp["thr"] == 250000.0 and nfp["period"] == "26JUN" and nfp["ineq"] == ">="
    fed = econ_parse("rdc-usfed-fomc-2026-06-17-maintains")
    assert fed["ineq"] == "cat" and fed["label"] == "maintains" and fed["period"] == "26JUN"
    assert econ_parse("aec-mlb-x-y-2026-06-10") is None and econ_parse("tc-temp-laxhigh-2026-06-09-gte73") is None
    # year boundary: December data released the NEXT January belongs to the PRIOR year (26DEC, not 27DEC)
    assert econ_parse("urc-us-seasonadj-gte-december-2027-01-08-atl4pt4")["period"] == "26DEC"
    assert econ_parse("urc-us-seasonadj-gte-june-2026-07-02-atl4pt4")["period"] == "26JUN"   # normal case unchanged
    # the identical twin is floor = T - print-grid-step (NOT floor == T, which is off by one bucket):
    # pmus '>=4.4' == Kalshi 'Above 4.3' on the 0.1 grid; '>=250k' == 'Above 249k' on the 1000 grid.
    assert econ_twin(4.4, ECON["urc"][2]) == 4.3 and econ_twin(250000.0, ECON["nfpc"][2]) == 249000.0
    assert econ_twin(2.0, ECON["gdpc"][2]) == 1.9 and econ_twin(4.0, ECON["urc"][2]) == 3.9   # float-safe rounding
    print("OK - helpers, smatch L1-collision rejection, C4 bounds-dict join, C2 exact-date + doubleheader binding, econ twin/year parse")


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
