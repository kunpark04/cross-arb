"""scripts/account_sim.py — $500 ACCOUNT simulation (realized vs unrealized book) for the cross-arb.

Unlike capital_sim.py (which assumes UNLIMITED capital and reports the *peak* a bot would need),
this answers the owner's question literally: run a fixed bankroll forward through the observed edge
episodes and report, BACKTEST-STYLE:

  • how many positions we ENTERED (capital-constrained — skip when the $ is already deployed),
  • how many we EXITED  -> REALIZED  (settled inside the data window; capital + edge booked back),
  • how many are still LOCKED -> UNREALIZED (held to a settlement that lands after the window ends),
  • the ending account statement (cash / locked / realized PnL / unrealized locked-in edge).

EXIT = SETTLEMENT.  pmus FREEZES the order book at resolution (exit_liquidity.py: 10/10 resolved
markets had empty books), so there is NO early-exit — a locked pair is held to settlement. The edge
CLOSE in the book is NOT an exit (we still hold the hedged pair); only settlement frees the capital.

Hold-to-settlement, one position per market (review C8), book-average edge across filled depth, sports
settlement-void haircut (C5). Reuses the proven model in capital_sim.py / analyze_persistence.py.
READ-ONLY. `--selftest` for the offline check.

  python scripts/account_sim.py [--capital 500] [--max-clip 1000] [--edge-min 0] [--settle-offset-h 28]
"""
import os, sys, argparse, glob
sys.path.insert(0, os.path.dirname(__file__))
from analyze_persistence import load, build_episodes, _pct
from capital_sim import (capturable, one_per_market, settle_t, void_haircut,
                         DEPTH_BOUNDARY_NET)


