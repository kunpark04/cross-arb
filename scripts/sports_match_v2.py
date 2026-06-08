"""Cross-venue sports matcher v2 (READ-ONLY). Joins polymarket.us <-> Kalshi games on
(league, date, team-ABBREVIATION pair) — robust to city-vs-fullname naming. Computes the
2-outcome cross-venue arb (min ask A + min ask B < $1) net of fees, with a price-sanity guard
against bad joins, and true fillable depth on positive edges."""
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
MON=["JAN","FEB","MAR","APR","MAY","JUN","JUL","AUG","SEP","OCT","NOV","DEC"]
def ktok_iso(t):
    m=re.match(r"(\d{2})([A-Z]{3})(\d{2})",t)
    return f"20{m.group(1)}-{MON.index(m.group(2))+1:02d}-{m.group(3)}" if m else None
def dnear(a,b):
    try:
        from datetime import date
        ya,ma,da=map(int,a.split("-")); yb,mb,db=map(int,b.split("-"))
        return abs((date(ya,ma,da)-date(yb,mb,db)).days)<=1
    except: return a==b

LEAGUES={"mlb":"KXMLBGAME","wnba":"KXWNBAGAME","nba":"KXNBAGAME","nhl":"KXNHLGAME",
         "atp":"KXATPMATCH","wta":"KXWTAGAME","ufc":"KXUFCFIGHT"}

# ---- polymarket.us games ----
allm,off=[],0
while True:
    d=get(f"https://gateway.polymarket.us/v1/markets?closed=false&limit=500&offset={off}")
    pg=d.get("markets",[]); allm+=pg
    if len(pg)<500 or len(allm)>12000: break
    off+=500
def pmleague(slug):
    p=str(slug).split("-"); return p[1] if len(p)>1 else "?"
def pm_book(slug):
    md=(get(f"https://gateway.polymarket.us/v1/markets/{slug}/book") or {}).get("marketData",{})
    offs=[(fl(x['px']['value']),fl(x['qty'])) for x in md.get("offers",[]) if x.get('px')]
    bids=[(fl(x['px']['value']),fl(x['qty'])) for x in md.get("bids",[]) if x.get('px')]
    return (min(offs) if offs else (None,0)),(max(bids) if bids else (None,0))

pmgames=collections.defaultdict(list)
for x in allm:
    if x.get("category")!="sports" or x.get("marketType")!="moneyline" or not x.get("gameStartTime"): continue
    L=pmleague(x.get("slug"))
    if L not in LEAGUES: continue
    sides=[s for s in (x.get("marketSides") or []) if (s.get("team") or {}).get("abbreviation")]
    if len(sides)<2: continue
    longs=next((s for s in sides if s.get("long")), sides[0]); other=next((s for s in sides if s is not longs), sides[1])
    (yask,ysz),(ybid,ybsz)=pm_book(str(x.get("slug"))); time.sleep(0.15)
    if yask is None or ybid is None: continue
    A=(longs.get("team") or {}).get("abbreviation","").lower(); B=(other.get("team") or {}).get("abbreviation","").lower()
    pmgames[L].append({"date":str(x.get("gameStartTime"))[:10],"A":A,"B":B,
                       "An":(longs.get("team") or {}).get("name"),"Bn":(other.get("team") or {}).get("name"),
                       "askA":yask,"askA_sz":ysz,"askB":round(1-ybid,3),"askB_sz":ybsz})
print("PM.us games by league:", {k:len(v) for k,v in pmgames.items()})

# ---- Kalshi games per league ----
def kalshi_games(series):
    d=get(f"https://external-api.kalshi.com/trade-api/v2/markets?series_ticker={series}&status=open&limit=1000")
    byev=collections.defaultdict(dict); dates={}
    for m in d.get("markets",[]):
        tk=str(m.get("ticker","")); ev=m.get("event_ticker"); ab=tk.split("-")[-1].lower()
        dm=re.search(r"-(\d{2}[A-Z]{3}\d{2})",tk); dates[ev]=ktok_iso(dm.group(1)) if dm else None
        byev[ev][ab]={"ybid":fl(m.get("yes_bid_dollars")),"yask":fl(m.get("yes_ask_dollars")),"ticker":tk}
    games=[]
    for ev,abd in byev.items():
        games.append({"date":dates.get(ev),"abbrs":abd})
    return games

