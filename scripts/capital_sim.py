"""scripts/capital_sim.py — capital + throughput model for the cross-arb.

Answers the two operating questions (NOT a go/no-go gate):
  1. HOW MUCH INITIAL CAPITAL?  A locked arb ties up capital from fill -> settlement, so peak concurrent
     capital = peak overlap of (capturable arb, held to its settlement). Little's Law in simulation form.
  2. HOW TO MAXIMIZE THROUGHPUT?  Capital is the constraint; the levers are per-arb SIZE (capped by book
     depth) and TOTAL capital (capped by peak concurrency). We sweep both -> a capital<->daily-return
     frontier + a saturation point, and print the INTRADAY arrival profile (verifies/kills the "edges
     cluster in the evening" hypothesis).

Reads Kalshi/data/cross-arb/ (transitions WITH the depth field + sessions.jsonl). Reuses the proven
episode reconstruction in analyze_persistence.py. READ-ONLY. `--selftest` for the offline check.

Model + assumptions (all knobs):
  • size/arb   = min(open depth c2 [contracts net-ish], --max-clip).  c2 = fillable while GROSS marginal
                 edge >= 2c (~net-positive after fees) — a conservative net-deployable proxy.
  • capital/arb= size x (1 - open_net) ~ ~$0.97/pair (you pay ~(1-edge) per $1 payout).
  • profit/arb = size x max(0, open_net - --haircut).  Locked at entry, outcome-independent at settlement.
  • hold       = open_t -> settlement proxy (event-date in the slug + --settle-offset-h). Uncertain, so a
                 W-SENSITIVITY sweep shows how capital scales with the assumed hold.
  • BASE CASE models ONE clip per capturable open; intra-episode LAYERING (add on WIDEN) is reported as
    headroom (peak vs open depth), not yet deployed — it would raise both capital and profit.

  python scripts/capital_sim.py --selftest
  python scripts/capital_sim.py [--edge-min 0.01] [--window-min 30] [--max-clip 1000] [--haircut 0] [--settle-offset-h 28]
"""
import os, sys, re, calendar, argparse
sys.path.insert(0, os.path.dirname(__file__))
from analyze_persistence import load, build_episodes, _pct

_DATE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")

def settle_t(market, open_t, offset_h):
    """Settlement proxy = event-date (from slug) midnight UTC + offset_h hours; floored at open_t + 1h."""
    m = _DATE.search(market or "")
    if m:
        y, mo, d = map(int, m.groups())
        s = calendar.timegm((y, mo, d, 0, 0, 0, 0, 0, 0)) + int(offset_h * 3600)
        if s > open_t: return s
    return open_t + 3600

def peak_and_avg(intervals):
    """intervals: [(start, end, amount)]. Peak concurrent amount (locks-before-frees on ties =
    conservative) + time-weighted average."""
    evs = []
    for s, e, a in intervals:
        if e <= s: e = s + 1
        evs += [(s, a), (e, -a)]
    if not evs: return 0.0, 0.0
    evs.sort(key=lambda x: (x[0], -x[1]))                 # at a tie, +amount (lock) before -amount (free)
    peak = cur = area = 0.0; last = evs[0][0]
    for t, da in evs:
        area += cur * (t - last); last = t
        cur += da; peak = max(peak, cur)
    span = evs[-1][0] - evs[0][0]
    return peak, (area / span if span > 0 else 0.0)

def capturable(episodes, edge_min, window_min):
    return [e for e in episodes if e["open_net"] >= edge_min and e["duration"] >= window_min and e["open_c2"] >= 1]

def simulate(cap, max_clip, haircut, offset_h, fixed_w=None):
    """-> (intervals, peak_capital, avg_capital, daily_profit_unscaled_sum)."""
    intervals, profit = [], 0.0
    for e in cap:
        size = min(e["open_c2"], max_clip)
        capital = size * max(0.1, 1.0 - e["open_net"])
        profit += size * max(0.0, e["open_net"] - haircut)
        end = (e["open_t"] + fixed_w) if fixed_w else settle_t(e["market"], e["open_t"], offset_h)
        intervals.append((e["open_t"], end, capital))
    peak, avg = peak_and_avg(intervals)
    return intervals, peak, avg, profit


