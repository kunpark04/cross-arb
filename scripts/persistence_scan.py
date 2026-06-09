"""READ-ONLY persistence probe. Repeated snapshots of cross-venue WEATHER edges (Kalshi x
polymarket.us, all 5 cities, every co-listed date, bucket-aligned, depth-aware, fee-netted)
plus polymarket.us SPORTS book-liquidity characterization. No auth, no orders.
Writes scripts/_data/persistence_log.jsonl (per snapshot) + _data/persistence_summary.txt (final)."""
import json, os, re, urllib.request, urllib.error, time, math, collections
from datetime import datetime, timezone

OUT = os.path.join(os.path.dirname(__file__), "_data"); os.makedirs(OUT, exist_ok=True)
UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
SNAPSHOTS, INTERVAL = 7, 150           # ~17 min total

def now(): return datetime.now(timezone.utc)
def get(url, tries=4):
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429 and i < tries-1: time.sleep(3*(i+1)); continue
            return {"_err": e.code}
        except Exception as e:
            return {"_err": str(e)[:60]}
    return {}
def kfee(p): return math.ceil(0.07*100*p*(1-p))/100 if 0 < p < 1 else 0.0
def pfee(p): return round(0.05*p*(1-p), 4) if 0 < p < 1 else 0.0
def fl(v):
    try: return float(v)
    except Exception: return 0.0

MON = ["JAN","FEB","MAR","APR","MAY","JUN","JUL","AUG","SEP","OCT","NOV","DEC"]
def ktok_to_iso(t):
    m = re.match(r"(\d{2})([A-Z]{3})(\d{2})", t)
    if not m: return None
    return f"20{m.group(1)}-{MON.index(m.group(2))+1:02d}-{m.group(3)}"

CITIES = {"Miami":("KXHIGHMIA","miahigh"),"Chicago":("KXHIGHCHI","mdwhigh"),
          "NYC":("KXHIGHNY","nychigh"),"LA":("KXHIGHLAX","laxhigh"),"SF":("KXHIGHTSFO","sfohigh")}

def pull_open():
    allm, off = [], 0
    while True:
        d = get(f"https://gateway.polymarket.us/v1/markets?closed=false&limit=500&offset={off}")
        page = d.get("markets", []) if isinstance(d, dict) else []
        allm += page
        if len(page) < 500 or len(allm) > 12000: break
        off += 500
    return allm
def pm_book(slug):
    md = (get(f"https://gateway.polymarket.us/v1/markets/{slug}/book") or {}).get("marketData", {})
    bids = [(fl(x["px"]["value"]), fl(x["qty"])) for x in md.get("bids", []) if x.get("px")]
    offs = [(fl(x["px"]["value"]), fl(x["qty"])) for x in md.get("offers", []) if x.get("px")]
    return (max(bids) if bids else (None,0)), (min(offs) if offs else (None,0))
def pm_lo(slug):
    m = re.search(r"-lt(\d+)f", slug);  m2 = re.search(r"gte(\d+)", slug)
    return (int(m.group(1))-100) if m else (int(m2.group(1)) if m2 else 0)
def pm_bounds(slug):                          # pm bucket -> inclusive (lo,hi) degF (mirrors bot/colisted_map.py)
    s = str(slug).lower()
    m = re.search(r"gte(\d+)lt(\d+)", s)
    if m: return (int(m.group(1)), int(m.group(2)))
    m = re.search(r"-lt(\d+)f", s)
    if m: return (None, int(m.group(1)) - 1)
    m = re.search(r"gte(\d+)", s)
    return (int(m.group(1)), None) if m else (None, None)
def kbounds(m):                               # Kalshi -> inclusive (lo,hi): middle [floor,cap]; tails floor+1/cap-1
    fls, cap = m.get("floor_strike"), m.get("cap_strike")
    if fls is None and cap is not None: return (None, cap - 1)
    if cap is None and fls is not None: return (fls + 1, None)
    return (fls, cap)

def weather_snapshot(climate):
    edges, rows = [], 0
    bycity = collections.defaultdict(lambda: collections.defaultdict(list))  # city->date->[mkts]
    for x in climate:
        slug = str(x.get("slug","")).lower(); dm = re.search(r"(\d{4}-\d{2}-\d{2})", slug)
        for city,(kser,tag) in CITIES.items():
            if tag in slug and dm: bycity[city][dm.group(1)].append(x)
    for city,(kser,tag) in CITIES.items():
        kd = get(f"https://external-api.kalshi.com/trade-api/v2/markets?series_ticker={kser}&limit=1000")
        kby = collections.defaultdict(list)
        for m in kd.get("markets", []):
            mt = re.search(r"-(\d{2}[A-Z]{3}\d{2})-", str(m.get("ticker","")))
            if mt:
                iso = ktok_to_iso(mt.group(1))
                if iso: kby[iso].append(m)
        for date in sorted(set(bycity[city]) & set(kby)):
            kb = sorted(kby[date], key=lambda m: (m.get("floor_strike") if m.get("floor_strike") is not None else -999))
            pm = sorted(bycity[city][date], key=lambda x: pm_lo(str(x.get("slug")).lower()))
            for i in range(min(len(kb), len(pm))):
                km, pmm = kb[i], pm[i]
                if pm_bounds(pmm.get("slug")) != kbounds(km):   # C4 boundary-NUMBER equality guard
                    continue                                    # non-identical floor/cap -> skip (settlement-identity)
                rows += 1
                ob = get(f"https://external-api.kalshi.com/trade-api/v2/markets/{km.get('ticker')}/orderbook").get("orderbook_fp", {})
                yb = [(fl(p), fl(s)) for p,s in ob.get("yes_dollars",[])]; nb = [(fl(p), fl(s)) for p,s in ob.get("no_dollars",[])]
                k_yb, k_yb_sz = (max(yb) if yb else (0.0,0)); nbid, nbid_sz = (max(nb) if nb else (0.0,0))
                k_ya = round(1-nbid,2) if nbid else None
                (p_yb,p_yb_sz),(p_ya,p_ya_sz) = pm_book(str(pmm.get("slug"))); time.sleep(0.3)
                cand = []
                if p_ya is not None and k_yb:
                    cand.append((round((k_yb-p_ya)-pfee(p_ya)-kfee(1-k_yb),3), "PMyes+KNo", int(min(p_ya_sz,k_yb_sz))))
                if k_ya is not None and p_yb is not None:
                    cand.append((round((p_yb-k_ya)-kfee(k_ya)-pfee(1-p_yb),3), "KYes+PMno", int(min(nbid_sz,p_yb_sz))))
                if cand:
                    net,dirn,sz = max(cand)
                    if net > 0 and sz > 0:
                        edges.append({"city":city,"date":date,"bucket":str(km.get("yes_sub_title") or "")[:14],
                                      "dir":dirn,"net":net,"size":sz})
    return edges, rows

