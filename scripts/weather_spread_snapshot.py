"""Live cross-venue weather spread snapshot. For a co-listed city/day, build P(high<=X) on
each venue and compute the best net-of-fee cross-venue lock. Top-of-book only (PM.us gives no
public depth). Read-only."""
import json, os, re, urllib.request, urllib.error, time

UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
def get(url, tries=3):
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

def kfee(p):  # Kalshi taker fee/contract
    import math
    return math.ceil(0.07*100*p*(1-p))/100 if 0<p<1 else 0
def pfee(p):  # PM.us taker fee/contract (feeCoef 0.05)
    return 0.05*p*(1-p) if 0<p<1 else 0

CITY = {"miami": ("KXHIGHMIA", "miahigh"), "chicago": ("KXHIGHCHI", "mdwhigh")}
DATE_KALSHI = "26JUN08"; DATE_PM = "2026-06-08"

pm_all = json.load(open(os.path.join(os.path.dirname(__file__), "_data", "pmus_open_markets.json")))

for city, (kser, pmtag) in CITY.items():
    print("\n" + "="*86 + f"\n{city.upper()}  Kalshi {kser}  {DATE_KALSHI}  vs  polymarket.us {pmtag} {DATE_PM}\n" + "="*86)

    # ---- Kalshi bucket ladder ----
    d = get(f"https://external-api.kalshi.com/trade-api/v2/markets?series_ticker={kser}&limit=1000")
    kmk = [m for m in d.get("markets", []) if DATE_KALSHI in str(m.get("ticker",""))]
    kmk.sort(key=lambda m: (m.get("cap_strike") if m.get("cap_strike") is not None else 999))
    print(f"Kalshi {kser} {DATE_KALSHI}: {len(kmk)} markets")
    kcdf = []  # (X, P(high<=X) from cumulative yes-mid)
    cum = 0.0
    def f(v):
        try: return float(v)
        except Exception: return 0.0
    for m in kmk:
        yb, ya = f(m.get("yes_bid_dollars")), f(m.get("yes_ask_dollars"))
        ymid = (yb+ya)/2 if ya else yb
        cap, flo = m.get("cap_strike"), m.get("floor_strike")
        cum += ymid
        print(f"   {str(m.get('yes_sub_title') or m.get('subtitle'))[:14]:14} floor={str(flo):4} cap={str(cap):4} "
              f"yes {yb:.2f}/{ya:.2f}  bucketP~{ymid:.2f}  cumP(<=cap)~{cum:.2f}")
        if cap is not None:
            kcdf.append((cap, cum))

    # ---- PM.us thresholds ----
    pm = [x for x in pm_all if x.get("category")=="climate" and pmtag in str(x.get("slug","")).lower()
          and DATE_PM in str(x.get("slug",""))]
    print(f"\npolymarket.us {pmtag} {DATE_PM}: {len(pm)} threshold markets (re-fetching live quotes)")
    pmcdf = {}  # X -> P(high<=X) yes-mid
    for x in sorted(pm, key=lambda x:str(x.get("slug"))):
        slug = str(x.get("slug",""))
        mth = re.search(r"(lt|lte|gte|gt)(\d+)f", slug)
        if not mth: continue
        dir_, X = mth.group(1), int(mth.group(2))
        r = get(f"https://gateway.polymarket.us/v1/markets?id={x.get('id')}&includeBook=true")
        m0 = (r.get("markets") or [{}])[0] if isinstance(r, dict) else {}
        outs = json.loads(m0.get("outcomes","[]")) if isinstance(m0.get("outcomes"), str) else (m0.get("outcomes") or [])
        prices = json.loads(m0.get("outcomePrices","[]")) if isinstance(m0.get("outcomePrices"), str) else (m0.get("outcomePrices") or [])
        yi = outs.index("Yes") if "Yes" in outs else 0
        ni = outs.index("No") if "No" in outs else 1
        yask = float(prices[yi]) if yi < len(prices) and prices[yi] else None
        nask = float(prices[ni]) if ni < len(prices) and prices[ni] else None
        ybid = (1-nask) if nask is not None else None
        ymid = (yask+ybid)/2 if (yask is not None and ybid is not None) else (yask if yask is not None else None)
        # P(high<=X): if dir is lt/lte -> Yes==P(<=X); if gte/gt -> Yes==P(>=X) -> P(<=X-1)=1-Yes
        pX = ymid if dir_ in ("lt","lte") else (1-ymid if ymid is not None else None)
        keyX = X if dir_ in ("lt","lte") else X-1
        tag = f"Yes(<= {X})" if dir_ in ("lt","lte") else f"Yes(>= {X})"
        print(f"   {tag:12} yes_ask={str(yask):5} yes_bid={str(round(ybid,3) if ybid is not None else None):5} -> P(high<={keyX})~{round(pX,3) if pX is not None else None}")
        if pX is not None: pmcdf[keyX] = pX
        time.sleep(0.3)

    # ---- align & compute divergence + crude lock ----
    print(f"\n  ALIGN  X    Kalshi P(<=X)   PMus P(<=X)   |gap|   net-lock?(gap - fees)")
    kmap = dict(kcdf)
    for X in sorted(set(kmap) & set(pmcdf)):
        kp, pp = kmap[X], pmcdf[X]
        gap = abs(kp-pp)
        # crude: an edge needs gap to exceed round-trip taker fees on both legs at ~these prices
        fee = kfee(kp) + pfee(pp)
        edge = gap - fee
        flag = f"+{edge:.3f} EDGE" if edge>0 else f"{edge:.3f}"
        print(f"         {X:3}    {kp:.3f}          {pp:.3f}        {gap:.3f}   {flag}")
