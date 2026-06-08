"""Discover how Kalshi structures sports GAME-WINNER markets so the matcher can be built on facts.
Also profiles polymarket.us league/naming. Read-only."""
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

# ---------- polymarket.us: league + naming ----------
allm,off=[],0
while True:
    d=get(f"https://gateway.polymarket.us/v1/markets?closed=false&limit=500&offset={off}")
    pg=d.get("markets",[]); allm+=pg
    if len(pg)<500 or len(allm)>12000: break
    off+=500
pmg=[x for x in allm if x.get("category")=="sports" and x.get("marketType")=="moneyline" and x.get("gameStartTime")]
def league(slug):
    p=str(slug).split("-")
    return p[1] if len(p)>1 else "?"
lg=collections.Counter(league(x.get("slug")) for x in pmg)
print("PM.us moneyline games by league:", dict(lg.most_common()))
print("\nPM.us sample team names per league (note: city-only vs full):")
for L,_ in lg.most_common(6):
    ex=[x for x in pmg if league(x.get("slug"))==L][:2]
    for x in ex:
        names=[ (s.get("team") or {}).get("name") for s in (x.get("marketSides") or []) ]
        abbrs=[ (s.get("team") or {}).get("abbreviation") for s in (x.get("marketSides") or []) ]
        print(f"  {L:6} {str(x.get('gameStartTime'))[:10]}  names={names} abbr={abbrs} slug={x.get('slug')}")

# ---------- Kalshi: open sports events -> series tally ----------
evs=[]; cursor=None
for _ in range(20):
    url="https://external-api.kalshi.com/trade-api/v2/events?status=open&limit=200"+(f"&cursor={cursor}" if cursor else "")
    d=get(url); evs+=d.get("events",[]); cursor=d.get("cursor")
    if not cursor: break
    time.sleep(0.8)
sp=[e for e in evs if (e.get("category") or "")=="Sports"]
print(f"\nKalshi open events total={len(evs)}  Sports={len(sp)}")
ser=collections.Counter(e.get("series_ticker") for e in sp)
print("\nKalshi Sports series_ticker tally (top 25):")
for s,c in ser.most_common(25):
    titles=[str(e.get('title'))[:34] for e in sp if e.get('series_ticker')==s][:2]
    print(f"  {str(s):22} {c:3}  e.g. {titles}")

# ---------- inspect a few likely game-winner series' market structure ----------
def looks_game(title):
    t=str(title).lower()
    return (" vs" in t or " at " in t or "@" in t or "winner" in t or "beat" in t or "win?" in t)
gameser=[s for s,_ in ser.most_common() if any(looks_game(e.get('title')) for e in sp if e.get('series_ticker')==s)][:6]
print(f"\nGame-like series to inspect: {gameser}")
for s in gameser:
    ev=next(e for e in sp if e.get('series_ticker')==s)
    et=ev.get('event_ticker')
    d=get(f"https://external-api.kalshi.com/trade-api/v2/markets?event_ticker={et}&limit=20"); time.sleep(0.5)
    mk=d.get("markets",[])
    print(f"\n  series {s}  event {et}  title='{str(ev.get('title'))[:46]}'  markets={len(mk)}")
    for m in mk[:4]:
        print(f"     yes_sub='{m.get('yes_sub_title')}' no_sub='{m.get('no_sub_title')}' "
              f"yes {m.get('yes_bid_dollars')}/{m.get('yes_ask_dollars')} "
              f"close={str(m.get('close_time'))[:10]} ticker={m.get('ticker')}")
