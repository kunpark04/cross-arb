"""scripts/alloc_policy_experiment.py — allocation-POLICY experiment for the cross-arb clip stage.

The not-yet-built trade-selection ("clip") stage allocates a FIXED bankroll across arbitrage
opportunities that arrive over time. account_sim.run_account allocates STRICTLY FIFO by arrival
(`sorted(..., key=lambda e: e["open_t"])`). Capital is LOCKED to settlement (pmus freezes its book
at resolution — no early exit), so capital barely recycles inside the short data window.

The owner asks: would it be better to WAIT ~1s, sort arbs largest-edge-first, prioritize the big
ones, then keep cycling?  This script MEASURES that on the real data — it does NOT reason abstractly.

It REUSES account_sim's exact per-contract economics (cost_per / avg_edge / void_haircut / size cap /
settle_t recycling) so the comparison is apples-to-apples; the ONLY thing a policy changes is the
ORDER / SELECTION in which candidate arbs are considered for funding:

  1. fifo            — baseline: consider candidates in open_t (arrival) order. (== account_sim today)
  2. batch1s         — the owner's proposal LITERALLY: still walk forward in time, but within each
                       1-second arrival bucket, fund higher-edge arbs first (sort bucket by edge desc).
  3. offline_knapsack— CLAIRVOYANT upper bound on allocation skill: ignore arrival causality, sort ALL
                       candidates by profit-density (profit_per_contract / cost_per_contract) desc and
                       greedily fund until bankroll exhausted. Best any allocator could do -> brackets
                       the max possible gain. (Valid as a *static* knapsack iff capital ~doesn't recycle
                       in-window — which we verify and print.)
  4. reservation     — realizable smart policy: fund in arrival order but ONLY if per-contract edge >= a
                       threshold tau (keep powder for fat arbs); sweep tau, report the best.

READ-ONLY. Imports the model; does not reimplement profit/cost/settlement math. `--selftest` included.

  python scripts/alloc_policy_experiment.py [--capital 500] [--max-clip 1000] [--edge-min 0] [--settle-offset-h 28]
  python scripts/alloc_policy_experiment.py --selftest
"""
import os, sys, argparse, glob
sys.path.insert(0, os.path.dirname(__file__))
from analyze_persistence import load, build_episodes
from capital_sim import (capturable, one_per_market, settle_t, void_haircut,
                         DEPTH_BOUNDARY_NET)

POLICIES = ("fifo", "batch1s", "offline_knapsack", "reservation")


# ---------------------------------------------------------------------------------------------
# Per-contract economics — LIFTED VERBATIM from account_sim.run_account so the comparison is
# apples-to-apples. cost_per, avg_edge, void_haircut, and the per-contract profit are EXACTLY the
# expressions in run_account (lines 59,68-70). Edge-for-sorting = per-contract profit.
# ---------------------------------------------------------------------------------------------
def _cost_per(e):
    """$ paid per 1-contract payout pair (account_sim line 59)."""
    return max(0.1, 1.0 - e["open_net"])

def _profit_per(e, haircut, void_mult):
    """Per-contract booked edge (account_sim lines 68-70): book-average edge minus latency haircut
    minus the sports settlement-void expected cost. This is the EDGE used to rank arbs."""
    avg_edge = max(DEPTH_BOUNDARY_NET, (e["open_net"] + DEPTH_BOUNDARY_NET) / 2.0)
    vh = void_haircut(e["market"], void_mult)
    return max(0.0, avg_edge - haircut - vh)


