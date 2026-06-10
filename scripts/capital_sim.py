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
  • profit/arb = size x max(0, BOOK-AVG edge - --haircut - VOID), where book-avg ~ (open_net + 2c-boundary)/2 (the
                 edge DECAYS down the depth; touch edge is captured only on the touch pair). Counted ONCE per
                 market (re-detections of the same market = one held position, not N concurrent ones - review C8).
  • VOID/arb   = sports settlement-void expected cost (review C5; 0 for weather). The venues' postpone/void rules
                 diverge — MLB Kalshi waits <=2d vs pmus <=2wk -> a replay in that gap breaks the lock into a naked
                 leg. ~P(postpone)*P(2d-2wk gap)*loss (MLB ~0.26c, other sports ~0.10c; --void-mult 0 to disable).
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

def capturable(episodes, edge_min, window_min, liq_floor=1, max_age=None, drop_restart=True):
    """Capturable = big enough (open_net), persistent enough (duration), AND fillable: deep enough
    (open_c2 >= liq_floor), not on a stale quote (max book age at open <= max_age), and NOT
    restart-censored. A restart-censored episode has a truncated/unreliable duration and is often a
    book-initialization phantom — captured seconds after a resubscribe when one venue's book is still
    half-built (e.g. a flat c2==c1==c0 ladder against a just-snapshotted side), so its "edge" is an
    artifact, not a fillable arb. The liq + age + restart gates separate a real fillable arb from a
    phantom (review finding L2, [L20]). drop_restart=True matches analyze_persistence's own CAPTURABLE
    definition (which already excludes restart-censored); pass False only to deliberately include them."""
    out = []
    for e in episodes:
        if not (e["open_net"] >= edge_min and e["duration"] >= window_min and e["open_c2"] >= max(1, liq_floor)):
            continue
        if max_age and e.get("open_age") is not None and e["open_age"] > max_age:   # max_age 0/None = no age filter
            continue
        if drop_restart and e.get("censored") == "restart":   # restart-censored = book-init phantom (L20)
            continue
        out.append(e)
    return out

DEPTH_BOUNDARY_NET = 0.005   # net edge of the DEEPEST fillable pair (~2c gross threshold minus ~1.5c fees); the
                             # book-average realized edge across c2 contracts is ~(open_net + this)/2, not open_net.

# --- SPORTS settlement-void haircut (review C5 / research/sports-settlement-verification.md). A held cross-venue
#     sports pair is a clean lock ONLY if the game completes on schedule. The venues' void/postpone rules DIVERGE,
#     materially for MLB: Kalshi waits for a replay only if rescheduled <=2 days (else voids to a fair price), pmus
#     waits <=2 weeks (else last-traded) -> a game replayed in that 2d-2wk gap settles real-winner on pmus but a
#     void on Kalshi -> the lock breaks into a NAKED directional leg (ledger.py S5). We charge the expected cost.
MLB_POSTPONE_RATE = 0.013    # ~29 (2024) / 31 (2023) postponements per ~2430 games (mlbschedulegrid.com/rainouts)
P_GAP             = 0.4      # fraction of postponements rescheduled into the 2d-2wk asymmetric window (vs next-day
                             # doubleheaders <=2d which stay ALIGNED, or >2wk where both void). ESTIMATE - tune w/ data.
VOID_LOSS_FRAC    = 0.5      # when the lock breaks the hedge is gone (coin-flip on full notional); conservative
                             # expected-shortfall charge ~half notional. ESTIMATE.
SPORTS_VOID_RATE  = 0.005    # generic non-MLB sports void/walkover/no-contest divergence rate (esports abandonment,
                             # pre-match walkover). ESTIMATE - per-league rates are a TODO (0010).

def void_haircut(market, mult=1.0):
    """Expected per-contract settlement-void cost for a market (0 for weather; weather's void risk is the
    CLI-revision path, handled separately). Sports: P(postpone & replayed-in-gap) * loss_fraction, MLB-weighted."""
    m = str(market)
    if not m.startswith("aec-"):                  # weather (tc-) / unknown -> no sports-void term
        return 0.0
    parts = m.split("-")
    league = parts[1] if len(parts) > 1 else ""
    rate = MLB_POSTPONE_RATE if league == "mlb" else SPORTS_VOID_RATE
    return mult * rate * P_GAP * VOID_LOSS_FRAC

