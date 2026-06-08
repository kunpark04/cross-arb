"""Match Kalshi series to the polymarket.us open families (weather 5 cities, econ 5 indicators,
spot-check politics/culture). Uses saved kalshi_series.json. Prints ticker/title/settlement_sources
/fee_multiplier so we can audit source-match + station-match. Read-only."""
import json, os, re, collections

OUT = os.path.join(os.path.dirname(__file__), "_data")
ks = json.load(open(os.path.join(OUT, "kalshi_series.json")))
by_ticker = {s.get("ticker"): s for s in ks}

def src(s):
    ss = s.get("settlement_sources")
    if isinstance(ss, list):
        return " | ".join((x.get("name", "?") if isinstance(x, dict) else str(x)) for x in ss)
    return str(ss)

def show(s, tag=""):
    print(f"  [{s.get('category')}] {s.get('ticker'):26} feeM={str(s.get('fee_multiplier')):5} "
          f"{str(s.get('title'))[:46]:46} | src: {src(s)[:95]} {tag}")

print("="*100)
print("WEATHER — Kalshi 'Climate and Weather' HIGH/MAX temperature series (match PM.us 5 cities)")
print("="*100)
wx = [s for s in ks if (s.get("category") in ("Climate and Weather", "World"))
      and re.search(r"high|highest|maximum|max temp", (s.get("title","")).lower())]
# group by city words of interest
cities = ["san francisco", "los angeles", "chicago", "new york", "nyc", "miami"]
print(f"\nAll Kalshi high/max-temp series ({len(wx)}):")
for s in sorted(wx, key=lambda s: s.get("ticker","")):
    show(s)

print("\n\n" + "="*100)
print("ECON — Kalshi 'Economics' series matching PM.us macro (CPI YoY, U-3, GDP, NFP, Fed)")
print("="*100)
econ_pat = {
    "CPI YoY":        r"cpi.*y|inflation rate y|year.*cpi|consumer price",
    "Unemployment U3": r"unemploy|u-3|u3|jobless rate",
    "GDP":            r"gdp|gross domestic",
    "Nonfarm/Jobs":   r"payroll|nonfarm|jobs added|jobs report|employment situation",
    "Fed/FOMC":       r"fed |fomc|federal funds|rate decision|interest rate",
}
for label, pat in econ_pat.items():
    hits = [s for s in ks if s.get("category") in ("Economics", "Financials")
            and re.search(pat, (s.get("ticker","")+" "+s.get("title","")).lower())]
    print(f"\n-- {label}: {len(hits)} Kalshi series")
    for s in sorted(hits, key=lambda s: s.get("ticker",""))[:10]:
        show(s)

print("\n\n" + "="*100)
print("POLITICS/ELECTIONS — Kalshi overlap spot-check (governor/senate/primary)")
print("="*100)
pol = [s for s in ks if s.get("category") in ("Elections", "Politics")
       and re.search(r"governor|senate|house|primary|midterm", (s.get("title","")).lower())]
print(f"count={len(pol)}; sample sources:")
for s in sorted(pol, key=lambda s: s.get("ticker",""))[:8]:
    show(s)
print("  distinct-ish sources:", collections.Counter(src(s)[:40] for s in pol).most_common(5))

print("\n\n" + "="*100)
print("CULTURE — Kalshi overlap spot-check (Spotify / Netflix / Nobel)")
print("="*100)
cul = [s for s in ks if re.search(r"spotify|netflix|nobel|top artist|streams|box office", (s.get("title","")).lower())]
for s in sorted(cul, key=lambda s: s.get("ticker",""))[:12]:
    show(s)
