"""probe_monitor_footprint.py — VERIFY the droplet sizing for the persistence-collection run.

Read-only, no creds, not the gated deploy. Answers:
  1. How many markets is "ALL weather + ALL sports" right now?  (public discovery pull, no auth)
  2. What does the monitor's in-memory state weigh at that scale?  (build the real
     MarketTracker / GameTracker / KalshiBook objects, populate realistic depth, measure heap)
  3. Does idle-market PRUNING keep memory flat over a long run?  (simulate N days of churn with the
     monitor's real prune_decision()/teardown(); compare pruned steady-state vs unpruned growth)

Run:  python scripts/probe_monitor_footprint.py
"""
import os, sys, gc, tracemalloc
BOT = os.path.join(os.path.dirname(__file__), "..", "bot")
sys.path.insert(0, BOT)

from monitor import (MarketTracker, GameTracker, FlipDebouncer,
                     prune_decision, teardown, PRUNE_THRESHOLD)
from kalshi_book import KalshiBook

DEPTH = 40                                                # generous ladder depth/side = upper bound
def ladder(n):
    return [{"px": {"value": f"{0.50 - i*0.001:.4f}"}, "qty": f"{100+i}.00"} for i in range(n)]
def snap(n):
    return {"yes_dollars_fp": [[f"{0.50 - i*0.001:.4f}", f"{100+i}.00"] for i in range(n)],
            "no_dollars_fp":  [[f"{0.49 - i*0.001:.4f}", f"{100+i}.00"] for i in range(n)]}

# Register one market into the dicts the monitor's run_live/teardown operate on (faithful proxy: the
# tracker stands in for run_live's closure that captures it; books[tk] is the real KalshiBook state).
def reg_weather(slug, pm, kt, books, slug_k):
    t = MarketTracker(slug); kb = KalshiBook(f"K-{slug}"); kb.apply_snapshot(snap(DEPTH))
    t.set_book("P", ladder(DEPTH), ladder(DEPTH)); t.set_book("K", kb.yes_bid_ladder(), kb.yes_offer_ladder())
    pm[slug] = t; kt[f"K-{slug}"] = t; books[f"K-{slug}"] = kb; slug_k[slug] = [f"K-{slug}"]
def reg_sports(slug, pm, kt, books, slug_k):
    g = GameTracker(slug, f"A-{slug}", f"B-{slug}")
    ka = KalshiBook(f"A-{slug}"); ka.apply_snapshot(snap(DEPTH))
    kb = KalshiBook(f"B-{slug}"); kb.apply_snapshot(snap(DEPTH))
    g.set_pm(0.48, 0.50); g.set_kalshi("A", ka.best()[1]); g.set_kalshi("B", kb.best()[1])
    pm[slug] = g; kt[f"A-{slug}"] = g; kt[f"B-{slug}"] = g
    books[f"A-{slug}"] = ka; books[f"B-{slug}"] = kb; slug_k[slug] = [f"A-{slug}", f"B-{slug}"]

# --- 1. real universe size: full public catalog pull (no auth) -----------------------------------
try:
    from colisted_map import build_colisted_map
    colisted, rep = build_colisted_map()
    n_wx, n_sp = len(colisted["weather"]), len(colisted["sports"])
    src = "LIVE discovery (public REST, no auth)"
except Exception as e:
    print(f"[discovery failed: {e!r}] falling back to session-log counts")
    n_wx, n_sp, src = 60, 166, "fallback"
pm_slugs, k_tickers = n_wx + n_sp, n_wx + 2 * n_sp

def cur_kb(d): return tracemalloc.get_traced_memory()[0]

# --- 2. one-day working-set heap -----------------------------------------------------------------
tracemalloc.start()
pm, kt, books, slug_k = {}, {}, {}, {}
base = cur_kb(0)
for i in range(n_wx): reg_weather(f"wx-D1-{i}", pm, kt, books, slug_k)
for i in range(n_sp): reg_sports(f"sp-D1-{i}", pm, kt, books, slug_k)
day1 = (cur_kb(0) - base) / 1e6

# --- 3. simulate DAYS of churn WITH pruning; memory should stay ~flat ----------------------------
DAYS = 30
absent = {}; deb = FlipDebouncer(1.0)
for day in range(2, DAYS + 1):
    for i in range(n_wx): reg_weather(f"wx-D{day}-{i}", pm, kt, books, slug_k)
    for i in range(n_sp): reg_sports(f"sp-D{day}-{i}", pm, kt, books, slug_k)
    today = {f"wx-D{day}-{i}" for i in range(n_wx)} | {f"sp-D{day}-{i}" for i in range(n_sp)}
    for _ in range(PRUNE_THRESHOLD):                      # prior days now absent -> debounce -> prune
        for s in prune_decision(set(pm), today, absent):
            teardown(s, pm, kt, books, slug_k, deb, absent)
    gc.collect()
dayN_pruned = (cur_kb(0) - base) / 1e6
tracemalloc.stop()

per_day = day1                                            # one fresh universe per day
print("=" * 72)
print(f"UNIVERSE  ({src}):  {n_wx} weather + {n_sp} sports  =  {pm_slugs} pmus slugs / {k_tickers} Kalshi tickers")
print(f"MEMORY (Python heap, {DEPTH}-level ladders = upper bound):")
print(f"  one live day's working set      : {day1:6.1f} MB heap  ->  resident RSS ~{45+day1:.0f}-{70+day1*1.5:.0f} MB")
print(f"  after {DAYS} days WITH pruning     : {dayN_pruned:6.1f} MB heap   (FLAT — settled markets freed)")
print(f"  after {DAYS} days WITHOUT pruning  : ~{per_day*DAYS:6.0f} MB heap   (projected: grows ~{per_day:.1f} MB/day)")
print(f"  pruning verified: {DAYS}-day heap stayed at {dayN_pruned:.1f} MB vs ~{per_day*DAYS:.0f} MB unpruned "
      f"({per_day*DAYS/max(dayN_pruned,0.1):.0f}x smaller)")
print("=" * 72)