def one_per_market(cap):
    """Collapse re-detections of the SAME market into ONE held position. A market that OPENs->CLOSEs->re-OPENs
    is NOT N concurrent arbs -- under the hold-to-settlement base case you ENTER at the first open and hold, so
    the re-opens are the same position flickering, not new entries. Summing each re-detection's capital/profit
    as if simultaneous overstates both ~6-7x (review C8). Representative = the FIRST (earliest-open) episode -
    the conservative base case (its own entry edge + depth), profit counted ONCE."""
    rep = {}
    for e in cap:
        m = e["market"]
        if m not in rep or e["open_t"] < rep[m]["open_t"]: rep[m] = e
    return list(rep.values())

def simulate(cap, max_clip, haircut, offset_h, fixed_w=None, void_mult=1.0):
    """-> (intervals, peak_capital, avg_capital, daily_profit_sum). One interval PER MARKET (not per episode),
    profit is the BOOK-AVERAGE edge across the filled depth (trapezoid touch..2c-boundary) minus the expected
    settlement-void cost (sports only; C5). void_mult=0 disables the void term."""
    intervals, profit = [], 0.0
    for e in one_per_market(cap):                       # C8: de-double-count same-market re-detections
        size = min(e["open_c2"], max_clip)
        capital = size * max(0.1, 1.0 - e["open_net"])
        avg_edge = max(DEPTH_BOUNDARY_NET, (e["open_net"] + DEPTH_BOUNDARY_NET) / 2.0)  # walk-the-book decay
        vh = void_haircut(e["market"], void_mult)       # sports settlement-void expected cost (0 for weather)
        profit += size * max(0.0, avg_edge - haircut - vh)
        end = (e["open_t"] + fixed_w) if fixed_w else settle_t(e["market"], e["open_t"], offset_h)
        intervals.append((e["open_t"], end, capital))
    peak, avg = peak_and_avg(intervals)
    return intervals, peak, avg, profit


