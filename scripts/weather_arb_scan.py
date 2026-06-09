"""Clean depth-aware cross-venue weather arb scan (Kalshi x polymarket.us), bucket-aligned,
fee-netted, with fillable size at top-of-book. Uses PUBLIC books on both venues (no auth). Read-only.

NOTE: the Kalshi side is LIVE; the polymarket.us side reads a CACHED snapshot (_data/pmus_open_markets.json)
so it is only as fresh as that file. The date (DK/DP) now defaults to TODAY (override the constants for a
past day). For the live 5-city universe with a fresh pmus pull, use scripts/scan_all.py (the canonical scan)."""
import json, os, re, urllib.request, urllib.error, time, math, datetime as _dt

UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
def get(url, tries=4):
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429 and i < tries-1: time.sleep(3); continue
            return {"_err": e.code}
        except Exception as e:
            return {"_err": str(e)[:80]}
    return {}

def kfee(p): return math.ceil(0.07*100*p*(1-p))/100 if 0 < p < 1 else 0.0
def pfee(p): return round(0.05*p*(1-p), 4) if 0 < p < 1 else 0.0
def fl(v):
    try: return float(v)
    except Exception: return 0.0

CITIES = {"MIAMI": ("KXHIGHMIA", "miahigh"), "CHICAGO": ("KXHIGHCHI", "mdwhigh"),
          "NYC": ("KXHIGHNY", "nychigh"), "LA": ("KXHIGHLAX", "laxhigh"), "SF": ("KXHIGHTSFO", "sfohigh")}
_t = _dt.date.today()                                       # default to TODAY (Kalshi ticker token = YYMMMDD)
DK, DP = _t.strftime("%y%b%d").upper(), _t.isoformat()      # e.g. 26JUN09 / 2026-06-09 (edit for a past day)
pm_all = json.load(open(os.path.join(os.path.dirname(__file__), "_data", "pmus_open_markets.json")))

def pm_topbook(slug):
    b = get(f"https://gateway.polymarket.us/v1/markets/{slug}/book")
    md = b.get("marketData", {}) if isinstance(b, dict) else {}
    bids = [(fl(x["px"]["value"]), fl(x["qty"])) for x in md.get("bids", []) if x.get("px")]
    offs = [(fl(x["px"]["value"]), fl(x["qty"])) for x in md.get("offers", []) if x.get("px")]
    ybid = max(bids) if bids else (None, 0)          # best YES bid (price,size)
    yask = min(offs) if offs else (None, 0)          # best YES ask
    return ybid, yask

def pm_lo(slug):  # ascending sort key
    m = re.search(r"-lt(\d+)f$", slug)
    if m: return int(m.group(1)) - 100
    m = re.search(r"gte(\d+)", slug)
    return int(m.group(1)) if m else 0

for city, (kser, pmtag) in CITIES.items():
    print("\n" + "="*94 + f"\n{city}  {DK}   (Kalshi YES vs polymarket.us YES, same NWS bucket)\n" + "="*94)
    d = get(f"https://external-api.kalshi.com/trade-api/v2/markets?series_ticker={kser}&limit=1000")
    kb = [m for m in d.get("markets", []) if DK in str(m.get("ticker", ""))]
    kb.sort(key=lambda m: (m.get("floor_strike") if m.get("floor_strike") is not None else -999))
    pm = sorted([x for x in pm_all if x.get("category")=="climate" and pmtag in str(x.get("slug","")).lower()
                 and DP in str(x.get("slug",""))], key=lambda x: pm_lo(str(x.get("slug"))))
    n = min(len(kb), len(pm))
    print(f"  {'bucket':16} | {'Kalshi YES':14} | {'PM.us YES':14} | best cross-venue arb (net/contract, size)")
    print("  " + "-"*94)
    found = []
    for i in range(n):
        km, pmm = kb[i], pm[i]
        klabel = str(km.get("yes_sub_title") or km.get("subtitle") or "")[:14]
        ob = get(f"https://external-api.kalshi.com/trade-api/v2/markets/{km.get('ticker')}/orderbook").get("orderbook_fp", {})
        ybids = [(fl(p), fl(s)) for p, s in ob.get("yes_dollars", [])]
        nbids = [(fl(p), fl(s)) for p, s in ob.get("no_dollars", [])]
        k_ybid, k_ybid_sz = (max(ybids) if ybids else (0.0, 0))
        best_nbid, best_nbid_sz = (max(nbids) if nbids else (0.0, 0))
        k_yask = round(1 - best_nbid, 2) if best_nbid else None
        (p_ybid, p_ybid_sz), (p_yask, p_yask_sz) = pm_topbook(str(pmm.get("slug")))
        time.sleep(0.35)
        # Arb1: buy PM YES @ p_yask  +  buy Kalshi NO @ (1-k_ybid)
        net1 = sz1 = None
        if p_yask is not None and k_ybid:
            gross1 = k_ybid - p_yask
            net1 = round(gross1 - pfee(p_yask) - kfee(1 - k_ybid), 3)
            sz1 = int(min(p_yask_sz, k_ybid_sz))
        # Arb2: buy Kalshi YES @ k_yask  +  buy PM NO @ (1-p_ybid)
        net2 = sz2 = None
        if k_yask is not None and p_ybid is not None:
            gross2 = p_ybid - k_yask
            net2 = round(gross2 - kfee(k_yask) - pfee(1 - p_ybid), 3)
            sz2 = int(min(best_nbid_sz, p_ybid_sz))
        best = max([(net1 or -9, "PMyes+KNo", sz1), (net2 or -9, "KYes+PMno", sz2)])
        kq = f"{k_ybid:.2f}/{k_yask if k_yask is not None else '--'}"
        pq = f"{p_ybid if p_ybid is not None else '--'}/{p_yask if p_yask is not None else '--'}"
        verdict = f"{best[1]} net {best[0]:+.3f} x{best[2]}" if best[0] > -9 else "n/a"
        flag = "  <== EDGE" if best[0] and best[0] > 0 else ""
        print(f"  {klabel:16} | {kq:14} | {pq:14} | {verdict}{flag}")
        if best[0] and best[0] > 0:
            found.append((city, klabel, best))
    if found:
        print(f"\n  >> {len(found)} positive-net bucket(s) in {city}")
