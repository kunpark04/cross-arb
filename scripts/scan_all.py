"""UNIFIED cross-venue scanner (READ-ONLY). Covers the ENTIRE co-listed universe and KEEPS EVERY
market (no edge pruning) with uniform metrics, for the live bot's later trade-selection layer.
  - Weather: all 5 cities x co-listed dates, every 2F bucket (IDENTICAL-bounds join).
  - Sports moneyline: every co-listed league. Team/esport -> abbreviation join; individual -> surname.
Metric per market (2 complementary outcomes A/B): net = 1 - (min askA + min askB) - fees;
fillable size + $ on positive edges. Output -> _data/scan_all.json (+ summary).
Matchers + fee models are IMPORTED from bot/ (colisted_map + ledger) — the scanner's old private
copies drifted (detection charged the n=1 CEIL fee, violating [L15]; weather used an index-zip)."""
import json, os, sys, re, time, collections
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bot"))
from colisted_map import (get, pm_catalog, econ_colisted, pair_weather_date, pick_game,
                          surname, ktok_iso, wcity, pmlg, WX, LEAGUES, KAL)
from ledger import kfee, pfee            # detection uses the MARGINAL (at-scale) fee — L10/L15

def fl(v):
    try: return float(v)
    except: return 0.0
def pm_book(slug):
    md=(get(f"https://gateway.polymarket.us/v1/markets/{slug}/book") or {}).get("marketData",{})
    offs=[(fl(x['px']['value']),fl(x['qty'])) for x in md.get("offers",[]) if x.get('px')]
    bids=[(fl(x['px']['value']),fl(x['qty'])) for x in md.get("bids",[]) if x.get('px')]
    return (max(bids) if bids else (None,0)),(min(offs) if offs else (None,0))  # (bid),(ask)
def k_ob(ticker):
    ob=get(f"{KAL}/{ticker}/orderbook").get("orderbook_fp",{})   # same host as the matcher (verified same shape)
    yb=[(fl(p),fl(s)) for p,s in ob.get("yes_dollars",[])]; nb=[(fl(p),fl(s)) for p,s in ob.get("no_dollars",[])]
    ybid,ybsz=(max(yb) if yb else (0.0,0)); nbid,nbsz=(max(nb) if nb else (0.0,0))
    return ybid,ybsz,(round(1-nbid,2) if nbid else None),nbsz  # yes_bid, yes_bid_sz, yes_ask, yes_ask_sz(=top no-bid sz)

def metric(pmA_ask,pmA_sz,pmB_ask,pmB_sz,kA_ask,kA_sz,kB_ask,kB_sz):
    if None in (pmA_ask,pmB_ask,kA_ask,kB_ask): return None
    minA=min(pmA_ask,kA_ask); minB=min(pmB_ask,kB_ask)
    net=round((1-(minA+minB)) - (pfee(pmA_ask) if minA==pmA_ask else kfee(kA_ask,marginal=True))
                              - (pfee(pmB_ask) if minB==pmB_ask else kfee(kB_ask,marginal=True)),3)
    bA="PM" if minA==pmA_ask else "K"; bB="PM" if minB==pmB_ask else "K"
    szA=pmA_sz if bA=="PM" else kA_sz; szB=pmB_sz if bB=="PM" else kB_sz
    size=int(min(szA,szB));
    return {"net":net,"buyA":bA,"buyB":bB,"size":size,"dollar":round(max(net,0.0)*size,2),
            "pm_askA":pmA_ask,"k_askA":kA_ask,"pm_askB":pmB_ask,"k_askB":kB_ask}

# ---------- pull PM.us catalog (shared paginator: 12k-cap warning included) ----------
allm=pm_catalog()
rows=[]

# ---------- WEATHER ----------
clim=[m for m in allm if m.get("category")=="climate"]
for city,kser in WX.items():
    pmc=[m for m in clim if wcity(m.get("slug"))==city]
    if not pmc: continue
    bydate=collections.defaultdict(list)
    for m in pmc:
        dm=re.search(r"(\d{4}-\d{2}-\d{2})",str(m.get("slug")));  bydate[dm.group(1) if dm else "?"].append(m)
    kd=get(f"{KAL}?series_ticker={kser}&limit=1000"); time.sleep(0.3)
    kby=collections.defaultdict(list)
    for m in kd.get("markets",[]):
        dm=re.search(r"-(\d{2}[A-Z]{3}\d{2})",str(m.get("ticker")));
        if dm: kby[ktok_iso(dm.group(1))].append(m)
    for date in sorted(set(bydate)&set(kby)):
        pairs,_flags=pair_weather_date(bydate[date],kby[date])   # IDENTICAL-bounds dict join (C4/M1)
        for pmm,kmm,_b in pairs:
            (pb,pbs),(pa,pas)=pm_book(str(pmm.get("slug"))); time.sleep(0.12)
            kyb,kybs,kya,kyas=k_ob(kmm.get("ticker")); time.sleep(0.12)
            mt=metric(pa,pas,(round(1-pb,2) if pb is not None else None),pbs, kya,kyas,(round(1-kyb,2) if kyb else None),kybs)
            if mt: rows.append({"cat":"weather","group":city,"date":date,
                                "A":str(kmm.get("yes_sub_title")),"B":"NOT "+str(kmm.get("yes_sub_title")),**mt})