def report(episodes, edge_min, window_min, max_clip, haircut, offset_h, liq_floor, max_age, void_mult=1.0):
    out = []; P = out.append
    cap = capturable(episodes, edge_min, window_min, liq_floor=liq_floor, max_age=max_age)   # baseline = every +arb
    robust = capturable(episodes, max(edge_min, 0.01), max(window_min, 30),                  # illustrative robust subset
                        liq_floor=max(liq_floor, 10), max_age=(max_age or 10))
    if not episodes:
        return "no episodes — nothing to simulate."
    t0 = min(e["open_t"] for e in episodes); t1 = max(e["open_t"] for e in episodes)
    span_d = max((t1 - t0) / 86400.0, 1e-9)
    per_day = lambda n: n / span_d

    P("=" * 80)
    P(f"CAPITAL / THROUGHPUT MODEL   (span {span_d:.2f} d; baseline = EVERY positive-edge arb; thresholds are opt-in knobs)")
    if span_d < 1: P("  *** < 1 day of data: every per-day / capital figure is PRELIMINARY noise. Tool, not verdict. ***")
    P(f"  positive-edge arbs (net>0)         : {len(cap)}   ({per_day(len(cap)):.1f}/day)   "
      f"[filters: net>={edge_min*100:.1f}c, lasts>={window_min:.0f}s, c2>={liq_floor}, age<={max_age or 'off'}s]")
    P(f"    of those, ROBUST (>=1c, >=30s, c2>=10, fresh<=10s): {len(robust)}   "
      f"[the other {len(cap)-len(robust)} are smaller/thinner/stale-ish but STILL +money - NOT filtered out]")
    if not cap:
        return "\n".join(out) + "\n(no positive-edge arbs in the data yet)"

    sizes = [min(e["open_c2"], max_clip) for e in cap]
    P("")
    P(f"PER-ARB SIZE  (contracts = min(open depth c2, max-clip {max_clip}); ~$1 capital/contract)")
    P(f"  median {_pct(sizes,50):.0f}   p90 {_pct(sizes,90):.0f}   max {max(sizes):.0f}   (raw depth max c2 {max(e['open_c2'] for e in cap):.0f})")
    lay = [e["peak_c2"] / e["open_c2"] for e in cap if e["open_c2"] > 0]
    if lay: P(f"  layering headroom: median peak/open depth = {_pct(lay,50):.1f}x  (depth that appears AFTER open -> add-on room)")

    n_sports = sum(1 for e in one_per_market(cap) if str(e["market"]).startswith("aec-"))
    n_mlb = sum(1 for e in one_per_market(cap) if str(e["market"]).split("-")[1:2] == ["mlb"])
    if n_sports and void_mult:
        P("")
        P(f"SPORTS SETTLEMENT-VOID HAIRCUT  (C5; sports positions only — {n_sports} markets, {n_mlb} MLB)")
        P(f"  MLB void cost ~{void_haircut('aec-mlb-x', void_mult)*100:.2f}c/contract "
          f"(P(postpone){MLB_POSTPONE_RATE*100:.1f}% x P(2d-2wk gap){P_GAP:.1f} x loss{VOID_LOSS_FRAC:.1f}); "
          f"other sports ~{void_haircut('aec-atp-x', void_mult)*100:.2f}c. Subtracted from sports edge below "
          f"(--void-mult 0 to disable). A postponed MLB pair must be UNWOUND before Kalshi's 2-day window.")
    _, peak, avg, profit = simulate(cap, max_clip, haircut, offset_h, void_mult=void_mult)
    P("")
    P(f"CAPITAL (event-date settlement proxy, +{offset_h:.0f}h; one clip/arb, haircut {haircut*100:.1f}c)")
    P(f"  PEAK concurrent capital : ${peak:,.0f}    <- the initial bankroll to never miss a fill")
    P(f"  avg concurrent capital  : ${avg:,.0f}")
    P(f"  daily gross profit      : ${per_day(profit):,.0f}/day   -> {(per_day(profit)/peak*100 if peak else 0):.1f}%/day on peak capital")

    P("")
    P("FRONTIER  capital vs return as you raise the per-arb clip (saturates at book depth)")
    P("   max-clip   peak $cap   $profit/day   %/day")
    for clip in (50, 200, 1000, 5000, 10**9):
        _, pk, _, pf = simulate(cap, clip, haircut, offset_h, void_mult=void_mult)
        tag = "  (uncapped)" if clip == 10**9 else ""
        P(f"   {('inf' if clip==10**9 else clip):>8}   {pk:>9,.0f}   {per_day(pf):>11,.0f}   {(per_day(pf)/pk*100 if pk else 0):>5.1f}{tag}")

    P("")
    P(f"W-SENSITIVITY  peak capital vs assumed hold time (clip {max_clip}; settlement time is uncertain)")
    P("    hold      peak $cap")
    for w_h in (2, 4, 8, 12, 24):
        _, pk, _, _ = simulate(cap, max_clip, haircut, offset_h, fixed_w=w_h * 3600, void_mult=void_mult)
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
    P("CAVEATS: one position PER MARKET (re-detections collapsed, review C8); profit is the book-AVERAGE edge")
    P("across the filled depth (trapezoid touch..2c-boundary), NOT touch x depth; c2 is DISPLAYED depth (gross")
    P(">=2c), an upper bound on takeable size (never pinged); settlement is a slug-date proxy (hence the W-sweep);")
    P(f"latency + leg-fill risk are NOT modelled (--haircut defaults 0). At {span_d:.2f}d this is preliminary.")
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
    ints, peak, _, profit = simulate(cap, 500, 0.0, 28, void_mult=0)  # size capped 500; void term isolated off
    avg_edge = (0.02 + DEPTH_BOUNDARY_NET) / 2.0                      # book-average (trapezoid), NOT touch x depth
    assert abs(profit - 500 * avg_edge) < 1e-9                        # $6.25 (not the old $10 touch x depth)
    assert profit < 500 * 0.02, "walk-the-book decay must reduce profit below touch x depth"
    assert abs(peak - 500 * (1 - 0.02)) < 1e-6                        # $490 locked
    # C5: sports settlement-void haircut — MLB > other sports > 0 ; weather = 0 ; it reduces sports profit
    assert void_haircut("tc-temp-laxhigh-2026-06-09-gte73") == 0.0    # weather: no sports-void term
    assert void_haircut("aec-mlb-x-y-2026-06-10") > void_haircut("aec-atp-x-y-2026-06-10") > 0.0
    _, _, _, pf_void = simulate(cap, 500, 0.0, 28)                    # MLB market WITH void term (default mult=1)
    assert abs(pf_void - 500 * (avg_edge - void_haircut("aec-mlb-x-y-2026-06-10"))) < 1e-9
    assert pf_void < profit, "void haircut must reduce the MLB sports profit"
    # C8: 3 re-detections of the SAME market (overlapping, same settle) collapse to ONE position, not 3
    re3 = [{**ep[0], "open_t": t, "open_net": n} for t, n in [(0, 0.02), (50, 0.05), (90, 0.03)]]
    cap3 = capturable(re3, 0.01, 30); assert len(cap3) == 3
    i3, pk3, _, pf3 = simulate(cap3, 500, 0.0, 28, void_mult=0)
    assert len(i3) == 1, "same-market re-detections must collapse to ONE interval (no 3x concurrent capital)"
    assert abs(pk3 - 500 * (1 - 0.02)) < 1e-6, "peak capital is ONE position (first-entry rep), not 3 summed"
    assert abs(pf3 - 500 * (0.02 + DEPTH_BOUNDARY_NET) / 2.0) < 1e-9  # FIRST entry (0.02) counted ONCE, book-averaged
    # filtered out when below thresholds
    assert capturable(ep, 0.05, 30) == [] and capturable([{**ep[0], "duration": 5}], 0.01, 30) == []
    # clean-fillable gates: thin depth and stale books are dropped (L2)
    assert capturable(ep, 0.01, 30, liq_floor=1000) == []                  # c2 800 < 1000 -> thin
    stale = [{**ep[0], "open_age": 99}]
    assert capturable(stale, 0.01, 30, max_age=10) == []                   # 99s stale -> phantom
    assert len(capturable(stale, 0.01, 30, max_age=None)) == 1             # age filter off -> kept
    # L20: restart-censored episodes are book-init phantoms -> dropped by default (matches analyze_persistence)
    restart_ep = [{**ep[0], "censored": "restart"}]
    assert capturable(restart_ep, 0.01, 30) == []                          # restart-censored -> dropped
    assert len(capturable(restart_ep, 0.01, 30, drop_restart=False)) == 1  # opt-in to include
    print("  OK - peak/avg overlap, settlement proxy, size cap, profit/capital, threshold + liq/stale filters")
    print("self-test passed.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="cross-arb capital / throughput model")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--data-dir", default=os.path.join(os.path.dirname(__file__), "..", "..", "data", "cross-arb"))
    ap.add_argument("--edge-min", type=float, default=0.0, help="min net edge fraction (default 0 = every positive arb)")
    ap.add_argument("--window-min", type=float, default=0, help="min episode duration s (default 0 = any)")
    ap.add_argument("--max-clip", type=int, default=1000, help="max contracts per arb (depth-capped below this)")
    ap.add_argument("--haircut", type=float, default=0.0, help="latency/slippage haircut on net edge (fraction)")
    ap.add_argument("--settle-offset-h", type=float, default=28.0, help="settlement = event-date 00:00 UTC + this")
    ap.add_argument("--liq-floor", type=int, default=1, help="min open depth c2 (default 1 = no liquidity filter)")
    ap.add_argument("--max-age", type=float, default=0.0, help="max book staleness s at open (default 0 = no age filter)")
    ap.add_argument("--void-mult", type=float, default=1.0, help="scale the sports settlement-void haircut (C5; 0 = disable)")
    a = ap.parse_args()
    if a.selftest:
        _selftest(); sys.exit(0)
    import glob
    data_dir = os.path.abspath(a.data_dir)
    if not glob.glob(os.path.join(data_dir, "transitions-*.jsonl*")):
        print(f"no data at {data_dir} — run `pwsh deploy/pull-data.ps1` first, or `--selftest`."); sys.exit(0)
    recs, sessions = load(data_dir)
    print(f"loaded {len(recs)} transitions + {len(sessions)} restarts from {data_dir}\n")
    print(report(build_episodes(recs, sessions), a.edge_min, a.window_min, a.max_clip, a.haircut,
                 a.settle_offset_h, a.liq_floor, a.max_age, void_mult=a.void_mult))