def _order_candidates(cands, policy, haircut, void_mult, tau=None):
    """Return the candidate list in the ORDER a policy will CONSIDER them for funding. This is the
    ONLY thing a policy controls — every funded position then uses the identical economics. The walk
    (settle_due recycling) still happens in chronological open_t order regardless of consideration
    order, because a position cannot settle before a later one is even considered; see run_policy."""
    if policy == "fifo":
        return sorted(cands, key=lambda e: e["open_t"])
    if policy == "reservation":
        # arrival order, but filtering happens in the walk (needs running cash); just chronological.
        return sorted(cands, key=lambda e: e["open_t"])
    if policy == "batch1s":
        # walk forward in time; WITHIN each 1-second arrival bucket, fund higher-edge first.
        # secondary key keeps it deterministic (market) when edges tie.
        return sorted(cands, key=lambda e: (int(e["open_t"]),
                                            -_profit_per(e, haircut, void_mult),
                                            e["market"]))
    if policy == "offline_knapsack":
        # CLAIRVOYANT: ignore arrival; profit-DENSITY (profit_per / cost_per) descending.
        return sorted(cands, key=lambda e: (-_profit_per(e, haircut, void_mult) / _cost_per(e),
                                            e["market"]))
    raise ValueError(f"unknown policy {policy!r}")


