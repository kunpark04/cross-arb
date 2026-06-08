"""First-pass cross-venue SPORTS matcher (Kalshi x polymarket.us), READ-ONLY.
PM.us moneyline games -> strict team-token match to Kalshi game markets -> 2-outcome arb
(min ask A + min ask B < $1, net of fees). Prints structure diagnostics + coverage + edges."""
import json, os, re, urllib.request, urllib.error, time, math, collections

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
def fl(v):
    try: return float(v)
    except: return 0.0
def kfee(p): return math.ceil(0.07*100*p*(1-p))/100 if 0<p<1 else 0.0
def pfee(p): return round(0.05*p*(1-p),4) if 0<p<1 else 0.0
STOP={"the","of","fc","sc","club","city"}
def toks(name):
    return [w for w in re.sub(r"[^a-z0-9 ]"," ",str(name).lower()).split() if w and w not in STOP]
def nick(name):  # distinctive token = last word of team name
    t=toks(name); return t[-1] if t else ""

# ---------- PM.us moneyline games ----------
allm,off=[],0
while True:
    d=get(f"https://gateway.polymarket.us/v1/markets?closed=false&limit=500&offset={off}")
    pg=d.get("markets",[]); allm+=pg
    if len(pg)<500 or len(allm)>12000: break
    off+=500
games=[x for x in allm if x.get("category")=="sports" and x.get("marketType")=="moneyline" and x.get("gameStartTime")]
print(f"PM.us open moneyline games: {len(games)}")

def pm_book_topask(slug):
    md=(get(f"https://gateway.polymarket.us/v1/markets/{slug}/book") or {}).get("marketData",{})
    offs=[(fl(x['px']['value']),fl(x['qty'])) for x in md.get("offers",[]) if x.get('px')]
    bids=[(fl(x['px']['value']),fl(x['qty'])) for x in md.get("bids",[]) if x.get('px')]
    return (min(offs) if offs else (None,0)),(max(bids) if bids else (None,0))

# structure diag on first game
if games:
    g=games[0]
    print("\nDIAG first game:", g.get("slug"), "| q:", str(g.get("question"))[:50])
    print("  outcomes:", g.get("outcomes"), " sportsMarketType:", g.get("sportsMarketType"))
    for s in (g.get("marketSides") or []):
        tm=s.get("team") or {}
        print(f"   side: desc={s.get('description')} team={tm.get('name')} abbr={tm.get('abbreviation')} quote={ (s.get('quote') or {}).get('value') } long={s.get('long')}")
    (a,asz),(b,bsz)=pm_book_topask(str(g.get("slug"))); print(f"  book: best_ask={a}x{asz} best_bid={b}x{bsz}")

# build PM games: teams + per-team ask (YES book = first/long side; other = 1-bid)
pmgames=[]
for g in games:
    sides=[s for s in (g.get("marketSides") or []) if (s.get("team") or {}).get("name")]
    if len(sides)<2: continue
    # the /book is the YES side = the 'long'/first INSTRUMENT side
    longside=next((s for s in sides if s.get("long")), sides[0])
    other=next((s for s in sides if s is not longside), sides[1] if len(sides)>1 else None)
    (yask,ysz),(ybid,_)=pm_book_topask(str(g.get("slug"))); time.sleep(0.2)
    if yask is None or ybid is None: continue
    teamL=(longside.get("team") or {}).get("name"); teamO=(other.get("team") or {}).get("name")
    date=str(g.get("gameStartTime"))[:10]
    pmgames.append({"slug":g.get("slug"),"date":date,
                    "teamL":teamL,"askL":yask,        # buy YES(=L wins) at ask
                    "teamO":teamO,"askO":round(1-ybid,3)})  # buy NO(=O wins) at 1-yesbid
print(f"PM.us games with 2 teams + book: {len(pmgames)}")

# ---------- Kalshi open markets index ----------
kmk=[]; cursor=None
for _ in range(5):
    url="https://external-api.kalshi.com/trade-api/v2/markets?status=open&limit=1000"+(f"&cursor={cursor}" if cursor else "")
    d=get(url); kmk+=d.get("markets",[]); cursor=d.get("cursor")
    if not cursor: break
    time.sleep(1.0)
print(f"Kalshi open markets pulled: {len(kmk)}")
# index: searchable text per market
kidx=[]
for m in kmk:
    txt=" ".join(str(m.get(k,"")) for k in ("title","subtitle","yes_sub_title","no_sub_title","event_ticker","ticker")).lower()
    kidx.append((txt,m))

# ---------- match + arb ----------
print("\nMATCHES (strict: both team nicknames present in a Kalshi market):")
matched=0; edges=[]
for pg in pmgames:
    nL,nO=nick(pg["teamL"]),nick(pg["teamO"])
    if not nL or not nO: continue
    cands=[m for txt,m in kidx if nL in txt and nO in txt]
    if not cands: continue
    matched+=1
    # find Kalshi market priced as "teamL wins": yes_sub_title contains nL
    kL=next((m for m in cands if nL in str(m.get("yes_sub_title","")).lower()), None)
    if not kL:
        print(f"  ~ {pg['teamL']} vs {pg['teamO']} ({pg['date']}): {len(cands)} Kalshi mkts but couldn't id team-L market")
        continue
    k_askL=fl(kL.get("yes_ask_dollars")); k_askO=round(1-fl(kL.get("yes_bid_dollars")),3)  # NO ask = 1-yesbid
    minA=min(pg["askL"],k_askL); minB=min(pg["askO"],k_askO)
    gross=round(1-(minA+minB),3)
    net=round(gross - pfee(pg['askL'] if minA==pg['askL'] else 0) - kfee(k_askL if minA==k_askL else 0)
                    - pfee(pg['askO'] if minB==pg['askO'] else 0) - kfee(k_askO if minB==k_askO else 0),3)
    side=f"A:{'PM' if minA==pg['askL'] else 'K'} B:{'PM' if minB==pg['askO'] else 'K'}"
    tag=" <== EDGE" if net>0 else ""
    print(f"  {pg['teamL'][:16]:16} vs {pg['teamO'][:16]:16} {pg['date']} | "
          f"L ask PM{pg['askL']:.2f}/K{k_askL:.2f}  O ask PM{pg['askO']:.2f}/K{k_askO:.2f} | "
          f"min {minA:.2f}+{minB:.2f}  net {net:+.3f} [{side}]{tag}")
    if net>0: edges.append((pg,net))

print(f"\nCoverage: {len(pmgames)} PM games, {matched} matched to Kalshi, {len(edges)} with positive net edge.")
