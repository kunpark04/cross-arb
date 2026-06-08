"""Probe Kalshi + polymarket.us public market-data APIs: confirm reachability and JSON shape.
Read-only. Prints compact summaries (counts, keys, sample titles) — no full dumps."""
import urllib.request, urllib.error, json

UA = {"User-Agent": "Mozilla/5.0 (cross-arb-audit; research)"}

def get(url, headers=None):
    req = urllib.request.Request(url, headers={**UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status, json.load(r)

def probe(name, url):
    try:
        st, data = get(url)
        print(f"\n=== {name}  [{st}]  {url}")
        if isinstance(data, dict):
            print("  top-level keys:", list(data.keys())[:25])
            for k in ("data", "markets", "events", "series", "results", "items"):
                v = data.get(k)
                if isinstance(v, list):
                    print(f"  '{k}': {len(v)} items")
                    if v and isinstance(v[0], dict):
                        print(f"     item keys:", list(v[0].keys())[:30])
                        for f in ("title", "ticker", "question", "name", "slug", "category", "series_ticker"):
                            if f in v[0]:
                                print(f"     sample {f}:", str(v[0][f])[:80])
        elif isinstance(data, list):
            print(f"  list of {len(data)}")
            if data and isinstance(data[0], dict):
                print("  item keys:", list(data[0].keys())[:30])
    except urllib.error.HTTPError as e:
        body = e.read()[:300]
        print(f"\n=== {name}  HTTPError {e.code}  {url}\n     {body}")
    except Exception as e:
        print(f"\n=== {name}  ERROR  {url}\n     {type(e).__name__}: {e}")

# polymarket.us (US-legal venue) — try documented gateway + alternates
probe("PM.US /v1/markets", "https://gateway.polymarket.us/v1/markets?limit=5")
probe("PM.US /v1/markets (noparam)", "https://gateway.polymarket.us/v1/markets")
probe("PM.US /v1/events", "https://gateway.polymarket.us/v1/events?limit=5")

# Kalshi (US-legal) — public market data, no auth
probe("Kalshi /series", "https://external-api.kalshi.com/trade-api/v2/series")
probe("Kalshi /markets", "https://external-api.kalshi.com/trade-api/v2/markets?limit=5&status=open")
probe("Kalshi /events", "https://external-api.kalshi.com/trade-api/v2/events?limit=5&status=open")
