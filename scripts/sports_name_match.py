"""Name-based cross-venue matcher for INDIVIDUAL sports (tennis ATP/WTA/ITF + UFC), READ-ONLY.
Joins polymarket.us <-> Kalshi on (league, date, player-SURNAME pair) since both venues expose full
names. Keeps EVERY matched market (no edge pruning) and computes net edge + fillable depth + $ for
all. Price-sanity guard rejects bad joins. Writes _data/name_match.json."""
import json, os, re, urllib.request, urllib.error, time, math, collections, unicodedata

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
    m=re.match(r"(\d{2})([A-Z]{3})(\d{2})",t); return f"20{m.group(1)}-{MON.index(m.group(2))+1:02d}-{m.group(3)}" if m else None
def dnear(a,b):
    from datetime import date
    try:
        ya,ma,da=map(int,a.split("-")); yb,mb,db=map(int,b.split("-")); return abs((date(ya,ma,da)-date(yb,mb,db)).days)<=1
    except: return a==b
def surname(name):
    n=unicodedata.normalize("NFKD",str(name)).encode("ascii","ignore").decode().lower()
    n=re.sub(r"[^a-z \-]"," ",n); toks=[t for t in n.split() if t]
    return toks[-1] if toks else ""
def smatch(a,b):
    return a==b or (len(a)>=4 and len(b)>=4 and (a.startswith(b) or b.startswith(a)))

LEAGUES={"atp":"KXATPMATCH","wta":"KXWTAMATCH","itfm":"KXITFMATCH","itfw":"KXITFWMATCH","ufc":"KXUFCFIGHT"}

# PM.us individual matches
allm,off=[],0
while True:
    d=get(f"https://gateway.polymarket.us/v1/markets?closed=false&limit=500&offset={off}")
    pg=d.get("markets",[]); allm+=pg
    if len(pg)<500 or len(allm)>12000: break
    off+=500
def pmlg(s):
    p=str(s).split("-"); return p[1] if len(p)>1 else "?"
def pm_book(slug):
    md=(get(f"https://gateway.polymarket.us/v1/markets/{slug}/book") or {}).get("marketData",{})
    offs=[(fl(x['px']['value']),fl(x['qty'])) for x in md.get("offers",[]) if x.get('px')]
    bids=[(fl(x['px']['value']),fl(x['qty'])) for x in md.get("bids",[]) if x.get('px')]
    return (min(offs) if offs else (None,0)),(max(bids) if bids else (None,0))
def k_ask_size(ticker):
    ob=get(f"https://external-api.kalshi.com/trade-api/v2/markets/{ticker}/orderbook").get("orderbook_fp",{})
    nb=[(fl(p),fl(s)) for p,s in ob.get("no_dollars",[])]; return int(max(nb)[1]) if nb else 0

pmg=collections.defaultdict(list)
for x in allm:
    if x.get("category")!="sports" or x.get("marketType")!="moneyline" or not x.get("gameStartTime"): continue
    L=pmlg(x.get("slug"))
    if L not in LEAGUES: continue
    sides=[s for s in (x.get("marketSides") or []) if (s.get("team") or {}).get("name")]
    if len(sides)<2: continue
    longs=next((s for s in sides if s.get("long")),sides[0]); other=next((s for s in sides if s is not longs),sides[1])
    (yask,ysz),(ybid,ybsz)=pm_book(str(x.get("slug"))); time.sleep(0.15)
    if yask is None or ybid is None: continue
    pmg[L].append({"date":str(x.get("gameStartTime"))[:10],
                   "An":(longs.get("team") or {}).get("name"),"Bn":(other.get("team") or {}).get("name"),
                   "sA":surname((longs.get("team") or {}).get("name")),"sB":surname((other.get("team") or {}).get("name")),
                   "askA":yask,"askA_sz":ysz,"askB":round(1-ybid,3),"askB_sz":ybsz})