def k_ask_size(ticker):
    ob=get(f"https://external-api.kalshi.com/trade-api/v2/markets/{ticker}/orderbook").get("orderbook_fp",{})
    nb=[(fl(p),fl(s)) for p,s in ob.get("no_dollars",[])]
    return int(max(nb)[1]) if nb else 0   # size buyable at yes_ask (= top no-bid size)

print("\nMATCHES & cross-venue arb (net/contract; price-sanity guard rejects >0.40 disagreements):")
allmatched=0; alledges=[]
for L,series in LEAGUES.items():
    if not pmgames.get(L): continue
    kg=kalshi_games(series); time.sleep(0.4)
    kindex=collections.defaultdict(list)
    for g in kg: kindex[frozenset(g["abbrs"].keys())].append(g)
    matched=0
    for pg in pmgames[L]:
        key=frozenset({pg["A"],pg["B"]})
        cands=[g for g in kindex.get(key,[]) if g["date"] and dnear(g["date"],pg["date"])]
        if not cands: continue
        kgame=cands[0]; matched+=1; allmatched+=1
        kA=kgame["abbrs"].get(pg["A"],{}); kB=kgame["abbrs"].get(pg["B"],{})
        kaskA=kA.get("yask"); kaskB=kB.get("yask")
        if not kaskA or not kaskB: continue
        # price-sanity: implied A-prob should roughly agree (both ~ askA). reject bad joins.
        if abs(pg["askA"]-kaskA)>0.40:
            print(f"  [{L}] {pg['An']} vs {pg['Bn']} {pg['date']} -> SANITY REJECT (PM A {pg['askA']:.2f} vs K {kaskA:.2f}) likely bad join")
            continue
        minA=min(pg["askA"],kaskA); minB=min(pg["askB"],kaskB)
        gross=round(1-(minA+minB),3)
        net=round(gross - (pfee(pg['askA']) if minA==pg['askA'] else kfee(kaskA))
                        - (pfee(pg['askB']) if minB==pg['askB'] else kfee(kaskB)),3)
        sideA="PM" if minA==pg["askA"] else "K"; sideB="PM" if minB==pg["askB"] else "K"
        tag=" <== EDGE" if net>0 else ""
        print(f"  [{L}] {str(pg['An'])[:16]:16} vs {str(pg['Bn'])[:16]:16} {pg['date']} | "
              f"A PM{pg['askA']:.2f}/K{kaskA:.2f} B PM{pg['askB']:.2f}/K{kaskB:.2f} | "
              f"buy A@{sideA} B@{sideB}  net {net:+.3f}{tag}")
        if net>0: alledges.append((L,pg,net,sideA,sideB,kA.get("ticker"),kB.get("ticker")))
    print(f"  -- {L}: {len(pmgames[L])} PM games, {matched} matched")

print(f"\nTOTAL: {allmatched} games matched across leagues, {len(alledges)} with positive net edge.")
if alledges:
    print("Positive edges (fillable depth + $ value at top-of-book):")
    tot=0.0
    for L,pg,net,sA,sB,kAtk,kBtk in sorted(alledges,key=lambda t:-t[2]):
        szA = pg["askA_sz"] if sA=="PM" else k_ask_size(kAtk)
        szB = pg["askB_sz"] if sB=="PM" else k_ask_size(kBtk); time.sleep(0.2)
        size=int(min(szA,szB)); val=net*size; tot+=max(val,0.0)
        print(f"  [{L}] {str(pg['An'])[:18]:18} vs {str(pg['Bn'])[:18]:18} net {net:+.3f} x{size} = ${val:.2f}  (buy A@{sA} B@{sB})")
    print(f"\n  Sum of positive sports-edge value right now: ~${tot:.2f}")
