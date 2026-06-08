"""Verify the candidate PUBLIC depth endpoint gateway.polymarket.us/v1/markets/{slug}/book.
Test on live two-sided weather + sports markets; show whether real multi-level depth comes back."""
import json, os, urllib.request, urllib.error, time

UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
def get(url, tries=3):
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=25) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429 and i < tries-1: time.sleep(2); continue
            return e.code, e.read()[:200].decode("utf-8", "ignore")
        except Exception as e:
            return None, f"{type(e).__name__}: {str(e)[:80]}"
    return None, "x"

mk = json.load(open(os.path.join(os.path.dirname(__file__), "_data", "pmus_open_markets.json")))
# pick live two-sided weather (Miami/Chicago Jun 8) + a sports market
wx = [x for x in mk if x.get("category")=="climate" and "2026-06-08" in str(x.get("slug",""))
      and any(c in str(x.get("slug")).lower() for c in ("miahigh","mdwhigh"))]
sp = [x for x in mk if x.get("category")=="sports"][:1]
targets = wx[:6] + sp

for x in targets:
    slug = x.get("slug")
    for variant in (f"/v1/markets/{slug}/book",
                    f"/v1/markets/{slug}/orderbook",
                    f"/v1/book/{slug}"):
        st, body = get("https://gateway.polymarket.us" + variant)
        if st == 200:
            md = body.get("marketData") or body.get("market_data") or body
            bids = md.get("bids") or md.get("buys") or []
            offs = md.get("offers") or md.get("asks") or md.get("sells") or []
            print(f"\n200 {variant}")
            print(f"    keys={list(body.keys())[:12]}  bids={len(bids)} offers={len(offs)}")
            if bids: print(f"    top bids: {json.dumps(bids[:4], default=str)[:300]}")
            if offs: print(f"    top offers: {json.dumps(offs[:4], default=str)[:300]}")
            break
        else:
            print(f"{st} {variant} -> {str(body)[:90]}")
    time.sleep(0.5)
