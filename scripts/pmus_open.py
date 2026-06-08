"""Pull the FULL open polymarket.us catalog (closed=false) and detail the non-sports families
(climate/macro/politics/culture): questions, outcomes (buckets), and settlement text from
each market description. Read-only; saves raw to _data/pmus_open_markets.json."""
import urllib.request, urllib.error, json, os, collections, re

UA = {"User-Agent": "Mozilla/5.0 (cross-arb-audit; research)"}
OUT = os.path.join(os.path.dirname(__file__), "_data")

def get(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=45) as r:
        return json.load(r)

# paginate all OPEN markets
allm, off, lim = [], 0, 500
while True:
    d = get(f"https://gateway.polymarket.us/v1/markets?closed=false&limit={lim}&offset={off}")
    page = d.get("markets", [])
    allm.extend(page)
    if len(page) < lim or len(allm) > 20000:
        break
    off += lim
json.dump(allm, open(os.path.join(OUT, "pmus_open_markets.json"), "w"))
print(f"OPEN pmus markets: {len(allm)}")
print("categories:", dict(collections.Counter(x.get('category') for x in allm).most_common()), "\n")

def clean(s):
    return re.sub(r"\s+", " ", str(s or "")).strip()

def family_key(x):
    # group markets into families: strip trailing date tokens from slug
    s = x.get("slug", "")
    return re.sub(r"[-_]?\d{4}-\d{2}-\d{2}.*$", "", re.sub(r"[-_]\d{8}.*$", "", s))

for catname in ("climate", "macro", "politics", "culture"):
    rows = [x for x in allm if x.get("category") == catname]
    fams = collections.defaultdict(list)
    for x in rows:
        fams[family_key(x)].append(x)
    print(f"\n{'='*90}\nCATEGORY '{catname}': {len(rows)} open markets, {len(fams)} families")
    for fk, xs in sorted(fams.items(), key=lambda kv: -len(kv[1])):
        ex = xs[0]
        print(f"\n  FAMILY {fk!r}  ({len(xs)} markets)")
        print(f"    e.g. question: {clean(ex.get('question'))[:90]}")
        print(f"    outcomes: {ex.get('outcomes')}")
        print(f"    endDate: {ex.get('endDate')}  feeCoef: {ex.get('feeCoefficient')}  minQty: {ex.get('minimumTradeQty')}")
        desc = clean(ex.get("description"))
        # surface the settlement-source sentence
        src_sent = ""
        for kw in ("National Weather Service", "NWS", "Weather Underground", "wunderground",
                   "Bureau of Labor", "BLS", "Bureau of Economic", "BEA", "Federal Reserve", "FOMC",
                   "Climate Report", "resolve", "source", "according to"):
            i = desc.lower().find(kw.lower())
            if i >= 0:
                src_sent = desc[max(0, i-60):i+160]
                break
        print(f"    desc[:240]: {desc[:240]}")
        if src_sent:
            print(f"    >> SOURCE CUE: ...{src_sent}...")
