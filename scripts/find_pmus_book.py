"""Discover polymarket.us live order-book / depth endpoint. Dump a climate market's full
marketSides (instrument ids), then brute-force REST endpoint candidates across hosts. Read-only."""
import json, urllib.request, urllib.error, time

UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}

def get(url, tries=2):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, r.read().decode("utf-8", "ignore")
        except urllib.error.HTTPError as e:
            if e.code == 429 and i < tries-1:
                time.sleep(2); continue
            return e.code, e.read()[:160].decode("utf-8", "ignore")
        except Exception as e:
            return None, f"{type(e).__name__}: {str(e)[:90]}"
    return None, "x"

# get a Miami climate market from the full saved open set
import os
mk = json.load(open(os.path.join(os.path.dirname(__file__), "_data", "pmus_open_markets.json")))
mia = next((x for x in mk if x.get("category") == "climate" and "miami" in str(x.get("slug","")).lower()), None) \
      or next((x for x in mk if x.get("category") == "climate"), None)
print("climate market:", mia.get("id"), mia.get("slug"))
print("question:", str(mia.get("question"))[:80])
print("outcomes:", mia.get("outcomes"), " outcomePrices:", mia.get("outcomePrices"))
sides = mia.get("marketSides") or []
print(f"marketSides ({len(sides)}):")
for s in sides:
    print("   ", json.dumps({k: s.get(k) for k in ("id","identifier","description","price","long","marketSideType")}, default=str))
mid = mia.get("id"); slug = mia.get("slug")
sideids = [s.get("id") for s in sides]
print("\n--- endpoint brute force ---")
hosts = ["https://gateway.polymarket.us", "https://clob.polymarket.us",
         "https://ep3.polymarket.us", "https://api.polymarket.us", "https://data.polymarket.us"]
paths = [
    f"/v1/markets/{mid}/orderbook", f"/v1/markets/{mid}/book", f"/v1/markets/{mid}/depth",
    f"/v1/orderbook?marketId={mid}", f"/v1/orderbook?market_id={mid}", f"/v1/book?marketId={mid}",
    f"/v1/depth?marketId={mid}", f"/v1/orderbooks?marketIds={mid}", f"/v1/quotes?marketId={mid}",
    f"/v1/markets/{slug}/orderbook", f"/v1/markets/{mid}", f"/v1/markets?id={mid}&includeBook=true",
    f"/orderbook?marketId={mid}", f"/v1/ep3/orderbook?marketId={mid}",
]
if sideids:
    sid = sideids[0]
    paths += [f"/v1/marketSides/{sid}/orderbook", f"/v1/instruments/{sid}/orderbook",
              f"/v1/orderbook?marketSideId={sid}", f"/v1/orderbook?instrumentId={sid}"]
for h in hosts:
    reach = False
    for p in paths:
        st, body = get(h + p)
        if st == 200:
            print(f"  200  {h}{p}\n        -> {body[:260]}")
            reach = True
        elif st not in (404, None):
            print(f"  {st}  {h}{p}  -> {str(body)[:80]}")
            reach = True
    if not reach:
        # quick host liveness check
        st, _ = get(h + "/v1/markets?limit=1")
        print(f"  (host {h}: /v1/markets?limit=1 -> {st})")
