"""Measure settlement timing (capital lockup) for the US-legal overlap families.
PM.us: from saved open markets (startDate/endDate). Kalshi: live pull open_time/expiration.
Prints per-family hold duration + remaining-to-settle + cadence signal. Read-only."""
import json, os, urllib.request, statistics as st
from datetime import datetime, timezone

OUT = os.path.join(os.path.dirname(__file__), "_data")
NOW = datetime.now(timezone.utc)
UA = {"User-Agent": "Mozilla/5.0 (cross-arb-audit; research)"}

def parse(s):
    if not s: return None
    try: return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception: return None

def days(a, b):
    if not a or not b: return None
    return (b - a).total_seconds() / 86400.0

def stats(vals):
    vals = [v for v in vals if v is not None]
    if not vals: return "n/a"
    return f"min={min(vals):.1f}d  median={st.median(vals):.1f}d  max={max(vals):.1f}d"

m = json.load(open(os.path.join(OUT, "pmus_open_markets.json")))

import re, collections
def fam(x):
    s = x.get("slug", "")
    return re.sub(r"[-_]?\d{4}-\d{2}-\d{2}.*$", "", re.sub(r"[-_]\d{8}.*$", "", s))

print("="*92)
print("polymarket.us — open->settle (endDate-startDate) and remaining (endDate-now)")
print(f"now = {NOW.isoformat()}")
print("="*92)
for cat in ("climate", "macro", "politics", "culture", "sports"):
    rows = [x for x in m if x.get("category") == cat]
    if not rows: continue
    dur = [days(parse(x.get("startDate")), parse(x.get("endDate"))) for x in rows]
    rem = [days(NOW, parse(x.get("endDate"))) for x in rows]
    # distinct settle dates -> cadence
    dates = sorted({str(x.get("endDate"))[:10] for x in rows})
    print(f"\n[{cat}]  n={len(rows)}  distinct settle-dates={len(dates)}  span {dates[0]}..{dates[-1]}")
    print(f"   listed lifetime (start->end): {stats(dur)}")
    print(f"   remaining now->settle:        {stats(rem)}")
    if cat in ("climate", "macro"):
        fams = collections.defaultdict(list)
        for x in rows: fams[fam(x)].append(x)
        for fk, xs in sorted(fams.items()):
            d = [days(parse(x.get("startDate")), parse(x.get("endDate"))) for x in xs]
            r = [days(NOW, parse(x.get("endDate"))) for x in xs]
            ed = sorted({str(x.get("endDate"))[:16] for x in xs})
            print(f"      {fk:24} n={len(xs):2}  lifetime {stats(d):42}  remaining {stats(r)}  settles~{ed[0]}")

# ---- Kalshi live timing for matched series ----
def get(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=45) as r:
        return json.load(r)

print("\n\n" + "="*92)
print("Kalshi — live open markets: open_time -> close/expiration (capital lockup)")
print("="*92)
series = {
    "Weather Miami": "KXHIGHMIA", "Weather Chicago": "KXHIGHCHI", "Weather NYC": "KXHIGHNY",
    "CPI YoY": "KXCPIYOY", "Unemployment U3": "KXU3", "GDP": "GDP",
    "Payrolls": "KXPAYROLLS", "Fed decision": "FEDDECISION",
}
for label, tk in series.items():
    try:
        d = get(f"https://external-api.kalshi.com/trade-api/v2/markets?series_ticker={tk}&status=open&limit=200")
        mk = d.get("markets", [])
        if not mk:
            print(f"\n{label:18} ({tk}): no open markets right now")
            continue
        opens = [parse(x.get("open_time")) for x in mk]
        exps = [parse(x.get("expiration_time") or x.get("close_time")) for x in mk]
        life = [days(o, e) for o, e in zip(opens, exps)]
        rem = [days(NOW, e) for e in exps]
        edates = sorted({str(x.get("expiration_time") or x.get("close_time"))[:10] for x in mk})
        print(f"\n{label:18} ({tk}): {len(mk)} open mkts; settle-dates {edates[0]}..{edates[-1]} ({len(edates)} distinct)")
        print(f"   open->expiration: {stats(life)}    now->expiration: {stats(rem)}")
    except Exception as e:
        print(f"\n{label:18} ({tk}): ERROR {type(e).__name__}: {e}")