def sports_snapshot(sports):
    games = [x for x in sports if x.get("marketType")=="moneyline" and x.get("gameStartTime")]
    stats = {"game_markets":len(games),"two_sided":0,"spreads":[],"top_depth":[]}
    for x in games[:40]:
        (b,bs),(a,asz) = pm_book(str(x.get("slug"))); time.sleep(0.2)
        if b is not None and a is not None:
            stats["two_sided"] += 1; stats["spreads"].append(round(a-b,3))
            stats["top_depth"].append(int(min(bs,asz)))
    if stats["spreads"]:
        s = sorted(stats["spreads"]); d = sorted(stats["top_depth"])
        stats["median_spread"] = s[len(s)//2]; stats["median_top_depth"] = d[len(d)//2]
    return stats

print(f"persistence probe start {now().isoformat()}  ({SNAPSHOTS} snaps x {INTERVAL}s)")
seen = collections.Counter(); netsum = collections.defaultdict(list); sizesum = collections.defaultdict(list)
sports_hist = []
for snap in range(SNAPSHOTS):
    t0 = time.time()
    pool = pull_open()
    climate = [x for x in pool if x.get("category")=="climate"]; sportsm = [x for x in pool if x.get("category")=="sports"]
    try: edges, rows = weather_snapshot(climate)
    except Exception as e: edges, rows = [], 0; print("  weather err:", str(e)[:80])
    try: sp = sports_snapshot(sportsm)
    except Exception as e: sp = {"_err":str(e)[:60]};
    ts = now().isoformat()
    for e in edges:
        k = (e["city"],e["date"],e["bucket"],e["dir"]); seen[k]+=1; netsum[k].append(e["net"]); sizesum[k].append(e["size"])
    sports_hist.append(sp)
    rec = {"ts":ts,"snap":snap,"weather_edges":edges,"weather_rows":rows,"sports":sp}
    with open(os.path.join(OUT,"persistence_log.jsonl"),"a") as f: f.write(json.dumps(rec)+"\n")
    tot = sum(e["net"]*e["size"] for e in edges)
    print(f"  [{snap+1}/{SNAPSHOTS} {ts[11:19]}] weather edges={len(edges)} est${tot:.2f}  "
          f"sports games={sp.get('game_markets','?')} 2sided={sp.get('two_sided','?')} "
          f"medspr={sp.get('median_spread','?')} meddepth={sp.get('median_top_depth','?')}")
    for e in sorted(edges, key=lambda e:-e["net"]*e["size"])[:6]:
        print(f"       {e['city']:7} {e['date']} {e['bucket']:13} {e['dir']:10} net{e['net']:+.3f} x{e['size']}")
    if snap < SNAPSHOTS-1:
        time.sleep(max(0, INTERVAL-(time.time()-t0)))

# summary
lines = ["CROSS-VENUE PERSISTENCE PROBE SUMMARY", f"generated {now().isoformat()}  snapshots={SNAPSHOTS} interval={INTERVAL}s","",
         "WEATHER edges (persistence = #snapshots seen / total):",""]
agg = 0.0
for k,c in sorted(seen.items(), key=lambda kv:-kv[1]):
    avgn = sum(netsum[k])/len(netsum[k]); avgs = sum(sizesum[k])/len(sizesum[k]); val = avgn*avgs; agg += val
    lines.append(f"  {k[0]:7} {k[1]} {k[2]:13} {k[3]:10} seen {c}/{SNAPSHOTS}  avg_net {avgn:+.3f}  avg_size {avgs:.0f}  ~${val:.2f}")
if not seen: lines.append("  (no positive-net weather edges in any snapshot)")
lines += ["", f"Sum of persistent-edge value (avg_net x avg_size, one day's weather): ~${agg:.2f}", "",
          "SPORTS (polymarket.us book liquidity, last snapshot):"]
sp = sports_hist[-1] if sports_hist else {}
lines.append(f"  game moneyline markets={sp.get('game_markets')}  two-sided={sp.get('two_sided')}  "
             f"median spread={sp.get('median_spread')}  median top-depth={sp.get('median_top_depth')} contracts")
lines += ["  (cross-venue sports arb matching deferred to a dedicated verified matcher.)"]
open(os.path.join(OUT,"persistence_summary.txt"),"w").write("\n".join(lines))
print("\n".join(lines))
print("\nDONE. Files: scripts/_data/persistence_log.jsonl , persistence_summary.txt")