print("PM.us individual-sport games:", {k:len(v) for k,v in pmg.items()})

rows=[]; summary={}
for L,series in LEAGUES.items():
    if not pmg.get(L): continue
    d=get(f"https://external-api.kalshi.com/trade-api/v2/markets?series_ticker={series}&status=open&limit=1000"); time.sleep(0.4)
    byev=collections.defaultdict(dict); evdate={}
    for m in d.get("markets",[]):
        ev=m.get("event_ticker"); tk=str(m.get("ticker","")); sn=surname(m.get("yes_sub_title"))
        dm=re.search(r"-(\d{2}[A-Z]{3}\d{2})",tk); evdate[ev]=ktok_iso(dm.group(1)) if dm else None
        if sn: byev[ev][sn]={"ybid":fl(m.get("yes_bid_dollars")),"yask":fl(m.get("yes_ask_dollars")),"ticker":tk}
    kbydate=collections.defaultdict(list)
    for ev,pl in byev.items(): kbydate[evdate.get(ev)].append(pl)
    matched=0; edges=0
    for pg in pmg[L]:
        cand=None
        for dd in (pg["date"],):  # plus near-date scan
            for kd in list(kbydate.keys()):
                if not kd or not dnear(kd,pg["date"]): continue
                for pl in kbydate[kd]:
                    snames=list(pl.keys())
                    mA=next((s for s in snames if smatch(s,pg["sA"])),None)
                    mB=next((s for s in snames if smatch(s,pg["sB"])),None)
                    if mA and mB and mA!=mB: cand=(pl,mA,mB); break
                if cand: break
            if cand: break
        if not cand: continue
        pl,mA,mB=cand; matched+=1
        kA,kB=pl[mA],pl[mB]; kaskA,kaskB=kA.get("yask"),kB.get("yask")
        if not kaskA or not kaskB: continue
        if abs(pg["askA"]-kaskA)>0.40:  # sanity guard
            continue
        minA=min(pg["askA"],kaskA); minB=min(pg["askB"],kaskB)
        net=round((1-(minA+minB)) - (pfee(pg['askA']) if minA==pg['askA'] else kfee(kaskA))
                                  - (pfee(pg['askB']) if minB==pg['askB'] else kfee(kaskB)),3)
        sA="PM" if minA==pg["askA"] else "K"; sB="PM" if minB==pg["askB"] else "K"
        row={"league":L,"date":pg["date"],"A":pg["An"],"B":pg["Bn"],
             "pm_askA":pg["askA"],"k_askA":kaskA,"pm_askB":pg["askB"],"k_askB":kaskB,
             "net":net,"buyA":sA,"buyB":sB}
        if net>0:
            edges+=1
            szA=pg["askA_sz"] if sA=="PM" else k_ask_size(kA["ticker"])
            szB=pg["askB_sz"] if sB=="PM" else k_ask_size(kB["ticker"]); time.sleep(0.15)
            row["size"]=int(min(szA,szB)); row["dollar"]=round(net*row["size"],2)
        rows.append(row)
    summary[L]={"pm_games":len(pmg[L]),"matched":matched,"edges":edges}

json.dump(rows,open(os.path.join(os.path.dirname(__file__),"_data","name_match.json"),"w"),indent=1)
print("\nper-league:", json.dumps(summary))
ed=[r for r in rows if r["net"]>0]
print(f"\nTotal matched (kept, all): {len(rows)}  | positive-edge: {len(ed)}  | total $ now: ~${round(sum(r.get('dollar',0) for r in ed),2)}")
print("\nTop positive edges (metrics kept for ALL matched in _data/name_match.json):")
for r in sorted(ed,key=lambda r:-r.get("dollar",0))[:15]:
    print(f"  [{r['league']}] {str(r['A'])[:18]:18} vs {str(r['B'])[:18]:18} {r['date']} net {r['net']:+.3f} x{r.get('size','?')} = ${r.get('dollar','?')}")
