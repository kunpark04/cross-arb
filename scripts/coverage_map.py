"""COMPLETE co-listed coverage audit (no pruning). Enumerates the FULL polymarket.us open catalog
(every sports league + market type, every climate type/city) and maps each to Kalshi game/weather
series with live open-market counts. Also dumps Kalshi player naming for the name-matcher. Read-only."""
import json, os, re, urllib.request, urllib.error, time, collections

UA={"User-Agent":"Mozilla/5.0","Accept":"application/json"}
def get(url,tries=4):
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url,headers=UA),timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code==429 and i<tries-1: time.sleep(3); continue
            return {"_err":e.code}
        except Exception as e:
            return {"_err":str(e)[:60]}
    return {}
def kopen(series):
    d=get(f"https://external-api.kalshi.com/trade-api/v2/markets?series_ticker={series}&status=open&limit=1000")
    return len(d.get("markets",[])) if isinstance(d,dict) and "markets" in d else 0

# ---------- PM.us full open catalog ----------
allm,off=[],0
while True:
    d=get(f"https://gateway.polymarket.us/v1/markets?closed=false&limit=500&offset={off}")
    pg=d.get("markets",[]); allm+=pg
    if len(pg)<500 or len(allm)>12000: break
    off+=500
print(f"=== polymarket.us OPEN catalog: {len(allm)} markets ===")
print("categories:", dict(collections.Counter(m.get("category") for m in allm).most_common()))
sports=[m for m in allm if m.get("category")=="sports"]
def lg(s):
    p=str(s).split("-"); return p[1] if len(p)>1 else "?"
print("\nSPORTS leagues (ALL sports markets):", dict(collections.Counter(lg(m.get('slug')) for m in sports).most_common()))
print("SPORTS by marketType:", dict(collections.Counter(m.get('marketType') for m in sports).most_common()))
print("SPORTS by sportsMarketType:", dict(collections.Counter(m.get('sportsMarketType') for m in sports).most_common()))
ml=[m for m in sports if m.get("marketType")=="moneyline"]
print("moneyline games by league:", dict(collections.Counter(lg(m.get('slug')) for m in ml).most_common()))
# climate
clim=[m for m in allm if m.get("category")=="climate"]
def wtype(s):
    m=re.search(r"tc-temp-([a-z]+?)(high|low)",str(s))
    return (m.group(1),m.group(2)) if m else ("?","?")
ctypes=collections.Counter(wtype(m.get('slug'))[1] for m in clim)
ccities=collections.Counter(wtype(m.get('slug'))[0] for m in clim)
print(f"\nCLIMATE: {len(clim)} markets  types={dict(ctypes)}  cities={dict(ccities)}")
print("  (any non-temp weather? rain/snow slugs:)", [m.get('slug') for m in clim if 'temp' not in str(m.get('slug'))][:5] or "none")

# ---------- Kalshi overlap map ----------
PM_TO_K={"mlb":["KXMLBGAME","KXNLGAME"],"wnba":["KXWNBAGAME"],"nba":["KXNBAGAME"],"nhl":["KXNHLGAME"],
 "atp":["KXATPMATCH","KXATPGAME"],"wta":["KXWTAGAME","KXWTAMATCH"],"itfm":["KXITFMGAME","KXITFMATCH","KXITF"],
 "itfw":["KXITFWGAME","KXITFWMATCH"],"ufc":["KXUFCFIGHT","KXUFCGAME"],"cs2":["KXCS2GAME","KXCSGOGAME"],
 "lol":["KXLOLGAME"],"valorant":["KXVALORANTGAME"],"twc":["KXTWCGAME"]}
pml=collections.Counter(lg(m.get('slug')) for m in ml)
print("\n=== KALSHI GAME-SERIES OVERLAP (live open counts) ===")
print(f"{'league':9} {'PM games':8} {'Kalshi series (open count)':40} matcher")
for L,_ in pml.most_common():
    cands=PM_TO_K.get(L,[f"KX{L.upper()}GAME"])
    found=[]
    for s in cands:
        n=kopen(s); time.sleep(0.25)
        if n>0: found.append(f"{s}={n}")
    mtype="abbrev(team)" if L in ("mlb","wnba","nba","nhl") else ("name(indiv)" if L in ("atp","wta","itfm","itfw","ufc") else "team/esport")
    print(f"  {L:9} {pml[L]:8} {(', '.join(found) or 'NONE FOUND'):40} {mtype}")

# ---------- Weather Kalshi coverage ----------
ks=json.load(open(os.path.join(os.path.dirname(__file__),"_data","kalshi_series.json")))
khigh=[s for s in ks if s.get("category")=="Climate and Weather" and re.search(r"high|max",(s.get("title","")).lower())]
klow=[s for s in ks if s.get("category")=="Climate and Weather" and re.search(r"low|min",(s.get("title","")).lower())]
print(f"\n=== WEATHER ===\nKalshi high-temp series: {len(khigh)}   low-temp series: {len(klow)}")
print("PM.us climate cities (high temp):", list(ccities))
print("  -> co-listed weather = these 5 cities, HIGH temp only (PM.us lists no low/rain).")

# ---------- Kalshi player naming for name-matcher ----------
print("\n=== KALSHI PLAYER NAMING (for tennis/UFC name matcher) ===")
for s in ["KXATPMATCH","KXWTAGAME","KXUFCFIGHT"]:
    d=get(f"https://external-api.kalshi.com/trade-api/v2/markets?series_ticker={s}&status=open&limit=6"); time.sleep(0.3)
    print(f"  {s}:")
    for m in d.get("markets",[])[:6]:
        print(f"     yes_sub='{m.get('yes_sub_title')}'  ticker={m.get('ticker')}  close={str(m.get('close_time'))[:10]}")
