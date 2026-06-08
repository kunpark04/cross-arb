"""Resolve the NYC 74-75 question: are Kalshi & polymarket.us NYC buckets defined over the
IDENTICAL degF ranges? Prints both ladders with explicit boundaries (Kalshi floor/cap vs the
polymarket.us market DESCRIPTION text), auto-flags any mismatch, then recomputes the arb only
on truly-aligned buckets. Read-only."""
import json, os, re, urllib.request, urllib.error, time, math

UA = {"User-Agent":"Mozilla/5.0","Accept":"application/json"}
def get(url, tries=4):
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
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

DK, DP = "26JUN08", "2026-06-08"

# --- polymarket.us NYC markets (fresh) ---
allm, off = [], 0
while True:
    d = get(f"https://gateway.polymarket.us/v1/markets?closed=false&limit=500&offset={off}")
    pg = d.get("markets", []); allm += pg
    if len(pg) < 500 or len(allm) > 12000: break
    off += 500
pm = [x for x in allm if x.get("category")=="climate" and "nychigh" in str(x.get("slug","")).lower()
      and DP in str(x.get("slug",""))]

def pm_range(desc, slug):
    d = (desc or "").lower()
    nums = [int(n) for n in re.findall(r"(\d+)\s*f", d)]
    if "less than or equal" in d and "greater" not in d and nums: return (None, nums[0], f"<= {nums[0]}")
    if "greater than or equal" in d and "less than" not in d and nums: return (nums[0], None, f">= {nums[0]}")
    if len(nums) >= 2: return (min(nums), max(nums), f"{min(nums)}-{max(nums)}")
    if nums: return (None, nums[0], f"<= {nums[0]}")
    return (None, None, "??")

def pm_lo(slug):
    m=re.search(r"-lt(\d+)f",slug); m2=re.search(r"gte(\d+)",slug)
    return (int(m.group(1))-100) if m else (int(m2.group(1)) if m2 else 0)

def pm_book(slug):
    md=(get(f"https://gateway.polymarket.us/v1/markets/{slug}/book") or {}).get("marketData",{})
    bids=[(fl(x["px"]["value"]),fl(x["qty"])) for x in md.get("bids",[]) if x.get("px")]
    offs=[(fl(x["px"]["value"]),fl(x["qty"])) for x in md.get("offers",[]) if x.get("px")]
    return (max(bids) if bids else (None,0)), (min(offs) if offs else (None,0))

pm_rows=[]
for x in sorted(pm, key=lambda x: pm_lo(str(x.get("slug")).lower())):
    lo,hi,lab = pm_range(x.get("description"), str(x.get("slug")))
    (pb,pbs),(pa,pas)=pm_book(str(x.get("slug"))); time.sleep(0.25)
    pm_rows.append({"lo":lo,"hi":hi,"lab":lab,"slug":x.get("slug"),"ybid":pb,"yask":pa,"ybid_sz":pbs,"yask_sz":pas})

# --- Kalshi NYC buckets ---
kd = get(f"https://external-api.kalshi.com/trade-api/v2/markets?series_ticker=KXHIGHNY&limit=1000")
kb=[m for m in kd.get("markets",[]) if DK in str(m.get("ticker",""))]
def krange(m):
    fls,cap=m.get("floor_strike"),m.get("cap_strike")
    if fls is None and cap is not None: return (None, cap-1, f"<= {cap-1}")
    if cap is None and fls is not None: return (fls+1, None, f">= {fls+1}")
    return (fls,cap,f"{fls}-{cap}")
krows=[]
for m in sorted(kb, key=lambda m:(m.get("floor_strike") if m.get("floor_strike") is not None else -999)):
    lo,hi,lab=krange(m)
    ob=get(f"https://external-api.kalshi.com/trade-api/v2/markets/{m.get('ticker')}/orderbook").get("orderbook_fp",{}); time.sleep(0.25)
    yb=[(fl(p),fl(s)) for p,s in ob.get("yes_dollars",[])]; nb=[(fl(p),fl(s)) for p,s in ob.get("no_dollars",[])]
    kyb,kyb_sz=(max(yb) if yb else (0.0,0)); nbid,nbid_sz=(max(nb) if nb else (0.0,0))
    kya=round(1-nbid,2) if nbid else None
    krows.append({"lo":lo,"hi":hi,"lab":lab,"ybid":kyb,"yask":kya,"ybid_sz":kyb_sz,"nbid_sz":nbid_sz})

print(f"NYC {DP}: Kalshi buckets={len(krows)}  polymarket.us buckets={len(pm_rows)}\n")
print(f"{'idx':3} {'Kalshi degF':12} {'K yes b/a':12} | {'PM.us degF':12} {'PM yes b/a':12} | aligned?")
n=max(len(krows),len(pm_rows)); aligned_all=True
for i in range(n):
    k=krows[i] if i<len(krows) else None; p=pm_rows[i] if i<len(pm_rows) else None
    ka=f"{k['ybid']:.2f}/{k['yask'] if k and k['yask'] is not None else '--'}" if k else "--"
    pa=f"{p['ybid'] if p and p['ybid'] is not None else '--'}/{p['yask'] if p and p['yask'] is not None else '--'}" if p else "--"
    same = bool(k and p and k['lo']==p['lo'] and k['hi']==p['hi'])
    if not same: aligned_all=False
    print(f"{i:3} {(k['lab'] if k else '--'):12} {ka:12} | {(p['lab'] if p else '--'):12} {pa:12} | {'YES' if same else 'NO <<<'}")

print(f"\nBuckets identical across venues: {'YES — alignment good' if aligned_all and len(krows)==len(pm_rows) else 'NO — MISALIGNED (the NYC edge is an artifact)'}")

if aligned_all and len(krows)==len(pm_rows):
    print("\nRecomputed arb on aligned buckets:")
    for i in range(len(krows)):
        k,p=krows[i],pm_rows[i]
        cand=[]
        if p['yask'] is not None and k['ybid']:
            cand.append((round((k['ybid']-p['yask'])-pfee(p['yask'])-kfee(1-k['ybid']),3),"PMyes+KNo",int(min(p['yask_sz'],k['ybid_sz']))))
        if k['yask'] is not None and p['ybid'] is not None:
            cand.append((round((p['ybid']-k['yask'])-kfee(k['yask'])-pfee(1-p['ybid']),3),"KYes+PMno",int(min(k['nbid_sz'],p['ybid_sz']))))
        if cand:
            net,dirn,sz=max(cand); tag=" <== EDGE" if net>0 else ""
            print(f"  {k['lab']:8} {dirn:10} net {net:+.3f} x{sz}{tag}")