# ---------- SPORTS moneyline ----------
pmg=collections.defaultdict(list)
for x in allm:
    if x.get("category")!="sports" or x.get("marketType")!="moneyline" or not x.get("gameStartTime"): continue
    L=pmlg(x.get("slug"))
    if L not in LEAGUES: continue
    pmg[L].append(x)
for L,(series,join) in LEAGUES.items():
    if not pmg.get(L): continue
    kd=get(f"{KAL}?series_ticker={series}&status=open&limit=1000"); time.sleep(0.4)
    byev=collections.defaultdict(dict); evd={}
    for m in kd.get("markets",[]):
        ev=m.get("event_ticker"); tk=str(m.get("ticker",""));
        key=tk.split("-")[-1].lower() if join=="abbrev" else surname(m.get("yes_sub_title"))
        dm=re.search(r"-(\d{2}[A-Z]{3}\d{2})",tk); evd[ev]=ktok_iso(dm.group(1)) if dm else None
        if key: byev[ev][key]={"ybid":fl(m.get("yes_bid_dollars")),"yask":fl(m.get("yes_ask_dollars")),"ticker":tk}
    kbydate=collections.defaultdict(list)
    for ev,pl in byev.items(): kbydate[evd.get(ev)].append(pl)
    used=set()                                                # doubleheader guard: one Kalshi event, one pm game
    for x in pmg[L]:
        sides=[s for s in (x.get("marketSides") or []) if (s.get("team") or {}).get("name")]
        if len(sides)<2: continue
        lo=next((s for s in sides if s.get("long")),sides[0]); ot=next((s for s in sides if s is not lo),sides[1])
        if join=="abbrev": kA=(lo.get("team") or {}).get("abbreviation","").lower(); kB=(ot.get("team") or {}).get("abbreviation","").lower()
        else: kA=surname((lo.get("team") or {}).get("name")); kB=surname((ot.get("team") or {}).get("name"))
        sm=re.search(r"(\d{4}-\d{2}-\d{2})",str(x.get("slug")))   # C2: pm slug ET date == Kalshi ticker date (exact join)
        date=sm.group(1) if sm else str(x.get("gameStartTime"))[:10]
        found=pick_game(kbydate,kA,kB,join,date,slug_dated=sm is not None,used=used)   # shared binder (C2+M3)
        if not found: continue
        pl,mA,mB=found
        (pb,pbs),(pa,pas)=pm_book(str(x.get("slug"))); time.sleep(0.12)
        if pa is None or pb is None: continue
        kaA=pl[mA].get("yask"); kaB=pl[mB].get("yask")
        if not kaA or not kaB or abs(pa-kaA)>0.40: continue   # sanity guard
        # PM: askA=pa, askB=1-pb. Kalshi sizes only fetched if a positive edge is plausible.
        prelim=metric(pa,pas,round(1-pb,3),pbs,kaA,10**9,kaB,10**9)  # price-only net
        kAsz=kBsz=0
        if prelim and prelim["net"]>0:
            _,_,_,kAsz=k_ob(pl[mA]["ticker"]); _,_,_,kBsz=k_ob(pl[mB]["ticker"]); time.sleep(0.2)
        mt=metric(pa,pas,round(1-pb,3),pbs,kaA,kAsz,kaB,kBsz)
        if mt: rows.append({"cat":"sports","group":L,"date":date,
                            "A":(lo.get("team") or {}).get("name"),"B":(ot.get("team") or {}).get("name"),**mt})

# ---------- ECON (macro): pmus threshold/categorical <-> Kalshi, SAME-orientation only (verified) ----------
econ_pairs, econ_flags = econ_colisted(allm)
for e in econ_pairs:
    (pb,pbs),(pa,pas)=pm_book(str(e["slug"])); time.sleep(0.12)
    kyb,kybs,kya,kyas=k_ob(e["kalshi"]); time.sleep(0.12)
    mt=metric(pa,pas,(round(1-pb,2) if pb is not None else None),pbs, kya,kyas,(round(1-kyb,2) if kyb else None),kybs)
    if mt: rows.append({"cat":"econ","group":e["family"],"date":e.get("period"),
                        "A":str(e.get("thr",e.get("outcome"))),"B":"complement","slug":str(e["slug"]),**mt})

# ---------- output ----------
json.dump(rows,open(os.path.join(os.path.dirname(__file__),"_data","scan_all.json"),"w"),indent=1)
print(f"TOTAL co-listed markets kept: {len(rows)}  (weather {sum(1 for r in rows if r['cat']=='weather')}, sports {sum(1 for r in rows if r['cat']=='sports')})")
bygrp=collections.defaultdict(lambda:[0,0,0.0])
for r in rows:
    g=bygrp[(r['cat'],r['group'])]; g[0]+=1
    if r['net']>0: g[1]+=1; g[2]+=r['dollar']
print(f"\n{'cat/group':22} {'markets':8} {'+edges':7} {'$ now':8}")
for (cat,grp),(n,e,dollar) in sorted(bygrp.items(),key=lambda kv:-kv[1][2]):
    print(f"  {cat+'/'+grp:22} {n:8} {e:7} ${dollar:.2f}")
tot=round(sum(r['dollar'] for r in rows if r['net']>0),2)
print(f"\nTOTAL positive-edge $ across ENTIRE universe right now: ~${tot}")
print("All markets + metrics saved -> scripts/_data/scan_all.json (nothing pruned).")
