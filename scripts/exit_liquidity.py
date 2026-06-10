"""scripts/exit_liquidity.py - can you EARLY-EXIT a pmus position once the outcome is known? (read-only)

The capital case for sports hinged on early-exit: once the result is known, sell the winning leg at ~$1 and
redeploy, rather than wait for the far-future pmus finalization (endDate). This probe MEASURES whether that is
even possible: for each market it reads the live order book and the market state, and classifies whether a
RESOLVED-but-unfinalized market still has a book to sell into.

MEASURED 2026-06-09 -- and it is STRUCTURAL / venue-timing-driven, opposite for sports vs weather:
  * SPORTS (resolved 06-08): once pmus sets closed=true (AT the event's end), the book is EMPTY/frozen -- 10/10
    resolved markets had zero bids+offers (the only one still closed=false had a full book). => NO early-exit;
    capital is locked from resolution to endDate (~2 weeks).
  * WEATHER (event-day evening ~8pm ET): the OPPOSITE -- pmus closes weather LATE (endDate ~2am ET, well after
    the ~6pm high-lock), so there is an ~8h window where the outcome is known AND the book is live. The WINNING
    bucket has a deep bid near $1 (MDW 88-89F: bid 0.99 depth 22,340 ; LAX 72-73F: bid 0.98 depth 3,273); losing
    buckets sit at 0.01/no-bid. => weather DOES have a liquid early-exit (~1-2c discount) -- a bonus, since its
    natural settlement is only ~1.2d anyway.
CONCLUSION: weather is doubly capital-efficient (fast natural settle + an exit window); sports/econ are locked to
their far endDate. capital_velocity.py's early-exit column is achievable ONLY for weather, not sports/econ.

  python scripts/exit_liquidity.py --selftest
  python scripts/exit_liquidity.py [--date 2026-06-08] [--max 10]   # probe tracked markets from a past (resolved) date
"""
import os, sys, re, glob, gzip, json, argparse
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bot"))
from colisted_map import get

PM = "https://gateway.polymarket.us/v1/markets"


def _fl(v):
    try: return float(v)
    except (TypeError, ValueError): return None


def pm_book(slug):
    md = (get(f"{PM}/{slug}/book") or {}).get("marketData", {})
    bids = [(_fl(x["px"]["value"]), _fl(x["qty"])) for x in md.get("bids", []) if x.get("px") and _fl(x["px"]["value"]) is not None]
    offs = [(_fl(x["px"]["value"]), _fl(x["qty"])) for x in md.get("offers", []) if x.get("px") and _fl(x["px"]["value"]) is not None]
    return bids, offs


def pm_market(slug):
    for url in (f"{PM}?slug={slug}", f"{PM}/{slug}"):
        d = get(url)
        if isinstance(d, dict):
            if d.get("markets"): return d["markets"][0]
            if d.get("slug") or d.get("market"): return d.get("market", d)
    return {}


def classify(closed, bids, offs):
    """-> ('FROZEN'|'TRADEABLE'|'OPEN-UNRESOLVED', can_early_exit:bool)."""
    has_book = bool(bids or offs)
    if closed and not has_book:
        return "FROZEN", False                  # resolved + no book -> CANNOT sell the winner -> no early-exit
    if closed and has_book:
        return "TRADEABLE", True                # resolved + a book still up -> early-exit possible (rare)
    return "OPEN-UNRESOLVED", False             # not yet closed -> outcome not final, book is pre-resolution


def archive_slugs(date):
    out = []
    base = os.path.join(os.path.dirname(__file__), "..", "..", "data", "cross-arb")
    for p in glob.glob(os.path.join(base, f"transitions-{date}.jsonl*")):
        op = gzip.open(p, "rt", encoding="utf-8") if p.endswith(".gz") else open(p, encoding="utf-8")
        for ln in op:
            try: out.append(json.loads(ln)["market"])
            except Exception: pass
        op.close()
    return sorted(set(out))


def _selftest():
    assert classify(True, [], []) == ("FROZEN", False)             # resolved, empty book -> no early-exit
    assert classify(True, [(0.99, 100)], []) == ("TRADEABLE", True)  # resolved but book up -> can exit
    assert classify(False, [(0.8, 10)], [(0.82, 10)])[0] == "OPEN-UNRESOLVED"
    assert classify(False, [], [])[1] is False
    print("OK - exit_liquidity classify: FROZEN(no early-exit) / TRADEABLE / OPEN-UNRESOLVED")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="pmus early-exit / post-resolution book-liquidity probe")
    ap.add_argument("--date", default="2026-06-08", help="probe tracked markets from this (past, resolved) event date")
    ap.add_argument("--max", type=int, default=12)
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        _selftest(); sys.exit(0)
    slugs = archive_slugs(a.date)
    if not slugs:
        print(f"no tracked slugs for {a.date} in the archive (pull data first)."); sys.exit(0)
    print(f"EXIT-LIQUIDITY probe on {len(slugs)} tracked {a.date} markets (outcome known by now):\n")
    tally = {"FROZEN": 0, "TRADEABLE": 0, "OPEN-UNRESOLVED": 0}
    for s in slugs[:a.max]:
        m = pm_market(s); bids, offs = pm_book(s)
        state, exit_ok = classify(bool(m.get("closed")), bids, offs)
        tally[state] += 1
        bb = max(bids)[0] if bids else None; dep = sum(q for _, q in bids) if bids else 0
        det = "empty book" if not (bids or offs) else f"best bid {bb} depth {dep:.0f}"
        print(f"  {s[:46]:48} closed={str(m.get('closed')):5} end={str(m.get('endDate'))[:10]}  {state:16} ({det})")
    print(f"\n  {tally}")
    print("  FROZEN = resolved but the order book is empty -> CANNOT early-exit -> capital locked to endDate.")
    print("  If FROZEN dominates, the passive-hold lockup (~15d sports) is the reality, NOT same-day early-exit.")