def report(episodes, edge_min, window_min, max_clip, haircut, offset_h):
    out = []; P = out.append
    cap = capturable(episodes, edge_min, window_min)
    if not episodes:
        return "no episodes — nothing to simulate."
    t0 = min(e["open_t"] for e in episodes); t1 = max(e["open_t"] for e in episodes)
    span_d = max((t1 - t0) / 86400.0, 1e-9)
    per_day = lambda n: n / span_d

    P("=" * 80)
    P(f"CAPITAL / THROUGHPUT MODEL   (span {span_d:.2f} d; capturable = net>= {edge_min*100:.1f}c & lasts>= {window_min:.0f}s & has depth)")
    if span_d < 1: P("  *** < 1 day of data: every per-day / capital figure is PRELIMINARY noise. Tool, not verdict. ***")
    P(f"  capturable arbs : {len(cap)}   ({per_day(len(cap)):.1f}/day)")
    if not cap:
        return "\n".join(out) + "\n(no capturable arbs at these thresholds yet)"

    sizes = [min(e["open_c2"], max_clip) for e in cap]
    P("")
    P(f"PER-ARB SIZE  (contracts = min(open depth c2, max-clip {max_clip}); ~$1 capital/contract)")
    P(f"  median {_pct(sizes,50):.0f}   p90 {_pct(sizes,90):.0f}   max {max(sizes):.0f}   (raw depth max c2 {max(e['open_c2'] for e in cap):.0f})")
    lay = [e["peak_c2"] / e["open_c2"] for e in cap if e["open_c2"] > 0]
    if lay: P(f"  layering headroom: median peak/open depth = {_pct(lay,50):.1f}x  (depth that appears AFTER open -> add-on room)")

    _, peak, avg, profit = simulate(cap, max_clip, haircut, offset_h)
    P("")
    P(f"CAPITAL (event-date settlement proxy, +{offset_h:.0f}h; one clip/arb, haircut {haircut*100:.1f}c)")
    P(f"  PEAK concurrent capital : ${peak:,.0f}    <- the initial bankroll to never miss a fill")
    P(f"  avg concurrent capital  : ${avg:,.0f}")
    P(f"  daily gross profit      : ${per_day(profit):,.0f}/day   -> {(per_day(profit)/peak*100 if peak else 0):.1f}%/day on peak capital")

    P("")
    P("FRONTIER  capital vs return as you raise the per-arb clip (saturates at book depth)")
    P("   max-clip   peak $cap   $profit/day   %/day")
    for clip in (50, 200, 1000, 5000, 10**9):
        _, pk, _, pf = simulate(cap, clip, haircut, offset_h)
        tag = "  (uncapped)" if clip == 10**9 else ""
        P(f"   {('inf' if clip==10**9 else clip):>8}   {pk:>9,.0f}   {per_day(pf):>11,.0f}   {(per_day(pf)/pk*100 if pk else 0):>5.1f}{tag}")

    P("")
    P(f"W-SENSITIVITY  peak capital vs assumed hold time (clip {max_clip}; settlement time is uncertain)")
    P("    hold      peak $cap")
    for w_h in (2, 4, 8, 12, 24):
        _, pk, _, _ = simulate(cap, max_clip, haircut, offset_h, fixed_w=w_h * 3600)
        P(f"   {w_h:>3}h     {pk:>9,.0f}")

    P("")
    P("INTRADAY ARRIVAL  (capturable opens by UTC hour;  ET ~ UTC-4)   -- tests the 'evening cluster' claim")
    hist = [0] * 24
    for e in cap: hist[int((e["open_t"] // 3600) % 24)] += 1
    mx = max(hist) or 1
    for h in range(24):
        bar = "#" * int(round(20 * hist[h] / mx))
        P(f"   {h:02d}Z {hist[h]:>4} {bar}")
    by_cat = {}
    for e in cap: by_cat[e["cat"]] = by_cat.get(e["cat"], 0) + 1
    P(f"  by category: " + "  ".join(f"{c}={n}" for c, n in sorted(by_cat.items())))
    P("")
    P("CAVEATS: depth c2 is GROSS>=2c (~net-positive, refine with a fee model); settlement is a slug-date")
    P("proxy (hence the W-sweep); base case is one clip/arb (layering would add capital + profit); and at")
    P(f"{span_d:.2f}d this is preliminary. Re-run as data grows.")
    P("=" * 80)
    return "\n".join(out)


# ============================================================================================
def _selftest():
    print("capital-sim self-test")
    assert peak_and_avg([(0, 10, 100), (20, 30, 50)])[0] == 100        # non-overlap -> peak = max single
    assert peak_and_avg([(0, 10, 100), (5, 15, 50)])[0] == 150         # overlap [5,10] -> peak = sum
    pk, avg = peak_and_avg([(0, 10, 100)]); assert pk == 100 and abs(avg - 100) < 1e-9
    assert settle_t("aec-mlb-x-y-2026-06-10", 0, 28) == calendar.timegm((2026, 6, 10, 0, 0, 0, 0, 0, 0)) + 28 * 3600
    assert settle_t("nodate", 1000, 28) == 1000 + 3600                 # fallback
    # one capturable episode, clip caps size, profit + capital sane
    ep = [{"market": "aec-mlb-x-y-2026-06-10", "cat": "sports", "open_t": 0, "duration": 100,
           "open_net": 0.02, "open_c2": 800, "peak_c2": 1200}]
    cap = capturable(ep, 0.01, 30); assert len(cap) == 1
    _, peak, _, profit = simulate(cap, 500, 0.0, 28)                   # size capped 500
    assert abs(profit - 500 * 0.02) < 1e-9                             # $10
    assert abs(peak - 500 * (1 - 0.02)) < 1e-6                         # $490 locked
    # filtered out when below thresholds
    assert capturable(ep, 0.05, 30) == [] and capturable([{**ep[0], "duration": 5}], 0.01, 30) == []
    print("  OK - peak/avg overlap, settlement proxy, size cap, profit/capital, threshold filter")
    print("self-test passed.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="cross-arb capital / throughput model")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--data-dir", default=os.path.join(os.path.dirname(__file__), "..", "..", "data", "cross-arb"))
    ap.add_argument("--edge-min", type=float, default=0.01)
    ap.add_argument("--window-min", type=float, default=30)
    ap.add_argument("--max-clip", type=int, default=1000, help="max contracts per arb (depth-capped below this)")
    ap.add_argument("--haircut", type=float, default=0.0, help="latency/slippage haircut on net edge (fraction)")
    ap.add_argument("--settle-offset-h", type=float, default=28.0, help="settlement = event-date 00:00 UTC + this")
    a = ap.parse_args()
    if a.selftest:
        _selftest(); sys.exit(0)
    import glob
    data_dir = os.path.abspath(a.data_dir)
    if not glob.glob(os.path.join(data_dir, "transitions-*.jsonl*")):
        print(f"no data at {data_dir} — run `pwsh deploy/pull-data.ps1` first, or `--selftest`."); sys.exit(0)
    recs, sessions = load(data_dir)
    print(f"loaded {len(recs)} transitions + {len(sessions)} restarts from {data_dir}\n")
    print(report(build_episodes(recs, sessions), a.edge_min, a.window_min, a.max_clip, a.haircut, a.settle_offset_h))
