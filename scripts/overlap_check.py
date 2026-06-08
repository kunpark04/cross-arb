"""Decisive check: do Kalshi & polymarket.us actually CO-LIST the same games right now?
Greps Kalshi's full series catalog for per-game-winner series by league, then live-queries the
plausible current-overlap leagues (esp. MLB) for open markets. Read-only."""
import json, os, re, urllib.request, urllib.error, time

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

ks=json.load(open(os.path.join(os.path.dirname(__file__),"_data","kalshi_series.json")))
# per-GAME winner series: ticker ends with GAME, or title is a matchup
gameser=[s for s in ks if re.search(r"GAME$", str(s.get("ticker",""))) or
         re.search(r"\b(vs|to win|game winner|moneyline)\b", str(s.get("title","")).lower())]
print(f"Kalshi *GAME / matchup series ({len(gameser)}):")
for s in sorted(gameser, key=lambda s:s.get("ticker","")):
    print(f"  {s.get('ticker'):20} cat={s.get('category'):20} {str(s.get('title'))[:40]}")

# league keyword scan in series catalog
print("\nKalshi series mentioning these leagues (any series, to gauge coverage):")
for kw in ["baseball","mlb","tennis","atp","wta","wnba","ufc","mma","nhl","hockey"]:
    hits=[s for s in ks if kw in (str(s.get("ticker",""))+" "+str(s.get("title",""))).lower()]
    sample=[s.get("ticker") for s in hits[:4]]
    print(f"  {kw:9}: {len(hits):3} series  e.g. {sample}")

# live: open markets for candidate game-winner series
print("\nLive open-market counts for candidate game series:")
for tk in ["KXMLBGAME","KXMLBGAMES","KXMLB","KXWNBAGAME","KXNBAGAME","KXNHLGAME",
           "KXATP","KXATPMATCH","KXTENNIS","KXUFCFIGHT","KXUFC","KXMMA"]:
    d=get(f"https://external-api.kalshi.com/trade-api/v2/markets?series_ticker={tk}&status=open&limit=50"); time.sleep(0.3)
    n=len(d.get("markets",[])) if isinstance(d,dict) else 0
    err=d.get("_err") if isinstance(d,dict) else None
    print(f"  {tk:14} open markets={n}" + (f"  (err {err})" if err else ""))

# direct: search Kalshi open markets for today's PM.us MLB teams
print("\nSearch Kalshi open markets for today's MLB teams (Red Sox / Rays / Mariners / Orioles):")
mk=[]; cursor=None
for _ in range(8):
    url="https://external-api.kalshi.com/trade-api/v2/markets?status=open&limit=1000"+(f"&cursor={cursor}" if cursor else "")
    d=get(url); mk+=d.get("markets",[]); cursor=d.get("cursor")
    if not cursor: break
    time.sleep(0.8)
print(f"  (scanned {len(mk)} open Kalshi markets)")
for team in ["red sox","rays","mariners","orioles","yankees","dodgers"]:
    hits=[m for m in mk if team in (str(m.get('title',''))+" "+str(m.get('yes_sub_title',''))+" "+str(m.get('event_ticker',''))).lower()]
    ex=[(m.get('event_ticker'),m.get('yes_sub_title')) for m in hits[:3]]
    print(f"  {team:9}: {len(hits)} markets  e.g. {ex}")
