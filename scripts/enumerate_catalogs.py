"""Enumerate Kalshi + polymarket.us live catalogs; print compact aggregates for overlap audit.
Read-only. Writes raw pulls to scripts/_data/*.json for reuse; prints summaries only."""
import urllib.request, urllib.error, json, os, collections, re

UA = {"User-Agent": "Mozilla/5.0 (cross-arb-audit; research)"}
OUT = os.path.join(os.path.dirname(__file__), "_data")
os.makedirs(OUT, exist_ok=True)

def get(url, headers=None):
    req = urllib.request.Request(url, headers={**UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=45) as r:
        return json.load(r)

# ---------- polymarket.us ----------
def pull_pmus():
    all_m, offset, lim = [], 0, 500
    while True:
        try:
            d = get(f"https://gateway.polymarket.us/v1/markets?limit={lim}&offset={offset}")
        except urllib.error.HTTPError as e:
            print("  PM.US markets HTTPError", e.code, "at offset", offset); break
        m = d.get("markets", [])
        all_m.extend(m)
        if len(m) < lim or len(all_m) > 30000:
            break
        offset += lim
    return all_m

print("Pulling polymarket.us markets ...")
pmus = pull_pmus()
json.dump(pmus, open(os.path.join(OUT, "pmus_markets.json"), "w"))
print(f"PM.US total markets pulled: {len(pmus)}")

def is_live(m):
    return m.get("active") and not m.get("closed") and not m.get("archived")

live = [m for m in pmus if is_live(m)]
print(f"PM.US live (active & !closed & !archived): {len(live)}")

cat = collections.Counter(m.get("category") for m in live)
print("\nPM.US live categories:")
for c, n in cat.most_common():
    print(f"  {c!r:22} {n}")

# distinct tags
tagc = collections.Counter()
for m in live:
    for t in (m.get("tags") or []):
        tagc[t if isinstance(t, str) else (t.get("label") if isinstance(t, dict) else str(t))] += 1
print("\nPM.US live top tags:")
for t, n in tagc.most_common(30):
    print(f"  {t!r:28} {n}")

# keyword scan for the high-value non-sports families
def hits(words):
    out = []
    for m in live:
        blob = " ".join(str(m.get(k, "")) for k in ("question", "slug", "category", "description")).lower()
        if any(w in blob for w in words):
            out.append(m)
    return out

for label, words in [
    ("WEATHER/TEMP", ["temperature", "weather", "highest temp", "rain", "snow", "°f"]),
    ("CRYPTO", ["bitcoin", "ethereum", " btc", " eth", "crypto", "solana"]),
    ("ECON", ["cpi", "inflation", "fed ", "fomc", "interest rate", "gdp", "jobs", "payroll", "unemployment", "recession"]),
    ("POLITICS", ["election", "president", "senate", "congress", "governor", "nominee", "shutdown", "supreme court"]),
]:
    h = hits(words)
    print(f"\nPM.US live '{label}' matches: {len(h)}")
    for m in h[:8]:
        print(f"   - [{m.get('category')}] {str(m.get('question'))[:70]}  ({m.get('slug')})")

# show a sample non-sports market fully (structure for buckets)
nonsport = [m for m in live if m.get("category") != "sports"]
print(f"\nPM.US live NON-sports markets: {len(nonsport)}")
for m in nonsport[:15]:
    print(f"   - [{m.get('category')}] {str(m.get('question'))[:75]}  outcomes={m.get('outcomes')}")

# ---------- Kalshi ----------
print("\n\nPulling Kalshi series ...")
ks = get("https://external-api.kalshi.com/trade-api/v2/series").get("series", [])
json.dump(ks, open(os.path.join(OUT, "kalshi_series.json"), "w"))
print(f"Kalshi total series: {len(ks)}")

kcat = collections.Counter(s.get("category") for s in ks)
print("\nKalshi series categories:")
for c, n in kcat.most_common():
    print(f"  {c!r:24} {n}")

# settlement_sources shape
print("\nKalshi settlement_sources samples (by category of interest):")
def src_str(s):
    ss = s.get("settlement_sources")
    if not ss: return "(none)"
    if isinstance(ss, list):
        return "; ".join((x.get("name","?") if isinstance(x, dict) else str(x)) for x in ss)[:90]
    return str(ss)[:90]

seen = set()
for s in ks:
    c = (s.get("category") or "").lower()
    if c in ("climate and weather", "weather", "economics", "financials", "crypto", "sports", "politics", "world") and c not in seen:
        seen.add(c)
    # print a few weather/econ/crypto series with sources
keyword_series = [s for s in ks if re.search(r"high|temp|cpi|inflation|gdp|payroll|fed|fomc|unemploy|bitcoin|ethereum|nba|nfl|election", (s.get("ticker","")+s.get("title","")).lower())]
print(f"\nKalshi keyword-matched series: {len(keyword_series)} (showing settlement sources)")
for s in keyword_series[:50]:
    print(f"   [{s.get('category')}] {s.get('ticker'):24} {str(s.get('title'))[:42]:42} | src: {src_str(s)}")
