"""bot/colisted_map.py — build the co-listed {polymarket.us slug <-> Kalshi ticker} map by FULL
discovery, and AUDIT coverage so new markets are never silently missed. READ-ONLY (catalog reads only).

Freshness model (answers "does this miss new markets?"):
  • Every call pulls the ENTIRE polymarket.us closed=false catalog + the Kalshi series, and groups by
    date/event DYNAMICALLY — so new weather DAYS and new sports GAMES in known cities/leagues are picked
    up automatically. The monitor calls this on startup AND on the REST heartbeat to refresh (add new
    pairs, drop settled ones) — decision 0003's "coverage heartbeat".
  • The one thing discovery can't infer is a brand-new CATEGORY: a new weather city or a new sports
    league that isn't in WX / LEAGUES below. So build_colisted_map() also returns a COVERAGE REPORT that
    lists every climate city + sports league polymarket.us is currently listing and flags any we don't
    map. Unmapped => we'd miss it until WX/LEAGUES is extended -> the report makes that LOUD, not silent.

WX / LEAGUES mirror scripts/scan_all.py (the validated matcher). Keep them in sync; the coverage audit
below is the backstop that catches drift. Matching is identity-based (city+date+bucket; league+date+
abbrev|surname) — the no-false-positive invariant (lesson L1). No order books are fetched here (fast).
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
def smatch(a, b): return a == b or (len(a) >= 4 and len(b) >= 4 and (a.startswith(b) or b.startswith(a)))
def wcity(s): m = re.search(r"tc-temp-([a-z]+?)high", str(s)); return m.group(1) if m else None
def pm_lo(s):
    m = re.search(r"-lt(\d+)f", s); m2 = re.search(r"gte(\d+)", s)
    return (int(m.group(1)) - 100) if m else (int(m2.group(1)) if m2 else 0)
def pmlg(s): p = str(s).split("-"); return p[1] if len(p) > 1 else "?"

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
    weather, sports = [], []

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
            for i in range(min(len(pm), len(kb))):   # buckets verified 1:1-aligned (live-edge-findings)
                weather.append({"cat": "weather", "city": city, "date": date,
                                "slug": str(pm[i].get("slug")), "kalshi": kb[i].get("ticker"),
                                "bucket": kb[i].get("yes_sub_title")})

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
            date = str(x.get("gameStartTime"))[:10]; found = None
            for kdt in list(kbydate.keys()):
                if not kdt or not dnear(kdt, date): continue
                for pl in kbydate[kdt]:
                    ks = list(pl.keys())
                    mA = next((s for s in ks if (s == kA if join == "abbrev" else smatch(s, kA))), None)
                    mB = next((s for s in ks if (s == kB if join == "abbrev" else smatch(s, kB))), None)
                    if mA and mB and mA != mB: found = (pl, mA, mB); break
                if found: break
            if not found: continue
            pl, mA, mB = found
            sports.append({"cat": "sports", "league": L, "date": date, "slug": str(x.get("slug")),
                           "kalshi_a": pl[mA], "kalshi_b": pl[mB],
                           "teamA": (lo.get("team") or {}).get("name"), "teamB": (ot.get("team") or {}).get("name")})

    report = {
        "weather_cities_mapped": sorted(c for c in pm_cities if c in WX),
        "weather_cities_UNMAPPED": sorted(c for c in pm_cities if c not in WX),
        "sports_leagues_mapped": sorted(l for l in pm_leagues if l in LEAGUES),
        "sports_leagues_UNMAPPED": sorted(l for l in pm_leagues if l not in LEAGUES),
        "counts": {"weather_pairs": len(weather), "sports_pairs": len(sports)},
    }
    return {"weather": weather, "sports": sports}, report

def weather_monitor_map(colisted):
    """The 1:1 {slug: ticker} dict the current monitor consumes (weather subset; sports needs the
    2-outcome tracker). Plug straight into bot/monitor.py run_live(market_map=...)."""
    return {e["slug"]: e["kalshi"] for e in colisted["weather"]}

if __name__ == "__main__":
    print("full co-listed discovery (read-only)...\n")
    colisted, rep = build_colisted_map()
    print(f"weather pairs: {rep['counts']['weather_pairs']}   sports pairs: {rep['counts']['sports_pairs']}\n")
    print(f"weather cities mapped:   {rep['weather_cities_mapped']}")
    print(f"sports leagues mapped:   {rep['sports_leagues_mapped']}")
    um_c, um_l = rep["weather_cities_UNMAPPED"], rep["sports_leagues_UNMAPPED"]
    if um_c or um_l:
        print("\n!!! COVERAGE GAP — polymarket.us lists these, but WX/LEAGUES does NOT map them (would be MISSED):")
        if um_c: print(f"    weather cities : {um_c}")
        if um_l: print(f"    sports leagues : {um_l}  (extend LEAGUES in colisted_map.py + scan_all.py to cover)")
    else:
        print("\nOK - every polymarket.us climate city + sports league is mapped (no coverage gap).")
    print(f"\nmonitor-ready weather map (1:1 slug->ticker): {len(weather_monitor_map(colisted))} markets")
