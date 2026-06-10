"""scripts/weather_depth.py - how much SIZE does the weather book actually hold? (read-only)

Weather is the one capital-efficient category (fast settle + liquid early-exit), so its constraint is SIZE.
This measures, per city x bucket for a date, the RAW book depth on both venues AND the edge-qualified crossable
depth, so the two don't get conflated.

MEASURED 2026-06-10 (5 co-listed cities, dir-P snapshot): the RESTING book is deep (~244k pmus / ~72k Kalshi
contracts), BUT the CROSSABLE depth -- pairs where the cross-venue prices actually CROSS so you can lock (Kalshi
YES bid > pmus YES ask) -- is only ~157 contracts at edge>=0 and ~1 at gross-2c, because weather prices are
EFFICIENT. So weather is DEEP-RESTING but EFFICIENT: lockable size at any instant is TINY (the "weather is thin"
finding stands -- thin on LOCKABLE edge/size, not on resting orders). Edges/lockable-size appear INTERMITTENTLY
(caught over time in the persistence data), not in an efficient snapshot. NOTE: min(total offers, total bids) is
NOT crossable depth -- the prices must cross; this walk enforces that. (Why only 5 cities: pmus lists only 5
weather markets total -- SF/LA/NYC/Miami/Chicago, all high-temp; Kalshi has ~22 but a cross-arb needs both, so
pmus is the cap. Displayed depth is an UPPER bound -- never pinged. dir-K is symmetric; measure both for totals.)

  python scripts/weather_depth.py [--date 2026-06-10]
"""
import os, sys, re, argparse, collections
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bot"))
from colisted_map import get, KAL, pm_catalog, pm_bounds, kbounds, WX, ktok_iso

KOB = KAL.replace("api.elections", "external-api")


def _fl(v):
    try: return float(v)
    except (TypeError, ValueError): return None


def pm_book(slug):
    md = (get(f"https://gateway.polymarket.us/v1/markets/{slug}/book") or {}).get("marketData", {})
    offs = [(_fl(x["px"]["value"]), _fl(x["qty"])) for x in md.get("offers", []) if x.get("px")]
    bids = [(_fl(x["px"]["value"]), _fl(x["qty"])) for x in md.get("bids", []) if x.get("px")]
    return [b for b in bids if b[0] is not None], [o for o in offs if o[0] is not None]


def k_orderbook(ticker):
    ob = get(f"{KOB}/{ticker}/orderbook").get("orderbook_fp", {})
    yb = [(_fl(p), _fl(s)) for p, s in ob.get("yes_dollars", [])]
    nb = [(_fl(p), _fl(s)) for p, s in ob.get("no_dollars", [])]
    return [x for x in yb if x[0] is not None], [x for x in nb if x[0] is not None]


def crossable(pm_offs, k_yes_bids, edge_floor=0.0):
    """Fillable PAIRS for dir P (YES@pmus offers + NO@Kalshi = lift Kalshi YES bids). Two-pointer walk; counts
    pairs whose gross marginal edge (1 - pm_ask - (1 - k_bid)) = (k_bid - pm_ask) >= edge_floor."""
    a = sorted(pm_offs)                                  # YES asks ascending
    b = sorted(k_yes_bids, reverse=True)                # YES bids descending -> NO asks (1-bid) ascending
    i = j = 0; ar = a[0][1] if a else 0; br = b[0][1] if b else 0; pairs = 0.0
    while i < len(a) and j < len(b):
        if (b[j][0] - a[i][0]) < edge_floor - 1e-9: break  # marginal gross edge below floor (eps guards float noise)
        step = min(ar, br)
        if step <= 1e-9: break
        pairs += step; ar -= step; br -= step
        if ar <= 1e-9: i += 1; ar = a[i][1] if i < len(a) else 0
        if br <= 1e-9: j += 1; br = b[j][1] if j < len(b) else 0
    return pairs


def _selftest():
    a = [(0.40, 10), (0.42, 20)]; b = [(0.55, 5), (0.50, 30)]   # YES bids 0.55,0.50 -> NO asks 0.45,0.50
    assert crossable(a, b, 0.0) == 30                            # raw crossable = min total
    assert crossable(a, b, 0.10) == 10                           # only the 0.55-bid vs 0.40-ask pair (edge 0.15>=.10)
    print("OK - weather_depth crossable walk (raw vs edge-floored)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="weather book-depth measurement")
    ap.add_argument("--date", default="2026-06-10")
    ap.add_argument("--edge-floor", type=float, default=0.02, help="edge-qualified depth threshold (gross)")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        _selftest(); sys.exit(0)
    clim = [m for m in pm_catalog() if m.get("category") == "climate"]
    tot = collections.Counter()
    print(f"WEATHER BOOK DEPTH (contracts) {a.date}, all 5 co-listed cities:\n")
    print(f"  {'city/bucket':22}{'pmus off':>9}{'kalshi yes-bid':>15}{'RAW pair':>10}{'pair@'+str(a.edge_floor):>10}")
    for city, kser in sorted(WX.items()):
        pm = [m for m in clim if re.search(rf"tc-temp-{city}high", str(m.get("slug", ""))) and a.date in str(m.get("slug", ""))]
        kd = get(f"{KAL}?series_ticker={kser}&limit=1000")
        kby = {}
        for m in kd.get("markets", []):
            dm = re.search(r"-(\d{2}[A-Z]{3}\d{2})", str(m.get("ticker", "")))
            if dm and ktok_iso(dm.group(1)) == a.date: kby[kbounds(m)] = m
        for m in sorted(pm, key=lambda m: (pm_bounds(m.get("slug"))[0] is None, pm_bounds(m.get("slug")))):
            km = kby.get(pm_bounds(m.get("slug")))
            if not km: continue
            _, offs = pm_book(str(m.get("slug"))); ybids, _ = k_orderbook(km.get("ticker"))
            raw = crossable(offs, ybids, 0.0); edged = crossable(offs, ybids, a.edge_floor)
            po = sum(q for _, q in offs)
            tot["raw"] += raw; tot["edged"] += edged
            print(f"  {city+' '+str(pm_bounds(m.get('slug'))):22}{po:>9.0f}{sum(q for _,q in ybids):>15.0f}{raw:>10.0f}{edged:>10.0f}")
    print(f"\n  TOTAL: RAW crossable ~{tot['raw']:.0f} contracts (~${tot['raw']:.0f})  |  "
          f"EDGE-qualified (>= {a.edge_floor} gross) ~{tot['edged']:.0f}")
    print("  => weather is DEEP (raw) but EFFICIENT (little at a >fee edge): the constraint is edge FREQUENCY, not size.")