def run_account(episodes, capital0, max_clip, edge_min, window_min, offset_h,
                haircut, void_mult, liq_floor, max_age):
    """Walk a FIXED bankroll forward through the capturable arbs in open-time order, recycling capital
    as positions settle. Returns a result dict (realized/unrealized split + account statement)."""
    cap = capturable(episodes, edge_min, window_min, liq_floor=liq_floor, max_age=max_age)
    cands = sorted(one_per_market(cap), key=lambda e: e["open_t"])          # one entry/market, chronological
    if not episodes:
        return None
    window_end = max(e["close_t"] if e.get("close_t") else e["open_t"] for e in episodes)

    cash = float(capital0)
    held = []            # open positions: dict(settle_t, capital, profit, market, cat, size)
    realized = []        # settled inside the window
    entered = []         # every position we opened
    skipped_capital = 0  # opportunities we could NOT afford (bankroll fully deployed)
    skipped_partial = 0  # entries we could only PARTIALLY fill vs the available depth

    def settle_due(now):
        nonlocal cash, held
        still = []
        for p in held:
            if p["settle_t"] <= now:
                cash += p["capital"] + p["profit"]     # payout = capital back + booked edge
                realized.append(p)
            else:
                still.append(p)
        held = still

    for e in cands:
        settle_due(e["open_t"])                        # free capital from anything that settled by now
        cost_per = max(0.1, 1.0 - e["open_net"])       # ~$0.98 paid per $1 payout pair
        want = min(e["open_c2"], max_clip)             # depth/clip-limited target size
        afford = int(cash // cost_per)                 # bankroll-limited size
        size = min(want, afford)
        if size < 1:
            skipped_capital += 1
            continue
        if size < want:
            skipped_partial += 1
        avg_edge = max(DEPTH_BOUNDARY_NET, (e["open_net"] + DEPTH_BOUNDARY_NET) / 2.0)
        vh = void_haircut(e["market"], void_mult)
        profit = size * max(0.0, avg_edge - haircut - vh)
        capital = size * cost_per
        cash -= capital
        pos = {"settle_t": settle_t(e["market"], e["open_t"], offset_h), "capital": capital,
               "profit": profit, "market": e["market"], "cat": e["cat"], "size": size}
        held.append(pos); entered.append(pos)

    settle_due(window_end)                             # final mark: realize anything settled by window end
    unrealized = held

    realized_pnl   = sum(p["profit"] for p in realized)
    unrealized_pnl = sum(p["profit"] for p in unrealized)
    locked_capital = sum(p["capital"] for p in unrealized)
    return {
        "capital0": capital0, "window_end": window_end,
        "candidates": len(cands), "entered": entered,
        "skipped_capital": skipped_capital, "skipped_partial": skipped_partial,
        "realized": realized, "unrealized": unrealized,
        "realized_pnl": realized_pnl, "unrealized_pnl": unrealized_pnl,
        "cash": cash, "locked_capital": locked_capital,
    }


def _cat_counts(positions):
    d = {}
    for p in positions:
        d[p["cat"]] = d.get(p["cat"], 0) + 1
    return "  ".join(f"{c}={n}" for c, n in sorted(d.items())) or "(none)"


def report(episodes, capital0, max_clip, edge_min, window_min, offset_h, haircut, void_mult,
           liq_floor, max_age):
    r = run_account(episodes, capital0, max_clip, edge_min, window_min, offset_h, haircut,
                    void_mult, liq_floor, max_age)
    if r is None:
        return "no episodes — nothing to simulate."
    t0 = min(e["open_t"] for e in episodes); t1 = max(e["open_t"] for e in episodes)
    span_d = max((t1 - t0) / 86400.0, 1e-9)
    eq = r["cash"] + r["locked_capital"] + r["realized_pnl"] + r["unrealized_pnl"]
    O = []; P = O.append
    P("=" * 78)
    P(f"$ {r['capital0']:,.0f} ACCOUNT SIM  (hold-to-settlement; exit = settlement; span {span_d:.2f} d)")
    if span_d < 1:
        P("  *** < 1 day of data — PRELIMINARY. And all PnL is PAPER/GROSS (see backtest caveats). ***")
    P("")
    P("ORDER FLOW")
    P(f"  capturable arbs (one/market)   : {r['candidates']}")
    P(f"  ENTERED (bankroll allowed)     : {len(r['entered'])}    [{_cat_counts(r['entered'])}]")
    P(f"  SKIPPED — capital fully deployed: {r['skipped_capital']}    "
      f"(of which partial-fill entries: {r['skipped_partial']})")
    P("")
    P("BOOK  (a position EXITS only at settlement — no early-exit, pmus freezes the book)")
    P(f"  REALIZED   (settled, exited)   : {len(r['realized']):>4}   [{_cat_counts(r['realized'])}]")
    P(f"  UNREALIZED (locked, still open): {len(r['unrealized']):>4}   [{_cat_counts(r['unrealized'])}]")
    if r["entered"]:
        pct_real = 100.0 * len(r["realized"]) / len(r["entered"])
        P(f"  -> {pct_real:.0f}% of entered positions realized within the window; "
          f"{100 - pct_real:.0f}% still locked.")
    P("")
    P("ACCOUNT STATEMENT  (paper / gross — fees+spread netted, execution frictions NOT)")
    P(f"  starting equity                : ${r['capital0']:>10,.2f}")
    P(f"  cash (free)                    : ${r['cash']:>10,.2f}")
    P(f"  capital locked in open pairs   : ${r['locked_capital']:>10,.2f}")
    P(f"  REALIZED PnL (booked)          : ${r['realized_pnl']:>10,.2f}   "
      f"({100*r['realized_pnl']/r['capital0']:+.2f}% on bankroll)")
    P(f"  UNREALIZED PnL (locked-in edge): ${r['unrealized_pnl']:>10,.2f}   "
      f"({100*r['unrealized_pnl']/r['capital0']:+.2f}% — realizes at settlement, barring leg-fail/void)")
    P(f"  ending equity (mark-to-edge)   : ${eq:>10,.2f}   "
      f"({100*(eq-r['capital0'])/r['capital0']:+.2f}% total)")
    P("")
    P("BACKTEST CAVEATS")
    P("  • PAPER: read-only phase, zero orders placed — every figure is simulated on observed quotes.")
    P("  • GROSS upper bound: fees+spread ARE netted; latency, leg-fill-failure, adverse selection, and")
    P("    slippage past displayed depth are NOT — realized would be lower (shadow_fill: ~55% leg-fail @2s).")
    P("  • ~100% paper 'win rate' is by construction (only +edge arbs entered); the real losers live in the")
    P("    un-modeled friction terms + the sports settlement-void tail (C5) + any settlement-identity break.")
    P("  • Settlement is a slug-date + {oh:.0f}h proxy; the realized/unrealized split moves with it (see sweep)."
      .format(oh=offset_h))
    P("=" * 78)
    return "\n".join(O)


def settlement_sweep(episodes, capital0, max_clip, edge_min, window_min, haircut, void_mult,
                     liq_floor, max_age):
    """How the realized/unrealized split moves with the (uncertain) settlement assumption."""
    O = ["SETTLEMENT SENSITIVITY  (realized vs locked as the hold assumption changes)",
         "   settle@evt+   realized   unrealized   realized$   unrealized$"]
    for oh in (6, 12, 28, 48, 96):
        r = run_account(episodes, capital0, max_clip, edge_min, window_min, oh, haircut,
                        void_mult, liq_floor, max_age)
        O.append(f"   {oh:>3}h        {len(r['realized']):>8}   {len(r['unrealized']):>10}   "
                 f"${r['realized_pnl']:>8,.2f}   ${r['unrealized_pnl']:>9,.2f}")
    return "\n".join(O)


def _selftest():
    print("account-sim self-test")
    # two same-day arbs; tiny bankroll forces a skip; nothing settles in-window -> all unrealized
    base = {"cat": "weather", "duration": 100, "open_c2": 400, "peak_c2": 400, "open_age": 0}
    eps = [
        {**base, "market": "tc-temp-x-2026-06-10-gte70", "open_t": 1000, "open_net": 0.02, "close_t": 1100},
        {**base, "market": "tc-temp-y-2026-06-10-gte70", "open_t": 2000, "open_net": 0.02, "close_t": 2100},
    ]
    # bankroll $50 @ ~$0.98/contract -> affords ~51 contracts -> fills FIRST (400 wanted -> 51), skips SECOND
    r = run_account(eps, 50, 1000, 0.0, 0, 28, 0.0, 0.0, 1, 0)
    assert r["entered"] == 1, r["entered"]
    assert r["skipped_capital"] == 1, r["skipped_capital"]
    assert r["skipped_partial"] == 1                          # first was depth-400 but only 51 affordable
    assert len(r["realized"]) == 0 and len(r["unrealized"]) == 1   # settle@+28h is after the window -> locked
    assert r["unrealized_pnl"] > 0 and r["realized_pnl"] == 0
    # generous bankroll -> both enter; still both unrealized (settle after window)
    r2 = run_account(eps, 100000, 1000, 0.0, 0, 28, 0.0, 0.0, 1, 0)
    assert r2["entered"] == 2 and r2["skipped_capital"] == 0
    assert len(r2["unrealized"]) == 2 and len(r2["realized"]) == 0
    # a dated slug always returns its ABSOLUTE event-date settle time (far future for 2026) -> unrealized here
    r3 = run_account(eps, 100000, 1000, 0.0, 0, 0.001, 0.0, 0.0, 1, 0)
    assert len(r3["realized"]) == 0                          # 2026 event-date settles ~1.78e9 >> window_end
    # a NO-DATE slug floors settlement to open_t + 1h -> lands inside a long synthetic window -> REALIZED
    eps_nd = eps + [{**base, "market": "nodate-arb-market", "open_t": 9000, "open_net": 0.02,
                     "close_t": 100000}]
    r4 = run_account(eps_nd, 100000, 1000, 0.0, 0, 28, 0.0, 0.0, 1, 0)
    assert any(p["market"] == "nodate-arb-market" for p in r4["realized"]), "no-date settles at open+1h, in-window"
    assert r4["realized_pnl"] > 0
    # equity conserved: ending equity = start + realized + unrealized edge
    eqs = r2["cash"] + r2["locked_capital"] + r2["realized_pnl"] + r2["unrealized_pnl"]
    assert abs(eqs - (100000 + r2["unrealized_pnl"])) < 1e-6
    print("  OK — capital constraint, skip-when-deployed, realized/unrealized split, equity conservation")
    print("self-test passed.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="cross-arb fixed-bankroll account sim (realized vs unrealized)")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--data-dir", default=os.path.join(os.path.dirname(__file__), "..", "..", "data", "cross-arb"))
    ap.add_argument("--capital", type=float, default=500.0, help="starting bankroll $ (default 500)")
    ap.add_argument("--max-clip", type=int, default=1000, help="max contracts per arb (depth/bankroll-capped below)")
    ap.add_argument("--edge-min", type=float, default=0.0, help="min net edge fraction (default 0 = every +arb)")
    ap.add_argument("--window-min", type=float, default=0.0, help="min episode duration s (default 0 = any)")
    ap.add_argument("--settle-offset-h", type=float, default=28.0, help="settlement = event-date 00:00 UTC + this")
    ap.add_argument("--haircut", type=float, default=0.0, help="latency/slippage haircut on net edge (fraction)")
    ap.add_argument("--void-mult", type=float, default=1.0, help="scale sports settlement-void haircut (0 = off)")
    ap.add_argument("--liq-floor", type=int, default=1, help="min open depth c2")
    ap.add_argument("--max-age", type=float, default=0.0, help="max book staleness s at open (0 = no filter)")
    ap.add_argument("--sweep", action="store_true", help="also print the settlement-assumption sensitivity")
    a = ap.parse_args()
    if a.selftest:
        _selftest(); sys.exit(0)
    data_dir = os.path.abspath(a.data_dir)
    if not glob.glob(os.path.join(data_dir, "transitions-*.jsonl*")):
        print(f"no data at {data_dir} — run `pwsh deploy/pull-data.ps1` first, or `--selftest`."); sys.exit(0)
    recs, sessions = load(data_dir)
    print(f"loaded {len(recs)} transitions + {len(sessions)} restarts from {data_dir}\n")
    eps = build_episodes(recs, sessions)
    print(report(eps, a.capital, a.max_clip, a.edge_min, a.window_min, a.settle_offset_h,
                 a.haircut, a.void_mult, a.liq_floor, a.max_age))
    if a.sweep:
        print()
        print(settlement_sweep(eps, a.capital, a.max_clip, a.edge_min, a.window_min,
                               a.haircut, a.void_mult, a.liq_floor, a.max_age))
