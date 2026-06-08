"""Establish live order-book access on BOTH venues for weather + sports, and inspect market
structure (prices, sides, token/id, bucket strikes). Prerequisite for spread measurement. Read-only."""
import json, urllib.request, urllib.error, time

UA = {"User-Agent": "Mozilla/5.0 (cross-arb-audit; research)"}

def get(url, tries=3):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=40) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429 and i < tries-1:
                time.sleep(3); continue
            return e.code, (e.read()[:200].decode("utf-8", "ignore"))
        except Exception as e:
            return None, f"{type(e).__name__}: {e}"
    return None, "retries exhausted"

def jtrim(o, n=900):
    s = json.dumps(o, default=str)
    return s[:n] + (" …(trunc)" if len(s) > n else "")

print("#"*90, "\n# polymarket.us — market structure + book endpoints\n", "#"*90)
_, d = get("https://gateway.polymarket.us/v1/markets?closed=false&limit=500")
mk = d.get("markets", []) if isinstance(d, dict) else []
climate = next((x for x in mk if x.get("category") == "climate"), None)
sport  = next((x for x in mk if x.get("category") == "sports"), None)

for nm, m in (("CLIMATE", climate), ("SPORTS", sport)):
    if not m:
        print(f"\n[{nm}] none found"); continue
    print(f"\n[{nm}] id={m.get('id')} slug={m.get('slug')}")
    print(f"   question: {str(m.get('question'))[:80]}")
    for f in ("outcomes", "outcomePrices", "marketSides", "orderPriceMinTickSize", "minimumTradeQty", "feeCoefficient"):
        print(f"   {f}: {jtrim(m.get(f), 300)}")
    mid = m.get("id")
    for ep in (f"https://gateway.polymarket.us/v1/markets/{mid}",
               f"https://gateway.polymarket.us/v1/markets/{mid}/book",
               f"https://gateway.polymarket.us/v1/markets/{mid}/orderbook",
               f"https://gateway.polymarket.us/v1/book?market={mid}",
               f"https://gateway.polymarket.us/v1/orderbook?market={mid}",
               f"https://gateway.polymarket.us/v1/prices?market={mid}"):
        st, r = get(ep)
        tag = jtrim(r, 220) if st == 200 else str(r)[:120]
        print(f"   [{st}] {ep.split('polymarket.us')[1]}\n        -> {tag}")

print("\n\n", "#"*90, "\n# Kalshi — weather + sports market + orderbook\n", "#"*90)
# weather: an open Miami market
st, d = get("https://external-api.kalshi.com/trade-api/v2/markets?series_ticker=KXHIGHMIA&status=open&limit=3")
for m in (d.get("markets", []) if isinstance(d, dict) else [])[:2]:
    tk = m.get("ticker")
    print(f"\n[KALSHI WX] {tk} | {str(m.get('subtitle') or m.get('yes_sub_title'))[:40]} "
          f"floor={m.get('floor_strike')} cap={m.get('cap_strike')} "
          f"yes_bid={m.get('yes_bid_dollars')} yes_ask={m.get('yes_ask_dollars')} "
          f"no_bid={m.get('no_bid_dollars')} no_ask={m.get('no_ask_dollars')} liq={m.get('liquidity_dollars')}")
    time.sleep(1)
    st2, ob = get(f"https://external-api.kalshi.com/trade-api/v2/markets/{tk}/orderbook")
    print(f"   orderbook[{st2}]: {jtrim(ob, 300)}")

# sports: find an open sports event/market on Kalshi
time.sleep(1)
st, d = get("https://external-api.kalshi.com/trade-api/v2/events?status=open&limit=200")
evs = d.get("events", []) if isinstance(d, dict) else []
sportev = [e for e in evs if (e.get("category") or "").lower() == "sports"]
print(f"\n[KALSHI] open events sampled={len(evs)}; sports among them={len(sportev)}")
for e in sportev[:3]:
    print(f"   sports event: {e.get('event_ticker')} | {str(e.get('title'))[:50]} series={e.get('series_ticker')}")