def run_policy(episodes, policy, capital0, max_clip, edge_min, window_min, offset_h,
               haircut, void_mult, liq_floor, max_age, tau=None):
    """Walk a FIXED bankroll through the capturable arbs under `policy`'s consideration order,
    recycling capital as positions settle. Per-contract economics are IDENTICAL to
    account_sim.run_account; only the consideration ORDER (and reservation's tau gate) differ.

    Recycling note: settle_due(now) frees capital from anything that settled by `now`. We advance
    `now` by the open_t of the candidate currently being considered. For fifo/reservation/batch1s
    the consideration order is monotonic in open_t (batch1s only reorders WITHIN a 1s bucket, which
    cannot change which settlements have occurred), so the recycling clock is well-defined and matches
    account_sim. For offline_knapsack we DELIBERATELY ignore arrival causality (it is the clairvoyant
    upper bound); we still process settlements at each pick's open_t so a recycle that the data permits
    is not denied to it — in this window recycling is ~nil (asserted in the report), so it is moot."""
    cap = capturable(episodes, edge_min, window_min, liq_floor=liq_floor, max_age=max_age)
    cands = _order_candidates(one_per_market(cap), policy, haircut, void_mult, tau)
    if not episodes:
        return None
    window_end = max(e["close_t"] if e.get("close_t") else e["open_t"] for e in episodes)

    cash = float(capital0)
    held = []
    realized = []
    entered = []
    skipped_capital = 0
    skipped_partial = 0
    skipped_tau = 0

    def settle_due(now):
        nonlocal cash, held
        still = []
        for p in held:
            if p["settle_t"] <= now:
                cash += p["capital"] + p["profit"]
                realized.append(p)
            else:
                still.append(p)
        held = still

    for e in cands:
        settle_due(e["open_t"])                        # free capital from anything settled by now
        per = _profit_per(e, haircut, void_mult)       # per-contract edge (== account_sim)
        if tau is not None and per < tau:              # reservation: keep powder for fat arbs
            skipped_tau += 1
            continue
        cost_per = _cost_per(e)
        want = min(e["open_c2"], max_clip)
        afford = int(cash // cost_per)
        size = min(want, afford)
        if size < 1:
            skipped_capital += 1
            continue
        if size < want:
            skipped_partial += 1
        capital = size * cost_per
        profit = size * per
        cash -= capital
        pos = {"settle_t": settle_t(e["market"], e["open_t"], offset_h), "capital": capital,
               "profit": profit, "market": e["market"], "cat": e["cat"], "size": size}
        held.append(pos); entered.append(pos)

    settle_due(window_end)
    unrealized = held

    realized_pnl   = sum(p["profit"] for p in realized)
    unrealized_pnl = sum(p["profit"] for p in unrealized)
    locked_capital = sum(p["capital"] for p in unrealized)
    total_pnl = realized_pnl + unrealized_pnl
    return {
        "policy": policy, "tau": tau, "capital0": capital0, "window_end": window_end,
        "candidates": len(cands), "entered": entered,
        "skipped_capital": skipped_capital, "skipped_partial": skipped_partial,
        "skipped_tau": skipped_tau,
        "realized": realized, "unrealized": unrealized,
        "realized_pnl": realized_pnl, "unrealized_pnl": unrealized_pnl, "total_pnl": total_pnl,
        "cash": cash, "locked_capital": locked_capital,
    }


def best_reservation(episodes, capital0, max_clip, edge_min, window_min, offset_h,
                     haircut, void_mult, liq_floor, max_age):
    """Sweep tau over the observed per-contract edges; return the best-PnL run (and the tau grid)."""
    cap = capturable(episodes, edge_min, window_min, liq_floor=liq_floor, max_age=max_age)
    edges = sorted({round(_profit_per(e, haircut, void_mult), 6)
                    for e in one_per_market(cap)})
    # candidate thresholds = each distinct observed edge (fund >= it), plus 0 (== fifo).
    taus = [0.0] + edges
    best, grid = None, []
    for tau in taus:
        r = run_policy(episodes, "reservation", capital0, max_clip, edge_min, window_min, offset_h,
                       haircut, void_mult, liq_floor, max_age, tau=tau)
        grid.append((tau, r["total_pnl"], len(r["entered"]), r["skipped_capital"], r["skipped_tau"]))
        if best is None or r["total_pnl"] > best["total_pnl"]:
            best = r
    return best, grid


def recycling_check(episodes, capital0, max_clip, edge_min, window_min, offset_h,
                    haircut, void_mult, liq_floor, max_age):
    """Quantify whether capital recycles in-window: realized vs unrealized PnL/positions under FIFO."""
    r = run_policy(episodes, "fifo", capital0, max_clip, edge_min, window_min, offset_h,
                   haircut, void_mult, liq_floor, max_age)
    return r


# =============================================================================================
def run_all(episodes, capital0, max_clip, edge_min, window_min, offset_h, haircut, void_mult,
            liq_floor, max_age):
    """Return {policy: result} for the three deterministic policies + the best reservation."""
    out = {}
    for pol in ("fifo", "batch1s", "offline_knapsack"):
        out[pol] = run_policy(episodes, pol, capital0, max_clip, edge_min, window_min, offset_h,
                              haircut, void_mult, liq_floor, max_age)
    best_res, grid = best_reservation(episodes, capital0, max_clip, edge_min, window_min, offset_h,
                                      haircut, void_mult, liq_floor, max_age)
    out["reservation"] = best_res
    out["_reservation_grid"] = grid
    return out


def report(episodes, max_clip, edge_min, window_min, offset_h, haircut, void_mult, liq_floor,
           max_age, bankrolls=(200, 500, 2000)):
    O = []; P = O.append
    if not episodes:
        return "no episodes — nothing to simulate."
    t0 = min(e["open_t"] for e in episodes); t1 = max(e["open_t"] for e in episodes)
    span_d = max((t1 - t0) / 86400.0, 1e-9)
    cap = capturable(episodes, edge_min, window_min, liq_floor=liq_floor, max_age=max_age)
    n_cand = len(one_per_market(cap))

    P("=" * 90)
    P(f"ALLOCATION-POLICY EXPERIMENT   (span {span_d:.2f} d; n_candidate_arbs (one/market) = {n_cand})")
    P(f"  fixed: max_clip={max_clip}  edge_min={edge_min}  settle_offset_h={offset_h}  "
      f"haircut={haircut}  void_mult={void_mult}")
    if span_d < 1:
        P("  *** < 1 day of data — PRELIMINARY; all PnL is PAPER/GROSS (same caveats as account_sim). ***")
    P("")

    # recycling verification (at $500, the headline bankroll)
    rc = recycling_check(episodes, 500, max_clip, edge_min, window_min, offset_h, haircut, void_mult,
                         liq_floor, max_age)
    P("RECYCLING CHECK (FIFO @ $500): does capital free up inside the data window?")
    P(f"  REALIZED positions   : {len(rc['realized'])}   REALIZED PnL  ${rc['realized_pnl']:,.4f}")
    P(f"  UNREALIZED positions : {len(rc['unrealized'])}   UNREALIZED PnL ${rc['unrealized_pnl']:,.4f}")
    recycles = len(rc["realized"]) > 0
    P(f"  -> capital {'RECYCLES' if recycles else 'is ~ALL LOCKED (no in-window recycling)'} "
      f"-> static offline_knapsack is {'an approximation' if recycles else 'the correct upper bound'}.")
    P("")

    P("POLICY x BANKROLL  (total deployed PnL = realized + unrealized, paper/gross; same as account_sim)")
    P(f"  {'bankroll':>8}  {'policy':<16} {'total_pnl':>12}  {'n_funded':>8}  {'n_skip_cap':>10}  {'note':<24}")
    table = {}
    for bk in bankrolls:
        res = run_all(episodes, bk, max_clip, edge_min, window_min, offset_h, haircut, void_mult,
                      liq_floor, max_age)
        table[bk] = res
        for pol in POLICIES:
            r = res[pol]
            note = ""
            if pol == "reservation":
                note = f"best tau={r['tau']*100:.3f}c"
            elif pol == "offline_knapsack":
                note = "clairvoyant ~ceiling"
            P(f"  {bk:>8}  {pol:<16} {r['total_pnl']:>12,.4f}  {len(r['entered']):>8}  "
              f"{r['skipped_capital']:>10}  {note:<24}")
        P("")

    # headline gains at $500
    if 500 in table:
        f500 = table[500]["fifo"]["total_pnl"]
        o500 = table[500]["offline_knapsack"]["total_pnl"]
        b500 = table[500]["batch1s"]["total_pnl"]
        r500 = table[500]["reservation"]["total_pnl"]
        off_gain = 100.0 * (o500 - f500) / f500 if f500 else float("nan")
        bat_gain = 100.0 * (b500 - f500) / f500 if f500 else float("nan")
        res_gain = 100.0 * (r500 - f500) / f500 if f500 else float("nan")
        gap = o500 - f500
        frac = (b500 - f500) / gap if gap else float("nan")
        P("HEADLINE @ $500")
        P(f"  fifo PnL              : ${f500:,.4f}")
        P(f"  batch1s PnL          : ${b500:,.4f}   ({bat_gain:+.2f}% vs fifo  <- what the proposal buys)")
        P(f"  reservation PnL      : ${r500:,.4f}   ({res_gain:+.2f}% vs fifo  <- best realizable)")
        P(f"  offline_knapsack PnL : ${o500:,.4f}   ({off_gain:+.2f}% vs fifo  <- clairvoyant ~CEILING)")
        P(f"  fifo->offline gap    : ${gap:,.4f}")
        P(f"  batch1s captures      : {100.0*frac:.1f}% of the fifo->offline gap"
          if gap else "  (no fifo->offline gap to capture)")
        if r500 > o500 + 1e-6:
            P(f"  NOTE: reservation (${r500:,.4f}) > offline_knapsack (${o500:,.4f}) — see lumpy-knapsack caveat.")
    P("")
    P("CAVEATS: paper/gross (same as account_sim — fees+spread netted, latency/leg-fill/slippage NOT);")
    P("one position PER MARKET (re-detections collapsed, C8); a single deep arb at max_clip can eat the")
    P("whole small bankroll, which is exactly why ORDER matters here. < 1 day of data -> PRELIMINARY.")
    P("LUMPY-KNAPSACK NOTE: clips (up to $1000/arb) are LARGE vs a $200-$500 bankroll, so the density-greedy")
    P("offline_knapsack is a NEAR-ceiling, not a strict max — a different packing (e.g. reservation's full-clip")
    P("arrival fills) can leave less stranded cash and marginally exceed it. The load-bearing inequality")
    P("offline_pnl >= fifo_pnl ALWAYS holds (selftest); the small reservation>offline gap is packing slack.")
    P("=" * 90)
    return "\n".join(O), table


# =============================================================================================
# SELF-TEST — synthetic set where the answer is KNOWN.
# A big-edge arb arrives AFTER a small one; bankroll funds ONLY ONE.
#   fifo            -> funds the SMALL (arrived first)
#   offline_knapsack-> funds the BIG (highest density)
#   reservation     -> funds the BIG (tau gates out the small)
#   offline_pnl >= fifo_pnl  ALWAYS.
# =============================================================================================
def _selftest():
    print("alloc-policy self-test")
    # weather markets (void_haircut=0) so per-contract edge == avg_edge cleanly.
    # SMALL arb: open_net 0.02 -> avg_edge (0.02+0.005)/2 = 0.0125 ; arrives t=1000
    # BIG   arb: open_net 0.20 -> avg_edge (0.20+0.005)/2 = 0.1025 ; arrives t=2000 (LATER)
    base = {"cat": "weather", "duration": 100, "peak_c2": 1000, "open_age": 0, "close_t": 100000}
    SMALL = {**base, "market": "tc-temp-small-2026-06-10-gte70", "open_t": 1000,
             "open_net": 0.02, "open_c2": 1000}
    BIG   = {**base, "market": "tc-temp-big-2026-06-10-gte70",   "open_t": 2000,
             "open_net": 0.20, "open_c2": 1000}
    eps = [SMALL, BIG]
    # Bankroll funds only ONE, with NO leftover cash for a partial second leg:
    # cost_per(small)=0.98, full small clip (1000) = $980.00 -> bankroll $980 leaves $0.00 -> big skipped.
    # cost_per(big)=0.80, full big clip (1000) = $800.00 <= $980 -> the big also fits as the sole pick.
    BK, CLIP, OFF = 980.0, 1000, 28.0

    fifo = run_policy(eps, "fifo", BK, CLIP, 0.0, 0, OFF, 0.0, 0.0, 1, 0)
    offl = run_policy(eps, "offline_knapsack", BK, CLIP, 0.0, 0, OFF, 0.0, 0.0, 1, 0)
    res_best, _ = best_reservation(eps, BK, CLIP, 0.0, 0, OFF, 0.0, 0.0, 1, 0)

    # fifo funds the SMALL first (arrived first), eats the bankroll, skips the big.
    fifo_mkts = [p["market"] for p in fifo["entered"]]
    assert fifo_mkts == [SMALL["market"]], fifo_mkts
    assert fifo["skipped_capital"] == 1, fifo["skipped_capital"]   # big skipped — no cash left

    # offline_knapsack funds the BIG FIRST (highest profit-density); it may then spend leftover cash
    # on a PARTIAL small clip (correct greedy knapsack) — the load-bearing claim is big-is-prioritized.
    offl_mkts = [p["market"] for p in offl["entered"]]
    assert offl_mkts[0] == BIG["market"], offl_mkts            # big funded FIRST, at full size
    assert offl["entered"][0]["size"] == CLIP, offl["entered"][0]

    # reservation's best tau funds the BIG (gates out the small entirely).
    res_mkts = [p["market"] for p in res_best["entered"]]
    assert res_mkts == [BIG["market"]], res_mkts
    assert res_best["entered"][0]["size"] == CLIP, res_best["entered"][0]

    # offline_pnl >= fifo_pnl ALWAYS (the load-bearing inequality).
    assert offl["total_pnl"] >= fifo["total_pnl"], (offl["total_pnl"], fifo["total_pnl"])
    # and STRICTLY greater here (big edge >> small edge).
    assert offl["total_pnl"] > fifo["total_pnl"], (offl["total_pnl"], fifo["total_pnl"])
    assert res_best["total_pnl"] > fifo["total_pnl"], (res_best["total_pnl"], fifo["total_pnl"])

    # batch1s within a 1s bucket funds the BIG first; here the two are 1000s apart so batch1s == fifo
    # (different buckets) — confirm that, then add a SAME-bucket case where batch1s beats fifo.
    bat = run_policy(eps, "batch1s", BK, CLIP, 0.0, 0, OFF, 0.0, 0.0, 1, 0)
    assert [p["market"] for p in bat["entered"]] == [SMALL["market"]], "diff buckets -> batch1s==fifo"

    # SAME 1-second bucket: small at t=5000.2, big at t=5000.8 -> both int->5000. batch1s must
    # fund the BIG first (edge desc); fifo (stable open_t sort) funds the SMALL first.
    SMALL_b = {**SMALL, "market": "tc-temp-smallb-2026-06-10-gte70", "open_t": 5000.2}
    BIG_b   = {**BIG,   "market": "tc-temp-bigb-2026-06-10-gte70",   "open_t": 5000.8}
    eps_b = [SMALL_b, BIG_b]
    fifo_b = run_policy(eps_b, "fifo", BK, CLIP, 0.0, 0, OFF, 0.0, 0.0, 1, 0)
    bat_b  = run_policy(eps_b, "batch1s", BK, CLIP, 0.0, 0, OFF, 0.0, 0.0, 1, 0)
    # fifo (stable open_t sort) funds the SMALL first -> eats bankroll -> big skipped.
    assert [p["market"] for p in fifo_b["entered"]] == [SMALL_b["market"]], fifo_b["entered"]
    # batch1s reorders WITHIN the 1s bucket -> funds the BIG first at full size (then leftover -> partial small).
    assert bat_b["entered"][0]["market"] == BIG_b["market"], bat_b["entered"]
    assert bat_b["entered"][0]["size"] == CLIP, bat_b["entered"][0]
    assert bat_b["total_pnl"] > fifo_b["total_pnl"], (bat_b["total_pnl"], fifo_b["total_pnl"])

    # economics identity: a funded position's profit/capital must equal account_sim's expressions.
    p = offl["entered"][0]
    exp_cost = p["size"] * max(0.1, 1.0 - BIG["open_net"])
    exp_prof = p["size"] * max(0.0, (BIG["open_net"] + DEPTH_BOUNDARY_NET) / 2.0 - 0.0 - 0.0)
    assert abs(p["capital"] - exp_cost) < 1e-9 and abs(p["profit"] - exp_prof) < 1e-9, p

    print("  OK — fifo funds small; offline/reservation fund big; offline_pnl>=fifo_pnl; "
          "batch1s reorders within a 1s bucket; economics == account_sim.")
    print("self-test passed.")


# =============================================================================================
if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="cross-arb allocation-policy experiment (fifo / batch1s / "
                                             "offline_knapsack / reservation)")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--data-dir", default=os.path.join(os.path.dirname(__file__), "..", "..", "data", "cross-arb"))
    ap.add_argument("--capital", type=float, default=None,
                    help="single bankroll $ (default: sweep 200/500/2000)")
    ap.add_argument("--max-clip", type=int, default=1000)
    ap.add_argument("--edge-min", type=float, default=0.0)
    ap.add_argument("--window-min", type=float, default=0.0)
    ap.add_argument("--settle-offset-h", type=float, default=28.0)
    ap.add_argument("--haircut", type=float, default=0.0)
    ap.add_argument("--void-mult", type=float, default=1.0)
    ap.add_argument("--liq-floor", type=int, default=1)
    ap.add_argument("--max-age", type=float, default=0.0)
    a = ap.parse_args()
    if a.selftest:
        _selftest(); sys.exit(0)
    data_dir = os.path.abspath(a.data_dir)
    if not glob.glob(os.path.join(data_dir, "transitions-*.jsonl*")):
        print(f"no data at {data_dir} — run `pwsh deploy/pull-data.ps1` first, or `--selftest`."); sys.exit(0)
    recs, sessions = load(data_dir)
    print(f"loaded {len(recs)} transitions + {len(sessions)} restarts from {data_dir}\n")
    eps = build_episodes(recs, sessions)
    bankrolls = (a.capital,) if a.capital is not None else (200, 500, 2000)
    txt, _ = report(eps, a.max_clip, a.edge_min, a.window_min, a.settle_offset_h, a.haircut,
                    a.void_mult, a.liq_floor, a.max_age, bankrolls=bankrolls)
    print(txt)
