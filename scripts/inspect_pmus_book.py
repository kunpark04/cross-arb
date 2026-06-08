"""Inspect the polymarket.us public order book via ?includeBook=true. Dump structure for a
live climate market, then show best bid/ask + depth per side. Read-only."""
import json, os, urllib.request, urllib.error, time, re

UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
def get(url, tries=3):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429 and i < tries-1: time.sleep(2); continue
            return {"_err": e.code}
        except Exception as e:
            return {"_err": str(e)[:80]}
    return {}

mk = json.load(open(os.path.join(os.path.dirname(__file__), "_data", "pmus_open_markets.json")))
clim = [x for x in mk if x.get("category") == "climate"]
# prefer Miami/Chicago and markets that look two-sided (No price present)
def thr(x):
    m = re.search(r"lt(\d+)f", str(x.get("slug","")))
    return int(m.group(1)) if m else None
clim.sort(key=lambda x: ("miami" not in str(x.get("slug")).lower(), thr(x) or 0))

target = clim[0]
print("DUMP one includeBook response for:", target.get("slug"))
d = get(f"https://gateway.polymarket.us/v1/markets?id={target.get('id')}&includeBook=true")
m0 = (d.get("markets") or [{}])[0] if isinstance(d, dict) else {}
print("market keys:", list(m0.keys()))
# find book-ish fields
for k, v in m0.items():
    if any(t in k.lower() for t in ("book", "bid", "ask", "order", "level", "depth", "side")):
        print(f"  {k} = {json.dumps(v, default=str)[:600]}")

print("\n\n--- best bid/ask + depth across Miami & Chicago thresholds ---")
sample = [x for x in clim if any(c in str(x.get("slug")).lower() for c in ("miami","mdw","chicago"))][:12]
for x in sample:
    d = get(f"https://gateway.polymarket.us/v1/markets?id={x.get('id')}&includeBook=true")
    m0 = (d.get("markets") or [{}])[0] if isinstance(d, dict) else {}
    book = m0.get("book") or m0.get("orderBook") or m0.get("orderbook")
    q = str(x.get("question"))[:42]
    if not book:
        # fall back: print whatever price field exists
        print(f"  {q:44} thr={thr(x)} NO-BOOK outcomePrices={m0.get('outcomePrices')}")
        continue
    print(f"  {q:44} thr={thr(x)} book={json.dumps(book, default=str)[:240]}")
    time.sleep(0.4)
