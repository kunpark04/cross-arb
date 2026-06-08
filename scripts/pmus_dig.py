"""Dig into polymarket.us: inspect flags/dates on the pulled set, scan ALL markets for
non-sports families, and probe server-side filters to find the OPEN catalog. Read-only."""
import urllib.request, urllib.error, json, os, collections

UA = {"User-Agent": "Mozilla/5.0 (cross-arb-audit; research)"}
OUT = os.path.join(os.path.dirname(__file__), "_data")

def get(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=45) as r:
        return json.load(r)

m = json.load(open(os.path.join(OUT, "pmus_markets.json")))
print(f"loaded {len(m)} pmus markets\n")

# flag value distributions
for f in ("active", "closed", "archived", "ep3Status", "marketType"):
    print(f"{f}:", dict(collections.Counter(x.get(f) for x in m).most_common(6)))

# date span
dates = sorted(str(x.get("endDate")) for x in m if x.get("endDate"))
print("\nendDate min:", dates[0] if dates else None, " max:", dates[-1] if dates else None)
sd = sorted(str(x.get("startDate")) for x in m if x.get("startDate"))
print("startDate min:", sd[0] if sd else None, " max:", sd[-1] if sd else None)

# category over ALL
print("\nALL categories:", dict(collections.Counter(x.get("category") for x in m).most_common()))

# keyword scan over ALL
def hits(words):
    out = []
    for x in m:
        blob = " ".join(str(x.get(k, "")) for k in ("question", "slug", "category", "description")).lower()
        if any(w in blob for w in words):
            out.append(x)
    return out
for label, words in [
    ("WEATHER", ["temperature", "weather", "rain", "snow", "degrees"]),
    ("CRYPTO", ["bitcoin", "ethereum", "btc", "eth ", "crypto", "solana"]),
    ("ECON", ["cpi", "inflation", "fomc", "interest rate", "gdp", "payroll", "unemployment", "recession", "jobs report"]),
    ("POLITICS", ["election", "president", "senate", "governor", "nominee", "shutdown"]),
]:
    h = hits(words)
    print(f"\nALL '{label}': {len(h)}")
    for x in h[:6]:
        print(f"   [{x.get('category')}] {str(x.get('question'))[:70]} | end={x.get('endDate')} closed={x.get('closed')}")

# probe server-side filters for the OPEN catalog
print("\n\n--- filter probes (count + first sample) ---")
probes = [
    "https://gateway.polymarket.us/v1/markets?closed=false&limit=500",
    "https://gateway.polymarket.us/v1/markets?active=true&closed=false&limit=500",
    "https://gateway.polymarket.us/v1/markets?order=endDate&ascending=false&limit=10",
    "https://gateway.polymarket.us/v1/markets?status=open&limit=10",
    "https://gateway.polymarket.us/v1/events?closed=false&limit=10",
    "https://gateway.polymarket.us/v1/events?active=true&closed=false&limit=300",
    "https://gateway.polymarket.us/v1/markets?category=weather&limit=10",
    "https://gateway.polymarket.us/v1/markets?category=crypto&limit=10",
]
for url in probes:
    try:
        d = get(url)
        key = "markets" if "markets" in d else ("events" if "events" in d else None)
        lst = d.get(key, []) if key else []
        cats = dict(collections.Counter(x.get("category") for x in lst).most_common())
        first = lst[0] if lst else {}
        print(f"\n{url}\n   {key}: {len(lst)}  cats={cats}")
        if first:
            print(f"   first: [{first.get('category')}] {str(first.get('question'))[:60]} end={first.get('endDate')} closed={first.get('closed')} active={first.get('active')}")
    except urllib.error.HTTPError as e:
        print(f"\n{url}\n   HTTP {e.code}: {e.read()[:150]}")
    except Exception as e:
        print(f"\n{url}\n   {type(e).__name__}: {e}")
